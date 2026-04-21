"""Registry-level tests for conductor.providers."""

from __future__ import annotations

import pytest

from conductor.providers import (
    ClaudeProvider,
    CodexProvider,
    GeminiProvider,
    KimiProvider,
    OllamaProvider,
    get_provider,
    known_providers,
)


def test_known_providers_returns_all_five():
    assert known_providers() == ["claude", "codex", "gemini", "kimi", "ollama"]


def test_get_provider_returns_correct_class():
    assert isinstance(get_provider("kimi"), KimiProvider)
    assert isinstance(get_provider("claude"), ClaudeProvider)
    assert isinstance(get_provider("codex"), CodexProvider)
    assert isinstance(get_provider("gemini"), GeminiProvider)
    assert isinstance(get_provider("ollama"), OllamaProvider)


def test_get_provider_unknown_raises_keyerror_with_hint():
    with pytest.raises(KeyError) as exc:
        get_provider("not-a-provider")
    assert "not-a-provider" in str(exc.value)
    assert "claude" in str(exc.value)


def test_every_provider_implements_protocol_surface():
    # Each provider must expose name/tags/default_model + configured/smoke/call.
    for name in known_providers():
        provider = get_provider(name)
        assert isinstance(provider.name, str)
        assert isinstance(provider.tags, list) and all(
            isinstance(t, str) for t in provider.tags
        )
        assert isinstance(provider.default_model, str)
        assert callable(provider.configured)
        assert callable(provider.smoke)
        assert callable(provider.call)


def test_provider_identifiers_match_class_name():
    # Canonical name on the class equals the registry key.
    for name in known_providers():
        assert get_provider(name).name == name
