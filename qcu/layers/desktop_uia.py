"""Windows UI Automation semantic backend (Windows real device unverified).

All COM objects live on one persistent MTA worker. Public methods return only
QCU dataclasses/dicts; no UIA handles cross thread or backend lifetimes. Pattern
methods call COM directly: no Control.Click, SendKeys or SetToggleState loops.
"""
from __future__ import annotations

import gc
import queue
import sys
import threading
import time
from concurrent.futures import Future, TimeoutError
from typing import Any, Callable

from qcu.common.normalize import from_uia
from qcu.common.refs import NativeRefRegistry
from qcu.common.types import Action, Element, LayerResult, Observation, Rect
from qcu.layers.runtime import register
from qcu.platforms import desktop_capabilities

_PATTERN_IDS = {"invoke": 10000, "value": 10002, "selection": 10010, "toggle": 10015}
_ACTION_PATTERNS = {"click": "invoke", "invoke": "invoke", "fill": "value",
                    "toggle": "toggle", "select": "selection"}


def _matches_process_image(requested: str, process_image: str) -> bool:
    """An absolute executable path is an identity constraint, not a basename."""
    import ntpath
    if ntpath.isabs(requested):
        return ntpath.normcase(ntpath.normpath(requested)) == ntpath.normcase(ntpath.normpath(process_image))
    return (ntpath.basename(requested).casefold().removesuffix(".exe") ==
            ntpath.basename(process_image).casefold().removesuffix(".exe"))


class UIAError(RuntimeError):
    def __init__(self, reason: str, message: str, **data: Any):
        super().__init__(message)
        self.reason, self.data = reason, data


def _error_reason(exc: Exception) -> str:
    if isinstance(exc, UIAError):
        return exc.reason
    code = getattr(exc, "hresult", 0)
    if code and (code & 0xFFFFFFFF) == 0x80070005:
        return "access_denied"
    if code and (code & 0xFFFFFFFF) == 0x80040201:
        return "element_unavailable"
    return "uia_access_failed"


class _WindowsAPI:
    """Small injected boundary, also used by non-Windows contract tests."""
    def __init__(self) -> None:
        # comtypes initializes COM on its import thread. Set its documented
        # coinit_flags before first import, then balance BOTH initializations.
        # QCU loads this adapter lazily, only on the dedicated Windows worker.
        self._import_initialized = "comtypes" not in sys.modules
        previous = getattr(sys, "coinit_flags", None)
        sys.coinit_flags = 0  # COINIT_MULTITHREADED
        try:
            import uiautomation as auto
        finally:
            if previous is None:
                del sys.coinit_flags
            else:
                sys.coinit_flags = previous
        self.auto = auto
        auto.comtypes.CoInitializeEx(0)
        self._client = None

    def control_from_handle(self, handle: int):
        control = self.auto.ControlFromHandle(handle)
        self._client = self.auto.uiautomation._AutomationClient._instance
        return control

    def pattern(self, control, kind: str):
        # Control.GetPattern swallows every COMError (including access denied).
        # Ask the native element directly to distinguish missing vs inaccessible.
        try:
            native = control.Element.GetCurrentPattern(_PATTERN_IDS[kind])
        except Exception as exc:
            if (getattr(exc, "hresult", 0) & 0xFFFFFFFF) in {0x80040200, 0x80004002}:
                return None  # UIA_E_NOTSUPPORTED / E_NOINTERFACE
            raise
        return self.auto.CreatePattern(_PATTERN_IDS[kind], native) if native else None

    def windows(self) -> list[dict[str, Any]]:
        # EnumWindows enumerates HWNDs only; never walk the desktop UIA tree.
        import ctypes
        from ctypes import wintypes
        user = ctypes.windll.user32
        user.IsWindowVisible.argtypes = [wintypes.HWND]
        user.IsWindowVisible.restype = wintypes.BOOL
        user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user.GetWindowTextLengthW.restype = ctypes.c_int
        user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user.GetWindowTextW.restype = ctypes.c_int
        user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user.GetWindowThreadProcessId.restype = wintypes.DWORD
        windows: list[dict[str, Any]] = []
        overflow = False
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        @callback_type
        def callback(hwnd, _):
            nonlocal overflow
            if len(windows) >= 1000:
                overflow = True
                return False
            if not user.IsWindowVisible(hwnd):
                return True
            size = user.GetWindowTextLengthW(hwnd)
            buf = ctypes.create_unicode_buffer(size + 1)
            user.GetWindowTextW(hwnd, buf, size + 1)
            pid = wintypes.DWORD()
            user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            windows.append({"window_id": int(hwnd), "pid": int(pid.value), "title": buf.value})
            return True
        user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
        user.EnumWindows.restype = wintypes.BOOL
        user.EnumWindows(callback, 0)
        if overflow:
            raise UIAError("window_enumeration_truncated", "Too many windows; specify a HWND directly")
        return windows

    def process_identity(self, pid: int) -> dict[str, Any]:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                                    wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            error = int(kernel.GetLastError())
            reason = "access_denied" if error == 5 else "process_unavailable" if error == 87 else "cross_process_access_failed"
            raise UIAError(reason, f"Cannot inspect process {pid} (Win32 error {error})")
        try:
            creation, exit_time, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(creation), ctypes.byref(exit_time),
                                          ctypes.byref(kernel_time), ctypes.byref(user_time)):
                raise UIAError("process_identity_unavailable", "Cannot verify process lifetime")
            buf, size = ctypes.create_unicode_buffer(32768), wintypes.DWORD(32768)
            if not kernel.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                raise UIAError("process_identity_unavailable", "Cannot resolve process image")
            return {"process_started": (creation.dwHighDateTime << 32) | creation.dwLowDateTime,
                    "process_image": buf.value}
        finally:
            kernel.CloseHandle(handle)

    def close(self) -> None:
        # Drop the library singleton while the creating apartment still lives.
        client_type = self.auto.uiautomation._AutomationClient
        if client_type._instance is self._client:
            client_type._instance = None
        self._client = None
        gc.collect()
        self.auto.comtypes.CoUninitialize()
        if self._import_initialized:
            self.auto.comtypes.CoUninitialize()


