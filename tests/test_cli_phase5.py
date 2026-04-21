"""CLI tests for list, smoke, doctor commands."""

from __future__ import annotations

import json

from click.testing import CliRunner

from conductor.cli import main


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
            cls, "configured", lambda self: (False, f"stub: {cls.__name__} unset")
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
    assert set(payload.keys()) >= {"version", "platform", "python", "providers", "credentials"}
    assert len(payload["providers"]) == 5
    cred_map = {c["name"]: c for c in payload["credentials"]}
    assert cred_map["CLOUDFLARE_API_TOKEN"]["in_env"] is True
    assert cred_map["CLOUDFLARE_ACCOUNT_ID"]["in_env"] is False
