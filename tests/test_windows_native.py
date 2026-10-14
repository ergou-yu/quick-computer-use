"""Native input/capture contracts with mocked Win32 boundaries (not live UI)."""
import contextlib
import struct
from types import SimpleNamespace
import zlib

import pytest

from qcu.common.types import Action
from qcu.layers._windows_native import WindowsNative, NativeError, key_events, unicode_events, _png, INPUT
from tests.test_desktop_uia import setup_uia, observed


def fake_native():
    native = object.__new__(WindowsNative)
    native.focus = lambda target: target["window_id"]
    native.geometry = lambda target: dict(X=-100, Y=0, Width=300, Height=200)
    native.physical_pixels = contextlib.nullcontext
    native._point_gate = lambda *_: None
    native.user = SimpleNamespace(GetAsyncKeyState=lambda *_: 0, IsWindowEnabled=lambda *_: True,
        GetSystemMetrics=lambda i: {76:-200, 77:0, 78:1000, 79:800}[i], IsChild=lambda *_: False)
    native.calls = []
    native.cursor=(0,0)
    def set_cursor(x,y): native.cursor=(x,y); return True
    def get_cursor(pointer): pointer._obj.x,pointer._obj.y=native.cursor; return True
    native.user.SetCursorPos=set_cursor
    native.user.GetCursorPos=get_cursor
    def send(count, array, size):
        native.calls.append((count, list(array), size))
        return count
    native.user.SendInput = send
    original=native.dispatch
    native.dispatch=lambda target,kind,params,**kw:original(target,kind,dict(params,input_transport="send_input"),**kw)
    return native


def test_native_packets_have_balanced_modifier_and_surrogate_pairs():
    import ctypes
    assert ctypes.sizeof(INPUT) in (28, 40)
    combo = key_events("Ctrl+Shift+Left")
    assert [e.ki.wVk for e in combo] == [17, 16, 37, 37, 16, 17]
    assert [e.ki.dwFlags for e in combo] == [0, 0, 1, 3, 2, 2]
    typed = unicode_events("A中🚀")
    assert len(typed) == 8
    assert [e.ki.wScan for e in typed[::2]] == [65, 0x4E2D, 0xD83D, 0xDE80]
    assert all(typed[i].ki.dwFlags == 4 and typed[i+1].ki.dwFlags == 6 for i in range(0,8,2))


@pytest.mark.parametrize("key", ["", "Cmd+A", "Ctrl+NotAKey"])
def test_bad_keys_never_dispatch(key):
    with pytest.raises(NativeError):
        key_events(key)


def test_png_converts_bgr_and_padding_into_actual_rgb():
    raw = _png(1, 2, b"\0\0\xff\0"+b"\xff\0\0\0", 4)
    assert struct.unpack(">II", raw[16:24]) == (1,2)
    offset, compressed = 8, b""
    while offset < len(raw):
        size = struct.unpack(">I", raw[offset:offset+4])[0]
        if raw[offset+4:offset+8] == b"IDAT":
            compressed += raw[offset+8:offset+8+size]
        offset += size+12
    assert zlib.decompress(compressed) == b"\0\xff\0\0\0\0\0\xff"


def test_negative_origin_double_click_is_one_balanced_dispatch():
    native = fake_native()
    data = native.dispatch({"window_id":10}, "double_click", {"x":-50,"y":40})
    assert data["inserted_events"] == 4 and len(native.calls) == 1
    events = native.calls[0][1]
    assert [e.mi.dwFlags for e in events] == [2,4,2,4]
    assert native.cursor == (-50,40)


def test_partial_send_is_unknown_and_only_releases_not_replays():
    native = fake_native()
    original = native.user.SendInput
    def partial(count, array, size):
        original(count, array, size)
        return 1
    native.user.SendInput = partial
    with pytest.raises(NativeError) as caught:
        native.dispatch({"window_id":10}, "press_key", {"key":"Ctrl+A"})
    assert caught.value.dispatched and caught.value.reason == "outcome_unknown"
    assert len(native.calls) == 2
    assert all(e.ki.dwFlags & 2 for e in native.calls[1][1])


def test_send_exception_is_unknown_and_not_retried():
    native = fake_native()
    def lost(*args):
        native.calls.append(args)
        raise OSError("lost acknowledgement")
    native.user.SendInput = lost
    with pytest.raises(NativeError) as caught:
        native.dispatch({"window_id":10}, "click", {"x":30,"y":40})
    assert caught.value.dispatched and len(native.calls) == 1


