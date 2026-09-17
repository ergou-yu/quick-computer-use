"""Tests for the native ``drag`` action added to desktop_ax and web_a11y.

Covers: endpoint resolution (ref/flat/nested forms), missing-endpoint
rejection, interpolation math, and the post-drag verification block. Runs
without real AX/Quartz (the fixture stubs the macOS frameworks) and without
a real browser (web_a11y is unit-tested via the async dispatch).
"""

from __future__ import annotations

import asyncio
import sys
import types
from typing import Any

import pytest

from qcu.common.types import Action


def _stub_frameworks():
    """Inject Quartz/ApplicationServices stubs into sys.modules so desktop_ax
    imports cleanly under the env's broken PyObjC."""
    quartz = types.ModuleType("Quartz")
    cg = types.ModuleType("Quartz.CoreGraphics")
    for s in ("CGEventCreateMouseEvent", "CGEventPost", "CGEventSetFlags",
              "CGEventCreateKeyboardEvent", "CGEventCreateScrollWheelEvent2",
              "kCGHIDEventTap", "kCGEventLeftMouseDown", "kCGEventLeftMouseUp",
              "kCGEventLeftMouseDragged", "kCGEventMouseMoved",
              "kCGEventRightMouseDown", "kCGEventRightMouseUp",
              "kCGMouseButtonLeft", "kCGMouseButtonRight",
              "kCGMouseEventClickState", "kCGScrollEventUnitLine"):
        setattr(cg, s, lambda *a, **kw: None)
    quartz.CoreGraphics = cg

    def cgpoint(x, y):
        return (x, y)
    setattr(cg, "CGPointMake", cgpoint)
    # desktop_ax does ``from Quartz import CGPointMake`` (top-level), so expose
    # it on the Quartz package too, not just on the CoreGraphics submodule.
    setattr(quartz, "CGPointMake", cgpoint)

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
    # Re-apply the framework stubs: other test modules' fixtures may have
    # replaced sys.modules["Quartz.CoreGraphics"] with a stub missing the drag
    # symbols between tests. Ensures _drag's Quartz import succeeds regardless
    # of test ordering.
    _stub_frameworks()
    L = DesktopAXLayer()
    L._available = True
    L._cached_trusted = True
    return L


# ===========================================================================
# Endpoint resolution
# ===========================================================================


def test_resolve_point_explicit_coords(layer):
    x, y, ref = layer._resolve_point({"x1": 10, "y1": 20}, "x1", "y1", "ref_from")
    assert (x, y, ref) == (10.0, 20.0, None)


def test_resolve_point_ref_fallback(layer, monkeypatch):
    # Endpoint parsing is isolated from native identity validation.
    monkeypatch.setattr(layer, "_resolve_ax_element", lambda ref: object())
    monkeypatch.setattr(layer, "_live_point", lambda elem: (100.0, 200.0))
    layer._last_refs["ref_3"] = {"cx": 100.0, "cy": 200.0}
    x, y, ref = layer._resolve_point({"ref_from": "ref_3"}, "x1", "y1", "ref_from")
    assert (x, y, ref) == (100.0, 200.0, "ref_3")


def test_resolve_point_nested_from(layer):
    x, y, ref = layer._resolve_point(
        {"from": {"x": 5, "y": 6}}, "x1", "y1", "ref_from"
    )
    assert (x, y) == (5.0, 6.0)


def test_resolve_point_missing_returns_none(layer):
    x, y, ref = layer._resolve_point({}, "x1", "y1", "ref_from")
    assert x is None and y is None


# ===========================================================================
# _drag — dispatch + missing endpoint rejection
# ===========================================================================


def test_drag_missing_endpoints_rejected(layer, monkeypatch):
    # No endpoints at all.
    monkeypatch.setattr(layer, "_post_key_code", lambda *a, **kw: None)
    res = layer._drag({})
    assert res.ok is False
    assert "both endpoints" in res.message


def test_drag_flat_coords_dispatches_events(layer, monkeypatch):
    """Flat x1,y1,x2,y2 form posts the full down→drag...→up sequence."""
    import qcu.layers.desktop_ax as mod
    events: list[str] = []

    def fake_post(tap, ev):
        events.append("post")

    # Stub the Quartz CGEvent constructors/post so _drag never touches Quartz.
    monkeypatch.setattr(mod, "CGEventPost", fake_post, raising=False)

    # Patch the names _drag imports from Quartz.CoreGraphics at call time.
    import Quartz.CoreGraphics as cg
    monkeypatch.setattr(cg, "CGEventCreateMouseEvent", lambda *a, **kw: object(), raising=False)
    monkeypatch.setattr(cg, "CGEventPost", fake_post, raising=False)

    checked = []
    monkeypatch.setattr(layer, "_assert_click_target_for", lambda x,y,ref=None: checked.append((x,y)) or {"ok": True})
    res = layer._drag({"x1": 0, "y1": 0, "x2": 100, "y2": 0, "duration": 0.0, "steps": 5})
    assert checked == [(0.0,0.0),(100.0,0.0)]
    assert res.ok is True
    # 1 initial move + 1 down + 5 drag steps + 1 up = 8 events.
    assert len(events) == 8
    vm = res.data["verification"]
    assert vm["verified"] == "n/a"  # coordinate-only drag, nothing to re-read
    assert vm["moved_px"] == 100.0


