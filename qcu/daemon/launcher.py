"""Process lifecycle for the resident Python CLI daemon.

Mirrors ``qcu.layers.browser_daemon``: detached launch so the daemon survives
its launcher, a readiness probe so the launcher knows when to send the first
real request, and a kill path for shutdown. Reuses browser_daemon's
``is_pid_alive`` / ``pick_free_port`` / ``kill_pid`` verbatim — no point
duplicating that logic.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Optional

from qcu.layers.browser_daemon import is_pid_alive, kill_pid, pick_free_port
from qcu.daemon.server import health_check


def python_executable() -> str:
    """The Python interpreter that runs the daemon. Same binary as the CLI so
    AX permission (granted per-binary) and installed deps carry over."""
    return sys.executable


def launch_daemon(port: int, env: Optional[dict[str, str]] = None) -> int:
    """Start a detached daemon process bound to ``port``; return its pid.

    Detached (``start_new_session=True`` on POSIX) so the launcher — a
    short-lived CLI command — exiting cannot take the daemon down. The daemon
    is the long-lived half of the split; this is the same pattern
    browser_daemon uses for Chromium.
    """
    args = [python_executable(), "-m", "qcu.daemon", "--port", str(port)]
    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        # Keep stderr visible-ish during bring-up debugging, but detached so a
        # terminal close can't kill the daemon.
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env or os.environ.copy(),
    }
    if sys.platform == "win32":  # pragma: no cover — darwin first, win v2
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        popen_kwargs["start_new_session"] = True
    proc = subprocess.Popen(args, **popen_kwargs)
    return proc.pid


def launch_and_wait(port: Optional[int] = None, start_timeout: float = 15.0,
                    env: Optional[dict[str, str]] = None) -> tuple[int, int]:
    """Pick a port, launch, and wait for the daemon's RPC health check.

    Returns ``(pid, port)``. Raises ``RuntimeError`` if the daemon never
    becomes healthy (the usual cause is a Python import error inside the
    daemon — the process exits before binding).
    """
    port = port or pick_free_port(start=9400, end=9500)  # distinct range from CDP
    pid = launch_daemon(port, env=env)
    if not wait_for_daemon(port, timeout=start_timeout):
        # Best-effort cleanup so a half-started process doesn't leak.
        with __import__("contextlib").suppress(Exception):
            kill_pid(pid)
        raise RuntimeError(
            f"qcu-daemon did not become healthy on port {port} within {start_timeout}s"
        )
    return pid, port


def is_daemon_alive(port: Optional[int], pid: Optional[int] = None) -> bool:
    """True iff the daemon answers its health probe.

    A live pid but dead port means the daemon wedged (or is mid-restart); we
    treat the RPC endpoint as the source of truth because that's what the CLI
    actually talks to.
    """
    if not port:
        return False
    return health_check(port, timeout=1.5)


def wait_for_daemon(port: int, timeout: float = 15.0, interval: float = 0.15) -> bool:
    """Poll ``health_check`` until it responds or ``timeout`` elapses."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if health_check(port, timeout=1.0):
            return True
        time.sleep(interval)
    return False


def ensure_daemon(port: Optional[int], pid: Optional[int]) -> tuple[int, int]:
    """Return a ready ``(pid, port)`` for the daemon, launching if needed.

    Decision tree (same shape as browser_daemon.ensure_daemon):
    - RPC endpoint responds → reuse (pid stays as recorded, just refresh port).
    - pid alive but endpoint dead → wedged; kill, then relaunch.
    - neither → launch fresh.
    """
    if port and is_daemon_alive(port, pid):
        return (pid or 0), port

    # Reclaim a wedged-but-alive daemon before relaunching.
    if pid and is_pid_alive(pid):
        with __import__("contextlib").suppress(Exception):
            kill_pid(pid)

    new_pid, new_port = launch_and_wait()
    return new_pid, new_port
