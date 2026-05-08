from __future__ import annotations

from click.testing import CliRunner

from conductor import agent_wiring as aw
from conductor import cli as cli_mod
from conductor.cli import main


def _isolate_user_scope(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / ".conductor"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / ".claude"))
    monkeypatch.delenv("CONDUCTOR_NO_AUTO_REFRESH", raising=False)


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


def test_auto_refresh_env_opt_out_leaves_stale_files(tmp_path, monkeypatch):
    _isolate_user_scope(tmp_path, monkeypatch)
    monkeypatch.setenv("CONDUCTOR_NO_AUTO_REFRESH", "1")
    monkeypatch.setattr(cli_mod, "__version__", "0.9.0")
    aw.wire_claude_code("0.8.0", patch_claude_md=True)

    result = CliRunner().invoke(main, ["doctor", "--json"])

    assert result.exit_code == 0, result.output
    assert "refreshed user-scope" not in result.stderr
    assert aw.is_user_scope_stale(binary_version="0.9.0") is True


def test_auto_refresh_skips_read_only_commands(monkeypatch):
    monkeypatch.delenv("CONDUCTOR_NO_AUTO_REFRESH", raising=False)

    def fail_scan(*, binary_version: str):
        raise AssertionError(f"unexpected auto-refresh scan for {binary_version}")

    monkeypatch.setattr(aw, "user_scope_version_decisions", fail_scan)

    for args in (["list"], ["--help"], ["--version"]):
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
