"""X11 pure pixel/event contracts, independent of a running X server."""
from types import SimpleNamespace
import zlib

import pytest

from qcu.layers._linux_x11 import X11Native, encode_png
from qcu.layers.desktop_linux import LinuxError


@pytest.mark.parametrize("bpp,little,raw",[(32,True,b"\x00\x00\xff\x00"),
    (32,False,b"\x00\xff\x00\x00"),(24,True,b"\x00\x00\xff\x00"),(24,False,b"\xff\x00\x00\x00")])
def test_png_red_pixel_with_endianness_and_padding(bpp,little,raw):
    png=encode_png(raw,1,1,bpp,4,little)
    assert png[:8]==b"\x89PNG\r\n\x1a\n"
    offset=png.index(b"IDAT")
    length=int.from_bytes(png[offset-4:offset],"big")
    assert zlib.decompress(png[offset+4:offset+4+length])==b"\x00\xff\x00\x00"


def test_wayland_never_silently_uses_xwayland(monkeypatch):
    monkeypatch.setenv("WAYLAND_DISPLAY","wayland-0")
    monkeypatch.setenv("DISPLAY",":0")
    with pytest.raises(LinuxError,match="Wayland") as exc:
        X11Native()
    assert exc.value.reason=="wayland_native_unsupported"


def fake_native():
    native=object.__new__(X11Native)
    native.geometry=lambda _:dict(x=-100,y=20,width=200,height=100)
    native._at_point=lambda x,y: "owned"
    native._owns=lambda window: window=="owned"
    return native


def test_negative_origin_and_occlusion():
    native=fake_native()
    assert native._point({},-50,30)==(-50,30)
    native._at_point=lambda x,y:"other"
    with pytest.raises(LinuxError) as exc:
        native._point({},-50,30)
    assert exc.value.reason=="point_occluded"


@pytest.mark.parametrize("x,y",[(float("nan"),30),(float("inf"),30),(True,30),(100,30),(-101,30),(0,120)])
def test_invalid_and_outside_points_never_move_pointer(x,y):
    with pytest.raises(LinuxError):
        fake_native()._point({},x,y)


def test_partial_key_packet_releases_only_owned_keys_and_does_not_replay():
    n=fake_native()
    n._foreground=lambda _:None
    n._idle=lambda:None
    n._keys=lambda _: [37,38]
    n.X=SimpleNamespace(KeyPress=2,KeyRelease=3)
    sent=[]
    def send(display,event,detail,**kw):
        sent.append((event,detail))
        if len(sent)==2:
            raise RuntimeError("late X11 error")
    n.xtest=SimpleNamespace(fake_input=send)
    n.d=SimpleNamespace(has_extension=lambda _:True,sync=lambda:None)
    with pytest.raises(LinuxError) as exc:
        n.dispatch({},"press_key",dict(key="Ctrl+a"))
    assert exc.value.dispatched and exc.value.reason=="outcome_unknown"
    assert sent==[(2,37),(2,38),(3,38),(3,37)]
