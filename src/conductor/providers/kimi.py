"""Kimi provider — OpenRouter-backed model preset.

The user-facing provider identifier remains ``kimi``, but credential and HTTP
transport now flow through the shared OpenRouter adapter.
"""

from __future__ import annotations

from conductor.providers.openrouter import OpenRouterProvider

KIMI_DEFAULT_MODEL = "moonshotai/kimi-k2.6"


class KimiProvider(OpenRouterProvider):
    """Moonshot Kimi on OpenRouter."""

    name = "kimi"
    default_model = KIMI_DEFAULT_MODEL
    tags = ["long-context", "cheap", "vision", "code-review"]
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
    cost_per_1k_in = 0.0007448
    cost_per_1k_out = 0.004655
    cost_per_1k_thinking = 0.0
    typical_p50_ms = 3500
    max_context_tokens = 256_000

    def call(self, task: str, model: str | None = None, **kwargs):
        return super().call(task, model=model or self.default_model, **kwargs)

    def exec(self, task: str, model: str | None = None, **kwargs):
        return super().exec(task, model=model or self.default_model, **kwargs)
