"""Tests for the desktop CDP bridge (Electron/CEF → web_a11y over CDP).

Covers the pure seams only — no real Electron app, no Playwright, no sockets:

  1. Port discovery parses lsof (macOS) / netstat (Windows) / ss (Linux)
     output and reports tool failures honestly.
  2. /json/version probing accepts only real CDP endpoints.
  3. find_cdp_endpoint explains every outcome (no ports / no CDP / found).
  4. _select_external_page refuses to guess on zero or multiple matches.
  5. desktop_cdp.observe reports a missing endpoint instead of substituting
     another target.
  6. The router keeps follow-up actions on desktop_cdp after a bridged
     observation.
  7. A plain web_a11y request detaches a stale external binding.
"""

from __future__ import annotations

import json
import subprocess
import sys
import types

import pytest

from qcu.layers import _cdp_bridge
from qcu.layers.web_a11y import _select_external_page


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ===========================================================================
# Port discovery parsing
# ===========================================================================

def test_lsof_listening_ports_parsed(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    out = "p1234\nf42\nn127.0.0.1:9222\nf43\nn*:54321 (LISTEN)\n"
    ports, err = _cdp_bridge.list_listening_ports(1234, runner=lambda *a, **k: _completed(out))
    assert err is None
    assert ports == [9222, 54321]


def test_lsof_no_match_is_empty_not_error(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    ports, err = _cdp_bridge.list_listening_ports(1234, runner=lambda *a, **k: _completed("", returncode=1))
    assert (ports, err) == ([], None)


def test_netstat_listening_ports_filtered_by_pid(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    out = (
        "  TCP    127.0.0.1:9222       0.0.0.0:0              LISTENING       1234\n"
        "  TCP    127.0.0.1:9222       0.0.0.0:0              ESTABLISHED     1234\n"
        "  TCP    127.0.0.1:5555       0.0.0.0:0              LISTENING       9999\n"
    )
    ports, err = _cdp_bridge.list_listening_ports(1234, runner=lambda *a, **k: _completed(out))
    assert err is None
    assert ports == [9222]


def test_discovery_tool_missing_is_reported(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError(2, "No such file or directory", "lsof")

    ports, err = _cdp_bridge.list_listening_ports(1234, runner=boom)
    assert ports == []
    assert "lsof" in err


def test_invalid_pid_rejected():
    for bad in (0, -5, True):
        ports, err = _cdp_bridge.list_listening_ports(bad)
        assert ports == [] and err


# ===========================================================================
# CDP probing
# ===========================================================================

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_probe_accepts_real_cdp_endpoint():
    payload = json.dumps({"Browser": "Chrome/126.0", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/x"}).encode()
    info = _cdp_bridge.probe_cdp_version(9222, opener=lambda url, timeout: _FakeResponse(payload))
    assert info == {"port": 9222, "url": "http://127.0.0.1:9222",
                    "browser": "Chrome/126.0", "protocol_version": ""}


def test_probe_rejects_non_cdp_http():
    payload = json.dumps({"hello": "world"}).encode()  # no webSocketDebuggerUrl
    assert _cdp_bridge.probe_cdp_version(9222, opener=lambda url, timeout: _FakeResponse(payload)) is None


def test_probe_rejects_connection_failure():
    def boom(url, timeout):
        raise OSError("connection refused")
    assert _cdp_bridge.probe_cdp_version(9222, opener=boom) is None


# ===========================================================================
# find_cdp_endpoint — honest diagnostics
# ===========================================================================

def test_find_endpoint_none_when_app_has_no_ports(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    endpoint, diag = _cdp_bridge.find_cdp_endpoint(1234, runner=lambda *a, **k: _completed(""))
    assert endpoint is None
    assert diag["reason"] == "no_listening_ports"
    assert "remote-debugging-port" in diag["hint"]


def test_find_endpoint_reports_ports_without_cdp(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    endpoint, diag = _cdp_bridge.find_cdp_endpoint(
        1234,
        runner=lambda *a, **k: _completed("p1234\nn*:8080\n"),
        opener=lambda url, timeout: (_ for _ in ()).throw(OSError("refused")),
    )
    assert endpoint is None
    assert diag["reason"] == "no_cdp_endpoint"
    assert diag["ports_checked"] == [8080]


def test_find_endpoint_success(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    payload = json.dumps({"Browser": "Chrome/126.0", "webSocketDebuggerUrl": "ws://x"}).encode()
    endpoint, diag = _cdp_bridge.find_cdp_endpoint(
        1234,
        runner=lambda *a, **k: _completed("p1234\nn*:9222\n"),
        opener=lambda url, timeout: _FakeResponse(payload),
    )
    assert endpoint["url"] == "http://127.0.0.1:9222"
    assert diag["browser"] == "Chrome/126.0"


# ===========================================================================
# Page selection — never guess
# ===========================================================================

def test_select_page_unique_match():
    page, err = _select_external_page([("p1", "Settings"), ("p2", "Chat")], "chat")
    assert page == "p2" and err is None


def test_select_page_no_hint_single_page():
    page, err = _select_external_page([("p1", "Only")], "")
    assert page == "p1" and err is None


def test_select_page_no_match_lists_titles():
    page, err = _select_external_page([("p1", "Settings")], "missing")
    assert page is None
    assert err["reason"] == "page_not_found" and err["pages"] == ["Settings"]


def test_select_page_ambiguous_lists_titles():
    page, err = _select_external_page([("p1", "A"), ("p2", "B")], "")
    assert page is None
    assert err["reason"] == "ambiguous_page" and err["pages"] == ["A", "B"]


# ===========================================================================
# desktop_cdp layer — failure honesty
# ===========================================================================

@pytest.fixture
def cdp_layer():
    from qcu.layers.desktop_cdp import DesktopCDPLayer
    return DesktopCDPLayer()


def test_observe_without_endpoint_reports_hint(cdp_layer, monkeypatch):
    monkeypatch.setattr(_cdp_bridge, "find_cdp_endpoint",
                        lambda pid, **k: (None, {"pid": pid, "reason": "no_listening_ports",
                                                 "hint": "relaunch with --remote-debugging-port"}))
    obs = cdp_layer.observe(pid=1234)
    assert obs.context == "desktop"
    assert obs.elements == []
    assert obs.routing_meta["reason"] == "no_listening_ports"
    assert "remote-debugging-port" in obs.routing_meta["hint"]


def test_act_without_binding_refuses(cdp_layer):
    from qcu.common.types import Action
    result = cdp_layer.act(Action(type="click", params={"ref": "x"}))
    assert result.ok is False
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "target_unbound"


def test_invalid_pid_refused(cdp_layer):
    obs = cdp_layer.observe(pid=-1)
    assert obs.routing_meta["reason"] == "invalid_target"


def test_observe_reuses_bound_target(cdp_layer, monkeypatch):
    cdp_layer._bound = {"pid": 4321, "app": "Demo", "endpoint": "http://127.0.0.1:9222", "title_hint": ""}
    seen = {}

    def fake_find(pid, **k):
        seen["pid"] = pid
        return None, {"pid": pid, "reason": "no_listening_ports"}

    monkeypatch.setattr(_cdp_bridge, "find_cdp_endpoint", fake_find)
    cdp_layer.observe()  # no explicit pid/app — must reuse the binding
    assert seen["pid"] == 4321


# ===========================================================================
# Router keeps the bridge
# ===========================================================================

def test_router_keeps_desktop_cdp_layer():
    from qcu.router.classifier import classify
    decision = classify({"context": "desktop", "observed_layer": "desktop_cdp",
                         "platform": "darwin", "desktop_backend": "desktop_ax",
                         "n_elements": 10, "n_interactive": 5}, last_action=None)
    assert decision.layer == "desktop_cdp"


# ===========================================================================
# Detach hook — plain web never inherits an Electron attach
# ===========================================================================

def test_plain_web_request_detaches_external():
    from qcu import cli_handlers
    from qcu.layers.runtime import get_layer
    web = get_layer("web_a11y")
    web._external = {"endpoint": "http://127.0.0.1:9222"}
    try:
        cli_handlers._detach_external_if_plain_web("web_a11y")
        assert web._external is None
        cli_handlers._detach_external_if_plain_web("desktop_cdp")  # no-op safety
        assert web._external is None
    finally:
        web._external = None
