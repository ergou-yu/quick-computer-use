"""Long-lived Python CLI daemon — a tiny JSON-RPC server over loopback TCP.

Why this exists (two problems it solves):

1. **Per-command process overhead.** Each ``qcu`` CLI invocation otherwise
   spawns a fresh Python process — ~300ms of interpreter + import startup per
   command, ~4.5s wasted across a 15-step task. A resident daemon pays that
   once and serves every later command from a warm process, so per-command
   latency drops to just the real AX/CDP work (~tens of ms).

2. **Desktop focus drift.** When QCU runs out-of-process, each new process
   can't hold a target app (App Store, Notes) in the foreground — the caller
   (ZCode/Terminal) steals focus between commands, so observe() ends up
   inspecting the wrong app. A single daemon process keeps the
   ``DesktopAXLayer`` instance alive across commands, holding ``_target_app``,
   ``_enhanced_apps``, and live ``ax_element`` refs — so the AX state (and the
   focused-app context) survives between ``qcu`` calls.

Design (mirrors ``browser_daemon.py``):

- Loopback-only TCP, no auth (same trust boundary as the CDP endpoint: anyone
  who can reach the loopback port already has the user's privileges).
- Trivial JSON-RPC: one request object per connection, one response object
  back. Requests are handled serially (QCU's model is one action at a time).
- Dispatch reuses the existing ``qcu.cli_handlers`` functions **unchanged** —
  we capture their stdout (they all ``print(json)`` + return an int exit code)
  and wrap it into the RPC response. This keeps handler logic in one place.

The one iron rule (from the codebase audit): never call ``Layer.close()``
between requests — ``WebA11yLayer._closed`` does not reset, so the daemon must
reuse instances and only tear them down on shutdown.
"""

from __future__ import annotations

import contextlib
import io
import json
import socket
import socketserver
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


