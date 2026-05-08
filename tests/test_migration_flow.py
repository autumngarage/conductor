from __future__ import annotations

import json
import subprocess
from typing import TYPE_CHECKING

from click.testing import CliRunner, Result

import conductor
from conductor import agent_wiring
from conductor import cli as cli_mod
from conductor.cli import main

if TYPE_CHECKING:
    from pathlib import Path


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _invoke(args: list[str]) -> Result:
    result = CliRunner().invoke(main, args)
    assert result.exit_code == 0, result.output or result.stderr
    return result


def _set_version(monkeypatch, version: str) -> None:
    monkeypatch.setattr(conductor, "__version__", version)
    monkeypatch.setattr(cli_mod, "__version__", version)


def _stub_all_providers_unconfigured(monkeypatch) -> None:
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
        monkeypatch.setattr(cls, "configured", lambda self: (False, "stubbed"))


def _versions_by_kind(repo: Path) -> dict[str, str | None]:
    return {
        artifact.kind: artifact.version
        for artifact in agent_wiring.detect(cwd=repo).managed
    }


def _assert_repo_versions(repo: Path, expected: str) -> None:
    versions = _versions_by_kind(repo)
    assert versions["agents-md-import"] == expected
    assert versions["gemini-md-import"] == expected
    assert versions["claude-md-repo-import"] == expected
    assert versions["cursor-rule"] == expected


def _assert_user_versions(expected: str) -> None:
    user_artifacts = [
        artifact
        for artifact in agent_wiring.detect().managed
        if artifact.kind not in cli_mod.REPO_INTEGRATION_KINDS
    ]
    versions_by_path = {artifact.path.name: artifact.version for artifact in user_artifacts}
    assert versions_by_path["delegation-guidance.md"] == expected
    assert versions_by_path["conductor.md"] == expected
    assert versions_by_path["CLAUDE.md"] == expected
    assert versions_by_path["kimi-long-context.md"] == expected
    assert versions_by_path["gemini-web-search.md"] == expected
    assert versions_by_path["codex-coding-agent.md"] == expected
    assert versions_by_path["ollama-offline.md"] == expected
    assert versions_by_path["conductor-auto.md"] == expected


def _staged_paths(repo: Path) -> list[str]:
    output = _git(repo, "diff", "--cached", "--name-only")
    return [line for line in output.splitlines() if line]


def test_brew_upgrade_then_consumer_commit_refresh(tmp_path, monkeypatch):
    """End-to-end upgrade UX: post-install refreshes user scope; the consumer
    repo hook refreshes and stages stale embedded repo integrations.
    """
    fake_home = tmp_path / "home"
    consumer = tmp_path / "consumer"
    fake_home.mkdir()
    consumer.mkdir()

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("CONDUCTOR_HOME", str(fake_home / ".conductor"))
    monkeypatch.setenv("CLAUDE_HOME", str(fake_home / ".claude"))
    monkeypatch.setenv(
        "CONDUCTOR_CREDENTIALS_FILE",
        str(fake_home / ".config" / "conductor" / "credentials.toml"),
    )
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    _stub_all_providers_unconfigured(monkeypatch)

    (fake_home / ".claude").mkdir()
    (consumer / ".cursor" / "rules").mkdir(parents=True)
    (consumer / "CLAUDE.md").write_text("# Consumer Claude rules\n", encoding="utf-8")
    (consumer / "GEMINI.md").write_text("# Consumer Gemini rules\n", encoding="utf-8")

    _git(consumer, "init", "-q", "-b", "main")
    _git(consumer, "config", "user.email", "test@example.com")
    _git(consumer, "config", "user.name", "Test User")

    monkeypatch.chdir(consumer)
    _set_version(monkeypatch, "0.10.0")
    _invoke(["init", "-y"])

    _assert_user_versions("0.10.0")
    _assert_repo_versions(consumer, "0.10.0")

    first_hooks = _invoke(["init", "-y"])
    second_hooks = _invoke(["init", "-y"])
    pre_commit_config = consumer / ".pre-commit-config.yaml"
    pre_commit_text = pre_commit_config.read_text(encoding="utf-8")
    assert "already present" in first_hooks.output
    assert "already present" in second_hooks.output
    assert pre_commit_text.count("id: conductor-refresh") == 1
    assert pre_commit_text.count("entry: conductor refresh-on-commit") == 1

    _git(consumer, "add", ".")
    _git(consumer, "commit", "-m", "baseline")

    _set_version(monkeypatch, "0.11.0")
    monkeypatch.chdir(fake_home)
    post_install = _invoke(["init", "-y", "--quiet", "--remaining"])

    assert post_install.output == ""
    assert post_install.stderr == ""
    _assert_user_versions("0.11.0")
    _assert_repo_versions(consumer, "0.10.0")

    monkeypatch.chdir(consumer)
    (consumer / "feature.txt").write_text("new consumer change\n", encoding="utf-8")
    _git(consumer, "add", "feature.txt")
    _invoke(["refresh-on-commit"])

    versions = _versions_by_kind(consumer)
    assert versions["agents-md-import"] == "0.11.0"
    assert versions["gemini-md-import"] == "0.11.0"
    assert versions["cursor-rule"] == "0.11.0"
    assert versions["claude-md-repo-import"] == "0.10.0"
    assert "@~/.conductor/delegation-guidance.md" in (
        consumer / "CLAUDE.md"
    ).read_text(encoding="utf-8")
    assert (fake_home / ".conductor" / "delegation-guidance.md").read_text(
        encoding="utf-8"
    ).startswith("<!-- managed-by: conductor v0.11.0")

    assert _staged_paths(consumer) == [
        ".cursor/rules/conductor-delegation.mdc",
        "AGENTS.md",
        "GEMINI.md",
        "feature.txt",
    ]

    doctor = _invoke(["doctor", "--json"])
    agent_integration = json.loads(doctor.output)["agent_integration"]
    assert agent_integration["user_version_skew_files"] == []
    assert set(agent_integration["repo_version_skew_files"]) == {
        str(consumer / "CLAUDE.md")
    }
