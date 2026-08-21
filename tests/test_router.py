"""Tests for ``qcu.router`` — feed mock features, assert layer chosen."""

from __future__ import annotations

from qcu.common.types import Action, Observation, Element
from qcu.router.classifier import classify
from qcu.router.features import extract


# ---------------------------------------------------------------------------
# Helper: build a small Observation
# ---------------------------------------------------------------------------


def _web_obs(elements=None, tools=None):
    return Observation(
        context="web",
        url_or_app="https://example.com",
        title="Example",
        elements=elements or [Element(ref="ref_1", role="button", name="OK")],
        tools=tools or [],
    )


def _desktop_obs(elements=None):
    return Observation(
        context="desktop",
        url_or_app="TextEdit",
        title="TextEdit",
        elements=elements or [Element(ref="ref_1", role="button", name="OK")],
    )


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


def test_extract_basic_web():
    feats = extract(_web_obs())
    assert feats["context"] == "web"
    assert feats["n_elements"] == 1
    assert feats["n_interactive"] == 1
    assert feats["has_canvas"] is False
    assert feats["has_webmcp"] is False


def test_extract_canvas_detected():
    obs = _web_obs(
        elements=[
            Element(ref="ref_1", role="canvas"),
            Element(ref="ref_2", role="button", name="OK"),
        ]
    )
    feats = extract(obs)
    assert feats["has_canvas"] is True


def test_extract_webmcp_detected():
    obs = _web_obs(tools=[{"name": "submit_order"}])
    feats = extract(obs)
    assert feats["has_webmcp"] is True
    assert "submit_order" in feats["webmcp_tool_names"]


def test_extract_with_action_target_in_a11y():
    obs = _web_obs()
    action = Action(type="click", params={"ref": "ref_1"})
    feats = extract(obs, last_action=action)
    assert feats["target_in_a11y"] is True
    assert feats["has_canvas_target"] is False


def test_extract_with_action_target_not_in_a11y_and_canvas():
    obs = _web_obs(
        elements=[
            Element(ref="ref_1", role="canvas"),
            Element(ref="ref_2", role="button", name="OK"),
        ]
    )
    action = Action(type="click", params={"ref": "ref_99"})  # unknown ref
    feats = extract(obs, last_action=action)
    assert feats["target_in_a11y"] is False
    assert feats["has_canvas"] is True
    assert feats["has_canvas_target"] is True


def test_extract_critical_action():
    obs = _web_obs()
    action = Action(type="submit_payment", params={"ref": "ref_1"})
    feats = extract(obs, last_action=action)
    assert feats["action_in_critical_list"] is True
    assert feats["needs_visual_confirm"] is True


def test_extract_webmcp_action_match():
    obs = _web_obs(tools=[{"name": "filter_products"}])
    action = Action(type="webmcp_call", params={"tool": "filter_products", "args": {}})
    feats = extract(obs, last_action=action)
    assert feats["action_in_webmcp_tools"] is True


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classify_web_picks_web_a11y_by_default():
    feats = extract(_web_obs())
    d = classify(feats)
    assert d.layer == "web_a11y"


def test_classify_web_with_target_picks_web_a11y():
    feats = extract(_web_obs(), last_action=Action(type="click", params={"ref": "ref_1"}))
    d = classify(feats, last_action=Action(type="click", params={"ref": "ref_1"}))
    assert d.layer == "web_a11y"


def test_classify_critical_action_picks_screenshot():
    feats = extract(_web_obs(), last_action=Action(type="submit_payment", params={"ref": "ref_1"}))
    d = classify(feats, last_action=Action(type="submit_payment", params={"ref": "ref_1"}))
    assert d.layer == "screenshot_fallback"
    assert d.priority == 95  # visual_confirm rule


def test_classify_canvas_target_picks_screenshot():
    obs = _web_obs(
        elements=[
            Element(ref="ref_1", role="canvas"),
            Element(ref="ref_2", role="button", name="OK"),
        ]
    )
    action = Action(type="click", params={"ref": "ref_99"})
    feats = extract(obs, last_action=action)
    d = classify(feats, last_action=action)
    assert d.layer == "screenshot_fallback"
    assert d.priority == 100  # canvas_target wins over visual_confirm


def test_classify_webmcp_picks_webmcp():
    obs = _web_obs(tools=[{"name": "submit_order"}])
    action = Action(type="webmcp_call", params={"tool": "submit_order", "args": {}})
    feats = extract(obs, last_action=action)
    d = classify(feats, last_action=action)
    assert d.layer == "webmcp"


def test_classify_desktop_with_ax_picks_desktop_ax():
    obs = _desktop_obs()
    feats = extract(obs)
    feats["ax_trusted"] = True
    d = classify(feats)
    assert d.layer == "desktop_ax"


