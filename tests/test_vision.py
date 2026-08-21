"""Unit tests for the V2 OCR vision layer.

Focus: ``occlusion_check`` (the pre-capture visibility gate) and ``match``
(the OCR-hit tie-break ladder). The heavier Vision/Quartz paths
(``capture_window``, ``ocr``, ``click``) are integration-tested on the
live host; here we only cover pure-logic branches that can run without a
display server.

The occlusion-check logic is the critical correctness piece — it must
return ``occluded=False`` when no window covers the target (vs. the old
``kCGWindowIsOnscreen`` heuristic that lied about it on macOS 26).
"""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from qcu.layers import _vision
from qcu.layers._vision import (
    OcrHit,
    OCCLUSION_MIN_COVERAGE,
    occlusion_check,
    match,
)


# ---------------------------------------------------------------------------
# Fakes for CGWindowListCopyWindowInfo
# ---------------------------------------------------------------------------


def _win(num: int, owner: str, x: float, y: float, w: float, h: float,
         *, layer: int = 0, alpha: float = 1.0, title: str = "") -> dict[str, Any]:
    return {
        "kCGWindowNumber": num,
        "kCGWindowOwnerName": owner,
        "kCGWindowLayer": layer,
        "kCGWindowAlpha": alpha,
        "kCGWindowTitle": title,
        "kCGWindowBounds": {"X": x, "Y": y, "Width": w, "Height": h},
    }


@pytest.fixture
def fake_quartz(monkeypatch):
    """Inject a controllable stand-in for the Quartz symbols that
    ``occlusion_check`` imports. Returns a list that tests mutate before
    invoking ``occlusion_check``."""
    state: list[dict[str, Any]] = []

    def fake_copy(_option, _wid):
        return list(state)

    fake_mod = types.ModuleType("Quartz")
    fake_mod.CGWindowListCopyWindowInfo = fake_copy
    fake_mod.kCGWindowListOptionOnScreenOnly = 1 << 0
    fake_mod.kCGNullWindowID = 0
    monkeypatch.setitem(sys.modules, "Quartz", fake_mod)
    return state


# ===========================================================================
# occlusion_check — target fully covered by one higher-stacking window
# ===========================================================================


