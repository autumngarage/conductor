"""Auto-mode router — pick a provider for a task.

v0.2 router. Axes:

  - ``tags``        — capability tags for soft matching (code-review, long-context, ...)
  - ``prefer``      — which dimension dominates scoring: best | cheapest | fastest | balanced
  - ``effort``      — thinking-depth dial applied to the chosen provider
  - ``tools``       — hard filter: provider.supported_tools ⊇ requested
  - ``sandbox``     — hard filter: sandbox ∈ provider.supported_sandboxes
  - ``exclude``     — blacklist; router must never pick these

Scoring pipeline:

  1. Filter on ``configured()`` + ``supported_tools ⊇ tools`` +
     ``sandbox ∈ supported_sandboxes`` + ``id ∉ exclude`` + health-ok.
  2. Score surviving candidates per ``prefer`` mode.
  3. Break ties via DEFAULT_PRIORITY.
  4. Return ``(provider, RouteDecision)``. RouteDecision always includes
     the full ranking so callers can log / explain the choice.

The router never calls ``smoke()`` — a real API round-trip per invocation
would be prohibitive. It filters on ``configured()`` (cheap) plus
session-local health tracking. Users run ``conductor smoke`` manually
to force a real health check.

Health tracking is session-local; there is no persistent state file in
v0.2. A rate-limit observed in one ``conductor`` invocation is forgotten
when the process exits. This is intentional — stateful cross-session
health would require a cache directory, which is deferred.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Literal

from conductor.providers import (
    TIER_RANK,
    Provider,
    ProviderError,
    get_provider,
    known_providers,
    resolve_effort_tokens,
)

# --------------------------------------------------------------------------- #
# Priority (v0.1 carry-over, tiebreak only).
# --------------------------------------------------------------------------- #
DEFAULT_PRIORITY: tuple[str, ...] = ("kimi", "claude", "mistral", "codex", "gemini", "ollama")

PreferMode = Literal["best", "cheapest", "fastest", "balanced"]
VALID_PREFER_MODES: tuple[str, ...] = ("best", "cheapest", "fastest", "balanced")


class NoConfiguredProvider(ProviderError):  # noqa: N818  — public API name; preserved from v0.1
    """Raised when no provider in the registry is configured enough to call."""


class InvalidRouterRequest(ProviderError):  # noqa: N818  — public API name, symmetry with NoConfiguredProvider
    """Raised when the caller passes an invalid combination (e.g. unknown prefer mode)."""


# --------------------------------------------------------------------------- #
# Session-local health tracking.
# --------------------------------------------------------------------------- #

_RATE_LIMIT_COOLDOWN_SEC = 60.0


@dataclass
class _HealthState:
    last_rate_limited_at: float | None = None
    last_auth_failed_at: float | None = None
    recent_outcomes: list[str] = field(default_factory=list)  # success / 5xx / timeout


_HEALTH: dict[str, _HealthState] = {}


def _health(name: str) -> _HealthState:
    if name not in _HEALTH:
        _HEALTH[name] = _HealthState()
    return _HEALTH[name]


def mark_rate_limited(name: str) -> None:
    _health(name).last_rate_limited_at = time.monotonic()


def mark_auth_failed(name: str) -> None:
    _health(name).last_auth_failed_at = time.monotonic()


def mark_outcome(name: str, outcome: str) -> None:
    h = _health(name)
    h.recent_outcomes.append(outcome)
    if len(h.recent_outcomes) > 20:
        h.recent_outcomes = h.recent_outcomes[-20:]


def reset_health(name: str | None = None) -> None:
    """Test helper; reset one provider's health or everything."""
    if name is None:
        _HEALTH.clear()
    elif name in _HEALTH:
        del _HEALTH[name]


