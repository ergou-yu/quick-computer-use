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
Rule = tuple[int, str, Condition, str, str]


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
    # Desktop path.
    # -----------------------------------------------------------------------
    # Two a11y backends on macOS. desktop_ax (Accessibility TCC) is the
    # highest-quality path: native AXPress, AXShowMenu, real AXValue sets.
    # desktop_appleevents (Apple Events via osascript) needs only the
    # Automation TCC — the same one ZCode already has to talk to Finder — so
    # it works on day one without toggling System Settings.
    #
    # AX-first, Apple Events second: when both are usable, AX wins because
    # AXPress-style actions don't depend on positional path indices that
    # could shift between observe() and act().
    (
        66,
        "desktop_webview_blind",
        # Two triggers, OR'd:
        #   (a) Desktop app embeds an opaque AXWebArea (WKWebView/Electron)
        #       AND the AX tree has very few actable controls — i.e. the app
        #       is essentially a web-view shell the desktop layers can't see
        #       into. The ``n_interactive < 8`` guard keeps real AppKit apps
        #       (dozens of actable controls) on the normal desktop_ax path.
        #   (b) A full browser (Chrome/Safari/Edge/Arc) detected via Apple
        #       Script tab probing (``has_web_app``). Stock Chrome does NOT
        #       expose AXWebArea without AXManualAccessibility, so trigger
        #       (a) never fires for it — yet its DOM is just as opaque. This
        #       branch surfaces the T3 takeover hint for the
        #       CRM-in-Chrome case the SKILL.md decision tree describes
        #       (``web_app is not None → T3``).
        # Sits ABOVE desktop_ax (65) so either case wins this branch; the
        # layer stays desktop_ax so act() still works for the chrome
        # controls (toolbar/omnibox), while the reason + alternatives tell
        # the LLM to escalate to T3 (QCU's own Chromium) for DOM work.
        lambda f: (
            _f(f, "context") == "desktop"
            and (
                (bool(_f(f, "has_web_area"))
                 and int(_f(f, "n_interactive", 0)) < 8)
                or bool(_f(f, "has_web_app"))
            )
        ),
        "desktop_ax",
        "Desktop target has opaque web content (AXWebArea shell or a full "
        "browser via web_app); AX/Apple Events cannot reach the DOM — "
        "consider T3 web takeover (session start --context web + navigate).",
    ),
    (
        65,
        "desktop_ax",
        lambda f: _f(f, "context") == "desktop" and (_f(f, "ax_trusted", False) or _ax_actually_trusted()),
        "desktop_ax",
        "Desktop context with macOS AX trusted.",
    ),
    (
        60,
        "desktop_appleevents",
        # Apple Events path: works without Accessibility grant, covers ~every
        # native + Electron app. Probe lazily so the rule only claims
        # availability when osascript is on PATH and System Events responds.
        lambda f: _f(f, "context") == "desktop" and not _ax_actually_trusted() and _appleevents_available(),
        "desktop_appleevents",
        "Desktop context; AX not granted, but Apple Events (Automation) path is usable.",
    ),
    (
        55,
        "desktop_no_ax",
        # Only fall back to screenshot when BOTH a11y backends are unavailable.
        # This is genuinely rare on macOS — Apple Events is granted as soon as
        # the user authorizes ZCode to use Finder/TextEdit.
        lambda f: _f(f, "context") == "desktop" and not _ax_actually_trusted() and not _appleevents_available(),
        "screenshot_fallback",
        "Desktop context but neither AX nor Apple Events is available — last resort only.",
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