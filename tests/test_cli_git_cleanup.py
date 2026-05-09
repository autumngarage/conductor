from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from conductor import cli
from conductor.cli import main

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
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "conductor-test@example.com")
    _git(root, "config", "user.name", "Conductor Test")
    _git(root, "config", "commit.gpgsign", "false")
    (root / "README.md").write_text("base\n", encoding="utf-8")
    _commit(root, "base")
    monkeypatch.chdir(root)
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / ".conductor"))
    monkeypatch.setenv(
        "CONDUCTOR_CREDENTIALS_FILE", str(tmp_path / ".config" / "credentials.toml")
    )
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / ".claude"))
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    for var in (
        "OPENROUTER_API_KEY",
        "CONDUCTOR_OLLAMA_MODEL",
        "OLLAMA_BASE_URL",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "DEEPSEEK_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return root


def _make_squash_merged_branch(repo: Path, name: str, filename: str) -> None:
    _git(repo, "checkout", "-q", "-b", name)
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    _commit(repo, name)
    _git(repo, "checkout", "-q", "main")
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    _commit(repo, f"squash {name}")


def _make_unique_branch(repo: Path, name: str, filename: str, *, days_old: int = 0) -> None:
    when = datetime.now(UTC) - timedelta(days=days_old)
    _git(repo, "checkout", "-q", "-b", name)
    (repo / filename).write_text(f"{name}\n", encoding="utf-8")
    _commit(repo, name, when=when)
    _git(repo, "checkout", "-q", "main")


def test_git_cleanup_dry_run_shape_does_not_delete(repo: Path) -> None:
    _make_squash_merged_branch(repo, "feat/stale", "stale.txt")

    result = CliRunner().invoke(main, ["git-cleanup"])

    assert result.exit_code == 0, result.output
    assert "Stale branches (1):" in result.output
    assert "feat/stale" in result.output
    assert "Run with --execute to actually delete." in result.output
    branches = _git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert "feat/stale" in branches


def test_git_cleanup_execute_deletes_only_cleanup_candidates(repo: Path, tmp_path: Path) -> None:
    _make_squash_merged_branch(repo, "feat/stale", "stale.txt")
    _make_unique_branch(repo, "feat/old-worktree", "old.txt", days_old=10)
    old_path = tmp_path / "old-worktree"
    _git(repo, "worktree", "add", "-q", str(old_path), "feat/old-worktree")
    _make_unique_branch(repo, "feat/keep", "keep.txt")

    result = CliRunner().invoke(main, ["git-cleanup", "--execute"])

    assert result.exit_code == 0, result.output
    branches = _git(repo, "branch", "--format=%(refname:short)").splitlines()
    assert "feat/stale" not in branches
    assert "feat/keep" in branches
    assert not old_path.exists()


def test_git_cleanup_dirty_worktree_is_protected(repo: Path, tmp_path: Path) -> None:
    _make_unique_branch(repo, "feat/dirty", "dirty.txt", days_old=10)
    dirty_path = tmp_path / "dirty"
    _git(repo, "worktree", "add", "-q", str(dirty_path), "feat/dirty")
    (dirty_path / "dirty.txt").write_text("changed\n", encoding="utf-8")

    result = CliRunner().invoke(main, ["git-cleanup", "--execute"])

    assert result.exit_code == 0, result.output
    assert dirty_path.exists()
    assert "uncommitted changes" in result.output


def test_git_cleanup_current_branch_and_worktree_are_protected(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "feat/current")
    (repo / "current.txt").write_text("current\n", encoding="utf-8")
    _commit(repo, "current")

    result = CliRunner().invoke(main, ["git-cleanup", "--execute"])

    assert result.exit_code == 0, result.output
    assert "feat/current (current checkout)" in result.output
    assert f"{repo} (current checkout)" in result.output
    assert _git(repo, "branch", "--show-current") == "feat/current"


def test_git_cleanup_threshold_respected(repo: Path, tmp_path: Path) -> None:
    _make_unique_branch(repo, "feat/recent", "recent.txt", days_old=5)
    recent_path = tmp_path / "recent"
    _git(repo, "worktree", "add", "-q", str(recent_path), "feat/recent")
    _make_unique_branch(repo, "feat/old", "old.txt", days_old=10)
    old_path = tmp_path / "old"
    _git(repo, "worktree", "add", "-q", str(old_path), "feat/old")

    result = CliRunner().invoke(main, ["git-cleanup", "--execute"])

    assert result.exit_code == 0, result.output
    assert recent_path.exists()
    assert not old_path.exists()


def test_git_cleanup_json_shape(repo: Path) -> None:
    _make_squash_merged_branch(repo, "feat/stale", "stale.txt")

    result = CliRunner().invoke(main, ["git-cleanup", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["dry_run"] is True
    assert payload["stale_branches"][0]["name"] == "feat/stale"
    assert payload["abandoned_worktrees"] == []
    assert payload["protected"]


def test_git_cleanup_command_timeout_returns_failure(mocker) -> None:
    mocker.patch(
        "conductor.cli.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["git"], 10),
    )

    ok, detail = cli._run_git_cleanup_command(["branch", "-D", "feat/stale"])

    assert ok is False
    assert "timed out after 10s" in detail


def _stub_all_unconfigured(mocker) -> None:
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        DeepSeekChatProvider,
        DeepSeekReasonerProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
        OpenRouterProvider,
    )

    for cls in (
        ClaudeProvider,
        CodexProvider,
        DeepSeekChatProvider,
        DeepSeekReasonerProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
        OpenRouterProvider,
    ):
        mocker.patch.object(
            cls,
            "configured",
            lambda self, _cls=cls: (False, f"stub: {_cls.__name__} unset"),
        )
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)


def test_doctor_clean_state_has_no_local_git_warning(repo: Path, mocker) -> None:
    _stub_all_unconfigured(mocker)

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Local git state has drift" not in result.output


def test_doctor_warns_for_stale_branches_only(repo: Path, mocker) -> None:
    _stub_all_unconfigured(mocker)
    _make_squash_merged_branch(repo, "feat/stale", "stale.txt")

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "⚠ Local git state has drift:" in result.output
    assert "Stale branches (1):" in result.output
    assert "feat/stale" in result.output
    assert "Abandoned worktrees (0):" in result.output
    assert "conductor git-cleanup --execute # actually delete" in result.output


def test_doctor_warns_for_abandoned_worktrees_only(
    repo: Path, tmp_path: Path, mocker
) -> None:
    _stub_all_unconfigured(mocker)
    _make_unique_branch(repo, "feat/old", "old.txt", days_old=10)
    old_path = tmp_path / "old"
    _git(repo, "worktree", "add", "-q", str(old_path), "feat/old")

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Stale branches (0):" in result.output
    assert "Abandoned worktrees (1):" in result.output
    assert str(old_path) in result.output


def test_doctor_warns_for_both_git_state_drifts(
    repo: Path, tmp_path: Path, mocker
) -> None:
    _stub_all_unconfigured(mocker)
    _make_squash_merged_branch(repo, "feat/stale", "stale.txt")
    _make_unique_branch(repo, "feat/old", "old.txt", days_old=10)
    old_path = tmp_path / "old"
    _git(repo, "worktree", "add", "-q", str(old_path), "feat/old")

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Stale branches (1):" in result.output
    assert "Abandoned worktrees (1):" in result.output


def test_doctor_json_includes_git_state(repo: Path, mocker) -> None:
    _stub_all_unconfigured(mocker)
    _make_squash_merged_branch(repo, "feat/stale", "stale.txt")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["git_state"]["stale_branches"][0]["name"] == "feat/stale"
    assert payload["git_state"]["abandoned_worktrees"] == []
    assert payload["git_state"]["branch_scan"]["limit"] == 50


def test_doctor_text_surfaces_git_state_scan_error(repo: Path, mocker) -> None:
    _stub_all_unconfigured(mocker)
    mocker.patch(
        "conductor.cli._git_state_doctor_payload",
        return_value={
            "stale_branches": [],
            "abandoned_worktrees": [],
            "branch_scan": {
                "checked": 0,
                "total": 0,
                "limit": 50,
                "capped": False,
            },
            "error": "`git rev-parse --show-toplevel` failed",
        },
    )

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "⚠ Local git state could not be checked:" in result.output
    assert "`git rev-parse --show-toplevel` failed" in result.output
