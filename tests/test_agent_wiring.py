"""Tests for conductor.agent_wiring — detection, managed-file round-trips,
sentinel-block injection/removal, wire + unwire end-to-end.

Every test isolates the environment via ``CONDUCTOR_HOME`` / ``CLAUDE_HOME``
pointed at ``tmp_path`` so nothing leaks onto the developer's real home.
"""

from __future__ import annotations

import pytest

from conductor import agent_wiring as aw


@pytest.fixture(autouse=True)
def _isolated_homes(tmp_path, monkeypatch):
    """Point every path helper at tmp_path; remove any inherited which()."""
    conductor_dir = tmp_path / ".conductor"
    claude_dir = tmp_path / ".claude"
    monkeypatch.setenv("CONDUCTOR_HOME", str(conductor_dir))
    monkeypatch.setenv("CLAUDE_HOME", str(claude_dir))
    # Default: claude CLI not on PATH unless a test explicitly patches it.
    monkeypatch.setattr("shutil.which", lambda _cmd: None)
    yield


# --------------------------------------------------------------------------- #
# Path helpers.
# --------------------------------------------------------------------------- #


def test_conductor_home_respects_override(tmp_path, monkeypatch):
    target = tmp_path / "custom-conductor"
    monkeypatch.setenv("CONDUCTOR_HOME", str(target))
    assert aw.conductor_home() == target


def test_claude_home_respects_override(tmp_path, monkeypatch):
    target = tmp_path / "custom-claude"
    monkeypatch.setenv("CLAUDE_HOME", str(target))
    assert aw.claude_home() == target


# --------------------------------------------------------------------------- #
# Detection.
# --------------------------------------------------------------------------- #


def test_detect_empty_env_reports_no_claude():
    d = aw.detect()
    assert d.claude_detected is False
    assert d.claude_cli_on_path is False
    assert d.claude_home_exists is False
    assert d.managed == ()


def test_detect_claude_home_dir_implies_detected():
    aw.claude_home().mkdir(parents=True)
    d = aw.detect()
    assert d.claude_detected is True
    assert d.claude_home_exists is True
    assert d.managed == ()


def test_detect_claude_cli_on_path_implies_detected(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None)
    d = aw.detect()
    assert d.claude_detected is True
    assert d.claude_cli_on_path is True


def test_detect_picks_up_managed_files_after_wire():
    aw.wire_claude_code("0.3.2", patch_claude_md=True)
    d = aw.detect()
    assert d.claude_detected is True
    kinds = {a.kind for a in d.managed}
    assert {"guidance", "slash-command", "subagent", "claude-md-import"}.issubset(kinds)


def test_detect_ignores_user_owned_files_at_managed_paths(tmp_path):
    """A hand-authored file at ~/.claude/agents/kimi-long-context.md is user-owned
    and must not be claimed by conductor's detector."""
    agents_dir = aw.claude_home() / "agents"
    agents_dir.mkdir(parents=True)
    user_file = agents_dir / "kimi-long-context.md"
    user_file.write_text("# my own agent, not conductor's\n", encoding="utf-8")

    d = aw.detect()
    # The user's file exists but it's not claimed.
    assert all(a.path != user_file for a in d.managed)


# --------------------------------------------------------------------------- #
# Managed-file helpers.
# --------------------------------------------------------------------------- #


def test_write_managed_markdown_round_trip(tmp_path):
    path = tmp_path / "notes.md"
    aw.write_managed_markdown(path, "# hello\n\nbody\n", version="1.2.3")
    content = path.read_text(encoding="utf-8")
    assert content.splitlines()[0].startswith("<!-- managed-by: conductor v1.2.3")
    assert "# hello" in content
    assert aw.is_managed_file(path)
    assert aw.read_managed_version(path) == "1.2.3"


def test_write_managed_markdown_overwrites_existing_managed(tmp_path):
    path = tmp_path / "notes.md"
    aw.write_managed_markdown(path, "first", version="1.0.0")
    aw.write_managed_markdown(path, "second", version="1.1.0")
    assert aw.read_managed_version(path) == "1.1.0"
    assert "second" in path.read_text(encoding="utf-8")


