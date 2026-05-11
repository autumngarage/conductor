from __future__ import annotations

from conductor.exec_completion import (
    brief_declares_read_only_text_output,
    detect_missing_deliverables,
)


def test_tests_requested_without_test_path_change_is_flagged() -> None:
    missing = detect_missing_deliverables(
        "Implement it.\n\n## Tests\nAdd regression coverage.",
        changed_paths=("src/conductor/foo.py",),
        recent_tool_calls=[],
    )

    assert [item.kind for item in missing] == ["tests"]
    assert "diff did not add to tests/" in missing[0].message


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
