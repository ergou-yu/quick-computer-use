"""WebMCP layer — detects and uses ``navigator.modelContext``.

Status (mid-2026): the W3C proposal exists at
``https://github.com/webmachinelearning/webmcp`` (Microsoft + Google) but is
**not yet shipped in any production browser**. ``navigator.modelContext``
will not exist in current Chromium builds, so this layer almost always
falls back to a11y. The infrastructure is here so future sites will
benefit automatically — and so we can prove the detection works against
the polyfill.

Two operational modes:

- **observe()** — probe the page for ``document.modelContext``, try
  ``getTools()`` if available, and return an Observation whose
  ``tools`` field carries the registered tool descriptors.
- **act(type="webmcp_call")** — execute a registered tool with the
  supplied arguments. Wraps ``document.modelContext.executeTool(name, args)``.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from qcu.common.types import Action, LayerResult, Observation
from qcu.layers.base import Layer
from qcu.layers.runtime import register


_PROBE_JS = """() => {
    const hasMC = typeof globalThis.modelContext !== 'undefined';
    if (!hasMC) return { available: false, reason: 'navigator.modelContext undefined' };
    const mc = globalThis.modelContext;
    const hasGetTools = typeof mc.getTools === 'function';
    const hasExecute = typeof mc.executeTool === 'function';
    return {
        available: hasGetTools,
        has_execute: hasExecute,
        reason: hasGetTools ? 'ok' : 'modelContext present but getTools missing',
    };
}"""


_GET_TOOLS_JS = """async () => {
    if (!globalThis.modelContext || typeof globalThis.modelContext.getTools !== 'function') return [];
    try {
        const tools = await globalThis.modelContext.getTools();
        return (tools || []).map(t => ({
            name: t.name,
            description: t.description,
            input_schema: t.inputSchema,
        }));
    } catch (e) { return []; }
}"""


@register("webmcp")
class WebMCPLayer(Layer):
    name = "webmcp"

    def __init__(self) -> None:
        self._probe: Optional[dict[str, Any]] = None
        self._page: Any = None
        self._loop: Any = None

    def _bind(self, page: Any, loop: Any) -> None:
        """Bind to a Playwright page; the web_a11y layer holds the browser."""
        self._page = page
        self._loop = loop
        if self._probe is None and page is not None:
            try:
                self._probe = self._loop.run_until_complete(page.evaluate(_PROBE_JS))
            except Exception as e:  # noqa: BLE001
                self._probe = {"available": False, "reason": f"probe failed: {e}"}

    def observe(self, max_depth: int = 8) -> Observation:  # noqa: ARG002
        # The web_a11y layer must have run first; we borrow its page handle.
        if self._page is None:
            # Try to get the singleton browser from web_a11y via runtime.
            try:
                from qcu.layers.runtime import get_layer

                web = get_layer("web_a11y")
                web._ensure_browser()  # type: ignore[attr-defined]
                self._bind(web._page, web._loop)  # type: ignore[attr-defined]
            except Exception as e:  # noqa: BLE001
                return Observation(
                    context="web",
                    elements=[],
                    routing_meta={
                        "layer": self.name,
                        "error": f"could not borrow page from web_a11y: {e}",
                    },
                )

        if self._probe is None or not self._probe.get("available"):
            return Observation(
                context="web",
                elements=[],
                routing_meta={
                    "layer": self.name,
                    "available": False,
                    "reason": (self._probe or {}).get("reason", "not probed"),
                },
            )

        # Tools available — fetch the tool list.
        tools: list[dict[str, Any]] = []
        try:
            assert self._loop is not None and self._page is not None
            tools = self._loop.run_until_complete(self._page.evaluate(_GET_TOOLS_JS))
        except Exception:
            tools = []

        return Observation(
            context="web",
            url_or_app=self._page.url if self._page is not None else None,
            elements=[],
            routing_meta={
                "layer": self.name,
                "available": True,
                "tool_count": len(tools),
            },
            tools=tools,
            raw_tree="\n".join(
                f"- {t.get('name')}: {t.get('description')}" for t in tools
            )
            or None,
        )

    def act(self, action: Action) -> LayerResult:
        if self._page is None:
            return LayerResult(
                ok=False,
                layer=self.name,
                message="webmcp not bound to a page; observe via web_a11y first",
            )
        if action.type != "webmcp_call":
            return LayerResult(
                ok=False,
                layer=self.name,
                message=f"webmcp layer only handles 'webmcp_call', got {action.type!r}",
            )
        tool = action.params.get("tool") or action.params.get("name")
        args = action.params.get("args") or action.params.get("arguments") or {}
        if not tool:
            return LayerResult(ok=False, layer=self.name, message="webmcp_call needs params.tool")
        js = (
            "async () => {\n"
            "  const tool = " + json.dumps(tool) + ";\n"
            f"  const args = {json.dumps(args)};\n"
            "  if (!globalThis.modelContext || typeof globalThis.modelContext.executeTool !== 'function') {\n"
            "    return { ok: false, error: 'modelContext.executeTool not available' };\n"
            "  }\n"
            "  try {\n"
            "    const out = await globalThis.modelContext.executeTool(tool, args);\n"
            "    return { ok: true, result: out };\n"
            "  } catch (e) { return { ok: false, error: String(e) }; }\n"
            "}"
        )
        assert self._loop is not None
        try:
            res = self._loop.run_until_complete(self._page.evaluate(js))
        except Exception as e:  # noqa: BLE001
            return LayerResult(ok=False, layer=self.name, message=f"{type(e).__name__}: {e}")
        if isinstance(res, dict) and res.get("ok"):
            return LayerResult(
                ok=True,
                layer=self.name,
                message=f"tool {tool} executed",
                data={"result": res.get("result")},
            )
        err = (res or {}).get("error") if isinstance(res, dict) else "unknown"
        return LayerResult(ok=False, layer=self.name, message=str(err))

    def close(self) -> None:
        self._probe = None
        self._page = None
        self._loop = None