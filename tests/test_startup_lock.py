from __future__ import annotations

import errno
import os
import threading
import time

import pytest

from conductor.providers import _startup_lock
from conductor.providers._startup_lock import claude_startup_lock
from conductor.providers.cli_auth import AuthPromptTracker, run_subprocess_with_live_stderr

pytestmark = pytest.mark.skipif(os.name != "posix", reason="flock is POSIX-only")


class _DelayedPipe:
    def __init__(
        self,
        text: str,
        *,
        first_delay: float = 0.0,
        on_eof=None,
    ) -> None:
        self._text = text
        self._first_delay = first_delay
        self._on_eof = on_eof
        self._sent = False
        self._closed = False

    def read(self, size: int = -1) -> str:
        if self._closed:
            return ""
        if not self._sent:
            self._sent = True
            if self._first_delay:
                time.sleep(self._first_delay)
            return self._text
        self._closed = True
        if self._on_eof is not None:
            self._on_eof()
        return ""

    def readline(self) -> str:
        return self.read()


class _StartupFakePopen:
    def __init__(self, stdout_delay: float) -> None:
        self.returncode: int | None = None
        self._finished = threading.Event()
        self.stdout = _DelayedPipe(
            "ok\n",
            first_delay=stdout_delay,
            on_eof=self._finish,
        )
        self.stderr = _DelayedPipe("", on_eof=self._finish)

    def _finish(self) -> None:
        if self.returncode is None:
            self.returncode = 0
            self._finished.set()

    def poll(self) -> int | None:
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        self.returncode = 0
        self._finished.set()
        return 0

    def terminate(self) -> None:
        self.returncode = -15
        self._finished.set()

    def kill(self) -> None:
        self.returncode = -9
        self._finished.set()


def test_live_subprocess_startup_lock_serializes_popen_until_first_output(
    monkeypatch, tmp_path
) -> None:
    lock_state = {"locked": False}
    lock_guard = threading.Lock()

    def fake_flock(_fd: int, op: int) -> None:
        with lock_guard:
            if op & _startup_lock.fcntl.LOCK_UN:
                lock_state["locked"] = False
                return
            if not (op & _startup_lock.fcntl.LOCK_EX):
                return
            if lock_state["locked"]:
                raise OSError(errno.EAGAIN, "locked")
            lock_state["locked"] = True

    monkeypatch.setattr(_startup_lock, "_lock_dir", lambda: tmp_path)
    monkeypatch.setattr(_startup_lock.fcntl, "flock", fake_flock)

    popen_times: list[float] = []
    popen_guard = threading.Lock()

    def run_one(stdout_delay: float) -> None:
        def popen_factory(*_args, **_kwargs):
            with popen_guard:
                popen_times.append(time.monotonic())
            return _StartupFakePopen(stdout_delay)

        run_subprocess_with_live_stderr(
            args=["claude"],
            cwd=None,
            env=None,
            timeout=3,
            provider_name="claude",
            tracker=AuthPromptTracker("claude"),
            popen_factory=popen_factory,
            startup_lock=claude_startup_lock(timeout_sec=1),
        )

    first = threading.Thread(target=run_one, args=(0.12,))
    second = threading.Thread(target=run_one, args=(0.0,))

    first.start()
    time.sleep(0.02)
    second.start()
    first.join(timeout=2)
    second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert len(popen_times) == 2
    assert popen_times[1] - popen_times[0] >= 0.08


def test_startup_lock_timeout_emits_diagnostic_and_proceeds(
    monkeypatch, tmp_path, capsys
) -> None:
    def always_locked(_fd: int, op: int) -> None:
        if op & _startup_lock.fcntl.LOCK_UN:
            return
        raise OSError(errno.EAGAIN, "locked")

    monkeypatch.setattr(_startup_lock, "_lock_dir", lambda: tmp_path)
    monkeypatch.setattr(_startup_lock.fcntl, "flock", always_locked)

    with claude_startup_lock(
        timeout_sec=0.01,
        snapshot_provider=lambda: (2, "pid=111 claude; pid=222 claude"),
    ) as handle:
        assert handle.acquired is False

    captured = capsys.readouterr()
    assert (
        "[conductor] claude startup lock contended; waited 0.01s. "
        "2 other claude processes alive (pid=111 claude; pid=222 claude)."
    ) in captured.err
    assert "Proceeding without startup lock." in captured.err
