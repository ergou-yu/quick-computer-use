"""Contract tests with injected UIA objects; these are NOT Windows UIA runs."""
import threading
import time
from types import SimpleNamespace

import pytest

from qcu.common.types import Action
from qcu.layers.desktop_uia import DesktopUIALayer


class Pattern:
    def __init__(self, *, value="", readonly=False, selected=False, state=0, fail=False):
        self.Value, self.IsReadOnly, self.IsSelected, self.ToggleState = value, readonly, selected, state
        self.calls = []
        self.fail = fail
        self.pattern = self

    def _send(self, method, value=None):
        self.calls.append((method, value, threading.get_ident()))
        if self.fail:
            raise RuntimeError("COM transport failed after dispatch")
        return 0

    def SetValue(self, value):
        result = self._send("value", value)
        self.Value = value
        return result

    def Invoke(self):
        return self._send("invoke")

    def Toggle(self):
        result = self._send("toggle")
        self.ToggleState = 1 - self.ToggleState
        return result

    def Select(self):
        result = self._send("select")
        self.IsSelected = True
        return result


class Control:
    def __init__(self, name="Target", role="WindowControl", *, pid=7, hwnd=100,
                 runtime=None, patterns=None, enabled=True):
        self.Name, self.ControlTypeName = name, role
        self.ProcessId, self.NativeWindowHandle = pid, hwnd
        self.runtime = runtime or [pid, hwnd, id(self)]
        self.AutomationId, self.ClassName = "fixture", "FixtureClass"
        self.IsEnabled, self.HasKeyboardFocus, self.IsOffscreen = enabled, False, False
        self.BoundingRectangle = SimpleNamespace(left=0, top=0, right=100, bottom=60)
        self.patterns = patterns or {}
        self.children = []
        self.parent = None
        self.stale = False
        self.read_count = 0

    def add(self, child):
        child.parent = self
        self.children.append(child)
        return child

    def GetRuntimeId(self):
        self.read_count += 1
        if self.stale:
            raise RuntimeError("UIA element unavailable")
        return self.runtime

    def GetParentControl(self):
        return self.parent

    def GetFirstChildControl(self):
        return self.children[0] if self.children else None

    def GetNextSiblingControl(self):
        if not self.parent:
            return None
        siblings = self.parent.children
        idx = siblings.index(self) + 1
        return siblings[idx] if idx < len(siblings) else None


class API:
    def __init__(self, roots):
        self.roots = roots
        self.thread = threading.get_ident()
        self.closed_thread = None
        self.enumerated = 0
        self.process_version = 1

    def control_from_handle(self, hwnd):
        assert threading.get_ident() == self.thread
        return next((root for root in self.roots if root.NativeWindowHandle == hwnd), None)

    def windows(self):
        self.enumerated += 1
        return [{"window_id": root.NativeWindowHandle, "pid": root.ProcessId, "title": root.Name}
                for root in self.roots]

    def process_identity(self, pid):
        return {"process_started": self.process_version, "process_image": f"C:\\fixture{pid}.exe"}

    def pattern(self, ctrl, kind):
        assert threading.get_ident() == self.thread
        return ctrl.patterns.get(kind)

    def close(self):
        self.closed_thread = threading.get_ident()


@pytest.fixture
def setup_uia(monkeypatch, tmp_path):
    from qcu import session
    monkeypatch.setattr(session, "SESSION_PATH", tmp_path / "session.json")
    monkeypatch.setenv("QCU_HOME", str(tmp_path))
    session.new("desktop", "desktop_uia")
    root = Control()
    controls = {
        "button": root.add(Control("Button", "ButtonControl", patterns={"invoke": Pattern()})),
        "edit": root.add(Control("Input", "EditControl", patterns={"value": Pattern()})),
        "toggle": root.add(Control("Check", "CheckBoxControl", patterns={"toggle": Pattern()})),
        "item": root.add(Control("Item", "ListItemControl", patterns={"selection": Pattern()})),
    }
    box = {}
    def factory():
        box["api"] = API([root])
        return box["api"]
    layer = DesktopUIALayer(api_factory=factory)
    yield layer, root, controls, box
    layer.close()


