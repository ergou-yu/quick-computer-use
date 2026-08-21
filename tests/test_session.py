"""Tests for ``qcu.session`` — session.json lifecycle in a temp dir."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from qcu import session as sess


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    """Point QCU_HOME at a temp dir so we don't touch the real ~/.qcu."""
    monkeypatch.setenv("QCU_HOME", str(tmp_path))
    # Also reset the cached module-level paths so they pick up the env var.
    monkeypatch.setattr(sess, "_qcu_home", lambda: tmp_path)
    monkeypatch.setattr(sess, "SESSION_PATH", tmp_path / "session.json")
    monkeypatch.setattr(sess, "TELEMETRY_PATH", tmp_path / "telemetry.jsonl")
    yield


def test_load_returns_none_when_no_file():
    assert sess.load() is None


def test_new_creates_session_file():
    s = sess.new(context="web", layer="web_a11y")
    assert sess.SESSION_PATH.exists()
    raw = json.loads(sess.SESSION_PATH.read_text())
    assert raw["context"] == "web"
    assert raw["layer"] == "web_a11y"
    assert raw["session_id"] == s.session_id
    assert raw["pid"] == s.pid


def test_load_returns_existing_session():
    s = sess.new(context="desktop", layer="desktop_ax")
    loaded = sess.load()
    assert loaded is not None
    assert loaded.session_id == s.session_id


def test_clear_removes_session():
    sess.new(context="web", layer="web_a11y")
    assert sess.SESSION_PATH.exists()
    sess.clear()
    assert not sess.SESSION_PATH.exists()
    assert sess.load() is None


def test_session_is_not_wiped_just_because_pid_is_dead():
    """Regression: we used to auto-wipe stale sessions on load(), which
    made it impossible to share state across the many short-lived CLI
    processes the agent spawns. The session pid is now informational."""
    sess.new(context="web", layer="web_a11y")
    # Write a session whose pid is guaranteed not to exist; load() should
    # still return it.
    raw = json.loads(sess.SESSION_PATH.read_text())
    raw["pid"] = 2**31 - 1  # almost certainly not a live pid
    sess.SESSION_PATH.write_text(json.dumps(raw))
    assert sess.load() is not None


def test_status_dict_inactive():
    assert sess.status_dict() == {"active": False}


def test_status_dict_active():
    sess.new(context="web", layer="web_a11y", headless=True)
    st = sess.status_dict()
    assert st["active"] is True
    assert st["context"] == "web"
    assert st["layer"] == "web_a11y"
    assert st["headless"] is True


def test_session_to_dict_roundtrip():
    s = sess.Session(
        session_id="abc",
        context="web",
        layer="web_a11y",
        pid=12345,
        started_at=0.0,
        headless=False,
        browser_channel="chrome",
        browser_debug_port=9222,
        extras={"foo": 1},
    )
    d = s.to_dict()
    s2 = sess.Session.from_dict(d)
    assert s2.session_id == "abc"
    assert s2.browser_channel == "chrome"
    assert s2.browser_debug_port == 9222
    assert s2.extras == {"foo": 1}


def test_session_from_dict_ignores_unknown_keys():
    s = sess.Session.from_dict(
        {
            "session_id": "x",
            "context": "web",
            "layer": "web_a11y",
            "pid": 1,
            "started_at": 0.0,
            "headless": True,
            "made_up_field": "ignored",  # should be dropped
        }
    )
    assert s.session_id == "x"


# ---------------------------------------------------------------------------
# Generation / invalidation (issues 9, 10, ref lifecycle)
# ---------------------------------------------------------------------------


def test_record_observation_advances_generation_and_persists_refs():
    sess.new(context="web", layer="web_a11y")
    before = sess.load()
    assert before is not None
    gen0 = before.observation_generation

    sess.record_observation(
        {"context": "web", "elements": ["ref_0"]},
        current_url="https://example.com/",
        refs=[{"ref": "ref_0", "role": "link"}],
    )
    after = sess.load()
    assert after is not None
    assert after.observation_generation == gen0 + 1
    assert after.last_refs == [{"ref": "ref_0", "role": "link"}]
    assert after.current_url == "https://example.com/"


def test_invalidate_observation_clears_refs_and_advances_document_generation():
    sess.new(context="web", layer="web_a11y")
    sess.record_observation({"x": 1}, refs=[{"ref": "obs_1:ref_0"}])
    before = sess.load()
    assert before is not None
    doc0 = before.document_generation
    sess.invalidate_observation(current_url="https://example.com/next", document_changed=True)
    after = sess.load()
    assert after is not None
    assert after.last_refs is None
    assert after.document_generation == doc0 + 1
    assert after.current_url == "https://example.com/next"


def test_migration_from_legacy_schema_drops_unsafe_refs():
    """Legacy refs were not generation-bound; migration must not silently
    reuse them against the current page (would click the wrong element)."""
    raw = {
        "session_id": "old",
        "context": "web",
        "layer": "web_a11y",
        "pid": 1,
        "started_at": 0.0,
        "schema_version": 1,
        "last_refs": [{"ref": "ref_0", "role": "link"}],  # legacy, no generation
    }
    sess.SESSION_PATH.write_text(json.dumps(raw))
    s = sess.load()
    assert s is not None
    assert s.last_refs is None  # unsafe refs dropped on migration
    assert s.schema_version == sess.SCHEMA_VERSION
