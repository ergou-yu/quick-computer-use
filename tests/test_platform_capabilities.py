"""Platform selection and dependency isolation without OS-specific imports."""
import json
import sys
import tomllib
from pathlib import Path

import pytest

from qcu import platforms
from qcu.common.types import Observation
from qcu.router.features import extract
from qcu.router.classifier import classify


@pytest.mark.parametrize("platform,backend", [("darwin", "desktop_ax"), ("win32", "desktop_uia"),
    ("linux", "desktop_linux"), ("harmonyos", "desktop_harmony"), ("other", "desktop_unsupported")])
def test_platform_routing(monkeypatch, platform, backend):
    monkeypatch.setattr(sys, "platform", platform)
    assert platforms.desktop_backend_name() == backend
    obs = Observation("desktop", routing_meta={"layer": backend})
    assert classify(extract(obs)).layer == backend


def test_dependency_present_is_not_session_available(monkeypatch):
    monkeypatch.setattr(platforms, "dependency_exists", lambda _: True)
    cap = platforms.desktop_capabilities(layer="desktop_uia", platform="win32")
    assert cap["implemented"] and cap["dependencies_present"]
    assert cap["session_accessible"] is None
    assert cap["available"] is False
    assert cap["reason"] == "target_not_probed"


def test_missing_dependency_and_unimplemented_are_distinct(monkeypatch):
    monkeypatch.setattr(platforms, "dependency_exists", lambda _: False)
    cap = platforms.desktop_capabilities(platform="win32")
    assert cap["implemented"] and not cap["dependencies_present"]
    assert cap["reason"] == "dependency_missing"
    linux = platforms.desktop_capabilities(platform="linux")
    assert linux["implemented"] is False
    assert linux["reason"] == "backend_not_implemented"


def test_platform_import_does_not_import_windows_dependencies():
    import subprocess
    result = subprocess.run([sys.executable, "-c",
        "import sys; import qcu.platforms; import qcu.layers.desktop_uia; "
        "assert 'uiautomation' not in sys.modules; assert 'comtypes' not in sys.modules"],
        capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_extras_have_platform_markers():
    data = tomllib.loads(Path("pyproject.toml").read_text())
    extras = data["project"]["optional-dependencies"]
    assert all("sys_platform == 'win32'" in req for req in extras["windows"])
    assert all("sys_platform == 'darwin'" in req for req in extras["macos"])


def test_registration_extension(monkeypatch):
    monkeypatch.setitem(platforms._DESKTOP_BACKENDS, "testos", "desktop_custom")
    assert platforms.desktop_backend_name("testos") == "desktop_custom"
    assert classify({"context": "desktop", "platform": "testos"}).layer == "desktop_custom"


@pytest.mark.parametrize("platform,backend", [("win32", "desktop_uia"), ("linux", "desktop_linux")])
def test_session_start_uses_platform_without_macos_preflight(monkeypatch, tmp_path, capsys, platform, backend):
    from qcu import cli_handlers as handlers, session
    monkeypatch.setattr(session, "SESSION_PATH", tmp_path / "session.json")
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(handlers, "_maybe_route_via_daemon", lambda *a, **k: None)
    monkeypatch.setattr(handlers.permissions, "probe_all", lambda: pytest.fail("macOS preflight on another OS"))
    assert handlers.session_start("desktop") == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["session"]["layer"] == backend
    assert payload["capabilities"]["backend"] == backend


@pytest.mark.parametrize("context,layer,scope", [("web", None, {"pid": 7}),
    ("web", "desktop_ax", {}), ("desktop", "web_a11y", {})])
def test_observe_rejects_cross_context_target(monkeypatch, tmp_path, capsys, context, layer, scope):
    from qcu import cli_handlers as handlers, session
    from qcu.layers import runtime
    monkeypatch.setattr(session, "SESSION_PATH", tmp_path / "session.json")
    session.new(context, "web_a11y" if context == "web" else "desktop_ax")
    monkeypatch.setattr(handlers, "_ensure_daemon_started", lambda: None)
    monkeypatch.setattr(handlers, "_maybe_route_via_daemon", lambda *a, **k: None)
    monkeypatch.setattr(runtime, "get_layer", lambda *_: pytest.fail("Cross-target backend must not start"))
    assert handlers.observe(layer, 8, **scope) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "target_mismatch"


def test_observe_error_returns_failure_without_changing_backend(monkeypatch, tmp_path, capsys):
    from qcu import cli_handlers as handlers, session
    from qcu.layers import runtime
    monkeypatch.setattr(session, "SESSION_PATH", tmp_path / "session.json")
    session.new("desktop", "desktop_uia")
    monkeypatch.setattr(handlers, "_ensure_daemon_started", lambda: None)
    monkeypatch.setattr(handlers, "_maybe_route_via_daemon", lambda *a, **k: None)
    class Failed:
        def observe(self, **kwargs):
            return Observation("desktop", routing_meta={"layer": "desktop_uia", "error": "Window missing"})
    calls = []
    def get_layer(name):
        calls.append(name)
        return Failed()
    monkeypatch.setattr(runtime, "get_layer", get_layer)
    monkeypatch.setattr(handlers.Telemetry, "record", lambda **_: None)
    assert handlers.observe(None, 8, pid=7) == 2
    assert calls == ["desktop_uia"]


def test_explicit_unimplemented_backend_reports_itself():
    from qcu.layers.runtime import get_layer
    layer = get_layer("desktop_harmony")
    obs = layer.observe()
    assert obs.routing_meta["layer"] == "desktop_harmony"
    assert obs.routing_meta["capabilities"]["implemented"] is False
    assert obs.routing_meta["reason"] == "backend_not_implemented"


def test_windows_session_liveness_never_calls_os_kill(monkeypatch, tmp_path):
    from qcu import session
    from qcu.layers import browser_daemon
    monkeypatch.setattr(session, "SESSION_PATH", tmp_path / "session.json")
    target = session.new("desktop", "desktop_uia")
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(session.os, "kill", lambda *_: pytest.fail("os.kill terminates on Windows"))
    seen = []
    monkeypatch.setattr(browser_daemon, "is_pid_alive", lambda pid: seen.append(pid) or True)
    assert session.is_alive(target) is True
    assert seen == [target.pid]


@pytest.mark.parametrize("layer,scope,context", [(None, {"pid": 7}, "desktop"),
    (None, {}, "web"), ("desktop_uia", {}, "desktop")])
def test_first_observation_starts_session_then_dispatches_once(monkeypatch, tmp_path, capsys, layer, scope, context):
    from qcu import cli_handlers as handlers, session
    from qcu.layers import runtime
    monkeypatch.setattr(session, "SESSION_PATH", tmp_path / "session.json")
    events = []
    def ensure():
        current = session.load()
        assert current is not None and current.context == context
        events.append("daemon_start")
    def dispatch(method, options):
        assert events == ["daemon_start"]
        assert method == "observe" and options["pid"] == scope.get("pid")
        events.append("observe_dispatch")
        print(json.dumps({"elements": [], "handled_by": "persistent_daemon"}))
        return 0
    monkeypatch.setattr(handlers, "_ensure_daemon_started", ensure)
    monkeypatch.setattr(handlers, "_maybe_route_via_daemon", dispatch)
    monkeypatch.setattr(runtime, "get_layer", lambda *_: pytest.fail("Must not re-observe locally after dispatch"))
    assert handlers.observe(layer, 8, **scope) == 0
    assert events == ["daemon_start", "observe_dispatch"]
    assert json.loads(capsys.readouterr().out)["handled_by"] == "persistent_daemon"
