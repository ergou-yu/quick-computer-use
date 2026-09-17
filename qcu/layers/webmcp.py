"""WebMCP compatibility layer for page-provided ``globalThis.modelContext``.

Availability is discovered on the connected page at runtime. Browser version
or presence of this adapter does not establish that a page offers tools.
This compatibility adapter uses the page's ``globalThis.modelContext`` shim.

Two operational modes:

- **observe()** — probe the page for ``globalThis.modelContext``, try
  ``getTools()`` if available, and return an Observation whose
  ``tools`` field carries the registered tool descriptors.
- **act(type="webmcp_call")** — execute a registered tool with the
  supplied arguments. Wraps ``globalThis.modelContext.executeTool(name, args)``.
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
        self._web: Any = None
        self._session_id: Optional[str] = None
        self._document: Any = None
        self._tools: set[str] = set()

    def _current_web(self):
        from qcu.session import load
        from qcu.layers.runtime import get_layer
        session = load()
        if session is None or session.context != "web":
            raise RuntimeError("WebMCP requires the current web session")
        web = get_layer("web_a11y")
        if web._page is None or web._loop is None or web._page.is_closed():
            raise RuntimeError("WebMCP needs an existing connected page; observe web_a11y first")
        return session, web

    def _bind(self, page: Any, loop: Any) -> None:
        """Bind only to the current session's connected page; never launch one."""
        session, web = self._current_web()
        if page is not web._page or loop is not web._loop:
            raise RuntimeError("WebMCP page differs from the active web target")
        document = loop.run_until_complete(page.evaluate("() => performance.timeOrigin"))
        if document is None:
            raise RuntimeError("cannot identify the current web document")
        self._page, self._loop, self._web = page, loop, web
        self._session_id, self._document = session.session_id, document
        self._tools.clear()
        self._probe = loop.run_until_complete(page.evaluate(_PROBE_JS))

    def _check_binding(self) -> None:
        session, web = self._current_web()
        if (session.session_id != self._session_id or web is not self._web
                or web._page is not self._page or web._loop is not self._loop):
            raise RuntimeError("WebMCP session or page changed; observe tools again")
        document = self._loop.run_until_complete(self._page.evaluate("() => performance.timeOrigin"))
        if document != self._document:
            raise RuntimeError("WebMCP document changed; observe tools again")

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        try:
            _, web = self._current_web()
            self._bind(web._page, web._loop)
        except Exception as exc:
            self._tools.clear()
            return Observation(context="web", elements=[], routing_meta={
                "layer": self.name, "available": False, "reason": "target_unavailable", "error": str(exc)})

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

        self._tools = {tool["name"] for tool in tools if isinstance(tool.get("name"), str)}
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
        from qcu.common.verification import validate_condition
        if action.type != "webmcp_call":
            return LayerResult(False, self.name, "WebMCP only supports webmcp_call", data={"reason": "unsupported"})
        condition = action.params.get("verify")
        try:
            self._check_binding()
            validate_condition(condition)
            if condition and condition.get("ref"):
                ref = condition["ref"]
                if ref not in self._web._last_refs or not self._web._ref_current(ref):
                    raise ValueError("verification ref is stale")
        except Exception as exc:
            return LayerResult(False, self.name, str(exc), data={"reason": "target_or_verification_invalid"}, dispatch_state="not_sent")
        tool = action.params.get("tool") or action.params.get("name")
        args = action.params.get("args") or action.params.get("arguments") or {}
        if not isinstance(tool, str) or tool not in self._tools:
            return LayerResult(False, self.name, "tool was not discovered in the current target; observe tools again",
                               data={"reason": "unknown_tool"}, dispatch_state="not_sent")
        js = (
            "async () => {\n"
            "  const tool = " + json.dumps(tool) + ";\n"
            f"  const args = {json.dumps(args)};\n"
            "  if (!globalThis.modelContext || typeof globalThis.modelContext.executeTool !== 'function') {\n"
            "    return { ok: false, dispatched: false, error: 'modelContext.executeTool not available' };\n"
            "  }\n"
            "  try {\n"
            "    const out = await globalThis.modelContext.executeTool(tool, args);\n"
            "    return { ok: true, dispatched: true, result: out };\n"
            "  } catch (e) { return { ok: false, dispatched: null, error: String(e) }; }\n"
            "}"
        )
        assert self._loop is not None
        try:
            res = self._loop.run_until_complete(self._page.evaluate(js))
        except Exception as e:
            return LayerResult(False, self.name, f"{type(e).__name__}: {e}",
                               data={"reason": "outcome_unknown"}, dispatch_state="unknown")
        if isinstance(res, dict) and res.get("ok"):
            result = LayerResult(True, self.name, f"tool {tool} call returned", data={"result": res.get("result")},
                                 dispatch_state="sent", outcome="unknown")
        else:
            not_sent = isinstance(res, dict) and res.get("dispatched") is False
            return LayerResult(False, self.name, str(res.get("error") if isinstance(res, dict) else "invalid tool reply"),
                data={"reason": "unsupported" if not_sent else "outcome_unknown"},
                dispatch_state="not_sent" if not_sent else "unknown")
        if condition:
            try:
                evidence = self._loop.run_until_complete(self._web._verify_condition(condition))
            except Exception as exc:
                evidence = {"verified": False, "reason": "verification_unavailable", "error": str(exc)}
            result.data["verification"] = evidence
            result.outcome = "verified" if evidence.get("verified") else "unknown"
            result.ok = result.outcome == "verified"
            if not result.ok:
                result.data["reason"] = "outcome_unknown"
        return result

    def close(self) -> None:
        self._tools.clear()
        self._web = None
        self._document = None
        self._session_id = None
        self._probe = None
        self._page = None
        self._loop = None