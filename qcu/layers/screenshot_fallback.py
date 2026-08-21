"""Screenshot fallback layer — last-resort vision-based control surface.

When to use (decided by the Router, never by the LLM directly):

1. a11y tree has no ref for the target.
2. Target sits on a ``<canvas>`` or WebGL surface.
3. Action is in the critical list and needs visual confirmation.
4. Desktop context with no AX permission.
5. Generic fallback when nothing else matched.

The MVP ships only the interface. Implementations:

- **web**: borrow the web_a11y browser handle, call ``page.screenshot()``,
  optionally invoke a registered GroundingModel to convert the image to
  coords/refs, then click via ``page.mouse.click(x, y)``.
- **desktop**: borrow the desktop_ax session for ``CGWindowListCreateImage``;
  same grounding path.
"""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from qcu.common.types import Action, Element, LayerResult, Observation, Rect
from qcu.grounding import get as get_grounding
from qcu.layers.base import Layer
from qcu.layers.runtime import register


@register("screenshot_fallback")
class ScreenshotFallbackLayer(Layer):
    name = "screenshot_fallback"

    def __init__(self) -> None:
        self._artifacts_dir: Optional[str] = None

    def _artifacts_path(self) -> str:
        if self._artifacts_dir is None:
            self._artifacts_dir = os.environ.get(
                "QCU_ARTIFACTS_DIR",
                os.path.join(os.path.expanduser("~"), ".qcu", "artifacts"),
            )
            os.makedirs(self._artifacts_dir, exist_ok=True)
        return self._artifacts_dir

    # ------------------------------------------------------------------
    # Observe: snapshot the screen, optionally run grounding.
    # ------------------------------------------------------------------

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:  # noqa: ARG002
        # ``**options`` absorbs ``full_text`` / ``text_limit`` / ``compact`` etc.
        # so this layer honors the Layer protocol (base.py) instead of raising
        # TypeError. A screenshot has no text to truncate, so the options are
        # intentionally ignored — but the signature must accept them.
        path = self._capture()
        if path is None:
            return Observation(
                context="unknown",
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "captured": False,
                    "reason": "no backend browser/desktop session to screenshot",
                },
            )
        return Observation(
            context="unknown",
            elements=[],  # grounding-dependent; the LLM can read screenshot_path directly
            routing_meta={"layer": self.name, "captured": True, "path": path},
            screenshot_path=path,
        )

    def _capture(self) -> Optional[str]:
        # Try web_a11y browser first.
        try:
            from qcu.layers.runtime import get_layer

            web = get_layer("web_a11y")
            # A screenshot must capture the page AS-IS — never silently
            # re-navigate to a stale session.current_url. autoload_last_url=False
            # prevents the "screenshot grabbed an old example.com page" bug.
            web._ensure_browser(autoload_last_url=False)  # type: ignore[attr-defined]
            loop = web._loop  # type: ignore[attr-defined]
            page = web._page  # type: ignore[attr-defined]
            if page is not None and loop is not None:
                ts = time.strftime("%Y%m%d-%H%M%S")
                fname = f"screenshot-{ts}.png"
                path = os.path.join(self._artifacts_path(), fname)

                async def _shot() -> None:
                    assert page is not None
                    await page.screenshot(path=path, full_page=False)

                loop.run_until_complete(_shot())
                return path
        except Exception:
            pass

        # Try desktop_ax next.
        try:
            from qcu.layers.runtime import get_layer

            ax = get_layer("desktop_ax")
            if ax.is_trusted(prompt=False):  # type: ignore[attr-defined]
                # desktop_ax._screenshot handles Quartz; give it a real path
                # so the file actually lands on disk (previously this returned
                # None on success, which made observe() report captured=False
                # — contradicting the truth).
                ts = time.strftime("%Y%m%d-%H%M%S")
                fname = f"screenshot-{ts}.png"
                path = os.path.join(self._artifacts_path(), fname)
                result = ax._screenshot(path)  # type: ignore[attr-defined]
                if result.ok and os.path.exists(path):
                    return path
        except Exception:
            pass
        return None

    # ------------------------------------------------------------------
    # Act: try grounding → coords → click.
    # ------------------------------------------------------------------

    def act(self, action: Action) -> LayerResult:
        # Path A: if action carries coords directly, click them.
        if "x" in action.params and "y" in action.params:
            return self._click_xy(float(action.params["x"]), float(action.params["y"]))

        # Path B: if there's a screenshot + registered grounding, ground it.
        grounding_name = action.params.get("grounding")
        query = action.params.get("query") or action.type
        if grounding_name:
            g = get_grounding(grounding_name)
            if g is not None:
                path = self._capture()
                if path is None:
                    return LayerResult(
                        ok=False,
                        layer=self.name,
                        message="could not capture screenshot for grounding",
                    )
                with open(path, "rb") as f:
                    png = f.read()
                res = g.ground(png, query=query)
                if res.coords:
                    x, y = res.coords[0]["x"], res.coords[0]["y"]
                    return self._click_xy(x, y)
                return LayerResult(
                    ok=False,
                    layer=self.name,
                    message=f"grounding model returned no coords for query={query!r}",
                )

        return LayerResult(
            ok=False,
            layer=self.name,
            message=(
                "screenshot_fallback needs either explicit {x,y} or "
                "{grounding: <name>, query: <text>} — see references/api.md"
            ),
        )

    def _click_xy(self, x: float, y: float) -> LayerResult:
        try:
            from qcu.layers.runtime import get_layer

            web = get_layer("web_a11y")
            web._ensure_browser()  # type: ignore[attr-defined]
            loop = web._loop  # type: ignore[attr-defined]
            page = web._page  # type: ignore[attr-defined]
            if page is not None and loop is not None:

                async def _click() -> None:
                    assert page is not None
                    await page.mouse.click(x, y)

                loop.run_until_complete(_click())
                return LayerResult(ok=True, layer=self.name, message=f"click at ({x},{y}) (via web_a11y)")
        except Exception:
            pass

        try:
            from qcu.layers.runtime import get_layer

            ax = get_layer("desktop_ax")
            res = ax._click("click", {"x": x, "y": y})  # type: ignore[attr-defined]
            return res
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"no backend available: {e}")

    def close(self) -> None:
        return None