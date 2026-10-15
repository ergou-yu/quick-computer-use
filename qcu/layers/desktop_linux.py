"""Bound-window AT-SPI2 controls. GI/DBus objects stay on one resident worker."""
from __future__ import annotations

import os
from pathlib import Path
import queue
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError

from qcu.common.refs import NativeRefRegistry
from qcu.common.types import Action, Element, LayerResult, Observation, Rect
from qcu.common.verification import validate_condition, match_condition
from qcu.layers.runtime import register
from qcu.platforms import desktop_capabilities


class LinuxError(RuntimeError):
    def __init__(self, reason, message, *, dispatched=False, **data):
        super().__init__(message)
        self.reason, self.dispatched, self.data = reason, dispatched, data


def process_identity(pid):
    try:
        # comm may contain spaces and parentheses; field 22 follows the LAST ')'.
        stat = Path(f"/proc/{pid}/stat").read_text()
        started = stat[stat.rfind(")") + 2:].split()[19]
        return {"process_started": started, "process_image": os.readlink(f"/proc/{pid}/exe")}
    except (OSError, IndexError) as exc:
        raise LinuxError("process_identity_unavailable", str(exc)) from None


class AtspiAPI:
    def __init__(self):
        if not (os.environ.get("DBUS_SESSION_BUS_ADDRESS") or os.environ.get("AT_SPI_BUS_ADDRESS")):
            raise LinuxError("session_bus_missing", "Run QCU in the target user's desktop DBus session")
        try:
            import gi
            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi, GLib
        except (ImportError, ValueError) as exc:
            raise LinuxError("dependency_missing", "Install python3-gi and gir1.2-atspi-2.0: " + str(exc)) from None
        self.a, self.glib, self._native = Atspi, GLib, None
        Atspi.init()
        Atspi.set_timeout(750, 1500)

    @property
    def native(self):
        if self._native is None:
            from qcu.layers._linux_x11 import X11Native
            self._native = X11Native()
        return self._native

    def refresh(self, obj):
        context = self.glib.MainContext.default()
        for _ in range(64):
            if not context.pending():
                break
            context.iteration(False)
        obj.clear_cache()

    def key(self, obj):
        bus, path = str(obj.app.bus_name), str(obj.path)
        if not bus.startswith(":") or not path.startswith("/"):
            raise LinuxError("identity_unavailable", "AT-SPI object lacks a unique bus/path identity")
        return bus + path

    def pid(self, obj):
        return int(obj.get_process_id())

    def children(self, obj, limit):
        return [obj.get_child_at_index(i) for i in range(min(obj.get_child_count(), limit))]

    def windows(self):
        desktop = self.a.get_desktop(0)
        if desktop is None:
            raise LinuxError("accessibility_bus_unavailable", "AT-SPI desktop is unavailable")
        windows = []
        for app in self.children(desktop, 256):
            if app is None:
                continue
            for obj in self.children(app, 128):
                if obj is not None and obj.get_role_name() in {"frame", "dialog", "window", "alert"}:
                    windows.append(obj)
        return windows

    process_identity = staticmethod(process_identity)

    def info(self, obj):
        self.refresh(obj)
        state = obj.get_state_set()
        has = lambda name: state.contains(getattr(self.a.StateType, name))
        if has("DEFUNCT"):
            raise LinuxError("element_unavailable", "AT-SPI object is defunct")
        interfaces = set(obj.get_interfaces())
        role = obj.get_role_name().replace("_", " ").lower()
        bounds = None
        if "Component" in interfaces and has("SHOWING"):
            r = obj.get_component_iface().get_extents(self.a.CoordType.SCREEN)
            bounds = Rect(r.x, r.y, r.width, r.height)
        value = None
        if "Text" in interfaces and role != "password text":
            text = obj.get_text_iface()
            value = text.get_text(0, min(text.get_character_count(), 16384))
        return dict(name=obj.get_name(), raw_role=role, value=value, bounds=bounds,
                    enabled=has("ENABLED") and has("SENSITIVE"), focused=has("FOCUSED"),
                    offscreen=not has("SHOWING"), checked=has("CHECKED"), selected=has("SELECTED"),
                    editable=has("EDITABLE"), interfaces=sorted(interfaces))

    def action_index(self, obj):
        action = obj.get_action_iface()
        if action:
            for i in range(action.get_n_actions()):
                if action.get_action_name(i).lower() in {"click", "press", "activate", "toggle", "check", "uncheck"}:
                    return i
        raise LinuxError("unsupported", "No supported AT-SPI activation action")

    def prepare(self, obj, kind, params):
        # Resolve interfaces/indices before the mutation boundary.
        if kind in {"click", "invoke", "toggle"}:
            index = self.action_index(obj)
            return lambda: obj.get_action_iface().do_action(index)
        if kind == "fill":
            iface = obj.get_editable_text_iface()
            if iface is None:
                raise LinuxError("unsupported", "EditableText is unavailable")
            return lambda: iface.set_text_contents(params["text"])
        if kind == "select":
            parent = obj.get_parent()
            selection = parent.get_selection_iface() if parent else None
            index = obj.get_index_in_parent()
            if selection is None or index < 0:
                raise LinuxError("unsupported", "Parent does not expose Selection")
            return lambda: selection.select_child(index)
        if kind == "type":
            text, editable = obj.get_text_iface(), obj.get_editable_text_iface()
            if text is None or editable is None or text.get_n_selections():
                raise LinuxError("unsupported", "Use fill for replacement/selected text; type needs an unselected editable caret")
            position = text.get_caret_offset()
            return lambda: editable.insert_text(position, params["text"], len(params["text"]))
        raise LinuxError("unsupported", f"Unsupported AT-SPI operation: {kind}")

    def close(self):
        if self._native:
            self._native.close()


