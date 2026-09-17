"""Native identity and dispatch contracts; all OS providers here are simulated."""
import sys
from types import SimpleNamespace

import pytest

from qcu.common.refs import NativeRefRegistry
from qcu.common.types import Action
from qcu.layers.desktop_ax import DesktopAXLayer


def test_native_ref_scope_lifetime_and_exact_generation():
    registry = NativeRefRegistry('desktop_ax')
    target = {'pid': 10, 'window_id': 100}
    registry.begin(target)
    ref = registry.issue(1)
    assert registry.validate(ref) == (True, 'current')
    assert registry.validate(ref, {'pid': 20, 'window_id': 100})[1] == 'ref_target_mismatch'
    registry.begin(target)
    assert registry.validate(ref)[1] == 'stale_ref'
    assert registry.validate(ref.replace('obs_1', 'obs_999'))[1] == 'stale_ref'
    current = registry.issue(1)
    registry.invalidate()
    assert registry.validate(current)[1] == 'ref_lifecycle_expired'


def test_native_backend_restart_cannot_restore_tokens():
    first, second = NativeRefRegistry('desktop_ax'), NativeRefRegistry('desktop_ax')
    for registry in (first, second):
        registry.begin({'pid': 10, 'window_id': 100})
    assert second.validate(first.issue(1))[1] == 'ref_lifecycle_expired'
    assert first.validate('ref_1')[1] == 'legacy_ref_requires_observe'
    assert first.validate('obs_1:ref_1')[1] == 'legacy_ref_requires_observe'
    assert first.validate(first.issue(1).replace('desktop_ax', 'desktop_uia'))[1] == 'ref_backend_mismatch'


@pytest.fixture
def native(monkeypatch):
    monkeypatch.setattr('qcu.session.load', lambda: None)
    monkeypatch.setattr('qcu.session.patch', lambda **kw: None)
    apps = [SimpleNamespace(processIdentifier=lambda: 10, localizedName=lambda: 'Fixture')]
    front = {'app': apps[0]}
    state = {
        'app10': {'AXRole': 'AXApplication', 'AXTitle': 'Fixture', 'AXWindows': ['win1', 'win2'],
                  'AXChildren': ['win1', 'win2'], 'AXFocusedWindow': 'win1'},
        'win1': {'AXRole': 'AXWindow', 'AXTitle': 'Editor One', 'AXWindowNumber': 101,
                 'AXChildren': ['button1', 'field1', 'text1'], 'AXPosition': SimpleNamespace(x=0, y=0),
                 'AXSize': SimpleNamespace(width=300, height=200)},
        'win2': {'AXRole': 'AXWindow', 'AXTitle': 'Editor Two', 'AXWindowNumber': 102,
                 'AXChildren': ['button2', 'text2']},
        'button1': {'AXRole': 'AXButton', 'AXTitle': 'Save', 'AXWindow': 'win1', 'AXEnabled': True,
                    'AXPosition': SimpleNamespace(x=10, y=20), 'AXSize': SimpleNamespace(width=20, height=20)},
        'button2': {'AXRole': 'AXButton', 'AXTitle': 'Save', 'AXWindow': 'win2', 'AXEnabled': True},
        'field1': {'AXRole': 'AXTextField', 'AXTitle': 'Input', 'AXWindow': 'win1', 'AXEnabled': True, 'AXValue': ''},
        'text1': {'AXRole': 'AXStaticText', 'AXValue': 'Ready'},
        'text2': {'AXRole': 'AXStaticText', 'AXValue': 'Saved'},
    }
    reads, sends = [], []
    def get(element, attr, *_):
        reads.append((element, attr))
        if element not in state:
            return -25202, None
        return 0, state[element].get(attr)
    def set_value(element, attr, value):
        sends.append((element, attr, value))
        state[element][attr] = value
        return 0
    ax = SimpleNamespace(AXUIElementCopyAttributeValue=get,
        AXUIElementCreateApplication=lambda pid: f'app{pid}',
        AXUIElementSetAttributeValue=set_value,
        AXUIElementIsAttributeSettable=lambda element, attr, *_: (0, element == 'field1'),
        AXUIElementGetPid=lambda element, _: (0, 10),
        AXUIElementCopyActionNames=lambda element, *_: (0, ['AXPress']),
        AXUIElementPerformAction=lambda element, action: sends.append((element, action)) or 0,
        AXIsProcessTrusted=lambda: True, AXUIElementCreateSystemWide=lambda: 'system',
        kAXTitleAttribute='AXTitle')
    monkeypatch.setitem(sys.modules, 'ApplicationServices', ax)
    monkeypatch.setitem(sys.modules, 'AppKit', SimpleNamespace(NSWorkspace=SimpleNamespace(sharedWorkspace=lambda:
        SimpleNamespace(runningApplications=lambda: apps, frontmostApplication=lambda: front['app']))))
    monkeypatch.setitem(sys.modules, 'Quartz.CoreGraphics', SimpleNamespace(CGEventCreateMouseEvent=lambda: None))
    monkeypatch.setitem(sys.modules, 'Quartz', SimpleNamespace())
    layer = DesktopAXLayer()
    layer._available = True
    layer._cached_trusted = True
    return SimpleNamespace(layer=layer, state=state, reads=reads, sends=sends, apps=apps, front=front, ax=ax)


