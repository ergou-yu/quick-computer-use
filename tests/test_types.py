"""Tests for ``qcu.common.types`` — Observation/Action/Element round-trip."""

from __future__ import annotations

import json

from qcu.common.types import (
    ACTION_TYPES,
    Action,
    DEFAULT_CRITICAL_ACTIONS,
    Element,
    LayerResult,
    Observation,
    Rect,
)


def test_rect_center():
    r = Rect(x=10, y=20, width=30, height=40)
    assert r.cx == 25.0
    assert r.cy == 40.0


def test_element_to_dict():
    e = Element(
        ref="ref_7",
        role="button",
        name="Submit",
        value=None,
        enabled=True,
        focused=False,
        bounds=Rect(0, 0, 100, 30),
        backend_id="42",
        properties={"raw_role": "button"},
    )
    d = e.to_dict()
    assert d["ref"] == "ref_7"
    assert d["role"] == "button"
    assert d["bounds"] == {"x": 0.0, "y": 0.0, "width": 100.0, "height": 30.0}
    assert d["backend_id"] == "42"


def test_observation_json_roundtrip():
    obs = Observation(
        context="web",
        url_or_app="https://example.com",
        title="Example",
        elements=[
            Element(ref="ref_1", role="button", name="OK", bounds=Rect(0, 0, 10, 10)),
            Element(ref="ref_2", role="link", name="More"),
        ],
        routing_meta={"layer": "web_a11y"},
        raw_tree="[ref_1] button 'OK'\n[ref_2] link 'More'",
        tools=[{"name": "do_thing", "description": "do it"}],
    )
    j = obs.to_json()
    parsed = json.loads(j)
    assert parsed["context"] == "web"
    assert len(parsed["elements"]) == 2
    assert parsed["elements"][0]["bounds"]["width"] == 10.0
    assert parsed["tools"][0]["name"] == "do_thing"


def test_observation_find_ref():
    obs = Observation(
        context="web",
        elements=[Element(ref="ref_1", role="button"), Element(ref="ref_2", role="link")],
    )
    assert obs.find_ref("ref_1") is not None
    assert obs.find_ref("nope") is None


def test_action_from_json_happy():
    a = Action.from_json('{"type": "click", "params": {"ref": "ref_42"}}')
    assert a.type == "click"
    assert a.params == {"ref": "ref_42"}


def test_action_from_json_bad_type():
    try:
        Action.from_json('["not", "a", "dict"]')
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError for non-object JSON")


def test_action_from_json_missing_type():
    try:
        Action.from_json('{"params": {}}')
    except ValueError:
        pass
    else:
        raise AssertionError("expected ValueError when 'type' missing")


def test_action_to_json_roundtrip():
    a = Action(type="type", params={"text": "hello world"})
    parsed = json.loads(a.to_json())
    assert parsed == {"type": "type", "params": {"text": "hello world"}}


def test_layer_result_dict():
    r = LayerResult(ok=True, layer="web_a11y", message="clicked ref_42")
    d = r.to_dict()
    assert d["ok"] is True
    assert d["layer"] == "web_a11y"
    assert d["message"] == "clicked ref_42"


def test_action_types_set():
    # Spot-check the canonical set; nothing should silently disappear.
    for t in ("click", "type", "press_key", "scroll", "navigate", "screenshot", "webmcp_call"):
        assert t in ACTION_TYPES


def test_critical_actions_set():
    assert "submit_payment" in DEFAULT_CRITICAL_ACTIONS
    assert "delete_account" in DEFAULT_CRITICAL_ACTIONS