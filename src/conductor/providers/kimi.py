"""Kimi provider — OpenRouter-backed model preset.

The user-facing provider identifier remains ``kimi``, but credential and HTTP
transport now flow through the shared OpenRouter adapter.
"""

from __future__ import annotations

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor.providers.openrouter import OpenRouterProvider

KIMI_DEFAULT_MODEL = "moonshotai/kimi-k2.6"


def _is_kimi_model(model: openrouter_catalog.ModelEntry) -> bool:
    return model.id.startswith("moonshotai/kimi-")


class KimiProvider(OpenRouterProvider):
    """Moonshot Kimi on OpenRouter."""

    name = "kimi"
    default_model = KIMI_DEFAULT_MODEL
    tags = ["long-context", "cheap", "vision", "code-review"]
    fix_command = "conductor init --only openrouter"

    quality_tier = "strong"
    supported_tools: frozenset[str] = frozenset()
    supported_sandboxes: frozenset[str] = frozenset({"none"})
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

    def _catalog_model(self) -> str:
        return openrouter_catalog.newest_matching_model_id(
            _is_kimi_model,
            fallback_model=self.default_model,
            label=self.name,
        )

    def _preset_model(self) -> str | None:
        return self._catalog_model()

    def _smoke_model(self) -> str:
        return self._catalog_model()
