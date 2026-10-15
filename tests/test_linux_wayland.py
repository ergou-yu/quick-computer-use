"""Portable bridge failure/identity contracts; compositor tests live in CI."""
import base64
import json
from types import SimpleNamespace

import pytest

from qcu.common.types import Rect
from qcu.layers._linux_wayland import GnomeNative
from qcu.layers._linux_x11 import encode_png
from qcu.layers.desktop_linux import AtspiAPI, LinuxError


def native(reply=None, error=None):
    n=object.__new__(GnomeNative)
    calls=[]
    def call(*args):
        calls.append(args)
        if error: raise error
        return SimpleNamespace(unpack=lambda: (json.dumps(reply),))
    n.proxy=SimpleNamespace(call_sync=call)
    n.glib=SimpleNamespace(Variant=lambda signature,value:value)
    n.instance="bridge-1"
    return n,calls


@pytest.mark.parametrize("mutation",[True,False])
def test_bridge_disconnect_is_not_replayed(mutation):
    n,calls=native(error=RuntimeError("connection lost after send"))
    with pytest.raises(LinuxError) as error:
        n._request("input" if mutation else "capture",mutation=mutation)
    assert error.value.dispatched is mutation
    assert error.value.reason==("outcome_unknown" if mutation else "wayland_bridge_unavailable")
    assert len(calls)==1


@pytest.mark.parametrize("dispatched",[False,True])
def test_structured_bridge_rejection_preserves_dispatch_boundary(dispatched):
    n,calls=native(dict(ok=False,reason="foreground_mismatch",error="wrong focus",dispatched=dispatched))
    with pytest.raises(LinuxError) as error:
        n._request("input",mutation=True)
    assert error.value.dispatched is dispatched
    assert len(calls)==1


def test_window_relative_refs_use_mutter_client_origin_not_unavailable_global_atspi():
    n,_=native()
    n._geometry=lambda target:dict(client_bounds=dict(x=-400,y=250,width=800,height=600))
    api=SimpleNamespace(relative_bounds=lambda obj:Rect(20,30,100,40))
    assert n.ref_point(api,object(),{})==(-330,300)


def test_atspi_window_coordinates_are_requested_explicitly():
    api=object.__new__(AtspiAPI)
    api.refresh=lambda _:None
    api.a=SimpleNamespace(CoordType=SimpleNamespace(WINDOW=1))
    calls=[]
    def extents(space):
        calls.append(space)
        return SimpleNamespace(x=14,y=30,width=80,height=20)
    obj=SimpleNamespace(get_component_iface=lambda:SimpleNamespace(get_extents=extents))
    assert api.relative_bounds(obj)==Rect(14,30,80,20)
    assert calls==[1]


def test_identity_change_prevents_dispatch(monkeypatch):
    n,calls=native()
    target=dict(pid=7,process_started="123",process_image="/fixture")
    n._bound=(target,dict(token="bound",window_id="42"))
    monkeypatch.setattr("qcu.layers._linux_wayland.process_identity",lambda _:dict(process_started="124",process_image="/fixture"))
    with pytest.raises(LinuxError) as error:
        n.dispatch(target,"click",dict(x=1,y=2))
    assert error.value.reason=="target_changed" and not error.value.dispatched and not calls


def test_malformed_input_ack_is_unknown_after_mutation():
    n,_=native(dict(ok=True))
    n._identity=lambda _:dict(token="bound",window_id="42")
    with pytest.raises(LinuxError) as error:
        n.dispatch({},"press_key",dict(key="F6"))
    assert error.value.reason=="outcome_unknown" and error.value.dispatched


def test_scaled_capture_records_pixel_to_logical_coordinates(tmp_path):
    n,_=native()
    g=dict(x=-100,y=20,width=2,height=1)
    png=encode_png(b"\0\0\xff\0"*8,4,2,32,16)
    n._bound=({},dict(window_id="42"))
    n._identity=lambda _:{}
    n.geometry=lambda _:g
    n._request=lambda *a,**k:dict(bounds=g,png=base64.b64encode(png).decode())
    result=n.capture({},str(tmp_path/"capture.png"))
    assert result["coordinate_scale"]==2
    assert result["coordinate_origin"]==dict(x=-100,y=20)
    assert result["coordinate_space"]=="wayland_logical"
    assert (tmp_path/"capture.png").read_bytes()==png


def test_moved_capture_does_not_write_a_stale_image(tmp_path):
    n,_=native()
    g=dict(x=1,y=2,width=1,height=1)
    png=encode_png(b"\0\0\xff\0",1,1,32,4)
    n._identity=lambda _:{}
    n.geometry=lambda _:dict(g,x=5)
    n._request=lambda *a,**k:dict(bounds=g,png=base64.b64encode(png).decode())
    with pytest.raises(LinuxError) as error:
        n.capture({},str(tmp_path/"capture.png"))
    assert error.value.reason=="target_changed" and not (tmp_path/"capture.png").exists()


def test_client_shadow_capture_uses_buffer_origin_and_validates_frame(tmp_path):
    n,_=native()
    buffer=dict(x=70,y=90,width=4,height=2)
    frame=dict(x=71,y=90,width=2,height=2)
    png=encode_png(b"\0\0\xff\0"*8,4,2,32,16)
    n._bound=({},dict(window_id="42"))
    n._identity=lambda _:{}
    n.geometry=lambda _:frame
    n._request=lambda *a,**k:dict(bounds=buffer,frame_bounds=frame,png=base64.b64encode(png).decode())
    result=n.capture({},str(tmp_path/"capture.png"))
    assert result["coordinate_origin"]==dict(x=70,y=90)
    assert result["bounds"]==buffer and result["frame_bounds"]==frame


def test_wayland_backend_is_lazy_and_never_selects_xwayland(monkeypatch):
    api=object.__new__(AtspiAPI)
    api._native=None
    calls=[]
    monkeypatch.setenv("WAYLAND_DISPLAY","wayland-0")
    monkeypatch.setenv("DISPLAY",":1")
    monkeypatch.setattr("qcu.layers._linux_wayland.GnomeNative",lambda:calls.append("wayland") or object())
    first=api.native
    assert api.native is first and calls==["wayland"]
