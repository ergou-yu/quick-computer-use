from unittest.mock import AsyncMock, MagicMock

import pytest

from qcu.layers.web_a11y import WebA11yLayer


def layer_fixture(monkeypatch):
    monkeypatch.setattr('qcu.session.load', lambda: None)
    monkeypatch.setattr('qcu.session.record_observation', lambda *a, **kw: None)
    layer = WebA11yLayer()
    page = MagicMock(url='http://localhost/fixture')
    page.title = AsyncMock(return_value='Fixture')
    page.wait_for_load_state = AsyncMock()
    layer._page = page
    layer._cdp = MagicMock(send=AsyncMock(return_value={'nodes':[]}))
    return layer, page


@pytest.mark.asyncio
async def test_default_observe_does_not_wait_for_background_network(monkeypatch):
    layer, page = layer_fixture(monkeypatch)
    obs = await layer._observe_async(10)
    page.wait_for_load_state.assert_awaited_once_with('domcontentloaded', timeout=3000)
    assert obs.routing_meta['readiness']['timed_out'] is False


@pytest.mark.asyncio
async def test_explicit_async_target_wait_and_timeout_are_visible(monkeypatch):
    layer, page = layer_fixture(monkeypatch)
    page.wait_for_load_state.side_effect = TimeoutError('busy')
    page.locator.return_value.wait_for = AsyncMock()
    obs = await layer._observe_async(10, wait_until='networkidle', wait_for='#ready', timeout_ms=200)
    assert obs.routing_meta['readiness']['timed_out'] is True
    page.locator.return_value.wait_for.assert_awaited_once_with(state='visible', timeout=200)
    page.locator.return_value.wait_for.side_effect = TimeoutError('target missing')
    with pytest.raises(TimeoutError):
        await layer._observe_async(10, wait_for='#missing')


@pytest.mark.asyncio
@pytest.mark.parametrize('method', ['_fill', '_ref_action'])
async def test_strict_ref_does_not_degrade_to_cached_coordinates(monkeypatch, method):
    layer, page = layer_fixture(monkeypatch)
    layer._last_refs = {'ref_1':dict(cx=10,cy=20)}
    loc = page.locator.return_value
    loc.count = AsyncMock(return_value=2)
    params = dict(ref='ref_1', text='x', strict_ref=True)
    if method == '_fill':
        result = await layer._fill(page, params)
    else:
        result = await layer._ref_action(page, 'click', params)
    assert not result.ok
    assert result.data['dispatched'] is False
    page.mouse.click.assert_not_called()


@pytest.mark.asyncio
async def test_strict_fill_detects_controlled_input_rejection(monkeypatch):
    layer, page = layer_fixture(monkeypatch)
    layer._last_refs = {'ref_1':dict(cx=10,cy=20)}
    loc = page.locator.return_value
    loc.count = AsyncMock(return_value=1)
    loc.fill = AsyncMock()
    loc.evaluate = AsyncMock(return_value='old value')
    result = await layer._fill(page, dict(ref='ref_1',text='new value',strict_ref=True))
    assert not result.ok
    assert result.data['effect_verified'] is False
    page.mouse.click.assert_not_called()
