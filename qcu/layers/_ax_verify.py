"""AX action verification: detect silent no-ops after AXPress / AXValue set.

Why this exists
---------------
``AXUIElementPerformAction(elem, "AXPress")`` can return err=0 even when the
target app did nothing — most commonly when:
- the menu item is disabled (silently accepted; we pre-check ``enabled``),
- the AX provider is a stub (Electron without ``AXManualAccessibility``
   activated reliably returns ok from a no-op),
- the action is implemented but the receiver's hit-test rejected it.

Without diffing the AX state around the action, the agent has no way to tell
"effect happened" from "IPC delivered to a no-op". That cost ZCode multiple
task failures ("Save" reported ok=true, dialog never appeared). This module
fixes it.

Design (per ZCode review on 2026-08-03)
----------------------------------------
- **No upfront sleep.** First sample fires ~50ms after the action; subsequent
  polls every ``poll_ms`` ms. Verified loops short-circuit the moment a
  signal matches, so the average cost on native AX apps is 150-250ms.
- **Window per signal class.** Fast signals (toggle AXValue, fill string)
  get 800ms; toolbar buttons 1500ms; menu/open-panel/menu modal 2500ms
  (SwiftUI cold launch may take 1.5-2s). Configurable per caller.
- **6 signal classes** (see ``SignalClass``): value flip, value set, value
  delta, children +/-, target element death, focused/window change.
- **Hashed string capture**: store ``(len, sha1(value))`` to detect any
  change rather than just the prefix, at the same byte cost as a 256-char
  truncation. ``StringSnap`` is the unit.
- **Three-state verdict**: ``yes`` (verified) / ``no`` (window elapsed with
  zero signal) / ``n/a`` (action type not verifiable, e.g. press_key).

External references
-------------------
The "post-action poll + early exit" pattern is what Apple's own Accessibility
Inspector uses internally; the per-action signal matrix mirrors what the
``AXObserver`` module would have surfaced if we trusted it (we don't — see
the AXObserver long-shot result in the changelog: native AX notifications are
laggy and Electron stub-noops them).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Optional


# ---------------------------------------------------------------------------
# Telemetry: append a single NDJSON line per verify call so real usage
# accumulates n>50 naturally without instrumented tests. Disable via
# ``QCU_VERIFY_NO_TELEMETRY=1`` for environments that don't want a file.
# ---------------------------------------------------------------------------


def _telemetry_path() -> Optional[str]:
    if os.environ.get("QCU_VERIFY_NO_TELEMETRY"):
        return None
    # Reuse QCU_HOME so tests under QCU_HOME=/tmp/... don't pollute the user.
    base = os.environ.get("QCU_HOME") or os.path.expanduser("~/.qcu")
    p = os.path.join(base, "verify_telemetry.jsonl")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        return p
    except Exception:
        return None


def _record_telemetry(verdict: "VerdictResult", signal: SignalClass) -> None:
    p = _telemetry_path()
    if p is None:
        return
    row = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "signal": signal.value,
        "verified": verdict.verified.value,
        "matched_at_ms": verdict.matched_at_ms,
        "samples": verdict.samples_taken,
        "elapsed_ms": round(verdict.elapsed_ms, 1),
    }
    try:
        with open(p, "a") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        # Telemetry must never break the actual operation.
        pass


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DEFAULT_FIRST_POLL_MS = 50     # First sample after the action
DEFAULT_POLL_MS = 100          # Interval between subsequent samples
TREE_NODE_SAMPLE_CAP = 200     # cap on per-snapshot children depth-walk
# Per-class windows (ms). Tuned by data collected on 2026-08-03:
#   - fill (value_set) on TextEdit: 10/10 trials, median matched 52ms,
#     max 55ms. 400ms gives 7x headroom without leaving ~400ms on the
#     table for the average fill task.
#   - menu_item AXPress on System Settings: 7/7 trials, median 256ms,
#     max 466ms. 2500ms is conservative for SwiftUI modal cold launch
#     (printing dialogs, About panels) which we haven't directly measured.
#
# When this table is changed, update tests/test_ax_verify.py.
WINDOW_BY_CLASS: dict[str, int] = {
    "value_flip": 400,         # was 800; same family as value_set
    "value_set": 400,          # was 800; data: median 52ms max 55ms
    "value_delta": 400,        # was 800; same family as value_set
    "children_change": 1500,   # untested; keep modal-bounded default
    "button_press": 1200,      # was 1500; data shows ≤500ms
    "menu_open": 2500,         # was 2500; max observed 466ms, but modal
                               # cold launch is untested — keep wide
    "element_death": 1500,     # untested; keep
    "window_change": 2500,     # same as menu_open (same signal source)
    "n/a": 0,
}


class Verdict(str, Enum):
    """Three-state operation result so callers can tell "transport failed"
    (``LayerResult.ok=False``) from "transport ok but effect unverifiable"
    (``verified="n/a"``) from "ok but no observable change"
    (``verified="no"``)."""

    YES = "yes"
    NO = "no"
    NA = "n/a"        # action type not amenable to verification
    SKIPPED = "skipped"  # caller suppressed verify


class SignalClass(str, Enum):
    """The signal channel to watch, picked by action type."""

    VALUE_FLIP = "value_flip"            # checkbox/radio: AXValue 0↔1
    VALUE_SET = "value_set"              # fill: AXValue equals expected text
    VALUE_DELTA = "value_delta"          # slider/stepper: number changed
    CHILDREN_CHANGE = "children_change"  # modal/sheet open/close
    ELEMENT_DEATH = "element_death"      # close/dismiss: target gone
    WINDOW_CHANGE = "window_change"      # menu item switches panel
    BUTTON_PRESS = "button_press"        # generic toolbar/AXButton: any signal
    MENU_OPEN = "menu_open"              # menu item opens panel/menu
    NA = "n/a"


# ---------------------------------------------------------------------------
# State snapshots — what we capture before/after the action
# ---------------------------------------------------------------------------

def hash_string(value: Any) -> tuple[int, str]:
    """Capture (len, sha1_hex_first_8_chars) for a value of any type.

    Why (len, sha1) instead of str truncated to 256 chars:
    - Same payload size (~20 bytes after JSON)
    - Resists false-positives on "first 256 chars same, tail differs"
    - Caller compares before/after pairs in O(1)
    """
    if value is None:
        return (0, "")
    s = str(value)
    if not s:
        return (0, "")
    return (len(s), hashlib.sha1(s.encode("utf-8")).hexdigest()[:16])


@dataclass
class Snapshot:
    """AX state captured before/after an action."""

    value_hash: tuple[int, str] = (0, "")
    value_raw: Any = None                  # only kept for value_set comparison
    children_count: int = 0                # capped at TREE_NODE_SAMPLE_CAP
    children_count_capped: bool = False    # True if cap hit
    focused: Optional[bool] = None
    element_alive: bool = True             # False = element no longer resolvable
    window_title_hash: tuple[int, str] = (0, "")
    window_count: int = 0
    window_text_hash: tuple[int, str] = (0, "")
    timestamp: float = 0.0

    @classmethod
    def empty(cls) -> "Snapshot":
        return cls(timestamp=time.perf_counter())


# ---------------------------------------------------------------------------
# Diff judgment
# ---------------------------------------------------------------------------

@dataclass
class VerdictResult:
    verified: Verdict
    signal: Optional[SignalClass] = None
    matched_at_ms: Optional[float] = None        # ms after the action
    samples_taken: int = 0
    elapsed_ms: float = 0.0
    diagnostics: dict[str, Any] = field(default_factory=dict)


def snapshot(target: Any, ax_getter: Callable[..., Any], *, app: Any = None) -> Snapshot:
    """Capture an AX state snapshot for ``target``.

    ``ax_getter`` is the layer's own PyObjC read path (usually
    ``qcu.layers.desktop_ax._ax_get``). We import lazily inside the function
    body to keep the module importable without ApplicationServices.
    """
    snap = Snapshot(timestamp=time.perf_counter())

    # Element death probe: the cheapest check first.
    try:
        _, role = ax_getter(__import__("ApplicationServices").AXUIElementCopyAttributeValue,
                            target, "AXRole")
        # err==0 means the element is still alive; any non-zero err means
        # its backing disposed and we treat that as death.
        snap.element_alive = (role is not None)
    except Exception:
        snap.element_alive = False

    if not snap.element_alive:
        return snap

    # Targets expose different attributes per role; fetch what we can
    # without raising.
    for attr in ("AXValue",):
        try:
            _, val = ax_getter(__import__("ApplicationServices").AXUIElementCopyAttributeValue,
                               target, attr)
            snap.value_hash = hash_string(val)
            snap.value_raw = val
            break
        except Exception:
            pass

    # Children count — recurses shallowly, capped.
    try:
        _, kids = ax_getter(__import__("ApplicationServices").AXUIElementCopyAttributeValue,
                            target, "AXChildren")
        kids = list(kids or [])
        n = len(kids)
        capped = False
        if n > TREE_NODE_SAMPLE_CAP:
            n = TREE_NODE_SAMPLE_CAP
            capped = True
        snap.children_count = n
        snap.children_count_capped = capped
    except Exception:
        pass

    # Focused flag on the target itself.
    try:
        _, f = ax_getter(__import__("ApplicationServices").AXUIElementCopyAttributeValue,
                         target, "AXFocused")
        snap.focused = bool(f) if f is not None else None
    except Exception:
        pass

    # Window order is not target identity: providers can put auxiliary windows
    # first even while the button belongs to another, background window.
    win = None
    try:
        _, win = ax_getter(__import__("ApplicationServices").AXUIElementCopyAttributeValue,
                           target, "AXWindow")
    except Exception:
        pass
    if app is not None:
        try:
            _, windows = ax_getter(__import__("ApplicationServices").AXUIElementCopyAttributeValue,
                                   app, "AXWindows")
            wins = list(windows or [])
            snap.window_count = len(wins)
            # A single app window is an unambiguous fallback. In a multi-window
            # app, no AXWindow means we cannot identify the target's window.
            if win is None and len(wins) == 1:
                win = wins[0]
        except Exception:
            pass

    if win is not None:
        try:
            _, t = ax_getter(__import__("ApplicationServices").AXUIElementCopyAttributeValue,
                             win, "AXTitle")
            snap.window_title_hash = hash_string(t)
            snap.window_text_hash = _window_text_hash(win, ax_getter)
        except Exception:
            pass

    return snap


def _window_text_hash(win: Any, ax_getter: Callable[..., Any]) -> tuple[int, str]:
    """Hash the text content of a window (AXStaticText values, node-capped).

    Why: buttons whose effect lands on a *different* element — Calculator's
    keypad updates the display field, media keys update a time label, steppers
    update a readout — never change their own AXValue/children. Watching the
    window's text captures those effects and turns a verify=false-negative
    (which used to cascade into a 3 s Apple Events fallback) into a match.

    Bounded cost: stops after TREE_NODE_SAMPLE_CAP nodes; only reads AXValue
    for nodes whose role is AXStaticText.
    """
    AS = __import__("ApplicationServices")
    texts: list[str] = []
    seen = 0
    stack = [win]
    while stack and seen < TREE_NODE_SAMPLE_CAP:
        el = stack.pop()
        seen += 1
        try:
            _, role = ax_getter(AS.AXUIElementCopyAttributeValue, el, "AXRole")
            if role == "AXStaticText":
                _, v = ax_getter(AS.AXUIElementCopyAttributeValue, el, "AXValue")
                if v:
                    texts.append(str(v))
        except Exception:
            continue
        try:
            _, kids = ax_getter(AS.AXUIElementCopyAttributeValue, el, "AXChildren")
            for k in (kids or []):
                stack.append(k)
        except Exception:
            continue
    return hash_string("\x1f".join(texts))


def verify(
    before: Snapshot,
    *,
    signal: SignalClass,
    expected_text: Optional[str] = None,
    ax_getter: Optional[Callable[..., Any]] = None,
    target: Any = None,
    app: Any = None,
    window_ms: Optional[int] = None,
    first_poll_ms: int = DEFAULT_FIRST_POLL_MS,
    poll_ms: int = DEFAULT_POLL_MS,
) -> VerdictResult:
    """Poll the target element until ``signal`` matches or ``window_ms``
    elapses. Returns the time-to-match in ms; ``-1`` if no match.

    No upfront sleep: the first sample is taken ``first_poll_ms`` after the
    action; subsequent polls follow every ``poll_ms``. Early exit on first
    match. Doing this rather than sleeping once saves 200-400ms per action
    on native AX targets.
    """
    if signal == SignalClass.NA:
        _record_telemetry(
            VerdictResult(verified=Verdict.NA, signal=signal), signal
        )
        return VerdictResult(verified=Verdict.NA, signal=signal)

    if window_ms is None:
        window_ms = WINDOW_BY_CLASS.get(signal.value, 1500)

    if ax_getter is None or target is None:
        # Caller didn't supply the read path — we can't actually poll, just
        # return NA so the action still succeeds without verification.
        return VerdictResult(
            verified=Verdict.NA,
            signal=signal,
            diagnostics={"reason": "no ax_getter/target supplied"},
        )

    samples_taken = 0
    # Start the polling budget after dispatch. A slow snapshot or AXPress can
    # outlast the whole window; timing from the old baseline would then skip
    # every post-action sample, falsely rejecting even a completed operation.
    t_start = time.perf_counter()
    t_next = t_start + (first_poll_ms / 1000.0)
    deadline = t_start + (window_ms / 1000.0)

    while True:
        now = time.perf_counter()
        if now < t_next:
            time.sleep(min(t_next - now, poll_ms / 1000.0))
        now = time.perf_counter()
        if now > deadline:
            break
        samples_taken += 1
        after = snapshot(target, ax_getter, app=app)
        match = _match_signal(before, after, signal, expected_text)
        if match:
            verdict = VerdictResult(
                verified=Verdict.YES,
                signal=signal,
                matched_at_ms=(now - t_start) * 1000.0,
                samples_taken=samples_taken,
                elapsed_ms=(now - t_start) * 1000.0,
                diagnostics=_collect_diagnostics(before, after),
            )
            _record_telemetry(verdict, signal)
            return verdict
        t_next = now + (poll_ms / 1000.0)

    verdict = VerdictResult(
        verified=Verdict.NO,
        signal=signal,
        samples_taken=samples_taken,
        elapsed_ms=(time.perf_counter() - t_start) * 1000.0,
        # For value_set/no, the caller needs to see the actual last value to
        # decide "app reformatted" vs "write didn't land". Capture it from the
        # most recent snapshot we took. We re-snapshot here rather than track
        # the last ``after`` separately — the latter would inflate the hot
        # loop's per-iteration cost.
        diagnostics=(
            _collect_diagnostics(before, snapshot(target, ax_getter, app=app))
            if (target is not None and signal in (
                SignalClass.VALUE_SET, SignalClass.VALUE_FLIP, SignalClass.VALUE_DELTA
            ))
            else {}
        ),
    )
    _record_telemetry(verdict, signal)
    return verdict


def _match_signal(
    before: Snapshot,
    after: Snapshot,
    signal: SignalClass,
    expected_text: Optional[str],
) -> bool:
    """Pure comparison: does ``after`` show the expected signal vs ``before``?"""
    if signal == SignalClass.VALUE_FLIP:
        return before.value_hash != after.value_hash
    if signal == SignalClass.VALUE_DELTA:
        # Both numeric? If either isn't, we fall back to "value string changed".
        try:
            return float(before.value_raw) != float(after.value_raw)
        except (TypeError, ValueError):
            return before.value_hash != after.value_hash
    if signal == SignalClass.VALUE_SET:
        if expected_text is None:
            return False
        # Strict equality first, normalize second (whitespace + full/half
        # width). If we relax too far we'd accept "almost correct" user input.
        if after.value_raw is None:
            return False
        v = str(after.value_raw)
        if v == expected_text:
            return True
        v_norm = _normalize_for_compare(v)
        if v_norm == _normalize_for_compare(expected_text):
            return True
        return False
    if signal == SignalClass.ELEMENT_DEATH:
        # The strongest signal: the element itself is gone.
        return after.element_alive is False
    if signal == SignalClass.CHILDREN_CHANGE:
        if before.children_count_capped or after.children_count_capped:
            # Can't compare precisely when capped — assume a change only if
            # the capped counts differ; otherwise declare no-op defensively.
            return before.children_count != after.children_count
        return before.children_count != after.children_count
    if signal == SignalClass.WINDOW_CHANGE:
        # Either window title changed or window count changed.
        return (
            before.window_title_hash != after.window_title_hash
            or before.window_count != after.window_count
        )
    if signal == SignalClass.BUTTON_PRESS:
        # The fallback "any signal" class: any of the above flipped.
        if before.value_hash != after.value_hash:
            return True
        if before.children_count != after.children_count:
            return True
        if before.window_count != after.window_count:
            return True
        if before.focused != after.focused and after.focused is not None:
            return True
        # Effect landed elsewhere in the window (Calculator keypad → display,
        # media key → time label). Watching the window's text content turns
        # the historical false-negative into a match.
        if before.window_text_hash != after.window_text_hash:
            return True
        return False
    if signal == SignalClass.MENU_OPEN:
        # Menu item that opens a panel/modal: window title or count changes,
        # OR children grew (modal appended).
        return (
            before.window_title_hash != after.window_title_hash
            or before.window_count != after.window_count
            or before.window_text_hash != after.window_text_hash
            or (not before.children_count_capped and not after.children_count_capped
                and before.children_count != after.children_count)
        )
    return False


def _normalize_for_compare(s: str) -> str:
    """Loosen equality for the value_set signal: apps sometimes reformat
    user input (date pickers auto-insert `/`, number fields strip leading
    zero, newline normalization, full/half-width digits/letters)."""
    if s is None:
        return ""
    out = []
    for ch in str(s):
        if ch.isspace():
            continue
        # Half/full-width digits 0-9 ↔ ASCII
        cp = ord(ch)
        if 0xFF10 <= cp <= 0xFF19:  # full-width 0-9
            out.append(chr(cp - 0xFF10 + ord("0")))
            continue
        # Full-width ASCII letters Ａ-Ｚ / ａ-ｚ → ASCII
        if 0xFF21 <= cp <= 0xFF3A:  # Ａ-Ｚ
            out.append(chr(cp - 0xFF21 + ord("A")))
            continue
        if 0xFF41 <= cp <= 0xFF5A:  # ａ-ｚ
            out.append(chr(cp - 0xFF41 + ord("a")))
            continue
        out.append(ch)
    return "".join(out).lower()


def _collect_diagnostics(before: Snapshot, after: Snapshot) -> dict[str, Any]:
    """Free-form dict for callers to surface in routing_meta / debug logs."""
    diag: dict[str, Any] = {}
    if before.value_hash != after.value_hash:
        diag["value_changed"] = True
        # Capture the raw after-value so callers (esp. fill verify) can tell
        # the LLM whether the app reformatted the input vs. dropped it.
        if after.value_raw is not None:
            raw = str(after.value_raw)
            # Cap to 200 chars so diagnostic field doesn't balloon.
            if len(raw) > 200:
                raw = raw[:200] + "…"
            diag["value_after"] = raw
    if before.children_count != after.children_count:
        diag["children_changed"] = True
        diag["children_delta"] = after.children_count - before.children_count
    if before.window_count != after.window_count:
        diag["window_count_changed"] = True
    if after.element_alive is False:
        diag["element_disposed"] = True
    return diag


# ---------------------------------------------------------------------------
# Per-action picker:  caller code looks up "what signal class applies here"
# ---------------------------------------------------------------------------

def signal_for(role: str, action: str) -> SignalClass:
    """Map (role, action) → signal class. Used by desktop_ax._click etc.

    The case-mapping is intentionally conservative: when in doubt we return
    ``BUTTON_PRESS`` (the "any signal" class) rather than declaring NA,
    because a false-positive verify is no worse than no verify, while a
    false-negative verify (NA) silently disables the safety net.
    """
    role = (role or "").lower()
    action = (action or "").lower()

    if role in ("checkbox", "axcheckbox", "radio", "axradiobutton", "axswitch"):
        return SignalClass.VALUE_FLIP
    if role in ("slider", "axslider", "axincrementor"):
        return SignalClass.VALUE_DELTA
    if role in ("axpopupbutton", "popup", "combobox"):
        # Pop-up: opening the menu is the primary effect; selection changes
        # the value but that's a follow-on. Use children_change as the
        # opening signal.
        return SignalClass.CHILDREN_CHANGE
    if role in ("axmenuitem", "menu_item", "menu"):
        if "close" in action or "dismiss" in action or "quit" in action or "exit" in action:
            return SignalClass.ELEMENT_DEATH
        # Most menu items open panels/menus.
        return SignalClass.MENU_OPEN
    if role in ("axbutton", "button", "toolbar_button"):
        return SignalClass.BUTTON_PRESS

    # Fallback: any observable change.
    return SignalClass.BUTTON_PRESS


def signal_for_value_set(role: str) -> SignalClass:
    """Used by AXValue setattr (fill action)."""
    return SignalClass.VALUE_SET


def signal_for_text_typing() -> SignalClass:
    """Press_key / keystroke — value_set against the focused element. Marked
    NA by callers when there's no obvious "expected text" (e.g. modifiers)."""
    return SignalClass.VALUE_SET
