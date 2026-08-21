"""V2 Vision Ladder: Apple Vision OCR-based click + diff verification.

This is the first rung in the Vision Ladder (V1 template-matching is
deferred until real V2 hit-rate data tells us where it's most needed).

CAPABILITY ENVELOPE — read this before promising anything
----------------------------------------------------------
- **click** by visible **text** (button labels, menu items, sidebar entries).
  Empty text (icons without labels, color swatches, custom-drawn glyphs)
  is **out of scope** — escalate to V4 small-VLM.
- **fill** only on inputs that already have visible text (placeholder OR
  pre-filled value). Empty no-placeholder inputs can't be OCR-located
  → ``locate_fail`` → caller should route to AXValue set (P1) or V4.
- **zero-disturbance is NOT a V2 capability.** The AX layer is the only
  path that operates without changing stacking order. V2 *can* reach a
  target that is visible-but-not-frontmost, but a fully **occluded**
  target cannot be captured or clicked via CGEvent — a window drawn
  over the target's rect is what CGWindowListCreateImage renders.
  When occlusion is detected, V2 returns ``stage="locate_fail"``,
  ``reason="target_occluded"`` with the list of covering windows, so the
  caller can either (a) request an explicit ``activate`` first, or
  (b) fall through to a different layer.

LADDER
------
1. ``capture_window(app)`` — resolve the *current* CGWindowID for the
   target app (never cache — window IDs mutate when apps re-activate /
   re-raise); ``CGWindowListCreateImage`` with the default flag
   (kCGWindowListOptionIncludingWindow = 1, imageOptions = 0).
   ⚠️ NEVER pass ``kCGWindowImageBoundsIgnoreFraming=2`` — when the target
   app isn't frontmost, WindowServer renders the *current frontmost*
   window's contents into the captured frame, producing a silent-content
   swap (caught by probe A on 2026-08-03).
2. ``downsample_if_large(png_bytes)`` — Retina 2× captures on a 2444×2070
   window average **545 ms** OCR. Halving the long edge → ~150 ms with ≤3%
   OCR miss on small text (probe B 2026-08-03). Threshold: ```pixels > 1.5M```.
3. ``ocr(png_bytes)`` — Apple Vision ``VNRecognizeTextRequest``, level=accurate,
   langs [zh-Hans, en-US, zh-Hant]. Coordinate frame is **normalized
   bottom-left origin** — ``y = img_h - (bb.origin.y + bb.size.height) * img_h``
   flips to screen-coordinate top-left origin.
4. ``tie_break(matches)`` — exact > substring, high confidence > low,
   larger bbox > smaller; if still tied → ``ambiguous`` verdict with all
   candidates surfaced for the LLM.
5. ``click_at(x, y)`` — Quartz ``CGEventCreateMouseEvent`` + ``CGEventPost``,
   then a post-click screen diff (SSIM / pHash) for ``verified``.
6. Return: ``LayerResult(ok, verified, via="v2_ocr", data={"stage": ...})``.

FAILURE TAXONOMY (review-driven, must not collapse)
---------------------------------------------------
- ``locate_fail`` — OCR returned no candidates for the desc; the action
  was never sent. Caller: upgrade to V4. ``ok=False``.
- ``act_fail`` — CGEvent dispatch raised (transport failure). Caller:
  retry. ``ok=False``.
- ``verify_fail`` — click dispatched, but pre-vs-post image diff was
  below SSIM threshold (no visible change). Caller: auto-retry once
  (re-ocr on the same target → re-click into the bbox center → re-diff);
  still no change → upgrade to V4. ``ok=True, verified="no"``.

These three are NOT interchangeable. The verify_telemetry schema and any
V4 escalation logic downstream depends on this categorization.
"""

from __future__ import annotations

import hashlib
import io
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Reuse the AX-path normalize so normalization doesn't drift between layers
# (KEYCODES-mismatch lesson: one source of truth).
from qcu.layers._ax_verify import _normalize_for_compare


# ---------------------------------------------------------------------------
# Retina / backing-scale detection
# ---------------------------------------------------------------------------

