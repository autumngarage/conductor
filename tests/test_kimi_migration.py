from __future__ import annotations

import json
import time

import httpx
import respx
from click.testing import CliRunner

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.cli import main
from conductor.providers.interface import ProviderHTTPError
from conductor.providers.kimi import KIMI_DEFAULT_MODEL, KimiProvider
from conductor.providers.openrouter import OPENROUTER_API_KEY_ENV, OpenRouterProvider


def _model(model_id: str, *, created: int) -> openrouter_catalog.ModelEntry:
    return openrouter_catalog.ModelEntry(
        id=model_id,
        name=model_id,
        created=created,
        context_length=256_000,
        pricing_prompt=0.001,
        pricing_completion=0.002,
        pricing_thinking=None,
        supports_thinking=False,
        supports_tools=False,
        supports_vision=False,
    )


def _write_catalog_cache(path, *, model_id: str, fetched_at: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "{\n"
        f'  "fetched_at": {fetched_at},\n'
        '  "models": [\n'
        "    {\n"
        f'      "id": "{model_id}",\n'
        f'      "name": "{model_id}",\n'
        '      "created": 1800000000,\n'
        '      "context_length": 256000,\n'
        '      "pricing_prompt": 0.001,\n'
        '      "pricing_completion": 0.002,\n'
        '      "pricing_thinking": null,\n'
        '      "supports_thinking": false,\n'
        '      "supports_tools": false,\n'
        '      "supports_vision": false\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


def test_kimi_provider_subclasses_openrouter_and_presets_model():
    assert issubclass(KimiProvider, OpenRouterProvider)
    provider = KimiProvider()
    assert provider.default_model == "moonshotai/kimi-k2.6"
    assert provider.fix_command == "conductor init --only openrouter"


def test_kimi_call_uses_preset_openrouter_model(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    monkeypatch.setattr(
        openrouter_catalog,
        "load_catalog",
        lambda: [_model(KIMI_DEFAULT_MODEL, created=1_700_000_000)],
    )
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
        "usage": {"include": True},
    }


def test_kimi_call_uses_newest_catalog_slug(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    monkeypatch.setattr(
        openrouter_catalog,
        "load_catalog",
        lambda: [
            _model("moonshotai/kimi-k2.6", created=1_700_000_000),
            _model("moonshotai/kimi-k3", created=1_800_000_000),
        ],
    )
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": "moonshotai/kimi-k3",
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = KimiProvider().call("hi")

    assert response.model == "moonshotai/kimi-k3"
    assert captured["payload"]["model"] == "moonshotai/kimi-k3"


def test_kimi_call_falls_back_to_pinned_slug_when_catalog_unavailable(
    monkeypatch, capsys
):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")

    def _raise_catalog_error():
        raise ProviderHTTPError("network down")

    monkeypatch.setattr(openrouter_catalog, "load_catalog", _raise_catalog_error)
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
    assert captured["payload"]["model"] == KIMI_DEFAULT_MODEL
    assert "using pinned fallback" in capsys.readouterr().err


def test_kimi_fresh_catalog_cache_hit_is_silent(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    cache_path = tmp_path / "openrouter-catalog.json"
    monkeypatch.setattr(openrouter_catalog, "OPENROUTER_CATALOG_CACHE_PATH", cache_path)
    cached_model = "moonshotai/kimi-k3"
    _write_catalog_cache(cache_path, model_id=cached_model, fetched_at=int(time.time()))
    captured: dict[str, object] = {}

    def _record(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "model": cached_model,
                "choices": [{"message": {"content": "ok"}}],
                "usage": {},
            },
        )

    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(side_effect=_record)
        response = KimiProvider().call("hi")

    assert response.model == cached_model
    assert captured["payload"]["model"] == cached_model
    assert capsys.readouterr().err == ""


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
