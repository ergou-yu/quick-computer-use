"""Lazy target-bound GDI capture and SendInput, in physical screen pixels.

Only loaded for explicit native fallback. No shell, clipboard, OCR dependency,
process-wide DPI change, automatic activation or action replay.
"""
from __future__ import annotations

import contextlib
import ctypes as c
from ctypes import wintypes as w
import math
from pathlib import Path
import struct
import zlib


class NativeError(RuntimeError):
    def __init__(self, reason, message, *, dispatched=False, **data):
        super().__init__(message)
        self.reason, self.dispatched, self.data = reason, dispatched, data


class MOUSEINPUT(c.Structure):
    _fields_ = [("dx", c.c_int32), ("dy", c.c_int32), ("mouseData", c.c_uint32),
                ("dwFlags", c.c_uint32), ("time", c.c_uint32), ("dwExtraInfo", c.c_size_t)]


class KEYBDINPUT(c.Structure):
    _fields_ = [("wVk", c.c_uint16), ("wScan", c.c_uint16), ("dwFlags", c.c_uint32),
                ("time", c.c_uint32), ("dwExtraInfo", c.c_size_t)]


class _InputUnion(c.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT)]


class INPUT(c.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", c.c_uint32), ("u", _InputUnion)]


class GUIINFO(c.Structure):
    _fields_ = [("cbSize", w.DWORD), ("flags", w.DWORD), ("hwndActive", w.HWND),
                ("hwndFocus", w.HWND), ("hwndCapture", w.HWND), ("hwndMenuOwner", w.HWND),
                ("hwndMoveSize", w.HWND), ("hwndCaret", w.HWND), ("rcCaret", w.RECT)]


_KEYS = {"enter": 13, "return": 13, "tab": 9, "escape": 27, "esc": 27,
         "space": 32, "backspace": 8, "delete": 46, "insert": 45,
         "home": 36, "end": 35, "pageup": 33, "pagedown": 34,
         "left": 37, "up": 38, "right": 39, "down": 40}
_KEYS.update({f"f{i}": 111+i for i in range(1, 25)})
_MODS = {"ctrl": 17, "control": 17, "alt": 18, "shift": 16, "win": 91, "windows": 91}


def key_events(key: str) -> list[INPUT]:
    if not isinstance(key, str) or not key:
        raise NativeError("invalid_key", "press_key requires a named key or combo")
    pieces = key.casefold().split("+")
    if any(p not in _MODS for p in pieces[:-1]):
        raise NativeError("invalid_key", "Unknown Windows modifier")
    mods = list(dict.fromkeys(_MODS[p] for p in pieces[:-1]))
    primary = pieces[-1]
    vk = ord(primary.upper()) if len(primary) == 1 and primary.isascii() and primary.isalnum() else _KEYS.get(primary)
    if vk is None:
        raise NativeError("invalid_key", "Unknown Windows key")
    extended = 1 if vk in {33, 34, 35, 36, 37, 38, 39, 40, 45, 46} else 0
    def event(code, flags=0):
        return INPUT(type=1, ki=KEYBDINPUT(wVk=code, dwFlags=flags))
    return ([event(code) for code in mods] + [event(vk, extended), event(vk, extended | 2)]
            + [event(code, 2) for code in reversed(mods)])


def unicode_events(text: str) -> list[INPUT]:
    if not isinstance(text, str) or not text or len(text.encode("utf-16-le")) > 8192:
        raise NativeError("invalid_text", "type needs 1..4096 UTF-16 code units")
    units = struct.unpack("<" + "H" * (len(text.encode("utf-16-le"))//2), text.encode("utf-16-le"))
    return [INPUT(type=1, ki=KEYBDINPUT(wScan=unit, dwFlags=flags))
            for unit in units for flags in (4, 6)]


def _png(width, height, bgr, stride):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind+data))
    rows = []
    for y in range(height):
        row = bgr[y*stride:y*stride+width*3]
        rgb = bytearray(width*3)
        rgb[0::3], rgb[1::3], rgb[2::3] = row[2::3], row[1::3], row[0::3]
        rows.append(b"\0"+rgb)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 1)) + chunk(b"IEND", b""))


