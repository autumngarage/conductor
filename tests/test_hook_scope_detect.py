from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from conductor import cli
from conductor.hook_scope_detect import (
    detect_hook_staged_scope_creep,
    git_head,
    git_head_via_fs,
    normalize_repo_relative_path,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(repo: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test User",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test User",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "base")


def _install_auto_stage_hook(repo: Path) -> None:
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text(
        "#!/bin/sh\n"
        "printf 'hook bump\\n' > AGENTS.md\n"
        "git add AGENTS.md\n",
        encoding="utf-8",
    )
    hook.chmod(hook.stat().st_mode | stat.S_IXUSR)


def test_emit_exec_warning_when_hook_expands_commit_scope(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _install_auto_stage_hook(repo)

    phase_start = git_head(repo)
    assert phase_start is not None

    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "feature")

    tool_log = repo / "tool-events.ndjson"
    tool_log.write_text(
        json.dumps(
            {
                "event": "tool_call",
                "data": {"name": "Write", "args": {"path": "src/app.py"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session_log = SimpleNamespace(log_path=tool_log)

    creep = detect_hook_staged_scope_creep(
        repo,
        {"src/app.py"},
        base_head=phase_start,
    )
    assert creep == [("AGENTS.md",)]

    cli._emit_exec_hook_scope_creep_warnings(
        cwd=str(repo),
        session_log=session_log,
        phase_start_head=phase_start,
    )

    stderr = capsys.readouterr().err
    assert "[conductor] commit grew by hook:" in stderr
    assert "AGENTS.md" in stderr
    assert "Agent intended: src/app.py" in stderr


def test_emit_exec_warning_quiet_when_commit_matches_intended(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    phase_start = git_head(repo)
    assert phase_start is not None

    (repo / "src").mkdir()
    (repo / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")
    _git(repo, "add", "src/app.py")
    _git(repo, "commit", "-q", "-m", "feature")

    tool_log = repo / "tool-events.ndjson"
    tool_log.write_text(
        json.dumps(
            {
                "event": "tool_call",
                "data": {"name": "Write", "args": {"path": "src/app.py"}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    session_log = SimpleNamespace(log_path=tool_log)

    cli._emit_exec_hook_scope_creep_warnings(
        cwd=str(repo),
        session_log=session_log,
        phase_start_head=phase_start,
    )

    stderr = capsys.readouterr().err
    assert "commit grew by hook" not in stderr


def test_normalize_preserves_dotfile_leading_dot() -> None:
    # Regression: `.lstrip("./")` would strip leading dots from `.env`. We need
    # to strip only `./` prefixes, not individual `.` characters.
    assert normalize_repo_relative_path(".env", worktree=Path("/tmp/x")) == ".env"
    assert (
        normalize_repo_relative_path("./src/.env", worktree=Path("/tmp/x"))
        == "src/.env"
    )
    assert normalize_repo_relative_path("./.env", worktree=Path("/tmp/x")) == ".env"


def test_git_head_via_fs_resolves_from_subdirectory(tmp_path: Path) -> None:
    # Regression: _resolve_git_dir() must walk parents so a subdirectory cwd
    # still finds the repo root's .git directory.
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    sub = repo / "src" / "deep"
    sub.mkdir(parents=True)

    head_from_root = git_head_via_fs(repo)
    head_from_sub = git_head_via_fs(sub)

    assert head_from_root is not None
    assert head_from_root == head_from_sub


def test_git_head_via_fs_resolves_in_linked_worktree(tmp_path: Path) -> None:
    # Regression: linked worktrees keep HEAD in worktree-local gitdir but
    # refs/heads/* lives in commondir (the main repo's .git). git_head_via_fs
    # must follow commondir to resolve the symbolic ref.
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _init_repo(main_repo)

    wt_path = tmp_path / "wt-branch"
    _git(main_repo, "worktree", "add", "-b", "feature", str(wt_path))

    head_from_main = git_head_via_fs(main_repo)
    head_from_wt = git_head_via_fs(wt_path)

    assert head_from_main is not None
    assert head_from_wt is not None
    # Both heads should resolve to a SHA (40-char hex) — same commit since
    # both branches point at the initial commit.
    assert len(head_from_main) == 40
    assert len(head_from_wt) == 40
    assert head_from_main == head_from_wt
