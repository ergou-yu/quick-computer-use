"""Tests for batch 4: observe --app/--pid/--window scoping and the
--no-screenshot-fallback session hard switch.

The observe-scoping tests verify the argument plumbing (argparse → handler →
daemon params) and the desktop_ax walk-root selection logic (pid > app >
target_app > frontmost, plus window-substring tree pruning). The
screenshot-fallback test verifies the router's screenshot choice is refused
when the session flag is set.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from qcu.common.types import Action


def _stub_frameworks():
    quartz = types.ModuleType("Quartz")
    cg = types.ModuleType("Quartz.CoreGraphics")
    for s in ("CGEventCreateMouseEvent", "CGEventPost", "CGEventSetFlags",
              "CGEventCreateKeyboardEvent", "kCGHIDEventTap",
              "kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGEventLeftMouseDragged",
              "kCGEventRightMouseDown", "kCGEventRightMouseUp",
              "kCGEventMouseMoved", "kCGMouseButtonLeft", "kCGMouseButtonRight",
              "kCGMouseEventClickState", "kCGScrollEventUnitLine",
              "CGEventCreateScrollWheelEvent2"):
        setattr(cg, s, lambda *a, **kw: None)
    quartz.CoreGraphics = cg
    setattr(cg, "CGPointMake", lambda x, y: (x, y))
    setattr(quartz, "CGPointMake", lambda x, y: (x, y))
    appsvc = types.ModuleType("ApplicationServices")
    for s in ("AXIsProcessTrusted", "AXIsProcessTrustedWithOptions",
              "AXUIElementCreateApplication", "AXUIElementCreateSystemWide",
              "AXUIElementCopyAttributeValue", "AXUIElementSetAttributeValue",
              "AXUIElementGetActionNames", "AXUIElementPerformAction",
              "kAXTrustedCheckOptionPrompt", "kAXTitleAttribute",
              "kAXFocusedUIElementAttribute", "kAXFocusedApplicationAttribute"):
        setattr(appsvc, s, lambda *a, **kw: None)
    appkit = types.ModuleType("AppKit")
    appkit.NSApplicationActivateIgnoringOtherApps = 1 << 0
    for name, mod in (("Quartz", quartz), ("Quartz.CoreGraphics", cg),
                      ("ApplicationServices", appsvc), ("AppKit", appkit)):
        sys.modules[name] = mod


_stub_frameworks()

from qcu.layers.desktop_ax import DesktopAXLayer  # noqa: E402


@pytest.fixture
def layer():
    L = DesktopAXLayer()
    L._available = True
    L._cached_trusted = True
    return L


# ===========================================================================
# CLI plumbing — argparse declares the flags and forwards them
# ===========================================================================


def test_observe_argparse_has_new_flags():
    """The observe subparser must declare --app/--pid/--window."""
    from qcu import cli as cli_mod
    import argparse

    # Re-build the parser the same way main() does and inspect the actions.
    ap = argparse.ArgumentParser(prog="qcu")
    # cli.main builds its own parser internally; instead grep the source.
    src = open(cli_mod.__file__).read()
    assert '"--app"' in src
    assert '"--pid"' in src
    assert '"--window"' in src
    assert '"--no-screenshot-fallback"' in src


def test_observe_handler_accepts_new_kwargs():
    """observe() must accept app/pid/window without TypeError."""
    import inspect
    from qcu import cli_handlers as h
    sig = inspect.signature(h.observe)
    for k in ("app", "pid", "window"):
        assert k in sig.parameters, f"observe() missing {k!r} param"


def test_session_start_handler_accepts_no_screenshot_fallback():
    import inspect
    from qcu import cli_handlers as h
    sig = inspect.signature(h.session_start)
    assert "no_screenshot_fallback" in sig.parameters


def test_daemon_observe_forwards_new_params():
    """daemon server's observe lambda must pass app/pid/window through."""
    src = open(__import__("qcu.daemon.server", fromlist=["x"]).__file__).read()
    assert "app=params.get" in src
    assert "pid=params.get" in src
    assert "window=params.get" in src


# ===========================================================================
# desktop_ax app selection — pid > app > target_app > frontmost
# ===========================================================================


