"""Tests for the desktop JS bridge layer (WKWebView in-app debug bridge).

All HTTP is faked through the ``_http`` seam and ``find_bridge``; no real app,
no sockets. Pinpoints:

  1. Discovery honesty: dead pid / missing file / invalid file / silent port
     each report a distinct reason, with the embed hint when no file exists.
  2. observe maps the bridge's DOM JSON into Elements with scoped refs.
  3. act dispatch semantics: ok→sent, element gone→not_sent, JS dispatch
     error→unknown (retry_safe=False), transport failure→unknown.
  4. Refs go stale across observations; cross-target refs are rejected.
  5. verify conditions evaluate against the bridged DOM and reach
     outcome=verified only on a real match.
  6. The router keeps follow-up actions on desktop_jsbridge.
"""

from __future__ import annotations

import json
import os

import pytest

from qcu.common.types import Action
from qcu.layers import desktop_jsbridge as dj
from qcu.layers.desktop_jsbridge import DesktopJSBridgeLayer


# ===========================================================================
# Discovery
# ===========================================================================

def _write_bridge(tmp_path, pid, port=45555, token="tok-123"):
    d = tmp_path / "bridges"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{pid}.json").write_text(json.dumps(
        {"pid": pid, "port": port, "token": token, "app": "Probe"}))


def test_find_bridge_reports_dead_pid(monkeypatch, tmp_path):
    monkeypatch.setenv("QCU_BRIDGE_DIR", str(tmp_path / "bridges"))
    monkeypatch.setattr(dj, "_pid_alive", lambda pid: False)
    bridge, diag = dj.find_bridge(9999)
    assert bridge is None and diag["reason"] == "pid_not_running"


def test_find_bridge_reports_missing_file_with_hint(monkeypatch, tmp_path):
    monkeypatch.setenv("QCU_BRIDGE_DIR", str(tmp_path / "bridges"))
    bridge, diag = dj.find_bridge(os.getpid())
    assert bridge is None
    assert diag["reason"] == "no_bridge_file"
    assert "QCUWebViewBridge" in diag["hint"]


def test_find_bridge_rejects_invalid_file(monkeypatch, tmp_path):
    monkeypatch.setenv("QCU_BRIDGE_DIR", str(tmp_path / "bridges"))
    d = tmp_path / "bridges"
    d.mkdir(parents=True)
    (d / f"{os.getpid()}.json").write_text("{not json")
    bridge, diag = dj.find_bridge(os.getpid())
    assert bridge is None and diag["reason"] == "bridge_file_unreadable"


def test_find_bridge_reports_silent_port(monkeypatch, tmp_path):
    monkeypatch.setenv("QCU_BRIDGE_DIR", str(tmp_path / "bridges"))
    _write_bridge(tmp_path, os.getpid())

    def dead(method, url, **kw):
        raise OSError("connection refused")

    bridge, diag = dj.find_bridge(os.getpid(), opener=dead)
    assert bridge is None and diag["reason"] == "bridge_not_responding"


def test_find_bridge_success(monkeypatch, tmp_path):
    monkeypatch.setenv("QCU_BRIDGE_DIR", str(tmp_path / "bridges"))
    _write_bridge(tmp_path, os.getpid())
    bridge, diag = dj.find_bridge(
        os.getpid(), opener=lambda m, u, **k: {"ok": True, "webview_attached": True})
    assert bridge["url"] == "http://127.0.0.1:45555"
    assert bridge["token"] == "tok-123"


# ===========================================================================
# Layer behavior with a fake bridge
# ===========================================================================

_OBSERVE_VALUE = json.dumps({
    "title": "Probe", "url": "about:blank",
    "elements": [
        {"i": 0, "role": "text_field", "name": "probe field", "value": "",
         "checked": None, "disabled": False, "cx": 10, "cy": 20},
        {"i": 1, "role": "button", "name": "Press", "value": None,
         "checked": None, "disabled": False, "cx": 30, "cy": 40},
    ],
})


