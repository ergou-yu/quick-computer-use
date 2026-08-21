"""Tests for the observation-blocker remediation hints.

Field report (Kimi): QCU was invoked in desktop context but returned ~0
elements because Accessibility/Automation wasn't granted **for that process
invocation**. The agent saw an empty element list, found no actionable
guidance, and abandoned desktop automation for the terminal. The fix surfaces
a loud, bilingual remediation in two channels:

  1. ``raw_tree`` preface on the degraded Observation (desktop_ax /
     desktop_appleevents) — the reliable channel, since it travels through
     the daemon RPC JSON unchanged.
  2. ``_emit_observation_blocker_hint`` stderr line on the local (non-daemon)
     observe/act path — mirrors the existing session_start/doctor hints.

These tests pin both. They run without a real macOS display by exercising the
pure-function hint logic and the degraded Observation construction directly.
"""

from __future__ import annotations

import io
import sys

import pytest

from qcu.cli_handlers import _emit_observation_blocker_hint


# ===========================================================================
# _emit_observation_blocker_hint — pure-function stderr logic
# ===========================================================================


def _capture_stderr(payload):
    """Run the hint helper against ``payload`` and return the stderr text."""
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        _emit_observation_blocker_hint(payload)
        return sys.stderr.getvalue()
    finally:
        sys.stderr = old


def test_hint_fires_for_trusted_false():
    """The Kimi case: AX not trusted → stderr must say so + name the fix."""
    out = _capture_stderr({
        "routing_meta": {"trusted": False, "reason": "Accessibility permission not granted"},
        "elements": [],
    })
    assert "Accessibility" in out or "輔助使用" in out
    assert "qcu doctor" in out
    assert "session end" in out


def test_hint_fires_for_available_false():
    """Apple Events / Automation not granted → stderr names the layer gap."""
    out = _capture_stderr({
        "routing_meta": {"available": False, "reason": "osascript unavailable"},
    })
    assert "不可用" in out or "unavailable" in out.lower()
    assert "qcu doctor" in out


def test_hint_fires_for_act_permission_message():
    """act() payloads carry the cause in `message` (LayerResult), not
    routing_meta. The hint must still catch it."""
    out = _capture_stderr({
        "ok": False,
        "message": "Accessibility permission not granted; pass needs_visual_confirm",
    })
    assert "Accessibility" in out or "輔助使用" in out
    assert "qcu doctor" in out


def test_hint_silent_for_healthy_observation():
    """A normal observe (trusted=True, has elements) must NOT emit anything."""
    out = _capture_stderr({
        "routing_meta": {"trusted": True},
        "elements": [{"ref": "ref_0"}, {"ref": "ref_1"}],
    })
    assert out == ""


def test_hint_silent_for_no_frontmost_app():
    """'no frontmost app' is a scoping gap (available=True), not a permission
    blocker — the raw_tree preface already handles it. stderr stays quiet so
    we don't double-warn."""
    out = _capture_stderr({
        "routing_meta": {"available": True, "reason": "no frontmost application"},
    })
    assert out == ""


# ===========================================================================
# desktop_ax degraded Observation carries a raw_tree preface
# ===========================================================================


def test_desktop_ax_not_trusted_has_raw_tree_preface(monkeypatch):
    """When AX is not trusted, observe() must return raw_tree with a
    remediation preface (not just routing_meta.reason). This is the channel
    the daemon RPC preserves when stderr is swallowed."""
    from qcu.layers import desktop_ax

    L = desktop_ax.DesktopAXLayer()
    L._available = True
    # Force "not trusted" on both the cached and prompt probes.
    monkeypatch.setattr(L, "is_trusted", lambda *, prompt=False: False)

    obs = L.observe()
    rm = obs.routing_meta or {}
    assert rm.get("trusted") is False
    assert obs.raw_tree is not None
    # The preface must tell the agent this is a permission gap, not empty UI,
    # and name the recovery commands.
    assert "輔助使用" in obs.raw_tree or "Accessibility" in obs.raw_tree
    assert "qcu doctor" in obs.raw_tree or "session end" in obs.raw_tree


def test_desktop_ax_missing_pyobjc_has_raw_tree_preface():
    """When pyobjc is unavailable, observe() must return raw_tree explaining
    the dependency gap + install command."""
    from qcu.layers import desktop_ax

    L = desktop_ax.DesktopAXLayer()
    L._available = False

    obs = L.observe()
    assert obs.raw_tree is not None
    assert "pyobjc" in obs.raw_tree.lower() or "PyObjC" in obs.raw_tree
    assert "pip install" in obs.raw_tree


# ===========================================================================
# desktop_appleevents degraded Observation carries a raw_tree preface
# ===========================================================================


def test_appleevents_not_available_has_raw_tree_preface(monkeypatch):
    """When Automation (Apple Events) isn't granted, the apple events layer
    must return raw_tree with the remediation preface."""
    from qcu.layers import desktop_appleevents

    L = desktop_appleevents.DesktopAppleEventsLayer()
    monkeypatch.setattr(L, "is_available", lambda *, prompt=True: False)

    obs = L.observe()
    rm = obs.routing_meta or {}
    assert rm.get("available") is False
    assert obs.raw_tree is not None
    assert "Automation" in obs.raw_tree or "自動化" in obs.raw_tree
    assert "qcu doctor" in obs.raw_tree


def test_appleevents_no_frontmost_has_scoping_preface(monkeypatch):
    """The 'no frontmost app' path gets a scoping preface (launch_app/--app),
    distinct from the permission preface."""
    from qcu.layers import desktop_appleevents

    L = desktop_appleevents.DesktopAppleEventsLayer()
    # is_available True, but _target_app_name resolves to None (no frontmost).
    # Stub it directly — setting _target_app/_last_app alone doesn't prevent
    # the osascript frontmost fallback from finding a real app on the test host.
    monkeypatch.setattr(L, "is_available", lambda *, prompt=True: True)
    monkeypatch.setattr(L, "_target_app_name", lambda: None)

    obs = L.observe()
    assert obs.raw_tree is not None
    assert "launch_app" in obs.raw_tree or "--app" in obs.raw_tree
