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


@dataclass(frozen=True)
class CapDiagnostics:
    """Operator-facing state captured when a managed exec loop hits its cap."""

    tool_usage: dict[str, int]
    git_state: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "tool_usage": dict(self.tool_usage),
            "git_state": dict(self.git_state),
        }


def changed_paths_for_completion_scan(cwd: Path) -> tuple[str, ...]:
    """Return changed paths relative to ``cwd`` for completion heuristics.

    Git failures are logged and degrade to no changed paths so the original cap
    behavior can still surface. Untracked files are included because agents
    often create new tests before staging them. Committed branch changes are
    included when a default branch can be resolved so a clean worktree with
    commits is not misread as no progress.
    """

    commands: list[list[str]] = [
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    base_ref = _default_branch_ref(cwd)
    if base_ref is not None:
        commands.append(["git", "diff", "--name-only", f"{base_ref}...HEAD"])
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


def cap_diagnostics_for_completion_scan(
    cwd: Path,
    *,
    recent_tool_calls: Iterable[dict[str, object]],
) -> CapDiagnostics:
    """Return cap-time tool and git state diagnostics.

    Invariant: diagnostic collection must never mask the original iteration-cap
    failure; every failed git probe is logged and represented in the returned
    payload.
    """

    return CapDiagnostics(
        tool_usage=_tool_usage_counts(recent_tool_calls),
        git_state=_git_state_at_cap(cwd),
    )


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
    changed = tuple(changed_paths)
    tool_calls = list(recent_tool_calls)[-RECENT_TOOL_CALL_LIMIT:]
    if (
        _brief_requests_tests(brief)
        and not brief_declares_read_only_text_output(brief)
        and not _has_test_path_change(changed)
    ):
        if changed:
            missing.append(
                MissingDeliverable(
                    "tests",
                    "Tests requested in brief; diff did not add to tests/.",
                )
            )
        else:
            missing.append(
                MissingDeliverable(
                    "changes",
                    "Agent made no changes before the iteration cap.",
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
    diagnostics: CapDiagnostics | dict[str, object] | None = None,
) -> str:
    message = (
        f"[conductor] Reached --max-iterations cap ({iteration_cap}). "
        "Re-run with --max-iterations <larger> or split the brief."
    )
    diagnostic_text = _format_cap_diagnostics(iteration_cap, diagnostics)
    if diagnostic_text:
        message = f"{message}\n{diagnostic_text}"
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


def _tool_usage_counts(tool_calls: Iterable[dict[str, object]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for call in tool_calls:
        name = call.get("name")
        if not isinstance(name, str) or not name:
            name = "<unknown>"
        counts[name] = counts.get(name, 0) + 1

    preferred = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
    ordered: dict[str, int] = {}
    for name in preferred:
        if name in counts:
            ordered[name] = counts.pop(name)
    for name in sorted(counts):
        ordered[name] = counts[name]
    return ordered


def _git_state_at_cap(cwd: Path) -> dict[str, object]:
    status = _run_git(["status", "--porcelain"], cwd)
    if status is None:
        return {
            "available": False,
            "error": "git status failed",
        }

    tracked_changes = 0
    untracked = 0
    for line in status.splitlines():
        if line.startswith("?? "):
            untracked += 1
        elif line.strip():
            tracked_changes += 1

    base_ref = _default_branch_ref(cwd)
    commits_on_branch: int | None = None
    commit_error: str | None = None
    if base_ref is None:
        commit_error = "default branch unavailable"
    else:
        raw_count = _run_git(["rev-list", "--count", f"{base_ref}..HEAD"], cwd)
        if raw_count is None:
            commit_error = f"git rev-list failed for {base_ref}..HEAD"
        else:
            try:
                commits_on_branch = int(raw_count.strip())
            except ValueError:
                commit_error = f"unexpected git rev-list output: {raw_count!r}"
                _LOG.warning(
                    "cap diagnostic git rev-list returned unexpected output: cwd=%s output=%r",
                    cwd,
                    raw_count,
                )

    state: dict[str, object] = {
        "available": True,
        "base_ref": base_ref,
        "commits_on_branch": commits_on_branch,
        "modified_files": tracked_changes,
        "untracked_files": untracked,
    }
    if commit_error is not None:
        state["commit_error"] = commit_error
    return state


def _default_branch_ref(cwd: Path) -> str | None:
    origin_head = _run_git(
        ["symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"],
        cwd,
        log_failure=False,
    )
    if origin_head:
        return origin_head.strip()

    for ref in ("origin/main", "origin/master", "main", "master"):
        if _run_git(["rev-parse", "--verify", "--quiet", ref], cwd, log_failure=False):
            return ref
    _LOG.info("completion scan could not resolve default branch in %s", cwd)
    return None


def _run_git(
    args: list[str],
    cwd: Path,
    *,
    log_failure: bool = True,
) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        _LOG.warning(
            "completion scan could not run git %s in %s: %r",
            " ".join(args),
            cwd,
            e,
        )
        return None
    if result.returncode != 0:
        if log_failure:
            _LOG.warning(
                "completion scan git command failed: command=git %s cwd=%s stderr=%s",
                " ".join(args),
                cwd,
                result.stderr.strip()[:500],
            )
        return None
    return result.stdout


def _format_cap_diagnostics(
    iteration_cap: int,
    diagnostics: CapDiagnostics | dict[str, object] | None,
) -> str:
    if diagnostics is None:
        return ""
    raw = diagnostics.as_dict() if isinstance(diagnostics, CapDiagnostics) else diagnostics

    tool_usage = raw.get("tool_usage")
    if isinstance(tool_usage, dict) and tool_usage:
        tool_text = " ".join(f"{name}={count}" for name, count in tool_usage.items())
    else:
        tool_text = "none"

    lines = [
        f"[conductor] iteration cap hit at {iteration_cap}. Tool usage: {tool_text}"
    ]
    git_state = raw.get("git_state")
    if isinstance(git_state, dict):
        if git_state.get("available") is True:
            line = (
                "[conductor] git state at cap-fire: "
                f"commits-on-branch={git_state.get('commits_on_branch')}, "
                f"modified-files={git_state.get('modified_files')}, "
                f"untracked-files={git_state.get('untracked_files')}"
            )
            base_ref = git_state.get("base_ref")
            if base_ref:
                line += f", base={base_ref}"
            commit_error = git_state.get("commit_error")
            if commit_error:
                line += f", commit-count={commit_error}"
            lines.append(line)
        else:
            error = git_state.get("error") or "unknown"
            lines.append(f"[conductor] git state at cap-fire: unavailable ({error})")
    return "\n".join(lines)


def _normalize_command(command: str) -> str:
    return " ".join(command.casefold().split())