def observe_one(native):
    obs = native.layer.observe(pid=10, window='Editor One')
    assert not obs.routing_meta.get('error')
    return obs


def test_window_missing_does_not_walk_application(native):
    obs = native.layer.observe(pid=10, window='Missing')
    assert obs.routing_meta['reason'] == 'window_not_found'
    assert len(obs.routing_meta['candidates']) == 2
    assert obs.elements == []
    assert not any(attr == 'AXRole' and element.startswith('button') for element, attr in native.reads)
    assert native.layer._assert_click_target_for(20, 30)['ok'] is False


def test_ambiguous_window_returns_candidates_without_merging(native):
    obs = native.layer.observe(pid=10, window='Editor')
    assert obs.routing_meta['reason'] == 'ambiguous_window'
    assert [c['window_id'] for c in obs.routing_meta['candidates']] == [101, 102]
    assert obs.elements == []


def test_explicit_missing_pid_and_app_never_use_frontmost(native):
    for options in ({'pid': 404}, {'app': 'Missing'}, {'pid': 10, 'app': 'Other'}):
        obs = native.layer.observe(**options)
        assert obs.routing_meta['reason'] == 'app_not_found'
        assert obs.elements == []


def test_duplicate_application_requires_pid(native):
    native.apps.append(SimpleNamespace(processIdentifier=lambda: 20, localizedName=lambda: 'Fixture'))
    obs = native.layer.observe(app='Fixture')
    assert obs.routing_meta['reason'] == 'ambiguous_app'
    assert [a['pid'] for a in obs.routing_meta['candidates']] == [10, 20]


def test_observation_has_live_scope_and_inherits_exact_window(native):
    obs = observe_one(native)
    assert obs.routing_meta['target']['pid'] == 10
    assert obs.routing_meta['target']['window_id'] == 101
    assert [e.name for e in obs.elements] == ['Save', 'Input']
    ref = obs.elements[0].ref
    assert native.layer._resolve_ax_element(ref) == 'button1'
    native.front['app'] = SimpleNamespace(processIdentifier=lambda: 999, localizedName=lambda: 'Other')
    again = native.layer.observe()
    assert again.routing_meta['target']['window_id'] == 101
    assert native.layer._resolve_ax_element(ref) is None
    assert native.layer._ref_error == 'stale_ref'
    assert native.layer._resolve_ax_element(again.elements[0].ref) == 'button1'


def test_window_switch_invalidates_prior_ref_and_never_guess_same_name(native):
    old = observe_one(native).elements[0].ref
    native.layer.observe(pid=10, window='Editor Two')
    result = native.layer.act(Action('click', {'ref': old}))
    assert result.dispatch_state == 'not_sent'
    assert result.data['reason'] == 'ref_target_mismatch'
    assert not any(len(send) == 2 for send in native.sends)


