"""CLI tests for list, smoke, doctor commands."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from conductor.cli import main


@pytest.fixture(autouse=True)
def _isolated_agent_homes(tmp_path, monkeypatch):
    """doctor now reports agent-integration state; isolate so tests don't
    depend on the developer's real ~/.claude/, ~/.conductor/, or current
    working directory's AGENTS.md."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / ".conductor"))
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / ".claude"))
    monkeypatch.chdir(repo_dir)
    monkeypatch.setattr("shutil.which", lambda _cmd: None)


def _stub_all_unconfigured(mocker):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    ):
        mocker.patch.object(
            cls,
            "configured",
            # Default-arg binds cls into the closure per iteration (fixes B023).
            lambda self, _cls=cls: (False, f"stub: {_cls.__name__} unset"),
        )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_text_output_shows_all_five_providers(mocker):
    _stub_all_unconfigured(mocker)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    for name in ("kimi", "claude", "codex", "gemini", "ollama"):
        assert name in result.output


def test_list_json_output_returns_structured_rows(mocker):
    _stub_all_unconfigured(mocker)
    result = CliRunner().invoke(main, ["list", "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.output)
    assert len(rows) == 5
    assert {r["provider"] for r in rows} == {
        "kimi",
        "claude",
        "codex",
        "gemini",
        "ollama",
    }
    assert all(r["configured"] is False for r in rows)
    assert all(r["default_model"] for r in rows)


# ---------------------------------------------------------------------------
# smoke
# ---------------------------------------------------------------------------


def test_smoke_unknown_provider_errors():
    result = CliRunner().invoke(main, ["smoke", "not-a-provider"])
    assert result.exit_code != 0
    assert "unknown provider" in result.output.lower()


def test_smoke_with_id_and_all_mutually_exclusive():
    result = CliRunner().invoke(main, ["smoke", "kimi", "--all"])
    assert result.exit_code != 0
    assert "not both" in result.output.lower()


def test_smoke_requires_target():
    result = CliRunner().invoke(main, ["smoke"])
    assert result.exit_code != 0
    assert "--all" in result.output or "provider id" in result.output.lower()


def test_smoke_specific_provider_passes(mocker):
    from conductor.providers import KimiProvider

    mocker.patch.object(KimiProvider, "smoke", return_value=(True, None))
    result = CliRunner().invoke(main, ["smoke", "kimi"])
    assert result.exit_code == 0
    assert "kimi" in result.output
    assert "✓" in result.output


def test_smoke_specific_provider_fails_exits_1(mocker):
    from conductor.providers import KimiProvider

    mocker.patch.object(KimiProvider, "smoke", return_value=(False, "bad token"))
    result = CliRunner().invoke(main, ["smoke", "kimi"])
    assert result.exit_code == 1
    assert "bad token" in result.output


def test_smoke_all_only_hits_configured_providers(mocker):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    # Only claude is configured.
    for cls in (CodexProvider, GeminiProvider, KimiProvider, OllamaProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "no"))
    mocker.patch.object(ClaudeProvider, "configured", lambda self: (True, None))
    claude_smoke = mocker.patch.object(
        ClaudeProvider, "smoke", return_value=(True, None)
    )

    result = CliRunner().invoke(main, ["smoke", "--all"])
    assert result.exit_code == 0
    assert claude_smoke.called


def test_smoke_json_output_shape(mocker):
    from conductor.providers import KimiProvider

    mocker.patch.object(KimiProvider, "smoke", return_value=(True, None))
    result = CliRunner().invoke(main, ["smoke", "kimi", "--json"])
    assert result.exit_code == 0
    results = json.loads(result.output)
    assert results == [{"provider": "kimi", "ok": True, "reason": None}]


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


def test_doctor_text_output_covers_every_provider(mocker, monkeypatch):
    _stub_all_unconfigured(mocker)
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    for name in ("kimi", "claude", "codex", "gemini", "ollama"):
        assert name in result.output
    assert "Credentials" in result.output or "credentials" in result.output.lower()
    assert "conductor init" in result.output


def test_doctor_json_shape(mocker, monkeypatch):
    _stub_all_unconfigured(mocker)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "x")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload.keys()) >= {
        "version", "platform", "python", "providers", "credentials", "warnings"
    }
    assert len(payload["providers"]) == 5
    cred_map = {c["name"]: c for c in payload["credentials"]}
    assert cred_map["CLOUDFLARE_API_TOKEN"]["in_env"] is True
    assert cred_map["CLOUDFLARE_ACCOUNT_ID"]["in_env"] is False


def test_doctor_reports_agent_integration_not_detected(mocker, monkeypatch):
    """With no ~/.claude/ and no claude CLI, doctor reports 'not detected'."""
    _stub_all_unconfigured(mocker)
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Agent integration:" in result.output
    assert "not detected" in result.output.lower()


