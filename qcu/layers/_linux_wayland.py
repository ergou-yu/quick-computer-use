"""Lazy, persistent GNOME Shell bridge. Never redirects input through XWayland."""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path
import struct

from qcu.layers.desktop_linux import LinuxError, process_identity

PATH = "/org/gnome/Shell/Extensions/QCU"
INTERFACE = "org.qcu.Wayland1"


class GnomeNative:
    def __init__(self):
        try:
            from gi.repository import Gio, GLib
            self.glib = GLib
            connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            owner = connection.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                "org.freedesktop.DBus", "GetNameOwner", GLib.Variant("(s)", ("org.gnome.Shell",)),
                GLib.VariantType.new("(s)"), Gio.DBusCallFlags.NO_AUTO_START, 2000, None).unpack()[0]
            # Pin the unique Shell connection: a restarted Shell cannot inherit a binding.
            self.proxy = Gio.DBusProxy.new_sync(connection, Gio.DBusProxyFlags.DO_NOT_AUTO_START,
                None, owner, PATH, INTERFACE, None)
            self.owner, self._bound = owner, None
            hello = self._request("hello")
            if hello.get("protocol") != 1 or hello.get("session_type") != "wayland":
                raise LinuxError("wayland_bridge_incompatible", "QCU needs the GNOME Wayland bridge protocol 1")
            self.instance = hello["instance"]
        except LinuxError:
            raise
        except Exception as exc:
            raise LinuxError("wayland_bridge_unavailable", "Install and enable the QCU GNOME extension: "
                "python -m qcu.gnome_bridge install. " + str(exc)) from None

    def _request(self, op, *, mutation=False, **data):
        payload = json.dumps(dict(op=op, instance=getattr(self, "instance", None), **data),
                             ensure_ascii=False, allow_nan=False)
        try:
            result = self.proxy.call_sync("Request", self.glib.Variant("(s)", (payload,)),
                0, 7000, None)
            value = json.loads(result.unpack()[0])
        except Exception as exc:
            # A timeout/disconnect may occur after the Shell accepted the request.
            raise LinuxError("outcome_unknown" if mutation else "wayland_bridge_unavailable",
                             str(exc), dispatched=mutation) from None
        if not isinstance(value, dict) or not isinstance(value.get("ok"), bool):
            raise LinuxError("outcome_unknown" if mutation else "wayland_bridge_incompatible",
                             "Invalid GNOME bridge reply", dispatched=mutation)
        if not value["ok"]:
            raise LinuxError(value.get("reason", "wayland_error"), value.get("error", "GNOME bridge failed"),
                             dispatched=bool(value.get("dispatched", mutation)))
        return value

    def _identity(self, target):
        if not self._bound or self._bound[0] != target:
            raise LinuxError("target_mismatch", "Wayland target differs from observation")
        live = process_identity(target["pid"])
        if any(live[k] != target[k] for k in ("process_started", "process_image")):
            raise LinuxError("target_changed", "Bound process lifetime/executable changed")
        return self._bound[1]

    def bind(self, target, title, bounds):
        if self._bound and self._bound[0] == target:
            self.geometry(target)
            return
        value = self._request("bind", target=target, title=title)
        self._bound = (dict(target), value["binding"])
        self.geometry(target)

    def _geometry(self, target):
        return self._request("geometry", binding=self._identity(target))

    def geometry(self, target):
        return self._geometry(target)["bounds"]

    def ref_point(self, api, obj, target):
        # Wayland clients cannot report their global position. Read an explicit
        # WINDOW coordinate and translate using Mutter's client rectangle.
        bounds = api.relative_bounds(obj)
        client = self._geometry(target)["client_bounds"]
        if not bounds or bounds.width <= 0 or bounds.height <= 0:
            raise LinuxError("geometry_unavailable", "Control has no window-relative bounds")
        return client["x"] + bounds.cx, client["y"] + bounds.cy

    def capture(self, target, path):
        if not isinstance(path, str) or not path:
            raise LinuxError("invalid_path", "Screenshot path must be a non-empty string")
        value = self._request("capture", binding=self._identity(target))
        try:
            png = base64.b64decode(value["png"], validate=True)
            if len(png) < 33 or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR":
                raise ValueError("not a PNG")
            width, height = struct.unpack(">II", png[16:24])
            g = value["bounds"]
            sx, sy = width/g["width"], height/g["height"]
            if not (0 < width <= 16384 and 0 < height <= 16384 and width*height <= 32000000
                    and math.isclose(sx, sy, abs_tol=.01) and .5 <= sx <= 4):
                raise ValueError(f"invalid PNG geometry or non-uniform scale: pixels={width}x{height}, bounds={g}")
        except (KeyError, ValueError, TypeError, ZeroDivisionError) as exc:
            raise LinuxError("capture_format_unsupported", str(exc)) from None
        self._identity(target)
        if self.geometry(target) != value.get("frame_bounds", g):
            raise LinuxError("target_changed", "Window moved during capture")
        output = Path(path).expanduser().absolute()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(png)
        return dict(path=str(output), target=dict(target), bounds=g, frame_bounds=value.get("frame_bounds", g),
            wayland_window_id=self._bound[1]["window_id"], method="GNOME.Shell.Screenshot",
            coordinate_origin=dict(x=g["x"], y=g["y"]), coordinate_scale=sx,
            coordinate_space="wayland_logical", pixel_size=dict(width=width, height=height))

    def dispatch(self, target, kind, params):
        binding = self._identity(target)
        value = self._request("input", mutation=True, binding=binding, kind=kind,
                              params={k: v for k, v in params.items() if k in {
                                  "key", "x", "y", "x1", "y1", "x2", "y2", "from", "to", "steps", "dx", "dy"}})
        if type(value.get("events")) is not int or value["events"] < 1:
            raise LinuxError("outcome_unknown", "Native reply did not confirm an event count", dispatched=True)
        return dict(target=dict(target), method="GNOME.Mutter.VirtualInputDevice",
                    wayland_window_id=binding["window_id"], retry_safe=False, input_mode="native",
                    coordinate_space="wayland_logical", events=value["events"],
                    pointer_position=value.get("pointer_position"))

    def close(self):
        # The shared session bus belongs to GI/AT-SPI; do not close it.
        self._bound, self.proxy = None, None
