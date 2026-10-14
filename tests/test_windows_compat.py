"""Windows regressions with real processes; provider checks use the smoke script."""
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from qcu.layers import browser_daemon as bd
from qcu.router.classifier import classify


@pytest.mark.parametrize("platform,backend", [("win32", "desktop_uia"), ("darwin", "desktop_ax")])
@pytest.mark.parametrize("bridge", ["desktop_cdp", "desktop_jsbridge"])
def test_bound_bridge_beats_platform_default(platform, backend, bridge):
    decision = classify({"context": "desktop", "platform": platform,
                         "desktop_backend": backend, "observed_layer": bridge})
    assert decision.layer == bridge


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows process handles")
def test_windows_liveness_never_signals_or_spawns(monkeypatch):
    from qcu.layers.desktop_jsbridge import _pid_alive
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("liveness must not terminate a process"))
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("no tasklist subprocess"))
    assert bd.is_pid_alive(os.getpid())
    assert _pid_alive(os.getpid())
    assert not bd.is_pid_alive(2**31 - 1)


@pytest.mark.skipif(sys.platform != "win32", reason="real Windows process handles")
def test_exited_process_with_still_active_exit_code_is_dead():
    # GetExitCodeProcess alone mistakes exit code 259 (STILL_ACTIVE) for alive.
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.exit(259)"])
    child.wait(timeout=5)
    assert not bd.is_pid_alive(child.pid)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 boundary")
def test_process_handles_closed_and_access_denied_is_conservative(monkeypatch):
    import ctypes
    from qcu.common import win32
    closed = []
    fake = SimpleNamespace(OpenProcess=lambda *_: 123,
                           WaitForSingleObject=lambda *_: 258,
                           CloseHandle=lambda handle: closed.append(handle))
    monkeypatch.setattr(win32, "_kernel", lambda: fake)
    assert win32.is_process_alive(7)
    assert closed == [123]
    fake.OpenProcess = lambda *_: 0
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 5)
    assert win32.is_process_alive(7)
    monkeypatch.setattr(ctypes, "get_last_error", lambda: 87)
    assert not win32.is_process_alive(7)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shells")
@pytest.mark.parametrize("shell", ["powershell", "cmd"])
def test_windows_entrypoint_from_outside_source(shell, tmp_path):
    root = Path(__file__).resolve().parents[1]
    env = dict(os.environ, QCU_PYTHON=sys.executable, QCU_HOME=str(tmp_path / "qcu"))
    if shell == "powershell":
        command = ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(root / "scripts" / "qcu.ps1"), "--version"]
    else:
        command = ["cmd.exe", "/d", "/c", str(root / "scripts" / "qcu.cmd"), "--version"]
    proc = subprocess.run(command, cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.startswith("qcu ")


def test_cli_stdin_preserves_unicode_and_bom(tmp_path):
    root = Path(__file__).resolve().parents[1]
    # Validate the real JSON CLI boundary without starting a browser/daemon.
    env = dict(os.environ, QCU_HOME=str(tmp_path / "qcu"),
               PYTHONIOENCODING="cp1252" if sys.platform == "win32" else "utf-8")
    proc = subprocess.run([sys.executable, "-m", "qcu", "batch", "-"], cwd=root, env=env,
                          input='\ufeff[{"type":"未知 🚀"}]'.encode("utf-8"), capture_output=True, timeout=10)
    assert proc.returncode == 1
    payload = json.loads(proc.stderr.decode("utf-8"))
    assert "未知 🚀" in payload["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PowerShell 5.1 native stdin")
def test_powershell_pipeline_preserves_unicode(tmp_path):
    root = Path(__file__).resolve().parents[1]
    fixture = tmp_path / "actions.json"
    fixture.write_text('[{"type":"未知 🚀"}]', encoding="utf-8")
    quote = lambda path: "'" + str(path).replace("'", "''") + "'"
    script = (f"Get-Content -Raw -Encoding UTF8 -LiteralPath {quote(fixture)} | "
              f"& {quote(root / 'scripts' / 'qcu.ps1')} batch -")
    env = dict(os.environ, QCU_PYTHON=sys.executable, QCU_HOME=str(tmp_path / "qcu"))
    proc = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                          cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=10)
    assert proc.returncode == 1
    assert "未知 🚀" in json.loads(proc.stderr)["error"]


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 TCP ownership API")
@pytest.mark.parametrize("family,host", [(2, "127.0.0.1"), (23, "::1")])
def test_live_listener_is_found_only_for_owning_pid(monkeypatch, family, host):
    import socket
    from qcu.layers._cdp_bridge import list_listening_ports
    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("no netstat subprocess"))
    with socket.socket(family, socket.SOCK_STREAM) as listener:
        listener.bind((host, 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        ports, error = list_listening_ports(os.getpid())
        assert error is None and port in ports
        foreign, error = list_listening_ports(2**31 - 1)
        assert error is None and port not in foreign
