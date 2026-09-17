"""Regression for auxiliary AXWindows preceding the target's real window."""
import sys
from types import SimpleNamespace

import pytest
from qcu.layers._ax_verify import snapshot, hash_string, _match_signal, SignalClass


@pytest.mark.parametrize('window_order', [('aux', 'main'), ('main', 'aux')])
def test_button_verifies_its_own_window_even_when_not_first(monkeypatch, window_order):
    monkeypatch.setitem(sys.modules, 'ApplicationServices', SimpleNamespace(AXUIElementCopyAttributeValue=object()))
    state = {
        'button': {'AXRole': 'AXButton', 'AXWindow': 'main'},
        'app': {'AXWindows': window_order},
        'aux': {'AXTitle': 'Window', 'AXChildren': []},
        'main': {'AXTitle': 'Calculator', 'AXChildren': ['display']},
        'display': {'AXRole': 'AXStaticText', 'AXValue': '0'},
    }
    def get(fn, el, attr): return 0, state[el].get(attr)
    before = snapshot('button', get, app='app')
    state['display']['AXValue'] = '7'
    after = snapshot('button', get, app='app')
    assert before.window_count == after.window_count == 2
    assert before.window_title_hash == hash_string('Calculator')
    assert before.window_text_hash == hash_string('0')
    assert after.window_text_hash == hash_string('7')
    assert _match_signal(before, after, SignalClass.BUTTON_PRESS, None)


def test_target_window_is_read_without_app_lookup(monkeypatch):
    monkeypatch.setitem(sys.modules, 'ApplicationServices', SimpleNamespace(AXUIElementCopyAttributeValue=object()))
    attrs = {'button': {'AXRole': 'AXButton', 'AXWindow': 'win'},
             'win': {'AXTitle': 'Target', 'AXChildren': ['text']},
             'text': {'AXRole': 'AXStaticText', 'AXValue': 'Saved'}}
    s = snapshot('button', lambda fn, el, attr: (0, attrs[el].get(attr)))
    assert s.window_text_hash == hash_string('Saved')


def test_missing_target_window_does_not_choose_arbitrary_window(monkeypatch):
    monkeypatch.setitem(sys.modules, 'ApplicationServices', SimpleNamespace(AXUIElementCopyAttributeValue=object()))
    attrs = {'button': {'AXRole': 'AXButton'}, 'app': {'AXWindows': ['aux', 'main']}}
    s = snapshot('button', lambda fn, el, attr: (0, attrs[el].get(attr)), app='app')
    assert s.window_count == 2
    assert s.window_text_hash == (0, '')


def test_slow_dispatch_does_not_consume_post_action_polling_budget(monkeypatch):
    import time
    from qcu.layers import _ax_verify as v
    before = v.Snapshot(timestamp=time.perf_counter()-10, window_text_hash=hash_string('0'))
    after = v.Snapshot(window_text_hash=hash_string('7'))
    monkeypatch.setattr(v, 'snapshot', lambda *a, **k: after)
    monkeypatch.setattr(v, '_record_telemetry', lambda *a: None)
    verdict = v.verify(before, signal=SignalClass.BUTTON_PRESS, target='button',
                       ax_getter=object(), window_ms=50, first_poll_ms=0)
    assert verdict.verified is v.Verdict.YES
    assert verdict.samples_taken == 1
