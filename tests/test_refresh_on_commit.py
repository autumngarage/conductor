from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

import pytest
from click.testing import CliRunner

from conductor import agent_wiring
from conductor import cli as cli_mod
from conductor.cli import main

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _isolated_agent_homes(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / ".conductor"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / ".claude"))
    monkeypatch.setattr("shutil.which", lambda _cmd: None)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _init_git_repo(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "main")


def _staged_paths(repo: Path) -> list[str]:
    output = _git(repo, "diff", "--cached", "--name-only")
    return [line for line in output.splitlines() if line]


def test_refresh_on_commit_clean_state_exits_zero_without_modifying(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.9.0")

    before = (repo / "AGENTS.md").read_text(encoding="utf-8")
    result = CliRunner().invoke(main, ["refresh-on-commit"])

    assert result.exit_code == 0, result.output
    assert (repo / "AGENTS.md").read_text(encoding="utf-8") == before
    assert _staged_paths(repo) == []


def test_refresh_on_commit_refreshes_stale_embedded_file_and_stages(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")

    result = CliRunner().invoke(main, ["refresh-on-commit"])

    assert result.exit_code == 0, result.output
    text = (repo / "AGENTS.md").read_text(encoding="utf-8")
    assert "<!-- conductor:begin v0.9.0 -->" in text
    assert _staged_paths(repo) == ["AGENTS.md"]


def test_refresh_on_commit_mixed_embedded_and_import_refreshes_only_embedded(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")
    repo_claude = repo / "CLAUDE.md"
    agent_wiring.inject_sentinel_block(repo_claude, "@.conductor/CLAUDE.md", version="0.8.9")

    result = CliRunner().invoke(main, ["refresh-on-commit"])

    assert result.exit_code == 0, result.output
    assert "<!-- conductor:begin v0.9.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    claude_text = repo_claude.read_text(encoding="utf-8")
    assert "<!-- conductor:begin v0.8.9 -->" in claude_text
    assert "@.conductor/CLAUDE.md" in claude_text
    assert _staged_paths(repo) == ["AGENTS.md"]


def test_init_hooks_creates_pre_commit_config_by_default(tmp_path):
    repo = tmp_path / "repo"

    result = CliRunner().invoke(main, ["init", "--yes"])

    assert result.exit_code == 0, result.output
    config = repo / ".pre-commit-config.yaml"
    text = config.read_text(encoding="utf-8")
    assert text.startswith("repos:\n")
    assert "id: conductor-refresh" in text
    assert "entry: conductor refresh-on-commit" in text
    assert "always_run: true" in text
    assert "Installed conductor-refresh pre-commit hook" in result.output


def test_init_no_hooks_skips_pre_commit_config(tmp_path):
    repo = tmp_path / "repo"

    result = CliRunner().invoke(main, ["init", "--yes", "--no-hooks"])

    assert result.exit_code == 0, result.output
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_init_accept_defaults_no_hooks_skips_prompts_and_hooks(tmp_path):
    repo = tmp_path / "repo"

    result = CliRunner().invoke(main, ["init", "-y", "--no-hooks"])

    assert result.exit_code == 0, result.output
    assert "Proceed?" not in result.output
    assert not (repo / ".pre-commit-config.yaml").exists()


def test_init_help_documents_hooks_default():
    result = CliRunner().invoke(main, ["init", "--help"])

    assert result.exit_code == 0, result.output
    assert "--hooks / --no-hooks" in result.output
    assert "default: yes; pass --no-hooks to skip" in result.output


def test_init_hooks_merges_existing_config_without_duplication(tmp_path):
    repo = tmp_path / "repo"
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        """repos:
- repo: https://github.com/astral-sh/ruff-pre-commit
  rev: v0.6.9
  hooks:
    - id: ruff
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["init", "--yes"])

    assert result.exit_code == 0, result.output
    text = config.read_text(encoding="utf-8")
    assert "id: ruff" in text
    assert text.count("id: conductor-refresh") == 1
    assert text.count("repo: local") == 1


def test_init_hooks_inserts_inside_repos_before_later_top_level_keys(tmp_path):
    repo = tmp_path / "repo"
    config = repo / ".pre-commit-config.yaml"
    config.write_text(
        """repos:
  - repo: https://github.com/astral-sh/ruff-pre-commit
    rev: v0.6.9
    hooks:
      - id: ruff
default_language_version:
  python: python3
""",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["init", "--yes"])

    assert result.exit_code == 0, result.output
    text = config.read_text(encoding="utf-8")
    assert text.index("id: conductor-refresh") < text.index("default_language_version:")
    assert "  - repo: local\n    hooks:" in text


def test_init_hooks_is_idempotent(tmp_path):
    repo = tmp_path / "repo"

    first = CliRunner().invoke(main, ["init", "--yes"])
    second = CliRunner().invoke(main, ["init", "--yes"])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    text = (repo / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert text.count("id: conductor-refresh") == 1
