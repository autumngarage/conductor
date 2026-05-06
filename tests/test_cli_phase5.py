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
    monkeypatch.setenv(
        "CONDUCTOR_CREDENTIALS_FILE", str(tmp_path / ".config" / "credentials.toml")
    )
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / ".claude"))
    monkeypatch.chdir(repo_dir)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("CONDUCTOR_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("shutil.which", lambda _cmd: None)


def _stub_all_unconfigured(mocker):
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
            # Default-arg binds cls into the closure per iteration (fixes B023).
            lambda self, _cls=cls: (False, f"stub: {_cls.__name__} unset"),
        )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_text_output_shows_all_builtin_providers(mocker):
    _stub_all_unconfigured(mocker)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    for name in (
        "kimi",
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "ollama",
        "openrouter",
    ):
        assert name in result.output


def test_list_json_output_returns_structured_rows(mocker):
    _stub_all_unconfigured(mocker)
    result = CliRunner().invoke(main, ["list", "--json"])
    assert result.exit_code == 0
    rows = json.loads(result.output)
    expected = {
        "kimi",
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "ollama",
        "openrouter",
    }
    assert len(rows) == len(expected)
    assert {r["provider"] for r in rows} == expected
    assert all(r["configured"] is False for r in rows)
    assert all(r["default_model"] for r in rows)
    # Every built-in provider exposes a copy-pasteable fix_command so
    # `conductor list` can show users their next action without any
    # downstream knowledge of provider-specific install/auth recipes.
    assert all(r["fix_command"] for r in rows)


def test_list_text_output_shows_fix_command_under_unconfigured_provider(mocker):
    """When a provider is unconfigured, `conductor list` prints the fix
    one-liner on its own line so the user's next step is one selection away
    instead of buried in the prose reason."""
    _stub_all_unconfigured(mocker)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0, result.output
    # Codex's fix is the install + auth one-liner, exposed verbatim.
    assert "→ fix: brew install codex && codex login" in result.output
    # Kimi is HTTP-backed, so the fix is the wizard.
    assert "→ fix: conductor init --only openrouter" in result.output


def test_list_text_output_suppresses_codex_fix_for_probe_api_config_error(mocker):
    from conductor.providers import CodexProvider

    _stub_all_unconfigured(mocker)
    mocker.patch.object(
        CodexProvider,
        "configured",
        lambda self: (
            False,
            "`codex exec` startup probe exited 1: invalid_request_error: "
            "The following tools cannot be used with reasoning.effort "
            "'minimal': image_gen, web_search.: param=tools",
        ),
    )

    result = CliRunner().invoke(main, ["list"])

    assert result.exit_code == 0, result.output
    assert "invalid_request_error" in result.output
    assert "brew install codex && codex login" not in result.output


def test_doctor_suppresses_codex_fix_for_startup_probe_failure(mocker):
    from conductor.providers import CodexProvider

    _stub_all_unconfigured(mocker)
    mocker.patch.object(
        CodexProvider,
        "configured",
        lambda self: (
            False,
            "`codex exec` startup probe exited 1: invalid_request_error: "
            "The request was rejected.: param=tools",
        ),
    )

    text_result = CliRunner().invoke(main, ["doctor"])
    assert text_result.exit_code == 0, text_result.output
    assert "invalid_request_error" in text_result.output
    assert "→ fix: brew install codex && codex login" not in text_result.output

    json_result = CliRunner().invoke(main, ["doctor", "--json"])
    assert json_result.exit_code == 0, json_result.output
    providers = {
        row["provider"]: row for row in json.loads(json_result.output)["providers"]
    }
    assert providers["codex"]["reason"].startswith("`codex exec` startup probe")
    assert providers["codex"]["fix_command"] is None


