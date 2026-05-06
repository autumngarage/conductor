"""Orphan codex process detection for stalled exec diagnostics.

When conductor exec --with codex wedges at startup, an orphan codex process
from a prior session may be holding the --ephemeral ChatGPT auth lock. This
module finds codex processes whose parent is no longer alive and formats
copy-pasteable kill hints so the operator knows how to unblock.

Cross-platform: detection runs on macOS and Linux. Windows is skipped.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class OrphanProcess:
    pid: int
    etime: str  # elapsed time string from ps (e.g. "06:32:00")


def find_orphan_codex_processes(cli_name: str = "codex") -> list[OrphanProcess]:
    """Return codex processes whose parent PID is no longer alive.

    Degrades gracefully: returns [] when ps is unavailable, times out, or
    exits non-zero (with a one-line stderr note so the skip is visible).
    """
    if sys.platform == "win32":
        return []

    try:
        result = subprocess.run(
            ["ps", "-eo", "pid,ppid,etime,command"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        sys.stderr.write("[conductor] orphan detection skipped: ps not found\n")
        return []
    except OSError as exc:
        sys.stderr.write(f"[conductor] orphan detection skipped: ps unavailable ({exc})\n")
        return []
    except subprocess.TimeoutExpired:
        sys.stderr.write("[conductor] orphan detection skipped: ps timed out\n")
        return []

    if result.returncode != 0:
        sys.stderr.write(
            f"[conductor] orphan detection skipped: ps exited {result.returncode}\n"
        )
        return []

    orphans: list[OrphanProcess] = []
    for line in result.stdout.splitlines()[1:]:  # skip header row
        parts = line.split(None, 3)
        if len(parts) < 4:
            continue
        pid_str, ppid_str, etime, command = parts
        if cli_name not in command:
            continue
        try:
            pid = int(pid_str)
            ppid = int(ppid_str)
        except ValueError:
            continue
        if not _pid_alive(ppid):
            orphans.append(OrphanProcess(pid=pid, etime=etime))

    return orphans


def _pid_alive(pid: int) -> bool:
    """Return True if a process with the given PID is currently alive."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but we lack permission to signal it — still alive.
        return True
    except OSError:
        return False


def format_orphan_hints(orphans: list[OrphanProcess]) -> str:
    """Format copy-pasteable kill hints for each orphan process."""
    lines = [
        f"[conductor] detected stale codex PID {o.pid} (running for {o.etime});"
        f" if no active conductor session is using it, run `kill {o.pid}` to unwedge."
        for o in orphans
    ]
    return "\n".join(lines)