def test_classify_desktop_no_ax_picks_apple_events(monkeypatch):
    # When macOS AX permission is missing, the router must NOT degrade to
    # screenshot. It should pick desktop_appleevents (the zero-toggle Apple
    # Events fallback) when osascript is available. Mock both probes so the
    # decision is deterministic on machines that have granted AX/Apple Events.
    from qcu.router import rules
    monkeypatch.setattr(rules, "_ax_actually_trusted", lambda: False)
    monkeypatch.setattr(rules, "_appleevents_available", lambda: True)
    obs = _desktop_obs()
    feats = extract(obs)
    feats["ax_trusted"] = False
    d = classify(feats)
    assert d.layer == "desktop_appleevents"


def test_classify_desktop_no_ax_no_apple_events_picks_screenshot(monkeypatch):
    # Truly last-resort: only when BOTH AX and Apple Events are unavailable
    # does the router fall through to screenshot_fallback.
    from qcu.router import rules
    monkeypatch.setattr(rules, "_ax_actually_trusted", lambda: False)
    monkeypatch.setattr(rules, "_appleevents_available", lambda: False)
    obs = _desktop_obs()
    feats = extract(obs)
    feats["ax_trusted"] = False
    d = classify(feats)
    assert d.layer == "screenshot_fallback"


def test_classify_desktop_with_ax_never_falls_back_even_without_feature(monkeypatch):
    """When AX is genuinely available, a desktop task must route to desktop_ax
    even if no feature populated ax_trusted — never degrade to screenshot."""
    from qcu.router import rules
    monkeypatch.setattr(rules, "_ax_actually_trusted", lambda: True)
    obs = _desktop_obs()
    feats = extract(obs)
    feats.pop("ax_trusted", None)  # not populated, as in real CLI usage
    d = classify(feats)
    assert d.layer == "desktop_ax"


def test_classify_web_target_missing_picks_screenshot():
    obs = _web_obs()
    action = Action(type="click", params={"ref": "ref_missing"})
    feats = extract(obs, last_action=action)
    d = classify(feats, last_action=action)
    assert d.layer == "screenshot_fallback"
    assert d.rule_name == "web_no_target"


def test_classify_no_features_picks_screenshot_default():
    d = classify({})
    assert d.layer == "screenshot_fallback"
    # Default rule fires at priority 10 with rule_name "default".
    assert d.rule_name == "default"
    assert d.priority == 10


def test_classify_includes_alternatives():
    feats = extract(_web_obs())
    d = classify(feats)
    assert d.alternatives  # at least one
    layers = {a["layer"] for a in d.alternatives}
    assert "screenshot_fallback" in layers or "web_a11y" in layers


def test_classify_includes_features_for_data_collection():
    feats = extract(_web_obs())
    d = classify(feats)
    assert d.features.get("context") == "web"


# ---------------------------------------------------------------------------
# Web-native actions (navigate/scroll/press_key/...) must use web_a11y
# even when no ref is present — there is no screenshot-layer equivalent.
# This guards the Bug #2 regression where navigate was misrouted.
# ---------------------------------------------------------------------------


def test_classify_navigate_is_web_native():
    """navigate has no ref but must still run in the browser layer."""
    feats = {"context": "web", "action_type": "navigate", "target_in_a11y": False}
    d = classify(feats)
    assert d.layer == "web_a11y"


def test_classify_press_key_is_web_native():
    feats = {"context": "web", "action_type": "press_key", "target_in_a11y": False}
    d = classify(feats)
    assert d.layer == "web_a11y"


def test_classify_scroll_is_web_native():
    feats = {"context": "web", "action_type": "scroll", "target_in_a11y": False}
    d = classify(feats)
    assert d.layer == "web_a11y"


def test_classify_click_with_known_ref_uses_web_a11y():
    """A click on a ref that IS in the a11y cache stays on the fast path."""
    feats = {"context": "web", "action_type": "click", "target_in_a11y": True}
    d = classify(feats)
    assert d.layer == "web_a11y"


def test_classify_click_with_unknown_ref_falls_to_screenshot():
    """A click on a ref NOT in the a11y cache degrades to the screenshot layer."""
    feats = {"context": "web", "action_type": "click", "target_in_a11y": False}
    d = classify(feats)
    assert d.layer == "screenshot_fallback"


def test_classify_critical_action_overrides_web_native():
    """A critical action forces the screenshot layer even on a web-native type.

    Although `confirm_purchase` is the action *type* (not a web-native one),
    this makes explicit that the priority-95 visual_confirm rule wins over
    the priority-70 web_a11y rule.
    """
    feats = {
        "context": "web",
        "action_type": "confirm_purchase",
        "target_in_a11y": True,
        "needs_visual_confirm": True,
    }
    d = classify(feats)
    assert d.layer == "screenshot_fallback"
    assert d.priority == 95