def test_write_managed_markdown_refuses_user_owned(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# I wrote this myself\n", encoding="utf-8")
    with pytest.raises(aw.UserOwnedFileError):
        aw.write_managed_markdown(path, "conductor content", version="1.0.0")
    # File is untouched.
    assert path.read_text(encoding="utf-8") == "# I wrote this myself\n"


def test_write_managed_frontmatter_embeds_version(tmp_path):
    path = tmp_path / "agent.md"
    aw.write_managed_frontmatter(
        path,
        {"name": "test", "description": "A test"},
        "You are a test.",
        version="2.0.0",
    )
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "managed-by: conductor v2.0.0" in text
    assert "name: test" in text
    assert "You are a test." in text
    assert aw.is_managed_file(path)
    assert aw.read_managed_version(path) == "2.0.0"


def test_write_managed_frontmatter_refuses_user_owned(tmp_path):
    path = tmp_path / "agent.md"
    path.write_text("---\nname: mine\n---\nhello", encoding="utf-8")
    with pytest.raises(aw.UserOwnedFileError):
        aw.write_managed_frontmatter(
            path, {"name": "test"}, "body", version="1.0.0"
        )


def test_read_managed_version_on_plain_file_returns_none(tmp_path):
    path = tmp_path / "notes.md"
    path.write_text("# just a file\n", encoding="utf-8")
    assert aw.read_managed_version(path) is None
    assert aw.is_managed_file(path) is False


def test_read_managed_version_on_missing_file_returns_none(tmp_path):
    assert aw.read_managed_version(tmp_path / "nope.md") is None


# --------------------------------------------------------------------------- #
# Sentinel-block injection / removal.
# --------------------------------------------------------------------------- #


def test_inject_sentinel_block_creates_new_file(tmp_path):
    path = tmp_path / "CLAUDE.md"
    aw.inject_sentinel_block(path, "@some/path.md", version="1.0.0")
    text = path.read_text(encoding="utf-8")
    assert "<!-- conductor:begin v1.0.0 -->" in text
    assert "@some/path.md" in text
    assert text.rstrip().endswith("<!-- conductor:end -->")


def test_inject_sentinel_block_appends_to_existing(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# My own instructions\n\nDo the thing.\n", encoding="utf-8")
    aw.inject_sentinel_block(path, "@x", version="1.0.0")
    text = path.read_text(encoding="utf-8")
    assert "# My own instructions" in text
    assert "Do the thing." in text
    assert "<!-- conductor:begin v1.0.0 -->" in text
    assert text.index("Do the thing.") < text.index("<!-- conductor:begin")


def test_inject_sentinel_block_replaces_existing_block(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# header\n", encoding="utf-8")
    aw.inject_sentinel_block(path, "@v1", version="1.0.0")
    aw.inject_sentinel_block(path, "@v2", version="2.0.0")
    text = path.read_text(encoding="utf-8")
    assert text.count("conductor:begin") == 1
    assert "@v2" in text
    assert "@v1" not in text
    assert "v2.0.0" in text
    assert "# header" in text


def test_remove_sentinel_block_preserves_user_content(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# mine\n", encoding="utf-8")
    aw.inject_sentinel_block(path, "@x", version="1.0.0")
    assert aw.remove_sentinel_block(path) is True
    text = path.read_text(encoding="utf-8")
    assert "# mine" in text
    assert "conductor:begin" not in text
    assert "conductor:end" not in text


def test_remove_sentinel_block_deletes_file_if_only_content(tmp_path):
    path = tmp_path / "CLAUDE.md"
    aw.inject_sentinel_block(path, "@x", version="1.0.0")
    assert aw.remove_sentinel_block(path) is True
    assert not path.exists()


def test_remove_sentinel_block_on_unmanaged_file_returns_false(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text("# user-owned\n", encoding="utf-8")
    assert aw.remove_sentinel_block(path) is False
    assert path.read_text(encoding="utf-8") == "# user-owned\n"


# --------------------------------------------------------------------------- #
# wire_claude_code end-to-end.
# --------------------------------------------------------------------------- #


def test_wire_claude_code_writes_all_artifacts():
    report = aw.wire_claude_code("0.3.2", patch_claude_md=True)
    assert report.skipped == ()
    assert report.patched_claude_md is True

    expected_paths = {
        aw.conductor_home() / "delegation-guidance.md",
        aw.claude_home() / "commands" / "conductor.md",
        aw.claude_home() / "agents" / "kimi-long-context.md",
        aw.claude_home() / "agents" / "gemini-web-search.md",
    }
    assert set(report.written) == expected_paths
    for p in expected_paths:
        assert p.exists()
        assert aw.is_managed_file(p)

    claude_md = aw.claude_home() / "CLAUDE.md"
    assert claude_md.exists()
    text = claude_md.read_text(encoding="utf-8")
    assert "conductor:begin v0.3.2" in text
    assert "@" in text and "delegation-guidance.md" in text


def test_wire_claude_code_patch_false_leaves_claude_md_alone():
    report = aw.wire_claude_code("0.3.2", patch_claude_md=False)
    assert report.patched_claude_md is False
    assert not (aw.claude_home() / "CLAUDE.md").exists()


def test_wire_claude_code_idempotent_on_second_run():
    aw.wire_claude_code("0.3.2", patch_claude_md=True)
    report = aw.wire_claude_code("0.3.3", patch_claude_md=True)
    # Every file overwritten cleanly with new version.
    assert report.skipped == ()
    for p in report.written:
        assert aw.read_managed_version(p) == "0.3.3"
    # Sentinel block updated to new version, only one present.
    claude_md = aw.claude_home() / "CLAUDE.md"
    text = claude_md.read_text(encoding="utf-8")
    assert text.count("conductor:begin") == 1
    assert "v0.3.3" in text


def test_wire_claude_code_skips_user_owned_file_at_managed_path():
    """If a user already has ~/.claude/agents/kimi-long-context.md (not ours),
    wiring must skip it and report — never overwrite."""
    agents_dir = aw.claude_home() / "agents"
    agents_dir.mkdir(parents=True)
    user_file = agents_dir / "kimi-long-context.md"
    user_file.write_text("# my hand-written agent\n", encoding="utf-8")

    report = aw.wire_claude_code("0.3.2", patch_claude_md=False)
    # The user's file is in skipped, not written.
    skipped_paths = {path for path, _ in report.skipped}
    assert user_file in skipped_paths
    assert user_file not in report.written
    # File content preserved.
    assert user_file.read_text(encoding="utf-8") == "# my hand-written agent\n"


# --------------------------------------------------------------------------- #
# unwire end-to-end.
# --------------------------------------------------------------------------- #


def test_unwire_removes_all_managed_files():
    aw.wire_claude_code("0.3.2", patch_claude_md=True)
    report = aw.unwire()
    assert len(report.removed) >= 4  # guidance + slash + 2 subagents + claude.md
    for p in [
        aw.conductor_home() / "delegation-guidance.md",
        aw.claude_home() / "commands" / "conductor.md",
        aw.claude_home() / "agents" / "kimi-long-context.md",
        aw.claude_home() / "agents" / "gemini-web-search.md",
    ]:
        assert not p.exists()
    # CLAUDE.md had only our block — should be gone.
    assert not (aw.claude_home() / "CLAUDE.md").exists()


def test_unwire_preserves_user_content_in_claude_md():
    claude_md = aw.claude_home() / "CLAUDE.md"
    claude_md.parent.mkdir(parents=True)
    claude_md.write_text("# My own CLAUDE.md\n\nDo X.\n", encoding="utf-8")

    aw.wire_claude_code("0.3.2", patch_claude_md=True)
    aw.unwire()

    # File still exists; user content preserved; no conductor block.
    text = claude_md.read_text(encoding="utf-8")
    assert "# My own CLAUDE.md" in text
    assert "Do X." in text
    assert "conductor:begin" not in text


def test_unwire_skips_user_owned_files_at_managed_paths():
    agents_dir = aw.claude_home() / "agents"
    agents_dir.mkdir(parents=True)
    user_file = agents_dir / "kimi-long-context.md"
    user_file.write_text("# my hand-written agent\n", encoding="utf-8")

    report = aw.unwire()
    # User's file was skipped (reason recorded) and not deleted.
    skipped_paths = {path for path, _ in report.skipped}
    assert user_file in skipped_paths
    assert user_file.exists()


def test_unwire_on_clean_env_is_noop():
    report = aw.unwire()
    assert report.removed == ()
    assert report.skipped == ()


def test_wire_then_unwire_then_wire_round_trip():
    aw.wire_claude_code("0.3.2", patch_claude_md=True)
    aw.unwire()
    # Second wiring works cleanly from an unwired state.
    report = aw.wire_claude_code("0.3.3", patch_claude_md=True)
    assert report.skipped == ()
    assert all(aw.read_managed_version(p) == "0.3.3" for p in report.written)


# --------------------------------------------------------------------------- #
# Version extraction edge cases.
# --------------------------------------------------------------------------- #


def test_version_extraction_handles_prerelease(tmp_path):
    path = tmp_path / "notes.md"
    aw.write_managed_markdown(path, "body", version="0.4.0.dev1+g1234abc")
    assert aw.read_managed_version(path) == "0.4.0.dev1+g1234abc"


def test_managed_path_stays_out_of_arbitrary_dirs(tmp_path):
    """Writing should only create dirs *inside* the configured homes."""
    aw.write_managed_markdown(
        aw.conductor_home() / "delegation-guidance.md", "x", version="1.0.0"
    )
    # The conductor_home dir now exists, but nothing else was created.
    assert aw.conductor_home().is_dir()
    assert not (tmp_path / "other").exists()