def _fake_app(name: str, pid: int, active: bool = True):
    return types.SimpleNamespace(
        localizedName=lambda: name,
        processIdentifier=lambda: pid,
        isActive=lambda: active,
    )


def test_observe_pid_overrides_frontmost(layer, monkeypatch):
    """--pid selects that exact process even if it's not frontmost."""
    textedit = _fake_app("TextEdit", 4321, active=False)
    finder = _fake_app("Finder", 999, active=True)

    class _WS:
        @classmethod
        def sharedWorkspace(cls):
            return cls

        @classmethod
        def runningApplications(cls):
            return [finder, textedit]

        @classmethod
        def frontmostApplication(cls):
            return finder

    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSWorkspace=_WS))
    # Drive observe far enough to confirm the selected app's pid is used to
    # build the AX element. We stub AXUIElementCreateApplication to capture it.
    captured: dict[str, Any] = {}
    import qcu.layers.desktop_ax as mod

    def fake_create(pid):
        captured["pid"] = pid
        return object()

    monkeypatch.setattr(sys.modules["ApplicationServices"], "AXUIElementCreateApplication", fake_create)
    # observe() does a lot after this; we only care that pid selection works.
    # Let it run — it will return some Observation; we assert on the captured pid.
    obs = layer.observe(pid=4321)
    assert captured.get("pid") == 4321


def test_observe_app_overrides_frontmost(layer, monkeypatch):
    """--app selects the named app even when not frontmost."""
    textedit = _fake_app("TextEdit", 4321, active=False)
    finder = _fake_app("Finder", 999, active=True)

    class _WS:
        @classmethod
        def sharedWorkspace(cls):
            return cls

        @classmethod
        def runningApplications(cls):
            return [finder, textedit]

        @classmethod
        def frontmostApplication(cls):
            return finder

    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSWorkspace=_WS))
    captured: dict[str, Any] = {}
    monkeypatch.setattr(sys.modules["ApplicationServices"], "AXUIElementCreateApplication",
                        lambda pid: captured.__setitem__("pid", pid) or object())
    layer.observe(app="TextEdit")
    assert captured.get("pid") == 4321


def test_observe_window_filter_falls_back_when_no_match(layer, monkeypatch):
    """--window with no matching title falls back to the app-root walk (no hard fail)."""
    finder = _fake_app("Finder", 999, active=True)

    class _WS:
        @classmethod
        def sharedWorkspace(cls):
            return cls

        @classmethod
        def runningApplications(cls):
            return [finder]

        @classmethod
        def frontmostApplication(cls):
            return finder

    monkeypatch.setitem(sys.modules, "AppKit", types.SimpleNamespace(NSWorkspace=_WS))
    monkeypatch.setattr(sys.modules["ApplicationServices"], "AXUIElementCreateApplication",
                        lambda pid: object())
    # AXWindows returns empty → no window matches → should NOT raise.
    import qcu.layers.desktop_ax as mod
    monkeypatch.setattr(mod, "_ax_get", lambda fn, e, attr: (-1, None))
    obs = layer.observe(window="Nonexistent Window Title")
    # No hard failure; we got an Observation back.
    assert obs is not None


# ===========================================================================
# --no-screenshot-fallback enforcement
# ===========================================================================


