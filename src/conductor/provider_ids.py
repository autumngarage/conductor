"""Canonical provider identifiers shared across registry-adjacent modules."""

from __future__ import annotations

BUILTIN_PROVIDER_IDS = frozenset(
    {
        "kimi",
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "ollama",
        "openrouter",
    }
)
