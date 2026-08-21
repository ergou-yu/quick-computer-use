"""Tests for ``browser_daemon.purge_profile`` — QCU-managed profile safety."""

from __future__ import annotations

from pathlib import Path

import pytest

from qcu.layers import browser_daemon as bd
from qcu import session as sess


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("QCU_HOME", str(tmp_path))
    monkeypatch.setattr(sess, "_qcu_home", lambda: tmp_path)
    yield


def test_purge_owned_profile_deletes_it(tmp_path: Path):
    profile = tmp_path / "browser-data"
    profile.mkdir()
    (profile / "Default").mkdir()
    (profile / "Default" / "Cookies").write_text("x")
    ok, why = bd.purge_profile(str(profile), owned=True)
    assert ok is True
    assert why is None
    assert not profile.exists()


def test_purge_refuses_user_supplied_profile(tmp_path: Path):
    # A profile under QCU_HOME but marked not-owned (QCU_USER_DATA_DIR case)
    # must not be deleted automatically.
    profile = tmp_path / "browser-data"
    profile.mkdir()
    ok, why = bd.purge_profile(str(profile), owned=False)
    assert ok is False
    assert "user-supplied" in why
    assert profile.exists()


def test_purge_refuses_path_outside_home(tmp_path: Path):
    ok, why = bd.purge_profile("/tmp/definitely-not-qcu", owned=True)
    assert ok is False
    assert "outside QCU home" in why


def test_purge_refuses_unexpected_managed_path(tmp_path: Path):
    # Inside home but not 'browser-data' — refuse even if owned=True.
    other = tmp_path / "something-else"
    other.mkdir()
    ok, why = bd.purge_profile(str(other), owned=True)
    assert ok is False
