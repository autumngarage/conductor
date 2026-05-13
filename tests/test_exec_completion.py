from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING

from conductor.exec_completion import (
    brief_declares_read_only_text_output,
    cap_diagnostics_for_completion_scan,
    changed_paths_for_completion_scan,
    detect_missing_deliverables,
    format_missing_deliverables_cap_message,
)

if TYPE_CHECKING:
    from pathlib import Path


def _init_git(path: Path) -> None:
    env = {
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "tester@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "tester@example.com",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=path, env=env, check=True)
    (path / "README.md").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=path, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=path, env=env, check=True)


def test_tests_requested_without_test_path_change_is_flagged() -> None:
    missing = detect_missing_deliverables(
        "Implement it.\n\n## Tests\nAdd regression coverage.",
        changed_paths=("src/conductor/foo.py",),
        recent_tool_calls=[],
    )

    assert [item.kind for item in missing] == ["tests"]
    assert "diff did not add to tests/" in missing[0].message


def test_tests_requested_with_empty_diff_reports_no_changes() -> None:
    missing = detect_missing_deliverables(
        "Implement it.\n\n## Tests\nAdd regression coverage.",
        changed_paths=(),
        recent_tool_calls=[],
    )

    assert [item.kind for item in missing] == ["changes"]
    assert missing[0].message == "Agent made no changes before the iteration cap."


def test_changed_paths_include_committed_branch_changes(tmp_path: Path) -> None:
    _init_git(tmp_path)
    subprocess.run(["git", "switch", "-q", "-c", "feature"], cwd=tmp_path, check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_feature.py").write_text("def test_it():\n    pass\n")
    subprocess.run(["git", "add", "tests/test_feature.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add test"], cwd=tmp_path, check=True)

    assert changed_paths_for_completion_scan(tmp_path) == ("tests/test_feature.py",)


def test_cap_diagnostics_format_tool_usage_and_git_state(tmp_path: Path) -> None:
    _init_git(tmp_path)
    (tmp_path / "scratch.txt").write_text("wip\n", encoding="utf-8")
    diagnostics = cap_diagnostics_for_completion_scan(
        tmp_path,
        recent_tool_calls=[
            {"name": "Read", "args": {"path": "README.md"}},
            {"name": "Bash", "args": {"command": "pytest"}},
            {"name": "Read", "args": {"path": "README.md"}},
        ],
    )

    text = format_missing_deliverables_cap_message(60, [], diagnostics)

    assert "Tool usage: Read=2 Bash=1" in text
    assert "git state at cap-fire: commits-on-branch=0" in text
    assert "untracked-files=1" in text


def test_committed_work_request_without_git_commit_is_flagged() -> None:
    missing = detect_missing_deliverables(
        (
            "Conductor swarm delivery contract:\n"
            "- Commit all intended changes before your final answer.\n"
            "- Leave the worktree clean."
        ),
        changed_paths=("src/conductor/foo.py",),
        recent_tool_calls=[{"name": "Write", "args": {"path": "src/conductor/foo.py"}}],
    )

    assert [item.kind for item in missing] == ["commit"]
    assert "`git commit` not invoked" in missing[0].message


def test_committed_work_request_accepts_git_commit_from_any_prior_turn() -> None:
    older_calls: list[dict[str, object]] = [
        {"name": "Read", "args": {"path": f"file-{idx}.py"}} for idx in range(25)
    ]
    missing = detect_missing_deliverables(
        "Commit all intended changes before your final answer.",
        changed_paths=("src/conductor/foo.py",),
        recent_tool_calls=[
            {"name": "Bash", "args": {"command": "git add . && git commit -m fix"}},
            *older_calls,
        ],
    )

    assert missing == []


def test_read_only_test_recommendations_do_not_require_test_path_change() -> None:
    missing = detect_missing_deliverables(
        (
            "Read-only analysis task. Do not modify files.\n\n"
            "Recommend focused regression tests only; do not implement or commit changes."
        ),
        changed_paths=(),
        recent_tool_calls=[],
    )

    assert missing == []


def test_implementation_brief_still_requires_requested_tests() -> None:
    missing = detect_missing_deliverables(
        (
            "Implement the fix.\n\n"
            "Do not modify files outside src/conductor.\n\n"
            "## Tests\nAdd regression tests."
        ),
        changed_paths=("src/conductor/foo.py",),
        recent_tool_calls=[],
    )

    assert [item.kind for item in missing] == ["tests"]


def test_tests_requested_with_test_path_change_is_not_flagged() -> None:
    missing = detect_missing_deliverables(
        "Implement it.\n\n## Tests\nAdd regression coverage.",
        changed_paths=("tests/test_foo.py", "src/conductor/foo.py"),
        recent_tool_calls=[],
    )

    assert missing == []


def test_read_only_test_recommendations_are_text_output_not_required_edits() -> None:
    brief = """
Goal:
Investigate the failure. Do not edit files; this is read-only.

Expected output:
- Root cause
- Regression tests to add/update
"""

    missing = detect_missing_deliverables(
        brief,
        changed_paths=(),
        recent_tool_calls=[],
    )

    assert missing == []


def test_read_only_classifier_requires_explicit_no_edit_semantics() -> None:
    assert brief_declares_read_only_text_output("Read-only investigation; no diff.")
    assert brief_declares_read_only_text_output("Do not edit files; report findings.")
    assert not brief_declares_read_only_text_output(
        "Implement the fix, but do not edit generated files."
    )


def test_validation_command_requested_without_recent_tool_call_is_flagged() -> None:
    missing = detect_missing_deliverables(
        "## Validation\n- uv run pytest",
        changed_paths=(),
        recent_tool_calls=[{"name": "Bash", "args": {"command": "uv run ruff check src/"}}],
    )

    assert [item.kind for item in missing] == ["validation"]
    assert "uv run pytest" in missing[0].message


def test_review_preflight_passed_does_not_require_fresh_validation_call() -> None:
    missing = detect_missing_deliverables(
        (
            "Review this merge using the project reviewer guide.\n\n"
            "Preflight passed before fallback dispatch: uv run ruff check.\n\n"
            "The LAST line must be CODEX_REVIEW_CLEAN or CODEX_REVIEW_BLOCKED."
        ),
        changed_paths=(),
        recent_tool_calls=[],
    )

    assert missing == []


def test_implementation_preflight_passed_still_requires_requested_validation() -> None:
    missing = detect_missing_deliverables(
        "Implement the fix.\n\nPreflight passed: uv run ruff check.",
        changed_paths=("src/conductor/foo.py",),
        recent_tool_calls=[],
    )

    assert [item.kind for item in missing] == ["validation"]
