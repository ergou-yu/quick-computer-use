"""Optional local PNG template matching; imported only on explicit request."""
from __future__ import annotations

import math
from pathlib import Path
import struct

from qcu.grounding.stub import GroundingResult


class TemplateError(ValueError):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class TemplateGrounder:
    name = "template"

    def __init__(self, *, template_path=None, threshold=.94, scales=None, margin=.03):
        self.path = template_path
        self.threshold, self.margin = float(threshold), float(margin)
        self.scales = [1.] if scales is None else scales
        if (not math.isfinite(self.threshold) or not .5 <= self.threshold <= 1
                or not math.isfinite(self.margin) or not 0 <= self.margin <= .2):
            raise TemplateError("invalid_template", "threshold needs .5..1 and margin 0...2")
        if (not isinstance(self.scales, list) or not 1 <= len(self.scales) <= 9
                or any(type(s) not in (float, int) or not math.isfinite(s) or not .25 <= s <= 4 for s in self.scales)):
            raise TemplateError("invalid_template", "template_scales needs 1..9 finite scales in .25..4")
        self._template = None

    @classmethod
    def from_options(cls, options):
        return cls(template_path=options.get("template_path"), threshold=options.get("template_threshold", .94),
                   scales=options.get("template_scales"), margin=options.get("template_margin", .03))

    def _decode(self, data, *, limit, require_opaque=False):
        if (len(data) > 32 * 1024 * 1024 or len(data) < 33 or data[:8] != b"\x89PNG\r\n\x1a\n"
                or data[12:16] != b"IHDR"):
            raise TemplateError("invalid_template", "template matching requires a bounded PNG image")
        width, height = struct.unpack(">II", data[16:24])
        if width < 3 or height < 3 or width * height > limit:
            raise TemplateError("invalid_template", "PNG dimensions exceed the matching limit")
        import cv2
        import numpy as np
        image = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
        if image is None or image.dtype != np.uint8:
            raise TemplateError("invalid_template", "PNG must decode to an 8-bit image")
        if image.ndim == 2:
            image = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        if image.shape[2] == 4 and require_opaque:
            if not np.all(image[:, :, 3] == 255):
                raise TemplateError("invalid_template", "use an opaque screenshot crop; transparent templates are unsupported")
            image = image[:, :, :3]
        return image

    @staticmethod
    def _overlap(a, b):
        x = max(0, min(a[0]+a[2], b[0]+b[2])-max(a[0], b[0]))
        y = max(0, min(a[1]+a[3], b[1]+b[3])-max(a[1], b[1]))
        area = x*y
        return area / (a[2]*a[3]+b[2]*b[3]-area)

    def ground(self, screenshot_bytes, *, query="", max_results=5):
        try:
            import cv2
            import numpy as np
        except ImportError:
            raise TemplateError("missing_capability", 'install qcu[vision] to use grounding:"template"') from None
        if self._template is None:
            if not isinstance(self.path, str) or not self.path:
                raise TemplateError("invalid_template", "template_path must name an opaque PNG crop")
            path = Path(self.path)
            try:
                with path.open('rb') as stream:
                    data = stream.read(8 * 1024 * 1024 + 1)
            except OSError as exc:
                raise TemplateError("invalid_template", str(exc)) from None
            if len(data) > 8 * 1024 * 1024:
                raise TemplateError("invalid_template", "template file exceeds 8 MiB")
            self._template = self._decode(data, limit=1024*1024, require_opaque=True)
            # Spatial variance per channel: a solid red icon is still constant.
            if float(np.max(np.std(self._template.astype(float), axis=(0, 1)))) < 5:
                raise TemplateError("invalid_template", "template needs spatial detail; a flat color cannot locate a target")
        image = self._decode(screenshot_bytes, limit=16*1024*1024)
        transparent = None
        if image.shape[2] == 4:
            # Mutter captures may have transparent rounded frame corners.
            # Ignore any candidate touching transparency, rather than matching
            # its arbitrary hidden RGB pixels or rejecting the whole window.
            transparent = cv2.integral((image[:, :, 3] != 255).astype(np.uint8))
            image = image[:, :, :3]
        ih, iw = image.shape[:2]
        th, tw = self._template.shape[:2]
        peaks = []
        for scale in sorted(set(self.scales)):
            w, h = round(tw*scale), round(th*scale)
            if min(w, h) < 3 or w > iw or h > ih:
                continue
            template = cv2.resize(self._template, (w, h), interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            # Resampling can erase the only detail in a very small template.
            if float(np.max(np.std(template.astype(float), axis=(0, 1)))) < 5:
                continue
            scores = cv2.matchTemplate(image, template, cv2.TM_CCOEFF_NORMED)
            scores[~np.isfinite(scores)] = -1
            if transparent is not None:
                covered = transparent[h:, w:]-transparent[:-h, w:]-transparent[h:, :-w]+transparent[:-h, :-w]
                scores[covered != 0] = -1
            for _ in range(6):
                _, score, _, (x, y) = cv2.minMaxLoc(scores)
                if score < self.threshold-self.margin:
                    break
                peaks.append(dict(x=x+w/2, y=y+h/2, confidence=float(score), bbox=[x, y, w, h], scale=scale))
                scores[max(0,y-h//2):y+h//2+1, max(0,x-w//2):x+w//2+1] = -1
        distinct = []
        for peak in sorted(peaks, key=lambda p: p["confidence"], reverse=True):
            if not any(self._overlap(peak["bbox"], p["bbox"]) >= .3 for p in distinct):
                distinct.append(peak)
        accepted = []
        if distinct and distinct[0]["confidence"] >= self.threshold:
            accepted = [p for p in distinct if p["confidence"] >= self.threshold
                        or p["confidence"] >= distinct[0]["confidence"]-self.margin]
        # Always retain a second candidate so max_results=1 cannot hide ambiguity.
        return GroundingResult(coords=accepted[:max(2, min(int(max_results), 6))], refs=[],
                               raw=dict(model=self.name, threshold=self.threshold, margin=self.margin,
                                        image_size=[iw, ih], best_confidence=distinct[0]["confidence"] if distinct else None))