def test_list_no_fix_line_for_configured_provider(mocker):
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

    # All unconfigured except claude.
    for cls in (
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
    mocker.patch.object(ClaudeProvider, "configured", lambda self: (True, None))

    result = CliRunner().invoke(main, ["list", "--json"])
    rows = {r["provider"]: r for r in json.loads(result.output)}
    # Configured row carries no fix_command — there's nothing to fix.
    assert rows["claude"]["fix_command"] is None
    # Unconfigured rows still carry theirs.
    assert rows["codex"]["fix_command"] is not None


def test_list_json_includes_tools_field(mocker):
    """conductor list --json exposes tool-support info per provider (#143)."""
    _stub_all_unconfigured(mocker)
    result = CliRunner().invoke(main, ["list", "--json"])
    assert result.exit_code == 0
    rows = {r["provider"]: r for r in json.loads(result.output)}
    # CLI-backed exec providers support all conductor tools.
    for name in ("claude", "codex", "gemini", "ollama", "openrouter"):
        assert rows[name]["tools"] == "all", f"{name}: expected tools=all"
    # HTTP-only providers have no exec tool loop.
    for name in ("kimi", "deepseek-chat", "deepseek-reasoner"):
        assert rows[name]["tools"] == "none", f"{name}: expected tools=none"


def test_list_text_output_shows_tools_column(mocker):
    """conductor list text output includes a TOOLS column (#143)."""
    _stub_all_unconfigured(mocker)
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0
    assert "TOOLS" in result.output
    # At least one provider shows "all" and at least one shows "none".
    assert "all" in result.output
    assert "none" in result.output


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
    for var in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "OLLAMA_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    for name in (
        "kimi",
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "ollama",
        "openrouter",
    ):
        assert name in result.output
    assert "Credentials" in result.output or "credentials" in result.output.lower()
    assert "conductor init" in result.output
    # Doctor surfaces the same per-provider fix one-liner that `list` does,
    # so users following any breadcrumb land on a copy-pasteable command.
    assert "→ fix:" in result.output
    assert "brew install codex && codex login" in result.output


def test_doctor_text_output_shows_smoke_nudge_for_each_configured_provider(mocker):
    from conductor.providers import CodexProvider, OpenRouterProvider

    _stub_all_unconfigured(mocker)
    mocker.patch.object(CodexProvider, "configured", lambda self: (True, None))
    mocker.patch.object(OpenRouterProvider, "configured", lambda self: (True, None))

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "✓ codex" in result.output
    assert "Verify end-to-end: conductor smoke codex" in result.output
    assert "✓ openrouter" in result.output
    assert "Verify end-to-end: conductor smoke openrouter" in result.output


def test_doctor_json_shape(mocker, monkeypatch):
    from conductor.providers import known_providers

    _stub_all_unconfigured(mocker)
    monkeypatch.setenv("OPENROUTER_API_KEY", "x")
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert set(payload.keys()) >= {
        "version",
        "platform",
        "python",
        "providers",
        "muted",
        "credentials",
        "active_credentials",
        "warnings",
    }
    assert len(payload["providers"]) == len(known_providers())
    assert payload["muted"] == []
    cred_map = {c["name"]: c for c in payload["credentials"]}
    assert cred_map["OPENROUTER_API_KEY"]["in_env"] is True
    assert cred_map["OPENROUTER_API_KEY"]["source"] == "env"
    assert cred_map["OLLAMA_BASE_URL"]["in_env"] is False
    assert cred_map["OLLAMA_BASE_URL"]["source"] is None
    assert cred_map["CONDUCTOR_OLLAMA_MODEL"]["in_env"] is False
    assert cred_map["CONDUCTOR_OLLAMA_MODEL"]["source"] is None
    # Every credential row carries the new fields.
    for row in payload["credentials"]:
        assert "has_key_command" in row
        assert "source" in row
    for row in payload["providers"]:
        assert "muted" in row


