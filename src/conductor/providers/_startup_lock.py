"""Per-provider filesystem locks for CLI startup contention windows."""

from __future__ import annotations

import contextlib
import errno
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if os.name == "posix":
    import fcntl
else:  # pragma: no cover - exercised only on non-POSIX platforms.
    fcntl = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

StartupProcessSnapshot = tuple[int, str]
SnapshotProvider = Callable[[], StartupProcessSnapshot]

STARTUP_LOCK_TIMEOUT_SEC = 5.0


@dataclass
class StartupLockHandle:
    provider: str
    path: Path | None
    acquired: bool
    waited_sec: float
    _fh: IO[Any] | None = None
    _session_log: SessionLog | None = None
    _released: bool = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        if not self.acquired or self._fh is None or fcntl is None:
            return
        try:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            sys.stderr.write(
                f"[conductor] {self.provider} startup lock release failed: {exc}\n"
            )
            sys.stderr.flush()
            if self._session_log is not None:
                self._session_log.emit(
                    "provider_startup_lock",
                    {
                        "provider": self.provider,
                        "action": "release_failed",
                        "path": str(self.path),
                        "error": str(exc),
                    },
                )
            return
        if self._session_log is not None:
            self._session_log.emit(
                "provider_startup_lock",
                {
                    "provider": self.provider,
                    "action": "released",
                    "path": str(self.path),
                },
            )


def _lock_dir() -> Path:
    cache_home = os.environ.get("XDG_CACHE_HOME")
    root = Path(cache_home) if cache_home else Path.home() / ".cache"
    return root / "conductor" / "locks"


def _format_seconds(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"


def _default_process_snapshot(provider: str) -> StartupProcessSnapshot:
    if os.name != "posix":
        return 0, "process snapshot unavailable on this platform"
    user = os.environ.get("USER")
    if not user:
        return 0, "USER is not set; process snapshot skipped"
    try:
        result = subprocess.run(
            ["ps", "-u", user, "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 0, f"process snapshot failed: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return 0, f"process snapshot exited {result.returncode}: {detail[:160]}"

    current_pid = os.getpid()
    matches: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(maxsplit=2)
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == current_pid:
            continue
        command = parts[2]
        executable = command.split(maxsplit=1)[0]
        if Path(executable).name != provider:
            continue
        matches.append(f"pid={pid} {command[:120]}")
    return len(matches), "; ".join(matches) if matches else "none"


def _emit_timeout_diagnostic(
    *,
    provider: str,
    waited_sec: float,
    snapshot_provider: SnapshotProvider | None,
    session_log: SessionLog | None,
) -> None:
    try:
        count, detail = (
            snapshot_provider()
            if snapshot_provider is not None
            else _default_process_snapshot(provider)
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics must not block startup.
        count, detail = 0, f"process snapshot failed: {exc!r}"

    seconds = _format_seconds(waited_sec)
    sys.stderr.write(
        f"[conductor] {provider} startup lock contended; waited {seconds}s. "
        f"{count} other {provider} processes alive ({detail}). "
        "Proceeding without startup lock.\n"
    )
    sys.stderr.flush()
    if session_log is not None:
        session_log.emit(
            "provider_startup_lock",
            {
                "provider": provider,
                "action": "timeout",
                "waited_sec": waited_sec,
                "live_process_count": count,
                "live_processes": detail,
            },
        )


@contextlib.contextmanager
def provider_startup_lock(
    provider: str,
    *,
    timeout_sec: float = STARTUP_LOCK_TIMEOUT_SEC,
    session_log: SessionLog | None = None,
    snapshot_provider: SnapshotProvider | None = None,
) -> Iterator[StartupLockHandle]:
    """Serialize one provider's CLI startup across conductor processes."""

    if os.name != "posix" or fcntl is None:
        yield StartupLockHandle(
            provider=provider,
            path=None,
            acquired=False,
            waited_sec=0.0,
            _session_log=session_log,
        )
        return

    lock_path = _lock_dir() / f"{provider}-startup.lock"
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = lock_path.open("a+", encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(
            f"[conductor] {provider} startup lock unavailable at {lock_path}: "
            f"{exc}. Proceeding without startup lock.\n"
        )
        sys.stderr.flush()
        if session_log is not None:
            session_log.emit(
                "provider_startup_lock",
                {
                    "provider": provider,
                    "action": "unavailable",
                    "path": str(lock_path),
                    "error": str(exc),
                },
            )
        yield StartupLockHandle(
            provider=provider,
            path=lock_path,
            acquired=False,
            waited_sec=0.0,
            _session_log=session_log,
        )
        return
    start = time.monotonic()
    handle = StartupLockHandle(
        provider=provider,
        path=lock_path,
        acquired=False,
        waited_sec=0.0,
        _fh=fh,
        _session_log=session_log,
    )
    try:
        while True:
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                waited = time.monotonic() - start
                if waited >= timeout_sec:
                    handle.waited_sec = waited
                    _emit_timeout_diagnostic(
                        provider=provider,
                        waited_sec=timeout_sec,
                        snapshot_provider=snapshot_provider,
                        session_log=session_log,
                    )
                    yield handle
                    return
                time.sleep(min(0.05, max(0.0, timeout_sec - waited)))
                continue
            else:
                handle.acquired = True
                handle.waited_sec = time.monotonic() - start
                if session_log is not None:
                    session_log.emit(
                        "provider_startup_lock",
                        {
                            "provider": provider,
                            "action": "acquired",
                            "path": str(lock_path),
                            "waited_sec": round(handle.waited_sec, 3),
                        },
                    )
                yield handle
                return
    finally:
        handle.release()
        fh.close()


def release_startup_lock(handle: object) -> None:
    release = getattr(handle, "release", None)
    if callable(release):
        release()


def claude_startup_lock(
    timeout_sec: float = STARTUP_LOCK_TIMEOUT_SEC,
    *,
    session_log: SessionLog | None = None,
    snapshot_provider: SnapshotProvider | None = None,
) -> contextlib.AbstractContextManager[StartupLockHandle]:
    """Serialize Claude CLI startups across all conductor processes for this user.

    File: $XDG_CACHE_HOME/conductor/locks/claude-startup.lock, or
    ~/.cache/conductor/locks/claude-startup.lock when XDG_CACHE_HOME is unset.
    Held only during the startup window (release as soon as first output arrives
    or initial probe completes). Mid-task / long-running phases are NOT held.
    """

    return provider_startup_lock(
        "claude",
        timeout_sec=timeout_sec,
        session_log=session_log,
        snapshot_provider=snapshot_provider,
    )


def codex_startup_lock(
    timeout_sec: float = STARTUP_LOCK_TIMEOUT_SEC,
    *,
    session_log: SessionLog | None = None,
    snapshot_provider: SnapshotProvider | None = None,
) -> contextlib.AbstractContextManager[StartupLockHandle]:
    return provider_startup_lock(
        "codex",
        timeout_sec=timeout_sec,
        session_log=session_log,
        snapshot_provider=snapshot_provider,
    )


def gemini_startup_lock(
    timeout_sec: float = STARTUP_LOCK_TIMEOUT_SEC,
    *,
    session_log: SessionLog | None = None,
    snapshot_provider: SnapshotProvider | None = None,
) -> contextlib.AbstractContextManager[StartupLockHandle]:
    return provider_startup_lock(
        "gemini",
        timeout_sec=timeout_sec,
        session_log=session_log,
        snapshot_provider=snapshot_provider,
    )
