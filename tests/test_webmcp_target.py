"""WebMCP target and dispatch contracts, using a fake page (no browser)."""
import asyncio
from types import SimpleNamespace

import pytest

from qcu.common.types import Action
from qcu.layers.webmcp import WebMCPLayer, _GET_TOOLS_JS, _PROBE_JS


@pytest.fixture
def bound_web(monkeypatch, tmp_path):
    from qcu import session as store
    from qcu.layers import runtime
    monkeypatch.setattr(store, 'SESSION_PATH', tmp_path / 'session.json')
    store.new('web', 'web_a11y')
    state = dict(document=1, calls=0, reply={'ok': True, 'dispatched': True, 'result': 'accepted'})
    loop = asyncio.new_event_loop()
    async def evaluate(js):
        if 'performance.timeOrigin' in js:
            return state['document']
        if js == _PROBE_JS:
            return {'available': True, 'has_execute': True}
        if js == _GET_TOOLS_JS:
            return [{'name': 'save', 'description': 'fixture tool'}]
        state['calls'] += 1
        if isinstance(state['reply'], BaseException):
            raise state['reply']
        return state['reply']
    async def verify(condition):
        return {'verified': True, 'condition': condition}
    page = SimpleNamespace(is_closed=lambda: False, url='http://fixture/', evaluate=evaluate)
    web = SimpleNamespace(_page=page, _loop=loop, _last_refs={}, _verify_condition=verify,
                          _ensure_browser=lambda **k: pytest.fail('WebMCP must not launch a browser'))
    monkeypatch.setattr(runtime, 'get_layer', lambda name: web if name == 'web_a11y' else pytest.fail('wrong target backend'))
    layer = WebMCPLayer()
    assert layer.observe().tools[0]['name'] == 'save'
    yield layer, web, state
    loop.close()


def test_webmcp_successful_call_is_sent_with_unknown_task_result(bound_web):
    layer, web, state = bound_web
    result = layer.act(Action('webmcp_call', {'tool': 'save'}))
    assert result.ok and result.dispatch_state == 'sent' and result.outcome == 'unknown'
    assert state['calls'] == 1 and result.data['retry_safe'] is False


@pytest.mark.parametrize('reply', [TimeoutError('reply lost'), {'ok': False, 'dispatched': None, 'error': 'tool timed out'}])
def test_webmcp_dispatch_uncertainty_never_replays(bound_web, reply):
    layer, web, state = bound_web
    state['reply'] = reply
    result = layer.act(Action('webmcp_call', {'tool': 'save'}))
    assert result.dispatch_state == 'unknown' and result.outcome == 'unknown'
    assert result.data['reason'] == 'outcome_unknown'
    assert result.data['retry_safe'] is False and state['calls'] == 1


def test_webmcp_missing_execute_is_not_sent(bound_web):
    layer, web, state = bound_web
    state['reply'] = {'ok': False, 'dispatched': False, 'error': 'missing execute'}
    result = layer.act(Action('webmcp_call', {'tool': 'save'}))
    assert result.dispatch_state == 'not_sent'


def test_webmcp_document_change_invalidates_discovered_tools(bound_web):
    layer, web, state = bound_web
    state['document'] = 2
    result = layer.act(Action('webmcp_call', {'tool': 'save'}))
    assert result.dispatch_state == 'not_sent' and state['calls'] == 0


def test_webmcp_undiscovered_tool_is_not_sent(bound_web):
    layer, web, state = bound_web
    result = layer.act(Action('webmcp_call', {'tool': 'other'}))
    assert result.dispatch_state == 'not_sent' and state['calls'] == 0


def test_webmcp_explicit_result_condition_can_verify(bound_web):
    layer, web, state = bound_web
    result = layer.act(Action('webmcp_call', {'tool': 'save', 'verify': {'kind': 'text', 'equals': 'Saved'}}))
    assert result.dispatch_state == 'sent' and result.outcome == 'verified'
    assert state['calls'] == 1


def test_webmcp_desktop_session_cannot_open_browser(monkeypatch, tmp_path):
    from qcu import session as store
    from qcu.layers import runtime
    monkeypatch.setattr(store, 'SESSION_PATH', tmp_path / 'session.json')
    store.new('desktop', 'desktop_ax')
    monkeypatch.setattr(runtime, 'get_layer', lambda name: pytest.fail('must not load web backend'))
    obs = WebMCPLayer().observe()
    assert not obs.routing_meta['available'] and obs.routing_meta['reason'] == 'target_unavailable'