@pytest.mark.parametrize('mutation,reason', [
    (lambda n: n.state.pop('button1'), 'element_stale'),
    (lambda n: n.state['button1'].update(AXEnabled=False), 'element_disabled'),
    (lambda n: n.state['button1'].update(AXEnabled=None), 'element_state_unavailable'),
    (lambda n: n.state['button1'].update(AXWindow='win2'), 'ref_window_mismatch'),
    (lambda n: n.state['app10'].update(AXWindows=['win2']), 'ref_window_mismatch'),
    (lambda n: setattr(n.ax, 'AXUIElementGetPid', lambda *_: (0, 20)), 'ref_target_mismatch'),
])
def test_changed_native_identity_or_disabled_never_dispatches(native, mutation, reason):
    ref = observe_one(native).elements[0].ref
    mutation(native)
    result = native.layer.act(Action('click', {'ref': ref}))
    assert result.dispatch_state == 'not_sent'
    assert result.data['reason'] == reason
    assert not any(len(send) == 2 for send in native.sends)


def test_ref_after_backend_restart_requires_observe(native):
    ref = observe_one(native).elements[0].ref
    restarted = DesktopAXLayer()
    restarted._target_scope = dict(native.layer._target_scope)
    restarted._last_refs = dict(native.layer._last_refs)
    result = restarted.act(Action('click', {'ref': ref}))
    assert result.data['reason'] == 'ref_lifecycle_expired'
    assert result.dispatch_state == 'not_sent'


def test_legacy_reference_not_silently_reinterpreted(native):
    observe_one(native)
    native.layer._last_refs['ref_1'] = native.layer._last_refs[next(iter(native.layer._last_refs))]
    result = native.layer.act(Action('click', {'ref': 'ref_1', 'x': 20, 'y': 30}))
    assert result.data['reason'] == 'legacy_ref_requires_observe'
    assert result.dispatch_state == 'not_sent'


def test_wrong_action_app_does_not_rebind_ref(native):
    ref = observe_one(native).elements[0].ref
    result = native.layer.act(Action('click', {'ref': ref, 'app': 'Other'}))
    assert result.data['reason'] == 'target_mismatch'
    assert result.dispatch_state == 'not_sent'


def test_fill_semantic_value_verified_and_ref_cache_preserved(native):
    obs = observe_one(native)
    ref = obs.elements[1].ref
    native.sends.clear()
    result = native.layer.act(Action('fill', {'ref': ref, 'text': 'hello'}))
    assert result.outcome == 'verified' and result.dispatch_state == 'sent'
    assert native.sends == [('field1', 'AXValue', 'hello')]
    assert native.layer._resolve_ax_element(ref) == 'field1'


def test_fill_error_unknown_no_keyboard_or_click_replay(native):
    ref = observe_one(native).elements[1].ref
    native.sends.clear()
    def failed(*args):
        native.sends.append(args)
        raise RuntimeError('lost reply')
    native.ax.AXUIElementSetAttributeValue = failed
    result = native.layer.act(Action('fill', {'ref': ref, 'text': 'hello'}))
    assert result.dispatch_state == 'unknown' and result.outcome == 'unknown'
    assert result.data['retry_safe'] is False
    assert len(native.sends) == 1


def test_missing_value_pattern_is_not_sent(native):
    ref = observe_one(native).elements[0].ref
    native.sends.clear()
    result = native.layer.act(Action('fill', {'ref': ref, 'text': 'hello'}))
    assert result.dispatch_state == 'not_sent' and result.data['reason'] == 'unsupported'
    assert native.sends == []


def test_text_verification_cannot_match_another_window(native, monkeypatch):
    ref = observe_one(native).elements[0].ref
    monkeypatch.setattr(native.layer, '_pre_action_snapshot', lambda _: None)
    native.sends.clear()
    result = native.layer.act(Action('click', {'ref': ref, 'verify': {'kind': 'text', 'contains': 'Saved', 'timeout_ms': 0}}))
    assert result.outcome == 'unknown' and result.dispatch_state == 'sent'
    assert len(native.sends) == 1
    assert native.layer._resolve_ax_element(ref) == 'button1'


def test_text_verification_matches_same_window(native, monkeypatch):
    ref = observe_one(native).elements[0].ref
    monkeypatch.setattr(native.layer, '_pre_action_snapshot', lambda _: None)
    def save(*args):
        native.state['text1']['AXValue'] = 'Saved'
        return 0
    native.ax.AXUIElementPerformAction = save
    result = native.layer.act(Action('click', {'ref': ref, 'verify': {'kind': 'text', 'equals': 'Saved', 'timeout_ms': 0}}))
    assert result.outcome == 'verified' and result.ok


