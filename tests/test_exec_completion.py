from __future__ import annotations

from conductor.exec_completion import detect_missing_deliverables


def test_tests_requested_without_test_path_change_is_flagged() -> None:
    missing = detect_missing_deliverables(
        "Implement it.\n\n## Tests\nAdd regression coverage.",
        changed_paths=("src/conductor/foo.py",),
        recent_tool_calls=[],
    )

    assert [item.kind for item in missing] == ["tests"]
    assert "diff did not add to tests/" in missing[0].message


def test_tests_requested_with_test_path_change_is_not_flagged() -> None:
    missing = detect_missing_deliverables(
        "Implement it.\n\n## Tests\nAdd regression coverage.",
        changed_paths=("tests/test_foo.py", "src/conductor/foo.py"),
        recent_tool_calls=[],
    )

    assert missing == []


def test_validation_command_requested_without_recent_tool_call_is_flagged() -> None:
    missing = detect_missing_deliverables(
        "## Validation\n- uv run pytest",
        changed_paths=(),
        recent_tool_calls=[{"name": "Bash", "args": {"command": "uv run ruff check src/"}}],
    )

    assert [item.kind for item in missing] == ["validation"]
    assert "uv run pytest" in missing[0].message
