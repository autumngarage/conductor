"""Regression tests for orphan codex process detection (#143 bullet 1).

A codex process from a prior session with a dead parent may be holding the
--ephemeral ChatGPT auth lock, causing new conductor exec --with codex
dispatches to wedge silently.

Tests verify:
- codex with a dead parent is flagged with PID and a copy-pasteable kill hint
- codex with a live parent is not flagged
- ps non-zero exit logs a note and returns empty without raising
- the stall error from CodexProvider includes orphan hints end-to-end
"""

from __future__ import annotations

import subprocess

import pytest

from conductor.orphan_detect import (
    OrphanProcess,
    find_orphan_codex_processes,
    format_orphan_hints,
)
from conductor.providers.codex import CodexProvider
from conductor.providers.interface import ProviderStalledError

# ---------------------------------------------------------------------------
# Unit tests for orphan_detect module
# ---------------------------------------------------------------------------

_PS_HEADER = "  PID  PPID     ELAPSED COMMAND\n"


def _ps_line(pid: int, ppid: int, etime: str, command: str) -> str:
    return f"{pid:5}  {ppid:4}  {etime:>10} {command}\n"


def _fake_ps(stdout: str, returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["ps"], returncode=returncode, stdout=stdout, stderr=""
    )


def test_find_orphan_codex_dead_parent_is_flagged(mocker):
    """Codex process whose parent no longer exists is returned as orphan."""
    ps_out = _PS_HEADER + _ps_line(
        10182, 9999, "06:32:00",
        "/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox",
    )
    mocker.patch("conductor.orphan_detect.subprocess.run", return_value=_fake_ps(ps_out))
    # Parent PID 9999 is dead
    mocker.patch("conductor.orphan_detect.os.kill", side_effect=ProcessLookupError)

    orphans = find_orphan_codex_processes("codex")

    assert len(orphans) == 1
    assert orphans[0].pid == 10182
    assert orphans[0].etime == "06:32:00"


def test_find_orphan_codex_live_parent_is_not_flagged(mocker):
    """Codex process with a live parent is not reported as an orphan."""
    ps_out = _PS_HEADER + _ps_line(
        2001, 500, "00:01:00",
        "/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox",
    )
    mocker.patch("conductor.orphan_detect.subprocess.run", return_value=_fake_ps(ps_out))
    # Parent PID 500 is alive
    mocker.patch("conductor.orphan_detect.os.kill", return_value=None)

    orphans = find_orphan_codex_processes("codex")

    assert orphans == []


def test_find_orphan_mixed_dead_and_live_parents(mocker):
    """Only processes with dead parents are included in the returned list."""
    ps_out = (
        _PS_HEADER
        + _ps_line(1001, 9999, "01:00:00", "/usr/bin/codex --ephemeral")  # dead parent
        + _ps_line(1002, 1, "00:05:00", "/usr/bin/codex --ephemeral")     # live parent
    )
    mocker.patch("conductor.orphan_detect.subprocess.run", return_value=_fake_ps(ps_out))

    def fake_kill(pid: int, sig: int) -> None:
        if pid == 9999:
            raise ProcessLookupError
        # PID 1 is alive — do nothing

    mocker.patch("conductor.orphan_detect.os.kill", side_effect=fake_kill)

    orphans = find_orphan_codex_processes("codex")

    assert len(orphans) == 1
    assert orphans[0].pid == 1001


def test_find_orphan_non_codex_processes_ignored(mocker):
    """Processes that don't contain the CLI name in their command are skipped."""
    ps_out = (
        _PS_HEADER
        + _ps_line(3001, 9999, "00:10:00", "/usr/bin/python some_script.py")
    )
    mocker.patch("conductor.orphan_detect.subprocess.run", return_value=_fake_ps(ps_out))
    mocker.patch("conductor.orphan_detect.os.kill", side_effect=ProcessLookupError)

    orphans = find_orphan_codex_processes("codex")

    assert orphans == []


