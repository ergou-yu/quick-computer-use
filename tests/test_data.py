"""Tests for ``qcu.data.collector`` — JSONL write + summary."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from qcu.common.types import Action, Observation, Element
from qcu.data.collector import Telemetry, summary as telemetry_summary
from qcu.router.classifier import RoutingDecision


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    from qcu import session as sess
    from qcu import data as data_mod
    from qcu.data import collector

    monkeypatch.setenv("QCU_HOME", str(tmp_path))
    monkeypatch.setattr(sess, "_qcu_home", lambda: tmp_path)
    monkeypatch.setattr(sess, "SESSION_PATH", tmp_path / "session.json")
    monkeypatch.setattr(sess, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    monkeypatch.setattr(collector, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    yield


def _record(layer: str, outcome: str, lat: float):
    feats = {"context": "web", "n_elements": 1}
    decision = RoutingDecision(layer=layer, reason="test", priority=10, rule_name="t", features=feats)
    Telemetry.record(
        context="web",
        features=feats,
        decision=decision,
        action={"type": "click", "params": {}},
        outcome=outcome,
        latency_ms=lat,
    )


def test_record_creates_file_and_appends():
    _record("web_a11y", "ok", 12.0)
    _record("web_a11y", "ok", 18.0)
    lines = Path(Telemetry.__module__ and __import__("qcu.session", fromlist=["TELEMETRY_PATH"]).TELEMETRY_PATH).read_text().splitlines()
    # Re-resolve path robustly
    from qcu.session import TELEMETRY_PATH as T
    lines = Path(T).read_text().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["decision"]["layer"] == "web_a11y"
    assert rec["outcome"] == "ok"
    assert rec["latency_ms"] == 12.0


def test_summary_counts_and_latency_percentiles():
    _record("web_a11y", "ok", 10.0)
    _record("web_a11y", "ok", 20.0)
    _record("web_a11y", "fail", 30.0)
    _record("screenshot_fallback", "ok", 200.0)
    s = telemetry_summary(since="7d")
    assert s["total_records"] == 4
    assert s["in_window"] == 4
    assert "web_a11y" in s["by_layer"]
    assert "screenshot_fallback" in s["by_layer"]
    w = s["by_layer"]["web_a11y"]
    assert w["hits"] == 3
    assert w["ok"] == 2
    assert w["fail"] == 1
    assert w["avg_ms"] == 20.0  # (10+20+30)/3
    assert w["p50_ms"] == 20.0


def test_summary_empty_file():
    s = telemetry_summary(since="7d")
    assert s["records"] == 0 if "records" in s else s["total_records"] == 0


def test_summary_respects_since_window():
    # Insert a record with an old timestamp.
    feats = {"context": "web"}
    decision = RoutingDecision(layer="web_a11y", reason="t", priority=10, rule_name="t", features=feats)
    Telemetry.record(
        context="web",
        features=feats,
        decision=decision,
        action=None,
        outcome="ok",
        latency_ms=5.0,
    )
    # Manually rewrite the file with an old timestamp.
    from qcu.session import TELEMETRY_PATH as T
    lines = Path(T).read_text().splitlines()
    rec = json.loads(lines[0])
    rec["ts"] = time.time() - 30 * 86400  # 30 days ago
    Path(T).write_text(json.dumps(rec) + "\n")
    s = telemetry_summary(since="7d")
    assert s["in_window"] == 0
    assert s["total_records"] == 1


def test_summary_by_outcome():
    _record("web_a11y", "ok", 10.0)
    _record("web_a11y", "ok", 12.0)
    _record("web_a11y", "error", 8.0)
    s = telemetry_summary(since="7d")
    assert s["by_outcome"]["ok"] == 2
    assert s["by_outcome"]["error"] == 1


def test_summary_groups_by_actual_layer_not_decision_layer():
    """A `--layer` override used to be misattributed to the router's decision
    layer in stats. The group key is now the layer that actually ran."""
    feats = {"context": "web"}
    decision = RoutingDecision(layer="web_a11y", reason="t", priority=10, rule_name="t", features=feats)
    Telemetry.record(
        context="web",
        features=feats,
        decision=decision,
        action={"type": "click", "params": {}},
        outcome="ok",
        latency_ms=5.0,
        requested_layer=None,
        actual_layer="screenshot_fallback",  # override ran a different layer
    )
    s = telemetry_summary(since="7d")
    assert "screenshot_fallback" in s["by_layer"]
    assert "web_a11y" not in s["by_layer"]