def test_doctor_mute_unmute_shifts_counts_and_hides_fix_lines(mocker, monkeypatch):
    _stub_all_unconfigured(mocker)
    for var in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "OLLAMA_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    runner = CliRunner()
    mute = runner.invoke(
        main, ["providers", "mute", "kimi", "ollama", "deepseek-chat"]
    )
    assert mute.exit_code == 0, mute.output

    result = runner.invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "Providers (0/5 active, 3 muted):" in result.output
    assert "Muted: deepseek-chat, kimi, ollama" in result.output

    available_section = result.output.split("  Available (not configured):\n", 1)[1]
    available_section = available_section.split("\n\n  Muted:", 1)[0]
    assert "kimi" not in available_section
    assert "ollama" not in available_section
    assert "deepseek-chat" not in available_section
    assert "conductor init --only kimi" not in available_section
    assert "conductor init --only ollama" not in available_section
    assert "deepseek-chat" in result.output

    unmute = runner.invoke(main, ["providers", "unmute", "kimi"])
    assert unmute.exit_code == 0, unmute.output

    unmuted_result = runner.invoke(main, ["doctor"])
    assert unmuted_result.exit_code == 0, unmuted_result.output
    assert "Providers (0/6 active, 2 muted):" in unmuted_result.output
    assert "Muted: deepseek-chat, ollama" in unmuted_result.output
    unmuted_available = unmuted_result.output.split("  Available (not configured):\n", 1)[1]
    unmuted_available = unmuted_available.split("\n\n  Muted:", 1)[0]
    assert "kimi" in unmuted_available
    # After PR 3 (kimi → OpenRouter migration), kimi's fix_command points at
    # the openrouter wizard rather than a kimi-specific one.
    assert "→ fix: conductor init --only openrouter" in unmuted_available


def test_doctor_json_includes_muted_state(mocker):
    _stub_all_unconfigured(mocker)
    runner = CliRunner()
    mute = runner.invoke(main, ["providers", "mute", "kimi", "ollama"])
    assert mute.exit_code == 0, mute.output

    result = runner.invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["muted"] == ["kimi", "ollama"]
    providers = {row["provider"]: row for row in payload["providers"]}
    assert providers["kimi"]["muted"] is True
    assert providers["ollama"]["muted"] is True
    assert providers["claude"]["muted"] is False


def test_doctor_active_credentials_http_provider_shows_source_and_last4(
    mocker, monkeypatch
):
    from conductor.providers import OpenRouterProvider

    _stub_all_unconfigured(mocker)
    mocker.patch.object(OpenRouterProvider, "configured", lambda self: (True, None))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-test-4f3a")
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    rows = {row["provider"]: row for row in payload["active_credentials"]}
    assert rows["openrouter"]["env_var"] == "OPENROUTER_API_KEY"
    assert rows["openrouter"]["source"] == "env"
    assert rows["openrouter"]["fingerprint"] == "sk-or-v1-test-...4f3a"

    text_result = CliRunner().invoke(main, ["doctor"])
    assert "Active credentials (per provider):" in text_result.output
    assert "openrouter" in text_result.output
    assert "OPENROUTER_API_KEY (env, sk-or-v1-test-...4f3a)" in text_result.output


def test_doctor_active_credentials_cli_provider_shows_oauth_session(
    mocker, monkeypatch
):
    from conductor.providers import CodexProvider

    _stub_all_unconfigured(mocker)
    mocker.patch.object(CodexProvider, "configured", lambda self: (True, None))
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    rows = {row["provider"]: row for row in payload["active_credentials"]}
    assert rows["codex"]["kind"] == "cli_session"
    assert rows["codex"]["source"] == "cli_session"
    assert rows["codex"]["env_var"] is None
    assert rows["codex"]["fingerprint"] is None

    text_result = CliRunner().invoke(main, ["doctor"])
    assert "OAuth via `codex` CLI session (no env var)" in text_result.output


def test_doctor_active_credentials_omits_unconfigured_providers(mocker, monkeypatch):
    from conductor.providers import CodexProvider

    _stub_all_unconfigured(mocker)
    mocker.patch.object(CodexProvider, "configured", lambda self: (True, None))
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert [row["provider"] for row in payload["active_credentials"]] == ["codex"]

    text_result = CliRunner().invoke(main, ["doctor"])
    active_section = text_result.output.split("Active credentials (per provider):\n", 1)[1]
    active_section = active_section.split("\n\nAgent integration:", 1)[0]
    assert "codex" in active_section
    assert "openrouter" not in active_section