class WindowsNative:
    def __init__(self):
        self.user = c.WinDLL("user32", use_last_error=True)
        self.gdi = c.WinDLL("gdi32", use_last_error=True)
        # Pointer-sized handles and INPUT alignment matter on 64-bit Windows.
        signatures = {
            "IsWindow": ([w.HWND], w.BOOL), "IsWindowVisible": ([w.HWND], w.BOOL),
            "IsIconic": ([w.HWND], w.BOOL), "IsWindowEnabled": ([w.HWND], w.BOOL),
            "GetWindowRect": ([w.HWND, c.POINTER(w.RECT)], w.BOOL),
            "GetWindowThreadProcessId": ([w.HWND, c.POINTER(w.DWORD)], w.DWORD),
            "GetForegroundWindow": ([], w.HWND), "GetAncestor": ([w.HWND, w.UINT], w.HWND),
            "WindowFromPoint": ([w.POINT], w.HWND), "IsChild": ([w.HWND, w.HWND], w.BOOL),
            "GetCursorPos": ([c.POINTER(w.POINT)], w.BOOL),
            "SetCursorPos": ([c.c_int, c.c_int], w.BOOL),
            "GetGUIThreadInfo": ([w.DWORD, c.POINTER(GUIINFO)], w.BOOL),
            "PostMessageW": ([w.HWND,w.UINT,w.WPARAM,w.LPARAM],w.BOOL),
            "MapVirtualKeyW": ([w.UINT,w.UINT],w.UINT),
            "ScreenToClient": ([w.HWND,c.POINTER(w.POINT)],w.BOOL),
            "GetClassNameW": ([w.HWND,w.LPWSTR,c.c_int],c.c_int),
            "SendMessageTimeoutW": ([w.HWND,w.UINT,w.WPARAM,w.LPARAM,w.UINT,w.UINT,c.POINTER(c.c_size_t)],c.c_ssize_t),
            "GetAsyncKeyState": ([c.c_int], w.SHORT), "GetSystemMetrics": ([c.c_int], c.c_int),
            "SendInput": ([w.UINT, c.POINTER(INPUT), c.c_int], w.UINT),
            "SetThreadDpiAwarenessContext": ([c.c_void_p], c.c_void_p),
            "GetWindowDC": ([w.HWND], w.HDC), "ReleaseDC": ([w.HWND, w.HDC], c.c_int),
            "PrintWindow": ([w.HWND, w.HDC, w.UINT], w.BOOL),
            "OpenInputDesktop": ([w.DWORD, w.BOOL, w.DWORD], w.HANDLE),
            "GetUserObjectInformationW": ([w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)], w.BOOL),
            "CloseDesktop": ([w.HANDLE], w.BOOL),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(self.user, name)
            fn.argtypes, fn.restype = args, result
        for name, args, result in (
            ("CreateCompatibleDC", [w.HDC], w.HDC),
            ("CreateCompatibleBitmap", [w.HDC, c.c_int, c.c_int], w.HBITMAP),
            ("SelectObject", [w.HDC, w.HANDLE], w.HANDLE),
            ("DeleteObject", [w.HANDLE], w.BOOL), ("DeleteDC", [w.HDC], w.BOOL),
            ("GetDIBits", [w.HDC, w.HBITMAP, w.UINT, w.UINT, c.c_void_p, c.c_void_p, w.UINT], c.c_int),
        ):
            fn = getattr(self.gdi, name)
            fn.argtypes, fn.restype = args, result

    @contextlib.contextmanager
    def physical_pixels(self):
        old = self.user.SetThreadDpiAwarenessContext(c.c_void_p(-4))  # PER_MONITOR_AWARE_V2
        if not old:
            raise NativeError("dpi_context_unavailable", "Cannot establish physical-pixel coordinates")
        try:
            yield
        finally:
            self.user.SetThreadDpiAwarenessContext(old)

    def _desktop(self):
        desktop = self.user.OpenInputDesktop(0, False, 1)  # DESKTOP_READOBJECTS
        if not desktop:
            raise NativeError("input_desktop_unavailable", "Interactive desktop is inaccessible")
        try:
            name, size = c.create_unicode_buffer(256), w.DWORD()
            if not self.user.GetUserObjectInformationW(desktop, 2, name, c.sizeof(name), c.byref(size)) or name.value.casefold() != "default":
                raise NativeError("input_desktop_unavailable", "Secure or non-default desktop is unsupported")
        finally:
            self.user.CloseDesktop(desktop)

    def _pid(self, hwnd):
        pid = w.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, c.byref(pid))
        return pid.value

    def geometry(self, target):
        self._desktop()
        hwnd = target["window_id"]
        if not self.user.IsWindow(hwnd) or self._pid(hwnd) != target["pid"]:
            raise NativeError("target_changed", "Bound window no longer belongs to the process")
        if not self.user.IsWindowVisible(hwnd) or self.user.IsIconic(hwnd):
            raise NativeError("target_not_visible", "Window is hidden or minimized")
        with self.physical_pixels():
            rect = w.RECT()
            if not self.user.GetWindowRect(hwnd, c.byref(rect)):
                raise NativeError("geometry_unavailable", "Cannot read target bounds")
            bounds = dict(X=rect.left, Y=rect.top, Width=rect.right-rect.left, Height=rect.bottom-rect.top)
            if not 0 < bounds["Width"] <= 16384 or not 0 < bounds["Height"] <= 16384 or bounds["Width"]*bounds["Height"] > 32000000:
                raise NativeError("invalid_geometry", "Window capture size exceeds the bound")
            return bounds

    def capture(self, target, path):
        bounds = self.geometry(target)
        hwnd, width, height = target["window_id"], bounds["Width"], bounds["Height"]
        with self.physical_pixels():
            dc = self.user.GetWindowDC(hwnd)
            memory = bitmap = old = None
            try:
                memory = self.gdi.CreateCompatibleDC(dc)
                bitmap = self.gdi.CreateCompatibleBitmap(dc, width, height)
                if not dc or not memory or not bitmap:
                    raise NativeError("capture_failed", "GDI allocation failed")
                old = self.gdi.SelectObject(memory, bitmap)
                if not self.user.PrintWindow(hwnd, memory, 2):
                    raise NativeError("capture_failed", "Window provider refused PrintWindow")
                self.gdi.SelectObject(memory, old)
                old = None
                stride = (width*3+3) & ~3
                pixels = c.create_string_buffer(stride*height)
                # Top-down 24-bit BI_RGB, aligned scanlines; no Pillow dependency.
                header = c.create_string_buffer(struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 24, 0, stride*height, 0, 0, 0, 0)+b"\0"*12)
                if self.gdi.GetDIBits(memory, bitmap, 0, height, pixels, header, 0) != height:
                    raise NativeError("capture_failed", "GDI bitmap readback failed")
                raw = pixels.raw
                if not any(raw):
                    raise NativeError("capture_empty", "Provider returned a black/empty capture")
            finally:
                if old and memory:
                    self.gdi.SelectObject(memory, old)
                if bitmap:
                    self.gdi.DeleteObject(bitmap)
                if memory:
                    self.gdi.DeleteDC(memory)
                if dc:
                    self.user.ReleaseDC(hwnd, dc)
        if self.geometry(target) != bounds:
            raise NativeError("target_changed", "Window moved during capture; capture again")
        output = Path(path)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(_png(width, height, raw, stride))
        return dict(path=str(output), capture_method="Win32.PrintWindow/GDI", bounds=bounds,
                    coordinate_origin=dict(x=bounds["X"], y=bounds["Y"]), coordinate_scale=1.0,
                    coordinate_space="physical_screen_pixels", width=width, height=height)

    def _point_gate(self, target, bounds, point):
        x, y = point
        if not math.isfinite(x) or not math.isfinite(y) or not (bounds["X"] <= x < bounds["X"]+bounds["Width"] and bounds["Y"] <= y < bounds["Y"]+bounds["Height"]):
            raise NativeError("point_outside_target", "Point lies outside the bound window")
        hit = self.user.WindowFromPoint(w.POINT(round(x), round(y)))
        if not hit or self.user.GetAncestor(hit, 2) != target["window_id"] or self._pid(hit) != target["pid"]:
            raise NativeError("target_occluded", "Another window owns this screen point")

    def focus(self, target):
        hwnd = self.user.GetForegroundWindow()
        if hwnd != target["window_id"] or self._pid(hwnd) != target["pid"]:
            raise NativeError("foreground_mismatch", "Native input requires the bound window in the foreground",
                              foreground_window=hwnd, foreground_pid=self._pid(hwnd) if hwnd else None)
        tid = self.user.GetWindowThreadProcessId(hwnd, None)
        info = GUIINFO(cbSize=c.sizeof(GUIINFO))
        if not self.user.GetGUIThreadInfo(tid, c.byref(info)) or not info.hwndFocus or self.user.GetAncestor(info.hwndFocus, 2) != hwnd or self._pid(info.hwndFocus) != target["pid"]:
            raise NativeError("focus_unavailable", "Cannot prove keyboard focus belongs to the bound window")
        return info.hwndFocus

    def dispatch(self, target, kind, params, *, focus_hwnd=None):
        transport=params.get("input_transport","window_message")
        if transport == "window_message":
            self._message_may_sent=False
            try:
                return self._message_dispatch(target,kind,params,focus_hwnd=focus_hwnd)
            except NativeError as exc:
                if self._message_may_sent and exc.reason != "outcome_unknown":
                    raise NativeError("outcome_unknown",str(exc),dispatched=True,cause=exc.reason,**exc.data) from None
                raise
            except Exception as exc:
                raise NativeError("outcome_unknown" if self._message_may_sent else "native_input_failed",
                                  str(exc),dispatched=self._message_may_sent) from None
        if transport != "send_input":
            raise NativeError("invalid_transport", "input_transport must be window_message or send_input")
        bounds = self.geometry(target)
        with self.physical_pixels():
            live_focus = self.focus(target)
            if focus_hwnd and live_focus != focus_hwnd and not self.user.IsChild(focus_hwnd, live_focus):
                raise NativeError("focus_mismatch", "Focused control differs from the requested ref")
            if any(self.user.GetAsyncKeyState(key) & 0x8000 for key in (1, 2, 4, 16, 17, 18, 91, 92)):
                raise NativeError("input_busy", "Physical buttons/modifiers are pressed")
            if not self.user.IsWindowEnabled(target["window_id"]):
                raise NativeError("control_disabled", "Bound window is disabled")
            if kind == "type":
                events = unicode_events(params.get("text"))
            elif kind == "press_key":
                events = key_events(params.get("key"))
            else:
                point = float(params["x"]), float(params["y"])
                self._point_gate(target, bounds, point)
                events = []
                def mouse(flags, data=0):
                    return INPUT(type=0, mi=MOUSEINPUT(dwFlags=flags, mouseData=data & 0xFFFFFFFF))
                if kind in {"click", "double_click", "right_click"}:
                    down, up = (8, 16) if kind == "right_click" else (2, 4)
                    for _ in range(2 if kind == "double_click" else 1):
                        events += [mouse(down), mouse(up)]
                elif kind == "scroll":
                    dx, dy = params.get("dx", 0), params.get("dy", 0)
                    if any(type(value) is not int or abs(value) > 12000 for value in (dx, dy)) or not (dx or dy):
                        raise NativeError("invalid_scroll", "dx/dy need bounded integer wheel units (120 per notch)")
                    if dy:
                        events.append(mouse(0x800, -dy))
                    if dx:
                        events.append(mouse(0x1000, dx))
                elif kind == "drag":
                    end = float(params["x2"]), float(params["y2"])
                    self._point_gate(target, bounds, end)
                    # Check the whole straight path so a middle overlay is not crossed.
                    path = [(point[0]+(end[0]-point[0])*i/16, point[1]+(end[1]-point[1])*i/16) for i in range(1, 17)]
                    for intermediate in path:
                        self._point_gate(target, bounds, intermediate)
                    events = [mouse(2)]
                elif kind != "hover":
                    raise NativeError("unsupported", "Unknown native input action")
            # Recheck right at the only input boundary; never activate or retry.
            if self.geometry(target) != bounds or self.focus(target) != live_focus:
                raise NativeError("target_changed", "Window geometry/focus changed before input")
            def move_cursor(location):
                self._point_gate(target, bounds, location)
                if self.focus(target) != live_focus:
                    raise NativeError("outcome_unknown", "Foreground changed during pointer input", dispatched=True)
                if not self.user.SetCursorPos(round(location[0]), round(location[1])):
                    raise NativeError("outcome_unknown", "Cursor movement was not acknowledged", dispatched=True)
                actual=w.POINT()
                if not self.user.GetCursorPos(c.byref(actual)) or (actual.x,actual.y) != (round(location[0]),round(location[1])):
                    raise NativeError("outcome_unknown", "Cursor position was not confirmed; no button sent", dispatched=True)
            if kind not in {"type", "press_key"}:
                # Direct physical coordinates avoid driver/RDP normalization
                # differences. Confirm the live position before button events.
                move_cursor(point)
                self._point_gate(target, bounds, point)
                if self.focus(target) != live_focus:
                    raise NativeError("outcome_unknown", "Foreground changed after cursor movement", dispatched=True)
            if not events:
                return dict(method="Win32.SetCursorPos", requested_events=0, inserted_events=0,
                            foreground_input_required=True, coordinate_space="physical_screen_pixels")
            array = (INPUT * len(events))(*events)
            try:
                inserted = self.user.SendInput(len(events), array, c.sizeof(INPUT))
            except Exception as exc:
                raise NativeError("outcome_unknown", str(exc), dispatched=True) from None
            if inserted != len(events):
                # Only release keys/buttons potentially left down, never replay input.
                releases = [e for e in events if (e.type == 1 and e.ki.dwFlags & 2)
                            or (e.type == 0 and e.mi.dwFlags in (4, 16))]
                if inserted and releases:
                    with contextlib.suppress(Exception):
                        self.user.SendInput(len(releases), (INPUT * len(releases))(*releases), c.sizeof(INPUT))
                raise NativeError("outcome_unknown", "SendInput acknowledgement incomplete; inspect before another action",
                                  dispatched=True, requested=len(events), inserted=inserted)
            if kind == "drag":
                try:
                    for intermediate in path:
                        if self.geometry(target) != bounds:
                            raise NativeError("outcome_unknown", "Window moved during drag", dispatched=True)
                        move_cursor(intermediate)
                except Exception as exc:
                    raise NativeError("outcome_unknown", str(exc), dispatched=True) from None
                finally:
                    # Release our pressed button even when a later drag gate fails.
                    try:
                        released=self.user.SendInput(1, (INPUT * 1)(mouse(4)), c.sizeof(INPUT))
                    except Exception as exc:
                        raise NativeError("outcome_unknown", "Drag button release failed: "+str(exc), dispatched=True) from None
                    if released != 1:
                        raise NativeError("outcome_unknown", "Drag button release not acknowledged", dispatched=True)
            position=w.POINT()
            self.user.GetCursorPos(c.byref(position))
            return dict(method="Win32.SendInput", requested_events=len(events), inserted_events=inserted,
                        cursor=dict(x=position.x,y=position.y),
                        foreground_input_required=True, coordinate_space="physical_screen_pixels")

    def _message_dispatch(self,target,kind,params,*,focus_hwnd=None):
        bounds=self.geometry(target)
        sent=0
        moved=False
        with self.physical_pixels():
            focus=self.focus(target)
            if focus_hwnd and focus != focus_hwnd and not self.user.IsChild(focus_hwnd,focus):
                raise NativeError("focus_mismatch","Focused control differs from the requested ref")
            if any(self.user.GetAsyncKeyState(key)&0x8000 for key in (1,2,4,16,17,18,91,92)):
                raise NativeError("input_busy","Physical buttons/modifiers are pressed")

            def live(hwnd):
                if (self._pid(hwnd)!=target["pid"] or self.user.GetAncestor(hwnd,2)!=target["window_id"]
                    or (kind in {"type","press_key"} and self.focus(target)!=focus) or self.geometry(target)!=bounds):
                    raise NativeError("target_changed","Input window/focus changed",dispatched=bool(sent or moved))
                self.focus(target)  # Always prove foreground ownership, allowing pointer focus changes.
                if not self.user.IsWindowEnabled(hwnd):
                    raise NativeError("control_disabled","Input control is disabled",dispatched=bool(sent or moved))
            def post(hwnd,message,value=0,detail=0):
                nonlocal sent
                live(hwnd)
                try:
                    self._message_may_sent=True
                    acknowledged=self.user.PostMessageW(hwnd,message,value,detail)
                except Exception as exc:
                    raise NativeError("outcome_unknown",str(exc),dispatched=True) from None
                if not acknowledged:
                    raise NativeError("outcome_unknown","Window message acknowledgement missing",dispatched=True,posted_messages=sent)
                sent+=1
            def location(point):
                nonlocal moved
                self._point_gate(target,bounds,point)
                if self.focus(target)!=focus and kind in {"type","press_key"}:
                    raise NativeError("foreground_mismatch","Foreground changed before pointer input",dispatched=bool(sent or moved))
                moved=True
                self._message_may_sent=True
                if not self.user.SetCursorPos(round(point[0]),round(point[1])):
                    raise NativeError("outcome_unknown","Cursor movement unconfirmed",dispatched=True)
                actual=w.POINT()
                if not self.user.GetCursorPos(c.byref(actual)) or (actual.x,actual.y)!=(round(point[0]),round(point[1])):
                    raise NativeError("outcome_unknown","Cursor readback differs; no button sent",dispatched=True)
                hwnd=self.user.WindowFromPoint(actual)
                live(hwnd)
                client=w.POINT(actual.x,actual.y)
                if not self.user.ScreenToClient(hwnd,c.byref(client)):
                    raise NativeError("outcome_unknown","Client coordinates unavailable",dispatched=True)
                return hwnd,(client.x&0xFFFF)|((client.y&0xFFFF)<<16)
            selection=None
            if kind == "type":
                # WM_CHAR uses UTF-16 code units, including surrogate pairs.
                for event in unicode_events(params.get("text"))[::2]:
                    post(focus,0x102,event.ki.wScan,1)
            elif kind == "press_key":
                key=params.get("key","")
                events=key_events(key)
                if "+" in key:
                    name=c.create_unicode_buffer(256)
                    self.user.GetClassNameW(focus,name,256)
                    class_name=name.value.casefold()
                    standard_edit=(class_name=="edit" or class_name.startswith("windowsforms10.edit.") or class_name.startswith("richedit"))
                    if key.casefold() not in {"ctrl+a","control+a"} or not standard_edit:
                        raise NativeError("unsupported_shortcut","This shortcut requires explicit input_transport=send_input")
                    live(focus)
                    result=c.c_size_t()
                    self._message_may_sent=True
                    if not self.user.SendMessageTimeoutW(focus,0xB1,0,-1,2,200,c.byref(result)):
                        raise NativeError("outcome_unknown","Edit selection timed out",dispatched=True)
                    sent=1
                    length=c.c_size_t(); selected=c.c_size_t()
                    if (self.user.SendMessageTimeoutW(focus,0xE,0,0,2,200,c.byref(length))
                        and self.user.SendMessageTimeoutW(focus,0xB0,0,0,2,200,c.byref(selected))):
                        selection=dict(verified=(selected.value&0xFFFF)==0 and (selected.value>>16)==length.value,
                                       kind="text_selection",start=selected.value&0xFFFF,end=selected.value>>16)
                else:
                    vk=events[0].ki.wVk
                    detail=1|(self.user.MapVirtualKeyW(vk,0)<<16)|((events[0].ki.dwFlags&1)<<24)
                    post(focus,0x100,vk,detail)
                    post(focus,0x101,vk,detail|0xC0000000)
            else:
                point=float(params["x"]),float(params["y"])
                end=None
                if kind=="drag":
                    end=float(params["x2"]),float(params["y2"])
                    for i in range(17):
                        self._point_gate(target,bounds,(point[0]+(end[0]-point[0])*i/16,point[1]+(end[1]-point[1])*i/16))
                hwnd,detail=location(point)
                if kind=="hover":
                    post(hwnd,0x200,0,detail)
                elif kind in {"click","double_click","right_click"}:
                    down,up,button=(0x204,0x205,2) if kind=="right_click" else (0x201,0x202,1)
                    post(hwnd,down,button,detail); post(hwnd,up,0,detail)
                    if kind=="double_click":
                        post(hwnd,0x203,1,detail); post(hwnd,up,0,detail)
                elif kind=="scroll":
                    dx,dy=params.get("dx",0),params.get("dy",0)
                    if any(type(v)is not int or abs(v)>12000 for v in (dx,dy)) or not (dx or dy):
                        raise NativeError("invalid_scroll","dx/dy need bounded integer wheel units",dispatched=moved)
                    screen=(round(point[0])&0xFFFF)|((round(point[1])&0xFFFF)<<16)
                    if dy: post(focus,0x20A,((-dy)&0xFFFF)<<16,screen)
                    if dx: post(focus,0x20E,(dx&0xFFFF)<<16,screen)
                elif kind=="drag":
                    post(hwnd,0x201,1,detail)
                    for i in range(1,17):
                        next_point=(point[0]+(end[0]-point[0])*i/16,point[1]+(end[1]-point[1])*i/16)
                        location(next_point)
                        client=w.POINT(round(next_point[0]),round(next_point[1]))
                        if not self.user.ScreenToClient(hwnd,c.byref(client)):
                            raise NativeError("outcome_unknown","Drag source client coordinates unavailable",dispatched=True)
                        next_detail=(client.x&0xFFFF)|((client.y&0xFFFF)<<16)
                        post(hwnd,0x200,1,next_detail)
                    post(hwnd,0x202,0,next_detail)
                else:
                    raise NativeError("unsupported","Unsupported HWND input action",dispatched=moved)
            return dict(method="Win32.WindowMessage",posted_messages=sent,foreground_input_required=True,
                        coordinate_space="physical_screen_pixels",native_verification=selection)
