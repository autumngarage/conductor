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


def openrouter_coding_stack(effort: str | int) -> tuple[str, ...]:
    """Return the OpenRouter model stack for tool-using coding work."""
    if effort == "max":
        return OPENROUTER_CODING_MAX
    return OPENROUTER_CODING_HIGH