def test_invalid_verification_is_rejected_before_dispatch(native):
    ref = observe_one(native).elements[0].ref
    native.sends.clear()
    result = native.layer.act(Action('click', {'ref': ref, 'verify': {'kind': 'guess'}}))
    assert result.dispatch_state == 'not_sent'
    assert result.data['reason'] == 'invalid_verification'
    assert native.sends == []


def test_coordinate_checks_pass_exact_observed_pid_and_window(native, monkeypatch):
    observe_one(native)
    captured = {}
    def gate(app, x, y, **kw):
        captured.update(kw)
        return {'ok': True}
    monkeypatch.setattr('qcu.layers._click_safety.assert_click_target', gate)
    assert native.layer._assert_click_target_for(20, 30)['ok']
    assert captured['expected_pid'] == 10 and captured['expected_window_id'] == 101
    assert captured['app_window_bounds']['Width'] == 300


def test_live_ref_coordinates_refresh_after_layout_move(native):
    ref = observe_one(native).elements[0].ref
    native.state['button1']['AXPosition'] = SimpleNamespace(x=100, y=110)
    assert native.layer._live_point(native.layer._resolve_ax_element(ref)) == (110, 120)


def test_foreground_input_rejects_same_app_wrong_window(native):
    observe_one(native)
    native.state['app10']['AXFocusedWindow'] = 'win2'
    result = native.layer.act(Action('press_key', {'key': 'Enter'}))
    assert result.dispatch_state == 'not_sent'
    assert result.data['reason'] == 'foreground_target_unconfirmed'


def test_empty_provider_not_claimed_empty_application(native):
    native.state['win1']['AXChildren'] = []
    obs = observe_one(native)
    assert obs.elements == []
    assert 'provider' in obs.routing_meta['empty_tree_reason']


def test_screenshot_requires_explicit_window_scope(native):
    native.layer.observe(pid=10)
    result = native.layer._screenshot(None)
    assert result.dispatch_state == 'not_sent'
    assert result.data['reason'] == 'target_window_unbound'


def test_boxed_ax_geometry_is_decoded(native):
    from qcu.layers.desktop_ax import _ax_geometry
    native.ax.kAXValueCGPointType = 1
    native.ax.kAXValueCGSizeType = 2
    calls = []
    def decode(value, kind, out):
        calls.append((value, kind, out))
        return True, SimpleNamespace(x=10, y=20) if kind == 1 else SimpleNamespace(width=100, height=200)
    native.ax.AXValueGetValue = decode
    assert _ax_geometry('boxed-position').x == 10
    assert _ax_geometry('boxed-size', size=True).width == 100
    assert calls == [('boxed-position', 1, None), ('boxed-size', 2, None)]


def test_window_number_read_error_does_not_skip_quartz_identity(native, monkeypatch):
    original = native.ax.AXUIElementCopyAttributeValue
    def get(element, attr, *args):
        if attr == 'AXWindowNumber':
            raise RuntimeError('unsupported attribute')
        return original(element, attr, *args)
    native.ax.AXUIElementCopyAttributeValue = get
    bounds = {'X': 0.0, 'Y': 0.0, 'Width': 300.0, 'Height': 200.0}
    windows = [{'kCGWindowOwnerPID': 999, 'kCGWindowNumber': 909, 'kCGWindowBounds': bounds},
               {'kCGWindowOwnerPID': 10, 'kCGWindowNumber': 101, 'kCGWindowBounds': bounds}]
    monkeypatch.setitem(sys.modules, 'Quartz', SimpleNamespace(CGWindowListCopyWindowInfo=lambda *a: windows,
                         kCGWindowListOptionOnScreenOnly=1, kCGNullWindowID=0))
    identity = native.layer._window_identity('win1', 10, 'Editor One')
    assert identity['window_id'] == 101
    # Two indistinguishable windows must not choose one.
    windows.append({'kCGWindowOwnerPID': 10, 'kCGWindowNumber': 102, 'kCGWindowBounds': bounds})
    assert 'window_id' not in native.layer._window_identity('win1', 10, 'Editor One')


def test_coordinate_ref_scopes_own_window_even_in_app_observation(native, monkeypatch):
    observation = native.layer.observe(pid=10)
    ref = observation.elements[0].ref
    captured = {}
    monkeypatch.setattr('qcu.layers._click_safety.assert_click_target',
                        lambda app, x, y, **kw: captured.update(kw) or {'ok': True})
    assert native.layer._assert_click_target_for(20, 30, ref)['ok']
    assert captured['expected_window_id'] == 101


