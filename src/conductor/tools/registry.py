"""Tool registry + path validation + executor for HTTP providers' tool-use loop.

Each tool is a ``Tool`` implementation with a name, JSON-Schema-described
parameters, and an ``execute`` method that produces a string result. The
``ToolExecutor`` dispatches by name, enforces the sandbox, and wraps all
errors as ``ToolExecutionError`` (which tool-use loops feed back to the
model as the tool's ``role: tool`` response rather than aborting).
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path
from typing import Any, Protocol

# --------------------------------------------------------------------------- #
# Public error types
# --------------------------------------------------------------------------- #


class ToolSchemaError(ValueError):
    """Raised when a tool's input params don't satisfy its schema."""


class ToolExecutionError(RuntimeError):
    """Raised when a tool fails at execute-time (not a schema issue).

    The HTTP tool-use loop catches this and feeds the message back to
    the model as the tool-result string so the conversation can adapt
    rather than aborting mid-loop. Callers outside the loop (e.g. test
    code) see the exception.
    """


# --------------------------------------------------------------------------- #
# Tool protocol
# --------------------------------------------------------------------------- #


class Tool(Protocol):
    name: str
    description: str
    parameters_schema: dict

    def requires_sandbox(self) -> str:
        """Minimum sandbox level this tool needs.

        Returns one of: ``"read-only"`` (tools that only read the
        filesystem or observe), ``"workspace-write"`` (tools that mutate
        files or run commands). The executor compares against the
        request's sandbox and refuses mismatches.
        """

    def execute(self, params: dict, *, cwd: Path) -> str: ...


# --------------------------------------------------------------------------- #
# Shared helpers — path validation
# --------------------------------------------------------------------------- #


def _resolve_in_cwd(raw: str, cwd: Path) -> Path:
    """Resolve ``raw`` against ``cwd`` and refuse anything that escapes.

    Handles:
      - absolute paths that fall outside ``cwd``
      - ``..`` traversal that escapes ``cwd``
      - symlinks whose realpath points outside ``cwd`` (checked via
        ``os.path.realpath`` — resolves even through chains)

    Raises ``ToolExecutionError`` with a message suitable for the
    model to read and try again.
    """
    cwd_real = Path(os.path.realpath(str(cwd)))
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = cwd_real / candidate
    # Resolve symlinks + normalize before comparison. Using os.path.realpath
    # on the parent (not the target) when the target doesn't exist yet —
    # writes to new files need to work without the file pre-existing.
    if candidate.exists() or candidate.is_symlink():
        resolved = Path(os.path.realpath(str(candidate)))
    else:
        parent_real = Path(os.path.realpath(str(candidate.parent)))
        resolved = parent_real / candidate.name

    # Containment check: resolved must be cwd_real itself or a descendant.
    try:
        resolved.relative_to(cwd_real)
    except ValueError as e:
        raise ToolExecutionError(
            f"path {raw!r} escapes the working directory {cwd_real} "
            f"(resolved to {resolved}). Provide a path inside the workspace."
        ) from e
    return resolved


# --------------------------------------------------------------------------- #
# Tool implementations — read-only
# --------------------------------------------------------------------------- #


class ReadTool:
    name = "Read"
    description = (
        "Read the contents of a file under the workspace. Returns the file "
        "text, optionally a byte range. Refuses paths that escape the workspace."
    )
    parameters_schema: dict  # assigned below

    def requires_sandbox(self) -> str:
        return "read-only"

    def execute(self, params: dict, *, cwd: Path) -> str:
        path = _require_str(params, "path")
        max_bytes_raw = params.get("max_bytes")
        max_bytes = int(max_bytes_raw) if max_bytes_raw is not None else 64_000
        offset_raw = params.get("offset")
        offset = int(offset_raw) if offset_raw is not None else 0
        if max_bytes <= 0 or max_bytes > 1_000_000:
            raise ToolSchemaError(
                f"max_bytes must be 1..1_000_000 (got {max_bytes})."
            )
        if offset < 0:
            raise ToolSchemaError(f"offset must be >= 0 (got {offset}).")
        target = _resolve_in_cwd(path, cwd)
        if not target.exists():
            raise ToolExecutionError(f"no such file: {path}")
        if target.is_dir():
            raise ToolExecutionError(f"{path} is a directory, not a file")
        try:
            with target.open("rb") as f:
                if offset:
                    f.seek(offset)
                chunk = f.read(max_bytes + 1)
        except OSError as e:
            raise ToolExecutionError(f"cannot read {path}: {e}") from e
        truncated = len(chunk) > max_bytes
        body = chunk[:max_bytes].decode("utf-8", errors="replace")
        if truncated:
            body += f"\n\n[truncated at {max_bytes} bytes; set max_bytes higher to read more]"
        return body


ReadTool.parameters_schema = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "File path relative to the workspace."},
        "max_bytes": {
            "type": "integer",
            "description": "Maximum bytes to read (default 64000, max 1000000).",
            "default": 64_000,
        },
        "offset": {
            "type": "integer",
            "description": "Byte offset to start reading from (default 0).",
            "default": 0,
        },
    },
    "required": ["path"],
}


class GrepTool:
    name = "Grep"
    description = (
        "Search for a regex pattern across files in the workspace. Returns "
        "matching lines with file path and line number."
    )
    parameters_schema: dict  # assigned below

    def requires_sandbox(self) -> str:
        return "read-only"

    def execute(self, params: dict, *, cwd: Path) -> str:
        pattern = _require_str(params, "pattern")
        path = params.get("path") or "."
        max_results = int(params.get("max_results") or 100)
        case_insensitive = bool(params.get("case_insensitive") or False)
        file_pattern = params.get("file_pattern") or "*"
        if max_results <= 0 or max_results > 2_000:
            raise ToolSchemaError("max_results must be 1..2000.")
        try:
            regex = re.compile(pattern, re.IGNORECASE if case_insensitive else 0)
        except re.error as e:
            raise ToolSchemaError(f"invalid regex {pattern!r}: {e}") from e

        root = _resolve_in_cwd(path, cwd)
        if not root.exists():
            raise ToolExecutionError(f"no such path: {path}")

        # Collect candidate files.
        candidates: list[Path] = []
        if root.is_file():
            candidates.append(root)
        else:
            for dirpath, dirnames, filenames in os.walk(root):
                # Skip common noise directories to keep output usable.
                dirnames[:] = [d for d in dirnames if d not in _DEFAULT_IGNORE_DIRS]
                for name in filenames:
                    if fnmatch.fnmatch(name, file_pattern):
                        candidates.append(Path(dirpath) / name)

        results: list[str] = []
        total_matches = 0
        for file in candidates:
            try:
                # Realpath-check every candidate to catch symlink escape.
                _ = _resolve_in_cwd(str(file.relative_to(cwd)), cwd)
            except (ToolExecutionError, ValueError):
                continue
            except Exception:
                continue
            try:
                with file.open("r", encoding="utf-8", errors="replace") as f:
                    for lineno, line in enumerate(f, start=1):
                        if regex.search(line):
                            results.append(
                                f"{file.relative_to(cwd)}:{lineno}:{line.rstrip()}"
                            )
                            total_matches += 1
                            if total_matches >= max_results:
                                break
            except OSError:
                continue
            if total_matches >= max_results:
                break

        if not results:
            return f"(no matches for pattern {pattern!r} under {path})"
        header = f"{total_matches} match(es):\n"
        if total_matches >= max_results:
            header = (
                f"{total_matches}+ match(es) (capped at max_results={max_results}):\n"
            )
        return header + "\n".join(results)


GrepTool.parameters_schema = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "Regex pattern (Python syntax)."},
        "path": {
            "type": "string",
            "description": "File or directory to search under (default: workspace root).",
        },
        "max_results": {
            "type": "integer",
            "description": "Cap on matches returned (default 100, max 2000).",
            "default": 100,
        },
        "case_insensitive": {"type": "boolean", "default": False},
        "file_pattern": {
            "type": "string",
            "description": "fnmatch glob to restrict searched files (e.g. '*.py').",
            "default": "*",
        },
    },
    "required": ["pattern"],
}


class GlobTool:
    name = "Glob"
    description = (
        "Find files by glob pattern under the workspace. Returns matching paths."
    )
    parameters_schema: dict  # assigned below

    def requires_sandbox(self) -> str:
        return "read-only"

    def execute(self, params: dict, *, cwd: Path) -> str:
        pattern = _require_str(params, "pattern")
        path = params.get("path") or "."
        max_results = int(params.get("max_results") or 200)
        if max_results <= 0 or max_results > 5_000:
            raise ToolSchemaError("max_results must be 1..5000.")

        root = _resolve_in_cwd(path, cwd)
        if not root.exists():
            raise ToolExecutionError(f"no such path: {path}")
        if not root.is_dir():
            raise ToolExecutionError(f"{path} is not a directory")

        matches: list[str] = []
        # Path.rglob accepts glob-style patterns with ** for recursion.
        for match in root.rglob(pattern):
            try:
                _ = _resolve_in_cwd(str(match.relative_to(cwd)), cwd)
            except (ToolExecutionError, ValueError):
                continue
            matches.append(str(match.relative_to(cwd)))
            if len(matches) >= max_results:
                break

        if not matches:
            return f"(no matches for {pattern!r} under {path})"
        header = f"{len(matches)} match(es):\n"
        if len(matches) >= max_results:
            header = f"{len(matches)}+ (capped at max_results={max_results}):\n"
        return header + "\n".join(matches)


GlobTool.parameters_schema = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Glob pattern. Use ** for recursive matches (e.g. '**/*.py').",
        },
        "path": {
            "type": "string",
            "description": "Directory to search under (default: workspace root).",
        },
        "max_results": {
            "type": "integer",
            "description": "Cap on matches returned (default 200, max 5000).",
            "default": 200,
        },
    },
    "required": ["pattern"],
}


# --------------------------------------------------------------------------- #
# Registry + executor
# --------------------------------------------------------------------------- #


_BUILTIN_TOOLS: dict[str, Tool] = {
    "Read": ReadTool(),
    "Grep": GrepTool(),
    "Glob": GlobTool(),
}

READ_ONLY_TOOL_NAMES = frozenset({"Read", "Grep", "Glob"})
WORKSPACE_WRITE_TOOL_NAMES = frozenset({"Edit", "Write", "Bash"})
ALL_TOOL_NAMES = READ_ONLY_TOOL_NAMES | WORKSPACE_WRITE_TOOL_NAMES


def get_tool(name: str) -> Tool:
    if name not in _BUILTIN_TOOLS:
        raise KeyError(
            f"unknown tool {name!r}; known: {sorted(_BUILTIN_TOOLS)}. "
            f"(Write/Edit/Bash tools land in the next slice.)"
        )
    return _BUILTIN_TOOLS[name]


def build_tool_specs(names: frozenset[str]) -> list[dict[str, Any]]:
    """Build the OpenAI-format ``tools`` parameter for a chat completion.

    Returns a list of ``{"type": "function", "function": {...}}`` dicts
    for every tool name in ``names`` that has a built-in implementation.
    Unknown names raise KeyError via ``get_tool``.
    """
    specs = []
    for name in sorted(names):
        tool = get_tool(name)
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters_schema,
                },
            }
        )
    return specs


class ToolExecutor:
    """Dispatches tool calls against a fixed cwd + sandbox contract.

    Instantiated per exec() call in the HTTP tool-use loop. All
    filesystem tools resolve paths against the given cwd; sandbox
    rejects tools whose ``requires_sandbox`` doesn't match.
    """

    def __init__(self, *, cwd: Path, sandbox: str):
        self._cwd = Path(cwd).resolve()
        if not self._cwd.exists() or not self._cwd.is_dir():
            raise ValueError(f"cwd {cwd} is not an existing directory")
        self._sandbox = sandbox

    def run(self, name: str, params: dict) -> str:
        """Execute a named tool call. Returns the tool's result string.

        Raises ``ToolExecutionError`` for sandbox / path / execution
        failures. The caller (tool-use loop) should catch these and
        feed the message back to the model as the tool's response.
        """
        try:
            tool = get_tool(name)
        except KeyError as e:
            raise ToolExecutionError(str(e)) from e

        required = tool.requires_sandbox()
        if not _sandbox_satisfies(self._sandbox, required):
            raise ToolExecutionError(
                f"tool `{name}` requires sandbox `{required}` but the request "
                f"provided `{self._sandbox}`. Ask for a less restrictive sandbox "
                f"or a tool that fits."
            )

        try:
            return tool.execute(params, cwd=self._cwd)
        except ToolExecutionError:
            raise
        except ToolSchemaError as e:
            raise ToolExecutionError(f"bad parameters for `{name}`: {e}") from e
        except Exception as e:  # pragma: no cover — belt-and-braces
            raise ToolExecutionError(f"`{name}` failed unexpectedly: {e}") from e


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


_DEFAULT_IGNORE_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".build",
        ".swiftpm",
        "DerivedData",
        "target",
        "dist",
        "build",
    }
)


def _require_str(params: dict, field: str) -> str:
    value = params.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ToolSchemaError(
            f"parameter `{field}` is required and must be a non-empty string."
        )
    return value


def _sandbox_satisfies(provided: str, required: str) -> bool:
    """True when the provided sandbox is at least as permissive as required.

    Hierarchy: ``none`` (no tools permitted) < ``read-only`` (observe) <
    ``workspace-write`` (mutate). A read-only tool is satisfied by either
    ``read-only`` or ``workspace-write``. A workspace-write tool needs
    exactly ``workspace-write``.
    """
    rank = {"none": 0, "read-only": 1, "workspace-write": 2}
    if provided not in rank:
        return False
    if required not in rank:
        return False
    return rank[provided] >= rank[required]