def _health_filter(name: str) -> str | None:
    """Return None if the provider passes, else a skip reason."""
    h = _HEALTH.get(name)
    if h is None:
        return None
    now = time.monotonic()
    if h.last_rate_limited_at and (now - h.last_rate_limited_at) < _RATE_LIMIT_COOLDOWN_SEC:
        wait = int(_RATE_LIMIT_COOLDOWN_SEC - (now - h.last_rate_limited_at))
        return f"rate-limited {int(now - h.last_rate_limited_at)}s ago (cooldown: {wait}s)"
    if h.last_auth_failed_at:
        return "auth failed earlier this session"
    return None


def _health_penalty(name: str) -> float:
    """Return a soft penalty in [0, 1) for degraded providers.

    Hard failures (rate-limit, auth) filter upstream; this only deprioritizes
    providers with a noisy recent-outcome window. Default 0.
    """
    h = _HEALTH.get(name)
    if h is None or len(h.recent_outcomes) < 5:
        return 0.0
    failures = sum(1 for o in h.recent_outcomes if o in {"5xx", "timeout"})
    ratio = failures / len(h.recent_outcomes)
    if ratio > 0.30:
        return min(0.5, ratio)
    return 0.0


# --------------------------------------------------------------------------- #
# RouteDecision — the explainable scoring output.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RankedCandidate:
    """One provider's full scoring breakdown for a given routing request.

    ``unconfigured_reason`` is set only on shadow candidates (see ``pick``'s
    ``shadow`` parameter) — providers that would have been scored but failed
    ``configured()``. Real (winnable) candidates leave it ``None``.
    """

    name: str
    tier: str
    tier_rank: int
    matched_tags: tuple[str, ...]
    tag_score: int
    cost_score: float           # estimated total cost/1k at requested effort
    latency_ms: int
    health_penalty: float
    combined_score: float       # the key used by the current prefer mode (higher=better)
    unconfigured_reason: str | None = None


@dataclass(frozen=True)
class RouteDecision:
    """Why the router picked what it picked — the explainable output.

    ``unconfigured_shadow`` is populated when ``pick(..., shadow=True)`` is
    used; it ranks providers that failed ``configured()`` against the same
    scoring rules as the winnable candidates. The CLI uses this to tell the
    user when an unconfigured provider would have been a better fit than the
    one auto-mode actually picked.
    """

    provider: str
    prefer: str
    effort: str | int
    thinking_budget: int
    tier: str
    task_tags: tuple[str, ...]
    matched_tags: tuple[str, ...]
    tools_requested: tuple[str, ...]
    sandbox: str
    ranked: tuple[RankedCandidate, ...]           # descending by combined_score
    candidates_skipped: tuple[tuple[str, str], ...]  # (name, reason)
    unconfigured_shadow: tuple[RankedCandidate, ...] = ()  # descending; never winners

    # Legacy fields retained for v0.1 callers that destructured these.
    @property
    def score(self) -> int:
        return self.ranked[0].tag_score if self.ranked else 0

    @property
    def candidates_considered(self) -> tuple[str, ...]:
        return tuple(r.name for r in self.ranked)


# --------------------------------------------------------------------------- #
# pick() — the main entrypoint.
# --------------------------------------------------------------------------- #


def _score_one(
    name: str,
    provider: Provider,
    *,
    task_tag_set: set[str],
    prefer: str,
    effort: str | int,
    priority_index: int,
    unconfigured_reason: str | None = None,
) -> RankedCandidate:
    """Score a single provider under the active prefer mode.

    Shared between the main winnable-candidate loop and the shadow pass that
    re-scores unconfigured providers. ``unconfigured_reason`` is plumbed
    through so shadow candidates carry the configured() failure text into
    the decision; winnable candidates leave it ``None``.

    Health penalty is suppressed for shadow candidates — there's no health
    history for a provider the caller couldn't actually invoke, so applying
    a penalty would be noise.
    """
    matched = tuple(sorted(task_tag_set & set(provider.tags)))
    tag_score = len(matched)
    tier_rank = TIER_RANK.get(provider.quality_tier, 0)

    # Expected total cost per 1k tokens at the requested effort.
    # We don't know the actual token count yet; use a per-request estimate
    # assuming ~4k input and ~500 output (typical review sizes).
    thinking_tokens = resolve_effort_tokens(effort, provider.effort_to_thinking)
    cost_estimate = (
        provider.cost_per_1k_in * 4
        + provider.cost_per_1k_out * 0.5
        + provider.cost_per_1k_thinking * (thinking_tokens / 1_000)
    )

    penalty = 0.0 if unconfigured_reason is not None else _health_penalty(name)
    combined = _combined_score(
        prefer=prefer,
        tag_score=tag_score,
        tier_rank=tier_rank,
        cost_estimate=cost_estimate,
        latency_ms=provider.typical_p50_ms,
        priority_index=priority_index,
    ) * (1 - penalty)

    return RankedCandidate(
        name=name,
        tier=provider.quality_tier,
        tier_rank=tier_rank,
        matched_tags=matched,
        tag_score=tag_score,
        cost_score=cost_estimate,
        latency_ms=provider.typical_p50_ms,
        health_penalty=penalty,
        combined_score=combined,
        unconfigured_reason=unconfigured_reason,
    )


