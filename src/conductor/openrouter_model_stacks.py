"""Audited OpenRouter model stacks for task classes where auto is too vague."""

from __future__ import annotations

OPENROUTER_CODING_STACK_VERSION = "2026-05-04"

# Verified against OpenRouter's /models catalog on 2026-05-04. This is an
# explicit quality policy for repo-editing tool-use work: do not delegate these
# tasks to raw openrouter/auto, where upstream may choose cheap or flash models.
OPENROUTER_CODING_STACK_POLICY = (
    "Manual quality policy for repo-editing tool-use work. Catalog validation "
    "can prove availability/capabilities, but ordering still requires human "
    "judgment from coding benchmarks, dogfood runs, and provider reliability."
)

OPENROUTER_CODING_MODEL_EVIDENCE: dict[str, str] = {
    "openai/gpt-5.3-codex": "Primary coding-agent fallback; optimized for repo edits and tool use.",
    "openai/gpt-5.5": "Frontier general reasoning model kept as a broad coding fallback.",
    "openai/gpt-5.5-pro": "Max-effort OpenAI reasoning fallback for the hardest coding briefs.",
    "anthropic/claude-opus-4.7": "Max-effort cross-vendor reasoning fallback.",
    "anthropic/claude-sonnet-4.6": "Fast frontier coding fallback with strong tool-use behavior.",
    "google/gemini-3.1-pro-preview": (
        "High-context cross-vendor fallback; preview status requires monitoring."
    ),
    "qwen/qwen3-coder-plus": "Specialized coding model included for implementation breadth.",
    "deepseek/deepseek-v4-pro": "Specialized coding/reasoning fallback for stack diversity.",
}

OPENROUTER_CODING_HIGH: tuple[str, ...] = (
    "openai/gpt-5.3-codex",
    "openai/gpt-5.5",
    "anthropic/claude-sonnet-4.6",
    "google/gemini-3.1-pro-preview",
    "qwen/qwen3-coder-plus",
    "deepseek/deepseek-v4-pro",
)

OPENROUTER_CODING_MAX: tuple[str, ...] = (
    "openai/gpt-5.3-codex",
    "openai/gpt-5.5-pro",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.6",
    "google/gemini-3.1-pro-preview",
    "qwen/qwen3-coder-plus",
    "deepseek/deepseek-v4-pro",
)

# Review fallback stack — used when codex/claude native review are unavailable.
# Starts cheap on purpose: review is a high-volume path (every push through the
# Touchstone merge gate retries the cascade) and the coding stacks above cost
# ~20x more per equivalent review. See issue #501.
OPENROUTER_REVIEW_CHEAP: tuple[str, ...] = (
    "deepseek/deepseek-v4-pro",
    "moonshotai/kimi-k2.6",
    "qwen/qwen3-coder-plus",
)

# Coding-tool-use fallback stack. Used when both flat-rate subscriptions
# (codex CLI, claude CLI) are unavailable and the cascade has to spend
# metered budget. Ordering bias differs from review: put the
# coding-specialized model first, then the strong-reasoning model, then
# the long-context backstop for huge diffs. See issue #448 for the
# cost-shape argument; PR landed alongside the review-cascade fix in #502.
OPENROUTER_CODING_CHEAP: tuple[str, ...] = (
    "qwen/qwen3-coder-plus",
    "deepseek/deepseek-v4-pro",
    "moonshotai/kimi-k2.6",
)


def model_family(model: str) -> str:
    """Return the lowercased provider family prefix for a model slug."""
    head, sep, _tail = model.partition("/")
    if not sep or not head:
        return ""
    return head.lower()


def bump_family_to_end(models: tuple[str, ...], family: str) -> tuple[str, ...]:
    """Move the named model family to the end while preserving order."""
    normalized = family.strip().lower()
    if not normalized:
        return models

    keep: list[str] = []
    bumped: list[str] = []
    for model in models:
        if model_family(model) == normalized:
            bumped.append(model)
        else:
            keep.append(model)
    return tuple([*keep, *bumped])


def provider_family_hint(provider_id: str) -> str | None:
    """Return a model-family hint for a provider id when known."""
    mapping = {
        "codex": "openai",
        "claude": "anthropic",
        "gemini": "google",
    }
    return mapping.get(provider_id.strip().lower())


def openrouter_coding_stack(effort: str | int) -> tuple[str, ...]:
    """Return the OpenRouter model stack for tool-using coding work."""
    if effort == "max":
        return OPENROUTER_CODING_MAX
    return OPENROUTER_CODING_HIGH