def test_find_orphan_ps_nonzero_exit_returns_empty_and_logs(mocker, capsys):
    """ps non-zero exit returns empty list and logs a one-line note to stderr."""
    mocker.patch(
        "conductor.orphan_detect.subprocess.run",
        return_value=_fake_ps("", returncode=1),
    )

    orphans = find_orphan_codex_processes("codex")

    assert orphans == []
    assert "orphan detection skipped" in capsys.readouterr().err


def test_find_orphan_ps_timeout_returns_empty_and_logs(mocker, capsys):
    """ps timeout returns empty list and logs a one-line note to stderr."""
    mocker.patch(
        "conductor.orphan_detect.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="ps", timeout=5),
    )

    orphans = find_orphan_codex_processes("codex")

    assert orphans == []
    assert "orphan detection skipped" in capsys.readouterr().err


def test_find_orphan_ps_not_found_returns_empty_and_logs(mocker, capsys):
    """ps not on PATH returns empty list and logs a note."""
    mocker.patch(
        "conductor.orphan_detect.subprocess.run",
        side_effect=FileNotFoundError("ps: not found"),
    )

    orphans = find_orphan_codex_processes("codex")

    assert orphans == []
    assert "orphan detection skipped" in capsys.readouterr().err


def test_format_orphan_hints_includes_pid_and_kill_command():
    """format_orphan_hints includes the PID and a copy-pasteable kill command."""
    orphans = [OrphanProcess(pid=10182, etime="06:32:00")]
    hints = format_orphan_hints(orphans)

    assert "10182" in hints
    assert "kill 10182" in hints
    assert "06:32:00" in hints


def test_format_orphan_hints_empty_list():
    assert format_orphan_hints([]) == ""


# ---------------------------------------------------------------------------
# Integration test: stall error includes orphan PIDs end-to-end
# ---------------------------------------------------------------------------

# Re-use the _FakePopen helper from the subprocess adapter tests to avoid
# duplicating threading infrastructure.
from tests.test_adapters_subprocess import _FakePopen, _patch_codex_popen  # noqa: E402


def test_codex_exec_stall_error_enriched_with_orphan_pid(mocker, monkeypatch, tmp_path):
    """ProviderStalledError from a wedged exec includes orphan PID and kill hint."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")

    # Codex starts but produces zero output → stall watchdog fires
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    # Simulate an orphan codex process (parent 9999 is dead)
    ps_out = (
        _PS_HEADER
        + _ps_line(
            10182, 9999, "06:32:00",
            "/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox",
        )
    )
    mocker.patch(
        "conductor.orphan_detect.subprocess.run",
        return_value=_fake_ps(ps_out),
    )
    mocker.patch("conductor.orphan_detect.os.kill", side_effect=ProcessLookupError)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "stalled" in msg
    assert "10182" in msg
    assert "kill 10182" in msg
    assert fake.terminated is True


def test_codex_exec_stall_error_no_orphans_unmodified(mocker, monkeypatch, tmp_path):
    """Stall error without orphans is unmodified (no extra lines appended)."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")

    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    # ps returns no matching processes
    mocker.patch(
        "conductor.orphan_detect.subprocess.run",
        return_value=_fake_ps(_PS_HEADER),
    )
    mocker.patch("conductor.orphan_detect.os.kill", return_value=None)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "stalled" in msg
    assert "kill" not in msg
    assert "stale codex" not in msg


def test_codex_exec_stall_orphan_detection_failure_does_not_crash(
    mocker, monkeypatch, tmp_path
):
    """If orphan detection throws unexpectedly, original stall error still surfaces."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")

    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    # Simulate an unexpected exception in the detection helper
    mocker.patch(
        "conductor.providers.codex.find_orphan_codex_processes",
        side_effect=RuntimeError("unexpected boom"),
    )

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)

    assert "stalled" in str(exc.value)
