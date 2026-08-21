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
import threading
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
        ),
        "act": lambda: h.act(params.get("action", ""), layer=params.get("layer")),
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
    try:
        # Handlers print their JSON payload to stdout; capture it. The int they
        # return is the process exit code (0 ok, 1 user error, 2 action fail).
        with contextlib.redirect_stdout(buf):
            rc = fn()
    except Exception as e:  # noqa: BLE001 — RPC boundary
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    out = buf.getvalue().strip()
    payload: Any = None
    if out:
        try:
            payload = json.loads(out)
        except json.JSONDecodeError:
            payload = {"raw": out}
    return {"ok": True, "result": payload, "exit_code": rc}


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
        method = raw.get("method")
        params = raw.get("params") or {}
        if not isinstance(method, str):
            self._send({"ok": False, "error": "request must have a string 'method'"})
            return
        response = _dispatch(method, params)
        self._send(response)

    # ------------------------------------------------------------------
    # Framing: newline-terminated JSON (simple, debuggable with nc/telnet).
    # ------------------------------------------------------------------

    def _recv_json(self) -> Any:
        chunks: list[bytes] = []
        while True:
            chunk = self.request.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
            if b"\n" in chunk:
                break
        data = b"".join(chunks).strip()
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

    ThreadingTCPServer lets overlapping connections not block the readiness
    probe (health check connects, then a real request connects). The handler
    is still cheap and requests are effectively serial because the CLI sends
    them one at a time; we keep threading only so the probe can't wedge a real
    caller. ``allow_reuse_address`` lets a restarted daemon rebind its port.
    """

    allow_reuse_address = True
    daemon_threads = True

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
        # Tear down any live layer instances on the way out. This is the ONLY
        # place the daemon calls close() — never between requests — because
        # WebA11yLayer._closed doesn't reset and reuse-after-close is broken.
        with contextlib.suppress(Exception):
            from qcu.layers.runtime import close_active

            close_active()
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
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(("127.0.0.1", port))
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
                raise DaemonError("empty response from daemon")
            resp = json.loads(data.decode("utf-8"))
            if not isinstance(resp, dict):
                raise DaemonError(f"non-object response: {resp!r}")
            return resp
    except DaemonError:
        raise
    except (OSError, json.JSONDecodeError) as e:
        raise DaemonError(f"daemon call failed: {type(e).__name__}: {e}") from e


def health_check(port: int, timeout: float = 2.0) -> bool:
    """Cheap liveness probe — True if the daemon answers ``daemon_status``."""
    try:
        resp = call_daemon(port, "daemon_status", {}, timeout=timeout)
        return bool(resp.get("ok"))
    except DaemonError:
        return False


class DaemonError(RuntimeError):
    """Raised when a daemon RPC fails at the transport level."""
