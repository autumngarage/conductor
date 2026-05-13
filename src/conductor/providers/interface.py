"""Provider protocol and shared types.

Conductor providers wrap one upstream LLM (or one CLI that wraps one). Every
provider exposes a uniform contract — `configured()`, `smoke()`, `call()`,
`exec()` — so the CLI and router can treat them uniformly.

Two physical shapes are supported:
  - HTTP adapters (e.g. openrouter, kimi, ollama) that talk to an OpenAI-compatible
    endpoint via httpx. These touch API keys directly.
  - Subprocess adapters (e.g. claude, codex, gemini) that shell out to a CLI
    that owns its own auth. These never touch API keys.

Both shapes share the `Provider` protocol, the `CallResponse` result type,
and the error hierarchy below.

Capability declarations (v0.2):
  - quality_tier            — "frontier" | "strong" | "standard" | "local"
  - runtime_kind            — "stateful-agent" | "stateless-tool-loop" | "text-only"
  - supported_tools         — frozenset of tool names ({Read, Grep, Glob, Edit, Write, Bash})
  - enforces_exec_tool_permissions — whether exec() honors Conductor's requested tool whitelist
  - supports_effort         — whether the provider has a thinking/reasoning dial
  - supports_image_attachments — whether the provider can accept image file attachments
                                 alongside the brief (today: codex only)
  - endpoint_url           — HTTPS URL whose RTT represents provider network path
  - effort_to_thinking      — mapping from symbolic effort level to expected thinking tokens
  - cost_per_1k_in/out/thinking — for prefer=cheapest scoring
  - typical_p50_ms          — for prefer=fastest scoring

Routing (see `conductor.router`) filters providers by supported_tools, then
scores by `prefer`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, ClassVar, Protocol, runtime_checkable

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

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
PROVIDER_RUNTIME_STATEFUL_AGENT = "stateful-agent"
PROVIDER_RUNTIME_STATELESS_TOOL_LOOP = "stateless-tool-loop"
PROVIDER_RUNTIME_TEXT_ONLY = "text-only"
PROVIDER_RUNTIME_KINDS = frozenset(
    {
        PROVIDER_RUNTIME_STATEFUL_AGENT,
        PROVIDER_RUNTIME_STATELESS_TOOL_LOOP,
        PROVIDER_RUNTIME_TEXT_ONLY,
    }
)


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

    def __init__(
        self,
        message: str,
        *,
        failure_reason: str | None = None,
        provider: str | None = None,
        status_code: int | None = None,
        upstream_body: str | None = None,
    ) -> None:
        self.failure_reason = failure_reason
        self.provider = provider
        self.status_code = status_code
        self.upstream_body = upstream_body
        super().__init__(message)


class ProviderExecutionError(ProviderError):
    """Raised when a provider completes transport but fails execution policy."""

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        status: dict | None = None,
    ) -> None:
        self.provider = provider
        self.status = status or {}
        self.error_response = {
            "error": "provider_execution_failed",
            "provider": provider,
            "message": message,
            "execution_status": self.status,
        }
        super().__init__(message)


class ProviderStalledError(ProviderError):
    """Raised when a provider produced no output for longer than the
    configured max_stall_sec watchdog. Distinct from wall-clock timeout —
    a stall means the subprocess is alive but not making progress."""


class ProviderStartupStalledError(ProviderStalledError):
    """Raised when a CLI provider produces no initial output after launch."""

    def __init__(
        self,
        *,
        provider: str,
        timeout_sec: float,
        message: str | None = None,
        diagnostic: dict | None = None,
    ) -> None:
        self.provider = provider
        self.timeout_sec = timeout_sec
        self.phase = "startup"
        formatted_timeout = (
            str(int(timeout_sec))
            if float(timeout_sec).is_integer()
            else f"{timeout_sec:g}"
        )
        self.diagnostic = diagnostic
        self.error_response = {
            "error": "provider_startup_stalled",
            "provider": provider,
            "timeout_sec": timeout_sec,
            "phase": self.phase,
            "message": message
            or (
                f"{provider} CLI startup stalled: produced no output within "
                f"{formatted_timeout}s after start"
            ),
        }
        super().__init__(self.error_response["message"])


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
    providers (openrouter, kimi, ollama) leave it None — they're stateless. Callers
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
    auth_prompts: list[dict] | None = None


@runtime_checkable
class Provider(Protocol):
    # --- identity ---------------------------------------------------------- #
    # Class-level attributes on every implementation. Declared as ClassVar so
    # mypy treats the implementations' class-level assignments as Protocol-
    # conformant (the default Protocol attribute is treated as a settable
    # instance variable, which read-only class attributes do not satisfy).
    name: ClassVar[str]
    default_model: ClassVar[str]

    # --- capability tags (soft matching for routing) ----------------------- #
    tags: ClassVar[list[str]]

    # --- setup hint -------------------------------------------------------- #
    # A copy-pasteable shell one-liner that takes the user from "not
    # configured" to "configured". Surfaced beneath the failure reason in
    # `conductor list` and `conductor doctor` so the next action is always
    # one selection away. CLI-wrapped providers point at the install + auth
    # commands; HTTP providers point at `conductor init --only <name>` since
    # their setup is an env-var/credentials wizard, not a binary install.
    fix_command: ClassVar[str | None]

    # --- capability declarations (hard filters + scoring dimensions) ------- #
    quality_tier: ClassVar[str]
    runtime_kind: ClassVar[str]
    supported_tools: ClassVar[frozenset[str]]
    enforces_exec_tool_permissions: ClassVar[bool]
    supports_effort: ClassVar[bool]
    supports_image_attachments: ClassVar[bool]
    effort_to_thinking: ClassVar[dict[str, int]]
    cost_per_1k_in: ClassVar[float]
    cost_per_1k_out: ClassVar[float]
    cost_per_1k_thinking: ClassVar[float]
    typical_p50_ms: ClassVar[int]

    # --- core methods ------------------------------------------------------ #
    def configured(self) -> tuple[bool, str | None]:
        """Return (True, None) if the provider can run, else (False, reason)."""

    def smoke(self) -> tuple[bool, str | None]:
        """Cheapest possible round-trip that proves auth + endpoint work."""

    def health_probe(self, *, timeout_sec: float = 30.0) -> tuple[bool, str | None]:
        """Cheapest possible end-to-end check that the provider can be invoked
        right now.

        Distinct from configured() (cheap, static config check) and smoke()
        (full round-trip). Providers override this with a fast probe suited to
        their transport.
        """
        raise NotImplementedError

    def endpoint_url(self) -> str | None:
        """Return the HTTPS URL whose RTT represents this provider's
        network path.

        Used by the slow-network scaler. Return None if the provider has no
        single canonical network endpoint, such as local-only providers.
        """
        return None

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str | int = "medium",
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        """Single-turn call. `effort` is a symbolic dial (see EFFORT_LEVELS)
        or an integer thinking-token budget. Providers without effort
        support silently accept and no-op.

        ``resume_session_id`` resumes a prior conversation when supported
        by the underlying CLI (claude, codex, gemini). Providers without
        a session model (openrouter, kimi, ollama) raise UnsupportedCapability if a
        non-None value is passed.

        Raises ProviderError on failure.
        """

    def exec(
        self,
        task: str,
        model: str | None = None,
        *,
        effort: str | int = "medium",
        tools: frozenset[str] = frozenset(),
        sandbox: str = "none",
        cwd: str | None = None,
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        resume_session_id: str | None = None,
        session_log: SessionLog | None = None,
        max_iterations: int | None = None,
        allow_completion_stretch: bool = False,
        write_validation: bool = True,
    ) -> CallResponse:
        """Multi-turn agent session with tool access.

        ``resume_session_id`` semantics match ``call()``.
        ``max_stall_sec`` is an optional no-output watchdog. CLI-backed
        providers should honor it; providers without a streaming subprocess
        may accept and ignore the value for API parity.

        Raises UnsupportedCapability if the provider cannot drive the
        requested tools, or cannot resume sessions when one is requested.
        Raises ProviderError on runtime failure.
        """


@runtime_checkable
class NativeReviewProvider(Protocol):
    """Optional provider capability for first-class code review.

    This intentionally lives outside ``Provider`` so generic chat/agent
    providers and user-defined shell providers do not need to implement a
    review entrypoint. Callers should check this protocol before invoking
    review mode.
    """

    name: ClassVar[str]
    default_model: ClassVar[str]
    supports_native_review: ClassVar[bool]

    def review_configured(self) -> tuple[bool, str | None]:
        """Return whether the provider's native review path is available."""

    def review(
        self,
        task: str,
        *,
        effort: str | int = "medium",
        cwd: str | None = None,
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        base: str | None = None,
        commit: str | None = None,
        uncommitted: bool = False,
        title: str | None = None,
    ) -> CallResponse:
        """Run the provider's native code-review mode.

        ``task`` is review guidance, not an engineering/editing brief.
        Providers must use read-only review functionality here; fixes belong
        in ``exec()``.
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
