"""Internal aggregation for delegation routing and token-efficiency reports."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from statistics import median
from typing import Any

REPORT_SCHEMA_VERSION = 1


@dataclass
class _Bucket:
    provider: str
    model: str | None = None
    calls: int = 0
    ok: int = 0
    non_ok: int = 0
    durations_ms: list[int] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cached_tokens: int = 0
    cost_usd: float = 0.0
    cost_seen: bool = False
    tags: Counter[str] = field(default_factory=Counter)
    models: Counter[str] = field(default_factory=Counter)

    def add(self, event: dict[str, Any]) -> None:
        self.calls += 1
        if event.get("status") == "ok":
            self.ok += 1
        else:
            self.non_ok += 1

        duration = _int_or_none(event.get("duration_ms"))
        if duration is not None:
            self.durations_ms.append(duration)
        self.input_tokens += _int_or_zero(event.get("input_tokens"))
        self.output_tokens += _int_or_zero(event.get("output_tokens"))
        self.thinking_tokens += _int_or_zero(event.get("thinking_tokens"))
        self.cached_tokens += _int_or_zero(event.get("cached_tokens"))

        cost = event.get("cost_usd")
        if isinstance(cost, (int, float)):
            self.cost_usd += float(cost)
            self.cost_seen = True

        model = event.get("model")
        if isinstance(model, str) and model:
            self.models[model] += 1
        for tag in event.get("tags") or []:
            if isinstance(tag, str) and tag:
                self.tags[tag] += 1

    def payload(self) -> dict[str, Any]:
        total_tokens = self.input_tokens + self.output_tokens + self.thinking_tokens
        median_duration_ms = int(median(self.durations_ms)) if self.durations_ms else None
        return {
            "provider": self.provider,
            "model": self.model,
            "calls": self.calls,
            "ok": self.ok,
            "non_ok": self.non_ok,
            "success_rate": _ratio(self.ok, self.calls),
            "median_duration_ms": median_duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cached_tokens": self.cached_tokens,
            "total_tokens": total_tokens,
            "output_tokens_per_1k_input": _per(self.output_tokens, self.input_tokens, 1000),
            "input_tokens_per_output_token": _per(self.input_tokens, self.output_tokens, 1),
            "thinking_tokens_per_output_token": _per(
                self.thinking_tokens, self.output_tokens, 1
            ),
            "ms_per_output_token": _per(sum(self.durations_ms), self.output_tokens, 1),
            "cost_usd": self.cost_usd if self.cost_seen else None,
            "cost_per_1k_total_tokens": (
                _per(self.cost_usd, total_tokens, 1000) if self.cost_seen else None
            ),
            "cost_per_1k_output_tokens": (
                _per(self.cost_usd, self.output_tokens, 1000) if self.cost_seen else None
            ),
            "models": [
                {"model": model, "calls": count}
                for model, count in self.models.most_common()
            ],
            "top_tags": [
                {"tag": tag, "calls": count} for tag, count in self.tags.most_common(8)
            ],
        }


def build_delegation_report(
    events: list[dict[str, Any]],
    *,
    since: str | None,
    tag: str | None = None,
) -> dict[str, Any]:
    filtered = [
        event
        for event in events
        if tag is None or tag in [candidate for candidate in event.get("tags") or []]
    ]
    providers: dict[str, _Bucket] = {}
    models: dict[tuple[str, str], _Bucket] = {}
    commands: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    route_selected: Counter[str] = Counter()
    route_fallbacks: Counter[str] = Counter()

    for event in filtered:
        provider = str(event.get("provider") or "-")
        model = event.get("model")
        provider_bucket = providers.setdefault(provider, _Bucket(provider=provider))
        provider_bucket.add(event)
        if isinstance(model, str) and model:
            model_bucket = models.setdefault(
                (provider, model),
                _Bucket(provider=provider, model=model),
            )
            model_bucket.add(event)
        commands[str(event.get("command") or "-")] += 1
        statuses[str(event.get("status") or "-")] += 1
        route = event.get("route")
        if isinstance(route, dict):
            selected = route.get("provider")
            if isinstance(selected, str) and selected:
                route_selected[selected] += 1
        chain_key = _fallback_chain_key(event, provider=provider)
        if chain_key is not None:
            route_fallbacks[chain_key] += 1

    provider_rows = sorted(
        (bucket.payload() for bucket in providers.values()),
        key=lambda row: (-int(row["calls"]), str(row["provider"])),
    )
    model_rows = sorted(
        (bucket.payload() for bucket in models.values()),
        key=lambda row: (-int(row["calls"]), str(row["provider"]), str(row["model"])),
    )
    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "window": {
            "since": since,
            "events": len(filtered),
            "events_before_tag_filter": len(events),
            "tag_filter": tag,
        },
        "commands": dict(sorted(commands.items())),
        "statuses": dict(sorted(statuses.items())),
        "route_selected": dict(sorted(route_selected.items())),
        "route_fallbacks": dict(sorted(route_fallbacks.items())),
        "providers": provider_rows,
        "models": model_rows,
    }


def _fallback_chain_key(event: dict[str, Any], *, provider: str) -> str | None:
    """Return the full attempted-provider chain as a "a->b->c" key, or None.

    Prefers the structured ``fallback_chain`` list (providers that failed
    before the completed one) so multi-hop fallback paths are not collapsed
    to primary→final. Falls back to the legacy ``route.provider`` heuristic
    when that field is absent (older v1/early-v2 events).
    """
    chain = event.get("fallback_chain")
    if isinstance(chain, list) and chain:
        attempted = [item for item in chain if isinstance(item, str) and item]
        if attempted:
            return "->".join([*attempted, provider])
    route = event.get("route")
    if isinstance(route, dict):
        selected = route.get("provider")
        if isinstance(selected, str) and selected and selected != provider:
            return f"{selected}->{provider}"
    return None


def _int_or_none(value: object) -> int | None:
    return value if isinstance(value, int) else None


def _int_or_zero(value: object) -> int:
    return value if isinstance(value, int) else 0


def _ratio(numerator: int | float, denominator: int | float) -> float | None:
    if denominator == 0:
        return None
    return round(float(numerator) / float(denominator), 4)


def _per(numerator: int | float, denominator: int | float, multiplier: int) -> float | None:
    if denominator == 0:
        return None
    return round((float(numerator) / float(denominator)) * multiplier, 4)
