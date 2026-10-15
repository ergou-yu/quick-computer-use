"""Injected AT-SPI contracts. These do not establish Linux interoperability."""
import threading
import time

import pytest

from qcu.common.types import Action, Rect
from qcu.layers.desktop_linux import DesktopLinuxLayer, LinuxError


class Node:
    def __init__(self, name, role="push button", parent=None):
        self.name, self.role, self.parent = name, role, parent
        self.children = []
        if parent:
            parent.children.append(self)
        self.key, self.pid = ":1.2/"+name, 7
        self.value, self.enabled, self.editable, self.focused = "", True, role=="text", False
        self.checked, self.selected, self.defunct = False, False, False

    def get_parent(self):
        return self.parent


class API:
    def __init__(self, root):
        self.root, self.version = root, 1
        self.owner, self.closed = threading.get_ident(), None
        self.calls, self.send_error, self.delay, self.ack = [], False, 0., True

    @property
    def native(self):
        pytest.fail("Semantic actions must not load X11, take screenshots or inject input")

    def windows(self, pid=None):
        return [self.root]

    def key(self, obj):
        return obj.key

    def pid(self, obj):
        return obj.pid

    def process_identity(self, pid):
        return dict(process_started=self.version, process_image="/usr/bin/fixture")

    def children(self, obj, limit):
        return obj.children[:limit]

    def info(self, obj):
        assert threading.get_ident() == self.owner
        if obj.defunct:
            raise LinuxError("element_unavailable", "defunct")
        return dict(name=obj.name, raw_role=obj.role, value=obj.value, bounds=Rect(10,20,100,30),
                    enabled=obj.enabled, editable=obj.editable, focused=obj.focused, offscreen=False,
                    checked=obj.checked, selected=obj.selected, interfaces=["Action","Text","EditableText"])

    def prepare(self, obj, kind, params):
        def send():
            self.calls.append((kind, threading.get_ident()))
            if self.delay:
                time.sleep(self.delay)
            if self.send_error:
                raise RuntimeError("late DBus error")
            if kind in {"fill","type"}:
                obj.value = params["text"] if kind=="fill" else obj.value+params["text"]
            elif kind=="toggle":
                obj.checked = not obj.checked
            elif kind=="select":
                obj.selected = True
            return self.ack
        return send

    def close(self):
        self.closed = threading.get_ident()


@pytest.fixture
def linux():
    from qcu import session
    session.new("desktop","desktop_linux")
    root = Node("Fixture","frame")
    for name,role in (("Input","text"),("Save","push button"),("Check","check box"),("Item","list item")):
        Node(name,role,root)
    box = {}
    def factory():
        box["api"] = API(root)
        return box["api"]
    layer = DesktopLinuxLayer(api_factory=factory)
    obs = layer.observe(pid=7)
    assert obs.routing_meta["available"], obs
    refs = {e.name:e.ref for e in obs.elements}
    yield layer,root,box["api"],refs
    layer.close()


def test_semantic_unicode_fill_is_fast_and_worker_bound(linux):
    layer,root,api,refs = linux
    result = layer.act(Action("fill",dict(ref=refs["Input"],text="中文 🚀")))
    assert result.ok and result.outcome=="verified"
    assert api.calls==[("fill",api.owner)] and api.owner!=threading.get_ident()
    assert root.children[0].value=="中文 🚀"
    layer.close()
    assert api.closed==api.owner


@pytest.mark.parametrize("kind,name,verify",[("click","Save",None),
    ("toggle","Check",dict(kind="checked",equals=True)),("select","Item",dict(kind="selected",equals=True))])
def test_semantic_operations_and_readback(linux,kind,name,verify):
    layer,_,api,refs = linux
    if verify:
        verify["ref"]=refs[name]
    result=layer.act(Action(kind,dict(ref=refs[name],verify=verify)))
    assert result.ok and result.dispatch_state=="sent"
    assert result.outcome==("verified" if verify else "unknown")
    assert len(api.calls)==1


def test_stale_ref_after_observation_never_dispatches(linux):
    layer,_,api,refs=linux
    layer.observe()
    result=layer.act(Action("click",dict(ref=refs["Save"])))
    assert result.data["reason"]=="stale_ref" and not api.calls


@pytest.mark.parametrize("change,reason",[("disabled","control_disabled"),("readonly","control_readonly"),
                                        ("reparent","ref_target_mismatch"),("identity","element_unavailable")])
def test_live_control_checks_precede_input(linux,change,reason):
    layer,root,api,refs=linux
    obj=root.children[0]
    if change=="disabled": obj.enabled=False
    if change=="readonly": obj.editable=False
    if change=="reparent": obj.parent=None
    if change=="identity": obj.key=":1.2/new"
    result=layer.act(Action("fill",dict(ref=refs["Input"],text="x")))
    assert result.data["reason"]==reason and result.dispatch_state=="not_sent" and not api.calls


def test_pid_reuse_and_session_switch_reject_old_target(linux):
    layer,_,api,refs=linux
    api.version+=1
    result=layer.act(Action("click",dict(ref=refs["Save"])))
    assert result.data["reason"]=="target_changed" and not api.calls


@pytest.mark.parametrize("failure",["exception","false_ack"])
def test_uncertain_dispatch_never_replays(linux,failure):
    layer,_,api,refs=linux
    api.send_error=failure=="exception"
    api.ack=failure!="false_ack"
    result=layer.act(Action("click",dict(ref=refs["Save"])))
    assert result.dispatch_state=="unknown" and result.outcome=="unknown"
    assert len(api.calls)==1


