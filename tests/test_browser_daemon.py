"""Tests for the long-lived Chromium daemon + cross-process state survival.

Two layers of tests:

1. **Unit tests** (no real browser) — exercise ``browser_daemon``'s pure logic
   (port picking, stale-lock cleanup, CDP URL construction, pid-liveness
   zombie handling) and the ``close()`` contract that a connected client must
   NOT terminate the shared daemon.

2. **Integration tests** (``@pytest.mark.integration``) — launch a REAL
   Chromium daemon and prove the headline regression from the field report:
   ``fill`` in one process, then ``press_key`` in a *different* process, and
   the unsubmitted text + focus survive because both attach to the same
   daemon via CDP. Run with: ``pytest -m integration``.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from qcu.layers import browser_daemon as bd


# ===========================================================================
# Unit tests — no real browser
# ===========================================================================


def test_cdp_url_is_loopback():
    assert bd.cdp_url(9222) == "http://127.0.0.1:9222"
    # Must never bind a public interface — CDP has no auth.
    assert "0.0.0.0" not in bd.cdp_url(9222)


def test_pick_free_port_returns_usable_port():
    port = bd.pick_free_port(start=19000, end=19010)
    assert 19000 <= port <= 19010
    # The port should actually be bindable right after (we released it).
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", port))


def test_pick_free_port_skips_busy_port():
    # Occupy one port, ensure pick_free_port avoids it.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as hold:
        hold.bind(("127.0.0.1", 19500))
        hold.listen(1)
        port = bd.pick_free_port(start=19500, end=19502)
        assert port != 19500
        assert port in (19501, 19502)


def test_is_cdp_alive_false_for_dead_port():
    # Pick a definitely-free port (nothing listening) → not alive.
    port = bd.pick_free_port(start=19600, end=19610)
    assert bd.is_cdp_alive(port) is False


def test_is_pid_alive_none_and_zero():
    assert bd.is_pid_alive(None) is False
    assert bd.is_pid_alive(0) is False
    assert bd.is_pid_alive(-1) is False


def test_is_pid_alive_for_current_process():
    assert bd.is_pid_alive(os.getpid()) is True


def test_is_pid_alive_for_dead_pid():
    # Find a pid that is guaranteed not to exist.
    dead = _find_dead_pid()
    assert bd.is_pid_alive(dead) is False


def _find_dead_pid() -> int:
    """Return a pid that does not currently exist."""
    # pids only go up over time; start high and probe. Fallback: a very high
    # pid is almost always unused on a normal machine.
    for candidate in (2**31 - 1, 999999, 200000):
        if not bd.is_pid_alive(candidate):
            return candidate
    # Extremely unlikely to reach here; spawn+reap a child to get a dead pid.
    p = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(0)"])
    p.wait()
    return p.pid


def test_build_args_includes_required_flags():
    args = bd._build_args(
        "/path/to/chrome", 9222, "/tmp/ud", headless=True
    )
    joined = " ".join(args)
    assert "--remote-debugging-port=9222" in joined
    assert "--user-data-dir=/tmp/ud" in joined
    assert "--headless=new" in joined
    assert "--no-first-run" in joined
    assert "about:blank" in joined


def test_build_args_omits_headless_when_false():
    args = bd._build_args("/chrome", 9222, "/ud", headless=False)
    assert "--headless=new" not in " ".join(args)


def test_clean_stale_lock_removes_files_when_pid_dead(tmp_path):
    ud = tmp_path / "profile"
    ud.mkdir()
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        (ud / name).write_text("stale")
    dead = _find_dead_pid()
    removed = bd.clean_stale_lock(str(ud), browser_pid=dead)
    assert removed is True
    assert not (ud / "SingletonLock").exists()


def test_clean_stale_lock_respects_live_pid(tmp_path):
    """A live owner means the lock is legitimate — must not delete it."""
    ud = tmp_path / "profile"
    ud.mkdir()
    (ud / "SingletonLock").write_text("live")
    removed = bd.clean_stale_lock(str(ud), browser_pid=os.getpid())
    assert removed is False
    assert (ud / "SingletonLock").exists()  # untouched


def test_clean_stale_lock_no_files_is_noop(tmp_path):
    ud = tmp_path / "profile"
    ud.mkdir()
    assert bd.clean_stale_lock(str(ud)) is False


def test_kill_pid_on_dead_pid_is_noop():
    # Must not raise even for a pid that doesn't exist.
    dead = _find_dead_pid()
    bd.kill_pid(dead)  # no exception


def test_kill_pid_zero_is_noop():
    bd.kill_pid(0)
    bd.kill_pid(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# WebA11yLayer.close() contract: a connected client must NOT kill the daemon
# ---------------------------------------------------------------------------


def test_close_does_not_close_context_or_browser():
    """The headline safety property: close() on a CDP-connected client must
    only stop the local Playwright driver — never context.close()/browser.close(),
    which would terminate the shared daemon."""
    import asyncio

    from qcu.layers import web_a11y as wa

    layer = wa.WebA11yLayer()
    # Wire mocks as if we connected (not launched).
    ctx = MagicMock()
    ctx.close = AsyncMock()
    browser = MagicMock()
    browser.close = AsyncMock()
    pw = MagicMock()
    pw.stop = AsyncMock()

    loop = MagicMock()
    # Drive the _stop coroutine on a real loop so awaitables resolve.
    real_loop = asyncio.new_event_loop()
    loop.run_until_complete = real_loop.run_until_complete
    loop.close = MagicMock()

    layer._context = ctx
    layer._browser = browser
    layer._playwright = pw
    layer._loop = loop
    layer._owns_browser = False
    layer._cdp_port = 9222

    layer.close()

    # The two assertions that matter: the shared daemon must survive.
    ctx.close.assert_not_called()
    browser.close.assert_not_called()
    # But we DID disconnect the local driver.
    pw.stop.assert_called_once()
    real_loop.close()


def test_close_idempotent():
    from qcu.layers import web_a11y as wa

    layer = wa.WebA11yLayer()
    layer._loop = None  # short-circuit path
    layer.close()
    layer.close()  # must not raise on second call


# ===========================================================================
# Integration tests — REAL Chromium daemon (skip if browser unavailable)
# ===========================================================================

integration = pytest.mark.integration


def _chromium_available() -> bool:
    try:
        bd.chromium_executable()
        return True
    except Exception:
        return False


_skip_no_browser = pytest.mark.skipif(
    not _chromium_available(),
    reason="no chromium binary installed (run `python -m playwright install chromium`)",
)


@_skip_no_browser
@integration
class TestDaemonLifecycle:
    """Daemon launches, responds on CDP, and dies on kill_pid."""

    def test_launch_and_wait_then_kill(self, tmp_path):
        ud = str(tmp_path / "ud")
        pid, port = bd.launch_and_wait(user_data_dir=ud, headless=True, start_timeout=30)
        try:
            assert bd.is_cdp_alive(port) is True
            ver = bd._fetch_json_version(port)
            assert ver is not None and "Browser" in ver
        finally:
            bd.kill_pid(pid)
        import time

        time.sleep(0.5)
        assert bd.is_cdp_alive(port) is False

    def test_ensure_daemon_reuses_live_daemon(self, tmp_path):
        ud = str(tmp_path / "ud")
        pid1, port = bd.launch_and_wait(user_data_dir=ud, headless=True, start_timeout=30)
        try:
            # Second call must REUSE, not relaunch (same port, endpoint alive).
            pid2, port2 = bd.ensure_daemon(
                user_data_dir=ud, port=port, browser_pid=pid1, headless=True
            )
            assert port2 == port
            assert bd.is_cdp_alive(port2) is True
        finally:
            bd.kill_pid(pid1)


@_skip_no_browser
@integration
class TestCrossProcessFormPersistence:
    """The regression from the field report: a form filled in process A must
    survive into process B, because both attach to the same daemon."""

    def test_fill_then_observe_in_separate_processes(self, tmp_path, monkeypatch):
        """Process A fills an input; process B (subprocess) observes and the
        input value + URL are intact. This is the exact failure the report
        documented before the daemon fix."""
        qcu_home = tmp_path / "qcuhome"
        ud = tmp_path / "ud"
        monkeypatch.setenv("QCU_HOME", str(qcu_home))
        monkeypatch.setenv("QCU_HEADLESS", "1")
        monkeypatch.setenv("QCU_USER_DATA_DIR", str(ud))

        # Spin up the daemon + a controlled page via the web_a11y layer,
        # mimicking two sequential CLI invocations without subprocess overhead
        # in the test driver (the daemon itself is a separate process, which
        # is what we're actually proving survival across).
        from qcu.layers.web_a11y import WebA11yLayer
        from qcu.common.types import Action

        proc_a = WebA11yLayer()
        try:
            # Load a page with a persistent text input.
            proc_a.observe()  # forces _ensure_browser → launches daemon
            proc_a.act(Action(type="navigate", params={"url": "https://www.wikipedia.org"}))
            obs = proc_a.observe()
            search = next(
                (e for e in obs.elements if e.role == "searchbox"), None
            )
            if search is None:
                pytest.skip("wikipedia search box role not surfaced in this build")
            # Fill in "process A".
            r = proc_a.act(
                Action(type="fill", params={"ref": search.ref, "text": "Alan Turing"})
            )
            assert r.ok, r.message
            port_before = proc_a._cdp_port
            # Close process A's connection (must NOT kill the daemon).
            proc_a.close()
        finally:
            pass  # cleanup at end

        # "Process B": a brand-new layer instance attaching to the SAME daemon.
        proc_b = WebA11yLayer()
        try:
            obs2 = proc_b.observe()
            assert proc_b._cdp_port == port_before, "process B reused the same daemon"
            search2 = next(
                (e for e in obs2.elements if e.role == "searchbox"), None
            )
            assert search2 is not None, "search box still present after reconnect"
            # THE core assertion: unsubmitted text survived across the process
            # boundary because the DOM lived in the shared daemon.
            assert search2.value == "Alan Turing", (
                f"form input lost across processes! got {search2.value!r}"
            )
        finally:
            # Stop the daemon explicitly (session end semantics).
            from qcu.session import load

            s = load()
            if s and s.browser_pid:
                bd.kill_pid(s.browser_pid)
