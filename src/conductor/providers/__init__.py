from conductor.providers.claude import ClaudeProvider
from conductor.providers.codex import CodexProvider
from conductor.providers.gemini import GeminiProvider
from conductor.providers.interface import (
    CallResponse,
    Provider,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
)
from conductor.providers.kimi import KimiProvider
from conductor.providers.ollama import OllamaProvider

__all__ = [
    "CallResponse",
    "ClaudeProvider",
    "CodexProvider",
    "GeminiProvider",
    "KimiProvider",
    "OllamaProvider",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderHTTPError",
    "get_provider",
    "known_providers",
]

_REGISTRY: dict[str, type[Provider]] = {
    "kimi": KimiProvider,
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}


def known_providers() -> list[str]:
    """Return the sorted list of canonical provider identifiers."""
    return sorted(_REGISTRY)


def get_provider(name: str) -> Provider:
    """Return a provider instance by canonical identifier.

    Raises KeyError if the identifier is not registered.
    """
    if name not in _REGISTRY:
        raise KeyError(
            f"unknown provider {name!r}; known: {known_providers()}"
        )
    return _REGISTRY[name]()
