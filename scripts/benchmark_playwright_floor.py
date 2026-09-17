"""Raw Playwright floor: the identical 6-field form fill + verified submit from
benchmark_ui.py, driven in-process with plain Playwright — no QCU CLI/daemon and
no screenshots. This is the automation lower bound used to attribute QCU
overhead; it does not include model inference and proves no universal speed
advantage.

    python scripts/benchmark_playwright_floor.py --output playwright-floor.json
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
import benchmark_ui as B

from playwright.sync_api import sync_playwright

VALUES = dict(Name="Test 0", Email="qcu@example.invalid", City="上海",
              Company="Local fixture", Role="Tester", Notes="AX / DOM test")


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--rounds", type=int, default=5)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", 0), B.Fixture)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}/static"
    fill_ms, click_ms, total_ms = [], [], []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url)
            for n in range(args.rounds):
                page.goto(url)
                t0 = time.perf_counter()
                for k, v in VALUES.items():
                    t1 = time.perf_counter()
                    page.fill(f'[aria-label="{k}"]', v)
                    fill_ms.append((time.perf_counter() - t1) * 1000)
                t2 = time.perf_counter()
                page.click('button:has-text("Save local fixture")')
                page.wait_for_selector('#result:has-text("Saved 1")')
                click_ms.append((time.perf_counter() - t2) * 1000)
                total_ms.append((time.perf_counter() - t0) * 1000)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        out = dict(n=args.rounds,
                   fill_p50_ms=statistics.median(fill_ms),
                   click_and_verify_p50_ms=statistics.median(click_ms),
                   whole_task_p50_ms=statistics.median(total_ms))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(out, ensure_ascii=False, indent=2))
        print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
