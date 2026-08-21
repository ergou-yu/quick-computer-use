"""Tests for ``qcu.layers.web_a11y`` — ref injection + fill fallback.

These run as unit tests by stubbing out the Playwright surface so we
don't need a real browser. The end-to-end path is covered by manual
runs against https://example.com in the README.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from qcu.common.types import Action, Rect
from qcu.layers import web_a11y as wa


def _fake_page():
    """Return a MagicMock that quacks like a Playwright ``Page`` for our needs."""
    page = MagicMock()
    page.goto = AsyncMock()
    page.url = "https://example.com/"
    page.locator = MagicMock()
    return page


def _fake_cdp():
    """Return a MagicMock with the CDP send() surface we touch."""
    cdp = MagicMock()
    cdp.send = AsyncMock()
    return cdp


def _make_layer_with_refs(page, cdp):
    """Build a WebA11yLayer wired up to mocks + one cached ref."""
    layer = wa.WebA11yLayer()
    layer._page = page
    layer._cdp = cdp
    layer._loop = MagicMock()  # not actually used because we call async methods directly
    layer._last_refs = {
        "ref_42": {
            "backend_id": 17,
            "cx": 100.0,
            "cy": 200.0,
            "role": "edit",
            "name": "Email",
            "bounds": Rect(50, 150, 200, 40),
        }
    }
    return layer


# ---------------------------------------------------------------------------
# _ensure_ref_injected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_ref_injected_locator_already_present():
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)
    page.locator.return_value = loc

    layer = _make_layer_with_refs(page, _fake_cdp())
    ok = await layer._ensure_ref_injected(page, "ref_42")
    assert ok is True
    # Did not need to call CDP.
    layer._cdp.send.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_ref_injected_missing_attribute_calls_cdp():
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)  # attribute not yet present
    page.locator.return_value = loc

    cdp = _fake_cdp()
    cdp.send.side_effect = [
        {"object": {"objectId": "obj-1"}},  # DOM.resolveNode
        None,  # Runtime.callFunctionOn
        None,  # Runtime.releaseObject
    ]

    layer = _make_layer_with_refs(page, cdp)
    ok = await layer._ensure_ref_injected(page, "ref_42")
    assert ok is True
    # We should have made 3 CDP calls.
    assert cdp.send.await_count == 3
    # First call resolves by backendNodeId.
    first = cdp.send.await_args_list[0]
    assert first.args[0] == "DOM.resolveNode"
    assert first.args[1] == {"backendNodeId": 17}


@pytest.mark.asyncio
async def test_ensure_ref_injected_returns_false_when_ref_unknown():
    page = _fake_page()
    layer = _make_layer_with_refs(page, _fake_cdp())
    ok = await layer._ensure_ref_injected(page, "ref_does_not_exist")
    assert ok is False


@pytest.mark.asyncio
async def test_ensure_ref_injected_returns_false_when_no_backend_id():
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.locator.return_value = loc

    layer = _make_layer_with_refs(page, _fake_cdp())
    layer._last_refs["ref_42"]["backend_id"] = None
    ok = await layer._ensure_ref_injected(page, "ref_42")
    assert ok is False


@pytest.mark.asyncio
async def test_ensure_ref_injected_swallows_cdp_errors():
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.locator.return_value = loc

    cdp = _fake_cdp()
    cdp.send.side_effect = RuntimeError("CDP exploded")

    layer = _make_layer_with_refs(page, cdp)
    ok = await layer._ensure_ref_injected(page, "ref_42")
    assert ok is False


# ---------------------------------------------------------------------------
# _fill fallback path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fill_uses_locator_when_ref_in_dom():
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=1)
    loc.fill = AsyncMock()
    page.locator.return_value = loc

    cdp = _fake_cdp()
    layer = _make_layer_with_refs(page, cdp)
    result = await layer._fill(page, {"ref": "ref_42", "text": "alice@example.com"})

    assert result.ok is True
    assert "locator" in result.message
    loc.fill.assert_awaited_with("alice@example.com")


@pytest.mark.asyncio
async def test_fill_falls_back_to_coord_and_keyboard():
    """When the DOM annotation is missing AND coord cache exists, we should
    click at cached coords, select-all, and type. No more 'ref not in DOM'."""
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)  # attribute missing
    loc.fill = AsyncMock()
    page.locator.return_value = loc
    page.mouse.click = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.keyboard.type = AsyncMock()

    cdp = _fake_cdp()
    cdp.send.side_effect = Exception("CDP down")  # injection also fails

    layer = _make_layer_with_refs(page, cdp)
    result = await layer._fill(page, {"ref": "ref_42", "text": "hi"})

    assert result.ok is True
    assert "coord+keyboard" in result.message
    page.mouse.click.assert_awaited_once()
    # Select-all (Meta+A on Mac, Control+A elsewhere), Delete, then type.
    presses = [c.args[0] for c in page.keyboard.press.await_args_list]
    assert any(p in ("Meta+A", "Control+A") for p in presses)
    assert "Delete" in presses
    page.keyboard.type.assert_awaited_with("hi")


@pytest.mark.asyncio
async def test_fill_errors_when_ref_unknown():
    page = _fake_page()
    layer = _make_layer_with_refs(page, _fake_cdp())
    result = await layer._fill(page, {"ref": "ref_does_not_exist", "text": "x"})
    assert result.ok is False
    assert "unknown ref" in result.message


@pytest.mark.asyncio
async def test_fill_errors_when_no_coord_and_no_dom():
    page = _fake_page()
    loc = MagicMock()
    loc.count = AsyncMock(return_value=0)
    page.locator.return_value = loc

    layer = _make_layer_with_refs(page, _fake_cdp())
    layer._last_refs["ref_42"]["cx"] = None
    layer._last_refs["ref_42"]["cy"] = None
    layer._last_refs["ref_42"]["bounds"] = None

    result = await layer._fill(page, {"ref": "ref_42", "text": "x"})
    assert result.ok is False


@pytest.mark.asyncio
async def test_fill_errors_when_no_ref_param():
    page = _fake_page()
    layer = _make_layer_with_refs(page, _fake_cdp())
    result = await layer._fill(page, {"text": "x"})
    assert result.ok is False
    assert "fill needs params.ref" in result.message


# ---------------------------------------------------------------------------
# _goto_with_retry
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goto_with_retry_succeeds_after_transient_error():
    page = _fake_page()
    page.goto.side_effect = [
        Exception("net::ERR_CONNECTION_CLOSED"),
        None,
    ]
    await wa._goto_with_retry(page, "https://example.com", attempts=3)
    assert page.goto.await_count == 2


@pytest.mark.asyncio
async def test_goto_with_retry_gives_up_on_non_transient_error():
    page = _fake_page()
    page.goto.side_effect = Exception("page.something: SyntaxError")
    with pytest.raises(Exception) as exc:
        await wa._goto_with_retry(page, "https://example.com", attempts=3)
    assert "SyntaxError" in str(exc.value)
    assert page.goto.await_count == 1  # no retry for non-transient


@pytest.mark.asyncio
async def test_goto_with_retry_retries_multiple_transient():
    page = _fake_page()
    page.goto.side_effect = [
        Exception("net::ERR_NETWORK_CHANGED"),
        Exception("net::ERR_CONNECTION_CLOSED"),
        None,
    ]
    await wa._goto_with_retry(page, "https://example.com", attempts=3)
    assert page.goto.await_count == 3


@pytest.mark.asyncio
async def test_goto_with_retry_eventually_gives_up():
    page = _fake_page()
    page.goto.side_effect = Exception("net::ERR_CONNECTION_CLOSED")
    with pytest.raises(Exception) as exc:
        await wa._goto_with_retry(page, "https://example.com", attempts=2)
    assert "ERR_CONNECTION_CLOSED" in str(exc.value)
    assert page.goto.await_count == 2


# ---------------------------------------------------------------------------
# _is_mac helper
# ---------------------------------------------------------------------------


def test_is_mac_is_bool(monkeypatch):
    import platform

    monkeypatch.setattr(platform, "system", lambda: "Darwin")
    assert wa._is_mac() is True
    monkeypatch.setattr(platform, "system", lambda: "Linux")
    assert wa._is_mac() is False
    monkeypatch.setattr(platform, "system", lambda: "Windows")
    assert wa._is_mac() is False