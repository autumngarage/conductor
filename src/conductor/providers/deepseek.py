"""DeepSeek providers — OpenRouter-backed model presets.

DeepSeek remains exposed as two conductor provider identifiers so the router
can distinguish the cheap chat path from the stronger reasoning path, but both
now share OpenRouter's credential + transport layer.
"""

from __future__ import annotations

from conductor.providers.openrouter import OpenRouterProvider

DEEPSEEK_CHAT_MODEL = "deepseek/deepseek-chat"
DEEPSEEK_REASONER_MODEL = "deepseek/deepseek-r1"


class DeepSeekChatProvider(OpenRouterProvider):
    """DeepSeek V3 chat model on OpenRouter."""

    name = "deepseek-chat"
    default_model = DEEPSEEK_CHAT_MODEL
    tags = ["cheap", "code-review", "tool-use"]
    fix_command = "conductor init --only openrouter"

    quality_tier = "strong"
    supports_effort = False
    effort_to_thinking: dict[str, int] = {}
    cost_per_1k_in = 0.00032
    cost_per_1k_out = 0.00089
    cost_per_1k_thinking = 0.0
    typical_p50_ms = 2500
    max_context_tokens = 163_840

    def _reasoning_payload(self, effort: str | int) -> dict[str, str] | None:
        return None

    def call(self, task: str, model: str | None = None, **kwargs):
        return super().call(task, model=model or self.default_model, **kwargs)

    def exec(self, task: str, model: str | None = None, **kwargs):
        return super().exec(task, model=model or self.default_model, **kwargs)


class DeepSeekReasonerProvider(OpenRouterProvider):
    """DeepSeek R1 reasoning model on OpenRouter."""

    name = "deepseek-reasoner"
    default_model = DEEPSEEK_REASONER_MODEL
    tags = ["strong-reasoning", "thinking", "code-review"]
    fix_command = "conductor init --only openrouter"

    quality_tier = "strong"
    supports_effort = True
    effort_to_thinking = {
        "minimal": 0,
        "low": 2_000,
        "medium": 4_000,
        "high": 8_000,
        "max": 16_000,
    }
    cost_per_1k_in = 0.00070
    cost_per_1k_out = 0.00250
    cost_per_1k_thinking = 0.00070
    typical_p50_ms = 12_000
    max_context_tokens = 64_000

    def call(self, task: str, model: str | None = None, **kwargs):
        return super().call(task, model=model or self.default_model, **kwargs)

    def exec(self, task: str, model: str | None = None, **kwargs):
        return super().exec(task, model=model or self.default_model, **kwargs)
