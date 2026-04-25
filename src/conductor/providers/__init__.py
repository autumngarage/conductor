from conductor.providers.claude import ClaudeProvider
from conductor.providers.codex import CodexProvider
from conductor.providers.deepseek import DeepSeekChatProvider, DeepSeekReasonerProvider
from conductor.providers.gemini import GeminiProvider
from conductor.providers.interface import (
    EFFORT_LEVELS,
    QUALITY_TIERS,
    SANDBOX_MODES,
    TIER_RANK,
    TOOL_NAMES,
    CallResponse,
    Provider,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    UnsupportedCapability,
    resolve_effort_tokens,
)
from conductor.providers.kimi import KimiProvider
from conductor.providers.ollama import OllamaProvider
from conductor.providers.shell import ShellProvider, ShellProviderSpec

__all__ = [
    "EFFORT_LEVELS",
    "QUALITY_TIERS",
    "SANDBOX_MODES",
    "TIER_RANK",
    "TOOL_NAMES",
    "CallResponse",
    "ClaudeProvider",
    "CodexProvider",
    "DeepSeekChatProvider",
    "DeepSeekReasonerProvider",
    "GeminiProvider",
    "KimiProvider",
    "OllamaProvider",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderHTTPError",
    "ShellProvider",
    "ShellProviderSpec",
    "UnsupportedCapability",
    "get_provider",
    "known_providers",
    "resolve_effort_tokens",
]

_BUILTIN_REGISTRY: dict[str, type[Provider]] = {
    "kimi": KimiProvider,
    "claude": ClaudeProvider,
    "codex": CodexProvider,
    "deepseek-chat": DeepSeekChatProvider,
    "deepseek-reasoner": DeepSeekReasonerProvider,
    "gemini": GeminiProvider,
    "ollama": OllamaProvider,
}


def known_providers() -> list[str]:
    """Return the sorted list of all provider identifiers (built-in + custom)."""
    names = set(_BUILTIN_REGISTRY)
    names.update(_custom_spec_names())
    return sorted(names)


def get_provider(name: str) -> Provider:
    """Return a provider instance by canonical identifier.

    Checks the built-in registry first, then the user-local custom
    providers (loaded fresh each call so `conductor providers add`
    doesn't require a restart).

    Raises KeyError if the identifier is not registered.
    """
    if name in _BUILTIN_REGISTRY:
        return _BUILTIN_REGISTRY[name]()

    for spec in _custom_specs():
        if spec.name == name:
            return ShellProvider(spec)

    raise KeyError(
        f"unknown provider {name!r}; known: {known_providers()}"
    )


# --------------------------------------------------------------------------- #
# Custom-provider loading. Wrapped in a function so the import-time cost of
# reading the TOML file only fires when the registry is actually queried,
# and imports don't cycle (conductor.custom_providers imports from here for
# QUALITY_TIERS validation).
# --------------------------------------------------------------------------- #


def _custom_specs() -> list[ShellProviderSpec]:
    from conductor.custom_providers import CustomProviderError, load_specs

    try:
        return load_specs()
    except CustomProviderError:
        # A corrupt user-local file should not brick the whole CLI.
        # `conductor doctor` surfaces the error with a clear pointer; other
        # commands skip custom providers silently in this path.
        return []


def _custom_spec_names() -> list[str]:
    return [s.name for s in _custom_specs()]
