from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "branch-guard.sh"

pytestmark = [
    pytest.mark.skipif(shutil.which("git") is None, reason="git not installed"),
    pytest.mark.skipif(shutil.which("jq") is None, reason="jq not installed"),
]


def _git(repo: Path, *args: str) -> None:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True, text=True)


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")


def _run_hook(command: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "cwd": str(cwd),
    }
    return subprocess.run(
        ["bash", str(SCRIPT)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=False,
    )


def test_blocks_git_commit_on_main_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    result = _run_hook('git commit -m "test"', repo)

    assert result.returncode == 2
    assert "Blocked by Touchstone branch-guard" in result.stderr
    assert "on 'main'" in result.stderr


def test_allows_git_commit_on_feature_branch(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _git(repo, "checkout", "-q", "-b", "feat/test")

    result = _run_hook('git commit -m "test"', repo)

    assert result.returncode == 0
    assert result.stderr == ""


def test_allows_git_commit_with_dash_capital_c_feature_worktree(tmp_path: Path) -> None:
    parent = tmp_path / "repo"
    worktree = tmp_path / "repo-feat"
    parent.mkdir()
    _init_repo(parent)
    _git(parent, "branch", "feat/test")
    _git(parent, "worktree", "add", str(worktree), "feat/test")

    result = _run_hook(f'git -C {worktree} commit -m "test"', parent)

    assert result.returncode == 0
    assert result.stderr == ""


def test_lowercase_dash_c_does_not_override_cwd(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    result = _run_hook('git -c core.editor=foo commit -m "test"', repo)

    assert result.returncode == 2
    assert "Blocked by Touchstone branch-guard" in result.stderr
    assert "on 'main'" in result.stderr
