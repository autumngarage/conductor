"""Bounded carry-over context for provider fallback chains."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

MAX_FALLBACK_DETAIL_CHARS = 320
MAX_FALLBACK_TEXT_CHARS = 1_500


@dataclass(frozen=True)
class FallbackAttempt:
    provider: str
    status: str
    detail: str
    elapsed_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    artifacts: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FallbackSummary:
    attempts: tuple[FallbackAttempt, ...] = field(default_factory=tuple)
    max_chars: int = MAX_FALLBACK_TEXT_CHARS

    def with_attempt(self, attempt: FallbackAttempt) -> FallbackSummary:
        return FallbackSummary((*self.attempts, attempt), max_chars=self.max_chars)

    def render(self) -> str:
        if not self.attempts:
            return ""
        lines = [
            "Conductor fallback context:",
            "You are a later provider attempt. Use the original brief as the source of truth.",
            "Do not replay prior raw transcripts; use this bounded outcome summary only.",
        ]
        for index, attempt in enumerate(self.attempts, start=1):
            detail = _one_line(attempt.detail)[:MAX_FALLBACK_DETAIL_CHARS]
            line = f"{index}. {attempt.provider}: {attempt.status}"
            if detail:
                line += f" - {detail}"
            if attempt.artifacts:
                line += " artifacts=" + ",".join(attempt.artifacts[:8])
            lines.append(line)
        rendered = "\n".join(lines)
        if len(rendered) <= self.max_chars:
            return rendered
        return rendered[: self.max_chars - 3].rstrip() + "..."

    def apply_to_task(self, task: str) -> str:
        rendered = self.render()
        if not rendered:
            return task
        return f"{task.rstrip()}\n\n---\n{rendered}\n"

    def cumulative_usage(self) -> dict[str, int | float | None]:
        input_tokens = _sum_optional_int(attempt.input_tokens for attempt in self.attempts)
        output_tokens = _sum_optional_int(attempt.output_tokens for attempt in self.attempts)
        costs = [attempt.cost_usd for attempt in self.attempts if attempt.cost_usd is not None]
        return {
            "chain_input_tokens": input_tokens,
            "chain_output_tokens": output_tokens,
            "chain_cost_usd": sum(costs) if costs else None,
            "attempt_count": len(self.attempts),
        }

    def to_dict(self) -> dict:
        return {
            "attempts": [attempt.to_dict() for attempt in self.attempts],
            **self.cumulative_usage(),
        }


def _one_line(value: str) -> str:
    return " ".join(value.split())


def _sum_optional_int(values) -> int | None:
    collected = [
        value for value in values if isinstance(value, int) and not isinstance(value, bool)
    ]
    return sum(collected) if collected else None
