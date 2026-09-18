"""Routing rules.

A rule is a triple ``(priority, condition, layer, reason_template)``.

- ``priority`` is the ordering key: higher wins.
- ``condition(features)`` returns True if the rule fires.
- ``layer`` is the layer name that should handle the request.
- ``reason_template`` is a short human-readable string; we don't currently
  format it (kept for future i18n / templating).

To add a rule: append it here, no other file needs to change.
"""

from __future__ import annotations

from typing import Any, Callable
from qcu.platforms import desktop_backend_name, platform_name


Condition = Callable[[dict[str, Any]], bool]


def _f(features: dict[str, Any], key: str, default: Any = False) -> Any:
    return features.get(key, default)


# Actions that only make sense inside the browser (no screenshot-layer
# equivalent). When the context is web, these must be handled by web_a11y
# even if no ref is present (e.g. navigate, scroll, press_key).
_WEB_NATIVE_ACTIONS = frozenset(
    {"navigate", "go_back", "go_forward", "scroll", "press_key", "type", "screenshot", "wait"}
)


def _ax_actually_trusted() -> bool:
    """Probe whether macOS AX permission is really granted right now.

    The router's feature dict defaults ax_trusted to True, but that's a guess
    unless something populated it from a real AX call. We probe live so the
    desktop_no_ax rule only fires when AX is genuinely unavailable — never
    degrading a desktop task to screenshot when AX works (QCU's principle: AX
    is the desktop path, screenshots are a last resort for canvas only).
    """
    try:
        from ApplicationServices import AXIsProcessTrusted
        return bool(AXIsProcessTrusted())
    except Exception:
        return False


def _appleevents_available() -> bool:
    """Probe whether the Apple Events (osascript) desktop path works.

    Returns True when osascript is on PATH and System Events responds to a
    trivial query. This needs the *Automation* TCC grant (different from the
    *Accessibility* grant desktop_ax needs). When the calling process (ZCode)
    has Automation rights, child processes inherit them, so this works
    out-of-the-box after the user authorizes ZCode.

    Caches the probe result in module state; a single CLI run benefits from
    this because routing may probe it for both observe and act.
    """
    global _AE_CACHE
    if _AE_CACHE is not None:
        return _AE_CACHE
    import shutil
    import subprocess
    if shutil.which("osascript") is None:
        _AE_CACHE = False
        return False
    try:
        res = subprocess.run(
            ["osascript", "-e", 'tell application "System Events" to count processes'],
            capture_output=True,
            text=True,
            timeout=4,
        )
        ok = res.returncode == 0 and res.stdout.strip().isdigit()
    except Exception:
        ok = False
    _AE_CACHE = bool(ok)
    return _AE_CACHE


# Cache for _appleevents_available() so we don't fork osascript on every rule
# evaluation. Single CLI invocation = one probe.
_AE_CACHE: bool | None = None


# (priority, name, condition, layer, reason)
Rule = tuple[int, str, Condition, str | Callable[[dict[str, Any]], str], str]


