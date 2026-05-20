"""Detect and reap long-running provider processes that escape operator attention.

See issue #499. Real incident on 2026-05-20: 8 forgotten codex agents ran 40+
hours each in Vesper panes the operator had stopped watching, driving a
$215/week OpenRouter bill. The right structural fix is a single command the
operator can run (and a `conductor doctor` audit that surfaces the same data)
to find and kill these before they compound.

Pure detection lives here; the CLI wiring is in cli.py. Keep the surface tight
so tests can drive it without spawning real processes.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass

DEFAULT_REAP_PATTERNS: tuple[str, ...] = (
    "codex --dangerously",
    "codex exec",
    "conductor exec",
    "conductor swarm",
)

DEFAULT_REAP_THRESHOLD_SEC = 3600  # 1 hour

_PS_FIELDS = ("pid", "ppid", "etime", "command")


@dataclass(frozen=True)
class StaleProcess:
    """One provider-related process that has been alive longer than the threshold."""

    pid: int
    ppid: int
    etime_seconds: int
    etime_str: str
    command: str

    def payload(self) -> dict[str, object]:
        return {
            "pid": self.pid,
            "ppid": self.ppid,
            "etime_seconds": self.etime_seconds,
            "etime": self.etime_str,
            "command": self.command,
        }


def parse_etime(value: str) -> int | None:
    """Parse a ps ``etime`` field (``MM:SS``, ``HH:MM:SS``, ``DD-HH:MM:SS``).

    Returns total seconds, or None on unrecognized formats. ps uses these forms
    on Linux and macOS, with leading zeros stripped from the most-significant
    field.
    """
    if not value or value == "-":
        return None
    days = 0
    rest = value
    if "-" in rest:
        head, _, rest = rest.partition("-")
        if not head.isdigit():
            return None
        days = int(head)
    parts = rest.split(":")
    if not all(p.isdigit() for p in parts):
        return None
    if len(parts) == 2:
        h, m, s = 0, int(parts[0]), int(parts[1])
    elif len(parts) == 3:
        h, m, s = int(parts[0]), int(parts[1]), int(parts[2])
    else:
        return None
    return days * 86400 + h * 3600 + m * 60 + s


def _matches_any(command: str, patterns: tuple[str, ...]) -> bool:
    return any(pat in command for pat in patterns)


def scan_ps(ps_runner=None) -> list[tuple[int, int, str, str]]:
    """Run ``ps`` and return parsed rows. Injectable for tests."""
    if ps_runner is None:
        result = subprocess.run(
            ["ps", "-o", ",".join(_PS_FIELDS), "-ax"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode != 0:
            return []
        output = result.stdout
    else:
        output = ps_runner()

    rows: list[tuple[int, int, str, str]] = []
    for line in output.splitlines()[1:]:  # skip header
        # ps fixed-width output: PID PPID ETIME COMMAND...
        # Splitting on whitespace works because PID/PPID/ETIME never contain spaces.
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_s, ppid_s, etime, command = parts[0], parts[1], parts[2], parts[3]
        if not pid_s.isdigit() or not ppid_s.isdigit():
            continue
        rows.append((int(pid_s), int(ppid_s), etime, command))
    return rows


def scan_stale_processes(
    *,
    threshold_sec: int = DEFAULT_REAP_THRESHOLD_SEC,
    patterns: tuple[str, ...] = DEFAULT_REAP_PATTERNS,
    ps_runner=None,
) -> list[StaleProcess]:
    """Return processes matching ``patterns`` whose age >= ``threshold_sec``.

    The ``ps_runner`` hook returns a ps-style stdout string and exists so tests
    can drive the parser without invoking the real binary.
    """
    rows = scan_ps(ps_runner=ps_runner)
    own_pid = os.getpid()
    stale: list[StaleProcess] = []
    for pid, ppid, etime, command in rows:
        if pid == own_pid:
            continue
        if not _matches_any(command, patterns):
            continue
        age = parse_etime(etime)
        if age is None or age < threshold_sec:
            continue
        stale.append(
            StaleProcess(
                pid=pid,
                ppid=ppid,
                etime_seconds=age,
                etime_str=etime,
                command=command,
            )
        )
    stale.sort(key=lambda p: p.etime_seconds, reverse=True)
    return stale


def reap_processes(
    processes: list[StaleProcess],
    *,
    grace_sec: float = 5.0,
    signal_sender=None,
    sleeper=None,
    alive_check=None,
) -> tuple[list[int], list[dict[str, object]]]:
    """Send SIGTERM then SIGKILL to ``processes`` and report what happened.

    Returns ``(killed_pids, errors)``. ``errors`` is a list of dicts each with
    ``pid`` and ``error`` keys for processes the reaper couldn't terminate.

    The signal sender, sleeper, and alive check are injectable for tests so we
    don't need to spawn real processes to exercise the kill flow.
    """
    if not processes:
        return [], []

    if signal_sender is None:
        def signal_sender(pid: int, sig: int) -> None:
            os.kill(pid, sig)

    if sleeper is None:
        sleeper = time.sleep

    if alive_check is None:
        def alive_check(pid: int) -> bool:
            try:
                os.kill(pid, 0)
                return True
            except ProcessLookupError:
                return False
            except PermissionError:
                return True

    killed: list[int] = []
    errors: list[dict[str, object]] = []

    for proc in processes:
        try:
            signal_sender(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            killed.append(proc.pid)
            continue
        except OSError as e:
            errors.append({"pid": proc.pid, "error": f"SIGTERM failed: {e}"})

    sleeper(grace_sec)

    for proc in processes:
        if any(err["pid"] == proc.pid for err in errors):
            continue
        if not alive_check(proc.pid):
            if proc.pid not in killed:
                killed.append(proc.pid)
            continue
        try:
            signal_sender(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            killed.append(proc.pid)
            continue
        except OSError as e:
            errors.append({"pid": proc.pid, "error": f"SIGKILL failed: {e}"})
            continue
        # one more brief grace, then a final liveness check
        sleeper(0.5)
        if alive_check(proc.pid):
            errors.append(
                {"pid": proc.pid, "error": "process survived SIGKILL (zombie?)"}
            )
        else:
            killed.append(proc.pid)

    return killed, errors


_DURATION_RE = re.compile(r"^(\d+)\s*([smhdw])?$", re.IGNORECASE)


def parse_duration(value: str) -> int:
    """Parse ``30m``, ``2h``, ``1d``, ``300s``, or bare integer seconds."""
    m = _DURATION_RE.match(value.strip())
    if not m:
        raise ValueError(
            f"invalid duration {value!r}: expected forms like 30s, 5m, 2h, 1d"
        )
    n = int(m.group(1))
    unit = (m.group(2) or "s").lower()
    multipliers = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 86400 * 7}
    return n * multipliers[unit]


def format_etime_human(seconds: int) -> str:
    """Human-readable age: ``2d 4h``, ``45m``, ``30s``."""
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h {(seconds % 3600) // 60}m"
    return f"{seconds // 86400}d {(seconds % 86400) // 3600}h"
