from __future__ import annotations

import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest

import conductor.git_state as git_state_mod
from conductor.git_state import (
    GitStateError,
    list_worktrees,
    scan_git_state,
    tree_equivalent_to,
)

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _commit(repo: Path, message: str, *, when: datetime | None = None) -> None:
    env = None
    if when is not None:
        env = {
            **os.environ,
            "GIT_AUTHOR_DATE": when.isoformat(),
            "GIT_COMMITTER_DATE": when.isoformat(),
        }
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", message, env=env)


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "conductor-test@example.com")
    _git(repo, "config", "user.name", "Conductor Test")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _commit(repo, "base")
    return repo


def test_tree_equivalent_to_detects_squash_shape(git_repo: Path) -> None:
    _git(git_repo, "checkout", "-q", "-b", "feat/applied")
    (git_repo / "feature.txt").write_text("same\n", encoding="utf-8")
    _commit(git_repo, "feature")
    _git(git_repo, "checkout", "-q", "main")
    (git_repo / "feature.txt").write_text("same\n", encoding="utf-8")
    _commit(git_repo, "squash equivalent")

    assert tree_equivalent_to("feat/applied", "main", cwd=git_repo) is True


def test_tree_equivalent_to_rejects_unique_branch(git_repo: Path) -> None:
    _git(git_repo, "checkout", "-q", "-b", "feat/unique")
    (git_repo / "feature.txt").write_text("unique\n", encoding="utf-8")
    _commit(git_repo, "feature")
    _git(git_repo, "checkout", "-q", "main")

    assert tree_equivalent_to("feat/unique", "main", cwd=git_repo) is False


def test_list_worktrees_parses_main_secondary_and_locked(git_repo: Path, tmp_path: Path) -> None:
    _git(git_repo, "branch", "feat/worktree")
    worktree_path = tmp_path / "secondary"
    _git(git_repo, "worktree", "add", "-q", str(worktree_path), "feat/worktree")
    _git(git_repo, "worktree", "lock", "--reason", "keep for test", str(worktree_path))

    worktrees = list_worktrees(cwd=git_repo)

    by_path = {w.path: w for w in worktrees}
    assert git_repo in by_path
    assert by_path[git_repo].branch == "main"
    assert worktree_path in by_path
    assert by_path[worktree_path].branch == "feat/worktree"
    assert by_path[worktree_path].locked is True
    assert by_path[worktree_path].lock_reason == "keep for test"


def test_scan_git_state_respects_worktree_age_threshold(git_repo: Path, tmp_path: Path) -> None:
    now = datetime.now(UTC)
    _git(git_repo, "checkout", "-q", "-b", "feat/recent")
    (git_repo / "recent.txt").write_text("recent\n", encoding="utf-8")
    _commit(git_repo, "recent", when=now - timedelta(days=5))
    _git(git_repo, "checkout", "-q", "main")
    recent_path = tmp_path / "recent"
    _git(git_repo, "worktree", "add", "-q", str(recent_path), "feat/recent")

    _git(git_repo, "checkout", "-q", "-b", "feat/old")
    (git_repo / "old.txt").write_text("old\n", encoding="utf-8")
    _commit(git_repo, "old", when=now - timedelta(days=10))
    _git(git_repo, "checkout", "-q", "main")
    old_path = tmp_path / "old"
    _git(git_repo, "worktree", "add", "-q", str(old_path), "feat/old")

    plan = scan_git_state(cwd=git_repo, keep_worktree_days=7)

    assert str(old_path.resolve()) in {
        str(worktree.path) for worktree in plan.abandoned_worktrees
    }
    assert any(
        item.kind == "worktree"
        and item.name == str(recent_path.resolve())
        and item.reason == "recent or unique work"
        for item in plan.protected
    )


def test_scan_git_state_protects_default_branch_worktree(
    git_repo: Path, tmp_path: Path
) -> None:
    _git(git_repo, "checkout", "-q", "-b", "feat/current")
    default_path = tmp_path / "main-worktree"
    _git(git_repo, "worktree", "add", "-q", str(default_path), "main")

    plan = scan_git_state(cwd=git_repo)

    assert not plan.abandoned_worktrees
    assert any(
        item.kind == "worktree"
        and item.name == str(default_path.resolve())
        and item.reason == "default branch"
        for item in plan.protected
    )


def test_scan_git_state_protects_prunable_missing_worktree(
    git_repo: Path, tmp_path: Path
) -> None:
    _git(git_repo, "branch", "feat/missing-worktree")
    missing_path = tmp_path / "missing-worktree"
    _git(git_repo, "worktree", "add", "-q", str(missing_path), "feat/missing-worktree")
    shutil.rmtree(missing_path)

    plan = scan_git_state(cwd=git_repo)

    assert not plan.abandoned_worktrees
    assert any(
        item.kind == "worktree"
        and item.name == str(missing_path.resolve())
        and item.reason == "missing path; run git worktree prune"
        for item in plan.protected
    )


def test_git_start_failure_is_converted_to_git_state_error(mocker) -> None:
    mocker.patch.object(
        git_state_mod.subprocess,
        "run",
        side_effect=FileNotFoundError("git missing"),
    )

    with pytest.raises(GitStateError, match="failed to start"):
        scan_git_state()


def test_git_timeout_is_converted_to_git_state_error(mocker) -> None:
    mocker.patch.object(
        git_state_mod.subprocess,
        "run",
        side_effect=subprocess.TimeoutExpired(["git"], 10),
    )

    with pytest.raises(GitStateError, match="timed out after 10s"):
        scan_git_state()
