"""Drift tests for conductor's Claude Code subagent prompt templates."""

from __future__ import annotations

from conductor import _agent_templates as templates

EXPECTED_TEMPLATE_COVERAGE = {
    "kimi-long-context": {
        "--with kimi",
        "--effort",
        "--json",
        "long-context",
    },
    "gemini-web-search": {
        "--with gemini",
        "--json",
        "fresh",
        "gemini",
        "web search",
    },
    "codex-coding-agent": {
        "--with codex",
        "--tools",
        "--json",
        "`--resume`",
        "Bash",
        "Edit",
        "Glob",
        "Grep",
        "Read",
        "Write",
        "codex",
        "conductor exec",
    },
    "ollama-offline": {
        "--offline",
        "--with ollama",
        "--json",
        "local",
        "offline",
        "ollama",
        "privacy-sensitive",
    },
    "conductor-auto": {
        "--auto",
        "--effort",
        "--offline",
        "--prefer",
        "--kind",
        "--tags",
        "--permission-profile",
        "--json",
        "Bash",
        "balanced",
        "best",
        "cheap",
        "cheapest",
        "code-review",
        "conductor ask",
        "conductor call",
        "conductor exec",
        "council",
        "fastest",
        "full",
        "high",
        "long-context",
        "low",
        "max",
        "medium",
        "minimal",
        "offline",
        "patch",
        "read-only",
        "research",
        "review",
        "tool-use",
        "vision",
        "web-search",
    },
}

SUBAGENT_TEMPLATES = {
    "kimi-long-context": templates.SUBAGENT_KIMI_LONG_CONTEXT,
    "gemini-web-search": templates.SUBAGENT_GEMINI_WEB_SEARCH,
    "codex-coding-agent": templates.SUBAGENT_CODEX_CODING_AGENT,
    "ollama-offline": templates.SUBAGENT_OLLAMA_OFFLINE,
    "conductor-auto": templates.SUBAGENT_CONDUCTOR_AUTO,
}


def _assert_template_mentions_expected_tokens(subagent_name: str) -> None:
    template = SUBAGENT_TEMPLATES[subagent_name]
    expected_tokens = EXPECTED_TEMPLATE_COVERAGE[subagent_name]
    missing = sorted(token for token in expected_tokens if token not in template)
    assert not missing, (
        f"{subagent_name} template missing required token(s): "
        f"{', '.join(missing)}"
    )


def test_expected_template_coverage_covers_every_subagent_constant():
    subagent_constants = {
        name
        for name, value in vars(templates).items()
        if name.startswith("SUBAGENT_") and isinstance(value, str)
    }
    expected_constants = {
        "SUBAGENT_KIMI_LONG_CONTEXT",
        "SUBAGENT_GEMINI_WEB_SEARCH",
        "SUBAGENT_CODEX_CODING_AGENT",
        "SUBAGENT_OLLAMA_OFFLINE",
        "SUBAGENT_CONDUCTOR_AUTO",
    }

    assert subagent_constants == expected_constants
    assert set(SUBAGENT_TEMPLATES) == set(EXPECTED_TEMPLATE_COVERAGE)


def test_kimi_long_context_template_mentions_required_cli_surface():
    _assert_template_mentions_expected_tokens("kimi-long-context")


def test_gemini_web_search_template_mentions_required_cli_surface():
    _assert_template_mentions_expected_tokens("gemini-web-search")


def test_codex_coding_agent_template_mentions_required_cli_surface():
    _assert_template_mentions_expected_tokens("codex-coding-agent")


def test_ollama_offline_template_mentions_required_cli_surface():
    _assert_template_mentions_expected_tokens("ollama-offline")


def test_conductor_auto_template_mentions_required_cli_surface():
    _assert_template_mentions_expected_tokens("conductor-auto")