@register("desktop_uia")
class DesktopUIALayer:
    name = "desktop_uia"

    def __init__(self, *, api_factory: Callable[[], Any] | None = None,
                 call_timeout: float = 10.0) -> None:
        self._api_factory = api_factory or _WindowsAPI
        self._injected = api_factory is not None
        self._call_timeout = call_timeout
        self._queue: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._closed = False
        self._poisoned = False
        self._api = None
        self._refs = NativeRefRegistry(self.name)
        self._controls: dict[str, Any] = {}
        self._identities: dict[str, tuple[int, ...]] = {}
        self._target: dict[str, Any] | None = None
        self._window = None
        self._cap = desktop_capabilities(layer=self.name)

    @property
    def current_target(self) -> dict[str, Any] | None:
        return dict(self._target) if self._target else None

    @property
    def _target_identity(self):
        return self.current_target

    def capabilities(self) -> dict[str, Any]:
        return dict(self._cap)

    def _worker(self) -> None:
        init_error = None
        try:
            self._api = self._api_factory()
        except Exception as exc:
            init_error = exc
        try:
            while True:
                item = self._queue.get()
                if item is None:
                    break
                future, method, args = item
                try:
                    if init_error:
                        raise init_error
                    future.set_result(method(*args))
                except Exception as exc:
                    # Do not carry traceback frames containing COM handles
                    # back to the caller thread.
                    future.set_exception(UIAError(_error_reason(exc), str(exc),
                                                  **(exc.data if isinstance(exc, UIAError) else {})))
        finally:
            self._controls.clear()
            self._identities.clear()
            self._window = None
            if self._api:
                self._api.close()
                self._api = None

    def _call(self, method, *args):
        if self._closed or self._poisoned:
            raise UIAError("backend_restart_required", "UIA worker closed or timed out; restart and observe again")
        if not self._injected and sys.platform != "win32":
            raise UIAError("platform_mismatch", "desktop_uia requires Windows")
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._worker, name="qcu-uia-mta", daemon=True)
                self._thread.start()
        future = Future()
        self._queue.put((future, method, args))
        try:
            return future.result(timeout=self._call_timeout)
        except TimeoutError:
            # Never schedule a second action behind an uncertain COM call.
            self._poisoned = True
            raise UIAError("uia_timeout", "UIA call timed out; outcome may be unknown; no replay")

    def _session_id(self) -> str | None:
        from qcu.session import load
        session = load()
        return session.session_id if session else None

    def _identity(self, control) -> dict[str, Any]:
        pid = int(control.ProcessId)
        runtime_id = list(control.GetRuntimeId())
        if not runtime_id or not int(control.NativeWindowHandle):
            raise UIAError("target_identity_unavailable", "Window lacks a durable HWND/runtime identity")
        return {"pid": pid, "window_id": int(control.NativeWindowHandle),
                "runtime_id": runtime_id,
                **self._api.process_identity(pid), "session_id": self._session_id()}

    def _resolve_target(self, *, pid=None, window=None, app=None):
        if pid is not None:
            try:
                if isinstance(pid, bool) or int(pid) <= 0:
                    raise ValueError()
                pid = int(pid)
            except (TypeError, ValueError):
                raise UIAError("invalid_target", "Process id must be a positive integer") from None
        if any(value is not None and not str(value).strip() for value in (window, app)):
            raise UIAError("invalid_target", "Explicit app/window must not be empty")
        if not any((pid, window, app)) and self._target:
            return self._validate_window()
        if not any((pid, window, app)):
            raise UIAError("target_required", "Specify --pid and/or --window (HWND or title) before observing")
        hwnd = None
        if window is not None:
            try:
                hwnd = int(str(window), 0) if str(window).lower().startswith("0x") else int(str(window))
            except ValueError:
                pass
        if hwnd is not None:
            control = self._api.control_from_handle(hwnd)
            if control is None:
                raise UIAError("window_not_found", f"No window with HWND {hwnd}")
            if pid is not None and int(control.ProcessId) != int(pid):
                raise UIAError("target_mismatch", "HWND does not belong to specified process")
            candidates = [{"window_id": hwnd, "pid": int(control.ProcessId), "title": control.Name}]
        else:
            candidates = self._api.windows()
            if pid is not None:
                candidates = [item for item in candidates if item["pid"] == int(pid)]
            if window:
                needle = str(window).casefold()
                candidates = [item for item in candidates if needle in item["title"].casefold()]
        if app:
            candidates = [item for item in candidates
                          if _matches_process_image(str(app), self._api.process_identity(item["pid"])["process_image"])]
        if not candidates:
            raise UIAError("window_not_found", "No window matches the explicit target")
        if len(candidates) != 1:
            raise UIAError("ambiguous_window", "Multiple windows match; specify a HWND", candidates=candidates)
        return self._api.control_from_handle(candidates[0]["window_id"])

    def _validate_window(self):
        if not self._target:
            raise UIAError("target_required", "Observe a target window first")
        current = self._api.control_from_handle(self._target["window_id"])
        if current is None or self._identity(current) != self._target:
            raise UIAError("target_changed", "Bound window/process/session changed; observe again")
        return current

    def _patterns(self, control) -> dict[str, Any]:
        return {kind: pattern for kind in _PATTERN_IDS
                if (pattern := self._api.pattern(control, kind)) is not None}

    def _element(self, control, ref: str, parent: str | None,
                 depth: int, patterns=None) -> Element:
        patterns = self._patterns(control) if patterns is None else patterns
        rect = control.BoundingRectangle
        enabled = bool(control.IsEnabled)
        props = {"raw_role": control.ControlTypeName, "automation_id": control.AutomationId,
                 "class_name": control.ClassName, "pid": int(control.ProcessId),
                 "runtime_id": list(control.GetRuntimeId()), "parent_ref": parent,
                 "children": [], "depth": depth, "offscreen": bool(control.IsOffscreen),
                 "patterns": list(patterns), "actions": []}
        if enabled:
            props["actions"] = [kind for kind, pat in _ACTION_PATTERNS.items() if pat in patterns]
        value = None
        if "value" in patterns:
            props["readonly"] = bool(patterns["value"].IsReadOnly)
            value = str(patterns["value"].Value)
            if props["readonly"] and "fill" in props["actions"]:
                props["actions"].remove("fill")
        if "toggle" in patterns:
            state = int(patterns["toggle"].ToggleState)
            props.update(toggle_state=state, checked={0: False, 1: True}.get(state, "mixed"))
        if "selection" in patterns:
            props["selected"] = bool(patterns["selection"].IsSelected)
        return Element(ref=ref, role=from_uia(control.ControlTypeName), name=control.Name,
                       value=value, enabled=enabled, focused=bool(control.HasKeyboardFocus),
                       bounds=Rect(rect.left, rect.top, rect.right-rect.left, rect.bottom-rect.top),
                       backend_id=str(props["runtime_id"]), properties=props)

    def _walk(self, root, max_depth: int, max_nodes: int, *, fresh: bool):
        elements: list[Element] = []
        stack = [(root, None, 0)]
        by_ref = {}
        truncated = False
        errors = []
        old_refs = {identity: ref for ref, identity in self._identities.items()}
        deadline = time.monotonic() + min(self._call_timeout * .7, 5.0)
        while stack:
            if len(elements) >= max_nodes or time.monotonic() > deadline:
                truncated = True
                break
            control, parent, depth = stack.pop()
            try:
                if int(control.ProcessId) != self._target["pid"]:
                    errors.append({"reason": "cross_process_element_skipped"})
                    continue
                runtime = tuple(control.GetRuntimeId())
                ref = self._refs.issue(len(elements) + 1) if fresh else old_refs.get(runtime, "")
                element = self._element(control, ref, parent, depth)
                elements.append(element)
                by_ref[ref] = element
                if parent in by_ref:
                    by_ref[parent].properties["children"].append(ref)
                if fresh:
                    self._controls[ref], self._identities[ref] = control, runtime
                if depth < max_depth:
                    # Bounded sibling traversal avoids GetChildren materializing an unbounded list.
                    child = control.GetFirstChildControl()
                    children = []
                    while child is not None and len(children) + len(elements) + len(stack) < max_nodes:
                        children.append((child, ref, depth + 1))
                        child = child.GetNextSiblingControl()
                        if time.monotonic() > deadline:
                            break
                    if child is not None:
                        truncated = True
                    stack.extend(reversed(children))
                else:
                    truncated = True
            except Exception as exc:
                errors.append({"reason": _error_reason(exc), "message": str(exc)})
        return elements, truncated, errors

    def observe(self, max_depth: int = 8, **options: Any) -> Observation:
        try:
            return self._call(self._observe, max_depth, options)
        except Exception as exc:
            self._refs.invalidate()
            self._cap.update(available=False, session_accessible=False, can_read_controls=False,
                             reason=_error_reason(exc))
            return Observation(context="desktop", routing_meta={"layer": self.name,
                "available": False, "reason": _error_reason(exc), "error": str(exc),
                **(exc.data if isinstance(exc, UIAError) else {}), "capabilities": self.capabilities()})

    def _observe(self, max_depth, options):
        self._controls.clear()
        self._identities.clear()
        root = self._resolve_target(pid=options.get("pid"), window=options.get("window"), app=options.get("app"))
        self._target = self._identity(root)
        self._window = root
        scope = self._refs.begin(self._target)
        elements, truncated, errors = self._walk(root, max(0, min(int(max_depth), 32)),
                    max(1, min(int(options.get("max_nodes", 500)), 2000)), fresh=True)
        readable = len(elements) > 1
        self._cap.update(dependencies_present=True, session_accessible=True,
                         can_read_controls=True if readable else None, available=readable,
                         reason="ready" if readable else "empty_or_unexposed_tree")
        if errors:
            self._cap["reason"] = "partial_tree_access"
        obs = Observation(context="desktop", url_or_app=self._target["process_image"],
                          title=root.Name, elements=elements, routing_meta={
                              "layer": self.name, "available": readable,
                              "target": self.current_target, "ref_scope": scope,
                              "capabilities": self.capabilities(), "tree_truncated": truncated,
                              "read_errors": errors, "n_total": len(elements)})
        from qcu.session import record_observation, patch
        record_observation(obs.to_dict(), refs=[{"ref": e.ref, "role": e.role, "name": e.name}
                                               for e in elements])
        patch(target_app=obs.url_or_app)
        selected = [e for e in elements if (not options.get("roles") or e.role == options["roles"])
                    and (not options.get("query") or options["query"].casefold() in (e.name or "").casefold())]
        offset = max(0, int(options.get("offset") or 0))
        limit = options.get("limit")
        obs.elements = selected[offset:offset + limit] if limit and limit > 0 else selected[offset:]
        obs.routing_meta["n_returned"] = len(obs.elements)
        return obs

    def _get_control(self, ref: str):
        valid, reason = self._refs.validate(ref, self._target)
        if not valid:
            raise UIAError(reason, "Reference invalid; observe the same target again")
        control = self._controls.get(ref)
        if control is None:
            raise UIAError("unknown_ref", "Reference was not issued by this observation")
        if tuple(control.GetRuntimeId()) != self._identities[ref] or int(control.ProcessId) != self._target["pid"]:
            raise UIAError("element_unavailable", "Control identity changed; observe again")
        # Membership may change while PID/runtime ID remains the same.
        parent = control
        for _ in range(64):
            if tuple(parent.GetRuntimeId()) == tuple(self._target["runtime_id"]):
                return control
            parent = parent.GetParentControl()
            if parent is None:
                break
        raise UIAError("ref_target_mismatch", "Control no longer belongs to the bound window")

    def act(self, action: Action) -> LayerResult:
        try:
            return self._call(self._act, action)
        except Exception as exc:
            unknown = _error_reason(exc) == "uia_timeout"
            return LayerResult(False, self.name, str(exc),
                               {"reason": "outcome_unknown" if unknown else _error_reason(exc),
                                "retry_safe": False}, dispatch_state="unknown" if unknown else "not_sent")

    def _act(self, action: Action) -> LayerResult:
        params = action.params
        kind = _ACTION_PATTERNS.get(action.type)
        if kind is None:
            return LayerResult(False, self.name, f"Unsupported semantic UIA action: {action.type}",
                               {"reason": "unsupported", "supported_actions": list(_ACTION_PATTERNS)},
                               dispatch_state="not_sent")
        from qcu.common.verification import validate_condition, match_condition
        condition = params.get("verify")
        if condition is not None:
            try:
                validate_condition(condition)
            except ValueError as exc:
                raise UIAError("invalid_verification", str(exc)) from None
        self._validate_window()
        for key, target_key in (("pid", "pid"), ("window_id", "window_id")):
            if key in params and str(params[key]) != str(self._target[target_key]):
                raise UIAError("target_mismatch", "Action target differs from bound observation")
        if "window" in params:
            requested = str(params["window"])
            if requested not in {str(self._target["window_id"]), hex(self._target["window_id"]), self._window.Name}:
                raise UIAError("target_mismatch", "Action window differs from bound observation")
        if "app" in params:
            if not _matches_process_image(str(params["app"]), self._target["process_image"]):
                raise UIAError("target_mismatch", "Action app differs from bound observation")
        control = self._get_control(params.get("ref"))
        if not control.IsEnabled:
            raise UIAError("control_disabled", "Disabled controls cannot receive a semantic action")
        pattern = self._api.pattern(control, kind)
        if pattern is None:
            raise UIAError("unsupported", f"Control does not expose UIA {kind} Pattern")
        if kind == "value" and pattern.IsReadOnly:
            raise UIAError("control_readonly", "Value Pattern is read-only")
        if condition and condition.get("ref"):
            self._get_control(condition["ref"])
        if kind == "value":
            if not isinstance(params.get("text"), str):
                raise UIAError("invalid_action", "fill requires a text string")
            condition = condition or {"kind": "value", "ref": params["ref"], "equals": params["text"]}
        elif kind == "selection":
            condition = condition or {"kind": "selected", "ref": params["ref"], "equals": True}
        # A raw Toggle is a cycle, not a setter: never cycle until an expected
        # state appears. Call once and verify only a supplied postcondition.
        dispatched = False
        try:
            dispatched = True  # Mark before the COM boundary: errors can be late.
            if kind == "value":
                native_result = pattern.pattern.SetValue(params["text"])
            elif kind == "selection":
                native_result = pattern.pattern.Select()
            elif kind == "toggle":
                native_result = pattern.pattern.Toggle()
            else:
                native_result = pattern.pattern.Invoke()
            data = {"method": f"UIA.{kind}", "target": self.current_target,
                    "interface_call_succeeded": (native_result is None or type(native_result) is int and native_result == 0), "retry_safe": False}
            if not (native_result is None or type(native_result) is int and native_result == 0):
                data["reason"] = "outcome_unknown"
                return LayerResult(False, self.name, "UIA returned an uncertain dispatch result", data,
                                   dispatch_state="unknown")
            if condition is None:
                data["reason"] = "verification_not_requested"
                return LayerResult(True, self.name, "Semantic action sent; requested result unverified", data,
                                   dispatch_state="sent")
            timeout_ms = min(max(int(condition.get("timeout_ms", 1000)), 0), 5000)
            deadline = time.monotonic() + timeout_ms / 1000
            while True:
                root = self._validate_window()
                if condition.get("kind") == "text" and not condition.get("ref"):
                    elements, _, errors = self._walk(root, 8, 500, fresh=False)
                else:
                    ref = condition.get("ref") or params["ref"]
                    elements = [self._element(self._get_control(ref), ref, None, 0)]
                verification = match_condition(condition, elements)
                if verification.get("verified") or verification.get("success"):
                    data["verification"] = verification
                    return LayerResult(True, self.name, "Requested UI state verified", data,
                                       dispatch_state="sent", outcome="verified")
                if time.monotonic() >= deadline:
                    data.update(verification=verification, reason="outcome_unknown")
                    return LayerResult(False, self.name, "Action sent; requested result was not confirmed", data,
                                       dispatch_state="sent")
                time.sleep(.05)
        except Exception as exc:
            if dispatched:
                return LayerResult(False, self.name, "Action may have been sent; observe before any further action",
                                   {"reason": "outcome_unknown", "cause": _error_reason(exc),
                                    "error": str(exc), "retry_safe": False}, dispatch_state="unknown")
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._refs.invalidate()
        if self._thread:
            self._queue.put(None)
            self._thread.join(timeout=2)
