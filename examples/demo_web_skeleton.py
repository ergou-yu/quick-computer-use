"""Demo: bring up a Playwright Chromium and dump an a11y snapshot.

Run from the package root:
    python3 examples/demo_web_skeleton.py [URL]

Requires:
    pip install playwright
    python3 -m playwright install chromium

If URL is omitted, defaults to https://example.com.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qcu.layers.web_a11y import WebA11yLayer  # noqa: E402
from qcu.common.types import Action  # noqa: E402


async def demo(url: str) -> dict[str, Any]:
    layer = WebA11yLayer()
    try:
        layer._ensure_browser()  # type: ignore[attr-defined]
        # Use the layer's own loop.
        from qcu.layers.web_a11y import WebA11yLayer as _L

        loop = layer._loop  # type: ignore[attr-defined]
        page = layer._page  # type: ignore[attr-defined]

        async def goto() -> None:
            assert page is not None
            await page.goto(url, wait_until="domcontentloaded")

        loop.run_until_complete(goto())
        obs = layer.observe(max_depth=8)
        print(f"\n# {url} — {len(obs.elements)} interactive elements\n")
        for el in obs.elements[:25]:
            bounds = (
                f"@({el.bounds.x:.0f},{el.bounds.y:.0f},{el.bounds.width:.0f}x{el.bounds.height:.0f})"
                if el.bounds
                else "@(?)"
            )
            name = repr(el.name) if el.name else ""
            print(f"  {el.ref:8s}  {el.role:14s}  {bounds:32s}  {name}")
        if len(obs.elements) > 25:
            print(f"  ... and {len(obs.elements) - 25} more")
        print("\n# raw_tree (truncated):\n")
        if obs.raw_tree:
            for line in obs.raw_tree.splitlines()[:30]:
                print(f"  {line}")
            if len(obs.raw_tree.splitlines()) > 30:
                print(f"  ... ({len(obs.raw_tree.splitlines()) - 30} more lines)")
        return obs.to_dict()
    finally:
        layer.close()


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else "https://example.com"
    asyncio.run(demo(url))


if __name__ == "__main__":
    main()