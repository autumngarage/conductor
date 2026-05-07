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

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

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
    symbol: str | None
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
    seen: set[tuple[str | None, str | None, int | None]] = set()
    results: list[Citation] = []
    occupied_spans: list[tuple[int, int]] = []

    def _add(
        symbol: str | None,
        path: str | None,
        line_s: str | None,
        raw: str,
    ) -> None:
        line = int(line_s) if line_s else None
        key = (symbol, path, line)
        if key not in seen:
            seen.add(key)
            results.append(Citation(symbol=symbol, path=path, line=line, raw=raw))

    symbol_matches: list[tuple[int, str | None, str | None, str | None, str]] = []
    for m in _BACKTICK_SYMBOL_PATH.finditer(text):
        symbol_matches.append((m.start(), m.group(1), m.group(2), m.group(3), m.group(0)))
        occupied_spans.append(m.span())
    for m in _FUNC_CALL_PATH.finditer(text):
        symbol_matches.append((m.start(), m.group(1), m.group(2), m.group(3), m.group(0)))
        occupied_spans.append(m.span())

    matches = symbol_matches
    for m in _BARE_PATH_LINE.finditer(text):
        if any(start <= m.start() and m.end() <= end for start, end in occupied_spans):
            continue
        matches.append((m.start(), None, m.group(1), m.group(2), m.group(0)))

    for _, symbol, path, line_s, raw in sorted(matches, key=lambda item: item[0]):
        _add(symbol, path, line_s, raw)

    return results


# --------------------------------------------------------------------------- #
# Grounder
# --------------------------------------------------------------------------- #


def _grep_symbol(symbol: str, path: Path) -> bool:
    """Return True if symbol appears under path."""
    try:
        result = subprocess.run(
            ["grep", "-rF", "--quiet", "--", symbol, str(path)],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"grep failed for symbol {symbol!r}: {e}") from e


def _resolve_path(path: str, worktree: Path) -> Path:
    if os.path.isabs(path):
        return Path(path)
    return worktree / path


def _line_exists(path: Path, line: int) -> bool:
    if line < 1:
        return False
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as fh:
            for index, _ in enumerate(fh, start=1):
                if index == line:
                    return True
    except OSError as e:
        raise RuntimeError(f"could not read {path}: {e}") from e
    return False


def ground_citations(
    text: str,
    worktree: str | os.PathLike[str],
) -> GroundingReport:
    """Verify citation candidates in text against the worktree.

    Path citations resolve only when the file exists and any cited line is in
    range. Symbol citations with a path must resolve inside that path; symbol
    citations without a path search the whole worktree. Grep/read failures are
    collected as errors, not misses, so the caller can warn without changing the
    dispatch exit code.
    """
    report = GroundingReport()
    worktree_path = Path(worktree)
    citations = parse_citations(text)
    for cit in citations:
        try:
            search_path = worktree_path
            if cit.path:
                search_path = _resolve_path(cit.path, worktree_path)
                if not search_path.exists():
                    report.misses.append(
                        GroundingMiss(citation=cit, reason="file does not exist")
                    )
                    continue
                if cit.line is not None and not _line_exists(search_path, cit.line):
                    report.misses.append(
                        GroundingMiss(
                            citation=cit,
                            reason=f"line {cit.line} does not exist in {cit.path}",
                        )
                    )
                    continue
            if cit.symbol is None:
                continue
            if not _grep_symbol(cit.symbol, search_path):
                location = f" in {cit.path}" if cit.path else ""
                report.misses.append(
                    GroundingMiss(
                        citation=cit,
                        reason=f"symbol not found{location}",
                    )
                )
        except RuntimeError as e:
            report.errors.append(str(e))
    return report


# --------------------------------------------------------------------------- #
# Reporter
# --------------------------------------------------------------------------- #


def _display_citation(citation: Citation) -> str:
    if citation.symbol and citation.path:
        symbol = citation.raw.split(" in ", 1)[0]
        return f"`{symbol.strip('`')}` in {citation.path}"
    if citation.path:
        return citation.path
    return f"`{citation.symbol}`"


def format_grounding_warning(report: GroundingReport) -> str | None:
    """Return a structured warning block for grounding misses/errors."""
    lines: list[str] = []
    if report.misses:
        lines.append(f"[conductor] grounding misses: {len(report.misses)}")
        for miss in report.misses:
            citation = _display_citation(miss.citation)
            line_suffix = (
                f":{miss.citation.line}"
                if miss.citation.line is not None and miss.citation.path
                else ""
            )
            if miss.citation.path:
                citation = f"{citation}{line_suffix}"
            lines.append(f"  - {citation} — {miss.reason}")
    for err in report.errors:
        if not lines:
            lines.append(f"[conductor] grounding errors: {len(report.errors)}")
        lines.append(f"  - grounding check error — {err}")
    return "\n".join(lines) if lines else None
