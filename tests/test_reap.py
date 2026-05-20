"""Tests for the reap module: detection, parsing, and kill flow."""

from __future__ import annotations

import signal

import pytest

from conductor.reap import (
    DEFAULT_REAP_PATTERNS,
    StaleProcess,
    format_etime_human,
    parse_duration,
    parse_etime,
    reap_processes,
    scan_stale_processes,
)


class TestParseEtime:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("00:30", 30),
            ("05:00", 300),
            ("01:30:00", 5400),
            ("12:34:56", 12 * 3600 + 34 * 60 + 56),
            ("1-00:00:00", 86400),
            ("2-04:30:15", 2 * 86400 + 4 * 3600 + 30 * 60 + 15),
        ],
    )
    def test_known_formats(self, value, expected):
        assert parse_etime(value) == expected

    @pytest.mark.parametrize("value", ["", "-", "garbage", "01", "xx:yy"])
    def test_unrecognized_returns_none(self, value):
        assert parse_etime(value) is None


class TestParseDuration:
    @pytest.mark.parametrize(
        "value,expected",
        [
            ("30s", 30),
            ("30", 30),
            ("5m", 300),
            ("2h", 7200),
            ("1d", 86400),
            ("1w", 86400 * 7),
            ("2H", 7200),  # case-insensitive
        ],
    )
    def test_known_forms(self, value, expected):
        assert parse_duration(value) == expected

    @pytest.mark.parametrize("value", ["", "abc", "5x", "1.5h", "h5"])
    def test_invalid_raises(self, value):
        with pytest.raises(ValueError):
            parse_duration(value)


class TestFormatEtimeHuman:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (5, "5s"),
            (59, "59s"),
            (60, "1m"),
            (3599, "59m"),
            (3600, "1h 0m"),
            (7320, "2h 2m"),
            (86400, "1d 0h"),
            (180_000, "2d 2h"),
        ],
    )
    def test_format(self, seconds, expected):
        assert format_etime_human(seconds) == expected


def _ps_output(*rows: tuple[int, int, str, str]) -> str:
    """Build a fake ps stdout. Each row is (pid, ppid, etime, command)."""
    lines = ["  PID  PPID     ELAPSED COMMAND"]
    for pid, ppid, etime, command in rows:
        lines.append(f"{pid:>5} {ppid:>5} {etime:>11} {command}")
    return "\n".join(lines) + "\n"


class TestScanStaleProcesses:
    def test_filters_by_pattern(self):
        codex_cmd = (
            "/opt/homebrew/bin/codex --dangerously-bypass-approvals-and-sandbox"
        )
        ps = _ps_output(
            (100, 1, "02:00:00", codex_cmd),
            (101, 1, "02:00:00", "/usr/bin/python3 some-other-thing.py"),
            (102, 1, "02:00:00", "conductor exec --brief-file /tmp/b.md"),
        )
        stale = scan_stale_processes(threshold_sec=3600, ps_runner=lambda: ps)
        pids = sorted(p.pid for p in stale)
        assert pids == [100, 102]

    def test_threshold_excludes_young(self):
        ps = _ps_output(
            (100, 1, "00:30:00", "codex --dangerously-x"),
            (200, 1, "02:00:00", "codex --dangerously-x"),
        )
        stale = scan_stale_processes(threshold_sec=3600, ps_runner=lambda: ps)
        assert [p.pid for p in stale] == [200]

    def test_default_patterns_cover_known_offenders(self):
        ps = _ps_output(
            (100, 1, "02:00:00", "codex --dangerously-bypass-approvals-and-sandbox"),
            (101, 1, "02:00:00", "codex exec --resume"),
            (102, 1, "02:00:00", "conductor exec --with codex"),
            (103, 1, "02:00:00", "conductor swarm --brief a.md"),
        )
        stale = scan_stale_processes(threshold_sec=3600, ps_runner=lambda: ps)
        assert sorted(p.pid for p in stale) == [100, 101, 102, 103]

    def test_extra_patterns_extend_defaults(self):
        ps = _ps_output(
            (100, 1, "02:00:00", "my-custom-agent --loop"),
            (101, 1, "02:00:00", "codex --dangerously-x"),
        )
        stale = scan_stale_processes(
            threshold_sec=3600,
            patterns=DEFAULT_REAP_PATTERNS + ("my-custom-agent",),
            ps_runner=lambda: ps,
        )
        assert sorted(p.pid for p in stale) == [100, 101]

    def test_results_sorted_by_age_desc(self):
        ps = _ps_output(
            (100, 1, "01:30:00", "codex --dangerously-x"),
            (101, 1, "04:00:00", "codex --dangerously-x"),
            (102, 1, "02:00:00", "codex --dangerously-x"),
        )
        stale = scan_stale_processes(threshold_sec=3600, ps_runner=lambda: ps)
        assert [p.pid for p in stale] == [101, 102, 100]

    def test_excludes_own_pid(self):
        import os

        ps = _ps_output(
            (os.getpid(), 1, "02:00:00", "codex --dangerously-x"),
            (9999, 1, "02:00:00", "codex --dangerously-x"),
        )
        stale = scan_stale_processes(threshold_sec=3600, ps_runner=lambda: ps)
        assert [p.pid for p in stale] == [9999]

    def test_malformed_ps_lines_are_skipped(self):
        ps = "PID PPID ELAPSED COMMAND\nbroken line\n100 1 02:00:00 codex --dangerously-x\n"
        stale = scan_stale_processes(threshold_sec=3600, ps_runner=lambda: ps)
        assert [p.pid for p in stale] == [100]


