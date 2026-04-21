"""Tests for the conductor init wizard."""

from __future__ import annotations

from click.testing import CliRunner

from conductor.cli import main
from conductor.providers.kimi import (
    CLOUDFLARE_ACCOUNT_ID_ENV,
    CLOUDFLARE_API_TOKEN_ENV,
)


def test_init_non_interactive_mode_reports_state(mocker, monkeypatch):
    # Non-interactive: everything unconfigured, wizard should report and exit 0.
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
        mocker.patch.object(cls, "configured", lambda self: (False, "stubbed"))
    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)
    mocker.patch("conductor.wizard.credentials.get", return_value=None)

    result = CliRunner().invoke(main, ["init", "--yes"])
    assert result.exit_code == 0, result.output
    assert "Summary" in result.output
    for name in ("kimi", "claude", "codex", "gemini", "ollama"):
        assert name in result.output


def test_init_skips_already_configured_providers(mocker):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    mocker.patch.object(ClaudeProvider, "configured", lambda self: (True, None))
    for cls in (CodexProvider, GeminiProvider, KimiProvider, OllamaProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "nope"))

    result = CliRunner().invoke(main, ["init", "--yes"])
    assert result.exit_code == 0
    assert "already configured" in result.output


def test_init_kimi_interactive_stores_in_keychain(mocker, monkeypatch):
    # Pretend we're on a TTY so the wizard takes the interactive path.
    mocker.patch("conductor.wizard._is_tty", return_value=True)

    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (ClaudeProvider, CodexProvider, GeminiProvider, OllamaProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "stubbed"))

    # Kimi starts unconfigured, becomes configured after the wizard writes.
    state = {"configured": False}

    def _kimi_configured(self):
        return (state["configured"], None if state["configured"] else "missing")

    mocker.patch.object(KimiProvider, "configured", _kimi_configured)
    mocker.patch.object(KimiProvider, "smoke", return_value=(True, None))

    # credentials.get returns None initially so the wizard prompts.
    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    set_mock = mocker.patch("conductor.wizard.credentials.set_in_keychain")

    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)

    # stdin order: api token, account id, storage choice
    result = CliRunner().invoke(
        main,
        ["init"],
        input="my-cf-token\nmy-account-id\nkeychain\n",
    )

    assert result.exit_code == 0, result.output
    # Both credentials were stored.
    assert set_mock.call_count == 2
    keys = [call.args[0] for call in set_mock.call_args_list]
    assert CLOUDFLARE_API_TOKEN_ENV in keys
    assert CLOUDFLARE_ACCOUNT_ID_ENV in keys
    assert "smoke test passed" in result.output.lower()


def test_init_kimi_interactive_print_only(mocker, monkeypatch):
    mocker.patch("conductor.wizard._is_tty", return_value=True)

    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (ClaudeProvider, CodexProvider, GeminiProvider, OllamaProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "stubbed"))
    mocker.patch.object(KimiProvider, "configured", lambda self: (False, "missing"))
    mocker.patch.object(KimiProvider, "smoke", return_value=(True, None))
    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    set_mock = mocker.patch("conductor.wizard.credentials.set_in_keychain")

    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)

    result = CliRunner().invoke(
        main,
        ["init"],
        input="tok\nacct\nprint\n",
    )

    assert result.exit_code == 0
    set_mock.assert_not_called()
    assert f"export {CLOUDFLARE_API_TOKEN_ENV}" in result.output


def test_init_kimi_aborts_when_credential_left_empty(mocker, monkeypatch):
    mocker.patch("conductor.wizard._is_tty", return_value=True)

    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (ClaudeProvider, CodexProvider, GeminiProvider, OllamaProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "stubbed"))
    mocker.patch.object(KimiProvider, "configured", lambda self: (False, "missing"))
    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    set_mock = mocker.patch("conductor.wizard.credentials.set_in_keychain")

    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)

    # User hits enter at the first prompt (empty value) → wizard skips.
    result = CliRunner().invoke(main, ["init"], input="\n")
    assert result.exit_code == 0
    assert "not provided" in result.output.lower() or "skipping" in result.output.lower()
    set_mock.assert_not_called()