def observed(setup):
    layer = setup[0]
    obs = layer.observe(pid=7, window="100")
    assert obs.routing_meta["available"] is True
    return {e.name: e.ref for e in obs.elements}


def test_direct_hwnd_never_enumerates_desktop(setup_uia):
    layer, root, controls, box = setup_uia
    refs = observed(setup_uia)
    assert box["api"].enumerated == 0
    obs = layer.observe(window="100", pid=7)
    child = next(e for e in obs.elements if e.name == "Input")
    assert child.role == "edit"
    assert child.properties["parent_ref"] == obs.elements[0].ref
    assert child.ref in obs.elements[0].properties["children"]
    assert child.properties["actions"] == ["fill"]
    assert obs.routing_meta["target"]["pid"] == 7


@pytest.mark.parametrize("options,reason", [({"window": "missing"}, "window_not_found"),
    ({"window": "100", "pid": 9}, "target_mismatch"), ({}, "target_required")])
def test_target_errors_do_not_expand_scope(setup_uia, options, reason):
    obs = setup_uia[0].observe(**options)
    assert not obs.elements
    assert obs.routing_meta["reason"] == reason


def test_ambiguous_window_has_candidates(setup_uia):
    layer, root, controls, box = setup_uia
    observed(setup_uia)
    box["api"].roots.append(Control("Target", hwnd=101))
    obs = layer.observe(pid=7, window="Target")
    assert obs.routing_meta["reason"] == "ambiguous_window"
    assert len(obs.routing_meta["candidates"]) == 2
    assert not obs.elements


@pytest.mark.parametrize("kind,name,params,expected", [
    ("click", "Button", {}, "unknown"), ("fill", "Input", {"text": "new value"}, "verified"),
    ("toggle", "Check", {}, "unknown"), ("select", "Item", {}, "verified")])
def test_patterns_are_semantic_and_report_evidence(setup_uia, kind, name, params, expected):
    layer = setup_uia[0]
    refs = observed(setup_uia)
    result = layer.act(Action(kind, {"ref": refs[name], **params}))
    assert result.ok
    assert result.dispatch_state == "sent"
    assert result.outcome == expected
    patterns = [p for c in setup_uia[2].values() for p in c.patterns.values()]
    assert sum(len(p.calls) for p in patterns) == 1
    assert all(call[2] == setup_uia[3]["api"].thread for p in patterns for call in p.calls)


def test_toggle_verifies_only_explicit_requested_state(setup_uia):
    refs = observed(setup_uia)
    result = setup_uia[0].act(Action("toggle", {"ref": refs["Check"],
        "verify": {"kind": "checked", "ref": refs["Check"], "equals": True}}))
    assert result.outcome == "verified"


def test_control_type_without_pattern_is_unsupported(setup_uia):
    refs = observed(setup_uia)
    setup_uia[2]["button"].patterns.clear()
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"]}))
    assert result.data["reason"] == "unsupported"
    assert result.dispatch_state == "not_sent"


@pytest.mark.parametrize("mutation,reason", [
    (lambda c: setattr(c, "IsEnabled", False), "control_disabled"),
    (lambda c: setattr(c, "stale", True), "uia_access_failed"),
    (lambda c: setattr(c, "parent", Control(hwnd=99)), "ref_target_mismatch")])
def test_disabled_stale_and_reparented_control_rejected(setup_uia, mutation, reason):
    refs = observed(setup_uia)
    button = setup_uia[2]["button"]
    mutation(button)
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"]}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == reason
    assert button.patterns["invoke"].calls == []


