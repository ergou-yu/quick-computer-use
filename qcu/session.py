"""Persistent, process-safe QCU session state."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, TypeVar

SCHEMA_VERSION = 2
_VALID_STATES = {"prepared", "daemon_running", "browser_running", "page_ready"}


def _qcu_home() -> Path:
    base = os.environ.get("QCU_HOME")
    return Path(base) if base else Path.home() / ".qcu"


SESSION_PATH = _qcu_home() / "session.json"
TELEMETRY_PATH = _qcu_home() / "telemetry.jsonl"


@dataclass
class Session:
    session_id: str
    context: str
    layer: str
    pid: int
    started_at: float
    schema_version: int = SCHEMA_VERSION
    status: str = "prepared"
    browser_channel: Optional[str] = None
    browser_debug_port: Optional[int] = None
    browser_pid: Optional[int] = None
    browser_status: str = "stopped"
    daemon_pid: Optional[int] = None
    daemon_port: Optional[int] = None
    daemon_status: str = "stopped"
    headless: bool = True
    user_data_dir: Optional[str] = None
    profile_owned: bool = False
    current_url: Optional[str] = None
    target_app: Optional[str] = None
    observation_generation: int = 0
    document_generation: int = 0
    document_id: Optional[str] = None
    last_observation: Optional[dict[str, Any]] = None
    last_refs: Optional[list[dict[str, Any]]] = None
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Session":
        migrated = _migrate_dict(d)
        known = set(cls.__dataclass_fields__)  # type: ignore[attr-defined]
        return cls(**{k: v for k, v in migrated.items() if k in known})


# ---------------------------------------------------------------------------
# Locking and storage
# ---------------------------------------------------------------------------


def ensure_home() -> Path:
    home = SESSION_PATH.parent
    home.mkdir(parents=True, exist_ok=True)
    return home


@contextlib.contextmanager
def _session_lock(*, exclusive: bool):
    """Hold the cross-process session lock for a complete transaction."""
    home = ensure_home()
    lock_path = home / "session.lock"
    with lock_path.open("a+b") as lock_file:
        if os.name == "nt":  # pragma: no cover - exercised on Windows
            import msvcrt

            lock_file.seek(0)
            if lock_file.read(1) == b"":
                lock_file.write(b"\0")
                lock_file.flush()
            lock_file.seek(0)
            mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
            msvcrt.locking(lock_file.fileno(), mode, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_unlocked() -> Optional[Session]:
    try:
        raw = json.loads(SESSION_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return Session.from_dict(raw)
    except (OSError, ValueError, TypeError):
        return None


def _write_unlocked(session: Session) -> None:
    home = ensure_home()
    session.schema_version = SCHEMA_VERSION
    fd, tmp = tempfile.mkstemp(prefix="session-", suffix=".json", dir=str(home))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, SESSION_PATH)
        # Persist the rename across a crash where the platform supports dir fsync.
        if os.name != "nt":
            dir_fd = os.open(home, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _migrate_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a current-schema copy, conservatively invalidating legacy refs."""
    d = dict(raw)
    version = d.get("schema_version", 0)
    try:
        version = int(version)
    except (TypeError, ValueError):
        version = 0

    d.setdefault("status", "prepared")
    if d["status"] not in _VALID_STATES:
        d["status"] = "prepared"
    d.setdefault("browser_status", "running" if d.get("browser_debug_port") else "stopped")
    d.setdefault("daemon_status", "running" if d.get("daemon_port") else "stopped")
    d.setdefault("profile_owned", False)
    d.setdefault("observation_generation", 0)
    d.setdefault("document_generation", 0)
    d.setdefault("document_id", None)
    d.setdefault("last_observation", None)
    d.setdefault("extras", {})

    # Old refs were not tied to an observation/document generation. They cannot
    # safely identify a node after migration, so never silently reuse them.
    if version < SCHEMA_VERSION:
        d["last_refs"] = None
        d["observation_generation"] = max(0, int(d.get("observation_generation") or 0))
        d["document_generation"] = max(0, int(d.get("document_generation") or 0))
    d["schema_version"] = SCHEMA_VERSION
    return d


def load() -> Optional[Session]:
    """Load and migrate the logical session without process-liveness filtering."""
    with _session_lock(exclusive=False):
        return _read_unlocked()


def save(session: Session) -> None:
    with _session_lock(exclusive=True):
        _write_unlocked(session)


T = TypeVar("T")


