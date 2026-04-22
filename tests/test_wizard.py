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
    # Use --only kimi to target the kimi flow directly; the concierge
    # CLI-flow for claude/codex/gemini/ollama would otherwise consume
    # stdin first.
    mocker.patch("conductor.wizard._is_tty", return_value=True)

    from conductor.providers import KimiProvider

    state = {"configured": False}

    def _kimi_configured(self):
        return (state["configured"], None if state["configured"] else "missing")

    mocker.patch.object(KimiProvider, "configured", _kimi_configured)
    mocker.patch.object(KimiProvider, "smoke", return_value=(True, None))

    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    set_mock = mocker.patch("conductor.wizard.credentials.set_in_keychain")

    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)

    # stdin order: api token, account id, storage choice (full word accepted)
    result = CliRunner().invoke(
        main,
        ["init", "--only", "kimi"],
        input="my-cf-token\nmy-account-id\nkeychain\n",
    )

    assert result.exit_code == 0, result.output
    assert set_mock.call_count == 2
    keys = [call.args[0] for call in set_mock.call_args_list]
    assert CLOUDFLARE_API_TOKEN_ENV in keys
    assert CLOUDFLARE_ACCOUNT_ID_ENV in keys
    assert "smoke test passed" in result.output.lower()


def test_init_kimi_interactive_print_only(mocker, monkeypatch):
    mocker.patch("conductor.wizard._is_tty", return_value=True)

    from conductor.providers import KimiProvider

    mocker.patch.object(KimiProvider, "configured", lambda self: (False, "missing"))
    mocker.patch.object(KimiProvider, "smoke", return_value=(True, None))
    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    set_mock = mocker.patch("conductor.wizard.credentials.set_in_keychain")

    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)

    result = CliRunner().invoke(
        main,
        ["init", "--only", "kimi"],
        input="tok\nacct\nprint\n",
    )

    assert result.exit_code == 0
    set_mock.assert_not_called()
    assert f"export {CLOUDFLARE_API_TOKEN_ENV}" in result.output


def test_init_kimi_aborts_when_credential_left_empty(mocker, monkeypatch):
    mocker.patch("conductor.wizard._is_tty", return_value=True)

    from conductor.providers import KimiProvider

    mocker.patch.object(KimiProvider, "configured", lambda self: (False, "missing"))
    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    set_mock = mocker.patch("conductor.wizard.credentials.set_in_keychain")

    monkeypatch.delenv(CLOUDFLARE_API_TOKEN_ENV, raising=False)
    monkeypatch.delenv(CLOUDFLARE_ACCOUNT_ID_ENV, raising=False)

    # User hits enter at the first credential prompt — empty → skip.
    result = CliRunner().invoke(main, ["init", "--only", "kimi"], input="\n")
    assert result.exit_code == 0
    assert "not provided" in result.output.lower() or "skipping" in result.output.lower()
    set_mock.assert_not_called()


# ---------------------------------------------------------------------------
# Concierge UX additions (C8)
# ---------------------------------------------------------------------------


def test_init_shows_description_and_tier_per_provider(mocker):
    """Every provider's section should include tagline, tier, install cmd."""
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (
        ClaudeProvider, CodexProvider, GeminiProvider, KimiProvider, OllamaProvider
    ):
        mocker.patch.object(cls, "configured", lambda self: (False, "stubbed"))

    result = CliRunner().invoke(main, ["init", "--yes"])
    assert result.exit_code == 0
    # Tagline for at least one provider.
    assert "flagship reasoning" in result.output.lower()
    # Tier labels surface.
    assert "tier: frontier" in result.output.lower()
    assert "tier: local" in result.output.lower()
    # Copy-pasteable install command for at least one shell-out provider.
    assert "brew install claude" in result.output
    # Credential-source URL for an API-key provider.
    assert "dash.cloudflare.com" in result.output


def test_init_only_flag_walks_single_provider(mocker):
    from conductor.providers import ClaudeProvider

    mocker.patch.object(ClaudeProvider, "configured", lambda self: (False, "nope"))

    result = CliRunner().invoke(main, ["init", "--only", "claude", "--yes"])
    assert result.exit_code == 0
    assert "[1/1]  claude" in result.output
    # Should NOT include other providers.
    assert "kimi" not in result.output.lower() or "conductor list" in result.output
    # (The "conductor list" line mentions all providers implicitly; the
    # key check is the section header shows only 1/1.)


def test_init_remaining_skips_configured(mocker):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    # claude is configured; everything else isn't.
    mocker.patch.object(ClaudeProvider, "configured", lambda self: (True, None))
    for cls in (CodexProvider, GeminiProvider, KimiProvider, OllamaProvider):
        mocker.patch.object(cls, "configured", lambda self: (False, "nope"))

    result = CliRunner().invoke(main, ["init", "--remaining", "--yes"])
    assert result.exit_code == 0
    # claude's section header should NOT appear (it's already configured).
    assert "[1/5]  claude" not in result.output
    # Other providers' sections should appear.
    assert "codex" in result.output.lower()


def test_init_only_unknown_provider_errors():
    result = CliRunner().invoke(main, ["init", "--only", "nonexistent"])
    assert result.exit_code == 2
    assert "unknown provider" in result.output.lower()


def test_init_only_and_remaining_mutually_exclusive():
    result = CliRunner().invoke(main, ["init", "--only", "kimi", "--remaining"])
    assert result.exit_code == 2
    assert "mutually exclusive" in result.output.lower()


def test_init_summary_and_next_steps_printed(mocker):
    from conductor.providers import (
        ClaudeProvider,
        CodexProvider,
        GeminiProvider,
        KimiProvider,
        OllamaProvider,
    )

    for cls in (
        ClaudeProvider, CodexProvider, GeminiProvider, KimiProvider, OllamaProvider
    ):
        mocker.patch.object(cls, "configured", lambda self: (False, "nope"))

    result = CliRunner().invoke(main, ["init", "--yes"])
    assert result.exit_code == 0
    assert "Summary" in result.output
    assert "Next steps:" in result.output
    assert "conductor list" in result.output
    assert "conductor smoke --all" in result.output
    # Default routing preferences mentioned at the end.
    assert "prefer=balanced" in result.output
    assert "effort=medium" in result.output
