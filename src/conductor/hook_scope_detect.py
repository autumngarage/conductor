from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_GIT_TIMEOUT_SEC = 5.0


def detect_hook_staged_scope_creep(
    worktree: Path,
    agent_write_set: set[str],
    *,
    base_head: str,
) -> list[tuple[str, ...]]:
    """Return per-commit extra paths that landed outside the intended write set.

    Each tuple corresponds to one commit created in ``base_head..HEAD`` and
    contains sorted paths that appeared in that commit but not in
    ``agent_write_set``.
    """

    if not agent_write_set:
        return []

    commits = _commits_since(worktree, base_head)
    if not commits:
        return []

    extras_per_commit: list[tuple[str, ...]] = []
    for commit in commits:
        changed = _commit_changed_paths(worktree, commit)
        if not changed:
            continue
        extras = tuple(sorted(path for path in changed if path not in agent_write_set))
        if extras:
            extras_per_commit.append(extras)
    return extras_per_commit


def format_hook_scope_creep_warning(
    *,
    extra_paths: Iterable[str],
    intended_paths: Iterable[str],
) -> str:
    extras = sorted(dict.fromkeys(extra_paths))
    intended = sorted(dict.fromkeys(intended_paths))
    extras_text = ", ".join(extras)
    intended_text = ", ".join(intended) if intended else "(none captured)"
    return (
        "[conductor] commit grew by hook: "
        f"{extras_text}. "
        f"Agent intended: {intended_text}. "
        "If unexpected, inspect .pre-commit-config.yaml."
    )


def git_head(worktree: Path) -> str | None:
    result = _run_git(worktree, ["rev-parse", "HEAD"])
    if result is None:
        return None
    head = result.strip()
    return head or None


def git_head_via_fs(worktree: Path) -> str | None:
    """Read git HEAD from the filesystem, no subprocess. Returns None if not in a repo."""
    git_dir = _resolve_git_dir(worktree)
    if git_dir is None:
        return None
    head_file = git_dir / "HEAD"
    try:
        contents = head_file.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if contents.startswith("ref: "):
        ref_path = contents[5:].strip()
        ref_file = git_dir / ref_path
        try:
            return ref_file.read_text(encoding="utf-8").strip() or None
        except OSError:
            packed = git_dir / "packed-refs"
            try:
                for line in packed.read_text(encoding="utf-8").splitlines():
                    if line.startswith(("#", "^")):
                        continue
                    parts = line.split(None, 1)
                    if len(parts) == 2 and parts[1] == ref_path:
                        return parts[0]
            except OSError:
                return None
            return None
    return contents or None


def _resolve_git_dir(worktree: Path) -> Path | None:
    """Return the .git directory for a worktree, walking parents and handling
    worktree's .git-file indirection."""
    try:
        current = worktree.resolve()
    except OSError:
        current = worktree
    while True:
        candidate = current / ".git"
        if candidate.is_dir():
            return candidate
        if candidate.is_file():
            try:
                line = candidate.read_text(encoding="utf-8").strip()
            except OSError:
                return None
            if line.startswith("gitdir: "):
                git_dir = Path(line[len("gitdir: ") :].strip())
                if not git_dir.is_absolute():
                    git_dir = (current / git_dir).resolve()
                return git_dir if git_dir.exists() else None
            return None
        if current.parent == current:
            return None
        current = current.parent


def normalize_repo_relative_path(path: str, *, worktree: Path) -> str | None:
    raw = path.strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            relative = candidate.resolve().relative_to(worktree.resolve())
        except (OSError, ValueError):
            return None
        normalized = relative.as_posix()
    else:
        normalized = candidate.as_posix()
        while normalized.startswith("./"):
            normalized = normalized[2:]
    return normalized or None


def _commits_since(worktree: Path, base_head: str) -> list[str]:
    output = _run_git(worktree, ["rev-list", "--reverse", f"{base_head}..HEAD"])
    if output is None:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _commit_changed_paths(worktree: Path, commit: str) -> list[str]:
    output = _run_git(
        worktree,
        ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", commit],
    )
    if output is None:
        return []
    paths: list[str] = []
    for line in output.splitlines():
        normalized = normalize_repo_relative_path(line, worktree=worktree)
        if normalized is not None:
            paths.append(normalized)
    return list(dict.fromkeys(paths))


def _run_git(worktree: Path, args: list[str]) -> str | None:
    return _run_git_command(
        ["git", *args],
        cwd=worktree,
        timeout_sec=_GIT_TIMEOUT_SEC,
    )


def _run_git_command(
    command: list[str],
    *,
    cwd: Path,
    timeout_sec: float = _GIT_TIMEOUT_SEC,
) -> str | None:
    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired, TypeError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout
