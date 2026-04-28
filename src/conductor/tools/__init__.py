"""Portable tool registry for Conductor's HTTP tool-use loop.

Shell-out providers (claude/codex/gemini) own their own tool-use: we
hand them ``--tools Read,Grep,...`` and they execute the tools inside
their own process, inside their own sandbox. For HTTP providers
(openrouter, ollama, and compatible presets when enabled), Conductor has to
drive the loop itself. That means
registering tools with the model, executing whichever the model
picks, feeding results back, and repeating until the model answers.

This module owns the portable Tool set Conductor exposes. Each tool:

- has a stable name (``Read``, ``Grep``, ``Glob``, ``Edit``, ``Write``,
  ``Bash``) — the same names shell-out providers accept via their
  respective ``--tools`` / ``--allowedTools`` / sandbox flags;
- declares a JSON Schema for its parameters (used as the ``tools``
  parameter in OpenAI-compatible chat completions);
- refuses to run when the sandbox level forbids its capability
  (``Edit`` raises under ``read-only``, ``Bash`` raises under ``none``).

Path validation is strict and enforced by every filesystem tool: no
absolute paths outside ``cwd``, no relative paths that ``..`` their
way out, no symlinks whose realpath escapes ``cwd``.
"""

from __future__ import annotations

from conductor.tools.registry import (
    ALL_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    WORKSPACE_WRITE_TOOL_NAMES,
    Tool,
    ToolExecutionError,
    ToolExecutor,
    ToolSchemaError,
    build_tool_specs,
    get_tool,
)

__all__ = [
    "ALL_TOOL_NAMES",
    "READ_ONLY_TOOL_NAMES",
    "WORKSPACE_WRITE_TOOL_NAMES",
    "Tool",
    "ToolExecutionError",
    "ToolExecutor",
    "ToolSchemaError",
    "build_tool_specs",
    "get_tool",
]
