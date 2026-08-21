"""macOS Accessibility (AX) layer.

Implemented with PyObjC against ``ApplicationServices`` and
``Quartz.CoreGraphics``. Verified on this machine (PyObjC 12.2.1,
macOS 26). Tested facts:

- AX positions are in Quartz points (not pixels); no scale math on Retina.
- ``AXUIElementCopyAttributeValue`` signature differs across PyObjC versions;
  ``_ax_get`` normalizes it to always return ``(err, value)``.
- Permission is required; use ``AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})``
  to surface the system prompt on first call.
- We map raw ``AX*`` roles to the normalized vocab in ``qcu.common.normalize``.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any, Optional

from qcu.common.normalize import clean_text, from_ax, INTERACTIVE_ROLES, wait_ms as _wait_ms
from qcu.common.types import Action, Element, LayerResult, Observation, Rect
from qcu.layers.base import Layer
from qcu.layers.runtime import register


# Roles that aren't actable but carry useful text for the raw_tree (window
# titles, static labels, menu items already in INTERACTIVE_ROLES, etc.).
# Used to decide which non-interactive nodes still get a raw_tree line.
_STRUCTURED_ROLES_DESKTOP = frozenset(
    {"text", "group", "heading", "window", "row", "cell", "list_item", "image"}
)


def running_apps_full_for_payload(apps: list[str], *, compact: bool) -> Optional[list[str]]:
    """Decide how much of ``apps`` to put in routing_meta.

    Under ``--compact``: drop the field entirely (``None``). The count and
    preview still survive so switching is possible.
    Otherwise: return the full list for backward compatibility.
    """
    if compact:
        return None
    return apps


def _check_pyobjc() -> bool:
    try:
        from ApplicationServices import (  # noqa: F401
            AXIsProcessTrusted,
            AXUIElementCreateSystemWide,
        )
        from Quartz.CoreGraphics import CGEventCreateMouseEvent  # noqa: F401

        return True
    except Exception:
        return False


def _ax_get(fn, elem, attr):
    """Call ``AXUIElementCopyAttributeValue`` across PyObjC signature variants.

    Older PyObjC: ``AXUIElementCopyAttributeValue(elem, attr) -> (err, value)``
    Newer PyObjC: requires 3 args, returns ``None`` and fills the 3rd byref:
                 ``AXUIElementCopyAttributeValue(elem, attr, None) -> (err, value)``

    We pick the form that runs without TypeError and always return
    ``(err, value)`` so callers can stay uniform regardless of the installed
    PyObjC version. ``fn`` is the bound function object.
    """
    try:
        return fn(elem, attr)  # old 2-arg form
    except TypeError:
        return fn(elem, attr, None)  # new 3-arg form


# ---------------------------------------------------------------------------
# Blocker prefaces — shown in raw_tree when observe can't read the UI.
#
# The field report: QCU was invoked on Kimi but returned ~0 elements with
# routing_meta.trusted=False / available=False, and the agent — seeing an
# empty tree and no actionable guidance — abandoned desktop automation and
# fell back to the terminal. These prefaces put a loud, bilingual remediation
# at the TOP of raw_tree so the agent's first read tells it (a) this is a
# permission/dependency gap, NOT an empty app, and (b) the exact commands to
# recover. Mirrors the web_view/AXWebArea preface pattern already in observe.
# ---------------------------------------------------------------------------

_BLOCKER_PREFACE_NOT_TRUSTED = (
    "# ⚠ 無法讀取 macOS UI:輔助使用(Accessibility)權限未授予。\n"
    "# 這不是「應用沒有介面」,而是 QCU 進程沒有 AX 權限——元素清單才會是空的。\n"
    "# 補救步驟:\n"
    "#   1. 執行 `qcu doctor` 確認權限狀態\n"
    "#   2. 到 系統設定 > 隱私權與安全性 > 輔助使用,勾選 QCU 使用的 Python/qcu\n"
    "#      (或直接 `open 'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'`)\n"
    "#   3. 授權後重啟 session:`qcu session end && qcu session start --context desktop`\n"
    "# Accessibility permission not granted — the empty element list is a permission\n"
    "# gap, not an empty UI. Run `qcu doctor`, grant in System Settings > Privacy &\n"
    "# Security > Accessibility, then restart the desktop session."
)

_BLOCKER_PREFACE_MISSING_PYOBJC = (
    "# ⚠ 無法讀取 macOS UI:缺少 PyObjC(ApplicationServices/Quartz)依賴。\n"
    "# QCU 的 desktop_ax 層需要 pyobjc 才能走 macOS Accessibility API。\n"
    "# 補救:執行 `pip install pyobjc-framework-ApplicationServices pyobjc-framework-Quartz`\n"
    "# Missing PyObjC dependency — install pyobjc-framework-ApplicationServices and\n"
    "# pyobjc-framework-Quartz, then retry. The empty element list is a dependency\n"
    "# gap, not an empty UI."
)


@register("desktop_ax")
class DesktopAXLayer(Layer):
    name = "desktop_ax"

    def __init__(self) -> None:
        self._available = _check_pyobjc()
        self._cached_trusted: Optional[bool] = None
        self._last_refs: dict[str, dict[str, Any]] = {}
        # The app the user most recently launched/activated. Observed frontmost
        # detection (menuBarOwningApplication) reports the *caller's* app when
        # QCU runs in the background (e.g. from ZCode), so we prefer the
        # explicitly-targeted app when set.
        self._target_app: Optional[str] = None
        # Pids we've already flipped AXEnhancedUserInterface on (WriteOnce, so
        # repeated observes of the same app don't re-pay the set cost).
        self._enhanced_apps: set[int] = set()
        try:
            from qcu.session import load as _load
            s = _load()
            if s and s.target_app:
                self._target_app = s.target_app
        except Exception:
            pass
        # Restore cached refs from the session so a fresh CLI process can act
        # on refs from a prior observe. Without this, every `qcu act` after an
        # observe in a separate process sees an empty _last_refs and rejects
        # every ref as "unknown".
        try:
            from qcu.session import load as _load
            s = _load()
            if s and s.last_refs:
                for entry in s.last_refs:
                    ref = entry.get("ref")
                    if not ref:
                        continue
                    b = entry.get("bounds")
                    bounds = None
                    if b:
                        bounds = Rect(b["x"], b["y"], b["width"], b["height"])
                    self._last_refs[ref] = {
                        "cx": entry.get("cx"),
                        "cy": entry.get("cy"),
                        "role": entry.get("role"),
                        "role_raw": entry.get("role_raw"),
                        "subrole": entry.get("subrole"),
                        "name": entry.get("name"),
                        "path": entry.get("path"),
                        "bounds": bounds,
                        # ax_element is process-local; a fresh process must
                        # re-resolve via _resolve_ax_element using the fingerprint.
                        "ax_element": None,
                    }
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Permission probe
    # ------------------------------------------------------------------

    def is_trusted(self, *, prompt: bool = False) -> bool:
        if not self._available:
            return False
        if self._cached_trusted is not None and not prompt:
            return self._cached_trusted
        from ApplicationServices import (
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )

        if prompt:
            try:
                self._cached_trusted = bool(
                    AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
                )
            except Exception:
                self._cached_trusted = AXIsProcessTrusted()
        else:
            self._cached_trusted = AXIsProcessTrusted()
        return self._cached_trusted

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        if not self._available:
            return Observation(
                context="desktop",
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "available": False,
                    "reason": "PyObjC ApplicationServices/Quartz not installed",
                },
                # raw_tree preface so the agent sees the blocker even when
                # stderr is swallowed (e.g. daemon RPC path). Without this the
                # agent sees an empty element list and gives up — the "無法讀取
                # UI" field report. The preface tells it this is a dependency
                # gap, not an empty app, and how to fix it.
                raw_tree=_BLOCKER_PREFACE_MISSING_PYOBJC,
            )

        if not self.is_trusted(prompt=False):
            # Try once with the prompt; user may have just granted.
            if not self.is_trusted(prompt=True):
                return Observation(
                    context="desktop",
                    elements=[],
                    routing_meta={
                        "layer": self.name,
                        "trusted": False,
                        "reason": "Accessibility permission not granted",
                    },
                    # See _BLOCKER_PREFACE_NOT_TRUSTED: tells the agent this is
                    # a permission gap (not an empty UI) and gives the exact
                    # remediation commands. routing_meta.reason alone was too
                    # easy to miss — the agent read "0 elements" and bailed.
                    raw_tree=_BLOCKER_PREFACE_NOT_TRUSTED,
                )

        # strict-background: observe itself doesn't steal focus, but surface a
        # warning in routing_meta if the frontmost app has drifted — the LLM may
        # be inspecting the wrong app entirely (the Finder→飞书→Clash drift seen
        # in production). Don't hard-abort observe; the caller can still read the
        # (possibly wrong) tree and decide.
        _strict_drift: dict[str, Any] = {}
        cfg = self._strict_background_mode()
        if cfg["enabled"] and cfg.get("expected"):
            current = self._frontmost_name()
            if current is not None and current.lower().removesuffix(".app") != \
                    cfg["expected"].lower().removesuffix(".app"):
                _strict_drift = {
                    "strict_background_drift": True,
                    "expected": cfg["expected"],
                    "actual": current,
                }

        from ApplicationServices import (
            AXUIElementCreateApplication,
            AXUIElementCopyAttributeValue,
            kAXTitleAttribute,
        )

        # Get the frontmost app via NSWorkspace's processIdentifier, then build
        # the AX element from its pid. This is far more reliable than the old
        # AXUIElementCreateSystemWide + kAXFocusedApplicationAttribute path,
        # which frequently returns err=-25204 (kAXErrorCannotComplete) even when
        # AX trust is freshly granted — the system-wide focused-app query is
        # inconsistently honored across macOS versions.
        try:
            from AppKit import NSWorkspace  # type: ignore
        except Exception as e:  # noqa: BLE001
            return Observation(
                context="desktop",
                elements=[],
                routing_meta={"layer": self.name, "error": f"AppKit unavailable: {e}"},
            )
        ws = NSWorkspace.sharedWorkspace()
        front = None
        # Priority 0: explicit --pid / --app scoping (highest precision). These
        # were added because when QCU runs in the background the frontmost app
        # drifts to the caller, so observe kept inspecting the wrong app. --pid
        # is exact; --app matches by name (case-insensitive, .app stripped).
        if options.get("pid"):
            try:
                for a in ws.runningApplications():
                    if int(a.processIdentifier()) == int(options["pid"]):
                        front = a
                        break
            except Exception:
                front = None
        if front is None and options.get("app"):
            needle = str(options["app"]).lower().removesuffix(".app")
            for a in ws.runningApplications():
                n = (a.localizedName() or "").lower().removesuffix(".app")
                if n == needle:
                    front = a
                    break
        # Priority 1: the explicitly-targeted app (set by launch_app/activate_app
        # and persisted to session). This matters most when QCU runs in the
        # background — menuBarOwningApplication then reports the CALLER (ZCode),
        # not the app we just launched, so we'd observe the wrong tree.
        if front is None and self._target_app:
            needle = self._target_app.lower().removesuffix(".app")
            for a in ws.runningApplications():
                n = (a.localizedName() or "").lower().removesuffix(".app")
                if n == needle and a.isActive():
                    front = a
                    break
            if front is None:
                # Fall back to the named app even if not active (still observable
                # via AX — active-state only affects window focus for keyboard).
                for a in ws.runningApplications():
                    n = (a.localizedName() or "").lower().removesuffix(".app")
                    if n == needle:
                        front = a
                        break
        # Priority 2: the actually-active app.
        if front is None:
            front = ws.frontmostApplication()
        if front is None:
            return Observation(
                context="desktop",
                elements=[],
                routing_meta={"layer": self.name, "error": "no frontmost application"},
            )
        app = AXUIElementCreateApplication(front.processIdentifier())

        title = ""
        try:
            terr, tval = _ax_get(AXUIElementCopyAttributeValue, app, kAXTitleAttribute)
            if terr == 0 and tval:
                title = str(tval)
        except Exception:
            pass

        # Enable AXEnhancedUserInterface on the target app. Catalyst/WebKit apps
        # (Notes, App Store, Music, TV) only expose their full window content
        # (text areas, buttons, search fields) when this flag is True; without
        # it they show just the menu bar and a self-referencing AXApplication
        # node, making the window content invisible to AX. This is the same flag
        # VoiceOver flips on; setting it is standard AX-client behavior.
        # WriteOnce per pid to avoid the (small) cost of repeated sets.
        try:
            app_pid = front.processIdentifier()
            if app_pid not in self._enhanced_apps:
                from ApplicationServices import AXUIElementSetAttributeValue
                AXUIElementSetAttributeValue(app, "AXEnhancedUserInterface", True)
                self._enhanced_apps.add(app_pid)
        except Exception:
            pass

        elements: list[Element] = []
        raw_lines: list[str] = []
        counter = 0
        new_refs: dict[str, dict[str, Any]] = {}
        # Detect an embedded WKWebView/Electron web view (AXWebArea). When the
        # target app's content is an opaque web view, the AX tree exposes only
        # the outer shell — the DOM is unreachable (the field-report gap:
        # "Mirroria 的 WKWebView 没有向 AX 暴露内部 DOM"). Surfacing this lets
        # the LLM escalate to T3 web takeover (session start --context web +
        # navigate) instead of staring at an empty tree. We do NOT attempt to
        # recover a URL (most WKWebView apps expose no scriptable tab model);
        # the hint is honest about that.
        found_web_area: bool = False

        def walk(elem: Any, depth: int, path: list[int]) -> None:
            nonlocal counter
            if max_depth > 0 and depth > max_depth:
                return
            # Query ROLE first — it's the cheapest discriminator. Most nodes
            # are layout containers (AXGroup/AXLayoutArea) we don't care about;
            # checking role first lets us skip the expensive name/value/pos/
            # size/enabled queries for them. This is the single biggest win on
            # big trees like Notes (~8s -> ~2s): previously every node paid 7
            # AX round-trips regardless of role.
            try:
                role_err, role = _ax_get(AXUIElementCopyAttributeValue, elem, "AXRole")
            except Exception:
                return
            if role_err != 0 or not role:
                return
            role_norm = from_ax(str(role)) if role else "group"

            # Only fetch name — cheap and needed for both branches below.
            try:
                name_err, name = _ax_get(AXUIElementCopyAttributeValue, elem, "AXTitle")
            except Exception:
                name_err, name = -1, None
            name_s = clean_text(str(name)) if name_err == 0 and name else ""

            if role_norm in INTERACTIVE_ROLES:
                # Full attribute fetch only for actable elements (the minority).
                try:
                    value_err, value = _ax_get(AXUIElementCopyAttributeValue, elem, "AXValue")
                except Exception:
                    value_err, value = -1, None
                try:
                    pos_err, pos = _ax_get(AXUIElementCopyAttributeValue, elem, "AXPosition")
                except Exception:
                    pos_err, pos = -1, None
                try:
                    size_err, size = _ax_get(AXUIElementCopyAttributeValue, elem, "AXSize")
                except Exception:
                    size_err, size = -1, None
                try:
                    en_err, enabled = _ax_get(AXUIElementCopyAttributeValue, elem, "AXEnabled")
                except Exception:
                    en_err, enabled = -1, None
                # Whether this element currently holds focus. AXFocused is a
                # boolean on actable elements; reading it lets the LLM (and our
                # own fill/press_key verification) know which field is live
                # without a separate probe. Previously hardcoded False, which
                # hid the only signal that distinguishes "body of TextEdit"
                # from "an unfocused text field".
                try:
                    _, focused_val = _ax_get(AXUIElementCopyAttributeValue, elem, "AXFocused")
                except Exception:
                    focused_val = None
                focused = bool(focused_val) if focused_val is not None else False
                # Subrole lets us distinguish AXSearchField / AXList / etc. and
                # is part of the ref's "AX fingerprint" used by act to relocate
                # the element in a fresh process.
                try:
                    _, subrole = _ax_get(AXUIElementCopyAttributeValue, elem, "AXSubrole")
                except Exception:
                    subrole = None
                # AXSearchField is a search box — surface it as searchbox so the
                # LLM treats it as searchable (normalize would otherwise call it
                # a plain edit, hiding its real function).
                if subrole == "AXSearchField":
                    role_norm = "searchbox"
                value_s = clean_text(str(value), max_len=1000) if value_err == 0 and value else ""

                bounds: Optional[Rect] = None
                cx: Optional[float] = None
                cy: Optional[float] = None
                if pos_err == 0 and size_err == 0 and pos is not None and size is not None:
                    try:
                        x = float(getattr(pos, "x", 0))
                        y = float(getattr(pos, "y", 0))
                        w = float(getattr(size, "width", 0))
                        h = float(getattr(size, "height", 0))
                        if w > 0 and h > 0:
                            bounds = Rect(x, y, w, h)
                            cx = bounds.cx
                            cy = bounds.cy
                    except Exception:
                        pass

                ref = f"ref_{counter}"
                counter += 1
                el = Element(
                    ref=ref,
                    role=role_norm,
                    name=name_s or None,
                    value=value_s or None,
                    enabled=(enabled is None) or bool(enabled),
                    focused=focused,
                    bounds=bounds,
                    backend_id=str(role_norm),
                    properties={"raw_role": str(role), "subrole": subrole, "depth": depth},
                )
                elements.append(el)
                new_refs[ref] = {
                    "role": role_norm,
                    "role_raw": str(role),
                    "subrole": subrole,
                    "name": name_s,
                    "cx": cx,
                    "cy": cy,
                    "bounds": bounds,
                    "path": list(path),
                    # In-process AXUIElement handle — can't be serialized to
                    # session.json, so patch() (which drops it via JSON) keeps
                    # only the rest as the cross-process fingerprint.
                    "ax_element": elem,
                }
                indent = "  " * depth
                line = f"{indent}[{ref}] {role_norm}"
                if name_s:
                    line += f" {name_s!r}"
                if value_s:
                    line += f" = {value_s!r}"
                raw_lines.append(line)
            elif name_s and role_norm in _STRUCTURED_ROLES_DESKTOP:
                indent = "  " * depth
                raw_lines.append(f"{indent}{role_norm} {name_s!r}")
            # Flag an embedded web view (WKWebView/Electron). The raw AX role
            # string is the reliable discriminator; ``role_norm`` is "document"
            # (normalize.py:225) which is shared with other document roles.
            if role == "AXWebArea":
                found_web_area = True
            # else: layout container with no useful name — skip entirely (no
            # further queries), which is what makes the walk fast.

            try:
                ch_err, kids = _ax_get(AXUIElementCopyAttributeValue, elem, "AXChildren")
            except Exception:
                ch_err, kids = -1, None
            if ch_err == 0 and kids:
                for idx, k in enumerate(kids):
                    walk(k, depth + 1, path + [idx])

        # Walk the target tree. Two modes:
        # 1. --window <substring>: walk ONLY the AXWindow(s) whose title
        #    contains the substring. This drops the menu bar + every other
        #    window's controls, which is the difference between 300 elements
        #    and 30 when an app has many panels. Falls back to mode 2 if no
        #    window matches (so --window is never a hard failure).
        # 2. default: walk from the app root. (We tried window-first, but
        #    Notes' AX tree is self-referencing — AXWindows[0]/AXFocusedWindow
        #    both return an AXApplication role, not an AXWindow — so starting
        #    from the app root with a generous depth actually reaches the
        #    window content too.)
        walked_roots: list[Any] = []
        win_filter = options.get("window")
        if win_filter:
            needle_w = str(win_filter).lower()
            try:
                _, wins = _ax_get(AXUIElementCopyAttributeValue, app, "AXWindows")
                for w in (wins or []):
                    try:
                        _, wt = _ax_get(AXUIElementCopyAttributeValue, w, "AXTitle")
                        if wt and needle_w in str(wt).lower():
                            walked_roots.append(w)
                    except Exception:
                        pass
            except Exception:
                pass
        if walked_roots:
            for w in walked_roots:
                walk(w, 0, [])
        else:
            walk(app, 0, [])
        self._last_refs = new_refs

        # Persist refs to the session so a fresh CLI process can act on them
        # without re-observing first. Mirrors how web_a11y does it — without
        # this, every `qcu act` after an observe in a separate process fails
        # with "unknown ref" because _last_refs starts empty.
        try:
            from qcu.session import patch
            serialized = []
            for ref, info in new_refs.items():
                b = info.get("bounds")
                serialized.append({
                    "ref": ref,
                    "cx": info.get("cx"),
                    "cy": info.get("cy"),
                    "role": info.get("role"),
                    "role_raw": info.get("role_raw"),
                    "subrole": info.get("subrole"),
                    "name": info.get("name"),
                    "path": info.get("path"),
                    "bounds": ({"x": b.x, "y": b.y, "width": b.width, "height": b.height}
                               if b is not None else None),
                })
            patch(last_refs=serialized)
        except Exception:
            pass

        # Expose the set of running apps so the LLM can pick a target for
        # activate_app / launch_app. We only need names, kept compact.
        running_apps: list[str] = []
        try:
            from AppKit import NSWorkspace  # type: ignore

            ws = NSWorkspace.sharedWorkspace()
            seen = set()
            for a in ws.runningApplications():
                n = a.localizedName()
                if n and n not in seen:
                    seen.add(n)
                    running_apps.append(str(n))
        except Exception:
            pass
        running_apps.sort()

        # Prepend a small header block so the LLM sees switchable apps right
        # away, alongside the focused app's tree.
        header: list[str] = []
        if running_apps:
            preview = ", ".join(running_apps[:40])
            header.append(f"# running apps ({len(running_apps)}): {preview}")
            header.append("# use activate_app or launch_app to switch target")
            header.append("")
        tree_text = "\n".join(header + raw_lines) if raw_lines else ("\n".join(header) or None)

        # Web-app awareness (shared with desktop_appleevents): when the target
        # is Chrome / Safari / Edge, Chrome's AX tree shows only the chrome —
        # the DOM is opaque unless Google's "Allow JS from Apple Events" and
        # ``AXManualAccessibility`` are both flipped (we can't do that
        # programmatically without AX trust on our process). We probe the
        # browser's native scripting dictionary for tab URL + title so the
        # LLM at least knows WHAT page is open and can navigate QCU's own
        # headless Chromium there to actually interact with the DOM.
        web_app_meta: Optional[dict[str, Any]] = None
        web_app_hint = ""
        try:
            from qcu.layers.desktop_appleevents import _probe_web_app

            web_app_meta, web_app_hint = _probe_web_app(self._target_app)
        except Exception:
            pass

        # Pagination + filter + compact, mirroring desktop_appleevents and
        # web_a11y. Without this, a Chrome/Safari window dumps all 400+
        # interactive elements (~46k tokens) back to the LLM, drowning the
        # high-signal web_app probe. Pagination only changes what's surfaced
        # to the caller — the AXUIElement cache + session.last_refs keep the
        # full set so ``act`` can still resolve any ref.
        from qcu.layers._paginate import apply_pagination_and_compact

        sliced, page_meta = apply_pagination_and_compact(
            elements,
            limit=options.get("limit"),
            offset=int(options.get("offset") or 0),
            compact=bool(options.get("compact", False)),
            role_filter=options.get("roles"),
            name_query=options.get("query"),
        )

        # Rebuild raw_tree from the sliced subset to keep stdout token count
        # proportional to what's actually returned. The structured-context
        # lines (window title etc.) are pulled in from the original raw_lines
        # for the first few so the LLM has *some* framing.
        ui_lines: list[str] = list(header)
        # Reuse up to the first 6 context lines from raw_lines (window labels,
        # group names) if they exist — these don't survive pagination.
        for ctx in raw_lines[:6]:
            if ctx and not ctx.lstrip().startswith("["):
                ui_lines.append(ctx)
                if len([u for u in ui_lines if not u.startswith("  [")]) >= 8:
                    break
        for e in sliced:
            ln = f"  [{e.ref}] {e.role}"
            if e.name:
                ln += f" {e.name!r}"
            if e.value:
                ln += f" = {e.value!r}"
            ui_lines.append(ln)
        if page_meta["tree_truncated"]:
            ui_lines = [
                f"# Showing {page_meta['n_returned']} of {page_meta['n_total']} "
                f"interactive elements (offset={int(options.get('offset') or 0)}, "
                f"limit={options.get('limit')}). Re-run with different "
                f"--offset/--limit to see more."
            ] + ui_lines

        tree_text = "\n".join(ui_lines) if ui_lines else None

        if web_app_meta is not None or web_app_hint:
            hint_lines = web_app_hint.splitlines() if web_app_hint else []
            tree_text = "\n".join(hint_lines + ["", "# --- AX UI tree (browser chrome only) ---", tree_text]) if tree_text else web_app_hint

        # Embedded web view (WKWebView/Electron): the AX tree is the outer
        # shell only — the DOM is opaque. Surface a structured hint so the
        # LLM can escalate to the QCU Chromium daemon. We deliberately set
        # ``url=None``: most WKWebView apps expose no scriptable tab model,
        # so we can't auto-recover the page URL and won't pretend to. The
        # raw_tree preface is the action a caller should take.
        web_view_meta: Optional[dict[str, Any]] = None
        if found_web_area:
            app_name_for_hint = self._target_app or title or None
            web_view_meta = {
                "app": app_name_for_hint,
                "url": None,
                "reason": (
                    "AXWebArea detected: the app embeds an opaque web view "
                    "(WKWebView/Electron) whose DOM AX cannot reach. To act on "
                    "page content, end this session and restart in web context: "
                    "`qcu session end && qcu session start --context web && "
                    "qcu act '{\"type\":\"navigate\",\"params\":{\"url\":\"<the page URL>\"}}'`."
                ),
            }
            wv_preface = (
                f"# ⚠ 检测到内嵌 web view（AXWebArea）— DOM 对 AX 不可见。\n"
                f"# 要操作页面内容：结束当前会话，用 web context 启动 QCU 自带的 Chromium：\n"
                f"#   qcu session end  →  qcu session start --context web  →  navigate <url>\n"
                f"# （QCU 无法自动获取该 web view 的 URL；需要你提供。）"
            )
            tree_text = (wv_preface + "\n\n" + tree_text) if tree_text else wv_preface

        # Full browser (Chrome/Safari/Edge/Arc) detected via AppleScript tab
        # probing. Stock Chrome does NOT expose AXWebArea without
        # AXManualAccessibility, so the AXWebArea branch above never fires for
        # it — yet its DOM is just as opaque. Mirror the structured web_view
        # flag here so the router's has_web_area (and the new has_web_app)
        # both trip and the T3 takeover hint reaches the agent through every
        # channel: routing_meta.web_view, router reason, AND a stderr line.
        # Field report: "CRM 网页 DOM 对 QCU 不可见" — the soft raw_tree
        # comment alone was being ignored.
        elif web_app_meta is not None:
            browser_name = web_app_meta.get("browser") or self._target_app or title or None
            first_tab_url = None
            tabs = web_app_meta.get("tabs") or []
            if tabs and isinstance(tabs[0], dict):
                first_tab_url = tabs[0].get("url")
            web_view_meta = {
                "app": browser_name,
                "url": first_tab_url,
                "reason": (
                    f"Frontmost app is a browser ({browser_name}) detected via "
                    "AppleScript tab probing; its web DOM is opaque to AX. To act "
                    "on page content, end this session and restart in web context, "
                    "navigating QCU's own Chromium to the same URL: "
                    "`qcu session end && qcu session start --context web && "
                    "qcu act '{\"type\":\"navigate\",\"params\":{\"url\":\"<url>\"}}'`."
                ),
            }
            import sys as _sys
            print(
                f"[qcu] 前台是浏览器 {browser_name!r}：DOM 对 AX 不透明，"
                f"建议 `qcu session end && qcu session start --context web` 后 "
                f"navigate 到同一 URL（如 {first_tab_url!r}）。",
                file=_sys.stderr,
            )

        return Observation(
            context="desktop",
            url_or_app=title or None,
            title=title or None,
            elements=sliced,
            routing_meta={
                "layer": self.name,
                "trusted": True,
                "n_refs": len(elements),  # total in this observation
                "n_returned": page_meta["n_returned"],
                "n_total": page_meta["n_total"],
                "n_interactive": page_meta["n_interactive"],
                "tree_truncated": page_meta["tree_truncated"],
                # ``running_apps`` was unconditionally emitting ~250 process
                # names (~3-4KB) into the JSON even under ``--compact``,
                # drowning the high-signal ``web_app`` probe. We now keep the
                # count and a short preview; the full list stays available as
                # ``running_apps_full`` unless compact wants the tightest
                # payload.
                "running_apps_count": len(running_apps),
                "running_apps_preview": running_apps[:30],
                "running_apps": running_apps_full_for_payload(running_apps, compact=bool(options.get("compact", False))),
                **({"web_app": web_app_meta} if web_app_meta is not None else {}),
                **({"web_view": web_view_meta} if web_view_meta is not None else {}),
                **_strict_drift,
            },
            raw_tree=tree_text,
        )

    # ------------------------------------------------------------------
    # Act
    # ------------------------------------------------------------------

    def act(self, action: Action) -> LayerResult:
        if not self._available:
            return LayerResult(ok=False, layer=self.name, message="PyObjC not installed")
        # launch_app uses `open -a`, which does NOT need the Accessibility
        # permission — so allow it even before AX trust is granted. This also
        # lets a first-time user bootstrap: launch_app works immediately, and
        # observe() will surface the permission prompt when they try to inspect
        # an app's UI.
        if action.type == "launch_app":
            # strict-background: launch/activate would steal focus → refuse.
            deny = self._check_strict_background(stealing_focus=True)
            if deny is not None:
                return deny
            return self._launch_app((action.params or {}).get("app", ""))
        # strict-background drift check for every other action (click/fill/...).
        deny = self._check_strict_background(stealing_focus=False)
        if deny is not None:
            return deny
        if not self.is_trusted(prompt=False):
            return LayerResult(
                ok=False,
                layer=self.name,
                message="Accessibility permission not granted; pass needs_visual_confirm or run session start",
            )

        atype = action.type
        params = action.params or {}

        if atype in ("click", "double_click", "hover", "right_click"):
            return self._click(atype, params)
        if atype == "drag":
            return self._drag(params)
        if atype == "type":
            return self._type_text(params.get("text") or "")
        if atype == "press_key":
            return self._press_key(params.get("key", ""))
        if atype == "fill":
            return self._fill(params)
        if atype == "screenshot":
            return self._screenshot(params.get("path"))
        if atype == "wait":
            ms = _wait_ms(params)
            time.sleep(ms / 1000.0)
            return LayerResult(ok=True, layer=self.name, message=f"waited {ms}ms")
        if atype == "scroll":
            return self._scroll(params)
        if atype == "launch_app":
            return self._launch_app(params.get("app", ""))
        if atype == "activate_app":
            # strict-background: activate would steal focus → refuse.
            deny = self._check_strict_background(stealing_focus=True)
            if deny is not None:
                return deny
            return self._activate_app(params.get("app", ""))
        if atype in ("go_back", "go_forward"):
            # Browser back/forward via Cmd+[ / Cmd+] — now uses the real
            # modifier-aware combo path (previously posted a bare '[' ).
            combo = f"Cmd+[" if atype == "go_back" else "Cmd+]"
            r = self._press_key(combo)
            return LayerResult(
                ok=r.ok,
                layer=self.name,
                message=(f"{atype} (via {combo})" if r.ok else f"{atype} failed: {r.message}"),
            )
        return LayerResult(ok=False, layer=self.name, message=f"unsupported action type: {atype}")

    # ------------------------------------------------------------------
    # AX-native element resolution + actions (no coordinates, no screenshot)
    # ------------------------------------------------------------------

    def _resolve_ax_element(self, ref: str) -> Optional[Any]:
        """Resolve a ref back to a live AXUIElement in THIS process.

        Priority:
        1. In-process handle cached during observe (fast path).
        2. Re-locate via the stored fingerprint (role/name/subrole/path) by
           walking the frontmost app's tree again. Slower but works across
           CLI process boundaries where the AXUIElement can't be serialized.
        3. None → caller falls back to coordinate input.

        This is what lets QCU act via AX-native APIs (AXPress / AXSetAttributeValue)
        instead of always dropping to CGEvent coordinate clicks.
        """
        cached = self._last_refs.get(ref)
        if cached is None:
            return None
        # Fast path: same process as the observe that created this ref.
        elem = cached.get("ax_element")
        if elem is not None:
            return elem

        # Slow path: re-walk to find the matching element by fingerprint.
        try:
            from ApplicationServices import (
                AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
            )
            from AppKit import NSWorkspace  # type: ignore
            ws = NSWorkspace.sharedWorkspace()
            front = None
            # Same target-app-first logic as observe(): when QCU runs in the
            # background, frontmost/menuBar report the caller, not the target.
            if self._target_app:
                needle = self._target_app.lower().removesuffix(".app")
                for a in ws.runningApplications():
                    n = (a.localizedName() or "").lower().removesuffix(".app")
                    if n == needle:
                        front = a
                        break
            if front is None:
                front = ws.frontmostApplication()
            if front is None:
                return None
            app_elem = AXUIElementCreateApplication(front.processIdentifier())
            # Prefer AXWindows (matches observe's new window-first walk);
            # AXMainWindow is unreliable on WebKit apps (returns the app root).
            wins: Optional[list] = None
            try:
                werr, wins = _ax_get(AXUIElementCopyAttributeValue, app_elem, "AXWindows")
            except Exception:
                werr, wins = -1, None
            if werr != 0 or not wins:
                return None
            win = wins[0]
            # Walk with a matcher: prefer path (exact position), fall back to
            # (role, name, subrole) tuple match if the tree shifted.
            target_role_raw = cached.get("role_raw")
            target_name = cached.get("name")
            target_subrole = cached.get("subrole")
            target_path = cached.get("path") or []

            def by_path(node: Any, path: list[int]) -> Optional[Any]:
                if not path:
                    return node
                try:
                    _, kids = _ax_get(AXUIElementCopyAttributeValue, node, "AXChildren")
                except Exception:
                    kids = None
                if not kids:
                    return None
                idx = path[0]
                if idx >= len(kids):
                    return None
                return by_path(kids[idx], path[1:])

            def by_fingerprint(node: Any, depth: int) -> Optional[Any]:
                if depth > 16:
                    return None
                try:
                    er, role = _ax_get(AXUIElementCopyAttributeValue, node, "AXRole")
                    en, name = _ax_get(AXUIElementCopyAttributeValue, node, "AXTitle")
                    es, sub = _ax_get(AXUIElementCopyAttributeValue, node, "AXSubrole")
                except Exception:
                    return None
                if (role == target_role_raw
                        and clean_text(str(name)) == (target_name or "")
                        and (sub or None) == (target_subrole or None)):
                    return node
                try:
                    _, kids = _ax_get(AXUIElementCopyAttributeValue, node, "AXChildren")
                except Exception:
                    kids = None
                if kids:
                    for k in kids:
                        hit = by_fingerprint(k, depth + 1)
                        if hit is not None:
                            return hit
                return None

            found = by_path(win, target_path) if target_path else None
            if found is None:
                found = by_fingerprint(win, 0)
            # Cache for repeat use within this process.
            if found is not None:
                cached["ax_element"] = found
            return found
        except Exception:
            return None

    def _strict_background_mode(self) -> dict[str, Any]:
        """Read the strict-background session config.

        Returns ``{"enabled": bool, "expected": str|None}``. When enabled,
        the session was started with ``--strict-background`` and any action
        that would disturb the foreground must be refused, and any observed
        drift of the frontmost app aborts the current operation. Lives in
        ``session.extras`` (no schema bump).
        """
        try:
            from qcu.session import load as _load
            s = _load()
            if s is None or not getattr(s, "extras", None):
                return {"enabled": False, "expected": None}
            ex = s.extras or {}
            return {
                "enabled": bool(ex.get("strict_background")),
                "expected": ex.get("expected_foreground"),
            }
        except Exception:
            return {"enabled": False, "expected": None}

    def _frontmost_name(self) -> Optional[str]:
        """Current frontmost app name, or None if it can't be read."""
        try:
            from AppKit import NSWorkspace  # type: ignore
            fm = NSWorkspace.sharedWorkspace().frontmostApplication()
            return (fm.localizedName() or "") if fm is not None else None
        except Exception:
            return None

    def _check_strict_background(self, *, stealing_focus: bool = False) -> Optional[LayerResult]:
        """Return a failure LayerResult if strict-background mode forbids this
        action, else None (caller proceeds).

        - ``stealing_focus=True`` (launch_app/activate_app): always refused —
          these can't be made background-safe without the larger AX refactor.
        - otherwise (observe/click/...): refused only if the frontmost app has
          drifted away from the one captured at session start. This catches the
          real-world failure where Finder tasks let focus drift to Clash/飞书
          between QCU calls.
        """
        cfg = self._strict_background_mode()
        if not cfg["enabled"]:
            return None
        if stealing_focus:
            return LayerResult(
                ok=False, layer=self.name,
                message=("strict-background mode forbids this action: it would "
                         "steal the foreground. Use AX-direct actions (fill/click "
                         "on a ref) instead of launch_app/activate_app."),
            )
        expected = cfg.get("expected")
        if expected:
            current = self._frontmost_name()
            if current is not None and current.lower().removesuffix(".app") != \
                    expected.lower().removesuffix(".app"):
                return LayerResult(
                    ok=False, layer=self.name,
                    message=(f"strict-background abort: frontmost drifted to "
                             f"{current!r} (expected {expected!r}). Re-focus the "
                             f"target app or restart the session without "
                             f"--strict-background."),
                )
        return None

    def _app_pid_for(self, app: Optional[str]) -> Optional[int]:
        """Resolve an app name to a pid via NSWorkspace.

        Case-insensitive, strips a trailing ``.app``. Returns None if AppKit
        is unavailable or the app isn't running. Used by _verify_action to
        build an app element for window-scope signals and by _launch_app/
        _activate_app verification.
        """
        if not app:
            return None
        try:
            from AppKit import NSWorkspace  # type: ignore
        except Exception:
            return None
        try:
            needle = app.lower().removesuffix(".app")
            for a in NSWorkspace.sharedWorkspace().runningApplications():
                name = (a.localizedName() or "").lower().removesuffix(".app")
                if name == needle:
                    return int(a.processIdentifier())
        except Exception:
            return None
        return None

    def _focused_element_signature(self, app_elem: Any = None) -> Optional[dict[str, Any]]:
        """Read the AXFocusedUIElement of an app and return its role/name/subrole.

        Used as a before/after probe for press_key verification: a keystroke
        that changed focus (Tab, Cmd+Q, a shortcut opening a panel) shows up
        as a different signature. Returns None when no focused element can be
        resolved or AX isn't trusted.
        """
        try:
            from ApplicationServices import (
                AXUIElementCopyAttributeValue, kAXFocusedUIElementAttribute,
            )
        except Exception:
            return None
        try:
            _, focused = _ax_get(AXUIElementCopyAttributeValue, app_elem, kAXFocusedUIElementAttribute)
            if not focused:
                return None
            _, role = _ax_get(AXUIElementCopyAttributeValue, focused, "AXRole")
            _, name = _ax_get(AXUIElementCopyAttributeValue, focused, "AXTitle")
            _, sub = _ax_get(AXUIElementCopyAttributeValue, focused, "AXSubrole")
            return {
                "role": str(role) if role is not None else None,
                "name": clean_text(str(name)) if name else None,
                "subrole": str(sub) if sub is not None else None,
            }
        except Exception:
            return None

    def _ax_native_click(self, elem: Any, role: str, atype: str = "click") -> tuple[bool, Optional[str]]:
        """Press an AX element via AXUIElementPerformAction.

        Returns ``(ok, action_name)``: ``ok=True`` means the IPC succeeded;
        ``action_name`` is the actual attribute name that fired (useful for
        diagnostics). Picks the action that best matches the intent:
        - click            → AXPress / AXConfirm / AXOpen (default press)
        - double_click     → AXPress twice (some apps count presses as clicks)
        - right_click      → AXShowMenu (the AX-native context menu action)
        - hover            → no AX equivalent, returns False (caller falls back
                             to CGEvent mouse-move, which is semantically right)
        Falls back through available actions so a button without AXShowMenu but
        with AXPress still works for right_click-ish intents.

        IMPORTANT: ``ok=True`` here does NOT guarantee the click actually
        happened — AXPress on a stub-noop target (Electron with
        AXManualAccessibility off) returns err=0 with no side effect. Callers
        MUST wrap the call in ``_verify_click`` for production safety.
        """
        if atype == "hover":
            return False, None  # AX has no hover; CGEvent mouse-move is correct here.
        try:
            # PyObjC 12.2.1 on macOS 26 renames this to AXUIElementCopyActionNames.
            # Accept both so older installs still work.
            try:
                from ApplicationServices import AXUIElementCopyActionNames as _GetActionNames
            except ImportError:
                from ApplicationServices import AXUIElementGetActionNames as _GetActionNames
        except Exception:
            return False, None
        try:
            _, actions = _ax_get(_GetActionNames, elem, None)
            acts = [str(a) for a in (actions or [])]
            from ApplicationServices import AXUIElementPerformAction
            # Pick the preferred action for this atype, then fall back.
            if atype == "right_click":
                preferred = ("AXShowMenu", "AXPress")
            else:  # click / double_click
                preferred = ("AXPress", "AXConfirm", "AXOpen", "AXShowMenu", "AXToggle")
            for act_name in preferred:
                if act_name in acts:
                    # double_click: press twice with a tiny gap so apps that
                    # count presses treat it as a double-click.
                    err = AXUIElementPerformAction(elem, act_name)
                    if atype == "double_click" and err == 0 and act_name == "AXPress":
                        time.sleep(0.05)
                        AXUIElementPerformAction(elem, act_name)
                    return (err == 0), act_name
            return False, None
        except Exception:
            return False, None

    def _verify_action(
        self,
        elem: Any,
        role: str,
        action: str,
        *,
        expected_text: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run the post-action verification loop. Returns a dict the caller
        merges into ``LayerResult.data``.

        Keys: ``verified`` (yes/no/n/a), ``signal``, ``matched_at_ms``,
        ``samples``. Failure here means the action may have been a stub
        no-op; the caller (``_click``) should fall back to P4 (coordinate
        click) and re-verify.
        """
        try:
            from qcu.layers._ax_verify import (
                Verdict, signal_for, signal_for_value_set, snapshot, verify,
            )
            # Choose signal class.
            if action == "fill":
                sig = signal_for_value_set(role)
            else:
                sig = signal_for(role, action)

            # Probe the application element for window-scope signals.
            app_elem = None
            pid = self._app_pid_for(self._target_app)
            if pid:
                try:
                    from ApplicationServices import AXUIElementCreateApplication
                    app_elem = AXUIElementCreateApplication(pid)
                except Exception:
                    app_elem = None

            before = snapshot(elem, _ax_get, app=app_elem)
            verdict = verify(
                before,
                signal=sig,
                expected_text=expected_text,
                ax_getter=_ax_get,
                target=elem,
                app=app_elem,
            )
            return {
                "verified": verdict.verified.value,
                "signal": verdict.signal.value if verdict.signal else None,
                "matched_at_ms": round(verdict.matched_at_ms, 1)
                if verdict.matched_at_ms is not None
                else None,
                "samples": verdict.samples_taken,
                "verify_diagnostics": verdict.diagnostics,
            }
        except Exception as e:  # noqa: BLE001
            return {
                "verified": "n/a",
                "signal": None,
                "error": f"{type(e).__name__}: {e}",
            }

    def _ax_native_set_value(self, elem: Any, text: str) -> bool:
        """Focus + set AXValue on a text/search field via AX. Returns True on
        success. App Store search, TextEdit body, any AX-conformant text field
        all work here — no coordinates, no keyboard, no clipboard."""
        try:
            from ApplicationServices import AXUIElementSetAttributeValue
        except Exception:
            return False
        try:
            AXUIElementSetAttributeValue(elem, "AXFocused", True)
            AXUIElementSetAttributeValue(elem, "AXValue", str(text))
            return True
        except Exception:
            return False

    def _click(self, atype: str, params: dict[str, Any]) -> LayerResult:
        ref = params.get("ref")
        cx: Optional[float] = None
        cy: Optional[float] = None
        if isinstance(ref, str):
            cached = self._last_refs.get(ref)
            if cached is None:
                return LayerResult(ok=False, layer=self.name, message=f"unknown ref {ref!r}")
            cx, cy = cached.get("cx"), cached.get("cy")
        # AX-native click path (preferred): resolve the live AXUIElement and
        # Press it via AXUIElementPerformAction. No coordinates, no mouse —
        # this is what makes buttons work even when they have no bouds (icons)
        # or when the target app is a background WebKit view. Applies to click,
        # double_click, and right_click (AXShowMenu); hover has no AX equivalent
        # and goes straight to CGEvent mouse-move. Coordinate clicking below is
        # the fallback only when AX press is unavailable.
        axpress_failed_reason: Optional[str] = None
        if isinstance(ref, str) and atype in ("click", "double_click", "right_click"):
            elem = self._resolve_ax_element(ref)
            if elem is not None:
                role = (self._last_refs.get(ref) or {}).get("role", "")
                ok_ax, act_name = self._ax_native_click(elem, role, atype)
                if ok_ax:
                    # Critical: AXPress returning err=0 doesn't guarantee
                    # the action took effect (Electron stub no-op returns 0).
                    # Run the post-action verify loop; if it's a stub no-op,
                    # fall through to coordinate click + re-verify.
                    verify_meta = self._verify_action(elem, role, atype)
                    verified = verify_meta.get("verified", "n/a")
                    if verified == "yes":
                        return LayerResult(
                            ok=True,
                            layer=self.name,
                            message=(
                                f"click ref={ref} (via AXPress={act_name}, "
                                f"verified at {verify_meta.get('matched_at_ms')}ms)"
                            ),
                            data={"verification": verify_meta},
                        )
                    # verified=NO or N/A: AXPress may have been a no-op.
                    # Fall through to coordinate click and re-verify there.
                    axpress_failed_reason = (
                        f"AXPress err=0 but verify={verified} "
                        f"(signal={verify_meta.get('signal')}, "
                        f"samples={verify_meta.get('samples')}, "
                        f"diag={verify_meta.get('verify_diagnostics')})"
                    )
                else:
                    axpress_failed_reason = "AXPress returned non-zero (element didn't accept)"
            else:
                axpress_failed_reason = "AXUIElement not resolvable (no AX trust on QCU process?)"

            # Apple Events fallback (priority 60 in the router, but reachable
            # here when desktop_ax was forced as the chosen layer / ref was
            # resolved against this layer's cache). Uses the numeric path we
            # stored at observe time → "perform action AXPress of UI element N of …"
            # chain. Needs only Automation TCC, not Accessibility, so it works
            # even when ``AXIsProcessTrusted()`` is False. This was the
            # concrete bug ZCode hit: Chrome ref clicks returned
            # "click needs ref or x,y" even though the ref was cached.
            if axpress_failed_reason is not None and self._target_app:
                try:
                    from qcu.layers._ax_action import (
                        ax_path_list_to_applescript_chain,
                        perform_axpress_via_osascript,
                        perform_axpress_by_attributes,
                    )

                    # Step 1: positional path. Fast, but the AXU tree index
                    # ≠ SE UI elements index, so this can fail with "invalid
                    # index" even on a real element.
                    path = cached.get("path") or []
                    chain = ax_path_list_to_applescript_chain(path)
                    action_name = (
                        "AXShowMenu" if atype == "right_click" else "AXPress"
                    )
                    if chain is not None:
                        ok, msg = perform_axpress_via_osascript(
                            chain,
                            app=self._target_app,
                            action=action_name,
                        )
                        if ok:
                            # double_click: press a second time, ~50ms gap.
                            if atype == "double_click":
                                import time as _t
                                _t.sleep(0.05)
                                perform_axpress_via_osascript(
                                    chain, app=self._target_app, action="AXPress"
                                )
                            return LayerResult(
                                ok=True,
                                layer=self.name,
                                message=(
                                    f"click ref={ref} (via Apple Events {action_name}; "
                                    f"AX fallback skipped: {axpress_failed_reason})"
                                ),
                            )
                    # Step 2: descriptor tuple (role + subrole + name).
                    # Works even when path indices disagree between AXUIElement
                    # and System Events, which is the common case for Chrome's
                    # web-area children. ~10-20ms slower due to an
                    # ``entire contents of window 1`` scan, but uniquely
                    # identifies the element when its name is set.
                    descriptor_name = cached.get("name") or None
                    descriptor_role = cached.get("role_raw") or None
                    descriptor_subrole = cached.get("subrole") or None
                    if descriptor_role or descriptor_subrole or descriptor_name:
                        ok2, msg2 = perform_axpress_by_attributes(
                            app=self._target_app,
                            role=descriptor_role,
                            subrole=descriptor_subrole,
                            name=descriptor_name,
                            action=action_name,
                        )
                        if ok2:
                            if atype == "double_click":
                                import time as _t
                                _t.sleep(0.05)
                                perform_axpress_by_attributes(
                                    app=self._target_app,
                                    role=descriptor_role,
                                    subrole=descriptor_subrole,
                                    name=descriptor_name,
                                    action="AXPress",
                                )
                            return LayerResult(
                                ok=True,
                                layer=self.name,
                                message=(
                                    f"click ref={ref} (via Apple Events {action_name}"
                                    f" by descriptor; AX path mismatched"
                                    f"{f': {msg}' if False else ''})"
                                ),
                            )
                        axpress_failed_reason = (
                            f"{axpress_failed_reason}; Apple Events also failed: "
                            f"path→{msg[:60]} | descriptor→{msg2[:80]}"
                        )
                except Exception:
                    pass
        if "x" in params and "y" in params:
            cx, cy = float(params["x"]), float(params["y"])
        if cx is None or cy is None:
            # Distinguish "ref looked plausible but had no geometry" from
            # "act was called without ref/coords". The previous message
            # ("click needs ref or x,y") was misleading — the caller DID
            # pass ref, the issue is the AX provider didn't expose bounds
            # (e.g. Chrome with AXManualAccessibility off).
            if isinstance(ref, str) and axpress_failed_reason:
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=(
                        f"ref {ref!r} resolved but AXPress failed "
                        f"({axpress_failed_reason}); element has no bounds for "
                        f"coordinate fallback (Chrome/Safari without "
                        f"AXManualAccessibility often returns no geometry). "
                        f"Try `qcu observe --layer desktop_appleevents` then "
                        f"re-issue the click via that layer."
                    ),
                )
            return LayerResult(ok=False, layer=self.name, message="click needs ref or x,y")

        # Click-safety gate (Bug: 多显示器/遮挡下坐标点击会落到飞书/Clash 等更高
        # 栈序窗口上)。在 CGEvent 下发前确认 (cx,cy) 仍属于目标 app 的窗口；
        # 失败则 ok=False 中止，不向错误的窗口发事件。fail-open：Quartz 不可
        # 用时安全门放行（不引入新硬依赖）。
        safety = self._assert_click_target_for(cx, cy)
        if not safety["ok"]:
            from qcu.layers._click_safety import format_safety_failure
            return LayerResult(
                ok=False, layer=self.name,
                message=format_safety_failure(safety),
                data={"click_safety": safety},
            )

        try:
            from Quartz.CoreGraphics import (
                CGEventCreateMouseEvent,
                CGEventPost,
                kCGEventLeftMouseDown,
                kCGEventLeftMouseUp,
                kCGEventMouseMoved,
                kCGEventRightMouseDown,
                kCGEventRightMouseUp,
                kCGHIDEventTap,
                kCGMouseButtonLeft,
                kCGMouseButtonRight,
                kCGMouseEventClickState,
            )
            from Quartz import CGPointMake as CGPoint  # type: ignore
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"Quartz import failed: {e}")

        try:
            pt = CGPoint(cx, cy)
            if atype == "hover":
                move = CGEventCreateMouseEvent(None, kCGEventMouseMoved, pt, kCGMouseButtonLeft)
                CGEventPost(kCGHIDEventTap, move)
            elif atype == "right_click":
                # Right button down/up.
                down = CGEventCreateMouseEvent(None, kCGEventRightMouseDown, pt, kCGMouseButtonRight)
                up = CGEventCreateMouseEvent(None, kCGEventRightMouseUp, pt, kCGMouseButtonRight)
                CGEventPost(kCGHIDEventTap, down)
                CGEventPost(kCGHIDEventTap, up)
            elif atype == "double_click":
                # A real double-click: two consecutive down/up pairs at the
                # same point, each tagged with an incrementing clickState so
                # the target app interprets it as a double-click (e.g. Finder
                # rename). The old code posted one pair for both click types.
                for click_no in (1, 2):
                    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft)
                    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft)
                    down.setIntegerValueField_(kCGMouseEventClickState, click_no)
                    up.setIntegerValueField_(kCGMouseEventClickState, click_no)
                    CGEventPost(kCGHIDEventTap, down)
                    CGEventPost(kCGHIDEventTap, up)
                    time.sleep(0.02)
            else:
                down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, pt, kCGMouseButtonLeft)
                up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, pt, kCGMouseButtonLeft)
                CGEventPost(kCGHIDEventTap, down)
                CGEventPost(kCGHIDEventTap, up)
            # Final verification for P4 (coordinate click). Same _verify_action
            # as for AXPress — only meaningful when we have a live element to
            # diff. If we don't (pure coordinate click with no element_ref),
            # we accept the IPC success without verification.
            verify_meta: dict[str, Any] = {}
            if isinstance(ref, str):
                elem_after = self._resolve_ax_element(ref)
                if elem_after is not None:
                    role = (self._last_refs.get(ref) or {}).get("role", "")
                    verify_meta = self._verify_action(elem_after, role, atype)
                    verified = verify_meta.get("verified", "n/a")
                    if verified == "yes":
                        return LayerResult(
                            ok=True,
                            layer=self.name,
                            message=(
                                f"{atype} at ({cx},{cy}) (via CGEvent, verified "
                                f"at {verify_meta.get('matched_at_ms')}ms)"
                            ),
                            data={"verification": verify_meta},
                        )
                    # verified=no: the coordinate click also failed to move
                    # the needle. Surface the AXPress-failed-reason too so
                    # the LLM has the full picture.
                    return LayerResult(
                        ok=True,  # IPC succeeded
                        layer=self.name,
                        message=(
                            f"{atype} at ({cx},{cy}) (CGEvent dispatched; "
                            f"verify={verified} — possible stub no-op). "
                            f"previous AXPress failure: {axpress_failed_reason or 'n/a'}"
                        ),
                        data={"verification": verify_meta, "axpress_failed_reason": axpress_failed_reason},
                    )
            return LayerResult(ok=True, layer=self.name, message=f"{atype} at ({cx},{cy})")
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}")

    def _scroll(self, params: dict[str, Any]) -> LayerResult:
        """Vertical/horizontal scroll via a CGEvent scroll wheel.

        params: {dy: int, dx: int} — positive dy scrolls down, dx right.
        Uses CGEventCreateScrollWheelEvent2 (kCGScrollEventUnitLine).
        """
        dy = int(params.get("dy", 0))
        dx = int(params.get("dx", 0))
        if dy == 0 and dx == 0:
            return LayerResult(ok=True, layer=self.name, message="scroll 0 (noop)")
        try:
            from Quartz.CoreGraphics import (
                CGEventCreateScrollWheelEvent2,
                CGEventPost,
                kCGScrollEventUnitLine,
                kCGHIDEventTap,
            )
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"Quartz scroll import failed: {e}")
        try:
            if dy:
                ev = CGEventCreateScrollWheelEvent2(None, kCGScrollEventUnitLine, 1, dy, 0, 0)
                CGEventPost(kCGHIDEventTap, ev)
            if dx:
                # Horizontal: swap the axis via a second wheel delta. CG scroll
                # is signed; negative = the opposite direction.
                ev = CGEventCreateScrollWheelEvent2(None, kCGScrollEventUnitLine, 1, 0, dx, 0)
                CGEventPost(kCGHIDEventTap, ev)
            return LayerResult(ok=True, layer=self.name, message=f"scrolled dy={dy} dx={dx}")
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}")

    def _assert_click_target_for(self, cx: float, cy: float) -> dict[str, Any]:
        """Click-safety gate: confirm ``(cx, cy)`` still belongs to the target
        app's frontmost window before any CGEvent is posted.

        Resolves the target app's live window bounds via
        ``_vision.resolve_wid`` (cheap, one CGWindowList call) so the gate can
        also catch the "window was dragged/resized since observe()" case. If
        ``resolve_wid`` fails or the target app is unset, we still run the
        owner check — a different app owning the point is the primary signal.

        See ``qcu.layers._click_safety.assert_click_target`` for the verdict
        schema and ``format_safety_failure`` for the message rendering.
        Returns ``{"ok": True}`` when Quartz introspection is unavailable
        (fail-open) so the gate never bricks clicks on a stripped host.
        """
        from qcu.layers._click_safety import assert_click_target

        app_window_bounds: Optional[dict[str, Any]] = None
        if self._target_app:
            try:
                from qcu.layers._vision import resolve_wid

                win = resolve_wid(self._target_app)
                if win is not None:
                    app_window_bounds = win.get("bounds")
            except Exception:
                app_window_bounds = None
        return assert_click_target(
            self._target_app, cx, cy, app_window_bounds=app_window_bounds,
        )

    def _resolve_point(self, params: dict[str, Any], key_x: str, key_y: str,
                       ref_key: str) -> tuple[Optional[float], Optional[float], Optional[str]]:
        """Resolve a drag endpoint to (x, y) in Quartz points.

        Priority: explicit ``params[key_x/y]`` > ``params[ref_key]`` (resolved
        via the AX fingerprint to center coords) > None. Returns (x, y, ref)
        where ``ref`` is the ref string used (for verification) or None.
        """
        x: Optional[float] = params.get(key_x)
        y: Optional[float] = params.get(key_y)
        ref: Optional[str] = params.get(ref_key)
        if x is None or y is None:
            # also accept {from:{x,y}} nested form
            nested_key = "from" if key_x == "x1" else "to"
            nested = params.get(nested_key)
            if isinstance(nested, dict):
                x = x if x is not None else nested.get("x")
                y = y if y is not None else nested.get("y")
        if (x is None or y is None) and isinstance(ref, str):
            cached = self._last_refs.get(ref)
            if cached is None:
                return None, None, ref
            x = cached.get("cx")
            y = cached.get("cy")
        if x is None or y is None:
            return None, None, ref
        return float(x), float(y), ref

    def _drag(self, params: dict[str, Any]) -> LayerResult:
        """Press-drag-release between two points via CGEvent mouse events.

        Accepts several endpoint spellings so the LLM can use whichever is
        natural:
          - ``{"ref_from":"ref_1","ref_to":"ref_2"}`` (AX-fingerprinted)
          - ``{"from":{"x":..,"y":..},"to":{"x":..,"y":..}}``
          - ``{"x1":..,"y1":..,"x2":..,"y2":..}`` (flat)

        ``duration`` (seconds, default 0.4) and ``steps`` (default 20) control
        the interpolated move path. Some apps (Finder icon rearrange, slider
        thumbs, DnD targets) require a *real* drag with intermediate moves to
        register as a drag rather than a teleporting click — a single
        mouseDown→mouseUp at the destination does not.
        """
        try:
            from Quartz.CoreGraphics import (
                CGEventCreateMouseEvent, CGEventPost,
                kCGEventLeftMouseDown, kCGEventLeftMouseDragged, kCGEventLeftMouseUp,
                kCGEventMouseMoved,
                kCGHIDEventTap, kCGMouseButtonLeft,
            )
            from Quartz import CGPointMake as CGPoint  # type: ignore
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"Quartz import failed: {e}")

        x1, y1, ref_from = self._resolve_point(params, "x1", "y1", "ref_from")
        x2, y2, ref_to = self._resolve_point(params, "x2", "y2", "ref_to")
        if x1 is None or y1 is None or x2 is None or y2 is None:
            return LayerResult(
                ok=False, layer=self.name,
                message="drag needs both endpoints: ref_from+ref_to or x1,y1,x2,y2",
            )

        duration = max(0.0, float(params.get("duration", 0.4)))
        steps = max(1, int(params.get("steps", 20)))
        step_sleep = duration / steps if steps > 0 else 0.0

        # Click-safety gate on the drag *start* point (the mouse-down lands
        # there; same wrong-window risk as a click). The interpolated path is
        # inside the target window by construction once the start is validated.
        safety = self._assert_click_target_for(x1, y1)
        if not safety["ok"]:
            from qcu.layers._click_safety import format_safety_failure
            return LayerResult(
                ok=False, layer=self.name,
                message=format_safety_failure(safety),
                data={"click_safety": safety},
            )

        moved_px = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
        try:
            # Move to start, press, drag along the interpolated path, release.
            CGEventPost(kCGHIDEventTap,
                        CGEventCreateMouseEvent(None, kCGEventMouseMoved, CGPoint(x1, y1), kCGMouseButtonLeft))
            time.sleep(0.03)
            CGEventPost(kCGHIDEventTap,
                        CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, CGPoint(x1, y1), kCGMouseButtonLeft))
            for i in range(1, steps + 1):
                t = i / steps
                ix = x1 + (x2 - x1) * t
                iy = y1 + (y2 - y1) * t
                CGEventPost(kCGHIDEventTap,
                            CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, CGPoint(ix, iy), kCGMouseButtonLeft))
                if step_sleep:
                    time.sleep(step_sleep)
            CGEventPost(kCGHIDEventTap,
                        CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, CGPoint(x2, y2), kCGMouseButtonLeft))

            # Post-drag verification: re-read the source ref's coords/value if
            # it was AX-fingerprinted. A drag that actually moved an element
            # changes its AXPosition; a no-op drag doesn't. Coordinate-only
            # drags have nothing to re-read (verified=n/a).
            verify_meta: dict[str, Any] = {"verified": "n/a", "moved_px": round(moved_px, 1)}
            if isinstance(ref_from, str):
                cached = self._last_refs.get(ref_from) or {}
                elem = self._resolve_ax_element(ref_from)
                if elem is not None:
                    try:
                        from ApplicationServices import AXUIElementCopyAttributeValue
                        _, pos = _ax_get(AXUIElementCopyAttributeValue, elem, "AXPosition")
                        if pos is not None:
                            nx = float(getattr(pos, "x", 0))
                            ny = float(getattr(pos, "y", 0))
                            old_cx = cached.get("cx")
                            old_cy = cached.get("cy")
                            if old_cx is not None and old_cy is not None:
                                d = ((nx - old_cx) ** 2 + (ny - old_cy) ** 2) ** 0.5
                                verify_meta = {
                                    "verified": "yes" if d > 1.0 else "no",
                                    "source_moved_px": round(d, 1),
                                    "source_new_pos": [nx, ny],
                                }
                    except Exception:
                        pass
            return LayerResult(
                ok=True, layer=self.name,
                message=f"drag ({x1:.0f},{y1:.0f})→({x2:.0f},{y2:.0f}) over {duration:.2f}s (verify={verify_meta.get('verified')})",
                data={"verification": verify_meta},
            )
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}")

    def _launch_app(self, app: str) -> LayerResult:
        """Launch (or reuse) an app **without bringing it to the foreground**.

        The previous implementation called ``open -a``, which activates the
        app — every QCU call ripped the user's focus away (the "霸道"行为).
        We now go through the shared :mod:`qcu.layers._app_launch` helper,
        which prefers ``NSWorkspace.launchApplicationAtURL:options:configuration:error:``
        with ``NSWorkspaceLaunchConfigurationActivationKey=False`` and only
        falls back to ``open -a`` (which activates) when nothing else works.

        Returns a LayerResult; the message includes ``(foreground)`` when
        QCU had no choice but to disturb focus.
        """
        from qcu.layers._app_launch import launch_app_background

        ok, msg, focus_disturbed = launch_app_background(app)
        if not ok:
            return LayerResult(ok=False, layer=self.name, message=msg)
        # Verify the app actually came up with a window. Cold launches take a
        # moment to register on NSWorkspace + create an AXWindow; we poll for
        # up to ~2.5s (SwiftUI cold-launch territory) instead of a blind
        # sleep(0.8) that returned ok=true even when no window appeared.
        verify_meta = self._wait_for_app_window(app, timeout=2.5)
        self._target_app = (app or "").strip()
        try:
            from qcu.session import patch

            patch(target_app=app)
        except Exception:
            pass
        marker = " (foreground — focus disturbed)" if focus_disturbed else " (background)"
        return LayerResult(
            ok=True, layer=self.name, message=msg + marker,
            data={"verification": verify_meta, "focus_disturbed": focus_disturbed},
        )

    def _wait_for_app_window(
        self, app: str, *, timeout: float = 2.5, poll: float = 0.15
    ) -> dict[str, Any]:
        """Poll until ``app`` has at least one AXWindow, then return a
        postcondition verdict dict. Mirrors the shape produced by
        ``_verify_action`` so callers see a uniform ``verification`` block.

        Returns ``verified=yes`` (window seen) / ``verified=no`` (timed out,
        the classic silent no-op: process exists but no UI) / ``verified=n/a``
        (AX unavailable). The window title and count are attached as evidence.
        """
        try:
            from ApplicationServices import (
                AXUIElementCreateApplication, AXUIElementCopyAttributeValue,
            )
        except Exception as e:  # noqa: BLE001
            return {"verified": "n/a", "error": f"AX unavailable: {type(e).__name__}"}
        pid = self._app_pid_for(app)
        if pid is None:
            return {"verified": "no", "reason": f"no running process for {app!r}"}
        app_elem = AXUIElementCreateApplication(pid)
        deadline = time.perf_counter() + timeout
        last_count = 0
        last_title: Optional[str] = None
        while time.perf_counter() < deadline:
            try:
                _, wins = _ax_get(AXUIElementCopyAttributeValue, app_elem, "AXWindows")
                wins = list(wins or [])
            except Exception:
                wins = []
            last_count = len(wins)
            if wins:
                try:
                    _, t = _ax_get(AXUIElementCopyAttributeValue, wins[0], "AXTitle")
                    last_title = clean_text(str(t)) if t else None
                except Exception:
                    last_title = None
                return {
                    "verified": "yes",
                    "signal": "window_change",
                    "n_windows": last_count,
                    "title": last_title,
                }
            time.sleep(poll)
        return {
            "verified": "no",
            "signal": "window_change",
            "reason": f"no AXWindow for {app!r} after {timeout:.1f}s",
            "n_windows": last_count,
        }

    def _activate_app(self, app: str) -> LayerResult:
        """Bring an already-running app to the foreground.

        Walks NSWorkspace.runningApplications, matches by localizedName, and
        activates via NSApplicationActivationPolicyAccessory/activateWithOptions.
        Falls back to `open -a` (which also activates) if the app isn't found
        in the running list.
        """
        app = (app or "").strip()
        if not app:
            return LayerResult(ok=False, layer=self.name, message="activate_app needs params.app")
        try:
            from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps  # type: ignore
        except Exception as e:  # noqa: BLE001
            # No AppKit — fall back to `open -a`, which activates a running app
            # just the same.
            return self._launch_app(app)
        try:
            ws = NSWorkspace.sharedWorkspace()
            target = None
            # Case-insensitive match; 'Finder'/'finder'/'Finder.app' all work.
            needle = app.lower().removesuffix(".app")
            for a in ws.runningApplications():
                name = (a.localizedName() or "")
                if name.lower().removesuffix(".app") == needle:
                    target = a
                    break
            if target is None:
                # Not running yet — launch it (open -a launches+activates).
                return self._launch_app(app)
            target.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
            time.sleep(0.4)
            self._target_app = app
            try:
                from qcu.session import patch
                patch(target_app=app)
            except Exception:
                pass
            # Verify: the app must now be frontmost AND have a window. A bare
            # activateWithOptions_ can return silently on a background-only or
            # unresponsive app — same "ok=true but nothing happened" failure
            # mode this module exists to eliminate.
            verify_meta = self._wait_for_app_window(app, timeout=2.0)
            frontmost_now = None
            try:
                frontmost_now = (ws.frontmostApplication().localizedName() or "")
            except Exception:
                pass
            verify_meta["frontmost_after"] = frontmost_now
            if frontmost_now and frontmost_now.lower().removesuffix(".app") != app.lower().removesuffix(".app"):
                verify_meta["verified"] = "no"
                verify_meta["reason"] = (
                    f"activate did not bring {app!r} to front (frontmost={frontmost_now!r})"
                )
            return LayerResult(
                ok=True, layer=self.name,
                message=f"activated {app} (verify={verify_meta.get('verified')})",
                data={"verification": verify_meta},
            )
        except Exception as e:  # noqa: BLE001
            # Last resort: open -a.
            return self._launch_app(app)

    def _type_text(self, text: str) -> LayerResult:
        """Type text via clipboard paste (Cmd+V).

        Quartz ``CGEventCreateKeyboardEvent`` needs per-character virtual
        keycodes; without a keycode table, looping over the string with a
        fixed keycode only ever produced garbage. Pasting through the system
        clipboard works for any text (including non-ASCII) and is the same
        trick Playwright's fill uses as a fallback.

        We preserve the user's existing clipboard contents around the paste
        and restore it afterwards.
        """
        if not text:
            return LayerResult(ok=True, layer=self.name, message="typed 0 chars (empty)")
        try:
            from AppKit import NSPasteboard, NSPasteboardTypeString  # type: ignore
        except ImportError:
            # Older PyObjC used NSPboardTypeString.
            try:
                from AppKit import NSPasteboard  # type: ignore
                from AppKit import NSPboardTypeString as NSPasteboardTypeString  # type: ignore
            except Exception as e:  # noqa: BLE001
                return LayerResult(ok=False, layer=self.name, message=f"AppKit import failed: {e}")
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"AppKit import failed: {e}")

        try:
            pb = NSPasteboard.generalPasteboard()
            # Snapshot the current clipboard so we can restore it.
            saved = pb.stringForType_(NSPasteboardTypeString)
            change_count_old = pb.changeCount()

            try:
                pb.clearContents()
                pb.setString_forType_(text, NSPasteboardTypeString)
                # Press Cmd+V via the modifier-aware helper. kVK_ANSI_V = 9.
                self._post_key_code(9, 1 << 20)  # 9=V, 1<<20 = command flag
            finally:
                # Restore the previous clipboard if ours was the last writer.
                import time as _t
                _t.sleep(0.05)  # let the paste land
                if pb.changeCount() > change_count_old and saved is not None:
                    pb.clearContents()
                    pb.setString_forType_(saved, NSPasteboardTypeString)

            return LayerResult(
                ok=True, layer=self.name, message=f"typed {len(text)} chars (via clipboard)"
            )
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}")

    # macOS virtual keycodes. Covers the common named keys PLUS letters and
    # the symbol keys most often used in shortcuts (Cmd+[ , Cmd+], Cmd+,).
    # Values come from Carbon/HIToolbox/Events.h (kVK_* constants).
    _KEYCODES: dict[str, int] = {
        "Enter": 36, "Return": 36,
        "Tab": 48,
        "Escape": 53, "Esc": 53,
        "Space": 49,
        "Delete": 51, "Backspace": 51, "ForwardDelete": 117,
        "Up": 126, "Down": 125, "Left": 123, "Right": 124,
        "Home": 115, "End": 119, "PageUp": 116, "PageDown": 121,
        "F1": 122, "F2": 120, "F3": 99, "F4": 118, "F5": 96, "F6": 97,
        "F7": 98, "F8": 100, "F9": 101, "F10": 109, "F11": 103, "F12": 111,
        "[": 33, "]": 30, ",": 43, ".": 47, "/": 44,
        "-": 27, "=": 24, "`": 50, "\\": 42, ";": 41, "'": 39,
        "0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23,
        "6": 22, "7": 26, "8": 28, "9": 25,
    }

    # Lazy-filled letter table (A=0 ... Z=25) — added in __init__ to keep the
    # literal clean. Keyed case-insensitively by uppercase letter.
    @classmethod
    def _ensure_letter_keycodes(cls) -> None:
        if getattr(cls, "_letters_done", False):
            return
        # A=0,B=11,C=8,D=2,E=14,F=3,G=5,H=4,I=34,J=38,K=40,L=37,M=46,N=45,
        # O=31,P=35,Q=12,R=15,S=1,T=17,U=32,V=9,W=13,X=7,Y=16,Z=6
        letter_codes = dict(A=0, B=11, C=8, D=2, E=14, F=3, G=5, H=4, I=34,
                            J=38, K=40, L=37, M=46, N=45, O=31, P=35, Q=12,
                            R=15, S=1, T=17, U=32, V=9, W=13, X=7, Y=16, Z=6)
        cls._KEYCODES.update(letter_codes)
        cls._letters_done = True

    def _parse_key_combo(self, key: str) -> tuple[Optional[int], int]:
        """Parse a key spec like 'Cmd+Q' into (keycode, modifier_flags).

        Returns (None, 0) if the primary key can't be resolved. Modifier
        aliases: Cmd/Command, Shift, Alt/Option/Opt, Ctrl/Control.
        """
        self._ensure_letter_keycodes()
        parts = [p.strip() for p in key.split("+")]
        # The last segment is the primary key; earlier are modifiers.
        mods_bits = 0
        for mod in parts[:-1]:
            m = mod.lower()
            if m in ("cmd", "command", "super"):
                mods_bits |= 1 << 20   # kCGEventFlagMaskCommand
            elif m in ("shift",):
                mods_bits |= 1 << 17   # kCGEventFlagMaskShift
            elif m in ("alt", "option", "opt"):
                mods_bits |= 1 << 19   # kCGEventFlagMaskAlternate
            elif m in ("ctrl", "control"):
                mods_bits |= 1 << 18   # kCGEventFlagMaskControl
            elif m in ("fn", "function"):
                mods_bits |= 1 << 23   # kCGEventFlagMaskSecondaryFn
        primary = parts[-1]
        # Case-insensitive lookup for letters / named keys.
        code = self._KEYCODES.get(primary)
        if code is None and len(primary) == 1 and primary.isalpha():
            code = self._KEYCODES.get(primary.upper())
        return code, mods_bits

    def _post_key_code(self, code: int, mods_bits: int = 0) -> None:
        """Post a key down/up with modifier flags. Caller imported Quartz."""
        from Quartz.CoreGraphics import (
            CGEventCreateKeyboardEvent, CGEventPost, CGEventSetFlags, kCGHIDEventTap,
        )

        down = CGEventCreateKeyboardEvent(None, code, True)
        up = CGEventCreateKeyboardEvent(None, code, False)
        if mods_bits:
            # PyObjC exposes CGEvent modifiers via the CGEventSetFlags function,
            # not a setFlags_ method (newer PyObjC CGEventRef isn't an ObjC
            # object). This was the bug that broke every Cmd+… combo.
            CGEventSetFlags(down, mods_bits)
            CGEventSetFlags(up, mods_bits)
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)

    def _press_key(self, key: str) -> LayerResult:
        """Press a key or key combo: 'Enter', 'Cmd+Q', 'Shift+Tab', 'Cmd+Shift+3'.

        Modifier syntax is '+'-separated; the final segment is the primary key
        and the rest are modifiers (Cmd/Shift/Alt/Ctrl/Fn). Previously this
        only posted a bare single key, so every shortcut (Cmd+Q, Cmd+Tab, …)
        silently did the wrong thing — now modifiers are applied as CGEvent
        flags. Single printable chars with no modifier still work via _type_text.

        Verification: for keys that should change window/focus state (close,
        quit, save, switch, dismiss), we snapshot the frontmost app + focused
        element before and after. ``ok`` still means "key was posted"; the
        ``data.verification`` block tells you whether anything actually changed
        — a silent no-op (the classic "Cmd+S reported ok but no Save dialog")
        shows up as ``verified=no``.
        """
        if not key:
            return LayerResult(ok=False, layer=self.name, message="press_key needs params.key")
        try:
            from Quartz.CoreGraphics import CGEventCreateKeyboardEvent  # noqa: F401
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"Quartz import failed: {e}")

        # Decide up-front whether this key warrants a state-change verify, and
        # capture the "before" probe if so. Modifier-only keys, plain letters,
        # and arrows don't reliably change frontmost app or focus, so verifying
        # them would just add latency + false "no" verdicts.
        verify_key = self._is_state_changing_key(key)
        before: Optional[dict[str, Any]] = None
        if verify_key:
            before = self._window_focus_snapshot()

        def _finalize(base: LayerResult) -> LayerResult:
            """Attach a verification block to ``base`` if we probed."""
            if not verify_key or before is None:
                return base
            after = self._window_focus_snapshot()
            base.data = dict(base.data or {})
            base.data["verification"] = self._diff_window_focus(before, after)
            return base

        # Combo path: anything with a '+' modifier, or a multi-char named key.
        if "+" in key:
            code, mods_bits = self._parse_key_combo(key)
            if code is None:
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=f"can't resolve primary key in {key!r}",
                )
            try:
                self._post_key_code(code, mods_bits)
                return _finalize(LayerResult(ok=True, layer=self.name, message=f"pressed {key}"))
            except Exception as e:  # noqa: BLE001
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=(
                        f"{type(e).__name__}: {e} "
                        "(failed posting key event — missing Input Monitoring "
                        "permission? run: qcu doctor)"
                    ),
                )

        # Single key: named (Enter/Tab/…) or a lone character.
        self._ensure_letter_keycodes()
        code = self._KEYCODES.get(key)
        if code is None and len(key) == 1 and key.isalpha():
            code = self._KEYCODES.get(key.upper())
        if code is None and len(key) == 1:
            # Single non-letter printable char (punctuation not in the table):
            # type it via the clipboard path.
            return self._type_text(key)
        if code is None:
            return LayerResult(
                ok=False,
                layer=self.name,
                message=(f"unsupported key {key!r}; use a named key, a single char, "
                         "or a combo like 'Cmd+Q'"),
            )
        try:
            self._post_key_code(code, 0)
            return _finalize(LayerResult(ok=True, layer=self.name, message=f"pressed {key}"))
        except Exception as e:  # noqa: BLE001
            return LayerResult(
                ok=False,
                layer=self.name,
                message=(
                    f"{type(e).__name__}: {e} "
                    "(failed posting key event — missing Input Monitoring "
                    "permission? run: qcu doctor)"
                ),
            )

    # Keys whose effect is a change in the frontmost app or the focused
    # element. We only verify these — plain letters / arrows / function keys
    # don't have a reliable "it worked" signal at the window/focus level.
    _STATE_CHANGING_KEYS = frozenset({
        "Tab", "Escape", "Esc",
        "Enter", "Return",  # may confirm/submit and move focus
        "Cmd+Q", "Cmd+W", "Cmd+S", "Cmd+O", "Cmd+P",
        "Cmd+M", "Cmd+H",  # minimize/hide → frontmost app changes
        "Cmd+`", "Cmd+Shift+`",
        "Cmd+Tab", "Cmd+Shift+Tab",
        "Cmd+[", "Cmd+]",
    })

    def _is_state_changing_key(self, key: str) -> bool:
        """Whether ``key`` warrants a window/focus verify pass."""
        k = (key or "").strip()
        if k in self._STATE_CHANGING_KEYS:
            return True
        # Any Cmd+… combo we didn't enumerate is likely a menu shortcut that
        # could open a panel; verify it too (cheap relative to a missed Save).
        return k.lower().startswith("cmd+")

    def _window_focus_snapshot(self) -> dict[str, Any]:
        """Capture frontmost app + focused element signature for diffing."""
        snap: dict[str, Any] = {"frontmost": None, "focused": None}
        try:
            from AppKit import NSWorkspace  # type: ignore
            ws = NSWorkspace.sharedWorkspace()
            fm = ws.frontmostApplication()
            snap["frontmost"] = (fm.localizedName() or "") if fm is not None else None
        except Exception:
            pass
        try:
            from ApplicationServices import (
                AXUIElementCreateSystemWide, AXUIElementCopyAttributeValue,
                kAXFocusedUIElementAttribute,
            )
            sys_wide = AXUIElementCreateSystemWide()
            _, focused = _ax_get(AXUIElementCopyAttributeValue, sys_wide, kAXFocusedUIElementAttribute)
            if focused:
                _, role = _ax_get(AXUIElementCopyAttributeValue, focused, "AXRole")
                _, name = _ax_get(AXUIElementCopyAttributeValue, focused, "AXTitle")
                snap["focused"] = {
                    "role": str(role) if role is not None else None,
                    "name": clean_text(str(name)) if name else None,
                }
        except Exception:
            pass
        return snap

    def _diff_window_focus(
        self, before: dict[str, Any], after: dict[str, Any]
    ) -> dict[str, Any]:
        """Compare two _window_focus_snapshot() results into a verification dict."""
        fm_changed = before.get("frontmost") != after.get("frontmost")
        fb = before.get("focused") or {}
        fa = after.get("focused") or {}
        focus_changed = fb != fa
        verified = "yes" if (fm_changed or focus_changed) else "no"
        return {
            "verified": verified,
            "signal": "window_change",
            "frontmost_before": before.get("frontmost"),
            "frontmost_after": after.get("frontmost"),
            "focus_changed": focus_changed,
            "frontmost_changed": fm_changed,
            "reason": (
                None if verified == "yes"
                else "no window/focus change detected after key press — possible silent no-op"
            ),
        }

    def _fill(self, params: dict[str, Any]) -> LayerResult:
        ref = params.get("ref")
        text = params.get("text", "")
        if not isinstance(ref, str):
            return LayerResult(ok=False, layer=self.name, message="fill needs params.ref")
        cached = self._last_refs.get(ref)
        if cached is None:
            return LayerResult(ok=False, layer=self.name, message=f"unknown ref {ref!r}")
        # AX-native fill path (preferred): resolve the live AXUIElement and set
        # AXValue directly (no click, no keyboard, no clipboard). This is what
        # makes App Store search / TextEdit body / any AX-conformant field
        # writable without needing geometry or stable focus. Below is the
        # coordinate+keyboard fallback only used if AX set fails.
        elem = self._resolve_ax_element(ref)
        if elem is not None and self._ax_native_set_value(elem, str(text)):
            # Verify the write landed. AXValue setattr on a well-behaved native
            # app is synchronous, but some apps (SwiftUI fields with input
            # masks) reformat the value. The verify loop catches both "the
            # write was a no-op" and "the app rejected or reformatted my input".
            role = cached.get("role", "")
            verify_meta = self._verify_action(elem, role, "fill", expected_text=str(text))
            verified = verify_meta.get("verified", "n/a")
            extra_msg = ""
            if verified == "no":
                # Surface the actual value seen — the app may have reformatted
                # it (date picker auto-slash / number-strip-leading-zero), not
                # failed. Give the LLM the raw read-back so it can decide.
                diag = verify_meta.get("verify_diagnostics") or {}
                actual_value = diag.get("value_after")
                extra_msg = (
                    f" (verify=no; app may have reformatted — actual value was "
                    f"{actual_value!r} (expected {str(text)!r}))"
                )
            elif verified == "n/a":
                extra_msg = " (verify unavailable)"
            return LayerResult(
                ok=True, layer=self.name,
                message=f"filled ref={ref} with {len(str(text))} chars (via AXValue, verify={verified}){extra_msg}",
                data={"verification": verify_meta},
            )
        cx, cy = cached.get("cx"), cached.get("cy")
        # If the ref has geometry, click into it first to focus it. If it has
        # NO geometry (common for AXTextArea / scroll-area bodies, which don't
        # report AXPosition/AXSize on all apps), skip the click and type into
        # whatever is currently focused — for a freshly-launched TextEdit/Notes
        # the body is focused by default, so the paste still lands correctly.
        if cx is not None and cy is not None:
            click_res = self._click("click", {"x": cx, "y": cy})
            if not click_res.ok:
                return click_res
        return self._type_text(str(text))

    def _screenshot(self, path: Optional[str]) -> LayerResult:
        try:
            from Quartz import CGWindowListCreateImage  # type: ignore
            from Quartz import kCGWindowListOptionOnScreenOnly  # type: ignore
            from Quartz import kCGNullWindowID  # type: ignore
            from Quartz.CoreGraphics import CGRectInfinite  # type: ignore
            from Quartz import CGImageDestinationCreateWithURL  # type: ignore
            from Quartz import CGImageDestinationAddImage  # type: ignore
            from Quartz import CGImageDestinationFinalize  # type: ignore
            from CoreFoundation import CFURLCreateWithFileSystemPath  # type: ignore
            from CoreFoundation import kCFURLPOSIXPathStyle  # type: ignore
            import Quartz  # type: ignore
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"Quartz screenshot failed: {e}")
        try:
            img = CGWindowListCreateImage(
                CGRectInfinite,
                kCGWindowListOptionOnScreenOnly,
                kCGNullWindowID,
                0,
            )
            # A None/all-black capture is the classic Screen Recording
            # denied fingerprint on modern macOS. Tell the user how to fix
            # it instead of leaving a generic "screenshot failed".
            if img is None:
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=(
                        "CGWindowListCreateImage returned no image — "
                        "missing Screen Recording permission? run: qcu doctor "
                        "(after granting, restart the QCU session for it to "
                        "take effect)"
                    ),
                )
            if path is None:
                return LayerResult(ok=True, layer=self.name, message="screenshot (in-memory only)")
            url = CFURLCreateWithFileSystemPath(None, path, kCFURLPOSIXPathStyle, False)
            dest = CGImageDestinationCreateWithURL(url, "public.png", 1, None)
            CGImageDestinationAddImage(dest, img, None)
            CGImageDestinationFinalize(dest)
            return LayerResult(ok=True, layer=self.name, message=f"screenshot saved to {path}")
        except Exception as e:  # noqa: BLE001
            return LayerResult(
                ok=False,
                layer=self.name,
                message=(
                    f"{type(e).__name__}: {e} "
                    "(screenshot failed — if the screen is non-empty this is "
                    "usually a missing Screen Recording permission; run: qcu doctor)"
                ),
            )

    def close(self) -> None:
        self._last_refs = {}