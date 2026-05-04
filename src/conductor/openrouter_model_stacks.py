"""Audited OpenRouter model stacks for task classes where auto is too vague."""

from __future__ import annotations

OPENROUTER_CODING_STACK_VERSION = "2026-05-04"

# Verified against OpenRouter's /models catalog on 2026-05-04. This is an
# explicit quality policy for repo-editing tool-use work: do not delegate these
# tasks to raw openrouter/auto, where upstream may choose cheap or flash models.
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
