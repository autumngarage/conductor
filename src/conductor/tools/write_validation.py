"""Pre-write validation for Conductor-owned file edit tools."""

from __future__ import annotations

import json
import re
import sys
import time
import tomllib
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

_VALIDATION_BUDGET_SEC = 0.05
_TOOL_CALL_LEAK_RE = re.compile(
    r"assistant\s+to=functions\.[A-Za-z_][\w.]*|"
    r"<\|im_start\|>|"
    r"<\|im_end\|>|"
    r"\[GMASK\]|"
    r"<\|assistant\s+to=functions\.[^|]+?\|>",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class WriteValidationError(ValueError):
    """Raised when generated file content should not be written."""

    reason: str

    def __str__(self) -> str:
        return self.reason


def validate_write_content(
    *,
    path: Path,
    old_content: str | None,
    new_content: str,
) -> None:
    """Reject common model-corruption modes before writing file bytes."""

    started = time.monotonic()
    _check_tool_call_leak(new_content)
    _check_mojibake(path=path, old_content=old_content, new_content=new_content)
    _check_syntax(
        path=path,
        old_content=old_content,
        new_content=new_content,
        started=started,
    )


def log_write_rejection(*, tool_name: str, path: str, reason: str) -> None:
    """Make rejected writes visible to the operator as well as the agent."""

    print(
        f"[conductor] {tool_name} rejected for {path}: {reason}",
        file=sys.stderr,
    )


def log_write_validation_notice(*, tool_name: str, path: str, message: str) -> None:
    """Emit non-rejection validation context to stderr."""

    print(
        f"[conductor] {tool_name} validation notice for {path}: {message}",
        file=sys.stderr,
    )


def _check_tool_call_leak(content: str) -> None:
    match = _TOOL_CALL_LEAK_RE.search(content)
    if match is None:
        return
    leaked = match.group(0)
    raise WriteValidationError(
        f"tool-call leak detected: {leaked!r}. "
        "Re-emit the write with the actual file content."
    )


def _check_mojibake(
    *,
    path: Path,
    old_content: str | None,
    new_content: str,
) -> None:
    if old_content is None or not old_content.isascii():
        return
    scripts = sorted(_unicode_scripts(new_content))
    if len(scripts) < 2:
        return
    raise WriteValidationError(
        "mojibake detected: "
        f"{len(scripts)} distinct Unicode scripts in previously-ASCII file "
        f"{path} ({', '.join(scripts)})."
    )


def _unicode_scripts(content: str) -> set[str]:
    scripts: set[str] = set()
    for char in content:
        if ord(char) < 128:
            continue
        category = unicodedata.category(char)
        if category.startswith(("M", "P", "S", "Z")):
            continue
        name = unicodedata.name(char, "")
        script = _script_from_name(name)
        if script is not None:
            scripts.add(script)
    return scripts


def _script_from_name(name: str) -> str | None:
    if not name:
        return None
    if any(token in name for token in ("CJK", "HIRAGANA", "KATAKANA", "HANGUL")):
        return "CJK"
    for token, script in (
        ("DEVANAGARI", "Devanagari"),
        ("CYRILLIC", "Cyrillic"),
        ("ARABIC", "Arabic"),
        ("HEBREW", "Hebrew"),
        ("GREEK", "Greek"),
        ("THAI", "Thai"),
        ("BENGALI", "Bengali"),
        ("TAMIL", "Tamil"),
        ("TELUGU", "Telugu"),
        ("GUJARATI", "Gujarati"),
        ("GURMUKHI", "Gurmukhi"),
        ("GEORGIAN", "Georgian"),
        ("ARMENIAN", "Armenian"),
    ):
        if token in name:
            return script
    if "LATIN" in name:
        return "Latin"
    return None


def _check_syntax(
    *,
    path: Path,
    old_content: str | None,
    new_content: str,
    started: float,
) -> None:
    if time.monotonic() - started > _VALIDATION_BUDGET_SEC:
        print(
            f"[conductor] skipped syntax validation for {path}: "
            "pre-write validation budget exhausted",
            file=sys.stderr,
        )
        return
    suffix = path.suffix.lower()
    if suffix in {".yaml", ".yml", ".js", ".jsx", ".ts", ".tsx"}:
        print(
            f"[conductor] skipped syntax validation for {path}: "
            f"no lightweight parser configured for {suffix}",
            file=sys.stderr,
        )
        return
    if old_content is not None:
        old_error = _syntax_error(suffix=suffix, path=path, content=old_content)
        if old_error is not None:
            print(
                f"[conductor] skipped syntax validation for {path}: "
                "existing file was already unparsable",
                file=sys.stderr,
            )
            return
    error = _syntax_error(suffix=suffix, path=path, content=new_content)
    if error is not None:
        raise error


def _syntax_error(
    *,
    suffix: str,
    path: Path,
    content: str,
) -> WriteValidationError | None:
    try:
        if suffix == ".py":
            compile(content, str(path), "exec")
        elif suffix == ".json":
            json.loads(content)
        elif suffix == ".toml":
            tomllib.loads(content)
    except SyntaxError as err:
        line = err.lineno or "?"
        detail = err.msg or err.__class__.__name__
        return WriteValidationError(
            f"Python syntax error at line {line}: {detail}."
        )
    except json.JSONDecodeError as err:
        return WriteValidationError(
            f"JSON syntax error at line {err.lineno}: {err.msg}."
        )
    except tomllib.TOMLDecodeError as err:
        return WriteValidationError(f"TOML syntax error: {err}.")
    return None