def test_drag_ref_source_verification_yes(layer, monkeypatch):
    """When the source ref is AX-resolvable and its position moved, verified=yes."""
    import Quartz.CoreGraphics as cg
    monkeypatch.setattr(cg, "CGEventCreateMouseEvent", lambda *a, **kw: object(), raising=False)
    monkeypatch.setattr(cg, "CGEventPost", lambda *a, **kw: None, raising=False)

    layer._last_refs["ref_src"] = {
        "cx": 0.0, "cy": 0.0, "role": "image", "role_raw": "AXImage",
    }
    fake_elem = object()

    # _resolve_ax_element returns our fake handle.
    monkeypatch.setattr(layer, "_resolve_ax_element", lambda ref: fake_elem)

    monkeypatch.setattr(layer, "_live_point", lambda elem: (0.0, 0.0))

    # _ax_get returns a "position" that moved from (0,0) to (50,0) → moved 50px.
    class _P:
        x = 50.0
        y = 0.0
    import qcu.layers.desktop_ax as mod

    def fake_ax_get(fn, elem, attr):
        if attr == "AXPosition":
            return (0, _P())
        return (-1, None)
    monkeypatch.setattr(mod, "_ax_get", fake_ax_get)

    monkeypatch.setattr(layer, "_assert_click_target_for", lambda *a: {"ok": True})
    res = layer._drag({"ref_from": "ref_src", "x2": 50, "y2": 0,
                       "duration": 0.0, "steps": 2})
    assert res.ok is True
    vm = res.data["verification"]
    assert vm["verified"] == "yes"
    assert vm["source_moved_px"] == 50.0


# ===========================================================================
# Action dispatch — drag routes through act()
# ===========================================================================


def test_act_drag_dispatches_to_drag(layer, monkeypatch):
    called = {"n": 0}

    def fake_drag(params):
        called["n"] += 1
        from qcu.common.types import LayerResult
        return LayerResult(ok=True, layer="desktop_ax", message="dragged")

    monkeypatch.setattr(layer, "_drag", fake_drag)
    res = layer.act(Action(type="drag", params={"x1": 0, "y1": 0, "x2": 1, "y2": 1}))
    assert res.ok is True
    assert called["n"] == 1


# ===========================================================================
# types.py — drag is in ACTION_TYPES
# ===========================================================================


def test_drag_in_action_types():
    from qcu.common.types import ACTION_TYPES
    assert "drag" in ACTION_TYPES


# ===========================================================================
# web_a11y — _drag endpoint resolution + dispatch (async, no browser)
# ===========================================================================


def test_web_a11y_drag_missing_endpoints_rejected():
    asyncio.run(_web_drag_missing())


async def _web_drag_missing():
    from qcu.layers.web_a11y import WebA11yLayer
    L = WebA11yLayer()
    # No browser page needed — the missing-endpoint check happens before any
    # page.mouse.* call.
    res = await L._drag(page=None, params={})
    assert res.ok is False
    assert "both endpoints" in res.message


def test_web_a11y_drag_runs_against_fake_page():
    asyncio.run(_web_drag_runs())


async def _web_drag_runs():
    from qcu.layers.web_a11y import WebA11yLayer
    L = WebA11yLayer()

    moves: list[tuple[float, float]] = []

    class _Mouse:
        async def move(self, x, y, steps=1):
            moves.append((x, y))

        async def down(self):
            moves.append(("down",))

        async def up(self):
            moves.append(("up",))

    class _Page:
        mouse = _Mouse()

        async def wait_for_timeout(self, ms):
            pass

    res = await L._drag(page=_Page(), params={"x1": 0, "y1": 0, "x2": 10, "y2": 0,
                                               "duration": 0.0, "steps": 5})
    assert res.ok is True
    # First the move to start, then down, then 5 interpolated moves, then up.
    assert moves[0] == (0.0, 0.0)
    assert moves[1] == ("down",)
    assert moves[-1] == ("up",)
    # Last interpolated step lands at the destination.
    assert moves[-2] == (10.0, 0.0)
    assert res.data["verification"]["verified"] == "n/a"