def pick(
    task_tags: list[str] | None = None,
    *,
    prefer: str = "balanced",
    effort: str | int = "medium",
    tools: frozenset[str] | set[str] | list[str] | None = None,
    sandbox: str = "none",
    exclude: frozenset[str] | set[str] | list[str] | None = None,
    priority: tuple[str, ...] = DEFAULT_PRIORITY,
    shadow: bool = False,
) -> tuple[Provider, RouteDecision]:
    """Pick the best provider for ``task_tags`` under the given preferences.

    See module docstring for the full pipeline. Default ``prefer="balanced"``
    reproduces v0.1 behavior (pure tag-overlap + priority tiebreak) for
    backward compatibility.

    ``shadow``: when True, providers that would otherwise be eligible but
    failed ``configured()`` are re-scored and returned in
    ``decision.unconfigured_shadow``. They never become the winner — this
    is purely an explainability hook so callers can tell users *"auto would
    have preferred X, but X isn't installed/authed"*. Off by default to
    avoid surprising existing callers (Sentinel, Touchstone) and the CLI's
    `--with` path that doesn't need it.
    """
    if prefer not in VALID_PREFER_MODES:
        raise InvalidRouterRequest(
            f"prefer={prefer!r} is not a valid mode. "
            f"Use one of: {list(VALID_PREFER_MODES)}. "
            f"Did you mean {_fuzzy_suggest(prefer, VALID_PREFER_MODES)!r}?"
        )

    task_tag_set = set(task_tags or [])
    tools_set = frozenset(tools or ())
    exclude_set = frozenset(exclude or ())

    if tools_set - {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}:
        unknown = sorted(tools_set - {"Read", "Grep", "Glob", "Edit", "Write", "Bash"})
        raise InvalidRouterRequest(
            f"unknown tool(s) requested: {unknown}. "
            "Known: Read, Grep, Glob, Edit, Write, Bash."
        )

    # Deterministic provider iteration order: priority first, then alphabetical.
    order = [p for p in priority if p in set(known_providers())]
    order += sorted(set(known_providers()) - set(order))
    priority_index = {name: i for i, name in enumerate(order)}

    ranked: list[RankedCandidate] = []
    skipped: list[tuple[str, str]] = []
    unconfigured: dict[str, str] = {}  # name → reason; only configured() failures

    for name in order:
        if name in exclude_set:
            skipped.append((name, "excluded by caller"))
            continue

        provider = get_provider(name)

        ok, reason = provider.configured()
        if not ok:
            failure = reason or "not configured"
            skipped.append((name, failure))
            # Hard capability filters still apply to shadow candidates — we
            # don't want to suggest "would prefer X" for a provider X that
            # couldn't satisfy the caller's tools/sandbox even if installed.
            if name not in exclude_set:
                if tools_set and not tools_set.issubset(provider.supported_tools):
                    pass
                elif sandbox not in provider.supported_sandboxes and sandbox != "none":
                    pass
                else:
                    unconfigured[name] = failure
            continue

        # Hard capability filters.
        if tools_set and not tools_set.issubset(provider.supported_tools):
            missing = sorted(tools_set - provider.supported_tools)
            skipped.append((name, f"does not support tools: {missing}"))
            continue
        if sandbox not in provider.supported_sandboxes and sandbox != "none":
            # "none" is always accepted (no-sandbox means no requirement).
            skipped.append((name, f"does not support sandbox={sandbox!r}"))
            continue

        # Session-local health filter.
        health_reason = _health_filter(name)
        if health_reason is not None:
            skipped.append((name, health_reason))
            continue

        ranked.append(
            _score_one(
                name,
                provider,
                task_tag_set=task_tag_set,
                prefer=prefer,
                effort=effort,
                priority_index=priority_index[name],
            )
        )

    if not ranked:
        raise NoConfiguredProvider(
            "no provider satisfies the routing request. "
            f"prefer={prefer!r} tools={sorted(tools_set)} sandbox={sandbox!r} "
            f"exclude={sorted(exclude_set)}. Skipped: {skipped}"
        )

    # Sort descending by combined_score; tiebreak by priority_index ascending.
    ranked.sort(key=lambda c: (-c.combined_score, priority_index[c.name]))
    winner = ranked[0]
    winner_provider = get_provider(winner.name)

    thinking_budget = resolve_effort_tokens(effort, winner_provider.effort_to_thinking)

    shadow_ranked: tuple[RankedCandidate, ...] = ()
    if shadow and unconfigured:
        shadow_list = [
            _score_one(
                name,
                get_provider(name),
                task_tag_set=task_tag_set,
                prefer=prefer,
                effort=effort,
                priority_index=priority_index[name],
                unconfigured_reason=reason,
            )
            for name, reason in unconfigured.items()
        ]
        shadow_list.sort(key=lambda c: (-c.combined_score, priority_index[c.name]))
        shadow_ranked = tuple(shadow_list)

    decision = RouteDecision(
        provider=winner.name,
        prefer=prefer,
        effort=effort,
        thinking_budget=thinking_budget,
        tier=winner.tier,
        task_tags=tuple(sorted(task_tag_set)),
        matched_tags=winner.matched_tags,
        tools_requested=tuple(sorted(tools_set)),
        sandbox=sandbox,
        ranked=tuple(ranked),
        candidates_skipped=tuple(skipped),
        unconfigured_shadow=shadow_ranked,
    )
    return winner_provider, decision


