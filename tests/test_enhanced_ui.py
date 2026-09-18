"""Tests for the AXEnhancedUserInterface / AXManualAccessibility unlock.

The unlock makes Catalyst/WebKit and Chromium/Electron apps expose their full
window content over AX. These tests pin the failure semantics:

  1. A successful set caches the pid — repeat observes don't re-pay the cost.
  2. Chromium/Electron honor BOTH flags: when AXEnhancedUserInterface is
     rejected (real-machine Cursor returns kAXErrorNotImplemented -25208) the
     AXManualAccessibility fallback is still attempted, and a successful
     fallback counts as enabled (reported with ``via``).
  3. When BOTH sets are rejected, a read-back of AXEnhancedUserInterface
     decides: newer macOS builds reject the set while the attribute is already
     True (real-machine Cursor behavior) — that reports ``enabled`` with
     ``via="already_true"``. A genuinely off flag reports ``failed`` and the
     pid is NOT cached, so the next observe retries. The old code cached
     unconditionally, silently assuming the tree was unlocked when it was not.
  4. A missing/broken pyobjc import reports ``unavailable`` instead of
     vanishing into a bare ``except: pass``.
  5. AXManualAccessibility failure never masks a successful enhanced set.
  6. observe() surfaces the unlock status in routing_meta so a sparse tree can
     be attributed to a failed unlock rather than misread as "no controls".

All tests stub the one-line ``_ax_set_attr`` seam; no real AX permission or
target app is required.
"""

from __future__ import annotations

import pytest

from qcu.layers.desktop_ax import DesktopAXLayer


@pytest.fixture
def layer():
    L = DesktopAXLayer()
    L._available = True
    L._cached_trusted = True
    return L


def _fake_setter(layer, script):
    """Install a scripted _ax_set_attr. ``script`` maps attribute -> error
    code (int) or an exception instance to raise. Returns the call log."""
    calls: list[tuple[str, object]] = []

    def setter(elem, attr, value):
        calls.append((attr, value))
        outcome = script[attr]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    layer._ax_set_attr = setter
    return calls


def test_successful_set_is_cached(layer):
    calls = _fake_setter(layer, {"AXEnhancedUserInterface": 0, "AXManualAccessibility": 0})
    first = layer._ensure_enhanced_ui(object(), 1234)
    assert first["status"] == "enabled"
    second = layer._ensure_enhanced_ui(object(), 1234)
    assert second == {"status": "already_set"}
    # The setter ran exactly once per attribute; the repeat observe hit cache.
    assert calls == [("AXEnhancedUserInterface", True), ("AXManualAccessibility", True)]


def test_enhanced_rejected_falls_back_to_manual_accessibility(layer):
    """The real Cursor case: enhanced set -> -25208, manual set succeeds."""
    calls = _fake_setter(layer, {"AXEnhancedUserInterface": -25208, "AXManualAccessibility": 0})
    meta = layer._ensure_enhanced_ui(object(), 888)
    assert meta["status"] == "enabled"
    assert meta["via"] == "AXManualAccessibility"
    assert meta["enhanced_ui_error"] == -25208
    assert 888 in layer._enhanced_apps
    assert calls == [("AXEnhancedUserInterface", True), ("AXManualAccessibility", True)]


def test_both_flags_failed_is_not_cached_and_retried(layer):
    calls = _fake_setter(layer, {"AXEnhancedUserInterface": -25204, "AXManualAccessibility": -25204})
    # Read-back seam: flag genuinely off.
    layer._ax_get_attr = lambda elem, attr: (0, False)
    first = layer._ensure_enhanced_ui(object(), 4321)
    assert first["status"] == "failed"
    assert first["ax_error"] == -25204
    assert first["manual_accessibility_error"] == -25204
    assert 4321 not in layer._enhanced_apps
    # Next observe retries the sets instead of assuming they took effect.
    layer._ax_set_attr = lambda elem, attr, value: (calls.append((attr, value)), 0)[1]
    retry = layer._ensure_enhanced_ui(object(), 4321)
    assert retry["status"] == "enabled"
    assert 4321 in layer._enhanced_apps


def test_rejected_set_disproved_by_read_back(layer):
    """The real Cursor case on newer macOS: the set returns
    kAXErrorNotImplemented (-25208) yet AXEnhancedUserInterface reads back
    True — the unlock is already in effect, so report enabled, not failed."""
    _fake_setter(layer, {"AXEnhancedUserInterface": -25208, "AXManualAccessibility": -25208})
    layer._ax_get_attr = lambda elem, attr: (0, True)
    meta = layer._ensure_enhanced_ui(object(), 999)
    assert meta["status"] == "enabled"
    assert meta["via"] == "already_true"
    assert meta["enhanced_ui_error"] == -25208
    assert 999 in layer._enhanced_apps


def test_missing_pyobjc_reports_unavailable(layer):
    calls = _fake_setter(layer, {"AXEnhancedUserInterface": ImportError("no ApplicationServices")})
    meta = layer._ensure_enhanced_ui(object(), 777)
    assert meta["status"] == "unavailable"
    assert "ImportError" in meta["error"]
    assert 777 not in layer._enhanced_apps
    assert len(calls) == 1


def test_manual_accessibility_failure_does_not_mask_success(layer):
    _fake_setter(layer, {"AXEnhancedUserInterface": 0, "AXManualAccessibility": -25202})
    meta = layer._ensure_enhanced_ui(object(), 555)
    assert meta["status"] == "enabled"
    assert meta["manual_accessibility_error"] == -25202
    assert 555 in layer._enhanced_apps


def test_different_pids_are_unlocked_independently(layer):
    calls = _fake_setter(layer, {"AXEnhancedUserInterface": 0, "AXManualAccessibility": 0})
    layer._ensure_enhanced_ui(object(), 100)
    layer._ensure_enhanced_ui(object(), 200)
    assert layer._enhanced_apps == {100, 200}
    assert len(calls) == 4


def test_observe_reports_enhanced_ui_status():
    """observe() must surface the unlock status in routing_meta."""
    import inspect
    from qcu.layers import desktop_ax

    src = inspect.getsource(desktop_ax.DesktopAXLayer.observe)
    assert "_ensure_enhanced_ui(" in src
    assert '"enhanced_ui": enhanced_meta' in src