def test_held_physical_modifier_rejects_without_send():
    native = fake_native()
    native.user.GetAsyncKeyState = lambda *_: 0x8000
    with pytest.raises(NativeError, match="pressed"):
        native.dispatch({"window_id":10}, "type", {"text":"must not write"})
    assert native.calls == []


def test_overlay_window_is_rejected_by_root_and_pid():
    native = fake_native()
    native.user.WindowFromPoint = lambda *_: 22
    native.user.GetAncestor = lambda *_: 20
    native._pid = lambda hwnd: 7
    with pytest.raises(NativeError) as caught:
        WindowsNative._point_gate(native, {"window_id":10,"pid":7}, native.geometry(None), (20,20))
    assert caught.value.reason == "target_occluded"


def test_semantic_fill_does_not_load_native_capture_or_input(setup_uia):
    refs = observed(setup_uia)
    result = setup_uia[0].act(Action("fill", {"ref":refs["Input"],"text":"fast"}))
    assert result.outcome == "verified"
    assert not hasattr(setup_uia[3]["api"], "native")


def test_explicit_native_ref_uses_fresh_control_bounds(setup_uia):
    refs = observed(setup_uia)
    calls=[]
    setup_uia[3]["api"].native = SimpleNamespace(dispatch=lambda target, kind, params, **kw:
        calls.append((kind,params.copy())) or {"method":"Win32.SendInput"})
    setup_uia[2]["button"].BoundingRectangle = SimpleNamespace(left=100,top=200,right=180,bottom=240)
    result = setup_uia[0].act(Action("click", {"ref":refs["Button"],"input_mode":"native"}))
    assert result.dispatch_state == "sent"
    assert calls[0][1]["x"] == 140 and calls[0][1]["y"] == 220
    assert setup_uia[2]["button"].patterns["invoke"].calls == []


def test_native_stale_ref_rejected_before_send(setup_uia):
    refs = observed(setup_uia)
    observed(setup_uia)
    setup_uia[3]["api"].native = SimpleNamespace(dispatch=lambda *_a,**_k:pytest.fail("stale input"))
    result = setup_uia[0].act(Action("type", {"ref":refs["Input"],"text":"must not write"}))
    assert result.dispatch_state == "not_sent" and result.data["reason"] == "stale_ref"


def test_native_unknown_does_not_repeat_semantic_invoke(setup_uia):
    refs = observed(setup_uia)
    calls=[]
    def unknown(*_a,**_k):
        calls.append(1)
        raise NativeError("outcome_unknown","late error",dispatched=True)
    setup_uia[3]["api"].native = SimpleNamespace(dispatch=unknown)
    result = setup_uia[0].act(Action("click", {"ref":refs["Button"],"input_mode":"native"}))
    assert result.dispatch_state == "unknown" and len(calls) == 1
    assert setup_uia[2]["button"].patterns["invoke"].calls == []


def message_native():
    native=fake_native()
    native._pid=lambda hwnd:7
    native.focus=lambda target:11
    native.user.GetAncestor=lambda *_:10
    native.user.MapVirtualKeyW=lambda vk,_:vk
    native.messages=[]
    native.user.PostMessageW=lambda *args:native.messages.append(args) or True
    return native


def test_window_message_utf16_is_targeted_and_does_not_use_global_sendinput():
    native=message_native()
    result=WindowsNative.dispatch(native,{"window_id":10,"pid":7},"type",{"text":"A中🚀"},focus_hwnd=11)
    assert result["posted_messages"]==4 and result["method"]=="Win32.WindowMessage"
    assert [message[2] for message in native.messages]==[65,0x4E2D,0xD83D,0xDE80]
    assert all(message[0]==11 and message[1]==0x102 for message in native.messages)
    assert native.calls==[]


def test_window_message_key_scan_extended_and_release_bits():
    native=message_native()
    WindowsNative.dispatch(native,{"window_id":10,"pid":7},"press_key",{"key":"Left"})
    assert [m[1] for m in native.messages]==[0x100,0x101]
    assert native.messages[0][3]&(1<<24)
    assert native.messages[1][3]&0xC0000000==0xC0000000


