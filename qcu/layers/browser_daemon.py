"""Long-lived Chromium daemon, controlled via the Chrome DevTools Protocol.

The problem this module solves: each ``qcu`` CLI invocation is a fresh process,
and Playwright's ``launch_persistent_context`` launches a brand-new browser each
time — fighting every other process for the user-data-dir lock and wiping the
previous process's DOM, focus, and unsubmitted form state. The fix is to run
Chromium **once** as a detached daemon exposing ``--remote-debugging-port``, and
have every short-lived CLI command attach via ``connect_over_cdp``.

This module is deliberately Playwright-free (only stdlib + ``subprocess``) so it
can be imported cheaply and tested without a browser. The browser binary itself
is resolved through Playwright's ``chromium.executable_path`` to avoid hard-coding
paths across machines.

Lifecycle rules (see references/api.md for the full picture):

1. ``launch_chromium`` starts a **detached** process (own session/process group)
   so a launcher crash or terminal close cannot take the browser down.
2. ``wait_for_cdp`` polls ``/json/version`` — a forked process is NOT a ready
   browser; only a responding CDP endpoint is.
3. Short-lived clients must ``connect_over_cdp`` and then ``playwright.stop()``
   — they must NEVER call ``context.close()``/``browser.close()``, which would
   terminate the shared daemon.
4. ``kill_pid`` (used by ``qcu session end``) is the only blessed way to stop
   the daemon: SIGTERM → grace → SIGKILL on POSIX, ``taskkill /T /F`` on Windows.
5. The CDP endpoint is loopback-only and has no auth — never expose the port.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# Loopback CDP endpoint. Loopback only — the endpoint has full browser control
# with no authentication, so it must never be reachable from the network.
CDP_HOST = "127.0.0.1"
# Chromium's conventional debugging port; we walk upward from here looking for a
# free one so a second daemon doesn't collide with the first.
DEFAULT_PORT_START = 9222
DEFAULT_PORT_END = 9322


def cdp_url(port: int) -> str:
    """HTTP base URL Playwright's ``connect_over_cdp`` expects."""
    return f"http://{CDP_HOST}:{port}"


# ---------------------------------------------------------------------------
# Port + endpoint health
# ---------------------------------------------------------------------------


