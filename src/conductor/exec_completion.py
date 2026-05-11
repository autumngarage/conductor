"""Heuristic completion checks for capped Conductor-managed exec loops.

These checks intentionally look for common, explicit deliverables rather than
trying to prove task completion. They can false-positive when a brief mentions
tests, validation, or shipping only as background context, and they can
false-negative for unusual wording, nonstandard test locations, or validation
performed outside a tool call. The value is an operator-visible hint at the
iteration cap, not an exhaustive verifier.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

_LOG = logging.getLogger(__name__)

RECENT_TOOL_CALL_LIMIT = 20


@dataclass(frozen=True)
class MissingDeliverable:
    """A likely unfinished brief deliverable detected at cap-time."""

    kind: str
    message: str


def changed_paths_for_completion_scan(cwd: Path) -> tuple[str, ...]:
    """Return changed paths relative to ``cwd`` for completion heuristics.

    Git failures are logged and degrade to no changed paths so the original cap
    behavior can still surface. Untracked files are included because agents
    often create new tests before staging them.
    """

    commands = (
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    )
    paths: list[str] = []
    for command in commands:
        try:
            result = subprocess.run(
                command,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            _LOG.warning(
                "completion scan could not run %s in %s: %r",
                " ".join(command),
                cwd,
                e,
            )
            continue
        if result.returncode != 0:
            _LOG.warning(
                "completion scan git command failed: command=%s cwd=%s stderr=%s",
                " ".join(command),
                cwd,
                result.stderr.strip()[:500],
            )
            continue
        paths.extend(line.strip() for line in result.stdout.splitlines() if line.strip())
    return tuple(dict.fromkeys(paths))


def detect_missing_deliverables(
    brief: str,
    *,
    changed_paths: Iterable[str],
    recent_tool_calls: Iterable[dict[str, object]],
) -> list[MissingDeliverable]:
    """Return likely unfinished deliverables requested by ``brief``.

    See the module docstring for the deliberate false-positive/negative shape.
    """

    missing: list[MissingDeliverable] = []
    tool_calls = list(recent_tool_calls)[-RECENT_TOOL_CALL_LIMIT:]
    if (
        _brief_requests_tests(brief)
        and not brief_declares_read_only_text_output(brief)
        and not _has_test_path_change(changed_paths)
    ):
        missing.append(
            MissingDeliverable(
                "tests",
                "Tests requested in brief; diff did not add to tests/.",
            )
        )

    if not _review_preflight_already_passed(brief):
        for command in _requested_validation_commands(brief):
            if not _recent_bash_invoked(tool_calls, command):
                missing.append(
                    MissingDeliverable(
                        "validation",
                        f"Validation calls for `{command}`; not invoked in this session.",
                    )
                )

    if _brief_requests_open_pr_ship(brief) and not _recent_shipping_invoked(tool_calls):
        missing.append(
            MissingDeliverable(
                "shipping",
                "Brief asks to ship via `bash scripts/open-pr.sh --auto-merge`; "
                "push/PR shipping command not invoked in this session.",
            )
        )

    return missing


def format_missing_deliverables_cap_message(
    iteration_cap: int,
    missing: Iterable[MissingDeliverable],
) -> str:
    message = (
        f"[conductor] Reached --max-iterations cap ({iteration_cap}). "
        "Re-run with --max-iterations <larger> or split the brief."
    )
    items = list(missing)
    if not items:
        return message
    bullets = "\n".join(f"  - {item.message}" for item in items)
    return f"{message}\n[conductor] Detected unfinished items:\n{bullets}"


def completion_stretch_prompt(missing: Iterable[MissingDeliverable]) -> str:
    labels = ", ".join(dict.fromkeys(item.kind for item in missing))
    return (
        "You're at the iteration cap. Detected unfinished: "
        f"{labels}. Spend this final turn finishing them or surfacing why you can't."
    )


def brief_declares_read_only_text_output(brief: str) -> bool:
    """Return true when the brief explicitly forbids workspace mutation.

    Test-shape language is common in read-only diagnosis. The invariant is that
    an explicit no-edit/read-only brief may discuss tests to recommend, but it
    is not an implementation task unless the caller separately asks for edits.
    """

    no_edit_patterns = (
        r"(?i)\bdo\s+not\s+(modify|edit|write|change|create|update)\s+"
        r"(files?|the\s+repo|the\s+repository|the\s+worktree|code|the\s+codebase)\b",
        r"(?i)\bdon't\s+(modify|edit|write|change|create|update)\s+"
        r"(files?|the\s+repo|the\s+repository|the\s+worktree|code|the\s+codebase)\b",
        r"(?i)\bwithout\s+(modifying|editing|writing|changing)\s+"
        r"(files?|the\s+repo|the\s+repository|the\s+worktree|code|the\s+codebase)\b",
        r"(?i)\bno\s+(file\s+)?(edits?|changes?|modifications?|writes?)\b",
        r"(?i)\bno\s+implementation\b",
        r"(?i)\bdo\s+not\s+implement\b",
        r"(?i)\bdon't\s+implement\b",
        r"(?i)\bread[- ]only\s+"
        r"(delegation|task|brief|prompt|analysis|investigation|diagnos(?:is|tic)|mode)\b",
        r"(?i)\b(permission[- ]profile|profile)\s+read[- ]only\b",
        r"(?i)\btext[- ]only\s+(delegation|task|brief|prompt|analysis|output|response)\b",
        r"(?i)\banalysis\s+only\b",
        r"(?i)\brecommend\s+(focused\s+)?(regression\s+)?tests?\s+only\b",
    )
    if not any(re.search(pattern, brief) for pattern in no_edit_patterns):
        return False

    implementation_patterns = (
        r"(?im)^\s*[-*]?\s*(implement|fix|modify|edit|write|create|update)\b",
    )
    return not any(re.search(pattern, brief) for pattern in implementation_patterns)


def _brief_requests_tests(brief: str) -> bool:
    patterns = (
        r"(?im)^##\s*tests?\b",
        r"(?i)\badd tests?\b",
        r"(?i)\btest coverage required\b",
        r"(?i)\bregression tests?\b",
    )
    return any(re.search(pattern, brief) for pattern in patterns)


def _has_test_path_change(changed_paths: Iterable[str]) -> bool:
    for raw_path in changed_paths:
        path = raw_path.replace("\\", "/").lstrip("./")
        name = Path(path).name
        if path.startswith("tests/") or path.startswith("test/"):
            return True
        if name.startswith("test_") or name.endswith("_test.py"):
            return True
    return False


def _requested_validation_commands(brief: str) -> tuple[str, ...]:
    commands: list[str] = []
    for command in ("uv run pytest", "uv run ruff check"):
        if re.search(rf"(?i)\b{re.escape(command)}\b", brief):
            commands.append(command)
    if re.search(r"(?i)\blint\b", brief) and "uv run ruff check" not in commands:
        commands.append("uv run ruff check")
    if re.search(r"(?i)\btypecheck\b", brief):
        commands.append("typecheck")
    return tuple(dict.fromkeys(commands))


def _review_preflight_already_passed(brief: str) -> bool:
    """Return True when review context says deterministic validation is done.

    Invariant: this only suppresses cap-time validation-tool-call hints for
    review gates. Implementation briefs still have to execute requested
    validation in the current session.
    """

    if not _brief_is_review_gate(brief):
        return False
    return bool(
        re.search(
            r"(?is)\b(?:pre[- ]?flight|validation)\b.{0,120}"
            r"\b(?:passed|succeeded|completed successfully|already passed)\b",
            brief,
        )
        or re.search(
            r"(?is)\b(?:passed|succeeded)\b.{0,120}"
            r"\b(?:pre[- ]?flight|validation)\b",
            brief,
        )
    )


def _brief_is_review_gate(brief: str) -> bool:
    return bool(
        re.search(r"\bCODEX_REVIEW_(?:CLEAN|FIXED|BLOCKED)\b", brief)
        or re.search(r"(?i)\bcode-review\b", brief)
        or re.search(r"(?i)\breview this merge\b", brief)
        or re.search(r"(?i)\breviewer guide\b", brief)
    )


def _brief_requests_open_pr_ship(brief: str) -> bool:
    return bool(
        re.search(
            r"(?i)\bship\s+via\s+`?bash\s+scripts/open-pr\.sh\s+--auto-merge`?",
            brief,
        )
    )


def _recent_bash_invoked(tool_calls: Iterable[dict[str, object]], command: str) -> bool:
    needle = _normalize_command(command)
    for call in tool_calls:
        if call.get("name") != "Bash":
            continue
        actual = _tool_call_command(call)
        if needle in _normalize_command(actual):
            return True
    return False


def _recent_shipping_invoked(tool_calls: Iterable[dict[str, object]]) -> bool:
    saw_push = False
    saw_pr = False
    for call in tool_calls:
        if call.get("name") != "Bash":
            continue
        command = _normalize_command(_tool_call_command(call))
        saw_push = saw_push or "git push" in command
        saw_pr = saw_pr or "gh pr" in command or "scripts/open-pr.sh --auto-merge" in command
    return saw_push and saw_pr


def _tool_call_command(call: dict[str, object]) -> str:
    args = call.get("args")
    if isinstance(args, dict):
        value = args.get("command")
        return value if isinstance(value, str) else ""
    return ""


def _normalize_command(command: str) -> str:
    return " ".join(command.casefold().split())