def test_doctor_reports_key_command_source(mocker, monkeypatch, tmp_path):
    """When a credential is set via key_command in credentials.toml, doctor
    surfaces 'key_command' as the active source — not env or keychain."""
    _stub_all_unconfigured(mocker)
    for var in (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "OLLAMA_BASE_URL",
        "OPENROUTER_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    cred_file = tmp_path / "credentials.toml"
    monkeypatch.setenv("CONDUCTOR_CREDENTIALS_FILE", str(cred_file))
    import subprocess

    from conductor import credentials as creds_mod

    creds_mod.clear_key_command_cache()
    creds_mod.save_key_command(
        "OPENROUTER_API_KEY", "op read op://Personal/OpenRouter/credential"
    )
    mocker.patch(
        "conductor.credentials.shutil.which",
        lambda cmd: "/opt/homebrew/bin/op" if cmd == "op" else None,
    )
    mocker.patch(
        "conductor.credentials.subprocess.run",
        return_value=subprocess.CompletedProcess(
            args=["op"], returncode=0, stdout="resolved-openrouter-key\n", stderr=""
        ),
    )

    result = CliRunner().invoke(main, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    cred_map = {c["name"]: c for c in payload["credentials"]}
    assert cred_map["OPENROUTER_API_KEY"]["source"] == "key_command"
    assert cred_map["OPENROUTER_API_KEY"]["has_key_command"] is True
    assert cred_map["OPENROUTER_API_KEY"]["in_env"] is False

    # Text rendering surfaces the secret-manager label.
    text_result = CliRunner().invoke(main, ["doctor"])
    assert "key_command (secret manager)" in text_result.output


def test_doctor_env_beats_key_command_in_source(mocker, monkeypatch, tmp_path):
    """Env var wins over key_command — operator can override secret-manager
    config from a single shell session for debugging/CI."""
    _stub_all_unconfigured(mocker)
    monkeypatch.setenv("OPENROUTER_API_KEY", "from-env")
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    cred_file = tmp_path / "credentials.toml"
    monkeypatch.setenv("CONDUCTOR_CREDENTIALS_FILE", str(cred_file))
    from conductor import credentials as creds_mod

    creds_mod.clear_key_command_cache()
    creds_mod.save_key_command("OPENROUTER_API_KEY", "echo from-op")

    result = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result.output)
    cred_map = {c["name"]: c for c in payload["credentials"]}
    assert cred_map["OPENROUTER_API_KEY"]["source"] == "env"
    assert cred_map["OPENROUTER_API_KEY"]["has_key_command"] is True


def test_doctor_warns_when_deepseek_key_is_set_without_openrouter(
    mocker, monkeypatch
):
    _stub_all_unconfigured(mocker)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-env")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "DEEPSEEK_API_KEY is deprecated" in result.output
    assert "conductor init --only openrouter" in result.output

    result_json = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result_json.output)
    assert any(
        "DEEPSEEK_API_KEY is deprecated" in warning["message"]
        for warning in payload["warnings"]
    )


def test_doctor_warns_when_legacy_kimi_creds_are_set_without_openrouter(
    mocker, monkeypatch
):
    _stub_all_unconfigured(mocker)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "legacy-token")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    mocker.patch("conductor.cli.credentials.keychain_has", return_value=False)

    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "kimi now routes through OpenRouter" in result.output
    assert "CLOUDFLARE_* credentials are no longer used" in result.output

    result_json = CliRunner().invoke(main, ["doctor", "--json"])
    payload = json.loads(result_json.output)
    assert any(
        "kimi now routes through OpenRouter" in warning["message"]
        for warning in payload["warnings"]
    )


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
    assert "no Conductor delegation block" in result.output
    assert "file still loads normally" in result.output


def test_doctor_reports_gemini_md_states(mocker, monkeypatch, tmp_path):
    _stub_all_unconfigured(mocker)
    for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "OLLAMA_BASE_URL"):
        monkeypatch.delenv(var, raising=False)

    # Pre-wire: no GEMINI.md exists.
    result = CliRunner().invoke(main, ["doctor"])
    assert "GEMINI.md" in result.output
    assert "no GEMINI.md" in result.output

    # Present but no Conductor delegation block.
    (tmp_path / "repo" / "GEMINI.md").write_text("# mine\n", encoding="utf-8")
    result = CliRunner().invoke(main, ["doctor"])
    assert "no Conductor delegation block" in result.output

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

    # Directory present but Conductor rule missing.
    (tmp_path / "repo" / ".cursor" / "rules").mkdir(parents=True)
    result = CliRunner().invoke(main, ["doctor"])
    assert "Cursor:" in result.output
    assert "no Conductor rule" in result.output

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
