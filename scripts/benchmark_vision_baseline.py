"""Vision-loop local baseline: same fixture page as benchmark_ui.py, headful window.

Measures the local floor of the screenshot paradigm on this machine: full-screen
Quartz capture (+PNG encode) and Apple Vision OCR (accurate and fast). Only
timings, byte sizes and recognized-string counts are reported. Requires macOS
Screen Recording permission for the Python process; cloud VLM latency is NOT
included — no universal speed advantage is claimed.

Run with the Python environment used by QCU:
    python scripts/benchmark_vision_baseline.py --output vision-baseline.json
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import benchmark_ui as B  # identical fixture HTML + handler

try:
    import Quartz
    from Foundation import NSMutableData, NSURL
    import Vision
    from Vision import VNImageRequestHandler, VNRecognizeTextRequest
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"macOS frameworks required (pip install '.[macos]'): {exc}")

from playwright.sync_api import sync_playwright


def png_bytes(cg_image) -> bytes:
    data = NSMutableData.data()
    dest = Quartz.CGImageDestinationCreateWithData(data, "public.png", 1, None)
    Quartz.CGImageDestinationAddImage(dest, cg_image, None)
    Quartz.CGImageDestinationFinalize(dest)
    return bytes(data)


def ocr_png(path: str, level):
    url = NSURL.fileURLWithPath_(path)
    handler = VNImageRequestHandler.alloc().initWithURL_options_(url, None)
    req = VNRecognizeTextRequest.alloc().init()
    req.setRecognitionLevel_(level)
    ok = handler.performRequests_error_([req], None)
    texts = []
    for obs in req.results() or []:
        try:
            cands = obs.topCandidates_(1)
            s = str(cands[0].string()) if cands else None
        except Exception:
            s = None
        if s:
            texts.append(s)
    return bool(ok), texts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    n = args.rounds

    server = ThreadingHTTPServer(("127.0.0.1", 0), B.Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/static"
    result = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False,
                                        args=["--window-size=1280,800", "--window-position=100,100"])
            page = browser.new_page()
            page.goto(url)
            page.wait_for_timeout(800)  # let it settle frontmost

            cap_ms, enc_ms, sizes = [], [], []
            for _ in range(n):
                t0 = time.perf_counter()
                img = Quartz.CGWindowListCreateImage(
                    Quartz.CGRectInfinite, Quartz.kCGWindowListOptionOnScreenOnly,
                    Quartz.kCGNullWindowID, Quartz.kCGWindowImageDefault)
                t1 = time.perf_counter()
                data = png_bytes(img)
                t2 = time.perf_counter()
                shot = Path(args.output).parent / "bench-screen.png"
                shot.write_bytes(data)
                cap_ms.append((t1 - t0) * 1000)
                enc_ms.append((t2 - t1) * 1000)
                sizes.append(len(data))
            result["capture_fullscreen"] = dict(
                n=n, p50_ms=statistics.median(cap_ms),
                encode_p50_ms=statistics.median(enc_ms),
                png_bytes_p50=int(statistics.median(sizes)),
                screen=f"{Quartz.CGImageGetWidth(img)}x{Quartz.CGImageGetHeight(img)}")

            ocr_ms, counts = [], []
            for _ in range(n):
                t0 = time.perf_counter()
                ok, texts = ocr_png(str(shot), Vision.VNRequestTextRecognitionLevelAccurate)
                ocr_ms.append((time.perf_counter() - t0) * 1000)
                counts.append(len(texts))
            result["ocr_vision_accurate"] = dict(n=n, ok=ok, p50_ms=statistics.median(ocr_ms),
                                                 strings_p50=int(statistics.median(counts)))

            url_ = NSURL.fileURLWithPath_(str(shot))
            ocr_fast_ms = []
            for _ in range(n):
                t0 = time.perf_counter()
                handler = VNImageRequestHandler.alloc().initWithURL_options_(url_, None)
                req = VNRecognizeTextRequest.alloc().init()
                req.setRecognitionLevel_(Vision.VNRequestTextRecognitionLevelFast)
                handler.performRequests_error_([req], None)
                ocr_fast_ms.append((time.perf_counter() - t0) * 1000)
            result["ocr_vision_fast"] = dict(n=n, p50_ms=statistics.median(ocr_fast_ms))

            win_ms, win_sizes = [], []
            windows = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID)
            target = next((w for w in windows
                           if "Chrome for Testing" in str(w.get("kCGWindowOwnerName", ""))
                           and w.get("kCGWindowLayer", 1) == 0), None)
            if target:
                b = target.get("kCGWindowBounds")
                rect = Quartz.CGRectMake(b["X"], b["Y"], b["Width"], b["Height"])
                wid = target.get("kCGWindowNumber")
                for _ in range(n):
                    t0 = time.perf_counter()
                    img = Quartz.CGWindowListCreateImage(rect,
                                                        Quartz.kCGWindowListOptionIncludingWindow, wid,
                                                        Quartz.kCGWindowImageDefault)
                    data = png_bytes(img)
                    win_ms.append((time.perf_counter() - t0) * 1000)
                    win_sizes.append(len(data))
                result["capture_window_only"] = dict(n=n, p50_ms=statistics.median(win_ms),
                                                     png_bytes_p50=int(statistics.median(win_sizes)))
            else:
                result["capture_window_only"] = "window-not-found"
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