def _backing_scale_for_bounds(bounds: dict[str, Any]) -> float:
    """Return the screen backing scale factor for the display that contains
    the given window ``bounds`` (a ``kCGWindowBounds``-shaped dict).

    Why this exists: OCR→click conversion historically hardcoded ``/ 2`` and
    ``* 2`` to map between Retina image pixels and screen points. That is
    correct only on a 2× Retina display. On an external non-Retina monitor
    (scale 1.0) or a mixed-DPI multi-monitor setup, the hardcoded 2× silently
    mis-converts and clicks land in the wrong place. This helper probes the
    real ``NSScreen.backingScaleFactor()`` for the screen hosting the window
    and returns it; callers use it as the divisor/multiplier instead of 2.

    Falls back to ``2.0`` when AppKit/NSScreen is unavailable (headless test
    box, missing pyobjc) so behavior on a stock Retina Mac is unchanged.
    """
    try:
        from AppKit import NSScreen  # type: ignore
    except Exception:
        return 2.0
    try:
        bx = float(bounds.get("X", 0))
        by = float(bounds.get("Y", 0))
        bw = float(bounds.get("Width", 0))
        bh = float(bounds.get("Height", 0))
        cx = bx + bw / 2.0
        cy = by + bh / 2.0
        best: Optional[NSScreen] = None
        best_area = -1.0
        for scr in NSScreen.screens():
            frame = scr.frame()
            sx, sy = frame.origin.x, frame.origin.y
            sw, sh = frame.size.width, frame.size.height
            if sx <= cx <= sx + sw and sy <= cy <= sy + sh:
                area = sw * sh
                if area > best_area:
                    best_area = area
                    best = scr
        if best is not None:
            return float(best.backingScaleFactor())
    except Exception:
        pass
    return 2.0


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

DOWNSAMPLE_PIXEL_THRESHOLD = 1_500_000   # 1.5M px
OCR_LANGUAGES = ["zh-Hans", "en-US", "zh-Hant"]
# SSIM threshold for "screen changed after click". Lowered from 0.95 to 0.80
# after I observed that one menu item toggle produces a 1px-level diff that
# only barely crosses ~0.91 SSIM on macOS 26 — wait for real data before tuning.
SSIM_VERIFIED_THRESHOLD = 0.80
DEFAULT_CLICK_VERIFY_TIMEOUT_MS = 600
DEFAULT_CLICK_VERIFY_POLL_MS = 80


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class OcrHit:
    """A single OCR candidate matched against the requested description."""

    text: str
    confidence: float
    bbox_origin_x_norm: float    # bottom-left origin
    bbox_origin_y_norm: float
    bbox_width_norm: float
    bbox_height_norm: float
    # Pixel resolution used to normalize. After downsampling, this is the
    # post-downsampled image size — multiply by ``scale`` to map back to
    # the original screen pixels.
    image_w_px: int = 0
    image_h_px: int = 0
    scale: int = 1               # 2 = original Retina downsampled 2×
    exact: bool = False          # True = exact-string match, False = substring

    def center_screen_px(self) -> tuple[float, float]:
        """Return (cx, cy) in screen pixels (top-left origin).

        ``y`` axis is flipped from Vision's natural bottom-left origin.
        Scale maps back from downsampled image space to original-screen
        pixel space so the resulting coords work with ``CGEventPost``.
        """
        cx_norm = self.bbox_origin_x_norm + self.bbox_width_norm / 2
        cy_norm_top_left = 1.0 - (
            self.bbox_origin_y_norm + self.bbox_height_norm
        ) + self.bbox_height_norm / 2
        return (
            cx_norm * self.image_w_px * self.scale,
            cy_norm_top_left * self.image_h_px * self.scale,
        )


@dataclass
class LayerResultData:
    """Structured data attached to the returned LayerResult."""

    stage: str = "ok"           # ok / locate_fail / act_fail / verify_fail
    via: str = "v2_ocr"
    ocr_n_results: int = 0
    ocr_n_candidates: int = 0
    chosen: Optional[dict[str, Any]] = None
    ambiguous: list[dict[str, Any]] = field(default_factory=list)
    ssim: Optional[float] = None
    scale: int = 1
    elapsed_ms: Optional[float] = None


# ---------------------------------------------------------------------------
# 1. wid resolution — never cache
# ---------------------------------------------------------------------------


