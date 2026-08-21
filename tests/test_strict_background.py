"""Tests for ``--strict-background`` mode: foreground-drift detection and
refusal of focus-stealing actions (launch_app/activate_app).

These confirm the behavior promised in the honest-limitations note: strict
mode DETECTS drift and PREVENTS focus theft, but does not enable true
background AX control (that needs a larger refactor and is out of scope).
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
# _strict_background_mode reads session.extras
# ===========================================================================


def test_strict_mode_disabled_by_default(layer, monkeypatch):
    # No session → disabled.
    monkeypatch.setattr("qcu.session.load", lambda: None)
    cfg = layer._strict_background_mode()
    assert cfg["enabled"] is False


def test_strict_mode_enabled_when_extras_set(layer, monkeypatch):
    fake = types.SimpleNamespace(extras={"strict_background": True,
                                         "expected_foreground": "Finder"})
    monkeypatch.setattr("qcu.session.load", lambda: fake)
    cfg = layer._strict_background_mode()
    assert cfg["enabled"] is True
    assert cfg["expected"] == "Finder"


# ===========================================================================
# launch_app / activate_app refused in strict mode
# ===========================================================================


def test_launch_app_refused_in_strict_mode(layer, monkeypatch):
    fake = types.SimpleNamespace(extras={"strict_background": True,
                                         "expected_foreground": "Finder"})
    monkeypatch.setattr("qcu.session.load", lambda: fake)
    res = layer.act(Action(type="launch_app", params={"app": "TextEdit"}))
    assert res.ok is False
    assert "strict-background" in res.message
    assert "steal" in res.message


def test_activate_app_refused_in_strict_mode(layer, monkeypatch):
    fake = types.SimpleNamespace(extras={"strict_background": True,
                                         "expected_foreground": "Finder"})
    monkeypatch.setattr("qcu.session.load", lambda: fake)
    # frontmost is the expected one (no drift), so only the focus-steal check fires.
    monkeypatch.setattr(layer, "_frontmost_name", lambda: "Finder")
    res = layer.act(Action(type="activate_app", params={"app": "Notes"}))
    assert res.ok is False
    assert "strict-background" in res.message


def test_launch_app_allowed_when_not_strict(layer, monkeypatch):
    monkeypatch.setattr("qcu.session.load", lambda: None)
    # Stub the actual launch so no process is spawned.
    monkeypatch.setattr(
        "qcu.layers._app_launch.launch_app_background",
        lambda app: (True, f"launched {app}", False),
    )
    monkeypatch.setattr(layer, "_wait_for_app_window", lambda app, **kw: {"verified": "yes"})
    monkeypatch.setattr("qcu.session.patch", lambda **kw: None, raising=False)
    res = layer.act(Action(type="launch_app", params={"app": "TextEdit"}))
    assert res.ok is True


# ===========================================================================
# drift detection — non-stealing actions abort when frontmost drifted
# ===========================================================================


def test_click_aborts_on_drift_in_strict_mode(layer, monkeypatch):
    fake = types.SimpleNamespace(extras={"strict_background": True,
                                         "expected_foreground": "Finder"})
    monkeypatch.setattr("qcu.session.load", lambda: fake)
    monkeypatch.setattr(layer, "_frontmost_name", lambda: "飞书")  # drifted!
    res = layer.act(Action(type="click", params={"ref": "ref_1"}))
    assert res.ok is False
    assert "drifted" in res.message
    assert "飞书" in res.message


def test_click_allowed_when_no_drift(layer, monkeypatch):
    fake = types.SimpleNamespace(extras={"strict_background": True,
                                         "expected_foreground": "Finder"})
    monkeypatch.setattr("qcu.session.load", lambda: fake)
    monkeypatch.setattr(layer, "_frontmost_name", lambda: "Finder")
    # Unknown ref → click will fail at the ref-lookup stage, NOT at the strict
    # check. That proves the strict gate let it through.
    res = layer.act(Action(type="click", params={"ref": "ref_1"}))
    assert res.ok is False
    assert "unknown ref" in res.message  # reached click logic, not blocked


def test_click_allowed_when_strict_disabled(layer, monkeypatch):
    monkeypatch.setattr("qcu.session.load", lambda: None)
    # Even with a "drift", disabled strict mode lets the action proceed.
    res = layer.act(Action(type="click", params={"ref": "ref_1"}))
    assert "strict-background" not in res.message
    assert res.ok is False  # fails on unknown ref, not strict


# ===========================================================================
# session_start persists the flag + expected frontmost into extras
# ===========================================================================


def test_session_start_persists_strict_flag(monkeypatch):
    """session_start(--strict-background) writes extras.strict_background and
    extras.expected_foreground."""
    import qcu.cli_handlers as h

    # Avoid daemon routing and real session state.
    monkeypatch.setattr(h, "_maybe_route_via_daemon", lambda *a, **kw: None)
    monkeypatch.setattr(h, "load", lambda: None, raising=False)
    monkeypatch.setattr(h, "new", lambda **kw: None, raising=False)
    # expected_foreground probe reads NSWorkspace.frontmostApplication().
    fake_frontmost = types.SimpleNamespace(localizedName=lambda: "Finder")

    class _WS:
        @classmethod
        def sharedWorkspace(cls):
            return cls

        @classmethod
        def frontmostApplication(cls):
            return fake_frontmost

    monkeypatch.setitem(sys.modules, "AppKit",
                        types.SimpleNamespace(NSWorkspace=_WS))
    captured: dict[str, Any] = {}
    monkeypatch.setattr("qcu.session.patch", lambda **kw: captured.update(kw), raising=False)
    # permissions preflight would touch real macOS; skip it.
    monkeypatch.setattr(h.permissions, "probe_all", lambda: None, raising=False)
    monkeypatch.setattr(h.permissions, "trigger_prompts_for_missing", lambda r: None, raising=False)

    rc = h.session_start("desktop", headless=True, strict_background=True)
    assert rc == 0
    assert captured.get("extras", {}).get("strict_background") is True
    assert captured.get("extras", {}).get("expected_foreground") == "Finder"
