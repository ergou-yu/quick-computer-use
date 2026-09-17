"""Risk tests: ambiguous dispatch is never another permission to send."""
from unittest.mock import AsyncMock, MagicMock
import pytest
from qcu.common.types import Action, Element, LayerResult
from qcu.common.verification import validate_condition, match_condition
from qcu.layers.web_a11y import WebA11yLayer


def test_call_and_ui_change_do_not_establish_requested_outcome():
    result = LayerResult(True, 'test', data={'effect_verified': True,
                                            'verification': {'verified': 'yes'}})
    assert result.dispatch_state == 'sent'
    assert result.outcome == 'unknown'
    assert result.to_dict()['data']['retry_safe'] is False


@pytest.mark.parametrize('condition', [{}, {'kind':'value'}, {'kind':'text','contains':4},
    {'kind':'checked','ref':'r','equals':1}, {'kind':'text','equals':'x','timeout_ms':-1}])
def test_bad_verification_rejected(condition):
    with pytest.raises(ValueError):
        validate_condition(condition)


def test_missing_state_does_not_verify_false():
    assert not match_condition({'kind':'checked','ref':'r','equals':False}, [Element('r','checkbox')])['verified']


def web(monkeypatch):
    monkeypatch.setattr('qcu.session.load', lambda: None)
    layer = WebA11yLayer()
    page = MagicMock(url='http://localhost/fixture')
    layer._page = page
    layer._last_refs = {'r': {'cx':20, 'cy':40}}
    loc = page.locator.return_value
    loc.count = AsyncMock(return_value=1)
    loc.click = AsyncMock()
    loc.fill = AsyncMock()
    loc.evaluate = AsyncMock(return_value='saved')
    page.mouse.click = AsyncMock()
    return layer, page, loc


@pytest.mark.asyncio
@pytest.mark.parametrize('action', ['click', 'fill'])
async def test_side_effect_then_transport_error_never_replays(monkeypatch, action):
    layer, page, loc = web(monkeypatch)
    sends = []
    async def accepted_then_timeout(*args, **kwargs):
        sends.append(action)
        raise TimeoutError('accepted, response lost')
    getattr(loc, action).side_effect = accepted_then_timeout
    result = await layer._act_async(Action(action, {'ref':'r', 'text':'saved'}))
    assert sends == [action]
    assert result.dispatch_state == 'unknown' and result.outcome == 'unknown'
    assert result.data['retry_safe'] is False
    page.mouse.click.assert_not_called()


@pytest.mark.asyncio
async def test_click_with_no_condition_is_only_sent(monkeypatch):
    layer, page, loc = web(monkeypatch)
    result = await layer._act_async(Action('click', {'ref':'r'}))
    assert result.ok and result.dispatch_state == 'sent' and result.outcome == 'unknown'
    loc.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_postcondition_does_not_repeat_click(monkeypatch):
    layer, page, loc = web(monkeypatch)
    loc.inner_text = AsyncMock(return_value='Pending')
    result = await layer._act_async(Action('click', {'ref':'r','verify':{
        'kind':'text','equals':'Saved','timeout_ms':0}}))
    assert not result.ok and result.dispatch_state == 'sent' and result.outcome == 'unknown'
    loc.click.assert_awaited_once()


@pytest.mark.asyncio
async def test_explicit_text_and_toggle_verification(monkeypatch):
    layer, page, loc = web(monkeypatch)
    loc.inner_text = AsyncMock(return_value='Saved 1')
    result = await layer._act_async(Action('click', {'ref':'r','verify':{
        'kind':'text','equals':'Saved 1','timeout_ms':0}}))
    assert result.outcome == 'verified'
    loc.evaluate = AsyncMock(return_value={'checked':True})
    result = await layer._act_async(Action('click', {'ref':'r','verify':{
        'kind':'checked','ref':'r','equals':True,'timeout_ms':0}}))
    assert result.outcome == 'verified'


@pytest.mark.asyncio
async def test_invalid_verification_prevents_send(monkeypatch):
    layer, page, loc = web(monkeypatch)
    result = await layer._act_async(Action('click', {'ref':'r','verify':{'kind':'value'}}))
    assert result.dispatch_state == 'not_sent'
    loc.click.assert_not_called()


def test_backend_restart_does_not_restore_web_handles(monkeypatch):
    session = MagicMock(last_refs=[{'ref':'obs_1:ref_0','backend_id':3}])
    monkeypatch.setattr('qcu.session.load', lambda: session)
    assert WebA11yLayer()._last_refs == {}


@pytest.mark.asyncio
@pytest.mark.parametrize('moved', [False, True])
async def test_history_timeout_does_not_claim_action_was_not_sent(monkeypatch, moved):
    layer, page, loc = web(monkeypatch)
    page.evaluate = AsyncMock(return_value=1)
    async def navigate():
        if moved:
            page.url = 'http://localhost/previous'
        raise TimeoutError('history moved but acknowledgement lost')
    page.go_back = AsyncMock(side_effect=lambda **kwargs: None)
    async def dispatch(**kwargs):
        await navigate()
    page.go_back.side_effect = dispatch
    result = await layer._act_async(Action('go_back', {}))
    assert result.dispatch_state == 'unknown'
    assert result.data['dispatched'] is None
    assert result.data['retry_safe'] is False
    page.go_back.assert_awaited_once()


@pytest.mark.parametrize('condition', [
    {'kind': 'text', 'contains': ''}, {'kind': 'text', 'equals': ''},
    {'kind': 'value', 'ref': 'r', 'contains': ''},
])
def test_empty_text_is_not_sufficient_outcome_evidence(condition):
    with pytest.raises(ValueError):
        validate_condition(condition)


def test_explicit_empty_field_value_is_valid_evidence():
    condition = {'kind': 'value', 'ref': 'r', 'equals': ''}
    assert match_condition(condition, [Element('r', 'textbox', value='')])['verified']
