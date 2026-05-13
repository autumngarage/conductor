from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from conductor import agent_wiring
from conductor import cli as cli_mod
from conductor.cli import main


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


def test_update_refreshes_stale_embedded_file_and_stages(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")

    result = CliRunner().invoke(main, ["update"])

    assert result.exit_code == 0, result.output
    assert "Refreshed Conductor repo integrations:" in result.output
    assert "AGENTS.md" in result.output
    assert "<!-- conductor:begin v0.9.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert _staged_paths(repo) == ["AGENTS.md"]


def test_update_dry_run_reports_stale_without_writing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")

    result = CliRunner().invoke(main, ["update", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Would refresh Conductor repo integrations:" in result.output
    assert "AGENTS.md (v0.8.9 -> v0.9.0)" in result.output
    assert "<!-- conductor:begin v0.8.9 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert _staged_paths(repo) == []


def test_update_check_exits_one_for_stale_without_writing(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")

    result = CliRunner().invoke(main, ["update", "--check"])

    assert result.exit_code == 1, result.output
    assert "Conductor repo integrations are stale:" in result.output
    assert "Run `conductor update` to refresh them." in result.output
    assert "<!-- conductor:begin v0.8.9 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert _staged_paths(repo) == []


def test_update_check_json_reports_exact_stale_repo_integrations(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")

    result = CliRunner().invoke(main, ["update", "--check", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["current"] is False
    assert payload["stale"] is True
    assert payload["update_command"] == "conductor update"
    integrations = {entry["kind"]: entry for entry in payload["integrations"]}
    assert integrations["agents-md-import"]["status"] == "stale"
    assert integrations["agents-md-import"]["installed_version"] == "0.8.9"
    assert integrations["agents-md-import"]["expected_version"] == "0.9.0"
    assert integrations["agents-md-import"]["update_command"] == "conductor update"
    assert _staged_paths(repo) == []


def test_update_check_json_treats_repo_claude_import_mode_as_current(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_claude_md_repo(cwd=repo, version="0.8.9")

    result = CliRunner().invoke(main, ["update", "--check", "--json"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["current"] is True
    assert payload["stale"] is False
    integrations = {entry["kind"]: entry for entry in payload["integrations"]}
    assert integrations["claude-md-repo-import"]["status"] == "import-mode"
    assert integrations["claude-md-repo-import"]["installed_version"] == "0.8.9"
    assert integrations["claude-md-repo-import"]["stale"] is False
    assert integrations["claude-md-repo-import"]["update_command"] is None
    assert _staged_paths(repo) == []


def test_update_check_json_reports_unreadable_repo_integration(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == repo / "AGENTS.md":
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    result = CliRunner().invoke(main, ["update", "--check", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["current"] is False
    assert payload["needs_attention"] is True
    integrations = {entry["kind"]: entry for entry in payload["integrations"]}
    assert integrations["agents-md-import"]["stale"] is True
    assert integrations["agents-md-import"]["requires_attention"] is True
    assert integrations["agents-md-import"]["status"].startswith("read-error:")
    assert (
        integrations["agents-md-import"]["update_command"]
        == "fix file permissions, then conductor update"
    )


def test_update_plain_reports_unreadable_repo_integration(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.8.9")
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == repo / "AGENTS.md":
            raise OSError("permission denied")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)

    result = CliRunner().invoke(main, ["update"])

    assert result.exit_code == 1, result.output
    assert "need manual repair" in result.output
    assert "permission denied" in result.output
    assert "Conductor repo integrations are current." not in result.output


def test_update_check_json_reports_malformed_sentinel_as_manual_repair(
    tmp_path, monkeypatch
):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    (repo / "AGENTS.md").write_text(
        "<!-- conductor:begin v0.8.9 -->\nmissing end marker\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["update", "--check", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["current"] is False
    assert payload["stale"] is False
    assert payload["needs_attention"] is True
    integrations = {entry["kind"]: entry for entry in payload["integrations"]}
    assert integrations["agents-md-import"]["status"] == "malformed sentinel"
    assert integrations["agents-md-import"]["stale"] is False
    assert integrations["agents-md-import"]["requires_attention"] is True
    assert (
        integrations["agents-md-import"]["update_command"]
        == "repair malformed sentinel, then conductor init -y"
    )


def test_update_json_reports_malformed_sentinel_as_json(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    (repo / "AGENTS.md").write_text(
        "<!-- conductor:begin v0.8.9 -->\nmissing end marker\n",
        encoding="utf-8",
    )

    result = CliRunner().invoke(main, ["update", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["current"] is False
    assert payload["needs_attention"] is True
    assert "Conductor repo integrations need manual repair" not in result.output


def test_update_check_exits_zero_when_current(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    agent_wiring.wire_agents_md(cwd=repo, version="0.9.0")

    result = CliRunner().invoke(main, ["update", "--check"])

    assert result.exit_code == 0, result.output
    assert "Conductor repo integrations are current." in result.output
    assert _staged_paths(repo) == []


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
