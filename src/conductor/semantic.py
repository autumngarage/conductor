"""Deterministic semantic routing policies for ``conductor ask``.

The classic ``call`` / ``exec`` / ``review`` commands expose provider-level
knobs. This module owns the higher-level ``kind × effort`` matrix so callers
can say what the work *is* while Conductor keeps the default model/provider
stack in one auditable place.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from conductor.openrouter_model_stacks import (
    OPENROUTER_CODING_HIGH,
    OPENROUTER_CODING_MAX,
)

SemanticKind = Literal["research", "code", "review", "council"]
SemanticMode = Literal["call", "exec", "review", "council"]
EffortBucket = Literal["minimal", "low", "medium", "high", "max"]

SEMANTIC_KINDS: tuple[str, ...] = ("research", "code", "review", "council")
EFFORT_BUCKETS: tuple[EffortBucket, ...] = ("minimal", "low", "medium", "high", "max")

EFFORT_TOKEN_BUCKETS: tuple[tuple[int, EffortBucket], ...] = (
    (0, "minimal"),
    (2_000, "low"),
    (8_000, "medium"),
    (24_000, "high"),
)
DEFAULT_HIGH_TOKEN_BUCKET: EffortBucket = "max"

OPENROUTER_GEMINI_FLASH = "~google/gemini-flash-latest"
OPENROUTER_GEMINI_PRO = "~google/gemini-pro-latest"
OPENROUTER_KIMI = "~moonshotai/kimi-latest"
OPENROUTER_OPENAI_MINI = "~openai/gpt-mini-latest"
OPENROUTER_OPENAI = "~openai/gpt-latest"
OPENROUTER_CLAUDE_HAIKU = "~anthropic/claude-haiku-latest"
OPENROUTER_CLAUDE_SONNET = "~anthropic/claude-sonnet-latest"
OPENROUTER_DEEPSEEK_PRO = "deepseek/deepseek-v4-pro"
OPENROUTER_QWEN_CODE = "qwen/qwen3.6-max-preview"


@dataclass(frozen=True)
class SemanticCandidate:
    """One ordered provider candidate in a semantic policy."""

    provider: str
    models: tuple[str, ...] = ()

    def label(self) -> str:
        if not self.models:
            return self.provider
        return f"{self.provider}:{','.join(self.models)}"


@dataclass(frozen=True)
class SemanticPlan:
    """Resolved default plan for one semantic kind and effort bucket."""

    kind: SemanticKind
    effort_bucket: EffortBucket
    mode: SemanticMode
    candidates: tuple[SemanticCandidate, ...]
    tags: tuple[str, ...] = ()
    prefer: str = "balanced"
    tools: frozenset[str] = frozenset()
    sandbox: str = "none"
    council_member_models: tuple[str, ...] = ()
    council_synthesis_models: tuple[str, ...] = ()


_RESEARCH: dict[EffortBucket, SemanticPlan] = {
    "minimal": SemanticPlan(
        kind="research",
        effort_bucket="minimal",
        mode="call",
        prefer="balanced",
        tags=("research", "long-context", "cheap"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
    "low": SemanticPlan(
        kind="research",
        effort_bucket="low",
        mode="call",
        prefer="balanced",
        tags=("research", "long-context", "cheap"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
    "medium": SemanticPlan(
        kind="research",
        effort_bucket="medium",
        mode="call",
        prefer="balanced",
        tags=("research", "long-context", "thinking"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
    "high": SemanticPlan(
        kind="research",
        effort_bucket="high",
        mode="call",
        prefer="best",
        tags=("research", "long-context", "strong-reasoning"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
    "max": SemanticPlan(
        kind="research",
        effort_bucket="max",
        mode="call",
        prefer="best",
        tags=("research", "long-context", "strong-reasoning"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
}


_CODE_EXEC_TOOLS = frozenset({"Read", "Grep", "Glob", "Edit", "Write", "Bash"})

_CODE: dict[EffortBucket, SemanticPlan] = {
    "minimal": SemanticPlan(
        kind="code",
        effort_bucket="minimal",
        mode="call",
        prefer="balanced",
        tags=("code", "cheap"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
    "low": SemanticPlan(
        kind="code",
        effort_bucket="low",
        mode="call",
        prefer="balanced",
        tags=("code", "cheap"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
    "medium": SemanticPlan(
        kind="code",
        effort_bucket="medium",
        mode="call",
        prefer="balanced",
        tags=("code", "thinking"),
        candidates=(
            SemanticCandidate("openrouter"),
            SemanticCandidate("ollama"),
        ),
    ),
    "high": SemanticPlan(
        kind="code",
        effort_bucket="high",
        mode="exec",
        prefer="best",
        tags=("code", "tool-use", "strong-reasoning"),
        tools=_CODE_EXEC_TOOLS,
        candidates=(
            SemanticCandidate("codex"),
            SemanticCandidate("openrouter", OPENROUTER_CODING_HIGH),
            SemanticCandidate("ollama"),
        ),
    ),
    "max": SemanticPlan(
        kind="code",
        effort_bucket="max",
        mode="exec",
        prefer="best",
        tags=("code", "tool-use", "strong-reasoning"),
        tools=_CODE_EXEC_TOOLS,
        candidates=(
            SemanticCandidate("codex"),
            SemanticCandidate("openrouter", OPENROUTER_CODING_MAX),
            SemanticCandidate("ollama"),
        ),
    ),
}


_REVIEW: dict[EffortBucket, SemanticPlan] = {
    bucket: SemanticPlan(
        kind="review",
        effort_bucket=bucket,
        mode="review",
        prefer="best",
        tags=("code-review",),
        candidates=(
            SemanticCandidate("codex"),
            SemanticCandidate("claude"),
            SemanticCandidate("gemini"),
        ),
    )
    for bucket in EFFORT_BUCKETS
}


_COUNCIL_LOW = (
    OPENROUTER_GEMINI_FLASH,
    OPENROUTER_OPENAI_MINI,
)
_COUNCIL_MEDIUM = (
    OPENROUTER_GEMINI_PRO,
    OPENROUTER_KIMI,
    OPENROUTER_DEEPSEEK_PRO,
)
_COUNCIL_HIGH = (
    OPENROUTER_GEMINI_PRO,
    OPENROUTER_CLAUDE_SONNET,
    OPENROUTER_OPENAI,
    OPENROUTER_DEEPSEEK_PRO,
    OPENROUTER_QWEN_CODE,
)

_COUNCIL: dict[EffortBucket, SemanticPlan] = {
    "minimal": SemanticPlan(
        kind="council",
        effort_bucket="minimal",
        mode="council",
        prefer="cheapest",
        tags=("council", "thinking"),
        candidates=(SemanticCandidate("openrouter", _COUNCIL_LOW),),
        council_member_models=_COUNCIL_LOW,
        council_synthesis_models=(OPENROUTER_GEMINI_FLASH, OPENROUTER_OPENAI_MINI),
    ),
    "low": SemanticPlan(
        kind="council",
        effort_bucket="low",
        mode="council",
        prefer="cheapest",
        tags=("council", "thinking"),
        candidates=(SemanticCandidate("openrouter", _COUNCIL_LOW),),
        council_member_models=_COUNCIL_LOW,
        council_synthesis_models=(OPENROUTER_GEMINI_FLASH, OPENROUTER_OPENAI_MINI),
    ),
    "medium": SemanticPlan(
        kind="council",
        effort_bucket="medium",
        mode="council",
        prefer="balanced",
        tags=("council", "thinking", "strong-reasoning"),
        candidates=(SemanticCandidate("openrouter", _COUNCIL_MEDIUM),),
        council_member_models=_COUNCIL_MEDIUM,
        council_synthesis_models=(OPENROUTER_GEMINI_PRO, OPENROUTER_OPENAI),
    ),
    "high": SemanticPlan(
        kind="council",
        effort_bucket="high",
        mode="council",
        prefer="best",
        tags=("council", "thinking", "strong-reasoning"),
        candidates=(SemanticCandidate("openrouter", _COUNCIL_HIGH),),
        council_member_models=_COUNCIL_HIGH,
        council_synthesis_models=(OPENROUTER_OPENAI, OPENROUTER_CLAUDE_SONNET),
    ),
    "max": SemanticPlan(
        kind="council",
        effort_bucket="max",
        mode="council",
        prefer="best",
        tags=("council", "thinking", "strong-reasoning"),
        candidates=(SemanticCandidate("openrouter", _COUNCIL_HIGH),),
        council_member_models=_COUNCIL_HIGH,
        council_synthesis_models=(OPENROUTER_OPENAI, OPENROUTER_CLAUDE_SONNET),
    ),
}


DEFAULT_SEMANTIC_MATRIX: dict[SemanticKind, dict[EffortBucket, SemanticPlan]] = {
    "research": _RESEARCH,
    "code": _CODE,
    "review": _REVIEW,
    "council": _COUNCIL,
}


def effort_bucket(effort: str | int) -> EffortBucket:
    """Map symbolic or token-budget effort onto the policy matrix."""
    if isinstance(effort, str):
        if effort not in EFFORT_BUCKETS:
            raise ValueError(f"unknown effort bucket {effort!r}")
        return effort  # type: ignore[return-value]

    for ceiling, bucket in EFFORT_TOKEN_BUCKETS:
        if effort <= ceiling:
            return bucket
    return DEFAULT_HIGH_TOKEN_BUCKET


def plan_for(kind: str, effort: str | int) -> SemanticPlan:
    """Return the deterministic default semantic plan."""
    if kind not in DEFAULT_SEMANTIC_MATRIX:
        raise ValueError(
            f"unknown semantic kind {kind!r}; expected one of {list(SEMANTIC_KINDS)}"
        )
    bucket = effort_bucket(effort)
    return DEFAULT_SEMANTIC_MATRIX[kind][bucket]  # type: ignore[index]


def with_candidate_override(
    plan: SemanticPlan,
    *,
    provider: str | None = None,
    models: tuple[str, ...] = (),
) -> SemanticPlan:
    """Return ``plan`` with a per-call provider/model override applied."""
    if provider is None and not models:
        return plan

    target_provider = provider or plan.candidates[0].provider
    candidate_models = models
    if target_provider == "openrouter" and not candidate_models:
        candidate_models = plan.candidates[0].models

    return SemanticPlan(
        kind=plan.kind,
        effort_bucket=plan.effort_bucket,
        mode=plan.mode,
        candidates=(SemanticCandidate(target_provider, candidate_models),),
        tags=plan.tags,
        prefer=plan.prefer,
        tools=plan.tools,
        sandbox=plan.sandbox,
        council_member_models=(
            candidate_models
            if plan.kind == "council" and candidate_models
            else plan.council_member_models
        ),
        council_synthesis_models=(
            candidate_models[:1]
            if plan.kind == "council" and candidate_models
            else plan.council_synthesis_models
        ),
    )
