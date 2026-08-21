"""Tests for the click-safety gate (``qcu.layers._click_safety``).

Background: a multi-monitor field report showed coordinate clicks landing on
飞书 / Clash when a higher-stacking window covered the cached AX point. The
gate walks ``CGWindowListCopyWindowInfo`` (frontmost-first) to find the
window that actually owns the point and refuses the click if that owner
isn't the intended app.

These tests mock the Quartz window-list API (via ``sys.modules`` patches on
the ``Quartz`` module) so they run on any platform without a real display.
They cover:
  - owner matches → ok
  - owner=飞书 ≠ Mirroria → ok=False with real owner named
  - point in no window → ok=False
  - Quartz import fails → fail-open ok=True
  - desktop_ax._click integration: wrong owner aborts before CGEvent fires
  - multi-window: a different app's overlapping window at the same point
    is reported as the wrong owner even when the target app also has a
    window elsewhere on screen.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from qcu.common.types import LayerResult
from qcu.layers import desktop_ax
from qcu.layers._click_safety import (
    assert_click_target,
    format_safety_failure,
    overlay_occluder,
    window_at_point,
    windows_at_point,
)


# ---------------------------------------------------------------------------
# Helpers: build a fake ``Quartz`` module exposing only the symbols the gate
# imports, backed by an in-memory window list. Order of the list IS the
# frontmost-first z-order (matches the real CG API the gate depends on).
# ---------------------------------------------------------------------------


def _install_fake_quartz(monkeypatch, windows, *, with_cgevents=False):
    """Inject a fake ``Quartz`` module with a controllable window list.

    ``with_cgevents=True`` also populates the ``Quartz.CoreGraphics`` CGEvent
    symbols that ``desktop_ax._click`` imports, for tests that need to reach
    the post-gate CGEvent dispatch path.
    """
    fake = types.ModuleType("Quartz")

    def CGWindowListCopyWindowInfo(opts, wid):
        return list(windows)

    fake.CGWindowListCopyWindowInfo = CGWindowListCopyWindowInfo
    fake.kCGWindowListOptionOnScreenOnly = 1
    fake.kCGNullWindowID = 0
    # desktop_ax imports CGEvent symbols from ``Quartz.CoreGraphics``; give the
    # fake a sub-module attribute so both the gate's ``from Quartz import …``
    # and the click layer's ``from Quartz.CoreGraphics import …`` resolve.
    cg = types.ModuleType("Quartz.CoreGraphics")
    fake.CoreGraphics = cg
    fake.CGPointMake = lambda x, y: (x, y)
    if with_cgevents:
        # Minimal constants desktop_ax._click imports; values are irrelevant.
        for sym in (
            "CGEventCreateMouseEvent", "CGEventPost",
            "kCGEventLeftMouseDown", "kCGEventLeftMouseUp",
            "kCGEventMouseMoved", "kCGEventRightMouseDown",
            "kCGEventRightMouseUp", "kCGHIDEventTap",
            "kCGMouseButtonLeft", "kCGMouseButtonRight",
            "kCGMouseEventClickState",
        ):
            setattr(cg, sym, sym)
    monkeypatch.setitem(sys.modules, "Quartz", fake)
    monkeypatch.setitem(sys.modules, "Quartz.CoreGraphics", cg)
    return fake


def _win(owner, x, y, w, h, *, wid=1, layer=0, title="", alpha=1.0):
    return {
        "kCGWindowOwnerName": owner,
        "kCGWindowNumber": wid,
        "kCGWindowLayer": layer,
        "kCGWindowTitle": title,
        "kCGWindowAlpha": alpha,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
    }


# ===========================================================================
# window_at_point
# ===========================================================================


def test_window_at_point_returns_frontmost_owner(monkeypatch):
    _install_fake_quartz(monkeypatch, [
        _win("飞书", 0, 0, 1920, 1080, wid=10, title="Lark"),
        _win("Mirroria", 0, 0, 1920, 1080, wid=20, title="Mirroria"),
    ])
    w = window_at_point(500, 500)
    # 飞书 is higher in the list (frontmost) → it wins the point.
    assert w is not None
    assert w["owner"] == "飞书"
    assert w["wid"] == 10


def test_window_at_point_skips_non_zero_layer(monkeypatch):
    _install_fake_quartz(monkeypatch, [
        _win("SystemUIServer", 0, 0, 1920, 24, wid=99, layer=25),  # menubar
        _win("Mirroria", 0, 0, 1920, 1080, wid=20),
    ])
    w = window_at_point(100, 10)  # inside the menubar rect
    # The layer-25 menubar is skipped; nothing layer-0 contains (100,10)
    # except Mirroria whose rect starts at (0,0) so it actually matches.
    assert w is not None
    assert w["owner"] == "Mirroria"


def test_window_at_point_no_hit_returns_none(monkeypatch):
    _install_fake_quartz(monkeypatch, [_win("Mirroria", 0, 0, 100, 100, wid=1)])
    assert window_at_point(5000, 5000) is None


# ===========================================================================
# assert_click_target
# ===========================================================================


def test_assert_ok_when_owner_matches(monkeypatch):
    _install_fake_quartz(monkeypatch, [_win("Mirroria", 0, 0, 1920, 1080, wid=1)])
    v = assert_click_target("Mirroria", 100, 200)
    assert v["ok"] is True
    assert v["reason"] == "ok"
    assert v["owner"] == "Mirroria"


def test_assert_owner_match_strips_app_suffix_and_case(monkeypatch):
    _install_fake_quartz(monkeypatch, [_win("Mirroria.app", 0, 0, 1920, 1080, wid=1)])
    assert assert_click_target("mirroria", 100, 200)["ok"] is True


def test_assert_wrong_owner_names_real_owner(monkeypatch):
    # The bug: 飞书 covers the cached point that belonged to Mirroria.
    _install_fake_quartz(monkeypatch, [
        _win("飞书", 0, 0, 1920, 1080, wid=10, title="Lark"),
        _win("Mirroria", 0, 0, 1920, 1080, wid=20),
    ])
    v = assert_click_target("Mirroria", 500, 500)
    assert v["ok"] is False
    assert v["reason"] == "wrong_owner"
    assert v["owner"] == "飞书"
    assert v["expected_app"] == "Mirroria"


def test_assert_point_in_no_window_aborts(monkeypatch):
    _install_fake_quartz(monkeypatch, [_win("Mirroria", 0, 0, 100, 100, wid=1)])
    v = assert_click_target("Mirroria", 5000, 5000)
    assert v["ok"] is False
    assert v["reason"] == "point_not_in_any_window"


def test_assert_fail_open_when_quartz_missing(monkeypatch):
    # Force the import to fail: remove Quartz and make any import of it raise.
    monkeypatch.setitem(sys.modules, "Quartz", None)
    v = assert_click_target("Mirroria", 100, 200)
    # fail-open: no Quartz → ok=True so we never brick clicks on non-macOS.
    assert v["ok"] is True
    assert v["reason"] == "safety_probe_unavailable"


def test_assert_point_outside_target_window_when_owner_matches(monkeypatch):
    # Owner matches, but the cached point is outside the live window bounds
    # (window was dragged). The caller supplies app_window_bounds to enable
    # the extra check.
    _install_fake_quartz(monkeypatch, [_win("Mirroria", 0, 0, 100, 100, wid=1)])
    v = assert_click_target(
        "Mirroria", 50, 50,
        app_window_bounds={"X": 2000, "Y": 2000, "Width": 800, "Height": 600},
    )
    # window_at_point finds Mirroria's (0,0,100,100) which DOES contain (50,50),
    # so owner check passes; but the caller's app_window_bounds says the real
    # target window is at (2000,2000) → point is outside → abort.
    assert v["ok"] is False
    assert v["reason"] == "point_outside_target_window"


def test_format_safety_failure_names_owner():
    msg = format_safety_failure({
        "ok": False, "reason": "wrong_owner", "owner": "飞书",
        "owner_title": "Lark", "expected_app": "Mirroria", "x": 5, "y": 6,
    })
    assert "飞书" in msg
    assert "Mirroria" in msg
    assert "aborted" in msg


# ===========================================================================
# desktop_ax._click integration: wrong owner aborts before CGEvent
# ===========================================================================


@pytest.fixture
def layer():
    L = desktop_ax.DesktopAXLayer()
    L._available = True
    L._cached_trusted = True
    return L


def test_click_aborts_before_cgevent_when_wrong_owner(layer, monkeypatch):
    """The concrete bug fix: when 飞书 covers the cached Mirroria point,
    _click must return ok=False and NOT post any CGEvent."""
    _install_fake_quartz(monkeypatch, [
        _win("飞书", 0, 0, 1920, 1080, wid=10),
        _win("Mirroria", 0, 0, 1920, 1080, wid=20),
    ])
    # A ref with cached geometry that USED to be Mirroria's. We exercise the
    # coordinate-only path (no resolvable AX element) so we land directly in
    # the CGEvent branch where the gate now sits.
    fired = {"n": 0}
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventPost",
        lambda tap, evt: fired.__setitem__("n", fired["n"] + 1),
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventCreateMouseEvent",
        lambda *a, **k: MagicMock(),
        raising=False,
    )

    layer._target_app = "Mirroria"
    result = layer._click("click", {"x": 500, "y": 500})

    assert result.ok is False
    assert "飞书" in result.message
    assert fired["n"] == 0  # no mouse event posted at the wrong window
    assert (result.data or {}).get("click_safety", {}).get("reason") == "wrong_owner"


# ===========================================================================
# Multi-window: target app has its own window elsewhere, but the point is
# over a different app's window → still aborted.
# ===========================================================================


def test_multi_window_wrong_owner_at_point(layer, monkeypatch):
    """Mirroria is running with a window on a secondary display, but the
    cached ref point (100,100) sits under 飞书's window on the main display.
    The gate must catch it regardless of how many Mirroria windows exist."""
    _install_fake_quartz(monkeypatch, [
        _win("飞书", 0, 0, 1920, 1080, wid=10, title="Lark"),
        _win("Mirroria", 1920, 0, 1920, 1080, wid=20, title="Mirroria"),
    ])
    layer._target_app = "Mirroria"
    result = layer._click("click", {"x": 100, "y": 100})
    assert result.ok is False
    assert (result.data or {}).get("click_safety", {}).get("owner") == "飞书"


def test_multi_window_correct_window_passes(layer, monkeypatch):
    """When the point actually sits inside the target app's own window
    (even with 飞书 elsewhere), the gate passes and the click proceeds."""
    _install_fake_quartz(monkeypatch, [
        _win("飞书", 0, 0, 1920, 1080, wid=10),
        _win("Mirroria", 1920, 0, 1920, 1080, wid=20),
    ], with_cgevents=True)
    fired = {"n": 0}
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventPost",
        lambda tap, evt: fired.__setitem__("n", fired["n"] + 1),
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventCreateMouseEvent",
        lambda *a, **k: MagicMock(),
        raising=False,
    )
    monkeypatch.setattr("Quartz.CGPointMake", lambda x, y: (x, y), raising=False)
    layer._target_app = "Mirroria"
    result = layer._click("click", {"x": 2500, "y": 500})
    assert result.ok is True
    assert fired["n"] >= 2  # down + up


# ===========================================================================
# Overlay occlusion (Bug: Chrome reported frontmost but Mirroria covered it)
#
# The field report: a floating/overlay window (Mirroria, 悬浮球) at a
# non-zero window level covers Chrome. macOS still reports Chrome as
# frontmost (frontmostApplication reflects activation, not stacking), and
# the old layer-0-only gate skipped the overlay, so the click passed the
# owner check and leaked to the overlay. These tests pin the fix.
# ===========================================================================


def test_overlay_occluder_detects_foreign_overlay():
    """Mirroria (layer 3) above Chrome (layer 0) → occluder returned."""
    chrome = _shape(_win("Google Chrome", 0, 0, 1920, 1080, wid=10))
    mirroria = _shape(_win("Mirroria", 0, 0, 1920, 1080, wid=20, layer=3))
    # mirroria stacks ABOVE chrome (earlier in frontmost-first list)
    occ = overlay_occluder([mirroria, chrome], "Google Chrome")
    assert occ is not None
    assert occ["owner"] == "Mirroria"


def test_overlay_occluder_ignores_same_app_panel():
    """A same-app floating panel (Chrome's own sheet) is NOT an occluder."""
    chrome = _shape(_win("Google Chrome", 0, 0, 1920, 1080, wid=10))
    panel = _shape(_win("Google Chrome", 0, 0, 400, 300, wid=11, layer=5))
    assert overlay_occluder([panel, chrome], "Google Chrome") is None


def test_overlay_occluder_ignores_transparent_overlay():
    """A fully transparent (alpha=0) overlay is click-through → not an occluder."""
    chrome = _shape(_win("Google Chrome", 0, 0, 1920, 1080, wid=10))
    ghost = _shape(_win("Mirroria", 0, 0, 1920, 1080, wid=20, layer=3, alpha=0.0))
    assert overlay_occluder([ghost, chrome], "Google Chrome") is None


def test_overlay_occluder_none_when_clear():
    chrome = _shape(_win("Google Chrome", 0, 0, 1920, 1080, wid=10))
    assert overlay_occluder([chrome], "Google Chrome") is None


def test_assert_aborts_when_overlay_covers_target(monkeypatch):
    """The bug scenario: Chrome is the layer-0 target, Mirroria (layer 3)
    covers the same point. Owner check would pass (Chrome == Chrome), but
    the overlay gate must abort with reason=occluded_by_overlay."""
    _install_fake_quartz(monkeypatch, [
        _win("Mirroria", 0, 0, 1920, 1080, wid=20, layer=3, title="悬浮球"),
        _win("Google Chrome", 0, 0, 1920, 1080, wid=10, title="CRM"),
    ])
    v = assert_click_target("Google Chrome", 500, 500)
    assert v["ok"] is False
    assert v["reason"] == "occluded_by_overlay"
    assert v["owner"] == "Mirroria"


def test_assert_passes_when_overlay_is_same_app(monkeypatch):
    """Same-app overlay (Chrome's own floating panel) does not block."""
    _install_fake_quartz(monkeypatch, [
        _win("Google Chrome", 0, 0, 400, 300, wid=11, layer=5),  # own panel
        _win("Google Chrome", 0, 0, 1920, 1080, wid=10),
    ])
    v = assert_click_target("Google Chrome", 100, 100)
    assert v["ok"] is True


def test_assert_passes_when_overlay_is_transparent(monkeypatch):
    """Transparent overlay (alpha=0) is click-through → click proceeds."""
    _install_fake_quartz(monkeypatch, [
        _win("Mirroria", 0, 0, 1920, 1080, wid=20, layer=3, alpha=0.0),
        _win("Google Chrome", 0, 0, 1920, 1080, wid=10),
    ])
    v = assert_click_target("Google Chrome", 500, 500)
    assert v["ok"] is True


def test_format_safety_failure_overlay():
    msg = format_safety_failure({
        "ok": False, "reason": "occluded_by_overlay", "owner": "Mirroria",
        "owner_title": "悬浮球", "expected_app": "Google Chrome", "x": 5, "y": 6,
    })
    assert "Mirroria" in msg
    assert "Google Chrome" in msg
    assert "overlay" in msg or "covers" in msg


def _shape(w):
    """Local helper mirroring _click_safety._shape_window for building pure-
    function test inputs without touching Quartz."""
    from qcu.layers._click_safety import _shape_window
    return _shape_window(w)
