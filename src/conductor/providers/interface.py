"""Provider protocol and shared types.

Conductor providers wrap one upstream LLM (or one CLI that wraps one). Every
provider exposes the same three operations — `configured()`, `smoke()`,
`call()` — so the CLI can treat them uniformly.

Two physical shapes are supported:
  - HTTP adapters (e.g. kimi, ollama) that talk to an OpenAI-compatible
    endpoint via httpx. These touch API keys directly.
  - Subprocess adapters (e.g. claude, codex, gemini) that shell out to a CLI
    that owns its own auth. These never touch API keys.

The two shapes share `Provider` (a Protocol), `CallResponse` (the result), and
the error hierarchy below.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Protocol, runtime_checkable


class ProviderError(Exception):
    """Base for provider-side failures the CLI should surface to the user."""


class ProviderConfigError(ProviderError):
    """Raised when a provider is invoked without its required configuration
    (env var missing, CLI not installed, etc.).

    The user-actionable remedy belongs in the message.
    """


class ProviderHTTPError(ProviderError):
    """Raised when an HTTP-backed provider receives an upstream error
    (non-2xx, malformed JSON, timeout)."""


@dataclass(frozen=True)
class CallResponse:
    """Normalized result of a single `provider.call(...)`."""

    text: str
    provider: str
    model: str
    duration_ms: int
    usage: dict = field(default_factory=dict)
    cost_usd: Optional[float] = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    name: str
    tags: list[str]
    default_model: str

    def configured(self) -> tuple[bool, Optional[str]]:
        """Return (True, None) if the provider can run, else (False, reason)."""

    def smoke(self) -> tuple[bool, Optional[str]]:
        """Cheapest possible round-trip that proves auth + endpoint work."""

    def call(self, task: str, model: Optional[str] = None) -> CallResponse:
        """Send the task; return a normalized response. Raises ProviderError on failure."""