@pytest.mark.parametrize("target", [{"pid": 8}, {"window": "other"}, {"app": "other.exe"}])
def test_action_cannot_override_scope(setup_uia, target):
    refs = observed(setup_uia)
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"], **target}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "target_mismatch"


def test_old_observation_and_backend_restart_refs_rejected(setup_uia):
    refs = observed(setup_uia)
    layer = setup_uia[0]
    layer.observe(pid=7, window="100")
    result = layer.act(Action("click", {"ref": refs["Button"]}))
    assert result.data["reason"] == "stale_ref"
    other = DesktopUIALayer(api_factory=lambda: API([setup_uia[1]]))
    try:
        other.observe(pid=7, window="100")
        result = other.act(Action("click", {"ref": refs["Button"]}))
        assert result.data["reason"] == "ref_lifecycle_expired"
    finally:
        other.close()


def test_process_restart_reusing_pid_and_hwnd_rejected(setup_uia):
    refs = observed(setup_uia)
    setup_uia[3]["api"].process_version += 1
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"]}))
    assert result.data["reason"] == "target_changed"
    assert result.dispatch_state == "not_sent"


def test_session_changed_rejects_old_handles(setup_uia):
    refs = observed(setup_uia)
    from qcu import session
    session.new("desktop", "desktop_uia")
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"]}))
    assert result.data["reason"] == "target_changed"


def test_late_error_and_failed_verification_never_replay(setup_uia):
    refs = observed(setup_uia)
    pattern = setup_uia[2]["button"].patterns["invoke"]
    pattern.fail = True
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"]}))
    assert result.dispatch_state == "unknown"
    assert result.outcome == "unknown"
    assert len(pattern.calls) == 1
    pattern.fail = False
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"],
        "verify": {"kind": "text", "equals": "not present", "timeout_ms": 0}}))
    assert result.dispatch_state == "sent"
    assert result.outcome == "unknown"
    assert len(pattern.calls) == 2


def test_worker_is_persistent_and_closed_on_creating_thread(setup_uia):
    observed(setup_uia)
    observed(setup_uia)
    layer, _, _, box = setup_uia
    layer.close()
    assert box["api"].thread == box["api"].closed_thread != threading.get_ident()


def test_empty_tree_is_not_claimed_readable(setup_uia):
    setup_uia[1].children.clear()
    obs = setup_uia[0].observe(pid=7)
    cap = obs.routing_meta["capabilities"]
    assert cap["session_accessible"] is True
    assert cap["can_read_controls"] is None
    assert cap["available"] is False
    assert cap["reason"] == "empty_or_unexposed_tree"


def test_bounded_traversal(setup_uia):
    obs = setup_uia[0].observe(window="100", max_nodes=2)
    assert len(obs.elements) == 2
    assert obs.routing_meta["tree_truncated"]


def test_invalid_verification_is_rejected_before_dispatch(setup_uia):
    refs = observed(setup_uia)
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"], "verify": {"kind": "fake"}}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "invalid_verification"
    assert setup_uia[2]["button"].patterns["invoke"].calls == []


def test_timeout_blocks_new_dispatch(setup_uia):
    refs = observed(setup_uia)
    layer = setup_uia[0]
    layer._call_timeout = .01
    pattern = setup_uia[2]["button"].patterns["invoke"]
    original = pattern.Invoke
    def slow():
        time.sleep(.05)
        return original()
    pattern.Invoke = slow
    result = layer.act(Action("click", {"ref": refs["Button"]}))
    assert result.dispatch_state == "unknown"
    second = layer.act(Action("click", {"ref": refs["Button"]}))
    assert second.dispatch_state == "not_sent"
    assert second.data["reason"] == "backend_restart_required"
    layer.close()
    assert len(pattern.calls) == 1


def test_desktop_observation_does_not_start_browser_status(setup_uia):
    observed(setup_uia)
    from qcu import session
    assert session.load().browser_status == "stopped"


