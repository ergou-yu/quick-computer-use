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
from qcu.common.refs import NativeRefRegistry
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


def _ax_geometry(value: Any, *, size: bool = False) -> Any:
    """Decode PyObjC AXValueRef geometry; test/provider structs also work."""
    required = ("width", "height") if size else ("x", "y")
    if all(hasattr(value, attr) for attr in required):
        return value
    try:
        from ApplicationServices import AXValueGetValue, kAXValueCGPointType, kAXValueCGSizeType
        ok, decoded = AXValueGetValue(value, kAXValueCGSizeType if size else kAXValueCGPointType, None)
        return decoded if ok else None
    except Exception:
        return None


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
        self._refs = NativeRefRegistry(self.name)
        self._target_scope: dict[str, Any] = {}
        self._target_window: Any = None
        self._ref_error = ""
        self._binding_error: Optional[str] = None
        self._observed_control_count: Optional[int] = None
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
        # Native handles are process-local. Persist target identity for a later
        # observation, but never reconstruct handles from a numeric path/name.
        try:
            from qcu.session import load as _load
            session = _load()
            meta = ((session.last_observation or {}).get("routing_meta") or {}) if session else {}
            if meta.get("layer") == self.name:
                self._target_scope = dict(meta.get("target") or {})
                if self._target_scope:
                    self._binding_error = "target_requires_observe"
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
    # Enhanced-UI unlock (Catalyst/WebKit and Chromium/Electron trees)
    # ------------------------------------------------------------------

    def _ax_set_attr(self, elem: Any, attr: str, value: Any) -> int:
        """Set one AX attribute and return the raw AXError code (0 == success).

        Kept as a one-line seam so tests can substitute a fake setter without
        stubbing the whole ApplicationServices module.
        """
        from ApplicationServices import AXUIElementSetAttributeValue
        return int(AXUIElementSetAttributeValue(elem, attr, value))

    def _ax_get_attr(self, elem: Any, attr: str) -> tuple[int, Any]:
        """Read one AX attribute; returns ``(AXError, value)``. Test seam."""
        from ApplicationServices import AXUIElementCopyAttributeValue
        return _ax_get(AXUIElementCopyAttributeValue, elem, attr)

    def _ensure_enhanced_ui(self, app: Any, pid: int) -> dict[str, Any]:
        """Best-effort unlock of the target app's full AX tree.

        Catalyst/WebKit apps (Notes, App Store, Music, TV) and Chromium/
        Electron apps expose little or no window content until a client
        asserts ``AXEnhancedUserInterface``; Chromium additionally honors
        ``AXManualAccessibility`` to keep accessibility enabled. This is the
        same flag VoiceOver flips on — setting it is standard AX-client
        behavior, not a workaround.

        The pid is cached in ``self._enhanced_apps`` ONLY after the unlock is
        confirmed (a set succeeded, or a rejected set is disproved by a
        read-back showing the flag already True — newer macOS builds return
        kAXErrorNotImplemented for a set while the attribute is already on).
        A genuinely failed unlock is NOT cached: the next observe retries.
        The returned status dict is surfaced in routing_meta so a sparse tree
        can be attributed to a failed unlock instead of being misreported as
        "the app has no controls".
        """
        if pid in self._enhanced_apps:
            return {"status": "already_set"}
        try:
            err = self._ax_set_attr(app, "AXEnhancedUserInterface", True)
        except Exception as exc:  # noqa: BLE001 — pyobjc missing or broken
            return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        # Chromium/Electron honor BOTH flags: some builds reject
        # AXEnhancedUserInterface (kAXErrorNotImplemented -25208) yet still
        # accept AXManualAccessibility. A rejected enhanced set must not skip
        # the manual attempt.
        try:
            manual_err: Optional[int] = self._ax_set_attr(app, "AXManualAccessibility", True)
        except Exception:  # noqa: BLE001 — best-effort secondary flag
            manual_err = None
        if err != 0 and manual_err != 0:
            # Both sets rejected. Before declaring failure, read the enhanced
            # flag back: on newer macOS the set returns kAXErrorNotImplemented
            # even though the attribute is already True (e.g. Cursor reads
            # back True after rejecting the set with -25208).
            try:
                gerr, current = self._ax_get_attr(app, "AXEnhancedUserInterface")
            except Exception:  # noqa: BLE001
                gerr, current = -1, None
            if gerr == 0 and bool(current):
                self._enhanced_apps.add(pid)
                return {"status": "enabled", "via": "already_true",
                        "enhanced_ui_error": err,
                        "manual_accessibility_error": manual_err}
            meta = {"status": "failed", "ax_error": err}
            if manual_err is not None:
                meta["manual_accessibility_error"] = manual_err
            return meta
        self._enhanced_apps.add(pid)
        meta = {"status": "enabled"}
        if err != 0:
            # Unlocked via AXManualAccessibility alone; keep the enhanced
            # error visible so a still-sparse tree stays attributable.
            meta["via"] = "AXManualAccessibility"
            meta["enhanced_ui_error"] = err
        elif manual_err:
            meta["manual_accessibility_error"] = manual_err
        return meta

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        # Every observation attempt retires previous refs, including failed reads.
        self._last_refs = {}
        self._binding_error = "observation_not_completed"
        self._observed_control_count = None
        self._refs.begin(self._target_scope)
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
        # Explicit identity never falls back to a different app/frontmost window.
        supplied_app = options.get("app")
        supplied_pid = options.get("pid")
        if supplied_pid is not None:
            try:
                if isinstance(supplied_pid, bool) or int(supplied_pid) <= 0:
                    raise ValueError("pid must be positive")
                supplied_pid = int(supplied_pid)
            except (TypeError, ValueError):
                return self._target_failure("invalid_target", "Explicit pid must be a positive integer.")
        if supplied_app is not None and (not isinstance(supplied_app, str) or not supplied_app.strip()):
            return self._target_failure("invalid_target", "Explicit app must be a nonempty string.")
        if options.get("window") is not None and not str(options["window"]).strip():
            return self._target_failure("invalid_target", "Explicit window must be a nonempty title filter.")
        inherited = supplied_app is None and supplied_pid is None
        wanted_pid = supplied_pid if supplied_pid is not None else (self._target_scope.get("pid") if inherited else None)
        wanted_app = supplied_app or (None if wanted_pid else self._target_app)
        apps = list(ws.runningApplications())
        if wanted_pid:
            candidates = [a for a in apps if int(a.processIdentifier()) == int(wanted_pid)]
            if supplied_app:
                candidates = [a for a in candidates if self._matches_app(str(supplied_app), a)]
        elif wanted_app:
            candidates = [a for a in apps if self._matches_app(str(wanted_app), a)]
        else:
            frontmost = ws.frontmostApplication()
            candidates = [frontmost] if frontmost else []
        if len(candidates) != 1:
            return self._target_failure(
                "app_not_found" if not candidates else "ambiguous_app",
                "Target application is missing or ambiguous; specify a live --pid.",
                [{"pid": int(a.processIdentifier()), "app": str(a.localizedName() or "")} for a in candidates],
            )
        front = candidates[0]
        app = AXUIElementCreateApplication(front.processIdentifier())
        self._target_app = str(front.localizedName() or wanted_app or "")
        # Unlock the app's accessibility tree BEFORE reading windows/children:
        # Catalyst/WebKit and Chromium/Electron apps expose little or no window
        # content over AX until a client asserts the enhanced-UI flags, so the
        # very first observe of such an app must set them up front.
        enhanced_meta = self._ensure_enhanced_ui(app, int(front.processIdentifier()))
        prior_scope = self._target_scope
        scope = {"pid": int(front.processIdentifier()), "app": self._target_app}
        self._target_window = None
        try:
            werr, windows = _ax_get(AXUIElementCopyAttributeValue, app, "AXWindows")
            if werr != 0:
                return self._target_failure("target_inaccessible", "Cannot read target windows; this is not an empty UI.")
            windows = list(windows or [])
        except Exception as exc:
            return self._target_failure("target_inaccessible", f"Cannot read target windows: {exc}")
        window_candidates = []
        for window in windows:
            _, wt = _ax_get(AXUIElementCopyAttributeValue, window, "AXTitle")
            window_candidates.append((window, self._window_identity(window, scope["pid"], str(wt or ""))))
        win_filter = options.get("window")
        if win_filter is not None:
            matched = [(w, info) for w, info in window_candidates
                       if str(win_filter).casefold() in info["window_title"].casefold()]
        elif inherited and (prior_scope.get("window_id") is not None or prior_scope.get("window_handle")):
            matched = [(w, info) for w, info in window_candidates
                       if (info.get("window_id") == prior_scope["window_id"]
                           if prior_scope.get("window_id") is not None else
                           info.get("window_handle") == prior_scope.get("window_handle"))]
        else:
            matched = []
        window_scoped = win_filter is not None or (inherited and bool(prior_scope.get("window_handle") or prior_scope.get("window_id")))
        if window_scoped:
            if len(matched) != 1:
                return self._target_failure(
                    "window_not_found" if not matched else "ambiguous_window",
                    "Target window is missing or ambiguous; observe with a unique window title.",
                    [info for _, info in (matched or window_candidates)],
                )
            self._target_window, window_info = matched[0]
            scope.update(window_info)
        self._target_scope = scope
        self._refs.begin(scope)

        title = ""
        try:
            terr, tval = _ax_get(AXUIElementCopyAttributeValue, app, kAXTitleAttribute)
            if terr == 0 and tval:
                title = str(tval)
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
        # caller understands which native provider exposed these controls.
        # It cannot establish a browser tab identity or authorize a new target.
        found_web_area: bool = False
        visited: set[int] = set()
        node_limit = max(1, min(int(options.get("max_nodes", 2000)), 10000))

        def walk(elem: Any, depth: int, path: list[int]) -> None:
            nonlocal counter, found_web_area
            if depth > (max_depth if max_depth > 0 else 64) or len(visited) >= node_limit:
                return
            identity = hash(elem)
            if identity in visited:
                return
            visited.add(identity)
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
            # AXStaticText (including Calculator's result) often has no title;
            # its visible text lives in AXValue. Read only informational roles,
            # keeping the fast path for anonymous layout containers intact.
            if not name_s and role_norm in {"text", "heading", "cell"}:
                try:
                    text_err, text_value = _ax_get(AXUIElementCopyAttributeValue, elem, "AXValue")
                    if text_err == 0 and text_value is not None:
                        name_s = clean_text(text_value, max_len=1000)
                except Exception:
                    pass

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
                # remains useful native metadata; it is never a relocation key.
                try:
                    _, subrole = _ax_get(AXUIElementCopyAttributeValue, elem, "AXSubrole")
                except Exception:
                    subrole = None
                # AXSearchField is a search box — surface it as searchbox so the
                # LLM treats it as searchable (normalize would otherwise call it
                # a plain edit, hiding its real function).
                if subrole == "AXSearchField":
                    role_norm = "searchbox"
                # SwiftUI controls (Calculator keypad, most modern Apple apps)
                # keep their accessible label in AXDescription, not AXTitle —
                # without this fallback every SwiftUI button is anonymous
                # ("button ''") and the LLM can only guess by list position.
                # Fetched only for interactive elements with an empty title so
                # the role-first traversal stays cheap.
                if not name_s:
                    try:
                        desc_err, desc = _ax_get(
                            AXUIElementCopyAttributeValue, elem, "AXDescription"
                        )
                    except Exception:
                        desc_err, desc = -1, None
                    if desc_err == 0 and desc:
                        name_s = clean_text(str(desc))
                value_s = clean_text(value, max_len=1000) if value_err == 0 and value is not None else ""

                bounds: Optional[Rect] = None
                cx: Optional[float] = None
                cy: Optional[float] = None
                pos = _ax_geometry(pos)
                size = _ax_geometry(size, size=True)
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

                ref = self._refs.issue(counter)
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
                    # session.json. A new backend must observe again.
                    "ax_element": elem,
                    "target": dict(self._target_scope),
                    "ax_window": self._element_window(elem),
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

        # One explicit window means one traversal root. Never widen on a miss.
        walk(self._target_window if self._target_window is not None else app, 0, [])
        self._last_refs = new_refs
        self._binding_error = None
        self._observed_control_count = len(elements)
        try:
            from qcu.session import patch
            patch(target_app=self._target_app, last_refs=[
                {"ref": ref, "role": info["role"], "name": info["name"],
                 "target": info["target"]} for ref, info in new_refs.items()
            ])
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
        if running_apps and not any(options.get(k) for k in ("compact", "app", "pid", "window")):
            preview = ", ".join(running_apps[:40])
            header.append(f"# running apps ({len(running_apps)}): {preview}")
            header.append("# use activate_app or launch_app to switch target")
            header.append("")
        tree_text = "\n".join(header + raw_lines) if raw_lines else ("\n".join(header) or None)

        # A native AX observation remains bound to its app/window. Browser tab
        # metadata from a second transport cannot identify this AX target.
        web_app_meta: Optional[dict[str, Any]] = None
        web_app_hint = ""

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
        # Keep bounded informational text wherever it occurs in the tree;
        # limiting this to the first six raw lines lost result/status labels.
        context_lines = [line for line in raw_lines if line and not line.lstrip().startswith("[")]
        context_limit = max(0, int(options.get("text_limit", 80)))
        visible_context = context_lines if options.get("full_text") else context_lines[:context_limit]
        ui_lines.extend(visible_context)
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

        web_view_meta: Optional[dict[str, Any]] = None
        if found_web_area:
            web_view_meta = {"app": self._target_app, "url": None,
                             "reason": "AXWebArea observed; only the controls actually exposed by this target are available. Browser tab identity is not established by AX."}
            tree_text = ("# Embedded web view: inspect the returned controls and target capabilities; "
                         "keep this app/window bound.\n" + (tree_text or ""))

        return Observation(
            context="desktop",
            url_or_app=title or None,
            title=title or None,
            elements=sliced,
            routing_meta={
                "layer": self.name,
                "trusted": True,
                "target": dict(self._target_scope),
                "ref_scope": self._refs.scope,
                "capabilities": self.capabilities(),
                "enhanced_ui": enhanced_meta,
                "empty_tree_reason": None if elements else "no_controls_observed; provider may be incomplete or inaccessible",
                "n_refs": len(elements),  # total in this observation
                "n_returned": page_meta["n_returned"],
                "n_total": page_meta["n_total"],
                "n_interactive": page_meta["n_interactive"],
                "tree_truncated": page_meta["tree_truncated"],
                "text_dropped": len(context_lines)-len(visible_context),
                # ``running_apps`` was unconditionally emitting ~250 process
                # names (~3-4KB) into the JSON even under ``--compact``,
                # drowning the high-signal ``web_app`` probe. We now keep the
                # count and a short preview; the full list stays available as
                # ``running_apps_full`` unless compact wants the tightest
                # payload.
                "running_apps_count": len(running_apps),
                "running_apps_preview": [] if options.get("compact") else running_apps[:30],
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
        params = action.params or {}
        condition = params.get("verify")
        if condition is not None:
            from qcu.common.verification import validate_condition
            try:
                validate_condition(condition)
            except ValueError as error:
                return LayerResult(ok=False, layer=self.name, message=str(error),
                                   data={"reason": "invalid_verification"}, dispatch_state="not_sent")
        for key in ("ref", "ref_from", "ref_to"):
            ref = params.get(key)
            if isinstance(ref, str) and self._resolve_ax_element(ref) is None:
                return self._invalid_ref_result(ref)
        if isinstance(condition, dict) and condition.get("ref"):
            if self._resolve_ax_element(condition["ref"], for_action=False) is None:
                return self._invalid_ref_result(condition["ref"])
        # Explicit action options cannot reinterpret an already-observed target.
        for key in ("pid", "app"):
            if action.type not in {"launch_app", "activate_app"} and key in params and self._target_scope.get(key) is not None:
                if str(params[key]).casefold() != str(self._target_scope[key]).casefold():
                    return LayerResult(ok=False, layer=self.name, message="Action target differs from observed target; observe again.",
                                       data={"reason": "target_mismatch"}, dispatch_state="not_sent")
        if action.type not in {"launch_app", "activate_app"}:
            window_id = params.get("window_id")
            window = params.get("window")
            mismatch = False
            if window_id is not None:
                try:
                    mismatch = int(window_id) != self._target_scope.get("window_id")
                except (TypeError, ValueError):
                    mismatch = True
            if window is not None:
                mismatch = mismatch or not str(window).strip() or not self._target_scope.get("window_title") or str(window).casefold() not in self._target_scope["window_title"].casefold()
            if mismatch:
                return LayerResult(ok=False, layer=self.name, message="Action window differs from the observed window; observe the requested window first.",
                                   data={"reason": "target_mismatch"}, dispatch_state="not_sent")
        if action.type in {"type", "press_key", "scroll", "go_back", "go_forward"}:
            denied = self._check_foreground_target()
            if denied is not None:
                return denied
        result = self._act(action)
        if condition is not None and result.dispatch_state != "not_sent":
            verification = self._verify_condition(condition)
            result.data["verification"] = verification
            result.outcome = "verified" if verification.get("verified") else "unknown"
            result.ok = bool(verification.get("verified"))
            if result.ok:
                result.data.pop("reason", None)
                result.message = f"{action.type}: requested postcondition confirmed"
            else:
                result.data.update(reason="outcome_unknown", retry_safe=False)
                result.message = f"{action.type}: dispatched; requested postcondition unconfirmed"
        return result

    def _check_foreground_target(self) -> Optional[LayerResult]:
        if not self._target_scope or self._binding_error:
            return LayerResult(ok=False, layer=self.name, message="Foreground input requires an observed target.",
                               data={"reason": "target_unbound"}, dispatch_state="not_sent")
        try:
            from AppKit import NSWorkspace
            from ApplicationServices import AXUIElementCreateApplication, AXUIElementCopyAttributeValue
            current = NSWorkspace.sharedWorkspace().frontmostApplication()
            valid = current is not None and int(current.processIdentifier()) == self._target_scope.get("pid")
            if valid and self._target_window is not None:
                err, focused = _ax_get(AXUIElementCopyAttributeValue,
                                      AXUIElementCreateApplication(int(current.processIdentifier())), "AXFocusedWindow")
                valid = err == 0 and focused == self._target_window
            if valid:
                return None
        except Exception:
            pass
        return LayerResult(ok=False, layer=self.name, message="Foreground input target cannot be confirmed; no event sent.",
                           data={"reason": "foreground_target_unconfirmed"}, dispatch_state="not_sent")

    def _verify_condition(self, condition: dict[str, Any]) -> dict[str, Any]:
        from qcu.common.verification import match_condition
        deadline = time.monotonic() + min(max(float(condition.get("timeout_ms", 1000)), 0), 10000) / 1000
        while True:
            result = match_condition(condition, self._verification_elements(condition))
            if result.get("verified") or time.monotonic() >= deadline:
                return result
            time.sleep(0.05)

    def _verification_elements(self, condition: dict[str, Any]) -> list[Element]:
        """Read the same scope without issuing refs or replacing the action cache."""
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue, AXUIElementCreateApplication
            ref = condition.get("ref")
            if ref:
                root = self._resolve_ax_element(ref, for_action=False)
                if root is None:
                    return []
            else:
                root = self._target_window
                if root is None and self._target_scope.get("pid"):
                    root = AXUIElementCreateApplication(self._target_scope["pid"])
            if root is None:
                return []
            pending, elements, visited = [(root, 0)], [], set()
            while pending and len(visited) < 500:
                element, depth = pending.pop()
                identity = hash(element)
                if identity in visited or depth > 12:
                    continue
                visited.add(identity)
                def get(attr):
                    err, value = _ax_get(AXUIElementCopyAttributeValue, element, attr)
                    return value if err == 0 else None
                role = get("AXRole")
                if not role:
                    continue
                value = get("AXValue")
                name = get("AXTitle") or get("AXDescription")
                normalized = from_ax(str(role))
                elements.append(Element(ref=ref or "", role=normalized,
                    name=str(name or ""), value=str(value) if value is not None else None,
                    properties={"checked": bool(value) if normalized in {"checkbox", "switch", "radio"} and value is not None else None,
                                "selected": get("AXSelected")}))
                if not ref:
                    pending.extend((child, depth+1) for child in get("AXChildren") or [])
            return elements
        except Exception:
            return []

    def _act(self, action: Action) -> LayerResult:
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
                data=dict(r.data), dispatch_state=r.dispatch_state, outcome=r.outcome,
            )
        return LayerResult(ok=False, layer=self.name, message=f"unsupported action type: {atype}")

    # ------------------------------------------------------------------
    # AX-native element resolution + actions (no coordinates, no screenshot)
    # ------------------------------------------------------------------

    def capabilities(self) -> dict[str, Any]:
        from qcu.platforms import desktop_capabilities
        report = desktop_capabilities(layer=self.name)
        trusted = self.is_trusted(prompt=False) if self._available else False
        accessible = False
        reason = "target_unbound"
        if not self._available:
            reason = "PyObjC ApplicationServices/Quartz not installed"
        elif not trusted:
            reason = "Accessibility permission not granted"
        elif self._binding_error:
            reason = self._binding_error
        elif self._target_scope.get("pid"):
            try:
                from ApplicationServices import AXUIElementCreateApplication, AXUIElementCopyAttributeValue
                root = self._target_window if self._target_window is not None else AXUIElementCreateApplication(self._target_scope["pid"])
                err, role = _ax_get(AXUIElementCopyAttributeValue, root, "AXRole")
                err2, children = _ax_get(AXUIElementCopyAttributeValue, root, "AXChildren")
                accessible = err == 0 and bool(role) and err2 == 0
                reason = None if accessible else "target_access_probe_failed"
            except Exception:
                reason = "target_access_probe_failed"
        can_read = bool(self._observed_control_count) if accessible else False
        if accessible and not self._observed_control_count:
            can_read = None
            reason = "empty_or_unexposed_tree" if self._observed_control_count == 0 else "target_not_observed"
        report.update(dependencies_present=self._available, session_accessible=accessible,
                      can_read_controls=can_read, available=accessible and can_read is True, reason=reason,
                      target=dict(self._target_scope))
        return report

    def _target_failure(self, reason: str, message: str, candidates=None) -> Observation:
        self._binding_error = reason
        return Observation(context="desktop", elements=[], raw_tree=f"# {message}",
                           routing_meta={"layer": self.name, "error": message, "reason": reason,
                                         "target": dict(self._target_scope), "candidates": candidates or []})

    @staticmethod
    def _element_window(elem: Any) -> Any:
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            err, win = _ax_get(AXUIElementCopyAttributeValue, elem, "AXWindow")
            return win if err == 0 else None
        except Exception:
            return None

    @staticmethod
    def _window_identity(window: Any, pid: int, title: str) -> dict[str, Any]:
        info: dict[str, Any] = {"window_title": title, "window_handle": str(hash(window))}
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            title_error, current_title = _ax_get(AXUIElementCopyAttributeValue, window, "AXTitle")
            if title_error == 0 and current_title is not None:
                title = str(current_title)
                info["window_title"] = title
            err, wid = _ax_get(AXUIElementCopyAttributeValue, window, "AXWindowNumber")
            if err == 0 and wid:
                info["window_id"] = int(wid)
        except Exception:
            pass
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            _, raw_pos = _ax_get(AXUIElementCopyAttributeValue, window, "AXPosition")
            _, raw_size = _ax_get(AXUIElementCopyAttributeValue, window, "AXSize")
            pos, size = _ax_geometry(raw_pos), _ax_geometry(raw_size, size=True)
            if pos is not None and size is not None and size.width > 0 and size.height > 0:
                info["bounds"] = {"X": float(pos.x), "Y": float(pos.y),
                                  "Width": float(size.width), "Height": float(size.height)}
        except Exception:
            pass
        if not info.get("window_id"):
            try:
                from Quartz import CGWindowListCopyWindowInfo, kCGWindowListOptionOnScreenOnly, kCGNullWindowID
                matches = []
                for candidate in CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly, kCGNullWindowID):
                    if int(candidate.get("kCGWindowOwnerPID") or 0) != pid:
                        continue
                    candidate_title = str(candidate.get("kCGWindowName") or "")
                    candidate_bounds = dict(candidate.get("kCGWindowBounds") or {})
                    if info.get("bounds"):
                        # Screen Recording denial can redact CG titles; unique
                        # exact AX/CG bounds within one pid still identify a window.
                        if candidate_bounds != info["bounds"] or (candidate_title and candidate_title != title):
                            continue
                    elif not title or candidate_title != title:
                        continue
                    matches.append(candidate)
                if len(matches) == 1:
                    info["window_id"] = int(matches[0]["kCGWindowNumber"])
            except Exception:
                pass
        return info

    def _resolve_ax_element(self, ref: str, *, for_action: bool = True) -> Optional[Any]:
        """Validate the exact live handle; never relocate an expired native ref."""
        valid, reason = self._refs.validate(ref, self._target_scope)
        self._ref_error = reason
        if not valid:
            return None
        cached = self._last_refs.get(ref)
        if not cached or cached.get("ax_element") is None:
            self._ref_error = "ref_handle_missing"
            return None
        elem = cached["ax_element"]
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue, AXUIElementCreateApplication
            pid = self._elem_pid(elem)
            if pid is None or pid != self._target_scope.get("pid"):
                self._ref_error = "ref_target_mismatch"
                return None
            err, role = _ax_get(AXUIElementCopyAttributeValue, elem, "AXRole")
            if err != 0 or not role or str(role) != cached.get("role_raw"):
                self._ref_error = "element_stale"
                return None
            window = self._element_window(elem)
            expected = self._target_window if self._target_window is not None else cached.get("ax_window")
            if expected is not None:
                err, windows = _ax_get(AXUIElementCopyAttributeValue, AXUIElementCreateApplication(pid), "AXWindows")
                if window != expected or err != 0 or not any(w == expected for w in windows or []):
                    self._ref_error = "ref_window_mismatch"
                    return None
            err, enabled = _ax_get(AXUIElementCopyAttributeValue, elem, "AXEnabled")
            if for_action and (err != 0 or enabled is None):
                self._ref_error = "element_state_unavailable"
                return None
            if for_action and not bool(enabled):
                self._ref_error = "element_disabled"
                return None
        except Exception:
            self._ref_error = "element_inaccessible"
            return None
        self._ref_error = "current"
        return elem

    def _invalid_ref_result(self, ref: str) -> LayerResult:
        return LayerResult(ok=False, layer=self.name,
                           message=f"Ref {ref!r} is invalid ({self._ref_error}); observe the target again.",
                           data={"reason": self._ref_error, "dispatched": False}, dispatch_state="not_sent")

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
            for a in NSWorkspace.sharedWorkspace().runningApplications():
                if self._matches_app(app, a):
                    return int(a.processIdentifier())
        except Exception:
            return None
        return None

    @staticmethod
    def _matches_app(app: str, running: Any) -> bool:
        """Match an absolute bundle path exactly; names must not pick a clone."""
        from pathlib import Path
        if Path(app).is_absolute():
            try:
                bundle = running.bundleURL()
                return bundle is not None and Path(str(bundle.path())).resolve() == Path(app).resolve()
            except Exception:
                return False
        return (running.localizedName() or "").lower().removesuffix(".app") == app.lower().removesuffix(".app")

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
        act_name = None
        try:
            _, actions = _ax_get(_GetActionNames, elem, None)
            acts = [str(a) for a in (actions or [])]
            from ApplicationServices import AXUIElementPerformAction
            # Pick the preferred action for this atype, then fall back.
            if atype == "right_click":
                preferred = ("AXShowMenu",)
            else:  # click / double_click
                preferred = ("AXPress", "AXConfirm", "AXOpen", "AXToggle")
            for candidate in preferred:
                if candidate in acts:
                    act_name = candidate
                    # double_click: press twice with a tiny gap so apps that
                    # count presses treat it as a double-click.
                    err = AXUIElementPerformAction(elem, act_name)
                    if atype == "double_click" and err == 0 and act_name == "AXPress":
                        time.sleep(0.05)
                        err = AXUIElementPerformAction(elem, act_name)
                    return (err == 0), act_name
            return False, None
        except Exception:
            return False, act_name

    @staticmethod
    def _elem_pid(elem: Any) -> Optional[int]:
        """Pid of the process that owns an AXUIElementRef.

        Handles the pyobjc quirk that the C out-parameter comes back as an
        ``(err, pid)`` tuple. Returns None on any failure — callers treat
        that as "no window-scope verification possible".
        """
        try:
            from ApplicationServices import AXUIElementGetPid
            got = AXUIElementGetPid(elem, None)
            if isinstance(got, tuple):
                got = got[1] if len(got) > 1 and got[0] == 0 else None
            return int(got) if got else None
        except Exception:
            return None

    def _pre_action_snapshot(self, elem: Any) -> Any:
        """Snapshot the element's AX state BEFORE an action fires.

        Returns None if the probe cannot run. A click with no pre-action
        baseline must report uncertainty rather than invent a late baseline.
        """
        try:
            from qcu.layers._ax_verify import snapshot
            pid = self._elem_pid(elem) or self._target_scope.get("pid")
            app_elem = None
            if pid:
                from ApplicationServices import AXUIElementCreateApplication
                app_elem = AXUIElementCreateApplication(pid)
            return snapshot(elem, _ax_get, app=app_elem)
        except Exception:
            return None

    def _verify_action(
        self,
        elem: Any,
        role: str,
        action: str,
        *,
        expected_text: Optional[str] = None,
        window_ms: Optional[int] = None,
        before: Optional[Any] = None,
    ) -> dict[str, Any]:
        """Run the post-action verification loop. Returns a dict the caller
        merges into ``LayerResult.data``.

        Keys: ``verified`` (yes/no/n/a), ``signal``, ``matched_at_ms``,
        ``samples``. Failure here means the action may have been a stub
        no-op; the caller (``_click``) should fall back to P4 (coordinate
        click) only if no action was sent. A missing signal after dispatch
        cannot justify a replay.

        ``before``: a pre-action Snapshot taken by the caller BEFORE firing
        the action. Historically this function snapshot-ped after the action
        had already fired (the AXPress happened in ``_ax_native_click`` and
        only THEN did we capture "before") — so for fast effects (Calculator
        keypad → display flips within the press RTT) before==after and the
        verify could NEVER match. Callers that can should pass ``before``;
        when omitted we snapshot here (correct only for actions not yet
        fired, e.g. the fill path which snapshots before AXValue setattr).
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

            # Probe the application element for window-scope signals. Prefer
            # the pid OWNED BY THE TARGET ELEMENT itself: AXUIElementGetPid
            # queries the live AX runtime and can't be stale. The name-based
            # NSWorkspace lookup (_app_pid_for) shares the daemon's possibly-
            # cached process list — after an app quit+relaunch it returns the
            # DEAD pid, AXWindows on it is always empty, and window-scope
            # verify signals silently never fire (the Calculator verify bug).
            app_elem = None
            pid = self._elem_pid(elem)
            if not pid:
                pid = self._target_scope.get("pid")
            if pid:
                try:
                    from ApplicationServices import AXUIElementCreateApplication
                    app_elem = AXUIElementCreateApplication(pid)
                except Exception:
                    app_elem = None

            if before is None:
                before = snapshot(elem, _ax_get, app=app_elem)
            verdict = verify(
                before,
                signal=sig,
                expected_text=expected_text,
                ax_getter=_ax_get,
                target=elem,
                app=app_elem,
                window_ms=window_ms,
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

    def _ax_native_set_value(self, elem: Any, text: str) -> tuple[bool, bool]:
        """Return (transport succeeded, attempted); a failed send cannot replay."""
        try:
            from ApplicationServices import AXUIElementSetAttributeValue, AXUIElementIsAttributeSettable
            err, writable = _ax_get(AXUIElementIsAttributeSettable, elem, "AXValue")
            if err != 0 or not writable:
                return False, False
        except Exception:
            return False, False
        try:
            return AXUIElementSetAttributeValue(elem, "AXValue", str(text)) == 0, True
        except Exception:
            return False, True

    def _live_point(self, elem: Any) -> tuple[Optional[float], Optional[float]]:
        try:
            from ApplicationServices import AXUIElementCopyAttributeValue
            err, position = _ax_get(AXUIElementCopyAttributeValue, elem, "AXPosition")
            err2, size = _ax_get(AXUIElementCopyAttributeValue, elem, "AXSize")
            position, size = _ax_geometry(position), _ax_geometry(size, size=True)
            if err == 0 and err2 == 0 and position is not None and size is not None and float(size.width) > 0 and float(size.height) > 0:
                return float(position.x) + float(size.width)/2, float(position.y) + float(size.height)/2
        except Exception:
            pass
        return None, None

    def _unknown_click_result(self, atype: str, target: str, verification: dict[str, Any]) -> LayerResult:
        """Stop after an attempted action whose effect could not be confirmed."""
        return LayerResult(
            ok=False, layer=self.name,
            message=(f"{atype} {target}: outcome unknown (verify={verification.get('verified', 'n/a')}). "
                     "Read the current UI before deciding on another action."),
            data={"reason": "outcome_unknown", "retry_safe": False,
                  "dispatched": True, "verification": verification},
            dispatch_state="sent", outcome="unknown",
        )

    def _click(self, atype: str, params: dict[str, Any]) -> LayerResult:
        ref = params.get("ref")
        cx: Optional[float] = None
        cy: Optional[float] = None
        if isinstance(ref, str):
            elem = self._resolve_ax_element(ref)
            if elem is None:
                return self._invalid_ref_result(ref)
            cached = self._last_refs[ref]
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
                # Pre-action snapshot BEFORE the press: fast effects (Calculator
                # keypad → display) land within the AXPress round-trip, so a
                # snapshot taken after the press already shows the new state
                # and before==after — verify could never match (the 1.5 bug).
                pre_snap = self._pre_action_snapshot(elem)
                ok_ax, act_name = self._ax_native_click(elem, role, atype)
                if ok_ax:
                    # Critical: AXPress returning err=0 doesn't guarantee
                    # the action took effect (Electron stub no-op returns 0).
                    # A missing change is not proof that the action did nothing.
                    verify_meta = (self._verify_action(elem, role, atype, before=pre_snap)
                                   if pre_snap is not None else
                                   {"verified": "n/a", "reason": "pre-action snapshot unavailable"})
                    verified = verify_meta.get("verified", "n/a")
                    if verified == "yes":
                        return LayerResult(
                            ok=True,
                            layer=self.name,
                            message=(
                                f"click ref={ref} (via AXPress={act_name}, "
                                f"UI change at {verify_meta.get('matched_at_ms')}ms; outcome unconfirmed)"
                            ),
                            data={"verification": verify_meta},
                        )
                    return self._unknown_click_result(
                        atype, f"ref={ref} via {act_name}", verify_meta,
                    )
                elif act_name is not None:
                    # A send error may follow an applied action. An alternate
                    # transport cannot establish that the first send did nothing.
                    result = self._unknown_click_result(
                        atype, f"ref={ref} via {act_name}",
                        {"verified": "n/a", "reason": "AX action returned an error"},
                    )
                    result.dispatch_state = "unknown"
                    result.data["dispatched"] = None
                    return result
                else:
                    axpress_failed_reason = "AX action unavailable; no action sent"
            else:
                return self._invalid_ref_result(ref)

        if "x" in params and "y" in params:
            cx, cy = float(params["x"]), float(params["y"])
        elif isinstance(ref, str):
            # Layout may move after observe. Do not click an old cached point.
            elem = self._resolve_ax_element(ref)
            if elem is None:
                return self._invalid_ref_result(ref)
            cx, cy = self._live_point(elem)
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
                        "No action was sent; re-observe this target or choose an explicitly supported action."
                    ),
                )
            return LayerResult(ok=False, layer=self.name, message="click needs ref or x,y")

        # Click-safety gate (Bug: 多显示器/遮挡下坐标点击会落到飞书/Clash 等更高
        # 栈序窗口上)。在 CGEvent 下发前确认 (cx,cy) 仍属于目标 app 的窗口；
        # 无法读取目标归属时中止，不向未经确认的窗口发送事件。
        safety = self._assert_click_target_for(cx, cy, ref)
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

        coord_elem = self._resolve_ax_element(ref) if isinstance(ref, str) else None
        coord_before = self._pre_action_snapshot(coord_elem) if coord_elem is not None else None
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
                    verify_meta = (self._verify_action(elem_after, role, atype, before=coord_before)
                                   if coord_before is not None else
                                   {"verified": "n/a", "reason": "pre-action snapshot unavailable"})
                    verified = verify_meta.get("verified", "n/a")
                    if verified == "yes":
                        return LayerResult(
                            ok=True,
                            layer=self.name,
                            message=(
                                f"{atype} at ({cx},{cy}) (via CGEvent, UI change "
                                f"at {verify_meta.get('matched_at_ms')}ms)"
                            ),
                            data={"verification": verify_meta},
                        )
                    return self._unknown_click_result(
                        atype, f"at ({cx},{cy}) via CGEvent", verify_meta,
                    )
            return LayerResult(ok=True, layer=self.name, message=f"{atype} at ({cx},{cy})")
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}",
                               data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown")

    def _scroll(self, params: dict[str, Any]) -> LayerResult:
        """Post wheel events only at a currently verified point in this target."""
        dy, dx = int(params.get("dy", 0)), int(params.get("dx", 0))
        if dy == 0 and dx == 0:
            return LayerResult(ok=True, layer=self.name, message="scroll 0 (noop)", dispatch_state="not_sent")
        try:
            from Quartz.CoreGraphics import (CGEventCreateScrollWheelEvent2, CGEventPost,
                CGEventCreate, CGEventGetLocation, CGEventSetLocation,
                kCGScrollEventUnitLine, kCGHIDEventTap)
            from Quartz import CGPointMake
            if params.get("x") is not None and params.get("y") is not None:
                x, y = float(params["x"]), float(params["y"])
            else:
                point = CGEventGetLocation(CGEventCreate(None))
                x, y = float(point.x), float(point.y)
        except Exception as exc:
            return LayerResult(ok=False, layer=self.name, message=f"Scroll position cannot be checked: {exc}",
                               data={"reason": "coordinate_check_unavailable"}, dispatch_state="not_sent")
        safety = self._assert_click_target_for(x, y)
        if not safety.get("ok"):
            return LayerResult(ok=False, layer=self.name, message="Scroll point is outside the confirmed target.",
                               data={"reason": "coordinate_target_unconfirmed", "click_safety": safety}, dispatch_state="not_sent")
        try:
            event = CGEventCreateScrollWheelEvent2(None, kCGScrollEventUnitLine, 2, dy, dx, 0)
            CGEventSetLocation(event, CGPointMake(x, y))
        except Exception as exc:
            return LayerResult(ok=False, layer=self.name, message=f"Scroll event could not be prepared: {exc}", dispatch_state="not_sent")
        try:
            CGEventPost(kCGHIDEventTap, event)
            return LayerResult(ok=True, layer=self.name, message=f"scrolled dy={dy} dx={dx} at ({x},{y})", dispatch_state="sent")
        except Exception as exc:
            return LayerResult(ok=False, layer=self.name, message=f"Scroll outcome unknown: {exc}",
                               data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown")

    def _assert_click_target_for(self, cx: float, cy: float, ref: Optional[str] = None) -> dict[str, Any]:
        """Coordinate input stays in the observed process and exact window."""
        from qcu.layers._click_safety import assert_click_target
        scope = self._target_scope
        if not scope.get("pid") or self._binding_error:
            return {"ok": False, "reason": self._binding_error or "target_unbound"}
        bounds = None
        window_id = scope.get("window_id")
        window = self._target_window
        if ref:
            elem = self._resolve_ax_element(ref)
            if elem is None:
                return {"ok": False, "reason": self._ref_error}
            window = self._element_window(elem)
            if window is None:
                return {"ok": False, "reason": "ref_window_identity_unavailable"}
        if window is not None:
            live = self._window_identity(window, scope["pid"], scope.get("window_title", ""))
            if not live.get("window_id") or (window_id is not None and live["window_id"] != window_id):
                return {"ok": False, "reason": "target_window_identity_unavailable"}
            window_id, bounds = live["window_id"], live.get("bounds")
        elif scope.get("window_handle") and not window_id:
            return {"ok": False, "reason": "target_window_identity_unavailable"}
        return assert_click_target(self._target_app, cx, cy, app_window_bounds=bounds,
                                   expected_pid=scope.get("pid"), expected_window_id=window_id)

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
        if isinstance(ref, str) and self._resolve_ax_element(ref) is None:
            return None, None, ref
        if x is None or y is None:
            # also accept {from:{x,y}} nested form
            nested_key = "from" if key_x == "x1" else "to"
            nested = params.get(nested_key)
            if isinstance(nested, dict):
                x = x if x is not None else nested.get("x")
                y = y if y is not None else nested.get("y")
        if (x is None or y is None) and isinstance(ref, str):
            elem = self._resolve_ax_element(ref)
            if elem is None:
                return None, None, ref
            x, y = self._live_point(elem)
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
        safety = self._assert_click_target_for(x1, y1, ref_from)
        if safety["ok"]:
            safety = self._assert_click_target_for(x2, y2, ref_to)
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
                        pos = _ax_geometry(pos)
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
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}",
                               data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown")

    def _launch_app(self, app: str) -> LayerResult:
        """Launch (or reuse) an app **without bringing it to the foreground**.

        The previous implementation called ``open -a``, which activates the
        app — every QCU call ripped the user's focus away (the "霸道"行为).
        We now go through the shared :mod:`qcu.layers._app_launch` helper,
        which selects a background transport before sending and never replays
        an uncertain native launch through another transport.

        Returns a LayerResult; the message includes ``(foreground)`` when
        QCU had no choice but to disturb focus.
        """
        from qcu.layers._app_launch import launch_app_background

        launch_result = launch_app_background(app)
        ok, msg, focus_disturbed = launch_result
        state = getattr(launch_result, "dispatch_state", "sent" if ok else "not_sent")
        if not ok:
            return LayerResult(ok=False, layer=self.name, message=msg, dispatch_state=state,
                               data={"reason": "outcome_unknown", "retry_safe": False} if state == "unknown" else {})
        # Verify the app actually came up with a window. Cold launches take a
        # moment to register on NSWorkspace + create an AXWindow; we poll for
        # up to ~2.5s (SwiftUI cold-launch territory) instead of a blind
        # sleep(0.8) that returned ok=true even when no window appeared.
        verify_meta = self._wait_for_app_window(app, timeout=2.5)
        self._target_app = verify_meta.get("app") or (app or "").strip()
        self._target_scope = {}
        self._target_window = None
        self._last_refs = {}
        self._refs.invalidate()
        try:
            from qcu.session import patch

            patch(target_app=self._target_app)
        except Exception:
            pass
        marker = " (foreground — focus disturbed)" if focus_disturbed else " (background)"
        return LayerResult(
            ok=True, layer=self.name, message=msg + marker,
            data={"verification": verify_meta, "focus_disturbed": focus_disturbed},
        )

    def _wait_for_app_window(
        self, app: str, *, timeout: float = 2.5, poll: float = 0.15, target_pid: Optional[int] = None
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
        deadline = time.perf_counter() + timeout
        last_count = 0
        last_title: Optional[str] = None
        pid = None
        while time.perf_counter() < deadline:
            pid = target_pid or self._app_pid_for(app)
            if pid is None:
                time.sleep(poll)
                continue
            app_elem = AXUIElementCreateApplication(pid)
            try:
                _, wins = _ax_get(AXUIElementCopyAttributeValue, app_elem, "AXWindows")
                wins = list(wins or [])
            except Exception:
                wins = []
            last_count = len(wins)
            if wins:
                app_name = app
                try:
                    from AppKit import NSRunningApplication
                    running = NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
                    if running is not None:
                        app_name = running.localizedName() or app
                except Exception:
                    pass
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
                    "pid": pid,
                    "app": app_name,
                }
            time.sleep(poll)
        return {
            "verified": "no",
            "signal": "window_change",
            "reason": (f"no running process for {app!r}" if pid is None else
                       f"no AXWindow for {app!r} after {timeout:.1f}s"),
            "n_windows": last_count,
        }

    def _activate_app(self, app: str) -> LayerResult:
        """Activate one running process; uncertain activation is never replayed."""
        app = (app or "").strip()
        if not app:
            return LayerResult(ok=False, layer=self.name, message="activate_app needs params.app")
        try:
            from AppKit import NSWorkspace, NSApplicationActivateIgnoringOtherApps
            ws = NSWorkspace.sharedWorkspace()
            candidates = [running for running in ws.runningApplications() if self._matches_app(app, running)]
            if self._target_scope.get("pid") and self._target_app == app:
                candidates = [running for running in candidates if int(running.processIdentifier()) == self._target_scope["pid"]]
            if len(candidates) != 1:
                return LayerResult(ok=False, layer=self.name, message="Application is missing or ambiguous; observe a unique process first.",
                                   data={"reason": "ambiguous_app" if candidates else "app_not_found",
                                         "candidates": [{"pid": int(a.processIdentifier()), "app": a.localizedName()} for a in candidates]}, dispatch_state="not_sent")
            target = candidates[0]
        except Exception as exc:
            return LayerResult(ok=False, layer=self.name, message=f"Cannot resolve activation target: {exc}", dispatch_state="not_sent")
        try:
            accepted = target.activateWithOptions_(NSApplicationActivateIgnoringOtherApps)
        except Exception as exc:
            return LayerResult(ok=False, layer=self.name, message=f"Activation outcome unknown: {exc}",
                               data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown")
        self._target_app = target.localizedName() or app
        self._target_scope = {"pid": int(target.processIdentifier()), "app": self._target_app}
        self._target_window = None
        self._last_refs = {}
        self._refs.invalidate()
        try:
            from qcu.session import patch
            patch(target_app=self._target_app)
        except Exception:
            pass
        verify_meta = self._wait_for_app_window(self._target_app, timeout=2.0, target_pid=self._target_scope["pid"])
        try:
            foreground = ws.frontmostApplication()
            front_matches = foreground is not None and int(foreground.processIdentifier()) == self._target_scope["pid"]
        except Exception:
            front_matches = False
        verified = verify_meta.get("verified") == "yes" and front_matches
        return LayerResult(ok=verified, layer=self.name,
                           message=f"activate {self._target_app}: " + ("foreground process confirmed" if verified else "outcome unconfirmed"),
                           data={"verification": {"verified": verified, "frontmost_matches": front_matches,
                                                   "window_evidence": verify_meta}, "retry_safe": False},
                           dispatch_state="sent" if accepted else "unknown", outcome="verified" if verified else "unknown")

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
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}",
                               data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown")

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
                    ), data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown",
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
                ), data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown",
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
        text = str(params.get("text", ""))
        if not isinstance(ref, str):
            return LayerResult(ok=False, layer=self.name, message="fill needs params.ref", dispatch_state="not_sent")
        elem = self._resolve_ax_element(ref)
        if elem is None:
            return self._invalid_ref_result(ref)
        succeeded, attempted = self._ax_native_set_value(elem, text)
        if not attempted:
            return LayerResult(ok=False, layer=self.name, message="AXValue is not writable; no action sent.",
                               data={"reason": "unsupported", "dispatched": False}, dispatch_state="not_sent")
        if not succeeded:
            return LayerResult(ok=False, layer=self.name, message="AXValue call outcome unknown; observe before acting again.",
                               data={"reason": "outcome_unknown", "retry_safe": False}, dispatch_state="unknown")
        verification = self._verify_condition({"kind": "value", "ref": ref, "equals": text, "timeout_ms": 400})
        return LayerResult(ok=bool(verification.get("verified")), layer=self.name,
                           message=f"fill ref={ref}: " + ("requested value confirmed" if verification.get("verified") else "sent; value unconfirmed"),
                           data={"verification": verification, "dispatched": True, "retry_safe": False},
                           dispatch_state="sent", outcome="verified" if verification.get("verified") else "unknown")

    def _screenshot(self, path: Optional[str]) -> LayerResult:
        scope = self._target_scope
        if not scope.get("window_id") or self._binding_error:
            return LayerResult(ok=False, layer=self.name,
                               message="Screenshot requires a uniquely observed window and its live capture identity.",
                               data={"reason": "target_window_unbound", "target": dict(scope)}, dispatch_state="not_sent")
        try:
            import Quartz
            from CoreFoundation import CFURLCreateWithFileSystemPath, kCFURLPOSIXPathStyle
            windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
            matched = [w for w in windows or [] if int(w.get("kCGWindowNumber") or 0) == scope["window_id"]
                       and int(w.get("kCGWindowOwnerPID") or 0) == scope["pid"]]
            if len(matched) != 1:
                return LayerResult(ok=False, layer=self.name, message="Bound window is missing; observe again.",
                                   data={"reason": "target_window_stale"}, dispatch_state="not_sent")
            bounds = dict(matched[0]["kCGWindowBounds"])
            image = Quartz.CGWindowListCreateImage(Quartz.CGRectNull,
                        Quartz.kCGWindowListOptionIncludingWindow, scope["window_id"],
                        Quartz.kCGWindowImageBoundsIgnoreFraming)
            if image is None:
                return LayerResult(ok=False, layer=self.name,
                                   message="Target window capture unavailable; check Screen Recording permission.",
                                   data={"reason": "capture_unavailable"}, dispatch_state="not_sent")
            scale = float(Quartz.CGImageGetWidth(image)) / float(bounds["Width"])
            if path:
                url = CFURLCreateWithFileSystemPath(None, path, kCFURLPOSIXPathStyle, False)
                destination = Quartz.CGImageDestinationCreateWithURL(url, "public.png", 1, None)
                Quartz.CGImageDestinationAddImage(destination, image, None)
                if not Quartz.CGImageDestinationFinalize(destination):
                    return LayerResult(ok=False, layer=self.name, message="Screenshot file could not be finalized.", dispatch_state="not_sent")
            return LayerResult(ok=True, layer=self.name, message=f"Target screenshot saved to {path}" if path else "Target screenshot captured",
                               data={"target": dict(scope), "coordinate_origin": {"x": bounds["X"], "y": bounds["Y"]},
                                     "coordinate_scale": scale, "path": path}, dispatch_state="not_sent")
        except Exception as exc:
            return LayerResult(ok=False, layer=self.name, message=f"Target screenshot unavailable: {exc}",
                               data={"reason": "capture_unavailable"}, dispatch_state="not_sent")

    def close(self) -> None:
        self._last_refs = {}
        self._refs.invalidate()
        self._target_window = None
        self._binding_error = "backend_closed"
        self._observed_control_count = None
