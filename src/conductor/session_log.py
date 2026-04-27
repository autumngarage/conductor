"""Structured session logs for `conductor exec`.

Each session log is NDJSON: one JSON object per line, append-only.
Metadata lives alongside the logs in the conductor cache so `sessions list`
and `sessions tail` can discover active and completed runs without parsing
the full event stream every time.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from conductor.offline_mode import _cache_dir


class SessionLogError(RuntimeError):
    """Raised when conductor cannot create or update a session log."""


def _iso8601_now() -> str:
    return datetime.now(UTC).isoformat()


def sessions_dir() -> Path:
    primary = _cache_dir() / "sessions"
    try:
        primary.mkdir(parents=True, exist_ok=True)
        return primary
    except OSError:
        fallback = Path(tempfile.gettempdir()) / "conductor" / "sessions"
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _meta_path(run_id: str) -> Path:
    return sessions_dir() / f"{run_id}.meta.json"


@dataclass(frozen=True)
class SessionRecord:
    run_id: str
    session_id: str
    log_path: Path
    status: str
    started_at: str
    updated_at: str
    finished_at: str | None
    provider: str | None
    explicit_log_path: bool


class SessionLog:
    """Append-only NDJSON event writer plus lightweight session metadata."""

    def __init__(
        self,
        *,
        path: Path | None = None,
        session_id: str | None = None,
    ) -> None:
        self.run_id = str(uuid.uuid4())
        self.session_id = session_id or self.run_id
        self.provider: str | None = None
        self.explicit_log_path = path is not None
        self.started_at = _iso8601_now()
        self.updated_at = self.started_at
        self.finished_at: str | None = None
        self.status = "running"

        if path is None:
            self.log_path = sessions_dir() / f"{self.session_id}.ndjson"
        else:
            self.log_path = path.expanduser()

        self._ensure_parent_dirs()
        self._write_meta()

    def _ensure_parent_dirs(self) -> None:
        try:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise SessionLogError(
                f"could not create session log directories: {e.strerror or e}"
            ) from e

    def bind_provider(self, provider: str) -> None:
        self.provider = provider
        self._touch_meta()

    def emit(self, event: str, data: dict | None = None) -> None:
        payload = {
            "ts": _iso8601_now(),
            "event": event,
            "data": data or {},
        }
        try:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, default=str))
                fh.write("\n")
        except OSError as e:
            raise SessionLogError(
                f"could not append session log {self.log_path}: {e.strerror or e}"
            ) from e
        self._touch_meta()

    def set_session_id(self, session_id: str | None) -> None:
        if not session_id or session_id == self.session_id:
            return

        old_path = self.log_path
        self.session_id = session_id
        if not self.explicit_log_path:
            target = sessions_dir() / f"{session_id}.ndjson"
            self._move_or_merge_log(old_path, target)
            self.log_path = target
        self._touch_meta()

    def mark_finished(self) -> None:
        self.status = "finished"
        self.finished_at = _iso8601_now()
        self._touch_meta()

    def _move_or_merge_log(self, source: Path, target: Path) -> None:
        if source == target or not source.exists():
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                with source.open("r", encoding="utf-8") as src, target.open(
                    "a", encoding="utf-8"
                ) as dst:
                    shutil.copyfileobj(src, dst)
                source.unlink()
            else:
                os.replace(source, target)
        except OSError as e:
            raise SessionLogError(
                f"could not move session log into canonical path {target}: "
                f"{e.strerror or e}"
            ) from e

    def _touch_meta(self) -> None:
        self.updated_at = _iso8601_now()
        self._write_meta()

    def _write_meta(self) -> None:
        payload = {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "log_path": str(self.log_path),
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "provider": self.provider,
            "explicit_log_path": self.explicit_log_path,
        }
        path = _meta_path(self.run_id)
        tmp = path.with_suffix(".tmp")
        try:
            sessions_dir().mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as e:
            raise SessionLogError(
                f"could not update session metadata {path}: {e.strerror or e}"
            ) from e


def list_session_records() -> list[SessionRecord]:
    records: list[SessionRecord] = []
    root = sessions_dir()
    if not root.exists():
        return []
    for meta in sorted(root.glob("*.meta.json")):
        try:
            data = json.loads(meta.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session_id = str(data.get("session_id") or "")
        log_path_raw = data.get("log_path")
        if not session_id or not log_path_raw:
            continue
        records.append(
            SessionRecord(
                run_id=str(data.get("run_id") or meta.stem.removesuffix(".meta")),
                session_id=session_id,
                log_path=Path(str(log_path_raw)).expanduser(),
                status=str(data.get("status") or "running"),
                started_at=str(data.get("started_at") or ""),
                updated_at=str(data.get("updated_at") or ""),
                finished_at=data.get("finished_at"),
                provider=data.get("provider"),
                explicit_log_path=bool(data.get("explicit_log_path")),
            )
        )
    return sorted(records, key=lambda record: (record.updated_at, record.started_at))


def find_session_record(session_id: str) -> SessionRecord | None:
    for record in reversed(list_session_records()):
        if record.session_id == session_id or record.run_id == session_id:
            return record
    return None


def latest_active_session() -> SessionRecord | None:
    for record in reversed(list_session_records()):
        if record.status == "running":
            return record
    return None
