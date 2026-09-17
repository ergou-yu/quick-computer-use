"""Every test gets disposable QCU state, including pre-imported path constants."""
import pytest


@pytest.fixture(autouse=True)
def isolated_qcu_home(monkeypatch, tmp_path):
    from qcu import session

    home = tmp_path / 'qcu-home'
    monkeypatch.setenv('QCU_HOME', str(home))
    monkeypatch.setenv('QCU_ARTIFACTS_DIR', str(home / 'artifacts'))
    monkeypatch.delenv('QCU_USER_DATA_DIR', raising=False)
    monkeypatch.delenv('QCU_DAEMON_PROCESS', raising=False)
    # QCU_HOME alone is insufficient if session was imported earlier during
    # collection. Patch the exported paths too; task-specific fixtures may
    # still replace them after this safety fixture runs.
    monkeypatch.setattr(session, 'SESSION_PATH', home / 'session.json')
    monkeypatch.setattr(session, 'TELEMETRY_PATH', home / 'telemetry.jsonl')
