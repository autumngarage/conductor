"""Provider protocol and shared types.

Conductor providers wrap one upstream LLM (or one CLI that wraps one). Every
provider exposes a uniform contract — `configured()`, `smoke()`, `call()`,
`exec()` — so the CLI and router can treat them uniformly.

Two physical shapes are supported:
  - HTTP adapters (e.g. kimi, ollama) that talk to an OpenAI-compatible
    endpoint via httpx. These touch API keys directly.
  - Subprocess adapters (e.g. claude, codex, gemini) that shell out to a CLI
    that owns its own auth. These never touch API keys.

Both shapes share the `Provider` protocol, the `CallResponse` result type,
and the error hierarchy below.

Capability declarations (v0.2):
  - quality_tier            — "frontier" | "strong" | "standard" | "local"
  - supported_tools         — frozenset of tool names ({Read, Grep, Glob, Edit, Write, Bash})
  - supported_sandboxes     — frozenset of sandbox modes ({"read-only", "workspace-write", "none"})
  - supports_effort         — whether the provider has a thinking/reasoning dial
  - effort_to_thinking      — mapping from symbolic effort level to expected thinking tokens
  - cost_per_1k_in/out/thinking — for prefer=cheapest scoring
  - typical_p50_ms          — for prefer=fastest scoring

Routing (see `conductor.router`) filters providers by supported_tools and
supported_sandboxes against the caller's request, then scores by `prefer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

# --------------------------------------------------------------------------- #
# Effort levels — symbolic dial for "how hard should this provider think".
# Providers translate to their own native parameter (claude --thinking-budget,
# codex --effort, kimi reasoning_content, etc.) via `effort_to_thinking`.
# --------------------------------------------------------------------------- #
EFFORT_LEVELS = ("minimal", "low", "medium", "high", "max")

# --------------------------------------------------------------------------- #
# Quality tiers — declared, not measured. Maintained in Conductor, updated
# when flagship models ship. Used by `prefer=best` scoring.
# --------------------------------------------------------------------------- #
QUALITY_TIERS = ("frontier", "strong", "standard", "local")
TIER_RANK = {name: len(QUALITY_TIERS) - i for i, name in enumerate(QUALITY_TIERS)}
# frontier=4, strong=3, standard=2, local=1

# --------------------------------------------------------------------------- #
# Tools — the portable set Conductor exposes to callers. Providers declare
# which of these they can drive; the router filters unsupported combinations.
# --------------------------------------------------------------------------- #
TOOL_NAMES = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
SANDBOX_MODES = frozenset({"read-only", "workspace-write", "none"})


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


class UnsupportedCapability(ProviderError):  # noqa: N818  — public API name; renaming to -Error breaks callers
    """Raised when a provider cannot satisfy the requested capability —
    e.g., tool-use requested but the provider only supports single-turn
    call() (kimi/ollama in v0.2 pre-tool-use-loop).

    The router should prefer filtering these providers *before* invocation,
    but the exception exists as a backstop for callers that bypass routing.
    """


@dataclass(frozen=True)
class CallResponse:
    """Normalized result of a single `provider.call(...)` or `provider.exec(...)`.

    For exec() calls that run a multi-turn tool-use loop, ``text`` holds the
    final agent message; ``usage`` includes ``thinking_tokens`` and
    ``tool_use_iterations`` when available.

    ``session_id`` is the underlying CLI's identifier for this conversation
    when one exists (claude/codex/gemini all assign one per call). HTTP
    providers (kimi, ollama) leave it None — they're stateless. Callers
    can persist this and pass it back via ``resume_session_id`` to resume
    a multi-turn conversation; routing-layer use is opaque.
    """

    text: str
    provider: str
    model: str
    duration_ms: int
    usage: dict = field(default_factory=dict)
    cost_usd: float | None = None
    session_id: str | None = None
    raw: dict = field(default_factory=dict)


@runtime_checkable
class Provider(Protocol):
    # --- identity ---------------------------------------------------------- #
    name: str
    default_model: str

    # --- capability tags (soft matching for routing) ----------------------- #
    tags: list[str]

    # --- capability declarations (hard filters + scoring dimensions) ------- #
    quality_tier: str
    supported_tools: frozenset[str]
    supported_sandboxes: frozenset[str]
    supports_effort: bool
    effort_to_thinking: dict[str, int]
    cost_per_1k_in: float
    cost_per_1k_out: float
    cost_per_1k_thinking: float
    typical_p50_ms: int

    # --- core methods ------------------------------------------------------ #
    def configured(self) -> tuple[bool, str | None]:
        """Return (True, None) if the provider can run, else (False, reason)."""

    def smoke(self) -> tuple[bool, str | None]:
        """Cheapest possible round-trip that proves auth + endpoint work."""

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str = "medium",
    ) -> CallResponse:
        """Single-turn call. `effort` is a symbolic dial (see EFFORT_LEVELS)
        or an integer thinking-token budget. Providers without effort
        support silently accept and no-op.

        Raises ProviderError on failure.
        """

    def exec(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str = "medium",
        tools: frozenset[str] = frozenset(),
        sandbox: str = "none",
        cwd: str | None = None,
        timeout_sec: int = 300,
    ) -> CallResponse:
        """Multi-turn agent session with tool access.

        Raises UnsupportedCapability if the provider cannot drive the
        requested tools or sandbox. Raises ProviderError on runtime failure.
        """


# --------------------------------------------------------------------------- #
# Helpers shared by providers.
# --------------------------------------------------------------------------- #


def resolve_effort_tokens(
    effort: str | int,
    effort_to_thinking: dict[str, int],
) -> int:
    """Translate a symbolic effort level or explicit integer to a thinking-token
    budget. Returns 0 for unknown values or empty maps (effort-unsupported
    providers silently no-op)."""
    if isinstance(effort, int):
        return max(0, effort)
    if not effort_to_thinking:
        return 0
    return effort_to_thinking.get(effort, effort_to_thinking.get("medium", 0))
