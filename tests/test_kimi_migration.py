from __future__ import annotations

import json

import httpx
import respx
from click.testing import CliRunner

from conductor.cli import main
from conductor.providers.kimi import KIMI_DEFAULT_MODEL, KimiProvider
from conductor.providers.openrouter import OPENROUTER_API_KEY_ENV, OpenRouterProvider


def test_kimi_provider_subclasses_openrouter_and_presets_model():
    assert issubclass(KimiProvider, OpenRouterProvider)
    provider = KimiProvider()
    assert provider.default_model == "moonshotai/kimi-k2.6"
    assert provider.fix_command == "conductor init --only openrouter"


def test_kimi_call_uses_preset_openrouter_model(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": KIMI_DEFAULT_MODEL,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = KimiProvider().call("hi")

    assert response.model == KIMI_DEFAULT_MODEL
    assert captured["payload"] == {
        "model": KIMI_DEFAULT_MODEL,
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning": {"effort": "medium"},
    }


def test_init_kimi_surfaces_legacy_cloudflare_migration_message(
    mocker, monkeypatch, tmp_path
):
    mocker.patch("conductor.wizard._is_tty", return_value=True)
    mocker.patch.object(KimiProvider, "configured", lambda self: (False, "missing"))
    mocker.patch.object(OpenRouterProvider, "smoke", return_value=(True, None))
    mocker.patch("conductor.wizard.credentials.get", return_value=None)
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    monkeypatch.setenv("CONDUCTOR_HOME", str(tmp_path / ".conductor"))
    monkeypatch.setenv(
        "CONDUCTOR_CREDENTIALS_FILE", str(tmp_path / ".config" / "credentials.toml")
    )
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / ".claude"))
    monkeypatch.chdir(repo_dir)
    monkeypatch.delenv(OPENROUTER_API_KEY_ENV, raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "legacy-token")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)

    result = CliRunner().invoke(
        main,
        ["init", "--only", "kimi"],
        input="or-test-key\nprint\n",
    )

    assert result.exit_code == 0, result.output
    assert "Detected legacy CLOUDFLARE_API_TOKEN" in result.output
    assert "kimi now routes through OpenRouter" in result.output
    assert "OpenRouter API key (OPENROUTER_API_KEY)" in result.output
