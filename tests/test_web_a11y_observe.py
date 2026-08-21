"""Observe-level contract tests for adaptive depth, href, and truncation meta.

These construct a minimal CDP ``Accessibility.getFullAXTree`` response and
assert the contract reported in the 22-issue audit. They run without a browser
by mocking the page/cdp surface and patching session.record_observation.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from qcu.layers import web_a11y as wa


def _ax_props(role: str, **extra: Any) -> list[dict[str, Any]]:
    props = [{"name": "role", "value": {"type": "role", "value": role}}]
    for k, v in extra.items():
        props.append({"name": k, "value": {"type": "string", "value": v}})
    return props


@pytest.mark.asyncio
async def test_observe_extracts_href_and_same_origin(monkeypatch):
    """A link element's properties must include href/absolute_href/same_origin.

    Regression for issue 6: previously links surfaced only role/name/value,
    so an LLM could not tell an internal "X comments" link from an external
    article-title link.
    """
    nodes = [
        {
            "nodeId": "root",
            "parentId": None,
            "childIds": ["c1"],
            "role": {"type": "role", "value": "WebArea"},
            "name": {"value": "Example"},
            "ignored": False,
        },
        {
            "nodeId": "c1",
            "parentId": "root",
            "childIds": [],
            "role": {"type": "role", "value": "link"},
            "name": {"value": "read more"},
            "ignored": False,
            "backendDOMNodeId": 42,
            "properties": _ax_props(
                "link", url="https://example.com/article", target="_self"
            ),
        },
    ]
    page = MagicMock()
    page.url = "https://example.com/post"
    page.title = AsyncMock(return_value="Example")
    page.wait_for_load_state = AsyncMock()
    cdp = MagicMock()
    async def _send(method, params=None):
        if method == "Accessibility.getFullAXTree":
            return {"nodes": nodes}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "o1"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": None}}  # no geometry
        if method == "Runtime.releaseObject":
            return None
        return None
    cdp.send = AsyncMock(side_effect=_send)

    layer = wa.WebA11yLayer()
    layer._page = page
    layer._cdp = cdp

    # Patch session so record_observation doesn't write to disk.
    monkeypatch.setattr("qcu.session.load", lambda: MagicMock(observation_generation=0))
    monkeypatch.setattr("qcu.session.record_observation", lambda *a, **k: None)

    obs = await layer._observe_async(10)
    links = [e for e in obs.elements if e.role == "link"]
    assert links, "expected exactly one link"
    el = links[0]
    assert el.properties.get("href") == "https://example.com/article"
    assert el.properties.get("same_origin") is True  # same host as page url


@pytest.mark.asyncio
async def test_observe_adaptive_deepens_when_refs_sparse(monkeypatch):
    """A shallow request with a large tree but few interactive refs must
    adaptively deepen (issue 3) and report tree_truncated.

    First call returns many text-only nodes; the layer should re-request with
    a deeper budget. We assert the second CDP call used a larger depth.
    """
    shallow_nodes = [
        {
            "nodeId": f"n{i}",
            "parentId": "root" if i else None,
            "childIds": [],
            "role": {"type": "role", "value": "StaticText"},
            "name": {"value": f"line {i}"},
            "ignored": False,
        }
        for i in range(70)
    ]
    shallow_nodes[0]["childIds"] = [f"n{i}" for i in range(1, 70)]

    page = MagicMock()
    page.url = "https://example.com/"
    page.title = AsyncMock(return_value="T")
    page.wait_for_load_state = AsyncMock()
    cdp = MagicMock()
    calls = []

    async def _send(method, params=None):
        if method == "Accessibility.getFullAXTree":
            calls.append(params.get("depth"))
            if len(calls) == 1:
                return {"nodes": shallow_nodes}
            return {"nodes": shallow_nodes[:0]}  # deep call returns empty
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "o"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": None}}
        if method == "Runtime.releaseObject":
            return None
        return None
    cdp.send = AsyncMock(side_effect=_send)

    layer = wa.WebA11yLayer()
    layer._page = page
    layer._cdp = cdp
    monkeypatch.setattr("qcu.session.load", lambda: MagicMock(observation_generation=0))
    monkeypatch.setattr("qcu.session.record_observation", lambda *a, **k: None)

    obs = await layer._observe_async(6)
    assert obs.routing_meta["adaptive_used"] is True
    assert calls[0] == 6
    assert calls[1] >= 14  # deepened
    assert "tree_truncated" in obs.routing_meta


@pytest.mark.asyncio
async def test_observe_reports_text_dropped_and_total_limit(monkeypatch):
    """Structured/text dropping must be reported, not silently lost (issue 7)."""
    # 5 headings + 200 paragraph texts under one root.
    kids: list[str] = []
    nodes = [
        {
            "nodeId": "root", "parentId": None, "childIds": [], "ignored": False,
            "role": {"type": "role", "value": "WebArea"}, "name": {"value": ""},
        }
    ]
    for i in range(5):
        nid = f"h{i}"
        kids.append(nid)
        nodes.append(
            {
                "nodeId": nid, "parentId": "root", "childIds": [], "ignored": False,
                "role": {"type": "role", "value": "heading"},
                "name": {"value": f"Heading {i}"},
            }
        )
    for i in range(200):
        nid = f"t{i}"
        kids.append(nid)
        nodes.append(
            {
                "nodeId": nid, "parentId": "root", "childIds": [], "ignored": False,
                "role": {"type": "role", "value": "StaticText"},
                "name": {"value": f"paragraph {i}"},
            }
        )
    nodes[0]["childIds"] = kids

    page = MagicMock()
    page.url = "https://example.com/"
    page.title = AsyncMock(return_value="T")
    page.wait_for_load_state = AsyncMock()
    cdp = MagicMock()

    async def _send(method, params=None):
        if method == "Accessibility.getFullAXTree":
            return {"nodes": nodes}
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "o"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": None}}
        if method == "Runtime.releaseObject":
            return None
        return None
    cdp.send = AsyncMock(side_effect=_send)

    layer = wa.WebA11yLayer()
    layer._page = page
    layer._cdp = cdp
    monkeypatch.setattr("qcu.session.load", lambda: MagicMock(observation_generation=0))
    monkeypatch.setattr("qcu.session.record_observation", lambda *a, **k: None)

    obs = await layer._observe_async(10, text_limit=20)
    rm = obs.routing_meta
    assert rm["n_structured_lines"] == 5
    assert rm["n_text_lines"] == 20
    assert rm["text_dropped"] == 180
    assert rm["tree_truncated"] is True