def test_unknown_activation_never_launches_again(native, monkeypatch):
    sys.modules['AppKit'].NSApplicationActivateIgnoringOtherApps = 1
    calls = []
    def activate(flags):
        calls.append(flags)
        raise RuntimeError('reply lost')
    native.apps[0].activateWithOptions_ = activate
    def forbid(*a, **kw):
        raise AssertionError('activation must not replay as launch')
    monkeypatch.setattr(native.layer, '_launch_app', forbid)
    result = native.layer.act(Action('activate_app', {'app': 'Fixture'}))
    assert result.dispatch_state == 'unknown' and result.data['retry_safe'] is False
    assert calls == [1]


def test_go_back_preserves_unknown_dispatch(native, monkeypatch):
    from qcu.common.types import LayerResult
    observe_one(native)
    monkeypatch.setattr(native.layer, '_press_key', lambda key: LayerResult(False, 'desktop_ax',
        data={'reason': 'outcome_unknown'}, dispatch_state='unknown'))
    result = native.layer.act(Action('go_back'))
    assert result.dispatch_state == 'unknown' and result.data['retry_safe'] is False


@pytest.mark.parametrize('options', [{'pid': 0}, {'pid': -1}, {'pid': 'bad'}, {'app': ''}, {'window': ''}])
def test_empty_explicit_target_never_becomes_frontmost(native, options):
    obs = native.layer.observe(**options)
    assert obs.routing_meta['reason'] == 'invalid_target' and obs.elements == []


@pytest.mark.parametrize('extra', [{'window': 'Editor Two'}, {'window_id': 102}, {'window': ''}])
def test_action_cannot_override_bound_window(native, extra):
    ref = observe_one(native).elements[0].ref
    native.sends.clear()
    result = native.layer.act(Action('click', {'ref': ref, **extra}))
    assert result.dispatch_state == 'not_sent' and result.data['reason'] == 'target_mismatch'
    assert native.sends == []


def test_restored_scope_requires_observation_before_foreground_input(native, monkeypatch):
    obs = observe_one(native)
    monkeypatch.setattr('qcu.session.load', lambda: SimpleNamespace(target_app='Fixture', last_observation=obs.to_dict()))
    restored = DesktopAXLayer()
    result = restored.act(Action('press_key', {'key': 'Enter'}))
    assert result.dispatch_state == 'not_sent'
    assert restored._binding_error == 'target_requires_observe'


def test_scroll_checks_actual_point_and_sets_event_target(native, monkeypatch):
    observe_one(native)
    cg = sys.modules['Quartz.CoreGraphics']
    event = object()
    cg.CGEventCreate = lambda _: object()
    cg.CGEventGetLocation = lambda _: SimpleNamespace(x=20, y=30)
    cg.CGEventCreateScrollWheelEvent2 = lambda *a: event
    cg.kCGScrollEventUnitLine, cg.kCGHIDEventTap = 1, 2
    events = []
    cg.CGEventSetLocation = lambda e, point: events.append(('location', e, point))
    cg.CGEventPost = lambda tap, e: events.append(('post', e))
    sys.modules['Quartz'].CGPointMake = lambda x,y: (x,y)
    points = []
    monkeypatch.setattr(native.layer, '_assert_click_target_for', lambda x,y: points.append((x,y)) or {'ok': True})
    result = native.layer.act(Action('scroll', {'dy': 3}))
    assert result.dispatch_state == 'sent'
    assert points == [(20,30)]
    assert events == [('location', event, (20,30)), ('post', event)]
    events.clear()
    monkeypatch.setattr(native.layer, '_assert_click_target_for', lambda *a: {'ok': False, 'reason': 'wrong_window'})
    result = native.layer.act(Action('scroll', {'dy': 3}))
    assert result.dispatch_state == 'not_sent' and events == []


def test_scroll_without_position_inspection_does_not_dispatch(native):
    observe_one(native)
    result = native.layer.act(Action('scroll', {'dy': 3}))
    assert result.dispatch_state == 'not_sent'
    assert result.data['reason'] == 'coordinate_check_unavailable'
