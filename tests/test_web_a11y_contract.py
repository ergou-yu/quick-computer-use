"""Regression tests for the observation/action contract fixes in web_a11y.

Each test pins down one of the 22 reported issues with a mocked CDP/page, so
we get coverage without a real browser. The end-to-end browser path is still
exercised by tests marked ``integration``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from qcu.common.types import Action, Rect
from qcu.layers import web_a11y as wa


def _fake_page(url: str = "https://example.com/post"):
    page = MagicMock()
    page.url = url
    page.goto = AsyncMock()
    page.locator = MagicMock()
    page.evaluate = AsyncMock(return_value=None)
    page.keyboard.press = AsyncMock()
    page.go_back = AsyncMock()
    page.go_forward = AsyncMock()
    page.mouse = MagicMock()
    page.mouse.click = AsyncMock()
    page.mouse.dblclick = AsyncMock()
    page.mouse.move = AsyncMock()
    return page


def _fake_cdp():
    cdp = MagicMock()
    cdp.send = AsyncMock()
    return cdp


def _layer(page, cdp):
    layer = wa.WebA11yLayer()
    layer._page = page
    layer._cdp = cdp
    layer._loop = None  # _act_async is awaited directly
    layer._last_refs = {
        "obs_5:ref_0": {
            "backend_id": 1,
            "node_id": "n0",
            "parent_id": None,
            "cx": 10.0,
            "cy": 20.0,
            "role": "link",
            "name": "More",
            "bounds": Rect(0, 10, 20, 20),
        }
    }
    return layer


# ---------------------------------------------------------------------------
# press_key: dispatch != effect  (issue 4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_press_key_reports_no_effect_when_page_unchanged(monkeypatch):
    """press_key must NOT claim success when nothing changed on the page.

    This is the regression test for the Alt+ArrowLeft/Meta+ArrowLeft incident:
    those returned ``ok: True`` three times even though the URL never moved.
    """
    page = _fake_page()
    layer = _layer(page, _fake_cdp())
    result = await layer._act_async(Action(type="press_key", params={"key": "Alt+ArrowLeft"}))
    assert result.ok is True  # dispatch succeeded
    assert result.data["dispatched"] is True
    assert result.data["effect_verified"] is False
    assert result.data["no_effect"] is True


@pytest.mark.asyncio
async def test_press_key_reports_effect_when_url_changed(monkeypatch):
    page = _fake_page(url="https://example.com/a")

    async def change_url_after_press(_key):  # noqa: ANN001
        # Simulate the page navigating in response to the key.
        page.url = "https://example.com/b"

    page.keyboard.press = AsyncMock(side_effect=change_url_after_press)
    layer = _layer(page, _fake_cdp())
    result = await layer._act_async(Action(type="press_key", params={"key": "Alt+ArrowLeft"}))
    assert result.data["effect_verified"] is True
    assert result.data["before_url"].endswith("/a")
    assert result.data["after_url"].endswith("/b")


# ---------------------------------------------------------------------------
# go_back / go_forward: verify even on the no-exception path  (issue 19)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_go_back_no_history_reports_no_effect():
    page = _fake_page(url="https://example.com/only")
    layer = _layer(page, _fake_cdp())
    result = await layer._act_async(Action(type="go_back"))
    assert result.ok is False
    assert result.data["no_effect"] is True
    assert result.data["effect_verified"] is False


# ---------------------------------------------------------------------------
# Stale ref rejection  (issues 9, 10)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_click_rejects_stale_ref(monkeypatch):
    """A ref whose generation is older than the session's must be rejected.

    Without this, clicking `ref_0` after navigation would target whatever
    element happens to reuse index 0 on the new page.
    """
    s = MagicMock()
    s.observation_generation = 9  # current generation advanced past obs_5
    monkeypatch.setattr("qcu.session.load", lambda: s)
    page = _fake_page()
    layer = _layer(page, _fake_cdp())
    result = await layer._ref_action(page, "click", {"ref": "obs_5:ref_0"})
    assert result.ok is False
    assert result.data["reason"] == "stale_ref"


@pytest.mark.asyncio
async def test_click_accepts_current_generation_ref(monkeypatch):
    s = MagicMock()
    s.observation_generation = 5
    monkeypatch.setattr("qcu.session.load", lambda: s)

    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)
    loc.click = AsyncMock()
    page.locator.return_value = loc
    layer = _layer(page, _fake_cdp())
    result = await layer._ref_action(page, "click", {"ref": "obs_5:ref_0"})
    assert result.ok is True


# ---------------------------------------------------------------------------
# Locator failure classification  (issue 12)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_locator_strict_mode_is_classified_not_swallowed(monkeypatch):
    s = MagicMock()
    s.observation_generation = 5
    monkeypatch.setattr("qcu.session.load", lambda: s)

    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)  # present, but click fails
    loc.click = AsyncMock(side_effect=Exception("strict mode violation: resolved to 3 elements"))
    page.locator.return_value = loc
    layer = _layer(page, _fake_cdp())
    result = await layer._ref_action(page, "click", {"ref": "obs_5:ref_0"})
    # The locator call may have sent an event: preserve uncertainty and stop.
    assert result.ok is False
    assert result.data["cause"] == "ambiguous"
    assert result.dispatch_state == "unknown"
    assert result.data["coord_fallback"] is False
    page.mouse.click.assert_not_called()


@pytest.mark.asyncio
async def test_locator_no_geometry_no_fallback(monkeypatch):
    s = MagicMock()
    s.observation_generation = 5
    monkeypatch.setattr("qcu.session.load", lambda: s)

    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)
    loc.click = AsyncMock(side_effect=Exception("timeout"))
    page.locator.return_value = loc
    layer = _layer(page, _fake_cdp())
    # Remove geometry so the fallback can't run.
    layer._last_refs["obs_5:ref_0"]["cx"] = None
    layer._last_refs["obs_5:ref_0"]["cy"] = None
    result = await layer._ref_action(page, "click", {"ref": "obs_5:ref_0"})
    assert result.ok is False
    assert result.data["cause"] == "timeout"
    assert result.data["reason"] == "outcome_unknown"


# ---------------------------------------------------------------------------
# reinjection now uses `this` binding  (the geometry bug)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reinjection_uses_this_not_param():
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.locator.return_value = loc
    cdp = _fake_cdp()
    cdp.send.side_effect = [
        {"object": {"objectId": "obj-1"}},
        None,  # callFunctionOn
        None,  # releaseObject
    ]
    layer = _layer(page, cdp)
    await layer._ensure_ref_injected(page, "obs_5:ref_0")
    call_fn = cdp.send.await_args_list[1]
    decl = call_fn.args[1]["functionDeclaration"]
    # The function must reference `this.setAttribute`, not `el.setAttribute`.
    assert "this.setAttribute" in decl
    assert "el.setAttribute" not in decl


# ---------------------------------------------------------------------------
# _locator_failure helper covers the documented categories
# ---------------------------------------------------------------------------


def test_locator_failure_categories():
    assert wa._locator_failure(Exception("strict mode violation")) == "ambiguous"
    assert wa._locator_failure(Exception("Page.click: Timeout 30000ms")) == "timeout"
    assert wa._locator_failure(Exception("element not visible")) == "not_visible"
    assert wa._locator_failure(Exception("element intercepts pointer")) == "not_actionable"
    assert wa._locator_failure(Exception("element detached")) == "detached"
    assert wa._locator_failure(Exception("irrelevant")) == "locator_error"
