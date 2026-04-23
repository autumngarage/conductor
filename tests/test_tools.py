"""Unit tests for the portable tool registry (conductor.tools)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from conductor.tools import (
    ALL_TOOL_NAMES,
    READ_ONLY_TOOL_NAMES,
    WORKSPACE_WRITE_TOOL_NAMES,
    ToolExecutionError,
    ToolExecutor,
    ToolSchemaError,
    build_tool_specs,
    get_tool,
)
from conductor.tools.registry import _resolve_in_cwd

# --------------------------------------------------------------------------- #
# Registry surface
# --------------------------------------------------------------------------- #


def test_read_only_names_are_expected():
    assert frozenset({"Read", "Grep", "Glob"}) == READ_ONLY_TOOL_NAMES


def test_workspace_write_names_are_expected():
    assert frozenset({"Edit", "Write", "Bash"}) == WORKSPACE_WRITE_TOOL_NAMES


def test_all_names_is_union():
    assert ALL_TOOL_NAMES == READ_ONLY_TOOL_NAMES | WORKSPACE_WRITE_TOOL_NAMES


def test_get_tool_returns_read_tool():
    tool = get_tool("Read")
    assert tool.name == "Read"
    assert tool.requires_sandbox() == "read-only"
    assert "path" in tool.parameters_schema["properties"]


def test_get_tool_rejects_unknown():
    with pytest.raises(KeyError):
        get_tool("NotATool")


def test_get_tool_returns_every_declared_tool():
    # ALL_TOOL_NAMES should map 1:1 to registered tools.
    for name in sorted(ALL_TOOL_NAMES):
        tool = get_tool(name)
        assert tool.name == name


def test_build_tool_specs_shape_is_openai_compatible():
    specs = build_tool_specs(frozenset({"Read", "Grep"}))
    assert len(specs) == 2
    for spec in specs:
        assert spec["type"] == "function"
        assert "name" in spec["function"]
        assert "description" in spec["function"]
        assert "parameters" in spec["function"]
    names = sorted(s["function"]["name"] for s in specs)
    assert names == ["Grep", "Read"]


def test_build_tool_specs_is_sorted_deterministic():
    # Deterministic order helps test stability and cache keys.
    specs = build_tool_specs(frozenset({"Glob", "Grep", "Read"}))
    assert [s["function"]["name"] for s in specs] == ["Glob", "Grep", "Read"]


# --------------------------------------------------------------------------- #
# Path validation (_resolve_in_cwd)
# --------------------------------------------------------------------------- #


def test_resolve_in_cwd_accepts_relative_path(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    result = _resolve_in_cwd("a.txt", tmp_path)
    assert result == tmp_path.resolve() / "a.txt"


def test_resolve_in_cwd_rejects_dotdot_escape(tmp_path: Path):
    with pytest.raises(ToolExecutionError) as exc:
        _resolve_in_cwd("../outside.txt", tmp_path)
    assert "escapes" in str(exc.value)


def test_resolve_in_cwd_rejects_absolute_outside_cwd(tmp_path: Path):
    with pytest.raises(ToolExecutionError):
        _resolve_in_cwd("/etc/passwd", tmp_path)


def test_resolve_in_cwd_accepts_absolute_inside_cwd(tmp_path: Path):
    target = tmp_path / "inside.txt"
    target.write_text("x")
    result = _resolve_in_cwd(str(target), tmp_path)
    assert result == target.resolve()


def test_resolve_in_cwd_rejects_symlink_escape(tmp_path: Path):
    # A symlink inside cwd that points outside the workspace must be refused
    # even though the link itself lives under cwd.
    outside = tmp_path.parent / "outside-target.txt"
    outside.write_text("secret")
    link = tmp_path / "evil-link"
    link.symlink_to(outside)
    try:
        with pytest.raises(ToolExecutionError) as exc:
            _resolve_in_cwd("evil-link", tmp_path)
        assert "escapes" in str(exc.value)
    finally:
        outside.unlink()


def test_resolve_in_cwd_allows_nonexistent_path_under_cwd(tmp_path: Path):
    # Writes need to be able to resolve a not-yet-created file.
    result = _resolve_in_cwd("new-file.txt", tmp_path)
    assert result == tmp_path.resolve() / "new-file.txt"


# --------------------------------------------------------------------------- #
# ReadTool
# --------------------------------------------------------------------------- #


def test_read_tool_reads_utf8_file(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("hello world")
    tool = get_tool("Read")
    out = tool.execute({"path": "hello.txt"}, cwd=tmp_path)
    assert out == "hello world"


def test_read_tool_truncates_at_max_bytes(tmp_path: Path):
    (tmp_path / "big.txt").write_text("A" * 1000)
    tool = get_tool("Read")
    out = tool.execute({"path": "big.txt", "max_bytes": 100}, cwd=tmp_path)
    assert out.startswith("A" * 100)
    assert "truncated" in out


def test_read_tool_honours_offset(tmp_path: Path):
    (tmp_path / "offset.txt").write_text("0123456789")
    tool = get_tool("Read")
    out = tool.execute({"path": "offset.txt", "offset": 4}, cwd=tmp_path)
    assert out == "456789"


def test_read_tool_errors_on_missing_file(tmp_path: Path):
    tool = get_tool("Read")
    with pytest.raises(ToolExecutionError) as exc:
        tool.execute({"path": "missing.txt"}, cwd=tmp_path)
    assert "no such file" in str(exc.value)


def test_read_tool_errors_on_directory(tmp_path: Path):
    (tmp_path / "subdir").mkdir()
    tool = get_tool("Read")
    with pytest.raises(ToolExecutionError) as exc:
        tool.execute({"path": "subdir"}, cwd=tmp_path)
    assert "directory" in str(exc.value)


def test_read_tool_requires_path_param():
    tool = get_tool("Read")
    with pytest.raises(ToolSchemaError):
        tool.execute({}, cwd=Path("."))


def test_read_tool_rejects_escape(tmp_path: Path):
    tool = get_tool("Read")
    with pytest.raises(ToolExecutionError) as exc:
        tool.execute({"path": "../../../etc/passwd"}, cwd=tmp_path)
    assert "escapes" in str(exc.value)


def test_read_tool_rejects_bad_max_bytes(tmp_path: Path):
    (tmp_path / "a.txt").write_text("x")
    tool = get_tool("Read")
    with pytest.raises(ToolSchemaError):
        tool.execute({"path": "a.txt", "max_bytes": 0}, cwd=tmp_path)
    with pytest.raises(ToolSchemaError):
        tool.execute({"path": "a.txt", "max_bytes": 10_000_000}, cwd=tmp_path)


# --------------------------------------------------------------------------- #
# GrepTool
# --------------------------------------------------------------------------- #


def test_grep_tool_finds_matches(tmp_path: Path):
    (tmp_path / "code.py").write_text("def foo():\n    return 42\n\nclass Bar:\n    pass\n")
    (tmp_path / "other.py").write_text("hello\n")
    tool = get_tool("Grep")
    out = tool.execute({"pattern": r"def \w+"}, cwd=tmp_path)
    assert "code.py" in out
    assert "def foo" in out
    assert "other.py" not in out


def test_grep_tool_respects_file_pattern(tmp_path: Path):
    (tmp_path / "a.py").write_text("hit\n")
    (tmp_path / "a.md").write_text("hit\n")
    tool = get_tool("Grep")
    out = tool.execute(
        {"pattern": "hit", "file_pattern": "*.py"}, cwd=tmp_path
    )
    assert "a.py" in out
    assert "a.md" not in out


def test_grep_tool_case_insensitive(tmp_path: Path):
    (tmp_path / "c.txt").write_text("HELLO\n")
    tool = get_tool("Grep")
    out = tool.execute(
        {"pattern": "hello", "case_insensitive": True}, cwd=tmp_path
    )
    assert "HELLO" in out


def test_grep_tool_caps_at_max_results(tmp_path: Path):
    lines = "\n".join(["match me"] * 50)
    (tmp_path / "many.txt").write_text(lines + "\n")
    tool = get_tool("Grep")
    out = tool.execute({"pattern": "match", "max_results": 5}, cwd=tmp_path)
    assert "capped" in out


def test_grep_tool_invalid_regex_errors(tmp_path: Path):
    tool = get_tool("Grep")
    with pytest.raises(ToolSchemaError):
        tool.execute({"pattern": "(unclosed"}, cwd=tmp_path)


def test_grep_tool_no_matches_is_friendly(tmp_path: Path):
    (tmp_path / "empty.txt").write_text("nothing here\n")
    tool = get_tool("Grep")
    out = tool.execute({"pattern": r"zzz_never_"}, cwd=tmp_path)
    assert "no matches" in out


def test_grep_tool_skips_ignored_dirs(tmp_path: Path):
    (tmp_path / "keeper.txt").write_text("hit\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "inside.txt").write_text("hit\n")
    tool = get_tool("Grep")
    out = tool.execute({"pattern": "hit"}, cwd=tmp_path)
    assert "keeper.txt" in out
    assert "node_modules" not in out


# --------------------------------------------------------------------------- #
# GlobTool
# --------------------------------------------------------------------------- #


def test_glob_tool_matches_recursive(tmp_path: Path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.py").write_text("")
    (tmp_path / "b.py").write_text("")
    tool = get_tool("Glob")
    out = tool.execute({"pattern": "**/*.py"}, cwd=tmp_path)
    assert "b.py" in out
    assert os.path.join("a", "x.py") in out


def test_glob_tool_no_matches_is_friendly(tmp_path: Path):
    tool = get_tool("Glob")
    out = tool.execute({"pattern": "*.never"}, cwd=tmp_path)
    assert "no matches" in out


def test_glob_tool_rejects_nondir_path(tmp_path: Path):
    (tmp_path / "f.txt").write_text("")
    tool = get_tool("Glob")
    with pytest.raises(ToolExecutionError) as exc:
        tool.execute({"pattern": "*", "path": "f.txt"}, cwd=tmp_path)
    assert "not a directory" in str(exc.value)


# --------------------------------------------------------------------------- #
# ToolExecutor — dispatch + sandbox
# --------------------------------------------------------------------------- #


def test_executor_requires_existing_cwd(tmp_path: Path):
    missing = tmp_path / "nope"
    with pytest.raises(ValueError):
        ToolExecutor(cwd=missing, sandbox="read-only")


def test_executor_runs_read_tool(tmp_path: Path):
    (tmp_path / "ok.txt").write_text("yay")
    executor = ToolExecutor(cwd=tmp_path, sandbox="read-only")
    assert executor.run("Read", {"path": "ok.txt"}) == "yay"


def test_executor_refuses_tool_needing_higher_sandbox(tmp_path: Path):
    executor = ToolExecutor(cwd=tmp_path, sandbox="none")
    with pytest.raises(ToolExecutionError) as exc:
        executor.run("Read", {"path": "whatever.txt"})
    assert "sandbox" in str(exc.value)


def test_executor_wraps_schema_errors(tmp_path: Path):
    executor = ToolExecutor(cwd=tmp_path, sandbox="read-only")
    with pytest.raises(ToolExecutionError) as exc:
        executor.run("Read", {})  # missing `path`
    assert "bad parameters" in str(exc.value).lower() or "required" in str(exc.value)


def test_executor_surfaces_unknown_tool(tmp_path: Path):
    executor = ToolExecutor(cwd=tmp_path, sandbox="read-only")
    with pytest.raises(ToolExecutionError) as exc:
        executor.run("Mystery", {})
    assert "unknown tool" in str(exc.value).lower()


def test_executor_workspace_write_satisfies_read_only(tmp_path: Path):
    # read-only tools run fine under a broader sandbox.
    (tmp_path / "w.txt").write_text("hi")
    executor = ToolExecutor(cwd=tmp_path, sandbox="workspace-write")
    assert executor.run("Read", {"path": "w.txt"}) == "hi"


# --------------------------------------------------------------------------- #
# EditTool
# --------------------------------------------------------------------------- #


def test_edit_tool_unique_replace(tmp_path: Path):
    (tmp_path / "a.py").write_text("hello world\n")
    tool = get_tool("Edit")
    out = tool.execute(
        {"path": "a.py", "old_string": "world", "new_string": "universe"},
        cwd=tmp_path,
    )
    assert "Edited" in out
    assert (tmp_path / "a.py").read_text() == "hello universe\n"


def test_edit_tool_rejects_ambiguous_match(tmp_path: Path):
    (tmp_path / "dup.py").write_text("x\nx\n")
    tool = get_tool("Edit")
    with pytest.raises(ToolExecutionError) as exc:
        tool.execute(
            {"path": "dup.py", "old_string": "x", "new_string": "y"},
            cwd=tmp_path,
        )
    assert "2 times" in str(exc.value) or "appears" in str(exc.value)


def test_edit_tool_replace_all_when_requested(tmp_path: Path):
    (tmp_path / "dup.py").write_text("x\nx\n")
    tool = get_tool("Edit")
    out = tool.execute(
        {
            "path": "dup.py",
            "old_string": "x",
            "new_string": "y",
            "replace_all": True,
        },
        cwd=tmp_path,
    )
    assert "2 replacement" in out
    assert (tmp_path / "dup.py").read_text() == "y\ny\n"


def test_edit_tool_rejects_no_match(tmp_path: Path):
    (tmp_path / "a.py").write_text("abc")
    tool = get_tool("Edit")
    with pytest.raises(ToolExecutionError) as exc:
        tool.execute(
            {"path": "a.py", "old_string": "xyz", "new_string": "q"},
            cwd=tmp_path,
        )
    assert "not found" in str(exc.value)


def test_edit_tool_rejects_same_strings(tmp_path: Path):
    (tmp_path / "a.py").write_text("abc")
    tool = get_tool("Edit")
    with pytest.raises(ToolSchemaError):
        tool.execute(
            {"path": "a.py", "old_string": "abc", "new_string": "abc"},
            cwd=tmp_path,
        )


def test_edit_tool_requires_workspace_write(tmp_path: Path):
    (tmp_path / "a.txt").write_text("hello")
    executor = ToolExecutor(cwd=tmp_path, sandbox="read-only")
    with pytest.raises(ToolExecutionError) as exc:
        executor.run(
            "Edit", {"path": "a.txt", "old_string": "hello", "new_string": "bye"}
        )
    assert "sandbox" in str(exc.value)


def test_edit_tool_rejects_escape(tmp_path: Path):
    tool = get_tool("Edit")
    with pytest.raises(ToolExecutionError):
        tool.execute(
            {
                "path": "../outside.txt",
                "old_string": "a",
                "new_string": "b",
            },
            cwd=tmp_path,
        )


# --------------------------------------------------------------------------- #
# WriteTool
# --------------------------------------------------------------------------- #


def test_write_tool_creates_new_file(tmp_path: Path):
    tool = get_tool("Write")
    out = tool.execute(
        {"path": "new.txt", "content": "hello\n"}, cwd=tmp_path
    )
    assert "new.txt" in out
    assert (tmp_path / "new.txt").read_text() == "hello\n"


def test_write_tool_creates_parent_dirs(tmp_path: Path):
    tool = get_tool("Write")
    tool.execute(
        {"path": "a/b/c/nested.txt", "content": "x"}, cwd=tmp_path
    )
    assert (tmp_path / "a" / "b" / "c" / "nested.txt").read_text() == "x"


def test_write_tool_overwrites_existing(tmp_path: Path):
    (tmp_path / "e.txt").write_text("old")
    tool = get_tool("Write")
    tool.execute({"path": "e.txt", "content": "new"}, cwd=tmp_path)
    assert (tmp_path / "e.txt").read_text() == "new"


def test_write_tool_requires_workspace_write(tmp_path: Path):
    executor = ToolExecutor(cwd=tmp_path, sandbox="read-only")
    with pytest.raises(ToolExecutionError) as exc:
        executor.run("Write", {"path": "x.txt", "content": "y"})
    assert "sandbox" in str(exc.value)


def test_write_tool_rejects_escape(tmp_path: Path):
    tool = get_tool("Write")
    with pytest.raises(ToolExecutionError):
        tool.execute(
            {"path": "../evil.txt", "content": "x"}, cwd=tmp_path
        )


def test_write_tool_rejects_directory_path(tmp_path: Path):
    (tmp_path / "subdir").mkdir()
    tool = get_tool("Write")
    with pytest.raises(ToolExecutionError) as exc:
        tool.execute({"path": "subdir", "content": "x"}, cwd=tmp_path)
    assert "directory" in str(exc.value)


# --------------------------------------------------------------------------- #
# BashTool
# --------------------------------------------------------------------------- #


def test_bash_tool_runs_command(tmp_path: Path):
    (tmp_path / "a.txt").write_text("content")
    tool = get_tool("Bash")
    out = tool.execute({"command": "ls a.txt"}, cwd=tmp_path)
    assert "exit=0" in out
    assert "a.txt" in out


def test_bash_tool_captures_nonzero_exit(tmp_path: Path):
    tool = get_tool("Bash")
    out = tool.execute({"command": "false"}, cwd=tmp_path)
    assert "exit=1" in out


def test_bash_tool_captures_stderr(tmp_path: Path):
    tool = get_tool("Bash")
    out = tool.execute({"command": "echo oops >&2; false"}, cwd=tmp_path)
    assert "stderr" in out
    assert "oops" in out


def test_bash_tool_times_out(tmp_path: Path):
    tool = get_tool("Bash")
    out = tool.execute({"command": "sleep 5", "timeout_sec": 1}, cwd=tmp_path)
    assert "TIMEOUT" in out


def test_bash_tool_pins_cwd(tmp_path: Path):
    # pwd should report the resolved workspace path.
    tool = get_tool("Bash")
    out = tool.execute({"command": "pwd"}, cwd=tmp_path)
    assert str(tmp_path.resolve()) in out


def test_bash_tool_rejects_bad_timeout(tmp_path: Path):
    tool = get_tool("Bash")
    with pytest.raises(ToolSchemaError):
        tool.execute({"command": "true", "timeout_sec": 0}, cwd=tmp_path)
    with pytest.raises(ToolSchemaError):
        tool.execute({"command": "true", "timeout_sec": 99999}, cwd=tmp_path)


def test_bash_tool_requires_workspace_write(tmp_path: Path):
    executor = ToolExecutor(cwd=tmp_path, sandbox="read-only")
    with pytest.raises(ToolExecutionError) as exc:
        executor.run("Bash", {"command": "true"})
    assert "sandbox" in str(exc.value)


def test_bash_tool_truncates_huge_output(tmp_path: Path):
    tool = get_tool("Bash")
    out = tool.execute(
        {"command": "yes hello | head -c 300000"}, cwd=tmp_path
    )
    assert "truncated" in out
