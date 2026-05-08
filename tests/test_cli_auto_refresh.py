from __future__ import annotations

import subprocess

from click.testing import CliRunner

from conductor import agent_wiring as aw
from conductor import cli as cli_mod
from conductor.cli import main


def _isolate_user_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / ".conductor"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / ".claude"))
    monkeypatch.delenv("CONDUCTOR_NO_AUTO_REFRESH", raising=False)


def _init_git_repo(path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)


def _git(path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )


def _configure_git_identity(path) -> None:
    _git(path, "config", "user.email", "conductor-test@example.com")
    _git(path, "config", "user.name", "Conductor Test")


def test_auto_refresh_current_user_scope_is_silent(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_claude_code("0.9.0", patch_claude_md=True)

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "refreshed user-scope" not in result.stderr


def test_auto_refresh_stale_user_scope_updates_files(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_claude_code("0.8.0", patch_claude_md=True)

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "[conductor] refreshed user-scope integration files to v0.9.0" in result.stderr
    assert aw.is_user_scope_stale(binary_version="0.9.0") is False
    versions = {
        artifact.kind: artifact.version
        for artifact in aw.detect().managed
        if artifact.kind in {"guidance", "slash-command", "claude-md-import"}
    }
    assert versions == {
        "guidance": "0.9.0",
        "slash-command": "0.9.0",
        "claude-md-import": "0.9.0",
    }


def test_auto_refresh_stale_repo_scope_updates_files(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_agents_md(cwd=repo, version="0.8.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert (
        f"[conductor] refreshed repo-scope integration files in {repo} to v0.9.0"
        in result.stderr
    )
    assert "<!-- conductor:begin v0.9.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")


def test_auto_refresh_stale_repo_scope_on_default_branch_uses_refresh_branch(
    tmp_path, monkeypatch
):
    _isolate_user_scope(tmp_path, monkeypatch)
    repo = tmp_path / "repo-default-branch"
    repo.mkdir()
    _init_git_repo(repo)
    _configure_git_identity(repo)
    aw.wire_agents_md(cwd=repo, version="0.8.0")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-m", "Initial conductor wiring")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    branch = "chore/conductor-refresh-v0.9.0"
    assert (
        "[conductor] auto-refreshed repo-scope integration files "
        f"on {branch} (no changes left on main)"
    ) in result.stderr
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(repo, "status", "--porcelain").stdout.strip() == ""
    assert "<!-- conductor:begin v0.8.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    assert "<!-- conductor:begin v0.9.0 -->" in _git(
        repo,
        "show",
        f"{branch}:AGENTS.md",
    ).stdout


def test_auto_refresh_default_branch_restores_operator_changes(
    tmp_path, monkeypatch
):
    _isolate_user_scope(tmp_path, monkeypatch)
    repo = tmp_path / "repo-dirty-default"
    repo.mkdir()
    _init_git_repo(repo)
    _configure_git_identity(repo)
    aw.wire_agents_md(cwd=repo, version="0.8.0")
    (repo / "operator.txt").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "AGENTS.md", "operator.txt")
    _git(repo, "commit", "-m", "Initial state")
    (repo / "operator.txt").write_text("operator edits\n", encoding="utf-8")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    branch = "chore/conductor-refresh-v0.9.0"
    assert (
        "[conductor] auto-refreshed repo-scope integration files "
        f"on {branch} (operator changes restored on main)"
    ) in result.stderr
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert (repo / "operator.txt").read_text(encoding="utf-8") == "operator edits\n"
    assert _git(repo, "status", "--porcelain").stdout.strip() == "M operator.txt"
    assert "<!-- conductor:begin v0.9.0 -->" in _git(
        repo,
        "show",
        f"{branch}:AGENTS.md",
    ).stdout


def test_auto_refresh_via_pr_never_keeps_in_place_behavior(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_AUTO_REFRESH_VIA_PR", "never")
    repo = tmp_path / "repo-never"
    repo.mkdir()
    _init_git_repo(repo)
    _configure_git_identity(repo)
    aw.wire_agents_md(cwd=repo, version="0.8.0")
    _git(repo, "add", "AGENTS.md")
    _git(repo, "commit", "-m", "Initial conductor wiring")
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "[conductor] refreshed repo-scope integration files" in result.stderr
    assert _git(repo, "branch", "--show-current").stdout.strip() == "main"
    assert _git(repo, "status", "--porcelain").stdout.strip() == "M AGENTS.md"
    assert "<!-- conductor:begin v0.9.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")


def test_auto_refresh_clean_repo_scope_is_silent(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_agents_md(cwd=repo, version="0.9.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "refreshed repo-scope" not in result.stderr


def test_auto_refresh_repo_scope_skips_import_mode_claude_md(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_agents_md(cwd=repo, version="0.8.0")
    aw.wire_claude_md_repo(cwd=repo, version="0.8.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "<!-- conductor:begin v0.9.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")
    claude_text = (repo / "CLAUDE.md").read_text(encoding="utf-8")
    assert "<!-- conductor:begin v0.8.0 -->" in claude_text
    assert "@~/.conductor/delegation-guidance.md" in claude_text


def test_auto_refresh_repo_scope_non_git_noops(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_agents_md(cwd=repo, version="0.8.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "refreshed repo-scope" not in result.stderr
    assert "<!-- conductor:begin v0.8.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")


def test_auto_refresh_repo_scope_non_wired_noops(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "refreshed repo-scope" not in result.stderr


def test_auto_refresh_env_opt_out_leaves_stale_files(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_NO_AUTO_REFRESH", "1")
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_claude_code("0.8.0", patch_claude_md=True)
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo(repo)
    monkeypatch.chdir(repo)
    aw.wire_agents_md(cwd=repo, version="0.8.0")

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "refreshed user-scope" not in result.stderr
    assert "refreshed repo-scope" not in result.stderr
    assert aw.is_user_scope_stale(binary_version="0.9.0") is True
    assert "<!-- conductor:begin v0.8.0 -->" in (
        repo / "AGENTS.md"
    ).read_text(encoding="utf-8")


def test_auto_refresh_skips_read_only_commands(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_NO_AUTO_REFRESH", raising=False)

    def fail_scan(*, binary_version: str):
        raise AssertionError(f"unexpected auto-refresh scan for {binary_version}")

    monkeypatch.setattr(aw, "user_scope_version_decisions", fail_scan)
    monkeypatch.setattr(aw, "repo_scope_version_decisions", fail_scan)

    for args in (["list"], ["--help"], ["--version"], ["init", "--help"]):
        result = CliRunner().invoke(main, args)
        assert result.exit_code == 0, result.output


def test_auto_refresh_failure_logs_and_continues(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_claude_code("0.8.0", patch_claude_md=True)

    def fail_wire(version: str, *, patch_claude_md: bool):
        raise PermissionError("permission denied")

    monkeypatch.setattr(aw, "wire_claude_code", fail_wire)

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "auto-refresh warning: failed to refresh user-scope integration files" in result.stderr
    assert "permission denied" in result.stderr