@pytest.fixture
def layer(monkeypatch):
    L = DesktopJSBridgeLayer()
    monkeypatch.setattr(dj, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(dj, "find_bridge", lambda pid, **k: (
        {"url": "http://127.0.0.1:45555", "token": "t", "app": "Probe", "pid": pid}, {}))
    L._evaluate = lambda expr: {"ok": True, "value": _OBSERVE_VALUE}
    return L


def test_observe_builds_scoped_refs(layer):
    obs = layer.observe(pid=1234)
    assert obs.context == "desktop"
    assert obs.routing_meta["layer"] == "desktop_jsbridge"
    assert [e.role for e in obs.elements] == ["text_field", "button"]
    assert obs.elements[0].ref.startswith("desktop_jsbridge:")
    assert obs.elements[0].properties["js_ref"] == "qcu_0"


def test_act_click_dispatches_and_reports_sent(layer):
    obs = layer.observe(pid=1234)
    ref = obs.elements[1].ref
    layer._evaluate = lambda expr: {"ok": True, "value": {"status": "ok"}}
    result = layer.act(Action(type="click", params={"ref": ref}))
    assert result.ok and result.dispatch_state == "sent"


def test_act_element_gone_is_not_sent(layer):
    obs = layer.observe(pid=1234)
    ref = obs.elements[1].ref
    layer._evaluate = lambda expr: {"ok": True, "value": {"status": "not_found"}}
    result = layer.act(Action(type="click", params={"ref": ref}))
    assert not result.ok
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "stale_ref"


def test_act_dispatch_error_is_unknown_and_not_retryable(layer):
    obs = layer.observe(pid=1234)
    ref = obs.elements[1].ref
    layer._evaluate = lambda expr: {"ok": True, "value": {"status": "dispatch_error", "message": "boom"}}
    result = layer.act(Action(type="click", params={"ref": ref}))
    assert result.dispatch_state == "unknown"
    assert result.data["retry_safe"] is False


def test_act_transport_failure_is_unknown(layer):
    obs = layer.observe(pid=1234)
    ref = obs.elements[1].ref

    def boom(expr):
        raise OSError("connection reset")

    layer._evaluate = boom
    result = layer.act(Action(type="click", params={"ref": ref}))
    assert result.dispatch_state == "unknown"
    assert result.data["retry_safe"] is False


def test_stale_ref_rejected_after_reobserve(layer):
    obs1 = layer.observe(pid=1234)
    old_ref = obs1.elements[0].ref
    layer.observe(pid=1234)  # new observation retires old refs
    layer._evaluate = lambda expr: {"ok": True, "value": {"status": "ok"}}
    result = layer.act(Action(type="fill", params={"ref": old_ref, "value": "x"}))
    assert not result.ok
    assert result.dispatch_state == "not_sent"
    assert result.data["reason"] == "stale_ref"


def test_act_without_binding_refused():
    result = DesktopJSBridgeLayer().act(Action(type="click", params={"ref": "x"}))
    assert result.data["reason"] == "target_unbound"
    assert result.dispatch_state == "not_sent"


def test_unsupported_action_refused(layer):
    obs = layer.observe(pid=1234)
    result = layer.act(Action(type="drag", params={"ref": obs.elements[0].ref}))
    assert result.data["reason"] == "unsupported"
    assert result.dispatch_state == "not_sent"


def test_fill_with_verify_reaches_verified(layer):
    obs = layer.observe(pid=1234)
    ref = obs.elements[0].ref
    calls = []

    def fake_eval(expr):
        calls.append(expr)
        if "status:'ok'" in expr or "dispatchEvent" in expr:
            return {"ok": True, "value": {"status": "ok"}}
        return {"ok": True, "value": {"value": "hello", "name": "probe field",
                                      "checked": None, "selected": None}}

    layer._evaluate = fake_eval
    result = layer.act(Action(type="fill", params={
        "ref": ref, "value": "hello",
        "verify": {"kind": "value", "ref": ref, "equals": "hello"}}))
    assert result.ok
    assert result.outcome == "verified"
    assert result.data["verification"]["verified"] is True


def test_fill_with_verify_mismatch_reports_unknown(layer):
    obs = layer.observe(pid=1234)
    ref = obs.elements[0].ref

    def fake_eval(expr):
        if "dispatchEvent" in expr:
            return {"ok": True, "value": {"status": "ok"}}
        return {"ok": True, "value": {"value": "different", "name": None,
                                      "checked": None, "selected": None}}

    layer._evaluate = fake_eval
    result = layer.act(Action(type="fill", params={
        "ref": ref, "value": "hello",
        "verify": {"kind": "value", "ref": ref, "equals": "hello"}}))
    assert not result.ok
    assert result.dispatch_state == "sent"
    assert result.outcome == "unknown"


def test_router_keeps_jsbridge_layer():
    from qcu.router.classifier import classify
    decision = classify({"context": "desktop", "observed_layer": "desktop_jsbridge",
                         "platform": "darwin", "desktop_backend": "desktop_ax",
                         "n_elements": 5, "n_interactive": 5}, last_action=None)
    assert decision.layer == "desktop_jsbridge"