def test_doctor_reports_agent_integration_detected_not_wired(mocker, monkeypatch, tmp_path):
    """~/.claude/ present but no managed files → detected, not wired."""
    _stub_all_unconfigured(mocker)
    (tmp_path / ".claude").mkdir()
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "not wired" in result.output.lower()
    assert "conductor init" in result.output


def test_doctor_reports_agent_integration_wired(mocker, monkeypatch, tmp_path):
    """After wiring, doctor reports managed files with their paths."""
    _stub_all_unconfigured(mocker)
    (tmp_path / ".claude").mkdir()
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    from conductor import agent_wiring
    agent_wiring.wire_claude_code("0.3.2", patch_claude_md=True)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "wired" in result.output.lower()
    assert "user-scope files" in result.output.lower()
    assert "guidance" in result.output.lower()
    assert "slash-command" in result.output.lower()
    assert "subagent" in result.output.lower()


def test_doctor_reports_agents_md_when_present(mocker, monkeypatch, tmp_path):
    _stub_all_unconfigured(mocker)
    agents_md = tmp_path / "repo" / "AGENTS.md"
    agents_md.write_text("# mine\n", encoding="utf-8")
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "AGENTS.md" in result.output
    assert "present but not wired" in result.output.lower()


def test_doctor_reports_gemini_md_states(mocker, monkeypatch, tmp_path):
    _stub_all_unconfigured(mocker)
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    # Pre-wire: no GEMINI.md exists.
    result = CliRunner().invoke(main, ["doctor"])
    assert "GEMINI.md" in result.output
    assert "no GEMINI.md" in result.output

    # Present but not wired.
    (tmp_path / "repo" / "GEMINI.md").write_text("# mine\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["doctor"])
    assert "present but not wired" in result.output.lower()

    # Wired.
    from conductor import agent_wiring
    agent_wiring.wire_gemini_md(version="0.4.2")
    result = CliRunner().invoke(main, ["doctor"])
    assert "GEMINI.md" in result.output
    # "wired —" appears twice in a fully-wired output; locate it on the
    # GEMINI.md line specifically.
    gemini_lines = [
        ln for ln in result.output.splitlines() if "GEMINI.md" in ln
    ]
    assert any("wired" in ln for ln in gemini_lines), gemini_lines


def test_doctor_reports_cursor_states(mocker, monkeypatch, tmp_path):
    _stub_all_unconfigured(mocker)
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    # No .cursor/rules/.
    result = CliRunner().invoke(main, ["doctor"])
    assert "Cursor:" in result.output
    assert "no .cursor/rules/" in result.output.lower()

    # Wired.
    from conductor import agent_wiring
    agent_wiring.wire_cursor(version="0.4.2")
    result = CliRunner().invoke(main, ["doctor"])
    assert "Cursor:" in result.output
    assert "rule wired" in result.output.lower()


def test_doctor_json_includes_slice_c_fields(mocker, monkeypatch, tmp_path):
    _stub_all_unconfigured(mocker)
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.output)
    ai = payload["agent_integration"]
    for key in (
        "gemini_md_path", "gemini_md_exists", "gemini_md_wired",
        "claude_md_repo_path", "claude_md_repo_exists", "claude_md_repo_wired",
        "cursor_rules_dir", "cursor_rules_dir_exists", "cursor_rule_wired",
    ):
        assert key in ai, f"missing {key}"


def test_doctor_reports_agents_md_wired(mocker, monkeypatch, tmp_path):
    _stub_all_unconfigured(mocker)
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    from conductor import agent_wiring
    agent_wiring.wire_agents_md(version="0.4.1")

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "AGENTS.md" in result.output
    assert "wired" in result.output.lower()


def test_doctor_json_includes_agent_integration(mocker, monkeypatch, tmp_path):
    _stub_all_unconfigured(mocker)
    (tmp_path / ".claude").mkdir()
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert "agent_integration" in payload
    ai = payload["agent_integration"]
    assert ai["claude_detected"] is True
    assert ai["claude_home_exists"] is True
    assert ai["managed_files"] == []


def test_doctor_warns_when_ollama_default_model_missing(mocker, monkeypatch):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    # Everyone else skipped-out as "not configured"; ollama is configured
    # but reports its default model missing.
    for cls in (ClaudeProvider, CodexProvider, GeminiProvider, KimiProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "nope"))
    mocker.patch.object(OllamaProvider, "configured", lambda self: (True, None))
    mocker.patch.object(
        OllamaProvider,
        "default_model_available",
        lambda self: (False, "default model 'qwen2.5-coder:14b' is not pulled"),
    )
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "⚠" in result.output
    assert "qwen2.5-coder:14b" in result.output
    assert "not pulled" in result.output

    # JSON carries the same warning on the ollama provider entry + top-level.
    result_json = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result_json.output)
    ollama_entry = next(p for p in payload["providers"] if p["provider"] == "ollama")
    assert ollama_entry["warnings"]
    assert any(w["provider"] == "ollama" for w in payload["warnings"])
