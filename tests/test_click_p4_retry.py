"""A missing verification signal must never replay a dispatched click."""
import sys
from types import SimpleNamespace

import pytest
from qcu.layers.desktop_ax import DesktopAXLayer


@pytest.fixture
def layer(monkeypatch):
    l = DesktopAXLayer()
    l._last_refs = {'ref_1': dict(role='button', cx=100, cy=200, path=[0])}
    l._target_app = 'Fixture'
    monkeypatch.setattr(l, '_resolve_ax_element', lambda ref: 'button')
    monkeypatch.setattr(l, '_pre_action_snapshot', lambda el: object())
    return l


def forbid(*args, **kwargs):
    raise AssertionError('a dispatched click must not be replayed')


@pytest.mark.parametrize('verdict', ['no', 'n/a', 'skipped'])
@pytest.mark.parametrize('action', ['click', 'double_click', 'right_click'])
def test_accepted_action_with_unconfirmed_effect_stops(layer, monkeypatch, verdict, action):
    presses = []
    monkeypatch.setattr(layer, '_ax_native_click', lambda *args: (presses.append(args) or True, 'AXPress'))
    monkeypatch.setattr(layer, '_verify_action', lambda *a, **k: {'verified': verdict})
    monkeypatch.setattr(layer, '_activate_app', forbid)
    monkeypatch.setattr(layer, '_assert_click_target_for', forbid)
    monkeypatch.setattr('qcu.layers._ax_action.perform_axpress_via_osascript', forbid)
    monkeypatch.setattr('qcu.layers._ax_action.perform_axpress_by_attributes', forbid)
    r = layer._click(action, {'ref': 'ref_1'})
    assert len(presses) == 1
    assert not r.ok
    assert r.data == dict(reason='outcome_unknown', retry_safe=False, dispatched=True,
                          verification={'verified': verdict})


def test_verified_action_succeeds_without_fallback(layer, monkeypatch):
    events = []
    snap = object()
    monkeypatch.setattr(layer, '_pre_action_snapshot', lambda el: events.append('before') or snap)
    monkeypatch.setattr(layer, '_ax_native_click', lambda *a: (events.append('press') or True, 'AXPress'))
    def verify(*a, **kw):
        assert kw['before'] is snap
        events.append('verify')
        return {'verified': 'yes'}
    monkeypatch.setattr(layer, '_verify_action', verify)
    monkeypatch.setattr(layer, '_activate_app', forbid)
    assert layer._click('click', {'ref': 'ref_1'}).ok
    assert events == ['before', 'press', 'verify']


def test_missing_pre_snapshot_does_not_sample_after_dispatch(layer, monkeypatch):
    monkeypatch.setattr(layer, '_pre_action_snapshot', lambda el: None)
    monkeypatch.setattr(layer, '_ax_native_click', lambda *a: (True, 'AXPress'))
    monkeypatch.setattr(layer, '_verify_action', forbid)
    r = layer._click('click', {'ref': 'ref_1'})
    assert not r.ok and r.data['reason'] == 'outcome_unknown'


def test_ax_transport_error_does_not_try_another_transport(layer, monkeypatch):
    monkeypatch.setattr(layer, '_ax_native_click', lambda *a: (False, 'AXPress'))
    monkeypatch.setattr(layer, '_activate_app', forbid)
    monkeypatch.setattr(layer, '_assert_click_target_for', forbid)
    r = layer._click('click', {'ref': 'ref_1'})
    assert not r.ok and r.data['retry_safe'] is False


def test_unavailable_ax_can_use_one_coordinate_click_with_pre_snapshot(layer, monkeypatch):
    events = []
    layer._target_app = None  # No Apple Events target in this fixture.
    monkeypatch.setattr(layer, '_ax_native_click', lambda *a: (False, None))
    monkeypatch.setattr(layer, '_live_point', lambda *a: (100, 200))
    monkeypatch.setattr(layer, '_assert_click_target_for', lambda *a: {'ok': True})
    snaps = []
    def before(el):
        snap = object(); snaps.append(snap); events.append('before')
        return snap
    monkeypatch.setattr(layer, '_pre_action_snapshot', before)
    def verify(*a, **kw):
        assert kw['before'] is snaps[-1]
        events.append('verify')
        return {'verified': 'yes'}
    monkeypatch.setattr(layer, '_verify_action', verify)
    symbols = ['kCGEventLeftMouseDown','kCGEventLeftMouseUp','kCGEventMouseMoved',
               'kCGEventRightMouseDown','kCGEventRightMouseUp','kCGHIDEventTap',
               'kCGMouseButtonLeft','kCGMouseButtonRight','kCGMouseEventClickState']
    cg = SimpleNamespace(**{n: i for i,n in enumerate(symbols)},
        CGEventCreateMouseEvent=lambda *a: object(),
        CGEventPost=lambda *a: events.append('post'))
    monkeypatch.setitem(sys.modules, 'Quartz.CoreGraphics', cg)
    monkeypatch.setitem(sys.modules, 'Quartz', SimpleNamespace(CGPointMake=lambda x,y: (x,y)))
    r = layer._click('click', {'ref': 'ref_1'})
    assert r.ok
    assert events == ['before', 'before', 'post', 'post', 'verify']


@pytest.mark.parametrize('outcomes, expected', [([0, 0], True), ([0, -25204], False), ([RuntimeError('lost reply')], False)])
def test_native_send_preserves_attempt_on_error(layer, monkeypatch, outcomes, expected):
    calls = []
    def perform(*args):
        result = outcomes[len(calls)]; calls.append(args)
        if isinstance(result, Exception): raise result
        return result
    monkeypatch.setitem(sys.modules, 'ApplicationServices', SimpleNamespace(
        AXUIElementCopyActionNames=lambda *a: (0, ['AXPress']),
        AXUIElementPerformAction=perform))
    ok, action = layer._ax_native_click('button', 'button', 'double_click')
    assert ok is expected and action == 'AXPress'
    assert len(calls) == len(outcomes)
