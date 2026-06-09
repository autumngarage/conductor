"""Exec-owned commit-boundary enforcement."""

from __future__ import annotations

import fnmatch
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

GIT_TIMEOUT_SEC = 5.0
PATH_TOKEN = re.compile(
    r"(?P<path>(?:[A-Za-z0-9_.-]+/)+(?:[A-Za-z0-9_.-]+)|[A-Za-z0-9_.-]+\.(?:py|md|sh|toml|yml|yaml|json|txt))"
)


@dataclass(frozen=True)
class ExecBoundarySnapshot:
    worktree: Path | None
    head: str | None
    dirty_paths: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class ExecBoundaryResult:
    status: str
    scoped_paths: tuple[str, ...] = ()
    committed_paths: tuple[str, ...] = ()
    out_of_scope_paths: tuple[str, ...] = ()
    commit_sha: str | None = None
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "scoped_paths": list(self.scoped_paths),
            "committed_paths": list(self.committed_paths),
            "out_of_scope_paths": list(self.out_of_scope_paths),
            "commit_sha": self.commit_sha,
            "warnings": list(self.warnings),
        }


def capture_exec_boundary_snapshot(cwd: str | None) -> ExecBoundarySnapshot:
    worktree = _git_root(Path(cwd or ".").resolve())
    if worktree is None:
        return ExecBoundarySnapshot(None, None)
    return ExecBoundarySnapshot(
        worktree=worktree,
        head=_git_stdout(worktree, ["rev-parse", "HEAD"]),
        dirty_paths=frozenset(_dirty_paths(worktree)),
    )


def brief_declares_exec_boundary_scope(brief: str) -> bool:
    """Return true when a brief names files/directories safe to bound commits to."""
    return bool(_scoped_paths(brief, agent_write_set=set()))


def enforce_exec_boundary(
    snapshot: ExecBoundarySnapshot,
    *,
    brief: str,
    agent_write_set: set[str],
    commit_message: str = "chore(exec): checkpoint delegated changes",
) -> ExecBoundaryResult:
    if snapshot.worktree is None:
        return ExecBoundaryResult("not-a-git-worktree")
    scoped = _scoped_paths(brief, agent_write_set)
    if not scoped:
        return ExecBoundaryResult(
            "scope-unknown",
            warnings=("exec boundary skipped: no deterministic file scope was captured",),
        )
    dirty = set(_dirty_paths(snapshot.worktree)) - set(snapshot.dirty_paths)
    if not dirty:
        return ExecBoundaryResult("clean", scoped_paths=tuple(sorted(scoped)))

    in_scope_paths = {path for path in dirty if _matches_scope(path, scoped)}
    out_of_scope_paths = dirty - in_scope_paths
    in_scope = tuple(sorted(in_scope_paths))
    out_of_scope = tuple(sorted(out_of_scope_paths))
    warnings: list[str] = []
    if out_of_scope:
        warnings.append(
            "exec produced out-of-scope dirty paths: " + ", ".join(out_of_scope)
        )
    if not in_scope:
        return ExecBoundaryResult(
            "out-of-scope-only",
            scoped_paths=tuple(sorted(scoped)),
            out_of_scope_paths=out_of_scope,
            warnings=tuple(warnings),
        )

    stage_error = _git_run(snapshot.worktree, ["add", "--", *in_scope])
    if stage_error is not None:
        return ExecBoundaryResult(
            "commit-failed",
            scoped_paths=tuple(sorted(scoped)),
            committed_paths=in_scope,
            out_of_scope_paths=out_of_scope,
            warnings=tuple([*warnings, stage_error]),
        )
    commit_error = _git_run(
        snapshot.worktree,
        ["commit", "-m", commit_message, "--", *in_scope],
    )
    if commit_error is not None:
        return ExecBoundaryResult(
            "commit-failed",
            scoped_paths=tuple(sorted(scoped)),
            committed_paths=in_scope,
            out_of_scope_paths=out_of_scope,
            warnings=tuple([*warnings, commit_error]),
        )
    return ExecBoundaryResult(
        "committed",
        scoped_paths=tuple(sorted(scoped)),
        committed_paths=in_scope,
        out_of_scope_paths=out_of_scope,
        commit_sha=_git_stdout(snapshot.worktree, ["rev-parse", "--short", "HEAD"]),
        warnings=tuple(warnings),
    )


def _scoped_paths(brief: str, agent_write_set: set[str]) -> set[str]:
    paths: set[str] = set()
    for match in PATH_TOKEN.finditer(brief):
        if match.end() < len(brief) and brief[match.end()] == "#":
            continue
        path = _trim_path_token(match.group("path"))
        if path and not path.startswith(("../", "/")):
            paths.add(path)
    if paths:
        return paths
    paths = {path for path in agent_write_set if path}
    return paths


def _trim_path_token(raw: str) -> str:
    path = raw.strip("`'\"")
    return path.rstrip(".,);:")


def _matches_scope(path: str, scoped: set[str]) -> bool:
    for scope in scoped:
        if path == scope or path.startswith(scope.rstrip("/") + "/"):
            return True
        if any(ch in scope for ch in "*?[]") and fnmatch.fnmatch(path, scope):
            return True
    return False


def _git_root(path: Path) -> Path | None:
    root = _git_stdout(path, ["rev-parse", "--show-toplevel"])
    return Path(root).resolve() if root else None


def _dirty_paths(worktree: Path) -> list[str]:
    output = _git_stdout(worktree, ["status", "--porcelain", "-z", "--untracked-files=all"])
    if not output:
        return []
    entries = output.split("\0")
    paths: list[str] = []
    idx = 0
    while idx < len(entries):
        entry = entries[idx]
        idx += 1
        if not entry:
            continue
        status, path = _status_entry_path(entry)
        if status.strip().startswith(("R", "C")) and idx < len(entries):
            path = entries[idx]
            idx += 1
        if path:
            paths.append(path)
    return sorted(dict.fromkeys(paths))


def _status_entry_path(entry: str) -> tuple[str, str]:
    if len(entry) >= 3 and entry[2] == " ":
        return entry[:2], entry[3:]
    status, separator, path = entry.partition(" ")
    if separator:
        return status, path
    if len(entry) > 2:
        return entry[:2], entry[2:]
    return entry, ""


def _git_stdout(worktree: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_run(worktree: Path, args: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SEC,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"`git {' '.join(args)}` failed: {exc}"
    if result.returncode != 0:
        detail = " ".join((result.stderr or result.stdout or "unknown error").split())
        return f"`git {' '.join(args)}` failed: {detail}"
    return None