RULES: list[Rule] = [
    # -----------------------------------------------------------------------
    # Hard overrides — these win even when an a11y layer is available.
    # -----------------------------------------------------------------------
    (
        100,
        "canvas_target",
        lambda f: bool(_f(f, "has_canvas_target")),
        "screenshot_fallback",
        "Target sits on a <canvas>/WebGL surface; a11y tree is blind to it.",
    ),
    (
        95,
        "visual_confirm",
        lambda f: bool(_f(f, "needs_visual_confirm")),
        "screenshot_fallback",
        "Action is in the critical list; force a visual confirmation pass.",
    ),
    # -----------------------------------------------------------------------
    # Forward-compat: WebMCP tool calls when the page exposed tools.
    # -----------------------------------------------------------------------
    (
        80,
        "webmcp_tool",
        lambda f: bool(_f(f, "has_webmcp")) and bool(_f(f, "action_in_webmcp_tools")),
        "webmcp",
        "Site registered a tool via navigator.modelContext; prefer it.",
    ),
    # -----------------------------------------------------------------------
    # Normal web path.
    # -----------------------------------------------------------------------
    # Fires for web context when the target is known to the a11y tree, or when
    # the action is "web-native" (navigate/scroll/press_key/type/screenshot) —
    # these only make sense inside the browser and have no screenshot-layer
    # equivalent, so the a11y layer must handle them regardless of ref state.
    (
        70,
        "web_a11y",
        lambda f: _f(f, "context") == "web"
        and (
            _f(f, "target_in_a11y", True)
            or not _f(f, "action_type")
            or _f(f, "action_type") in _WEB_NATIVE_ACTIONS
        ),
        "web_a11y",
        "Web context; a11y tree available — fastest path.",
    ),
    # -----------------------------------------------------------------------
    (
        69, "desktop_registered",
        lambda f: _f(f, "context") == "desktop" and desktop_backend_name(_f(f, "platform", platform_name()))
        not in {"desktop_ax", "desktop_uia", "desktop_linux", "desktop_harmony", "desktop_unsupported"},
        lambda f: desktop_backend_name(_f(f, "platform", platform_name())),
        "Use the explicitly registered platform backend; it reports its own capabilities.",
    ),
    # Desktop selection follows the platform and observed backend. Missing
    # access is reported by that backend; never reinterpret native refs on AE.
    (
        68, "desktop_uia",
        lambda f: _f(f, "context") == "desktop" and _f(f, "desktop_backend", desktop_backend_name()) == "desktop_uia",
        "desktop_uia", "Windows desktop target; use UIA semantic Patterns.",
    ),
    (
        68, "desktop_linux_unimplemented",
        lambda f: _f(f, "context") == "desktop" and _f(f, "desktop_backend", desktop_backend_name()) == "desktop_linux",
        "desktop_linux", "Linux desktop backend is not implemented.",
    ),
    (
        68, "desktop_harmony_unimplemented",
        lambda f: _f(f, "context") == "desktop" and _f(f, "desktop_backend", desktop_backend_name()) == "desktop_harmony",
        "desktop_harmony", "HarmonyOS desktop backend is not implemented.",
    ),
    (
        67, "desktop_explicit_appleevents",
        lambda f: _f(f, "context") == "desktop" and _f(f, "observed_layer") == "desktop_appleevents"
        and _f(f, "platform", platform_name()) == "darwin",
        "desktop_appleevents", "Keep the explicitly observed Apple Events target.",
    ),
    (
        # An Electron/CEF app bridged over CDP: follow-up actions must stay on
        # the bridge (its refs are web_a11y DOM refs), never drop to AX menus.
        67, "desktop_cdp_bridge",
        lambda f: _f(f, "context") == "desktop" and _f(f, "observed_layer") == "desktop_cdp",
        "desktop_cdp", "Keep the explicitly observed CDP-bridged app target.",
    ),
    (
        # A WKWebView app with the embedded JS bridge: follow-up actions must
        # stay on the bridge (its refs are data-qcu-ref DOM refs).
        67, "desktop_jsbridge_keep",
        lambda f: _f(f, "context") == "desktop" and _f(f, "observed_layer") == "desktop_jsbridge",
        "desktop_jsbridge", "Keep the explicitly observed JS-bridged app target.",
    ),
    (
        66, "desktop_webview_blind",
        lambda f: _f(f, "context") == "desktop" and _f(f, "desktop_backend", desktop_backend_name()) == "desktop_ax"
        and ((_f(f, "has_web_area") and int(_f(f, "n_interactive", 0)) < 8) or _f(f, "has_web_app")),
        "desktop_ax", "Bound macOS target contains opaque web content; report its missing read capability.",
    ),
    (
        65, "desktop_ax",
        lambda f: _f(f, "context") == "desktop" and _f(f, "desktop_backend", desktop_backend_name()) == "desktop_ax",
        "desktop_ax", "macOS desktop target; keep AX scope and report missing permissions.",
    ),
    (
        54, "desktop_unsupported",
        lambda f: _f(f, "context") == "desktop",
        "desktop_unsupported", "No implemented desktop backend is registered for this platform.",
    ),
    # -----------------------------------------------------------------------
    # Last-resort fallbacks.
    # -----------------------------------------------------------------------
    (
        40,
        "web_no_target",
        lambda f: _f(f, "context") == "web"
        and _f(f, "action_type")
        and not _f(f, "target_in_a11y")
        and not _f(f, "has_webmcp"),
        "screenshot_fallback",
        "Web context but target ref not in a11y tree.",
    ),
    (
        10,
        "default",
        lambda f: True,
        "screenshot_fallback",
        "No rule matched cleanly; fall back to screenshot + grounding.",
    ),
]


def default_layer() -> str:
    """Layer used when nothing else matches. Always safe."""
    return "screenshot_fallback"