def test_focus_change_after_first_character_is_unknown_and_stops():
    native=message_native()
    native.focus=lambda target:11 if not native.messages else 22
    with pytest.raises(NativeError) as caught:
        WindowsNative.dispatch(native,{"window_id":10,"pid":7},"type",{"text":"ab"})
    assert caught.value.dispatched and caught.value.reason=="outcome_unknown"
    assert len(native.messages)==1 and native.calls==[]


def test_post_ack_failure_is_not_repeated_or_changed_to_sendinput():
    native=message_native()
    native.user.PostMessageW=lambda *args:native.messages.append(args) and False
    with pytest.raises(NativeError) as caught:
        WindowsNative.dispatch(native,{"window_id":10,"pid":7},"type",{"text":"ab"})
    assert caught.value.dispatched and len(native.messages)==1 and native.calls==[]


def test_window_message_unknown_shortcut_is_refused_before_any_send():
    native=message_native()
    native.user.GetClassNameW=lambda _h,name,_n:setattr(name,"value","Edit") or 4
    with pytest.raises(NativeError) as caught:
        WindowsNative.dispatch(native,{"window_id":10,"pid":7},"press_key",{"key":"Ctrl+V"})
    assert not caught.value.dispatched and caught.value.reason=="unsupported_shortcut"
    assert native.messages==[] and native.calls==[]


def test_ctrl_a_custom_editor_does_not_claim_standard_edit_selection():
    native=message_native()
    native.user.GetClassNameW=lambda _h,name,_n:setattr(name,"value","CustomEditorWidget") or 18
    with pytest.raises(NativeError) as caught:
        WindowsNative.dispatch(native,{"window_id":10,"pid":7},"press_key",{"key":"Ctrl+A"})
    assert caught.value.reason=="unsupported_shortcut" and not caught.value.dispatched
    assert native.messages==[]


def test_native_type_readonly_is_refused_before_dispatch(setup_uia):
    refs=observed(setup_uia)
    setup_uia[2]["edit"].patterns["value"].IsReadOnly=True
    setup_uia[3]["api"].native=SimpleNamespace(dispatch=lambda *_a,**_k:pytest.fail("readonly typing"))
    result=setup_uia[0].act(Action("type",{"ref":refs["Input"],"text":"must not write"}))
    assert result.dispatch_state=="not_sent" and result.data["reason"]=="control_readonly"


def test_bound_uia_screenshot_preserves_semantic_refs(setup_uia,monkeypatch,tmp_path):
    from qcu.layers import runtime
    from qcu.layers.screenshot_fallback import ScreenshotFallbackLayer
    refs=observed(setup_uia)
    layer=setup_uia[0]
    def capture(target,path):
        from pathlib import Path
        Path(path).write_bytes(_png(1,1,b"\0\0\xff\0",4))
        return dict(target=target,coordinate_origin=dict(x=0,y=0),coordinate_scale=1.0)
    setup_uia[3]["api"].native=SimpleNamespace(capture=capture,geometry=lambda _:dict(X=0,Y=0,Width=100,Height=60))
    monkeypatch.setattr(runtime,"get_layer",lambda name:layer if name=="desktop_uia" else pytest.fail("wrong target"))
    result=ScreenshotFallbackLayer().act(Action("screenshot",{"path":str(tmp_path/"bound.png"),"window":"Target","app":"FIXTURE7.EXE"}))
    assert result.outcome=="verified" and result.data["target"]["window_id"]==100
    filled=layer.act(Action("fill",{"ref":refs["Input"],"text":"same refs"}))
    assert filled.outcome=="verified" and filled.data["method"]=="UIA.value"


def test_explicit_uia_observe_rebinds_session_after_cdp(monkeypatch,capsys):
    from qcu import cli_handlers as h,session
    from qcu.layers import runtime
    from qcu.common.types import Observation
    session.new("desktop","desktop_cdp")
    monkeypatch.setattr(h,"_ensure_daemon_started",lambda:None)
    monkeypatch.setattr(h,"_maybe_route_via_daemon",lambda *_:None)
    monkeypatch.setattr(h.Telemetry,"record",lambda **_:None)
    monkeypatch.setattr(runtime,"get_layer",lambda name:SimpleNamespace(observe=lambda **_:Observation("desktop",routing_meta={"layer":name})))
    assert h.observe("desktop_uia",8)==0
    assert session.load().layer=="desktop_uia"
