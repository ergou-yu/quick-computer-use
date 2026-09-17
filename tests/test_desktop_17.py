from types import SimpleNamespace
from unittest.mock import MagicMock

from qcu.layers.desktop_ax import DesktopAXLayer


def test_absolute_bundle_path_does_not_match_another_copy():
    running = MagicMock()
    running.localizedName.return_value = 'Calculator'
    running.bundleURL.return_value.path.return_value = '/System/Applications/Calculator.app'
    assert DesktopAXLayer._matches_app('/System/Applications/Calculator.app', running)
    assert not DesktopAXLayer._matches_app('/tmp/Calculator.app', running)
    assert DesktopAXLayer._matches_app('Calculator.app', running)


def test_launch_waits_for_process_registration(monkeypatch):
    import ApplicationServices as ax
    import qcu.layers.desktop_ax as module
    layer = DesktopAXLayer()
    pids = iter([None, 321])
    monkeypatch.setattr(layer, '_app_pid_for', lambda app: next(pids))
    monkeypatch.setattr(ax, 'AXUIElementCreateApplication', lambda pid: 'app')
    monkeypatch.setattr(module, '_ax_get', lambda fn, elem, attr: (0, ['window']) if attr=='AXWindows' else (0, 'Fixture'))
    result = layer._wait_for_app_window('/tmp/Fixture.app', timeout=.3, poll=.01)
    assert result['verified']=='yes' and result['pid']==321


def test_unverified_press_does_not_activate_and_replay(monkeypatch):
    import qcu.layers.desktop_ax as module
    layer = DesktopAXLayer()
    layer._last_refs = {'ref_1':dict(role='button',cx=None,cy=None)}
    layer._target_app = 'Fixture'
    monkeypatch.setattr(layer, '_resolve_ax_element', lambda ref: 'element')
    events = []
    def snapshot(element):
        events.append('snapshot')
        return object()
    def press(*args):
        events.append('press')
        return True,'AXPress'
    def verify(*args, **kwargs):
        assert kwargs.get('before') is not None
        events.append('verify')
        return dict(verified='no')
    monkeypatch.setattr(layer, '_pre_action_snapshot', snapshot)
    monkeypatch.setattr(layer, '_ax_native_click', press)
    monkeypatch.setattr(layer, '_verify_action', verify)
    monkeypatch.setattr(layer, '_activate_app', lambda app: events.append('activate'))
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: None)
    result = layer._click('click',dict(ref='ref_1'))
    assert not result.ok
    assert result.data['reason'] == 'outcome_unknown'
    assert events == ['snapshot','press','verify']