class _FakeSignals:
    def __init__(
        self,
        *,
        dies_on_term: set[int] | None = None,
        dies_on_kill: set[int] | None = None,
    ):
        self.dies_on_term = dies_on_term or set()
        self.dies_on_kill = dies_on_kill or set()
        self.alive: set[int] = set()
        self.calls: list[tuple[int, int]] = []

    def register(self, *pids: int) -> None:
        self.alive.update(pids)

    def send(self, pid: int, sig: int) -> None:
        self.calls.append((pid, sig))
        if pid not in self.alive:
            raise ProcessLookupError(f"no such pid {pid}")
        if (sig == signal.SIGTERM and pid in self.dies_on_term) or (
            sig == signal.SIGKILL and pid in self.dies_on_kill
        ):
            self.alive.discard(pid)

    def is_alive(self, pid: int) -> bool:
        return pid in self.alive


class TestReapProcesses:
    def _proc(self, pid: int, etime_seconds: int = 7200) -> StaleProcess:
        return StaleProcess(
            pid=pid,
            ppid=1,
            etime_seconds=etime_seconds,
            etime_str="02:00:00",
            command=f"codex --dangerously-x-{pid}",
        )

    def test_empty_input_returns_empty(self):
        killed, errors = reap_processes([])
        assert killed == []
        assert errors == []

    def test_dies_on_sigterm(self):
        sigs = _FakeSignals(dies_on_term={100})
        sigs.register(100)
        killed, errors = reap_processes(
            [self._proc(100)],
            signal_sender=sigs.send,
            sleeper=lambda _s: None,
            alive_check=sigs.is_alive,
        )
        assert killed == [100]
        assert errors == []
        assert (100, signal.SIGTERM) in sigs.calls
        assert (100, signal.SIGKILL) not in sigs.calls

    def test_escalates_to_sigkill(self):
        sigs = _FakeSignals(dies_on_kill={100})
        sigs.register(100)
        killed, errors = reap_processes(
            [self._proc(100)],
            signal_sender=sigs.send,
            sleeper=lambda _s: None,
            alive_check=sigs.is_alive,
        )
        assert killed == [100]
        assert errors == []
        # SIGTERM tried first, then SIGKILL when it didn't take
        assert (100, signal.SIGTERM) in sigs.calls
        assert (100, signal.SIGKILL) in sigs.calls

    def test_zombie_survives_sigkill_reported_as_error(self):
        sigs = _FakeSignals()  # dies on neither — pretends to be a stuck process
        sigs.register(100)
        killed, errors = reap_processes(
            [self._proc(100)],
            signal_sender=sigs.send,
            sleeper=lambda _s: None,
            alive_check=sigs.is_alive,
        )
        assert killed == []
        assert len(errors) == 1
        assert errors[0]["pid"] == 100
        assert "survived" in errors[0]["error"]

    def test_already_dead_process_counts_as_killed(self):
        sigs = _FakeSignals()  # nothing registered, so SIGTERM raises ProcessLookupError
        killed, errors = reap_processes(
            [self._proc(100)],
            signal_sender=sigs.send,
            sleeper=lambda _s: None,
            alive_check=sigs.is_alive,
        )
        assert killed == [100]
        assert errors == []

    def test_mixed_outcomes(self):
        sigs = _FakeSignals(dies_on_term={100}, dies_on_kill={101})
        sigs.register(100, 101, 102)
        killed, errors = reap_processes(
            [self._proc(100), self._proc(101), self._proc(102)],
            signal_sender=sigs.send,
            sleeper=lambda _s: None,
            alive_check=sigs.is_alive,
        )
        assert sorted(killed) == [100, 101]
        assert len(errors) == 1
        assert errors[0]["pid"] == 102