def update(mutator: Callable[[Session], T]) -> tuple[Optional[Session], Optional[T]]:
    """Atomically load, mutate, and save a session under one exclusive lock."""
    with _session_lock(exclusive=True):
        session = _read_unlocked()
        if session is None:
            return None, None
        result = mutator(session)
        _write_unlocked(session)
        return session, result


def clear() -> None:
    with _session_lock(exclusive=True):
        with contextlib.suppress(FileNotFoundError, OSError):
            SESSION_PATH.unlink()


def new(context: str, layer: str, **fields: Any) -> Session:
    s = Session(
        session_id=str(uuid.uuid4()), context=context, layer=layer,
        pid=os.getpid(), started_at=time.time(), **fields,
    )
    save(s)
    return s


def patch(**fields: Any) -> Optional[Session]:
    known = set(Session.__dataclass_fields__)  # type: ignore[attr-defined]

    def apply(s: Session) -> None:
        for key, value in fields.items():
            if key in known:
                setattr(s, key, value)

    session, _ = update(apply)
    return session


def record_observation(
    observation: dict[str, Any], *, current_url: Optional[str] = None,
    document_id: Optional[str] = None, document_changed: bool = False,
    refs: Optional[list[dict[str, Any]]] = None,
) -> Optional[Session]:
    """Persist an observation and advance its generation in one transaction."""
    def apply(s: Session) -> None:
        s.observation_generation += 1
        if document_changed or (document_id is not None and document_id != s.document_id):
            s.document_generation += 1
            s.last_refs = None
        if document_id is not None:
            s.document_id = document_id
        if current_url is not None:
            s.current_url = current_url
        s.last_observation = observation
        s.last_refs = refs
        s.status = "page_ready"
        if s.context == "web":
            s.browser_status = "running"

    session, _ = update(apply)
    return session


def invalidate_observation(*, current_url: Optional[str] = None, document_changed: bool = True) -> Optional[Session]:
    """Invalidate refs immediately when navigation or document replacement occurs."""
    def apply(s: Session) -> None:
        s.last_refs = None
        s.last_observation = None
        if current_url is not None:
            s.current_url = current_url
        if document_changed:
            s.document_generation += 1
            s.document_id = None
        s.status = "browser_running" if s.browser_debug_port else "prepared"

    session, _ = update(apply)
    return session


# ---------------------------------------------------------------------------
# Status/liveness
# ---------------------------------------------------------------------------


def is_alive(session: Session) -> bool:
    if sys.platform == "win32":
        # Windows os.kill(pid, 0) uses TerminateProcess; it is not a probe.
        from qcu.layers.browser_daemon import is_pid_alive
        return is_pid_alive(session.pid)
    try:
        os.kill(session.pid, 0)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def is_browser_alive(session: Session) -> bool:
    if not session.browser_debug_port:
        return False
    try:
        from qcu.layers.browser_daemon import is_cdp_alive
        return is_cdp_alive(session.browser_debug_port)
    except Exception:
        return False


def is_daemon_alive(session: Session) -> bool:
    if not session.daemon_port:
        return False
    try:
        from qcu.daemon.server import health_check
        return health_check(session.daemon_port, timeout=1.5)
    except Exception:
        return False


def status_dict() -> dict[str, Any]:
    s = load()
    if s is None:
        return {"active": False}
    browser_alive = is_browser_alive(s)
    daemon_alive = is_daemon_alive(s)
    return {
        "active": True, "schema_version": s.schema_version, "status": s.status,
        "session_id": s.session_id, "context": s.context, "layer": s.layer,
        "pid": s.pid, "started_at": s.started_at, "age_seconds": time.time() - s.started_at,
        "browser_channel": s.browser_channel, "browser_debug_port": s.browser_debug_port,
        "browser_pid": s.browser_pid, "browser_alive": browser_alive,
        "browser_status": "running" if browser_alive else s.browser_status,
        "daemon_pid": s.daemon_pid, "daemon_port": s.daemon_port,
        "daemon_alive": daemon_alive, "daemon_status": "running" if daemon_alive else s.daemon_status,
        "headless": s.headless, "user_data_dir": s.user_data_dir,
        "profile_owned": s.profile_owned, "current_url": s.current_url,
        "observation_generation": s.observation_generation,
        "document_generation": s.document_generation, "document_id": s.document_id,
        "has_last_observation": s.last_observation is not None,
        "n_refs_cached": len(s.last_refs) if s.last_refs else 0, "extras": s.extras,
    }