def test_readonly_value_rejected_before_dispatch(setup_uia):
    refs = observed(setup_uia)
    pattern = setup_uia[2]["edit"].patterns["value"]
    pattern.IsReadOnly = True
    result = setup_uia[0].act(Action("fill", {"ref": refs["Input"], "text": "unused"}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "control_readonly"
    assert pattern.calls == []


def test_uia_native_pattern_probe_distinguishes_missing_and_access_denied():
    from qcu.layers.desktop_uia import _WindowsAPI
    api = object.__new__(_WindowsAPI)
    api.auto = SimpleNamespace(CreatePattern=lambda pid, pattern: (pid, pattern))
    class COMFailure(Exception):
        def __init__(self, hresult):
            self.hresult = hresult
    class NativeElement:
        code = None
        def GetCurrentPattern(self, pattern_id):
            assert pattern_id == 10000
            if self.code:
                raise COMFailure(self.code)
            return "native-pattern"
    native = NativeElement()
    ctrl = SimpleNamespace(Element=native)
    assert api.pattern(ctrl, "invoke") == (10000, "native-pattern")
    native.code = 0x80040200
    assert api.pattern(ctrl, "invoke") is None
    native.code = 0x80070005
    with pytest.raises(COMFailure):
        api.pattern(ctrl, "invoke")


def test_uia_permission_denied_is_not_claimed_empty_tree(setup_uia):
    observed(setup_uia)
    class Denied(Exception):
        hresult = 0x80070005
    def denied(*args):
        raise Denied("provider access denied")
    setup_uia[3]["api"].control_from_handle = denied
    obs = setup_uia[0].observe(window="100")
    assert obs.routing_meta["reason"] == "access_denied"
    assert obs.routing_meta["capabilities"]["session_accessible"] is False
    assert not obs.elements


def test_same_name_controls_have_distinct_refs(setup_uia):
    setup_uia[1].add(Control("Button", "ButtonControl", patterns={"invoke": Pattern()}))
    obs = setup_uia[0].observe(window="100")
    buttons = [e for e in obs.elements if e.name == "Button"]
    assert len(buttons) == 2 and buttons[0].ref != buttons[1].ref
    result = setup_uia[0].act(Action("click", {"name": "Button"}))
    assert result.dispatch_state == "not_sent"


@pytest.mark.parametrize("scope", [{"pid": 0}, {"pid": -1}, {"window": ""}, {"app": " "}])
def test_invalid_explicit_scope_does_not_inherit_old_target(setup_uia, scope):
    observed(setup_uia)
    obs = setup_uia[0].observe(**scope)
    assert obs.routing_meta["reason"] == "invalid_target"
    assert not obs.elements


def test_absolute_executable_path_cannot_match_another_directory(setup_uia):
    obs = setup_uia[0].observe(app=r"C:\other\fixture7.exe")
    assert obs.routing_meta["reason"] == "window_not_found"
    assert not obs.elements


def test_action_absolute_executable_path_cannot_match_another_directory(setup_uia):
    refs = observed(setup_uia)
    result = setup_uia[0].act(Action("click", {"ref": refs["Button"], "app": r"C:\other\fixture7.exe"}))
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "target_mismatch"
    assert setup_uia[2]["button"].patterns["invoke"].calls == []


@pytest.mark.parametrize("app", ["fixture7", "FIXTURE7.EXE", "c:/tmp/../FIXTURE7.exe"])
def test_executable_name_and_normalized_absolute_path_match(setup_uia, app):
    layer = setup_uia[0]
    obs = layer.observe(app=app)
    assert obs.routing_meta["available"] is True
    ref = next(e.ref for e in obs.elements if e.name == "Button")
    result = layer.act(Action("click", {"ref": ref, "app": app}))
    assert result.dispatch_state == "sent"
    assert len(setup_uia[2]["button"].patterns["invoke"].calls) == 1
