"""Lazy X11 capture/XTEST fallback. No compositor, clipboard or keymap changes."""
from __future__ import annotations

import math
import os
from pathlib import Path
import struct
import zlib

from qcu.layers.desktop_linux import LinuxError


def encode_png(data, width, height, bpp, stride, little_endian=True):
    if bpp not in (24, 32) or len(data) != stride*height or stride < width*(bpp//8):
        raise LinuxError("capture_format_unsupported", "Unsupported X11 pixel layout")
    pixel = bpp//8
    raw = b"".join(data[y*stride:y*stride+width*pixel] for y in range(height))
    rgb = bytearray(width*height*3)
    channels = (2, 1, 0) if little_endian else ((1, 2, 3) if pixel == 4 else (0, 1, 2))
    for channel, source in enumerate(channels):
        rgb[channel::3] = raw[source::pixel]
    rows = b"".join(b"\0"+rgb[y*width*3:(y+1)*width*3] for y in range(height))
    def chunk(kind, value):
        return struct.pack(">I", len(value))+kind+value+struct.pack(">I", zlib.crc32(kind+value)&0xffffffff)
    return b"\x89PNG\r\n\x1a\n"+chunk(b"IHDR", struct.pack(">IIBBBBB",width,height,8,2,0,0,0))+chunk(b"IDAT",zlib.compress(rows,3))+chunk(b"IEND",b"")


class X11Native:
    def __init__(self):
        if os.environ.get("WAYLAND_DISPLAY") or os.environ.get("XDG_SESSION_TYPE", "").lower() == "wayland":
            raise LinuxError("wayland_native_unsupported", "Wayland needs compositor/portal consent; AT-SPI semantic controls remain available")
        if not os.environ.get("DISPLAY"):
            raise LinuxError("display_missing", "X11 DISPLAY is unavailable")
        try:
            from Xlib import X, XK, display
            from Xlib.ext import xtest
            self.d = display.Display()
        except Exception as exc:
            raise LinuxError("x11_unavailable", str(exc)) from None
        self.X, self.XK, self.xtest = X, XK, xtest
        self.root, self._atoms, self._bound = self.d.screen().root, {}, None

    def atom(self, name):
        if name not in self._atoms:
            self._atoms[name] = self.d.intern_atom(name)
        return self._atoms[name]

    def prop(self, window, name):
        value = window.get_full_property(self.atom(name), self.X.AnyPropertyType)
        return value.value if value is not None else None

    def title(self, window):
        value = self.prop(window, "_NET_WM_NAME")
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else (window.get_wm_name() or "")

    def bind(self, target, title, bounds):
        if self._bound and self._bound[0] == target:
            self.geometry(target)
            return
        candidates = []
        ids = self.prop(self.root, "_NET_CLIENT_LIST")
        if ids is None:
            raise LinuxError("window_manager_unsupported", "An EWMH window manager is required for exact X11 binding")
        for xid in ids:
            window = self.d.create_resource_object("window", int(xid))
            pid = self.prop(window, "_NET_WM_PID")
            if pid is not None and len(pid) and int(pid[0]) == target["pid"] and self.title(window) == title:
                candidates.append(window)
        if len(candidates) != 1:
            raise LinuxError("x11_window_ambiguous" if candidates else "x11_window_not_found", "Cannot uniquely match AT-SPI PID/title to an X11 client")
        self._bound = (dict(target), candidates[0])
        self.geometry(target)

    def geometry(self, target):
        if not self._bound or self._bound[0] != target:
            raise LinuxError("target_mismatch", "X11 target differs from observation")
        window = self._bound[1]
        from qcu.layers.desktop_linux import process_identity
        live = process_identity(target["pid"])
        if any(live[k] != target[k] for k in ("process_started", "process_image")):
            raise LinuxError("target_changed", "Bound process lifetime/executable changed")
        pid = self.prop(window, "_NET_WM_PID")
        if pid is None or not len(pid) or int(pid[0]) != target["pid"]:
            raise LinuxError("target_changed", "X11 client PID changed")
        if window.get_attributes().map_state != self.X.IsViewable:
            raise LinuxError("window_not_visible", "X11 client is not viewable")
        g = window.get_geometry()
        # python-xlib translate_coords is invoked on the DESTINATION window.
        position = self.root.translate_coords(window, 0, 0)
        if not 0 < g.width <= 16384 or not 0 < g.height <= 16384 or g.width*g.height > 32000000:
            raise LinuxError("capture_too_large", "Invalid or oversized X11 window")
        return dict(x=position.x, y=position.y, width=g.width, height=g.height)

    def _owns(self, window):
        bound = self._bound[1].id
        for _ in range(64):
            if not hasattr(window, "id") or window.id == self.root.id:
                return False
            if window.id == bound:
                return True
            window = window.query_tree().parent
        return False

    def _foreground(self, target):
        self.geometry(target)
        active = self.prop(self.root, "_NET_ACTIVE_WINDOW")
        if active is not None and len(active) and int(active[0]) != self._bound[1].id:
            raise LinuxError("foreground_mismatch", "Bound window is not active; QCU never activates it")
        if not self._owns(self.d.get_input_focus().focus):
            raise LinuxError("focus_mismatch", "Keyboard focus is outside the bound window")

    def _at_point(self, x, y):
        window = self.root
        for _ in range(32):
            child = None
            for item in reversed(window.query_tree().children):
                if item.get_attributes().map_state != self.X.IsViewable:
                    continue
                g = item.get_geometry()
                p = self.root.translate_coords(item, 0, 0)
                if p.x <= x < p.x+g.width and p.y <= y < p.y+g.height:
                    child = item
                    break
            if child is None:
                return window
            window = child
        raise LinuxError("ownership_unavailable", "X11 child traversal exceeded its bound")

    def _point(self, target, x, y):
        if type(x) not in (int, float) or type(y) not in (int, float) or not math.isfinite(x) or not math.isfinite(y):
            raise LinuxError("invalid_coordinates", "Coordinates must be finite numbers")
        g = self.geometry(target)
        if not g["x"] <= x < g["x"]+g["width"] or not g["y"] <= y < g["y"]+g["height"]:
            raise LinuxError("outside_target", "Pointer point is outside the bound window")
        if not self._owns(self._at_point(int(x), int(y))):
            raise LinuxError("point_occluded", "Another X11 window covers this point")
        return int(x), int(y)

    def _idle(self):
        if self.root.query_pointer().mask & sum(getattr(self.X, f"Button{i}Mask") for i in range(1,6)):
            raise LinuxError("input_busy", "A pointer button is already held")
        state = self.d.query_keymap()
        for name in ("Shift_L", "Shift_R", "Control_L", "Control_R", "Alt_L", "Alt_R", "Super_L", "Super_R", "Meta_L", "Meta_R"):
            code = self.d.keysym_to_keycode(self.XK.string_to_keysym(name))
            if code and state[code//8] & (1 << (code%8)):
                raise LinuxError("input_busy", "A keyboard modifier is already held")

    def capture(self, target, path):
        if not isinstance(path, str) or not path:
            raise LinuxError("invalid_path", "Screenshot path must be a non-empty string")
        g = self.geometry(target)
        # Reject every mapped root sibling above the client's WM frame that
        # intersects the capture, rather than trusting a few sampled pixels.
        frame = self._bound[1]
        for _ in range(64):
            parent = frame.query_tree().parent
            if parent.id == self.root.id:
                break
            frame = parent
        else:
            raise LinuxError("ownership_unavailable", "Cannot resolve the client WM frame")
        siblings = self.root.query_tree().children
        ids = [w.id for w in siblings]
        if frame.id not in ids:
            raise LinuxError("target_changed", "Client frame disappeared")
        for sibling in siblings[ids.index(frame.id)+1:]:
            if sibling.get_attributes().map_state != self.X.IsViewable:
                continue
            size = sibling.get_geometry()
            pos = self.root.translate_coords(sibling,0,0)
            if (pos.x < g["x"]+g["width"] and pos.x+size.width > g["x"]
                    and pos.y < g["y"]+g["height"] and pos.y+size.height > g["y"]):
                raise LinuxError("point_occluded", "Another X11 window intersects the capture")
        for x, y in ((g["x"]+1,g["y"]+1),(g["x"]+g["width"]-2,g["y"]+g["height"]-2),
                     (g["x"]+g["width"]//2,g["y"]+g["height"]//2)):
            self._point(target,x,y)
        window = self._bound[1]
        visual = window.get_attributes().visual
        visuals = [v for depth in self.d.screen().allowed_depths for v in depth.visuals if v.visual_id == visual]
        if len(visuals)!=1 or (visuals[0].red_mask,visuals[0].green_mask,visuals[0].blue_mask)!=(0xff0000,0xff00,0xff):
            raise LinuxError("capture_format_unsupported", "Only TrueColor RGB X11 visuals are supported")
        image = window.get_image(0,0,g["width"],g["height"],self.X.ZPixmap,0xffffffff)
        formats = [f for f in self.d.display.info.pixmap_formats if f.depth == image.depth]
        if len(formats)!=1:
            raise LinuxError("capture_format_unsupported", "Unknown X11 pixmap depth")
        fmt = formats[0]
        stride = ((g["width"]*fmt.bits_per_pixel+fmt.scanline_pad-1)//fmt.scanline_pad)*(fmt.scanline_pad//8)
        png = encode_png(image.data,g["width"],g["height"],fmt.bits_per_pixel,stride,self.d.display.info.image_byte_order==0)
        if self.geometry(target)!=g:
            raise LinuxError("target_changed", "Window moved during capture")
        output = Path(path).expanduser().absolute()
        output.parent.mkdir(parents=True,exist_ok=True)
        output.write_bytes(png)
        return dict(path=str(output),target=dict(target),x11_window_id=window.id,bounds=g,
                    coordinate_origin=dict(x=g["x"],y=g["y"]),coordinate_scale=1.,coordinate_space="x11_screen_pixels")

    def _keys(self, key):
        if not isinstance(key,str) or not key:
            raise LinuxError("invalid_key", "key must be a named key or shortcut")
        names = {"Ctrl":"Control_L","Control":"Control_L","Alt":"Alt_L","Shift":"Shift_L",
                 "Super":"Super_L","Meta":"Super_L","Enter":"Return","Escape":"Escape",
                 "Backspace":"BackSpace","Delete":"Delete","Space":"space","PageDown":"Next","PageUp":"Prior"}
        parts = key.split("+")
        if len(parts)>5 or any(p not in {"Ctrl","Control","Alt","Shift","Super","Meta"} for p in parts[:-1]):
            raise LinuxError("unsupported_key", "Unsupported shortcut")
        codes = []
        for part in parts:
            symbol = self.XK.string_to_keysym(names.get(part,part))
            code = self.d.keysym_to_keycode(symbol) if symbol else 0
            if not code:
                raise LinuxError("unsupported_key", "Key is absent from the existing X11 keymap; use semantic fill for Unicode")
            mapping = self.d.get_keyboard_mapping(code,1)[0]
            if part == parts[-1] and mapping[0] != symbol:
                if len(mapping)>1 and mapping[1] == symbol:
                    shift = self.d.keysym_to_keycode(self.XK.string_to_keysym("Shift_L"))
                    if shift not in codes:
                        codes.append(shift)
                else:
                    raise LinuxError("unsupported_key", "Key needs an unsupported layout group")
            if code not in codes:
                codes.append(code)
        return codes

    def dispatch(self, target, kind, p):
        self._foreground(target)
        self._idle()
        if not self.d.has_extension("XTEST"):
            raise LinuxError("xtest_unavailable", "X11 server does not expose XTEST")
        before = self.geometry(target)
        points, keys, buttons = [], [], []
        if kind == "press_key":
            keys = self._keys(p.get("key"))
        else:
            if kind == "drag":
                start, end = p.get("from"), p.get("to")
                x,y = (start.get("x"),start.get("y")) if isinstance(start,dict) else (p.get("x1",p.get("x")),p.get("y1",p.get("y")))
                x2,y2 = (end.get("x"),end.get("y")) if isinstance(end,dict) else (p.get("x2"),p.get("y2"))
                x,y = self._point(target,x,y)
                x2,y2 = self._point(target,x2,y2)
                steps = p.get("steps",8)
                if type(steps) is not int or not 2<=steps<=64:
                    raise LinuxError("invalid_action", "Drag steps must be in 2..64")
                points = [self._point(target,x+(x2-x)*i/steps,y+(y2-y)*i/steps) for i in range(steps+1)]
            else:
                points = [self._point(target,p.get("x"),p.get("y"))]
            if kind in {"click","double_click","right_click"}:
                buttons = [3 if kind=="right_click" else 1]*(2 if kind=="double_click" else 1)
            elif kind == "scroll":
                for axis,positive,negative in (("dy",5,4),("dx",7,6)):
                    delta = p.get(axis,0)
                    if type(delta) not in (int,float) or not math.isfinite(delta) or abs(delta)>2400:
                        raise LinuxError("invalid_action", "Wheel deltas must be finite and at most 2400")
                    buttons.extend([positive if delta>0 else negative]*math.ceil(abs(delta)/120))
                if not buttons:
                    raise LinuxError("invalid_action", "A nonzero wheel delta is required")
            elif kind not in {"hover","drag"}:
                raise LinuxError("unsupported", f"Unsupported X11 action: {kind}")
        sent, held_keys, held_buttons = False, [], []
        def send(event,detail=0,**kw):
            nonlocal sent
            self._foreground(target)
            if self.geometry(target)!=before:
                raise LinuxError("target_changed", "Window moved during input")
            sent = True
            self.xtest.fake_input(self.d,event,detail,**kw)
            self.d.sync()
        try:
            if keys:
                for code in keys:
                    held_keys.append(code)
                    send(self.X.KeyPress,code)
                for code in reversed(keys):
                    send(self.X.KeyRelease,code)
                    held_keys.remove(code)
            else:
                for i,(x,y) in enumerate(points):
                    self._point(target,x,y)
                    send(self.X.MotionNotify,x=x,y=y)
                    pointer = self.root.query_pointer()
                    if (pointer.root_x,pointer.root_y)!=(x,y):
                        raise LinuxError("pointer_mismatch", "X11 pointer position did not match")
                    if kind=="drag" and i==0:
                        held_buttons.append(1)
                        send(self.X.ButtonPress,1)
                if kind=="drag":
                    send(self.X.ButtonRelease,1)
                    held_buttons.clear()
                for button in buttons:
                    held_buttons.append(button)
                    send(self.X.ButtonPress,button)
                    send(self.X.ButtonRelease,button)
                    held_buttons.clear()
            return dict(target=dict(target),method="X11.XTEST",retry_safe=False,input_mode="native")
        except Exception as exc:
            raise LinuxError("outcome_unknown" if sent else getattr(exc,"reason","x11_error"),str(exc),dispatched=sent) from None
        finally:
            # Release only events attempted by THIS action; never replay actions.
            try:
                for code in reversed(held_keys):
                    self.xtest.fake_input(self.d,self.X.KeyRelease,code)
                for button in held_buttons:
                    self.xtest.fake_input(self.d,self.X.ButtonRelease,button)
                if held_keys or held_buttons:
                    self.d.sync()
            except Exception as exc:
                raise LinuxError("outcome_unknown", "Input release could not be confirmed: "+str(exc),dispatched=True) from None

    def close(self):
        self.d.close()