# --------------------------------------------------------------------------- #
# Scoring — convert each prefer mode to a single descending-is-best number.
# --------------------------------------------------------------------------- #


def _combined_score(
    *,
    prefer: str,
    tag_score: int,
    tier_rank: int,
    cost_estimate: float,
    latency_ms: int,
    priority_index: int,
) -> float:
    """Return a single score where higher is better.

    Each prefer mode encodes its own primary/secondary:
      - best:     primary tier, secondary tag_score
      - cheapest: primary -cost,  secondary tier
      - fastest:  primary -latency, secondary tier
      - balanced: primary tag_score, secondary -priority_index (v0.1 behavior)
    """
    if prefer == "best":
        # tier dominates (1000× magnitude); tags are fine-grained secondary.
        return tier_rank * 1_000 + tag_score
    if prefer == "cheapest":
        # Negated cost: smaller cost → bigger score. Scale by 1e6 for precision.
        # Secondary: tier_rank for quality floor.
        return -cost_estimate * 1_000_000 + tier_rank
    if prefer == "fastest":
        # Negated latency. Secondary: tier_rank.
        return -latency_ms + tier_rank * 100
    # balanced (v0.1 carry-over): tag overlap, then priority index (lower-is-earlier).
    return tag_score * 1_000 - priority_index


def _fuzzy_suggest(query: str, options: tuple[str, ...]) -> str:
    """Tiny no-dep fuzzy match for fix-it hints."""
    from difflib import get_close_matches

    match = get_close_matches(query, options, n=1, cutoff=0.3)
    return match[0] if match else options[0]