def test_occlusion_detects_full_cover(fake_quartz):
    # Frontmost first. TextEdit (target) sits at index 2; ChatGPT above it
    # at index 0 fully covers the rect. Terminal is parked far away so it
    # doesn't intersect at all.
    target = _win(100, "TextEdit", 185, 83, 603, 505)
    fake_quartz.extend([
        _win(1, "ChatGPT", 0, 79, 1722, 960, title="ChatGPT"),   # covers all
        _win(2, "Terminal", 2000, 2000, 400, 300),               # off in corner, no overlap
        target,
        _win(3, "Finder", 0, 0, 100, 50),                        # lower stacking, ignored
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is True
    assert res["coverage_ratio"] == 1.0
    # Finder is below target stacking-wise → must not be flagged
    owners = [o["owner"] for o in res["occluders"]]
    assert "Finder" not in owners
    assert "ChatGPT" in owners
    assert "Terminal" not in owners  # far away, no rect intersection


def test_occlusion_full_cover_terminal_no_overlap(fake_quartz):
    # Same as above but push Terminal completely off — only ChatGPT remains.
    target = _win(100, "TextEdit", 185, 83, 603, 505)
    fake_quartz.extend([
        _win(1, "ChatGPT", 0, 79, 1722, 960),
        _win(2, "Terminal", 2000, 2000, 400, 300),
        target,
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is True
    owners = [o["owner"] for o in res["occluders"]]
    assert owners == ["ChatGPT"]
    assert res["coverage_ratio"] == 1.0


# ===========================================================================
# occlusion_check — target fully visible (frontmost)
# ===========================================================================


def test_occlusion_frontmost_is_clean(fake_quartz):
    # Target at index 0 → nothing above it → not occluded even with overlap.
    target = _win(100, "TextEdit", 100, 100, 600, 400)
    fake_quartz.extend([
        target,
        _win(1, "Finder", 0, 0, 1728, 1072),  # bigger but below
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is False
    assert res["occluders"] == []
    assert res["coverage_ratio"] == 0.0


def test_occlusion_partial_below_threshold(fake_quartz):
    # Big window above just clips a 1px strip — below OCCLUSION_MIN_COVERAGE.
    target = _win(100, "TextEdit", 100, 100, 600, 400)  # 240_000 px²
    fake_quartz.extend([
        _win(1, "Sliver", 0, 0, 1728, 1),  # 1-pixel-high strip: tiny intersection
        target,
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is False
    assert res["coverage_ratio"] == 0.0


# ===========================================================================
# occlusion_check — duplicates de-duped
# ===========================================================================


def test_occlusion_de_dupes_repeated_window(fake_quartz):
    # Feishu sometimes shows two copies at identical bounds (observed
    # 2026-08-03). They shouldn't both appear in the occluder list.
    target = _win(100, "TextEdit", 100, 100, 600, 400)
    fake_quartz.extend([
        _win(5, "Feishu", 0, 0, 1728, 1072),
        _win(6, "Feishu", 0, 0, 1728, 1072),  # exact dup
        target,
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is True
    feishu = [o for o in res["occluders"] if o["owner"] == "Feishu"]
    assert len(feishu) == 1


# ===========================================================================
# occlusion_check — edge cases
# ===========================================================================


def test_occlusion_target_missing_from_list(fake_quartz):
    target = _win(100, "TextEdit", 100, 100, 600, 400)
    fake_quartz.extend([
        _win(1, "ChatGPT", 0, 0, 1728, 1072),
        # target wid 100 NOT in the list (was minimized between resolve_wid and check)
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is True
    assert res["reason"] == "target_not_in_onscreen_list"


def test_occlusion_zero_size_target(fake_quartz):
    target = _win(100, "TextEdit", 100, 100, 0, 0)
    fake_quartz.extend([target])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is True
    assert res["reason"] == "target_zero_size"


def test_occlusion_skips_non_layer0_window(fake_quartz):
    # Menu-bar / overlay-layer windows don't occlude usable app windows.
    target = _win(100, "TextEdit", 100, 100, 600, 400)
    fake_quartz.extend([
        _win(1, "MenuBar", 0, 0, 1728, 25, layer=25),  # menubar layer
        _win(2, "FullscreenOverlay", 0, 0, 1728, 1072, layer=1),
        target,
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is False
    assert res["occluders"] == []


def test_occlusion_skips_transparent_window(fake_quartz):
    target = _win(100, "TextEdit", 100, 100, 600, 400)
    fake_quartz.extend([
        _win(1, "Invisible", 0, 0, 1728, 1072, alpha=0.0),
        target,
    ])
    res = occlusion_check(100, target["kCGWindowBounds"])
    assert res["occluded"] is False


# ===========================================================================
# match — tie-break ladder (pure-logic, no Quartz needed)
# ===========================================================================


def _hit(text: str, conf: float = 0.9, bb=(0.1, 0.1, 0.1, 0.1)) -> OcrHit:
    return OcrHit(
        text=text,
        confidence=conf,
        bbox_origin_x_norm=bb[0],
        bbox_origin_y_norm=bb[1],
        bbox_width_norm=bb[2],
        bbox_height_norm=bb[3],
    )


def test_match_picks_exact_over_substring():
    hits = [
        _hit("File", conf=0.5),                     # exact, low conf
        _hit("Filesystem options", conf=0.99),      # substring, high conf
    ]
    chosen, ambig = match(hits, "File")
    assert chosen is not None
    assert chosen.text == "File"
    assert chosen.exact is True
    assert ambig == []


def test_match_picks_higher_confidence_among_substrings():
    hits = [
        _hit("Open File…", conf=0.6),
        _hit("Recent Files", conf=0.95),  # higher conf wins
    ]
    chosen, _ = match(hits, "File")
    assert chosen.text == "Recent Files"


def test_match_returns_empty_when_no_candidates():
    hits = [_hit("Window"), _hit("Help")]
    chosen, ambig = match(hits, "Edit")
    assert chosen is None
    assert ambig == []


def test_match_ambiguous_when_truly_tied():
    # Two exact hits with identical conf and identical bbox area → tie.
    hits = [
        _hit("Edit", conf=0.9, bb=(0.1, 0.1, 0.1, 0.1)),
        _hit("Edit", conf=0.9, bb=(0.1, 0.1, 0.1, 0.1)),
    ]
    chosen, ambig = match(hits, "Edit")
    assert chosen is None
    assert len(ambig) == 2


def test_match_normalizes_full_width_letters():
    # Full-width Ｆｉｌｅ should normalize-equal to File. The shared
    # normalizer is owned by _ax_verify; V2 reuses it.
    hits = [_hit("Ｆｉｌｅ", conf=0.9)]
    chosen, _ = match(hits, "File")
    assert chosen is not None
    assert chosen.exact is True


# ===========================================================================
# OcrHit.center_screen_px — coordinate math sanity
# ===========================================================================


def test_center_screen_px_origin_flip_and_scale():
    # bbox at normalized (0.4, 0.3), size (0.2, 0.2) on 1000×1000 image, scale=2.
    # Vision bbox is bottom-left origin. Center y_norm = 1 - (0.3 + 0.2) + 0.1 = 0.6
    # cx = 0.5 * 1000 * 2 = 1000
    # cy = 0.6 * 1000 * 2 = 1200
    h = OcrHit(
        text="x", confidence=1.0,
        bbox_origin_x_norm=0.4, bbox_origin_y_norm=0.3,
        bbox_width_norm=0.2, bbox_height_norm=0.2,
        image_w_px=1000, image_h_px=1000, scale=2,
    )
    cx, cy = h.center_screen_px()
    assert cx == pytest.approx(1000.0)
    assert cy == pytest.approx(1200.0)


# ===========================================================================
# _backing_scale_for_bounds — Retina / mixed-DPI scaling
#
# The fix for the "Retina 坐标换算" report: the OCR→click conversion used
# to hardcode /2, which silently mis-clicks on a non-Retina external display
# (scale 1.0). _backing_scale_for_bounds probes the real
# NSScreen.backingScaleFactor() for the screen hosting the window, falling
# back to 2.0 when AppKit/NSScreen is unavailable.
# ===========================================================================


def test_backing_scale_fallback_when_appkit_missing(monkeypatch):
    """When AppKit can't be imported, the helper must return 2.0 (preserving
    the historical Retina behavior) rather than raising."""
    import builtins
    real_import = builtins.__import__

    def _block_appkit(name, *args, **kwargs):
        if name == "AppKit" or name.startswith("AppKit."):
            raise ImportError("simulated: AppKit unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_appkit)
    s = _vision._backing_scale_for_bounds({"X": 0, "Y": 0, "Width": 1000, "Height": 800})
    assert s == 2.0


def test_backing_scale_fallback_when_no_screen_contains_bounds(monkeypatch):
    """If no NSScreen contains the window center (offscreen / bogus bounds),
    the helper returns the safe 2.0 default instead of None."""
    class _FakeFrame:
        def __init__(self, x, y, w, h):
            class _O:
                def __init__(self, x, y): self.x, self.y = x, y
            class _S:
                def __init__(self, w, h): self.width, self.height = w, h
            self.origin = _O(x, y)
            self.size = _S(w, h)

    class _FakeScreen:
        def __init__(self, frame, scale): self._f, self._s = frame, scale
        def frame(self): return self._f
        def backingScaleFactor(self): return self._s

    class _FakeNSScreen:
        @staticmethod
        def screens(): return [_FakeScreen(_FakeFrame(0, 0, 1920, 1080), 2.0)]

    monkeypatch.setitem(sys.modules, "AppKit", types.ModuleType("AppKit"))
    sys.modules["AppKit"].NSScreen = _FakeNSScreen
    # Point far away from any screen.
    s = _vision._backing_scale_for_bounds({"X": 50000, "Y": 50000, "Width": 10, "Height": 10})
    assert s == 2.0


def test_backing_scale_picks_correct_screen_in_mixed_dpi(monkeypatch):
    """A multi-monitor setup: Retina (2.0) on the left, non-Retina external
    (1.0) on the right. A window centered on the external display must
    report 1.0, not the Retina default."""
    class _FakeFrame:
        def __init__(self, x, y, w, h):
            class _O:
                def __init__(self, x, y): self.x, self.y = x, y
            class _S:
                def __init__(self, w, h): self.width, self.height = w, h
            self.origin = _O(x, y)
            self.size = _S(w, h)

    class _FakeScreen:
        def __init__(self, frame, scale): self._f, self._s = frame, scale
        def frame(self): return self._f
        def backingScaleFactor(self): return self._s

    retina = _FakeScreen(_FakeFrame(0, 0, 1440, 900), 2.0)
    external = _FakeScreen(_FakeFrame(1440, 0, 1920, 1080), 1.0)

    class _FakeNSScreen:
        @staticmethod
        def screens(): return [retina, external]

    monkeypatch.setitem(sys.modules, "AppKit", types.ModuleType("AppKit"))
    sys.modules["AppKit"].NSScreen = _FakeNSScreen
    # Window centered on the external display.
    s = _vision._backing_scale_for_bounds(
        {"X": 1440 + 100, "Y": 100, "Width": 800, "Height": 600}
    )
    assert s == 1.0


def test_backing_scale_retina_on_main_screen(monkeypatch):
    """Sanity: a window on the Retina main screen reports 2.0."""
    class _FakeFrame:
        def __init__(self, x, y, w, h):
            class _O:
                def __init__(self, x, y): self.x, self.y = x, y
            class _S:
                def __init__(self, w, h): self.width, self.height = w, h
            self.origin = _O(x, y)
            self.size = _S(w, h)

    class _FakeScreen:
        def __init__(self, frame, scale): self._f, self._s = frame, scale
        def frame(self): return self._f
        def backingScaleFactor(self): return self._s

    class _FakeNSScreen:
        @staticmethod
        def screens(): return [_FakeScreen(_FakeFrame(0, 0, 1440, 900), 2.0)]

    monkeypatch.setitem(sys.modules, "AppKit", types.ModuleType("AppKit"))
    sys.modules["AppKit"].NSScreen = _FakeNSScreen
    s = _vision._backing_scale_for_bounds({"X": 100, "Y": 100, "Width": 800, "Height": 600})
    assert s == 2.0