def test_timeout_poisons_worker_without_queueing_second_action(linux):
    layer,_,api,refs=linux
    layer._timeout=.1
    api.delay=.25
    first=layer.act(Action("click",dict(ref=refs["Save"])))
    second=layer.act(Action("click",dict(ref=refs["Save"])))
    assert first.dispatch_state=="unknown"
    assert second.data["reason"]=="backend_restart_required"
    assert len(api.calls)==1


def test_type_needs_focus_and_uses_editable_text(linux):
    layer,root,api,refs=linux
    assert layer.act(Action("type",dict(ref=refs["Input"],text="x"))).data["reason"]=="focus_mismatch"
    root.children[0].focused=True
    assert layer.act(Action("type",dict(ref=refs["Input"],text="中文"))).ok
    assert len(api.calls)==1


def test_explicit_target_and_mixed_coordinates_rejected(linux):
    layer,_,api,refs=linux
    for params in (dict(ref=refs["Save"],pid=8),dict(ref=refs["Save"],x=1,y=2)):
        result=layer.act(Action("click",params))
        assert result.dispatch_state=="not_sent"
    assert not api.calls


def test_observation_filter_keeps_reference_generation(linux):
    layer,_,_,_=linux
    obs=layer.observe(query="Input",limit=1)
    assert len(obs.elements)==1 and obs.routing_meta["n_total"]==5
    assert layer.act(Action("fill",dict(ref=obs.elements[0].ref,text="filtered"))).ok


def test_unbound_target_and_platform_guard():
    import sys
    layer=DesktopLinuxLayer()
    obs=layer.observe(pid=7)
    if not sys.platform.startswith("linux"):
        assert obs.routing_meta["reason"]=="platform_mismatch"
    layer.close()


def test_gi_text_interface_does_not_call_accessible_interface_getter():
    from types import SimpleNamespace
    from qcu.layers.desktop_linux import AtspiAPI
    api=object.__new__(AtspiAPI)
    state_names=("DEFUNCT","SHOWING","ENABLED","SENSITIVE","FOCUSED","CHECKED","SELECTED","SELECTABLE","EDITABLE")
    api.a=SimpleNamespace(StateType=SimpleNamespace(**{n:n for n in state_names}),
                         Text=SimpleNamespace(get_text=lambda obj,start,end:obj.value[start:end]))
    api.refresh=lambda _:None
    class GIObject:
        value="中文 🚀"
        def get_text(self):
            pytest.fail("Accessible.get_text is an interface getter, not text content")
        def get_text_iface(self): return self
        def get_character_count(self): return len(self.value)
        def get_state_set(self): return SimpleNamespace(contains=lambda s:s in {"ENABLED","SENSITIVE","EDITABLE"})
        def get_interfaces(self): return ["Text","EditableText"]
        def get_role_name(self): return "entry"
        def get_name(self): return "Name"
    assert api.info(GIObject())["value"]=="中文 🚀"


def install_native(monkeypatch):
    from types import SimpleNamespace
    calls=[]
    native=SimpleNamespace(bind=lambda *args:None,
        dispatch=lambda target,kind,p: calls.append((kind,p)) or dict(target=target,method="X11.XTEST"))
    monkeypatch.setattr(API,"native",property(lambda _:native))
    return calls


def test_native_drag_uses_live_ref_endpoints(linux,monkeypatch):
    layer,_,_,refs=linux
    calls=install_native(monkeypatch)
    assert layer.act(Action("drag",dict(ref_from=refs["Input"],ref_to=refs["Save"])) ).ok
    assert len(calls)==1 and calls[0][1]["x"]==60 and calls[0][1]["y2"]==35
    bad=layer.act(Action("drag",dict(ref_from=refs["Input"],x1=10,y1=20,ref_to=refs["Save"])))
    assert bad.data["reason"]=="ref_coordinate_conflict" and len(calls)==1


def test_explicit_click_fallback_only_precedes_semantic_send(linux,monkeypatch):
    layer,_,api,refs=linux
    calls=install_native(monkeypatch)
    def unsupported(*args): raise LinuxError("unsupported","No Action")
    api.prepare=unsupported
    result=layer.act(Action("click",dict(ref=refs["Save"],fallback="native")))
    assert result.ok and len(calls)==1 and not api.calls


def test_late_error_cannot_enter_explicit_fallback(linux):
    layer,_,api,refs=linux
    api.send_error=True
    result=layer.act(Action("click",dict(ref=refs["Save"],fallback="native")))
    assert result.dispatch_state=="unknown" and len(api.calls)==1


def test_atk_insert_length_counts_utf8_bytes_but_caret_counts_characters():
    from types import SimpleNamespace
    from qcu.layers.desktop_linux import AtspiAPI
    calls=[]
    editable=SimpleNamespace(insert_text=lambda pos,text,length:calls.append((pos,text,length)) or True)
    text=SimpleNamespace(get_n_selections=lambda:0,get_caret_offset=lambda:8)
    obj=SimpleNamespace(get_text_iface=lambda:text,get_editable_text_iface=lambda:editable)
    api=object.__new__(AtspiAPI)
    send=api.prepare(obj,"type",dict(text=" 追加🚀"))
    assert send() is True and calls==[(8," 追加🚀",11)]
