"""Auto-mode router — pick a provider for a task based on tags.

v0.1 router: rule-based, not LLM-based. For a task with tags T, a provider P
scores ``len(set(P.tags) & T)``. Tied scores break by a hardcoded priority
order (kimi, claude, codex, gemini, ollama) — the opinionated default the
project ships with. Users who want different priorities will configure them
via ``conductor init`` in Phase 5 (``~/.config/conductor/config.toml``).

The router never calls ``smoke()`` — that would burn a real API round-trip
on every ``--auto`` invocation. It filters on ``configured()`` only, which
is cheap (env-var/CLI presence check). Users who suspect a configured
provider is unhealthy run ``conductor smoke <id>`` manually.

The router also never swallows "no provider available" — if every
candidate is unconfigured, ``pick()`` raises ``NoConfiguredProvider``
rather than returning None, per the project's no-silent-failures rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from conductor.providers import (
    Provider,
    ProviderError,
    get_provider,
    known_providers,
)

# Opinionated v0.1 priority. Lower index wins on tie.
# - kimi first: cheap, long context, widely capable → sensible default for
#   most "just pick something" calls.
# - claude next: strongest reasoning when the task calls for it.
# - mistral: new, strong reasoning and function calling.
# - codex: parallel to claude but via Codex CLI.
# - gemini: long-context and web-search fallback.
# - ollama last: only when the others are unavailable (local-only, slower).
DEFAULT_PRIORITY: tuple[str, ...] = ("kimi", "claude", "mistral", "codex", "gemini", "ollama")


class NoConfiguredProvider(ProviderError):
    """Raised when no provider in the registry is configured enough to call."""


@dataclass(frozen=True)
class RouteDecision:
    """Why the router picked what it picked.

    Surfaced via ``--json`` so consumers can log/debug routing behavior.
    """

    provider: str
    score: int
    task_tags: tuple[str, ...]
    matched_tags: tuple[str, ...]
    candidates_considered: tuple[str, ...]
    candidates_skipped: tuple[tuple[str, str], ...]  # (provider, reason)


def pick(
    task_tags: Optional[list[str]] = None,
    *,
    priority: tuple[str, ...] = DEFAULT_PRIORITY,
) -> tuple[Provider, RouteDecision]:
    """Pick the best-scoring configured provider for ``task_tags``.

    Resolution:
      1. Iterate registry in ``priority`` order (unknown registry entries
         tacked on at the end, alphabetized).
      2. Drop providers where ``configured()`` returns False.
      3. Score each remaining provider by tag-overlap with the task.
      4. Return the highest scorer; ties break by priority order.

    Returns (provider, decision). Raises NoConfiguredProvider if every
    provider is unconfigured.
    """
    task_tag_set = set(task_tags or [])

    order = [p for p in priority if p in set(known_providers())]
    order += sorted(set(known_providers()) - set(order))

    considered: list[tuple[str, int, tuple[str, ...]]] = []
    skipped: list[tuple[str, str]] = []

    for name in order:
        provider = get_provider(name)
        ok, reason = provider.configured()
        if not ok:
            skipped.append((name, reason or "not configured"))
            continue
        matched = tuple(sorted(task_tag_set & set(provider.tags)))
        considered.append((name, len(matched), matched))

    if not considered:
        raise NoConfiguredProvider(
            "no provider is configured. Configure at least one via its "
            "auth mechanism (e.g. `claude login`, `codex login`, `gemini`, "
            "`ollama serve`, or `CLOUDFLARE_API_TOKEN`+`CLOUDFLARE_ACCOUNT_ID`). "
            f"Skipped: {skipped}"
        )

    # max score, then earliest priority-index on ties.
    priority_index = {name: i for i, name in enumerate(order)}
    considered.sort(key=lambda entry: (-entry[1], priority_index[entry[0]]))
    winner_name, winner_score, winner_matched = considered[0]

    return get_provider(winner_name), RouteDecision(
        provider=winner_name,
        score=winner_score,
        task_tags=tuple(sorted(task_tag_set)),
        matched_tags=winner_matched,
        candidates_considered=tuple(name for name, _, _ in considered),
        candidates_skipped=tuple(skipped),
    )
