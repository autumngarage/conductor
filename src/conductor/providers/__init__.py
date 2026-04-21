from conductor.providers.interface import (
    CallResponse,
    Provider,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
)
from conductor.providers.kimi import KimiProvider

__all__ = [
    "CallResponse",
    "KimiProvider",
    "Provider",
    "ProviderConfigError",
    "ProviderError",
    "ProviderHTTPError",
]


def get_provider(name: str) -> Provider:
    """Return a provider instance by canonical identifier.

    Raises KeyError if the identifier is not registered.
    """
    registry: dict[str, type[Provider]] = {
        "kimi": KimiProvider,
    }
    if name not in registry:
        raise KeyError(
            f"unknown provider {name!r}; known: {sorted(registry)}"
        )
    return registry[name]()
