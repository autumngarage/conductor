"""Citation grounding guardrail for conductor exec.

After a dispatch completes, parse the output for citation patterns (symbol names,
file paths with line numbers) and grep the worktree to verify they exist. Misses
are emitted as warnings — the run is never failed, just flagged.

Opt-in via ``conductor exec --ground-citations``. Off by default so existing
callers are unaffected.

Patterns recognised (pragmatic, not exhaustive):
  - `symbol_name` in path/to/file.py:N   (backtick-quoted name + path:line)
  - `symbol_name` in path/to/file.py     (backtick-quoted name + path, no line)
  - path/to/file.py:N                    (bare path:line)
  - symbol_name(...) followed by a path  (function call shape, best-effort)
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field

# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #

_BACKTICK_SYMBOL_PATH = re.compile(
    r"`([^`]+)`\s+in\s+([\w./\-]+\.py)(?::(\d+))?",
)
_BARE_PATH_LINE = re.compile(
    r"\b([\w./\-]+\.py):(\d+)\b",
)
_FUNC_CALL_PATH = re.compile(
    r"\b(\w[\w.]*)\([^)]*\)\s+(?:in\s+)?([\w./\-]+\.py)(?::(\d+))?",
)


@dataclass(frozen=True)
class Citation:
    symbol: str
    path: str | None
    line: int | None
    raw: str


@dataclass
class GroundingMiss:
    citation: Citation
    reason: str


@dataclass
class GroundingReport:
    misses: list[GroundingMiss] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


def parse_citations(text: str) -> list[Citation]:
    """Extract citation candidates from dispatch output.

    Returns deduplicated citations ordered by first appearance.
    """
    seen: set[tuple[str, str | None]] = set()
    results: list[Citation] = []

    def _add(symbol: str, path: str | None, line_s: str | None, raw: str) -> None:
        line = int(line_s) if line_s else None
        key = (symbol, path)
        if key not in seen:
            seen.add(key)
            results.append(Citation(symbol=symbol, path=path, line=line, raw=raw))

    for m in _BACKTICK_SYMBOL_PATH.finditer(text):
        _add(m.group(1), m.group(2), m.group(3), m.group(0))

    for m in _BARE_PATH_LINE.finditer(text):
        _add(m.group(1), m.group(1), m.group(2), m.group(0))

    for m in _FUNC_CALL_PATH.finditer(text):
        _add(m.group(1), m.group(2), m.group(3), m.group(0))

    return results


# --------------------------------------------------------------------------- #
# Grounder
# --------------------------------------------------------------------------- #


def _grep_symbol(symbol: str, worktree: str) -> bool:
    """Return True if symbol appears anywhere under worktree."""
    try:
        result = subprocess.run(
            ["grep", "-rF", "--quiet", symbol, worktree],
            capture_output=True,
            timeout=10,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"grep failed for symbol {symbol!r}: {e}") from e


def _path_exists(path: str, worktree: str) -> bool:
    """Return True if path exists relative to worktree (or as absolute)."""
    import os

    if os.path.isabs(path):
        return os.path.exists(path)
    return os.path.exists(os.path.join(worktree, path))


def ground_citations(
    citations: list[Citation],
    worktree: str,
) -> GroundingReport:
    """Verify each citation against the worktree.

    For citations with a path: first check path existence, then grep symbol.
    For symbol-only citations: grep the worktree.
    Grep failures (OSError, timeout) are collected as errors, not misses.
    """
    report = GroundingReport()
    for cit in citations:
        try:
            if cit.path and not _path_exists(cit.path, worktree):
                report.misses.append(
                    GroundingMiss(
                        citation=cit,
                        reason=f"path not found: {cit.path}",
                    )
                )
                continue
            found = _grep_symbol(cit.symbol, worktree)
            if not found:
                report.misses.append(
                    GroundingMiss(
                        citation=cit,
                        reason=f"symbol not found in worktree: {cit.symbol!r}",
                    )
                )
        except RuntimeError as e:
            report.errors.append(str(e))
    return report


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #


def report_grounding(report: GroundingReport) -> list[str]:
    """Emit grounding warnings to stderr and return them for JSON injection.

    Always writes to stderr. Caller injects the returned list into the JSON
    payload when ``--json`` is active.
    """
    warnings: list[str] = []

    for miss in report.misses:
        msg = f"[grounding] unverified citation: {miss.citation.raw!r} — {miss.reason}"
        warnings.append(msg)
        print(f"conductor: WARNING {msg}", file=sys.stderr)

    for err in report.errors:
        msg = f"[grounding] error: {err}"
        warnings.append(msg)
        print(f"conductor: WARNING {msg}", file=sys.stderr)

    return warnings