def test_no_screenshot_fallback_refuses_router_choice(monkeypatch):
    """When the session flag is set and the router picks screenshot_fallback,
    act() must refuse with ok=false instead of executing the vision path."""
    import qcu.cli_handlers as h
    from qcu.router.classifier import RoutingDecision
    from qcu.common.types import LayerResult

    # Session with the flag set.
    fake_session = types.SimpleNamespace(
        context="web", layer="web_a11y", extras={"no_screenshot_fallback": True},
        last_refs=None,
    )
    monkeypatch.setattr(h, "load", lambda: fake_session, raising=False)
    # Avoid daemon routing.
    monkeypatch.setattr(h, "_ensure_daemon_started", lambda: None, raising=False)
    monkeypatch.setattr(h, "_maybe_route_via_daemon", lambda *a, **kw: None, raising=False)
    # Router picks screenshot_fallback.
    monkeypatch.setattr(h, "classify",
                        lambda feats, last_action=None: RoutingDecision(
                            layer="screenshot_fallback", reason="test",
                            priority=10, rule_name="default", alternatives=[],
                            features=feats),
                        raising=False)
    monkeypatch.setattr(h, "extract_features", lambda obs: {}, raising=False)
    # The feature extractor consults the web_a11y layer's _last_refs; stub a
    # minimal layer object. We never reach .act() because the flag refuses first,
    # but if we did, this would surface it (no act method). act() imports
    # get_layer from qcu.layers.runtime, so patch it there, not on h.
    import qcu.layers.runtime as runtime
    monkeypatch.setattr(runtime, "get_layer",
                        lambda name: types.SimpleNamespace(_last_refs={}),
                        raising=False)
    # Capture stdout.
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = h.act('{"type":"click","params":{"ref":"ref_1"}}', layer=None)
    out = buf.getvalue()
    assert rc == 2  # action-fail exit code
    assert "disabled by session flag" in out
    assert '"ok": false' in out


def test_no_screenshot_fallback_not_enforced_when_disabled(monkeypatch):
    """Without the flag, a screenshot_fallback route executes normally."""
    import qcu.cli_handlers as h
    from qcu.router.classifier import RoutingDecision
    from qcu.common.types import LayerResult

    fake_session = types.SimpleNamespace(
        context="web", layer="web_a11y", extras={}, last_refs=None,
    )
    monkeypatch.setattr(h, "load", lambda: fake_session, raising=False)
    monkeypatch.setattr(h, "_ensure_daemon_started", lambda: None, raising=False)
    monkeypatch.setattr(h, "_maybe_route_via_daemon", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(h, "classify",
                        lambda feats, last_action=None: RoutingDecision(
                            layer="screenshot_fallback", reason="test",
                            priority=10, rule_name="default", alternatives=[],
                            features=feats),
                        raising=False)
    monkeypatch.setattr(h, "extract_features", lambda obs: {}, raising=False)

    class _OkLayer:
        _last_refs = {}

        def act(self, action):
            return LayerResult(ok=True, layer="screenshot_fallback", message="ran")

    import qcu.layers.runtime as runtime
    monkeypatch.setattr(runtime, "get_layer", lambda name: _OkLayer(), raising=False)
    monkeypatch.setattr(h, "Observation", lambda *a, **kw: None, raising=False)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = h.act('{"type":"click","params":{"ref":"ref_1"}}', layer=None)
    assert rc == 0
    assert '"ok": true' in buf.getvalue()


def test_explicit_layer_override_bypasses_flag(monkeypatch):
    """An explicit --layer screenshot_fallback is the user deliberately asking
    for it; the session flag must not override a direct request."""
    import qcu.cli_handlers as h
    from qcu.common.types import LayerResult

    fake_session = types.SimpleNamespace(
        context="web", layer="web_a11y",
        extras={"no_screenshot_fallback": True}, last_refs=None,
    )
    monkeypatch.setattr(h, "load", lambda: fake_session, raising=False)
    monkeypatch.setattr(h, "_ensure_daemon_started", lambda: None, raising=False)
    monkeypatch.setattr(h, "_maybe_route_via_daemon", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(h, "extract_features", lambda obs: {}, raising=False)
    from qcu.router.classifier import RoutingDecision
    monkeypatch.setattr(h, "classify",
                        lambda feats, last_action=None: RoutingDecision(
                            layer="screenshot_fallback", reason="test",
                            priority=10, rule_name="default", alternatives=[],
                            features=feats),
                        raising=False)

    class _OkLayer:
        _last_refs = {}

        def act(self, action):
            return LayerResult(ok=True, layer="screenshot_fallback", message="ran")

    import qcu.layers.runtime as runtime
    monkeypatch.setattr(runtime, "get_layer", lambda name: _OkLayer(), raising=False)
    monkeypatch.setattr(h, "Observation", lambda *a, **kw: None, raising=False)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = h.act('{"type":"click","params":{"ref":"ref_1"}}',
                   layer="screenshot_fallback")  # explicit override
    assert rc == 0
    assert '"ok": true' in buf.getvalue()
