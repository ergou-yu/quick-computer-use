import json
from types import SimpleNamespace

import pytest

from qcu.batch import execute, parse_actions


@pytest.mark.parametrize('value', ['{}', '[]', '[null]', '[{"type":4}]',
    '[{"type":"fill","params":null}]', '[{"type":"fill","params":{"ref":"r"}}]',
    '[{"type":"click"},{"type":"fill","params":{"ref":"r","text":"x"}}]',
    '[{"type":"made_up"}]'])
def test_invalid_group_is_rejected_before_execution(value):
    with pytest.raises((ValueError, TypeError)):
        parse_actions(value)


def test_group_limit():
    with pytest.raises(ValueError):
        parse_actions(json.dumps([dict(type='fill', params=dict(ref='r', text='x'))]*33))


def setup_batch(monkeypatch, outcomes):
    from qcu import cli_handlers as h
    monkeypatch.setattr('qcu.session.load', lambda: SimpleNamespace(last_refs=[dict(ref='a'), dict(ref='b')]))
    calls = []
    def act(value, layer=None):
        calls.append(json.loads(value))
        rc, payload = outcomes[len(calls)-1]
        print(json.dumps(payload))
        return rc
    monkeypatch.setattr(h, 'act', act)
    actions = parse_actions(json.dumps([
        dict(type='fill', params=dict(ref='a', text='one')),
        dict(type='fill', params=dict(ref='b', text='two')),
        dict(type='click', params=dict(ref='b'))]))
    return actions, calls


def test_order_and_one_final_observation(monkeypatch, capsys):
    actions, calls = setup_batch(monkeypatch, [(0,dict(ok=True))]*3)
    observed = []
    def observe(**kwargs):
        observed.append(kwargs)
        print(json.dumps({'elements': [{'ref':'new', 'value':'two'}]}))
        return 0
    monkeypatch.setattr('qcu.cli_handlers.observe', observe)
    assert execute(actions, layer=None, observe_after=True, compact=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['executed'] == 3
    assert [a['type'] for a in calls] == ['fill','fill','click']
    assert all(a['params']['strict_ref'] for a in calls)
    assert len(observed) == 1
    assert result['observation']['elements'][0]['value'] == 'two'


@pytest.mark.parametrize('outcome', [(2,dict(ok=False)),
    (0,dict(ok=True,data=dict(effect_verified=False))),
    (0,dict(ok=True,data=dict(verification=dict(verified='no'))))])
def test_failure_does_not_submit_or_repeat(monkeypatch, capsys, outcome):
    actions, calls = setup_batch(monkeypatch, [outcome])
    assert execute(actions, layer=None, observe_after=False, compact=False) != 0
    result = json.loads(capsys.readouterr().out)
    assert len(calls) == 1
    assert result['executed'] == 1 and result['stopped_at'] == 0


def test_unknown_ref_prevents_partial_form_writes(monkeypatch, capsys):
    actions, calls = setup_batch(monkeypatch, [])
    actions[1].params['ref'] = 'old'
    assert execute(actions, layer=None, observe_after=False, compact=False) == 2
    assert not calls
    assert json.loads(capsys.readouterr().out)['executed'] == 0


@pytest.mark.parametrize('outcome', [dict(ok=True,dispatch_state='sent',outcome='unknown'),
    dict(ok=False,dispatch_state='unknown',outcome='unknown',data=dict(reason='outcome_unknown'))])
def test_explicit_unknown_outcome_stops_with_attempt_count(monkeypatch, capsys, outcome):
    actions, calls = setup_batch(monkeypatch, [(0,outcome)])
    assert execute(actions, layer=None, observe_after=False, compact=False) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == 'outcome_unknown'
    assert result['executed'] == 1 and result['stopped_at'] == 0
    assert len(calls) == 1


def test_not_sent_failure_is_not_reported_as_uncertain_dispatch(monkeypatch, capsys):
    actions, calls = setup_batch(monkeypatch, [(2, dict(ok=False, dispatch_state='not_sent', outcome='unknown'))])
    assert execute(actions, layer=None, observe_after=False, compact=True) == 2
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == 'action_failed' and len(calls) == 1


def test_explicit_verification_takes_precedence_over_legacy_ui_change_hints(monkeypatch, capsys):
    payload = dict(ok=True, dispatch_state='sent', outcome='verified',
                   data=dict(no_effect=True, effect_verified=False, verification=dict(verified=True)))
    actions, calls = setup_batch(monkeypatch, [(0, payload)] * 3)
    assert execute(actions, layer=None, observe_after=False, compact=True) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['executed'] == 3 and len(calls) == 3


def test_exception_after_attempt_reports_unknown_and_stops(monkeypatch, capsys):
    actions, calls = setup_batch(monkeypatch, [])
    def accepted_then_error(*a, **k):
        calls.append('sent')
        raise RuntimeError('reply lost after action')
    monkeypatch.setattr('qcu.cli_handlers.act', accepted_then_error)
    assert execute(actions, layer=None, observe_after=False, compact=True) == 1
    result = json.loads(capsys.readouterr().out)
    assert result['reason'] == 'outcome_unknown' and result['executed'] == 1
    assert result['results'][0]['dispatch_state'] == 'unknown'


def test_later_verification_ref_rejected_before_first_form_write(monkeypatch, capsys):
    actions, calls = setup_batch(monkeypatch, [])
    actions[1].params['verify'] = {'kind': 'value', 'ref': 'expired', 'equals': 'two'}
    assert execute(actions, layer=None, observe_after=False, compact=True) == 2
    result = json.loads(capsys.readouterr().out)
    assert not calls and result['executed'] == 0
    assert result['reason'] == 'unknown_verification_ref'


def test_later_invalid_verification_rejected_before_first_form_write(monkeypatch, capsys):
    actions, calls = setup_batch(monkeypatch, [])
    actions[1].params['verify'] = {'kind': 'invalid'}
    assert execute(actions, layer=None, observe_after=False, compact=True) == 2
    assert not calls and json.loads(capsys.readouterr().out)['executed'] == 0