# Methods the daemon knows how to serve. Each maps to a (handler, arg-extractor)
# pair. The arg-extractor turns the JSON-RPC ``params`` dict into positional
# args for the handler (handlers are imported lazily so the daemon process only
# imports what it actually serves).
def _dispatch(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Run one handler in-process and return its JSON-RPC response.

    Handlers print JSON to stdout and return an int exit code. We redirect
    stdout to capture that JSON (the real result payload) and report the exit
    code alongside, so the CLI caller can mirror local-execution semantics.
    """
    from qcu import cli_handlers as h

    table: dict[str, Callable[..., int]] = {
        "session_start": lambda: h.session_start(
            params.get("context", "auto"), headless=params.get("headless"),
            strict_background=bool(params.get("strict_background", False)),
            no_screenshot_fallback=bool(params.get("no_screenshot_fallback", False)),
        ),
        "session_status": h.session_status,
        "session_end": lambda: h.session_end(
            purge_profile=bool(params.get("purge_profile", False))
        ),
        "observe": lambda: h.observe(
            layer=params.get("layer"),
            max_depth=params.get("max_depth", 10),
            full_text=params.get("full_text", False),
            text_limit=params.get("text_limit"),
            tail=params.get("tail", False),
            limit=params.get("limit"),
            offset=params.get("offset", 0),
            compact=params.get("compact", False),
            role=params.get("role"),
            name=params.get("name"),
            app=params.get("app"),
            pid=params.get("pid"),
            window=params.get("window"),
            wait_until=params.get("wait_until", "domcontentloaded"),
            wait_for=params.get("wait_for"),
            timeout_ms=params.get("timeout_ms", 3000),
        ),
        "act": lambda: h.act(params.get("action", ""), layer=params.get("layer")),
        "batch": lambda: h.batch(params.get("actions", ""), layer=params.get("layer"),
                                  observe_after=params.get("observe_after", False),
                                  compact=params.get("compact", False)),
        "route": lambda: h.route(params.get("features", "-")),
        "stats": lambda: h.stats(since=params.get("since", "7d"), path=params.get("path")),
        "schema": lambda: h.schema(params.get("what", "observation")),
        "browser_status": h.browser_status,
        "browser_stop": h.browser_stop,
        # daemon-only introspection: which layer singletons are alive.
        "daemon_status": _daemon_status,
    }
    fn = table.get(method)
    if fn is None:
        return {"ok": False, "error": f"unknown method: {method!r}"}

    buf = io.StringIO()
    err = io.StringIO()
    try:
        # Handlers print their JSON payload to stdout; capture it. The int they
        # return is the process exit code (0 ok, 1 user error, 2 action fail).
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = fn()
    except Exception as e:  # noqa: BLE001 — RPC boundary
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "stderr": err.getvalue()}

    out = buf.getvalue().strip()
    payload: Any = None
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = {"raw": out}
    return {"ok": True, "result": payload, "exit_code": rc, "stderr": err.getvalue()}


def _daemon_status() -> int:
    """Introspection: report which layer singletons this daemon is holding.

    Prints JSON (like every other handler) and returns 0. Useful for
    ``qcu daemon status`` and for diagnosing whether state survived across
    commands (the whole point of the daemon).
    """
    import json as _json
    from qcu.layers.runtime import _INSTANCES

    instances = []
    for name, inst in _INSTANCES.items():
        # Surface a couple of telltale attributes so callers can see state did
        # survive (e.g. desktop_ax._target_app, web_a11y._page is not None).
        info: dict[str, Any] = {"name": name}
        for attr in ("_target_app", "_page", "_cdp_port"):
            if hasattr(inst, attr):
                v = getattr(inst, attr)
                info[attr.lstrip("_")] = (None if v is None else
                                          (str(v) if not isinstance(v, (str, int, float, bool)) else v))
        instances.append(info)
    print(_json.dumps({"instances": instances, "n": len(instances)}))
    return 0


class _Handler(socketserver.BaseRequestHandler):
    """Handle one JSON-RPC request per connection. Serial by construction
    (``socketserver.TCPServer`` serves one connection at a time, which is
    exactly QCU's one-action-at-a-time model)."""

    def handle(self) -> None:  # noqa: D401
        try:
            raw = self._recv_json()
        except _BadFrame as e:
            self._send({"ok": False, "error": f"bad request frame: {e}"})
            return
        if raw is None:
            return  # client closed without sending; nothing to do.
        if not isinstance(raw, dict):
            self._send({"ok": False, "error": "request must be an object"})
            return
        method = raw.get("method")
        params = raw.get("params", {})
        if not isinstance(method, str) or not isinstance(params, dict):
            self._send({"ok": False, "error": "request needs a string method and object params"})
            return
        if method == "ping":
            # Liveness must not call UI handlers or enter stdout redirection.
            self._send({"ok": True})
            return
        response = self.server.worker.submit(_dispatch, method, params).result()
        self._send(response)

    # ------------------------------------------------------------------
    # Framing: newline-terminated JSON (simple, debuggable with nc/telnet).
    # ------------------------------------------------------------------

    def _recv_json(self) -> Any:
        chunks: list[bytes] = []
        size = 0
        self.request.settimeout(5.0)
        while True:
            try:
                chunk = self.request.recv(65536)
            except OSError as exc:
                raise _BadFrame(str(exc)) from exc
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > 4 * 1024 * 1024:
                raise _BadFrame("request exceeds 4 MiB")
            if b"\n" in chunk:
                break
        data = b"".join(chunks).split(b"\n", 1)[0].strip()
        if not data:
            return None
        try:
            return json.loads(data.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise _BadFrame(str(e)) from e

    def _send(self, obj: dict[str, Any]) -> None:
        try:
            self.request.sendall((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError:
            pass  # client gone; nothing useful to do.


class _BadFrame(Exception):
    """Raised when an incoming frame can't be parsed as JSON."""


class _LoopbackTCPServer(socketserver.ThreadingTCPServer):
    """Serve the daemon on loopback only.

    Socket readers and ping can overlap; ALL UI handlers run on one persistent
    worker. A lock alone is insufficient: backend handles also need thread
    affinity. This keeps asyncio loops and stdout capture from racing.
    """

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, *args, **kwargs):
        self.worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="qcu-ui")
        super().__init__(*args, **kwargs)

    def server_close(self):
        from qcu.layers.runtime import close_active
        self.worker.submit(close_active).result()
        self.worker.shutdown(wait=True)
        super().server_close()

    def server_bind(self) -> None:
        # Force loopback — never expose the RPC endpoint beyond this machine.
        # It has no auth and grants full AX/CDP control.
        self.server_address = ("127.0.0.1", self.server_address[1])
        super().server_bind()


def serve(port: int) -> None:
    """Run the daemon's RPC server on ``port`` until killed. Blocks the caller.

    ``port`` must already be free (the launcher picks it and tells us). On
    bind failure the process exits non-zero so the launcher's readiness poll
    fails fast and it can retry.
    """
    server = _LoopbackTCPServer(("127.0.0.1", port), _Handler)
    # Advertise readiness on stderr for whoever launched us (optional; the
    # launcher also probes the port directly).
    import sys

    print(f"qcu-daemon ready on 127.0.0.1:{port}", file=sys.stderr, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


# ---------------------------------------------------------------------------
# Client side: a tiny function the CLI uses to call the daemon.
# ---------------------------------------------------------------------------


def call_daemon(port: int, method: str, params: dict[str, Any] | None = None,
                timeout: float = 120.0) -> dict[str, Any]:
    """Send one JSON-RPC request to the daemon on ``port``; return its response.

    Raises ``DaemonError`` on connection failure / timeout / non-JSON reply.
    Used by the thin CLI when it decides to route through the daemon.
    """
    request = json.dumps({"method": method, "params": params or {}}) + "\n"
    dispatched = False
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", port))
            dispatched = True  # sendall can partially send before raising
            s.sendall(request.encode("utf-8"))
            # Read until newline (response framing mirrors the request's).
            chunks: list[bytes] = []
            while True:
                chunk = s.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                if b"\n" in chunk:
                    break
            data = b"".join(chunks).strip()
            if not data:
                raise DaemonError("empty response from daemon", dispatched=True)
            resp = json.loads(data.decode("utf-8"))
            if not isinstance(resp, dict):
                raise DaemonError(f"non-object response: {resp!r}", dispatched=True)
            return resp
    except DaemonError:
        raise
    except (OSError, ValueError) as e:
        raise DaemonError(f"daemon call failed: {type(e).__name__}: {e}", dispatched=dispatched) from e


def health_check(port: int, timeout: float = 2.0) -> bool:
    """Cheap liveness probe independent of the UI worker and its stdout."""
    try:
        resp = call_daemon(port, "ping", {}, timeout=timeout)
        return bool(resp.get("ok"))
    except DaemonError:
        return False


class DaemonError(RuntimeError):
    """Raised when a daemon RPC fails at the transport level."""

    def __init__(self, message: str, *, dispatched: bool = False):
        super().__init__(message)
        self.dispatched = dispatched