_ROLES = {"push button": "button", "toggle button": "button", "check box": "checkbox",
          "radio button": "radio", "text": "edit", "entry": "edit", "password text": "password",
          "label": "text", "static": "text", "frame": "window", "dialog": "dialog",
          "combo box": "combobox", "list box": "listbox", "list item": "option",
          "page tab": "tab", "page tab list": "tab_list", "menu item": "menu_item"}
_SEMANTIC = {"click", "invoke", "fill", "toggle", "select", "type"}
_NATIVE = {"screenshot", "click", "double_click", "right_click", "hover", "scroll", "drag", "press_key"}


@register("desktop_linux")
class DesktopLinuxLayer:
    name = "desktop_linux"

    def __init__(self, *, api_factory=None, call_timeout=10.0):
        self._factory, self._injected = api_factory or AtspiAPI, api_factory is not None
        self._timeout = call_timeout
        self._queue, self._lock = queue.Queue(), threading.Lock()
        self._thread = self._api = self._target = self._window = None
        self._closed = self._poisoned = False
        self._refs, self._controls, self._keys = NativeRefRegistry(self.name), {}, {}
        self._cap = desktop_capabilities(layer=self.name)

    @property
    def current_target(self):
        return dict(self._target) if self._target else None

    def capabilities(self):
        return dict(self._cap)

    def _worker(self):
        error = None
        try:
            self._api = self._factory()
        except Exception as exc:
            error = exc
        try:
            while (item := self._queue.get()) is not None:
                future, fn, args = item
                try:
                    if error:
                        raise error
                    future.set_result(fn(*args))
                except Exception as exc:
                    future.set_exception(LinuxError(getattr(exc, "reason", "atspi_error"), str(exc),
                        dispatched=getattr(exc, "dispatched", False), **getattr(exc, "data", {})))
        finally:
            self._controls.clear()
            self._keys.clear()
            self._window = None
            if self._api:
                self._api.close()

    def _call(self, fn, *args):
        if self._closed or self._poisoned:
            raise LinuxError("backend_restart_required", "Restart the timed-out/closed AT-SPI worker and observe again")
        if not self._injected and not sys.platform.startswith("linux"):
            raise LinuxError("platform_mismatch", "desktop_linux requires Linux")
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="qcu-atspi", daemon=True)
                self._thread.start()
        future = Future()
        self._queue.put((future, fn, args))
        try:
            return future.result(timeout=self._timeout)
        except TimeoutError:
            self._poisoned = True
            raise LinuxError("atspi_timeout", "AT-SPI worker timed out; no replay") from None

    def _identity(self, obj):
        from qcu.session import load
        session = load()
        pid = self._api.pid(obj)
        return dict(pid=pid, window_id=self._api.key(obj), **self._api.process_identity(pid),
                    session_id=session.session_id if session else None)

    def _validate_window(self):
        if not self._window or not self._target:
            raise LinuxError("target_required", "Observe a specific Linux window first")
        self._api.info(self._window)
        if self._identity(self._window) != self._target:
            raise LinuxError("target_changed", "Bound process, bus object or session changed")
        return self._window

    def _resolve(self, options):
        pid, window, app = (options.get(k) for k in ("pid", "window", "app"))
        if pid is not None and (type(pid) is not int or pid <= 0):
            raise LinuxError("invalid_target", "PID must be a positive integer")
        if any(v is not None and not str(v).strip() for v in (window, app)):
            raise LinuxError("invalid_target", "app/window must not be empty")
        if pid is None and window is None and app is None:
            return self._validate_window()
        candidates = []
        for obj in self._api.windows():
            if pid is not None and self._api.pid(obj) != pid:
                continue
            name = self._api.info(obj)["name"]
            if window is not None and str(window) != self._api.key(obj) and str(window).casefold() not in name.casefold():
                continue
            if app is not None:
                image = self._api.process_identity(self._api.pid(obj))["process_image"]
                if str(app) != image and (os.path.isabs(str(app)) or str(app) != Path(image).name):
                    continue
            candidates.append(obj)
        if len(candidates) != 1:
            raise LinuxError("window_not_found" if not candidates else "ambiguous_window",
                             "Select one AT-SPI window by PID and title or exact window_id",
                             candidates=[dict(pid=self._api.pid(o), window_id=self._api.key(o),
                                              title=self._api.info(o)["name"]) for o in candidates])
        return candidates[0]

    def _element(self, obj, ref, parent=None, depth=0):
        info = self._api.info(obj)
        role = _ROLES.get(info["raw_role"], info["raw_role"].replace(" ", "_"))
        props = {k: info[k] for k in ("raw_role", "offscreen", "checked", "selected", "interfaces")}
        props.update(parent_ref=parent, children=[], depth=depth, readonly=not info["editable"])
        return Element(ref, role, name=info["name"], value=info["value"], enabled=info["enabled"],
                       focused=info["focused"], bounds=info["bounds"], backend_id=self._api.key(obj), properties=props)

    def _walk(self, root, depth, limit, *, fresh):
        elements, by_ref, stack, seen, errors = [], {}, [(root, None, 0)], set(), []
        old = {key: ref for ref, key in self._keys.items()}
        deadline = time.monotonic() + min(5., self._timeout * .7)
        truncated = False
        while stack and len(elements) < limit and time.monotonic() < deadline:
            obj, parent, level = stack.pop()
            try:
                key = self._api.key(obj)
                if key in seen or self._api.pid(obj) != self._target["pid"]:
                    continue
                seen.add(key)
                ref = self._refs.issue(len(elements)+1) if fresh else old.get(key, "")
                el = self._element(obj, ref, parent, level)
                elements.append(el)
                by_ref[ref] = el
                if parent in by_ref:
                    by_ref[parent].properties["children"].append(ref)
                if fresh:
                    self._controls[ref], self._keys[ref] = obj, key
                if level < depth:
                    children = self._api.children(obj, max(0, limit-len(elements)-len(stack)))
                    stack.extend((c, ref, level+1) for c in reversed(children) if c is not None)
                else:
                    truncated = True
            except Exception as exc:
                errors.append(dict(reason=getattr(exc, "reason", "atspi_error"), message=str(exc)))
        return elements, truncated or bool(stack), errors

    def observe(self, max_depth=8, **options):
        try:
            return self._call(self._observe, max_depth, options)
        except Exception as exc:
            self._refs.invalidate()
            self._cap.update(available=False, reason=getattr(exc, "reason", "atspi_error"))
            return Observation("desktop", routing_meta=dict(layer=self.name, available=False,
                reason=self._cap["reason"], error=str(exc), capabilities=self.capabilities(), **getattr(exc, "data", {})))

    def _observe(self, max_depth, options):
        root = self._resolve(options)
        self._target, self._window = self._identity(root), root
        self._controls.clear()
        self._keys.clear()
        scope = self._refs.begin(self._target)
        elements, truncated, errors = self._walk(root, max(0, min(int(max_depth), 32)),
            max(1, min(int(options.get("max_nodes", 500)), 2000)), fresh=True)
        self._cap.update(dependencies_present=True, session_accessible=True, can_read_controls=len(elements)>1,
                         available=len(elements)>1, reason="ready" if len(elements)>1 else "empty_or_unexposed_tree")
        obs = Observation("desktop", url_or_app=self._target["process_image"], title=self._api.info(root)["name"],
            elements=elements, routing_meta=dict(layer=self.name, target=self.current_target,
                available=len(elements)>1, ref_scope=scope, capabilities=self.capabilities(),
                tree_truncated=truncated, read_errors=errors, n_total=len(elements)))
        from qcu.session import record_observation, patch
        record_observation(obs.to_dict(), refs=[dict(ref=e.ref, role=e.role, name=e.name) for e in elements])
        patch(target_app=obs.url_or_app)
        selected = [e for e in elements if (not options.get("roles") or e.role == options["roles"])
                    and (not options.get("query") or options["query"].casefold() in (e.name or "").casefold())]
        offset, limit = max(0, int(options.get("offset") or 0)), options.get("limit")
        obs.elements = selected[offset:offset+limit] if limit and limit>0 else selected[offset:]
        obs.routing_meta["n_returned"] = len(obs.elements)
        return obs

    def _get(self, ref):
        valid, reason = self._refs.validate(ref, self._target)
        if not valid or ref not in self._controls:
            raise LinuxError(reason if not valid else "unknown_ref", "Observe again before using this reference")
        obj = self._controls[ref]
        if self._api.key(obj) != self._keys[ref] or self._api.pid(obj) != self._target["pid"]:
            raise LinuxError("element_unavailable", "AT-SPI identity changed")
        parent = obj
        for _ in range(64):
            if self._api.key(parent) == self._target["window_id"]:
                return obj
            parent = parent.get_parent()
            if parent is None:
                break
        raise LinuxError("ref_target_mismatch", "Control left the bound window")

    def act(self, action):
        try:
            return self._call(self._act, action)
        except Exception as exc:
            unknown = getattr(exc, "dispatched", False) or (getattr(exc, "reason", "") == "atspi_timeout" and action.type != "screenshot")
            return LayerResult(False, self.name, str(exc), dict(reason="outcome_unknown" if unknown else getattr(exc, "reason", "atspi_error"),
                retry_safe=False, **getattr(exc, "data", {})), dispatch_state="unknown" if unknown else "not_sent")

    def _act(self, action):
        p, kind = dict(action.params), action.type
        validate_condition(p.get("verify"))
        root = self._validate_window()
        self._check_options(p)
        condition = p.get("verify")
        if condition and condition.get("ref"):
            self._get(condition["ref"])
        native = kind in _NATIVE and (kind != "click" or "ref" not in p or p.get("input_mode") == "native")
        obj = self._get(p["ref"]) if p.get("ref") else None
        info = self._api.info(obj) if obj is not None else None
        if info and (not info["enabled"] or info["offscreen"]):
            raise LinuxError("control_disabled" if not info["enabled"] else "control_offscreen", "Control cannot receive input")
        if obj and ("x" in p or "y" in p):
            raise LinuxError("ref_coordinate_conflict", "Use live refs or coordinates, not both")
        if kind in {"fill", "type"}:
            if obj is None or not info["editable"]:
                raise LinuxError("control_readonly", "A writable EditableText reference is required")
            if not isinstance(p.get("text"), str) or len(p["text"]) > 65536:
                raise LinuxError("invalid_action", "text must be a string of at most 65536 characters")
            if kind == "type" and not info["focused"]:
                raise LinuxError("focus_mismatch", "type requires the referenced editable to have focus")
            if kind == "fill" and condition is None:
                condition = dict(kind="value", ref=p["ref"], equals=p["text"])
        if native:
            if kind == "screenshot":
                from qcu.session import _qcu_home
                import uuid
                path = p.get("path") or str(_qcu_home()/"artifacts"/(uuid.uuid4().hex+".png"))
                self._bind_native(root)
                data = self._api.native.capture(self.current_target, path)
                self._validate_window()
                return LayerResult(True, self.name, "Bound X11 window captured", data,
                                   dispatch_state="not_sent", outcome="verified")
            if obj and kind != "press_key":
                if not info["bounds"]:
                    raise LinuxError("geometry_unavailable", "Control has no live bounds")
                p.update(x=info["bounds"].cx, y=info["bounds"].cy)
            if obj and kind == "press_key" and not info["focused"]:
                raise LinuxError("focus_mismatch", "Requested ref is not focused")
            self._bind_native(root)
            data = self._api.native.dispatch(self.current_target, kind, p)
        else:
            if kind not in _SEMANTIC or obj is None:
                raise LinuxError("unsupported", "Use a semantic ref or an explicit X11 native action")
            send = self._api.prepare(obj, kind, p)
            try:
                accepted = send()
            except Exception as exc:
                raise LinuxError("outcome_unknown", str(exc), dispatched=True) from None
            if accepted is not True:
                raise LinuxError("outcome_unknown", "AT-SPI did not confirm acceptance", dispatched=True)
            data = dict(method="AT-SPI."+kind, target=self.current_target, retry_safe=False)
        if condition is None:
            return LayerResult(True, self.name, "Input sent; requested result unverified", data, dispatch_state="sent")
        deadline = time.monotonic() + min(condition.get("timeout_ms", 1000), 5000)/1000
        try:
            while True:
                root = self._validate_window()
                if condition.get("ref"):
                    elements = [self._element(self._get(condition["ref"]), condition["ref"])]
                else:
                    elements, _, _ = self._walk(root, 8, 500, fresh=False)
                evidence = match_condition(condition, elements)
                if evidence["verified"] or time.monotonic() >= deadline:
                    data.update(verification=evidence, reason="condition_matched" if evidence["verified"] else "outcome_unknown")
                    return LayerResult(evidence["verified"], self.name, "Result read back" if evidence["verified"] else "Result unconfirmed",
                                       data, dispatch_state="sent", outcome="verified" if evidence["verified"] else "unknown")
                time.sleep(.02)
        except Exception as exc:
            raise LinuxError("outcome_unknown", str(exc), dispatched=True) from None

    def _bind_native(self, root):
        info = self._api.info(root)
        self._api.native.bind(self.current_target, info["name"], info["bounds"])

    def _check_options(self, options):
        for key in ("pid", "window_id"):
            if options.get(key) is not None and str(options[key]) != str(self._target[key]):
                raise LinuxError("target_mismatch", "Explicit target differs from observation")
        if options.get("window") is not None and str(options["window"]) not in {self._target["window_id"], self._api.info(self._window)["name"]}:
            raise LinuxError("target_mismatch", "Window differs from observation")
        if options.get("app") is not None:
            app, image = str(options["app"]), self._target["process_image"]
            if app != image and (os.path.isabs(app) or app != Path(image).name):
                raise LinuxError("target_mismatch", "Executable differs from observation")

    def _validate_capture_options(self, options):
        def check():
            self._validate_window()
            self._check_options(options)
        return self._call(check)

    def _capture_geometry(self):
        def read():
            root = self._validate_window()
            self._bind_native(root)
            return self._api.native.geometry(self.current_target)
        return self._call(read)

    def _screenshot(self, path):
        return self.act(Action("screenshot", dict(path=path)))

    def close(self):
        if not self._closed:
            self._closed = True
            self._refs.invalidate()
            if self._thread:
                self._queue.put(None)
                self._thread.join(timeout=2)