def resolve_wid(app: str, window_title: Optional[str] = None) -> Optional[dict[str, Any]]:
    """Look up the live window for ``app`` (by owner name) every time.

    Returns ``{wid, bounds, owner}`` or None when no on-screen window
    matches. ``window_title`` is matched as substring when present (useful
    when an app has multiple open windows, e.g. TextEdit with two docs).
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
    except Exception:
        return None
    try:
        windows = CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        )
    except Exception:
        return None
    for w in windows or []:
        try:
            if (w.get("kCGWindowOwnerName") or "") != app:
                continue
            if w.get("kCGWindowLayer", 0) != 0:
                continue  # skip menu bar / overlay layers
            t = w.get("kCGWindowTitle") or ""
            if window_title and window_title not in t:
                continue
            return {
                "wid": w.get("kCGWindowNumber"),
                "bounds": dict(w.get("kCGWindowBounds") or {}),
                "title": t,
                "owner": app,
            }
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------
# 1b. occlusion check — rect intersection with higher-stacking windows
# ---------------------------------------------------------------------------

#: A window covering more than this fraction of the target's rect counts
#: as a real occluder. macOS sometimes reports a 1px-edge intersection on
#: adjacent windows that don't actually obscure anything; 5% filters that.
OCCLUSION_MIN_COVERAGE = 0.05


def occlusion_check(
    target_wid: int, target_bounds: dict[str, float]
) -> dict[str, Any]:
    """Detect whether ``target_wid`` is genuinely visible, not just "registered".

    Contract: ``kCGWindowIsOnscreen`` lies. A background-window whose rect
    is fully covered by another app's window still shows ``onscreen=True``
    in ``CGWindowListCopyWindowInfo``. The real test is geometric + z-order:
    if any other layer-0 window that sits *higher* in the stacking list
    intersects the target's rect, CGWindowListCreateImage will render that
    occluding app into the captured frame — a silent content swap.

    Returns ``{"occluded": bool, "occluders": [...], "coverage_ratio": float}``.

    Implementation notes
    --------------------
    - CG returns the on-screen list **frontmost-first** (lower index = higher
      stacking). So windows at strictly lower indices than the target are
      *above* it.
    - Per-window coverage ratios are reported (not just a boolean) so a
      caller logging telemetry can distinguish "partially covered 8%" from
      "fully masked 100%".
    - Duplicate windows for the same owner (e.g. Feishu's two copies at
      identical bounds — observed 2026-08-03) are de-duped by
      ``(owner, x, y, w, h)`` so they don't artificially inflate the
      occluder count.
    """
    try:
        from Quartz import (
            CGWindowListCopyWindowInfo,
            kCGWindowListOptionOnScreenOnly,
            kCGNullWindowID,
        )
    except Exception:
        return {"occluded": False, "occluders": [], "coverage_ratio": 0.0,
                "reason": "quartz_unavailable"}

    try:
        windows = list(CGWindowListCopyWindowInfo(
            kCGWindowListOptionOnScreenOnly, kCGNullWindowID
        ) or [])
    except Exception:
        return {"occluded": False, "occluders": [], "coverage_ratio": 0.0,
                "reason": "windowlist_failed"}

    # Find target index inside the same on-screen list so stacking is consistent.
    target_idx = None
    for i, w in enumerate(windows):
        if w.get("kCGWindowNumber") == target_wid and w.get("kCGWindowLayer", 0) == 0:
            target_idx = i
            break
    if target_idx is None:
        # Target got reaped between resolve_wid and this call (minimized,
        # closed, space switch). Treat as occluded-of-a-different-kind.
        return {"occluded": True, "occluders": [], "coverage_ratio": 1.0,
                "reason": "target_not_in_onscreen_list"}

    tx = float(target_bounds.get("X", 0))
    ty = float(target_bounds.get("Y", 0))
    tw = float(target_bounds.get("Width", 0))
    th = float(target_bounds.get("Height", 0))
    t_area = tw * th
    if t_area <= 0:
        return {"occluded": True, "occluders": [], "coverage_ratio": 1.0,
                "reason": "target_zero_size"}

    occluders: list[dict[str, Any]] = []
    seen: set[tuple] = set()   # de-dup (owner,x,y,w,h)
    max_coverage = 0.0

    for i in range(target_idx):  # strictly above in stacking
        w = windows[i]
        if w.get("kCGWindowLayer", 0) != 0:
            continue
        # Explicit default to 1.0 — never use ``or`` because 0.0 (fully
        # transparent) is itself falsy and would be replaced by 1.0.
        alpha = w.get("kCGWindowAlpha", 1.0)
        if alpha is None:
            alpha = 1.0
        try:
            alpha = float(alpha)
        except (TypeError, ValueError):
            alpha = 1.0
        if alpha <= 0.0:
            continue
        b = w.get("kCGWindowBounds") or {}
        try:
            bx = float(b["X"]); by = float(b["Y"])
            bw = float(b["Width"]); bh = float(b["Height"])
        except (KeyError, ValueError, TypeError):
            continue
        ix1 = max(tx, bx); iy1 = max(ty, by)
        ix2 = min(tx + tw, bx + bw); iy2 = min(ty + th, by + bh)
        if ix2 <= ix1 or iy2 <= iy1:
            continue
        overlap = (ix2 - ix1) * (iy2 - iy1)
        coverage = overlap / t_area
        if coverage < OCCLUSION_MIN_COVERAGE:
            continue
        owner = str(w.get("kCGWindowOwnerName") or "?")
        key = (owner, round(bx, 1), round(by, 1), round(bw, 1), round(bh, 1))
        if key in seen:
            continue
        seen.add(key)
        occluders.append({
            "owner": owner,
            "title": str(w.get("kCGWindowTitle") or ""),
            "bounds": {"X": bx, "Y": by, "Width": bw, "Height": bh},
            "coverage": round(coverage, 3),
        })
        if coverage > max_coverage:
            max_coverage = coverage

    return {
        "occluded": bool(occluders),
        "occluders": occluders,
        "coverage_ratio": round(max_coverage, 3),
    }


def _activate_and_recheck(
    app: str, window_title: Optional[str], *, timeout_ms: int = 1200
) -> Optional[dict[str, Any]]:
    """Self-heal path: call ``activate_app`` and re-resolve the window.

    The frontmost window of ``app`` won't be occluded (it sits at stacking
    index 0). This helper is invoked only when ``occlusion_check`` reports
    ``occluded=True`` — it polls ``resolve_wid`` + ``occlusion_check`` until
    either the window appears un-occluded (returns the fresh window dict) or
    the timeout elapses (returns None, letting the caller decide).

    Returns a fresh ``{wid, bounds, ...}`` dict on success so the OCR
    pipeline can proceed without re-resolving.
    """
    try:
        from qcu.layers._app_launch import activate_app
    except Exception:
        return None
    ok, _ = activate_app(app)
    if not ok:
        return None
    t0 = time.perf_counter()
    poll_ms = 80
    while (time.perf_counter() - t0) * 1000 < timeout_ms:
        win = resolve_wid(app, window_title)
        if win is not None:
            occ = occlusion_check(win["wid"], win["bounds"])
            if not occ["occluded"]:
                return win
        time.sleep(poll_ms / 1000.0)
    return None


# ---------------------------------------------------------------------------
# 2. capture — default framing flag, never IgnoreFraming
# ---------------------------------------------------------------------------


def capture_window(app: str, window_title: Optional[str] = None) -> tuple[bytes, dict[str, Any]]:
    """Capture the target app's window as PNG bytes.

    Returns ``(png_bytes, meta)`` where meta has ``image_w_px`` /
    ``image_h_px`` (the actual Retina-resolution capture dims). Raises
    ``RuntimeError`` if the window can't be located (=> the caller surfaces
    a clean ``locate_fail``-equivalent at the capture stage; we don't return
    an empty image — empty image is exactly the silent-blank bug we must
    avoid).
    """
    win = resolve_wid(app, window_title)
    if win is None:
        raise RuntimeError(f"no on-screen window for {app!r}{f' / {window_title!r}' if window_title else ''}")
    b = win["bounds"]
    if not {"X", "Y", "Width", "Height"} <= set(b):
        raise RuntimeError(f"window bounds missing fields: {b!r}")
    try:
        from Quartz import CGWindowListCreateImage, CGRectMake
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Quartz import failed: {e}") from e

    rect = CGRectMake(
        float(b["X"]), float(b["Y"]), float(b["Width"]), float(b["Height"])
    )
    # imageOptions = 0 is the DEFAULT. ⚠️ Never set
    # ``kCGWindowImageBoundsIgnoreFraming = 2`` here — when the target is
    # in the background, WindowServer renders the frontmost app into the
    # captured frame, producing a silent-content swap. Probe A 2026-08-03.
    img = CGWindowListCreateImage(rect, 1, win["wid"], 0)
    if img is None:
        raise RuntimeError("CGWindowListCreateImage returned None (TCC? window vanished?)")

    try:
        from Quartz import (
            CGImageDestinationCreateWithData,
            CGImageDestinationAddImage,
            CGImageDestinationFinalize,
        )
        from CoreFoundation import CFDataCreateMutable, kCFAllocatorDefault
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"ImageIO import failed: {e}") from e

    data = CFDataCreateMutable(kCFAllocatorDefault, 0)
    dest = CGImageDestinationCreateWithData(data, "public.png", 1, None)
    if dest is None:
        raise RuntimeError("CGImageDestinationCreateWithData returned None")
    CGImageDestinationAddImage(dest, img, None)
    if not CGImageDestinationFinalize(dest):
        raise RuntimeError("CGImageDestinationFinalize failed")
    # CFDataRef supports __bytes__ in PyObjC 12.2.1.
    buf = bytes(data)
    # The image's pixel dims encode the backing scale (Retina = 2× the window's
    # point dims). We don't have an easy API on CGImageRef in PyObjC, so prefer
    # Pillow for the true pixel size; otherwise approximate from bounds using
    # the real backing scale factor (not a hardcoded 2× — wrong on non-Retina
    # external displays).
    backing_scale = _backing_scale_for_bounds(b)
    try:
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(buf)) as pil:
            w_px, h_px = pil.size
    except Exception:
        # Fall back: screenshot dims = bounds * backingScaleFactor.
        w_px = int(b["Width"] * backing_scale)
        h_px = int(b["Height"] * backing_scale)
    return buf, {
        "image_w_px": w_px,
        "image_h_px": h_px,
        "scale": 1,
        "backing_scale": backing_scale,  # image px ÷ this = screen points
        "wid": win["wid"],
        "bounds": b,
    }


# ---------------------------------------------------------------------------
# 3. downsample-if-large
# ---------------------------------------------------------------------------


def maybe_downsample(png_bytes: bytes) -> tuple[bytes, int]:
    """If the image has > DOWNSAMPLE_PIXEL_THRESHOLD pixels, halve both
    dimensions and return (new_png_bytes, scale) where ``scale`` is the
    factor to multiply downsampled coordinates back to original-screen
    space. For images below the threshold, returns (png_bytes, 1).

    Uses Pillow (the only dependency QCU already chains via Playwright's
    bundled runtime). For images that Pillow can't decode, return the
    original bytes with scale=1 (OCR runs slower but still works).
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return png_bytes, 1
    try:
        with Image.open(io.BytesIO(png_bytes)) as pil:
            w, h = pil.size
            if w * h <= DOWNSAMPLE_PIXEL_THRESHOLD:
                return png_bytes, 1
            # Halve both dims, high-quality LANCZOS so we don't destroy
            # small text legibility harder than we have to.
            half = (max(1, w // 2), max(1, h // 2))
            small = pil.resize(half, Image.Resampling.LANCZOS)
            out = io.BytesIO()
            small.save(out, format="PNG")
            return out.getvalue(), 2
    except Exception:
        return png_bytes, 1


# ---------------------------------------------------------------------------
# 4. OCR
# ---------------------------------------------------------------------------


def ocr(png_bytes: bytes) -> tuple[list[OcrHit], int, int]:
    """Run VNRecognizeTextRequest on the PNG bytes.

    Returns ``(hits, image_w_px, image_h_px)``. Each ``OcrHit`` is already
    in Vision's natural normalized bottom-left coordinate frame; the caller
    either keeps it as-is or calls ``center_screen_px`` after setting the
    ``image_w_px`` / ``scale`` it received from capture_window.
    """
    from AppKit import NSData  # type: ignore
    from Vision import VNImageRequestHandler, VNRecognizeTextRequest

    # VNImageRequestHandler accepts NSData via initWithData:options:
    nsdata = NSData.dataWithBytes_length_(png_bytes, len(png_bytes))
    req = VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(0)             # accurate — small text survives
    req.setRecognitionLanguages_(OCR_LANGUAGES)
    req.setUsesLanguageCorrection_(True)

    handler = VNImageRequestHandler.alloc().initWithData_options_(nsdata, None)
    ok, err = handler.performRequests_error_([req], None)
    if not ok:
        return [], 0, 0

    # Image dims (for caller reference)
    try:
        from PIL import Image  # type: ignore

        with Image.open(io.BytesIO(png_bytes)) as pil:
            iw, ih = pil.size
    except Exception:
        iw, ih = 0, 0

    hits: list[OcrHit] = []
    for obs in (req.results() or []):
        try:
            cands = obs.topCandidates_(1)
            if not cands:
                continue
            c = cands[0]
            bb = obs.boundingBox()
            hits.append(
                OcrHit(
                    text=str(c.string()),
                    confidence=float(c.confidence()),
                    bbox_origin_x_norm=float(bb.origin.x),
                    bbox_origin_y_norm=float(bb.origin.y),
                    bbox_width_norm=float(bb.size.width),
                    bbox_height_norm=float(bb.size.height),
                    image_w_px=iw,
                    image_h_px=ih,
                )
            )
        except Exception:
            continue
    return hits, iw, ih


# ---------------------------------------------------------------------------
# 5. match + tie-break
# ---------------------------------------------------------------------------


def match(hits: list[OcrHit], desc: str) -> tuple[Optional[OcrHit], list[OcrHit]]:
    """Find the best hit matching ``desc``.

    Tie-break ladder (per ZCode review 2026-08-03):
      1. exact normalized equality wins over substring contains
      2. higher OCR confidence wins
      3. larger bbox (in normalized px^2) wins
      4. still tied → caller gets ``None`` plus the full candidate list
         so it can surface ``ambiguous`` to the LLM
    """
    desc_n = _normalize_for_compare(desc)

    # Categorize: exact and substring passes
    exacts: list[OcrHit] = []
    substr: list[OcrHit] = []
    for h in hits:
        text_n = _normalize_for_compare(h.text)
        if text_n == desc_n:
            h.exact = True
            exacts.append(h)
        elif desc_n in text_n:
            substr.append(h)

    candidates = exacts if exacts else substr

    def _rank(h: OcrHit) -> tuple:
        # exact < substring (exact first → wins tie-break pick #
        # 1). Tuple sort: higher is "more candidate", so flip the bool.
        return (
            1 if h.exact else 0,
            h.confidence,
            h.bbox_width_norm * h.bbox_height_norm,
        )

    if not candidates:
        return None, []
    ranked = sorted(candidates, key=_rank, reverse=True)
    if len(ranked) == 1:
        return ranked[0], []
    top = ranked[0]
    # Check whether 2nd-place is genuinely equal per the 3 usable attributes.
    second = ranked[1]
    if _rank(top) == _rank(second):
        # Genuine tie: caller decides.
        return None, ranked
    return top, []


# ---------------------------------------------------------------------------
# 6. click dispatch + verify
# ---------------------------------------------------------------------------


def click_at(x: float, y: float) -> bool:
    """Send a left-button down+up via Quartz CGEvent. Returns True if both
    events dispatched without exception. The events go to whatever window
    is currently frontmost at dispatch time — caller is responsible for
    either having the target be frontmost or accepting
    background-delivery semantics.
    """
    try:
        from Quartz.CoreGraphics import (
            CGEventCreateMouseEvent,
            CGEventPost,
            kCGEventLeftMouseDown,
            kCGEventLeftMouseUp,
            kCGHIDEventTap,
            kCGMouseButtonLeft,
        )
        from Quartz import CGPointMake as CGPoint  # type: ignore
    except Exception:
        return False
    try:
        down = CGEventCreateMouseEvent(
            None, kCGEventLeftMouseDown, CGPoint(x, y), kCGMouseButtonLeft
        )
        up = CGEventCreateMouseEvent(
            None, kCGEventLeftMouseUp, CGPoint(x, y), kCGMouseButtonLeft
        )
        CGEventPost(kCGHIDEventTap, down)
        CGEventPost(kCGHIDEventTap, up)
        return True
    except Exception:
        return False


def perceptual_hash_png(png_bytes: bytes) -> Optional[bytes]:
    """Compute a perceptual hash for a PNG, suitable for fast SSIM-ish diff.

    Returns a small bytes blob (~16 bytes) representing the downsampled
    grayscale image. Two hashes can be compared via Hamming distance; below
    a threshold means "visually identical → no change".
    """
    try:
        from PIL import Image  # type: ignore
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(png_bytes)).convert("L").resize(
            (16, 16), Image.Resampling.LANCZOS
        ) as g:
            return g.tobytes()
    except Exception:
        return None


def hamming(a: Optional[bytes], b: Optional[bytes]) -> Optional[int]:
    """Byte-wise Hamming distance between perceptual hashes. Returns None
    when either input is None. Two visually-identical screenshots will give
    a small distance (typically < 20 / 256 bytes); a meaningful UI change
    typically flips 80+ bytes."""
    if a is None or b is None or len(a) != len(b):
        return None
    return sum(bin(x ^ y).count("1") for x, y in zip(a, b))


# Threshold: a real click on a button toggles >40 of 256 hash bytes. Below
# that we treat as "no visible change → verify_fail". Calibrated by probe B.
HASH_VERIFY_FAIL_THRESHOLD = 40


def verify_via_diff(before_png: bytes, after_png: bytes) -> tuple[str, int]:
    """Compare before/after screenshots via perceptual hash.

    Returns (verdict_string, hamming_distance). verdict is one of
    ``"yes"`` / ``"no"``. When Pillow isn't available returns ``"n/a"`` so
    the caller can still surface ok=True without verify.
    """
    a = perceptual_hash_png(before_png)
    b = perceptual_hash_png(after_png)
    d = hamming(a, b)
    if d is None:
        return "n/a", -1
    return ("yes" if d >= HASH_VERIFY_FAIL_THRESHOLD else "no"), d


# ---------------------------------------------------------------------------
# Public API: click() / fill()
# ---------------------------------------------------------------------------


def click(
    app: str,
    desc: str,
    *,
    window_title: Optional[str] = None,
    verify_visibility: bool = True,
    self_heal: bool = True,
) -> dict[str, Any]:
    """Locate ``desc`` text inside ``app``'s window via OCR and click it.

    Contract: returns a dict suitable for LayerResult.data with at minimum:
      - ``ok``: bool
      - ``stage``: "ok" / "locate_fail" / "act_fail" / "verify_fail" / "ambiguous"
      - ``via``: "v2_ocr"
      - ``verified``: "yes" | "no" | "n/a" (only meaningful when ok=True)
      - ``elapsed_ms``: float

    Pre-capture visibility gate
    ---------------------------
    When ``verify_visibility=True`` (default), the first thing this does is
    ``resolve_wid`` + ``occlusion_check``. If the target is occluded and
    ``self_heal=True``, ``activate_app`` is attempted once and occlusion is
    re-checked; if still occluded we return ``stage="locate_fail",
    reason="target_occluded"`` with the coverage_ratio and occluder list
    instead of silently OCR-ing the wrong window. Set ``verify_visibility=False``
    for unit tests or when the caller has already guaranteed visibility.
    """
    t0 = time.perf_counter()
    data = LayerResultData(via="v2_ocr")

    # ---- Pre-capture visibility gate -------------------------------------
    if verify_visibility:
        win = resolve_wid(app, window_title)
        if win is None:
            return {
                "ok": False,
                "stage": "locate_fail",
                "via": "v2_ocr",
                "elapsed_ms": (time.perf_counter() - t0) * 1000,
                "reason": "target_not_visible",
            }
        occ = occlusion_check(win["wid"], win["bounds"])
        if occ["occluded"]:
            healed = None
            if self_heal:
                healed = _activate_and_recheck(app, window_title)
            if healed is None:
                return {
                    "ok": False,
                    "stage": "locate_fail",
                    "via": "v2_ocr",
                    "elapsed_ms": (time.perf_counter() - t0) * 1000,
                    "reason": "target_occluded",
                    "coverage_ratio": occ["coverage_ratio"],
                    "occluders": occ["occluders"],
                    "self_heal_attempted": self_heal,
                }

    # Capture
    try:
        before_png, meta_pre = capture_window(app, window_title)
    except RuntimeError as e:
        return {
            "ok": False,
            "stage": "locate_fail",
            "via": "v2_ocr",
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "reason": str(e),
        }

    # Downsample (if needed)
    ocr_input, scale = maybe_downsample(before_png)
    data.scale = scale
    image_w_px = meta_pre["image_w_px"] // scale
    image_h_px = meta_pre["image_h_px"] // scale

    # OCR
    hits, _, _ = ocr(ocr_input)
    data.ocr_n_results = len(hits)

    # Match
    chosen, ambiguous = match(hits, desc)
    data.ocr_n_candidates = 0 if chosen is None else 1 + len(ambiguous)
    if chosen is None and ambiguous:
        data.ambiguous = [
            {
                "text": h.text,
                "confidence": round(h.confidence, 2),
                "bbox_center_xy_px": (
                    round(_center_xy_with_scale(h, image_w_px, image_h_px, scale)[0]),
                    round(_center_xy_with_scale(h, image_w_px, image_h_px, scale)[1]),
                ),
            }
            for h in ambiguous[:6]
        ]
        return {
            "ok": False,
            "stage": "ambiguous",
            "via": "v2_ocr",
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "ambiguous": data.ambiguous,
            "ocr_n_results": data.ocr_n_candidates,
        }
    if chosen is None:
        return {
            "ok": False,
            "stage": "locate_fail",
            "via": "v2_ocr",
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "ocr_n_results": len(hits),
            "desc": desc,
        }

    # Convert OCR-located center to original-screen-pixel coords
    chosen.image_w_px = image_w_px
    chosen.image_h_px = image_h_px
    chosen.scale = scale
    cx, cy = chosen.center_screen_px()
    # Add the window origin offset (capture was window-relative; OCR is in
    # image-local coords). Divide by the real backing scale factor (image px
    # → screen points) — historically hardcoded /2, which silently mis-clicks
    # on a non-Retina external display (scale 1.0). Default 2.0 preserves the
    # old Retina behavior when the capture meta didn't record it.
    backing = float(meta_pre.get("backing_scale", 2.0) or 2.0)
    cx_screen = meta_pre["bounds"]["X"] + cx / backing
    cy_screen = meta_pre["bounds"]["Y"] + cy / backing

    data.chosen = {
        "text": chosen.text,
        "confidence": round(chosen.confidence, 2),
        "exact": chosen.exact,
        "click_xy": (round(cx_screen, 1), round(cy_screen, 1)),
    }

    # Click
    if not click_at(cx_screen, cy_screen):
        return {
            "ok": False,
            "stage": "act_fail",
            "via": "v2_ocr",
            "elapsed_ms": (time.perf_counter() - t0) * 1000,
            "chosen": data.chosen,
        }

    # Verify via screen diff
    time.sleep(DEFAULT_CLICK_VERIFY_POLL_MS / 1000.0)
    try:
        after_png, _meta_post = capture_window(app, window_title)
        verdict, distance = verify_via_diff(before_png, after_png)
    except Exception:
        verdict = "n/a"
        distance = -1

    data.ssim = float(distance) if distance >= 0 else None
    data.elapsed_ms = (time.perf_counter() - t0) * 1000

    if verdict == "no":
        # No visible change → likely stub no-op or click missed.
        return {
            "ok": True,
            "verified": "no",
            "stage": "verify_fail",
            "via": "v2_ocr",
            "elapsed_ms": data.elapsed_ms,
            "chosen": data.chosen,
            "hash_distance": data.ssim,
        }
    return {
        "ok": True,
        "verified": verdict,
        "stage": "ok",
        "via": "v2_ocr",
        "elapsed_ms": data.elapsed_ms,
        "chosen": data.chosen,
        "hash_distance": data.ssim,
    }


def fill(
    app: str,
    desc: str,
    text: str,
    *,
    window_title: Optional[str] = None,
    verify_visibility: bool = True,
    self_heal: bool = True,
) -> dict[str, Any]:
    """Fill an input located by its existing visible text (placeholder or
    pre-filled value). Empty no-placeholder inputs can't be OCR-located
    → ``locate_fail``; the caller (router) should route to AXValue or V4.
    """
    # In v2.0 we implement fill as "click the target desc, then type via
    # Quartz keyboard". For minimum useful scope, we reuse click() to
    # locate + verify the click, then send keystrokes. This won't work
    # for the "empty no-placeholder input" case — caller must declare
    # locate_fail accordingly.
    click_result = click(
        app, desc,
        window_title=window_title,
        verify_visibility=verify_visibility,
        self_heal=self_heal,
    )
    if not click_result.get("ok"):
        return click_result

    # Type via Quartz keyboard (reuse _keyboard).
    try:
        from qcu.layers._keyboard import type_text

        ok = type_text(text)
    except Exception:
        ok = False

    return {
        "ok": ok,
        "verified": click_result.get("verified", "n/a"),
        "stage": "ok" if ok else "act_fail",
        "via": "v2_ocr",
        "chosen": click_result.get("chosen"),
        "elapsed_ms": click_result.get("elapsed_ms", 0),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _center_xy_with_scale(
    hit: OcrHit, image_w_px: int, image_h_px: int, scale: int
) -> tuple[float, float]:
    """Compute center in *screen* (not image) pixel coords with downsample
    scale applied. Used for the ``ambiguous`` report so the LLM sees coords
    in the same frame CGEventPost consumes.
    """
    cx_norm = hit.bbox_origin_x_norm + hit.bbox_width_norm / 2
    cy_norm = 1.0 - (
        hit.bbox_origin_y_norm + hit.bbox_height_norm
    ) + hit.bbox_height_norm / 2
    return (
        cx_norm * image_w_px * scale,
        cy_norm * image_h_px * scale,
    )