def pick_free_port(start: int = DEFAULT_PORT_START, end: int = DEFAULT_PORT_END) -> int:
    """Find a free loopback port in ``[start, end]``.

    We probe a range rather than asking the OS for "any" port because we want a
    stable, recognizable number to persist into ``session.json``; a fully random
    port would still work but is harder to eyeball in diagnostics.
    """
    for port in range(start, end + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                s.bind((CDP_HOST, port))
                return port
        except OSError:
            continue
    # Last resort: let the OS choose.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((CDP_HOST, 0))
        return s.getsockname()[1]


def _port_listening(port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((CDP_HOST, port), timeout=timeout):
            return True
    except OSError:
        return False


def _fetch_json_version(port: int, timeout: float = 1.0) -> Optional[dict]:
    """Hit ``GET /json/version`` — the canonical "CDP is ready" probe.

    A TCP connect can succeed before the HTTP endpoint is wired up, so for
    readiness we require the HTTP response, not just an open socket.
    """
    url = f"{cdp_url(port)}/json/version"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            if r.status != 200:
                return None
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def is_cdp_alive(port: int) -> bool:
    """True iff the CDP HTTP endpoint responds (browser truly ready)."""
    return _fetch_json_version(port) is not None


def wait_for_cdp(port: int, timeout: float = 15.0, interval: float = 0.15) -> bool:
    """Poll ``/json/version`` until it responds or ``timeout`` elapses.

    Returns True once the browser is genuinely ready to accept CDP clients.
    A freshly forked Chromium is NOT ready — it can take a second or two to
    bind the debugging endpoint, especially cold start.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_cdp_alive(port):
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Process liveness
# ---------------------------------------------------------------------------


def is_pid_alive(pid: Optional[int]) -> bool:
    """True iff a process with ``pid`` is running (not gone, not a zombie).

    ``pid=None``/``<=0`` → False. A zombie (defunct) process counts as dead:
    Chromium daemons are detached (reparented to init), so after SIGTERM they
    linger as zombies until init reaps them; treating a zombie as "alive" would
    make ``kill_pid`` wait forever and ``ensure_daemon`` refuse to relaunch.
    """
    if not pid or pid <= 0:
        return False
    if sys.platform == "win32":
        # os.kill(pid,0) semantics differ on Windows; probe via tasklist.
        try:
            r = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                capture_output=True, text=True, timeout=2,
            )
            return str(pid) in (r.stdout or "")
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False  # no such process
    except PermissionError:
        return True  # exists but not ours — treat as alive
    except OSError:
        return False
    # Process exists, but it may be a zombie. Probe via waitpid(WNOHANG): a
    # return of the pid means it was reaped (now truly gone); 0 means still
    # running. We only reap our own (detached) children opportunistically.
    try:
        waited, _ = os.waitpid(pid, os.WNOHANG)
        if waited == pid:
            return False  # reaped — it's gone now
    except (ChildProcessError, OSError):
        # Not our child (e.g. reparented to init already): fall back to a
        # stat-based zombie check so we don't treat a defunct proc as alive.
        try:
            r = subprocess.run(
                ["ps", "-p", str(pid), "-o", "stat="],
                capture_output=True, text=True, timeout=2,
            )
            state = (r.stdout or "").strip()
            if state and state[0] in ("Z",):  # zombie / defunct
                return False
        except Exception:
            pass
    return True


# ---------------------------------------------------------------------------
# Chromium executable resolution
# ---------------------------------------------------------------------------


def chromium_executable(channel: Optional[str] = None) -> str:
    """Resolve the Chromium binary path via Playwright.

    Prefer the bundled Playwright Chromium (channel=None). If ``channel`` is
    set (``chrome``/``msedge``) use that system build instead. Resolving through
    Playwright keeps this portable across machines and install layouts.

    Raises RuntimeError if Playwright is missing or the binary isn't installed.
    """
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError as e:  # pragma: no cover - exercised via _ensure_browser
        raise RuntimeError(
            "playwright is required to resolve the chromium binary; "
            "install with `pip install playwright && python -m playwright install chromium`"
        ) from e

    # Resolve synchronously via the sync API to avoid an event loop here.
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        if channel:
            # Playwright knows the channel's executable; the property raises a
            # helpful error if the channel isn't installed.
            try:
                return pw.chromium.executable_path
            except Exception:
                # Fallback: let the OS find it by channel name; the daemon will
                # fail loudly via subprocess if it really isn't there.
                return channel
        return pw.chromium.executable_path


# ---------------------------------------------------------------------------
# Daemon launch
# ---------------------------------------------------------------------------


def _build_args(
    executable: str,
    port: int,
    user_data_dir: str,
    headless: bool,
    extra_args: Optional[list[str]] = None,
) -> list[str]:
    args = [
        executable,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        # Keep the viewport in sync with web_a11y's expectations so coordinate
        # clicks land correctly.
        "--window-size=1280,800",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate",
        # Chrome 136+ ignores --remote-debugging-port for the default profile;
        # we always pass an explicit user-data-dir (above) so we stay compliant.
        # See https://developer.chrome.com/blog/remote-debugging-port
    ]
    if headless:
        # --headless=new is the modern headless; old headless is removed in
        # recent Chrome and misbehaves for some a11y APIs.
        args.append("--headless=new")
    if extra_args:
        args.extend(extra_args)
    # An initial URL keeps the first tab real (some CDP calls want a page).
    args.append("about:blank")
    return args


def launch_chromium(
    *,
    port: int,
    user_data_dir: str,
    headless: bool = True,
    channel: Optional[str] = None,
    executable: Optional[str] = None,
    extra_args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
) -> int:
    """Start a detached Chromium daemon exposing CDP on ``port``.

    Returns the daemon PID. The process is started in its own session/process
    group so that the launcher (a short-lived CLI command) exiting or its
    terminal closing cannot terminate the browser. ``session.json`` is expected
    to persist ``port`` + the returned pid so ``qcu session end`` can stop it.
    """
    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    exe = executable or chromium_executable(channel)
    args = _build_args(exe, port, user_data_dir, headless, extra_args)

    popen_kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": env or os.environ.copy(),
    }
    if sys.platform == "win32":
        # DETACHED_PROCESS: no console inheritance.
        # CREATE_NEW_PROCESS_GROUP: Ctrl+C won't propagate to the daemon.
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            | getattr(subprocess, "DETACHED_PROCESS", 0)
        )
    else:
        # Own session/process group — decoupled from the launcher's terminal.
        popen_kwargs["start_new_session"] = True

    proc = subprocess.Popen(args, **popen_kwargs)
    return proc.pid


def launch_and_wait(
    *,
    user_data_dir: str,
    headless: bool = True,
    channel: Optional[str] = None,
    port: Optional[int] = None,
    start_timeout: float = 15.0,
    env: Optional[dict[str, str]] = None,
) -> tuple[int, int]:
    """Pick a free port, launch, and wait for CDP readiness.

    Returns ``(pid, port)``. Raises ``RuntimeError`` if the endpoint never
    comes up (the most common cause is the chromium binary not being installed
    or crashing on startup — check stderr by NOT redirecting it).
    """
    port = port or pick_free_port()
    pid = launch_chromium(
        port=port,
        user_data_dir=user_data_dir,
        headless=headless,
        channel=channel,
        env=env,
    )
    if not wait_for_cdp(port, timeout=start_timeout):
        # Best-effort cleanup so a half-started process doesn't leak.
        try:
            kill_pid(pid)
        except Exception:
            pass
        raise RuntimeError(
            f"chromium daemon did not expose CDP on port {port} within {start_timeout}s; "
            "is the chromium binary installed? (`python -m playwright install chromium`)"
        )
    return pid, port


# ---------------------------------------------------------------------------
# Daemon shutdown
# ---------------------------------------------------------------------------


def kill_pid(pid: int, grace_s: float = 2.0) -> None:
    """Terminate a daemon process: SIGTERM → grace → SIGKILL (POSIX).

    On Windows, ``taskkill /PID /T /F`` kills the process tree. Idempotent: a
    missing pid is silently ignored (the daemon may already be gone).
    """
    if not pid or pid <= 0:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except Exception:
            pass
        return

    # POSIX: ask nicely, then force.
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return  # already gone
        except PermissionError:
            return  # not ours; leave it
        except OSError:
            return
        if sig == signal.SIGKILL:
            return
        # Wait out the grace period after SIGTERM before escalating.
        deadline = time.time() + grace_s
        while time.time() < deadline:
            if not is_pid_alive(pid):
                return
            time.sleep(0.1)


# ---------------------------------------------------------------------------
# Stale-lock recovery
# ---------------------------------------------------------------------------


def clean_stale_lock(user_data_dir: str, browser_pid: Optional[int] = None) -> bool:
    """Remove Chromium's singleton lock files if the owning pid is dead.

    Chromium writes ``SingletonLock`` (symlink → PID), ``SingletonCookie`` and
    ``SingletonSocket`` into the user-data-dir to enforce one-process-per-profile.
    A hard crash (SIGKILL / power loss) leaves a stale lock pointing at a dead
    pid; a subsequent launch then refuses to start. We only remove the lock when
    the recorded pid is confirmed dead, to avoid racing a live browser.

    Returns True if anything was removed.
    """
    if browser_pid and is_pid_alive(browser_pid):
        # A live owner means the lock is legitimate — do not touch it.
        return False

    ud = Path(user_data_dir)
    removed = False
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        p = ud / name
        if p.exists() or p.is_symlink():
            try:
                if p.is_symlink() or p.is_file():
                    p.unlink()
                removed = True
            except OSError:
                pass
    return removed


def purge_profile(user_data_dir: str, *, owned: bool) -> tuple[bool, Optional[str]]:
    """Delete a browser profile directory created by QCU.

    Safety policy:
    - Only delete when ``owned`` is True (i.e. the profile was QCU-managed under
      ``$QCU_HOME/browser-data``). A user-supplied QCU_USER_DATA_DIR is never
      recursively removed — that's their data.
    - Never follow a symlink outside the QCU home or act on a path that escapes
      it (guards against traversal/symlink tricks in a recorded profile path).
    - Refuse if any Chromium process is still alive against the directory.

    Returns (True, None) on success or (False, reason) when deletion was
    deliberately skipped.
    """
    from qcu.session import _qcu_home

    target = Path(user_data_dir).expanduser().resolve(strict=False)
    home = _qcu_home().resolve(strict=False)
    try:
        target.relative_to(home)
    except ValueError:
        return False, f"path outside QCU home: {target}"
    # Symlink at the directory level must point inside the QCU home too.
    if target.is_symlink():
        return False, "refusing to purge a symlinked profile"

    if not target.exists():
        return False, "profile directory not found"

    if not owned:
        return False, "profile is user-supplied (QCU_USER_DATA_DIR); not deleting"

    expected = (home / "browser-data").resolve(strict=False)
    try:
        same = target.samefile(expected)
    except OSError:
        # samefile raises if either path doesn't exist; fall back to string compare.
        same = str(target) == str(expected)
    if not same:
        return False, f"unexpected managed profile path: {target}"

    # Refuse to delete while the browser might still hold files open.
    if _profile_in_use(target):
        return False, "browser still running against profile; stop it first"

    try:
        shutil.rmtree(target)
        return True, None
    except OSError as e:
        return False, f"{type(e).__name__}: {e}"


def _profile_in_use(profile: Path) -> bool:
    """Detect whether a Chromium instance still holds the SingletonLock."""
    lock = profile / "SingletonLock"
    if not (lock.exists() or lock.is_symlink()):
        return False
    try:
        link = lock.readlink() if lock.is_symlink() else None
    except OSError:
        return False
    if link is None:
        return False
    # Chromium writes the lock as "hostname-PID"; the trailing number is the pid.
    import re

    m = re.search(r"-(\d+)$", str(link))
    if not m:
        return False
    try:
        return is_pid_alive(int(m.group(1)))
    except (ValueError, OSError):
        return False



# ---------------------------------------------------------------------------
# Convenience: connect-the-dots for a CLI command
# ---------------------------------------------------------------------------


def ensure_daemon(
    *,
    user_data_dir: str,
    port: Optional[int],
    browser_pid: Optional[int],
    headless: bool = True,
    channel: Optional[str] = None,
    start_timeout: float = 15.0,
) -> tuple[int, int]:
    """Return a ready ``(pid, port)`` for the daemon, launching if needed.

    Decision tree:
    - If ``port`` is given AND its CDP endpoint responds → reuse it (pid best-
      effort from the live process; keep the recorded ``browser_pid``).
    - Else if ``browser_pid`` is alive but port is dead → browser hung; kill it,
      clean stale lock, relaunch.
    - Else → clean any stale lock and launch fresh.
    """
    # Reuse a live, responding daemon.
    if port and is_cdp_alive(port):
        pid = browser_pid if (browser_pid and is_pid_alive(browser_pid)) else 0
        return pid, port

    # Browser pid alive but endpoint dead → wedged process; reclaim it.
    if browser_pid and is_pid_alive(browser_pid):
        try:
            kill_pid(browser_pid)
        except Exception:
            pass

    # Stale lock from a prior hard crash would block the new launch.
    clean_stale_lock(user_data_dir, browser_pid)

    pid, port = launch_and_wait(
        user_data_dir=user_data_dir,
        headless=headless,
        channel=channel,
        port=port,
        start_timeout=start_timeout,
    )
    return pid, port
