"""Tests for the verified=no → P4 coordinate-click retry path in desktop_ax.

The whole point of the verify integration (upgrade 3) is that AXPress can
return err=0 even when the app does nothing (Electron stub no-op, disabled
menu, etc.). When that happens, desktop_ax._click must NOT just report
``ok=True`` and leave the LLM stuck — it must:

  1. Detect via ``_verify_action`` that verified == "no"
  2. Fall through to P4 (coordinate CGEvent click)
  3. Verify the P4 click too
  4. Surface BOTH attempts (with their verify verdicts) in the final
     LayerResult so the LLM can decide whether to retry or escalate

These tests mock ``_ax_native_click`` / ``_verify_action`` / ``_resolve_ax_element``
so they run on any platform without a real macOS app behind them.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from qcu.common.types import LayerResult
from qcu.layers import desktop_ax


@pytest.fixture
def layer():
    L = desktop_ax.DesktopAXLayer()
    # Force-availability and trust so the dispatch logic is reachable; the
    # pure-parsing paths these tests exercise don't touch Quartz/AX APIs.
    L._available = True
    L._cached_trusted = True
    return L


@pytest.fixture(autouse=True)
def _bypass_click_safety(monkeypatch):
    """Skip the click-safety gate (introduced to fix multi-monitor wrong-
    window clicks) for these P4-retry unit tests. They run on any platform
    and patch the CGEvent layer directly, so the safety gate's real Quartz
    window-list probe would either no-op the click (real Quartz sees a
    different owner than ``FakeApp``) or fail-open inconsistently. The gate
    itself has dedicated coverage in ``tests/test_click_safety.py``.
    """
    monkeypatch.setattr(
        desktop_ax.DesktopAXLayer, "_assert_click_target_for",
        lambda self, cx, cy: {"ok": True, "reason": "ok", "x": cx, "y": cy},
    )


# ===========================================================================
# Scenario: AXPress returns ok but verify=no → coordinate click fallback fires
# ===========================================================================


def test_axpress_ok_verify_no_triggers_p4_coordinate_click(layer, monkeypatch):
    """AXPress IPC succeeds but is a stub no-op (verified=no). _click must
    fall through to the P4 coordinate path and try again there.
    """
    # Pre-seed the ref cache so _click finds bounds and a target_app.
    layer._last_refs = {
        "ref_42": {
            "role": "button",
            "cx": 100.0,
            "cy": 200.0,
            "bounds": None,
            "path": [0, 0, 1],
        }
    }
    layer._target_app = "FakeApp"

    # Mock _resolve_ax_element so the AX path is reachable and to give the
    # P4 retry something to verify against.
    fake_elem = MagicMock(name="fake_elem")
    monkeypatch.setattr(layer, "_resolve_ax_element", lambda ref: fake_elem)

    # AXPress IPC succeeds (returns err=0 / True), as it does on stub no-op.
    monkeypatch.setattr(
        layer, "_ax_native_click",
        lambda elem, role, atype="click": (True, "AXPress"),
    )
    # First verify (after AXPress) → no (stub no-op detected).
    # Second verify (after P4 coordinate click) → yes (real change observed).
    call_log = {"verify_calls": []}

    def fake_verify(elem, role, action, expected_text=None):
        call_idx = len(call_log["verify_calls"])
        call_log["verify_calls"].append({"elem": elem, "action": action})
        # 1st call (from AXPress path) → NO; 2nd call (from P4 retry) → YES
        if call_idx == 0:
            return {"verified": "no", "signal": "button_press", "samples": 15, "matched_at_ms": None}
        return {"verified": "yes", "signal": "button_press", "samples": 2, "matched_at_ms": 124.5}

    monkeypatch.setattr(layer, "_verify_action", fake_verify)

    # Also mock the Quartz CGEvent path so we don't need ApplicationServices.
    fired = {"mouse_events": 0}
    from Quartz.CoreGraphics import (
        CGEventCreateMouseEvent, CGEventPost,
    )

    original_run = None

    def fake_create_mouse_event(none, evt_type, pt, btn):
        return MagicMock(name="fake_cg_event")

    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventCreateMouseEvent", fake_create_mouse_event, raising=False
    )
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventPost",
        lambda tap, evt: fired.__setitem__("mouse_events", fired["mouse_events"] + 1),
        raising=False,
    )
    # CGPoint is needed by _click; mock it too.
    monkeypatch.setattr(
        "Quartz.CGPointMake", lambda x, y: (x, y), raising=False
    )

    result = layer._click("click", {"ref": "ref_42"})

    # P4 must have fired.
    assert fired["mouse_events"] >= 2  # down + up = 2 events
    # _verify_action called twice: once for AXPress, once for P4 coordinate
    assert len(call_log["verify_calls"]) == 2
    assert call_log["verify_calls"][0]["action"] == "click"
    assert call_log["verify_calls"][1]["action"] == "click"
    # Final LayerResult.signal is ok=True with verified=yes on coordinate path.
    assert result.ok is True
    assert "CGEvent" in result.message
    assert "verified" in result.message.lower()
    # The structured data confirms verified=yes on the second attempt.
    v = (result.data or {}).get("verification", {})
    assert v.get("verified") == "yes"


# ===========================================================================
# Scenario: AXPress returns ok and verify=yes → no P4 fallback needed
# ===========================================================================


def test_axpress_ok_verify_yes_skips_p4(layer, monkeypatch):
    """AXPress + first verify=yesshould short-circuit and NOT call CGEvent."""

    layer._last_refs = {
        "ref_43": {"role": "button", "cx": 100.0, "cy": 200.0, "bounds": None, "path": [0]},
    }
    layer._target_app = "FakeApp"

    fake_elem = MagicMock(name="fake_elem")
    monkeypatch.setattr(layer, "_resolve_ax_element", lambda ref: fake_elem)
    monkeypatch.setattr(
        layer, "_ax_native_click",
        lambda elem, role, atype="click": (True, "AXPress"),
    )
    verify_calls = {"n": 0}

    def fake_verify(elem, role, action, expected_text=None):
        verify_calls["n"] += 1
        return {"verified": "yes", "signal": "button_press", "samples": 1, "matched_at_ms": 80.0}

    monkeypatch.setattr(layer, "_verify_action", fake_verify)

    fired = {"mouse_events": 0}
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventPost",
        lambda tap, evt: fired.__setitem__("mouse_events", fired["mouse_events"] + 1),
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventCreateMouseEvent",
        lambda *a, **k: MagicMock(),
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CGPointMake", lambda x, y: (x, y), raising=False,
    )

    result = layer._click("click", {"ref": "ref_43"})

    # Only one verify call (right after AXPress); no P4 fallback.
    assert verify_calls["n"] == 1
    assert fired["mouse_events"] == 0
    assert result.ok is True
    assert "AXPress" in result.message
    assert "verified" in (result.data or {}).get("verification", {})


# ===========================================================================
# Scenario: both AXPress and P4 verify=no → surface both failures honestly
# ===========================================================================


def test_both_axpress_and_p4_verify_no_surface_clear(layer, monkeypatch):
    """Critical regression-protector: when both paths verify=no, the final
    LayerResult must say so explicitly. This is what tells the LLM "give up
    on this path — fall back to web URL or escalate to a visual method"."""
    layer._last_refs = {
        "ref_44": {"role": "button", "cx": 100.0, "cy": 200.0, "bounds": None, "path": [0]},
    }
    layer._target_app = "FakeApp"

    fake_elem = MagicMock(name="fake_elem")
    monkeypatch.setattr(layer, "_resolve_ax_element", lambda ref: fake_elem)
    monkeypatch.setattr(
        layer, "_ax_native_click",
        lambda elem, role, atype="click": (True, "AXPress"),
    )

    def fake_verify(elem, role, action, expected_text=None):
        # Both attempts return NO.
        return {"verified": "no", "signal": "button_press", "samples": 25, "matched_at_ms": None}

    monkeypatch.setattr(layer, "_verify_action", fake_verify)
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventPost",
        lambda tap, evt: None,
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventCreateMouseEvent",
        lambda *a, **k: MagicMock(),
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CGPointMake", lambda x, y: (x, y), raising=False,
    )

    result = layer._click("click", {"ref": "ref_44"})
    # Even on dual-failure we report ok=True (IPC succeeded) but with a
    # verbose message that flags the stub no-op + P4 retry failed.
    assert result.ok is True
    assert "verify=no" in result.message.lower() or "verify=no" in (
        (result.data or {}).get("verification", {}).get("verified", "")
    )
    # Message contains both attempts so LLM has full context.
    assert "no-op" in result.message.lower() or "verify=no" in result.message.lower()


# ===========================================================================
# Scenario: coordinate-only click (no ref resolved) → verify only if element
# can be re-resolved afterwards
# ===========================================================================


def test_coordinate_click_with_unresolvable_ref_skips_verify(layer, monkeypatch):
    """When click is called with explicit {x,y} but the ref can't be
    re-resolved after the click, we accept without verify (the contract
    is: verify only if we have something to diff)."""

    fired = {"events": 0}
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventPost",
        lambda tap, evt: fired.__setitem__("events", fired["events"] + 1),
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CoreGraphics.CGEventCreateMouseEvent",
        lambda *a, **k: MagicMock(),
        raising=False,
    )
    monkeypatch.setattr(
        "Quartz.CGPointMake", lambda x, y: (x, y), raising=False,
    )
    # No ref → no _resolve_ax_element call → no verify.
    result = layer._click("click", {"x": 100, "y": 200})
    assert result.ok is True
    # No verification data attached (verify is ref-gated).
    assert (result.data or {}).get("verification") in (None, {})
    assert fired["events"] >= 2  # down + up fired
