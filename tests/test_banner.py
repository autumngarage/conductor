"""Tests for conductor.banner — the ASCII hero shared with init/doctor."""

from __future__ import annotations

import io

from conductor.banner import print_banner, render_banner


def test_render_without_color_is_plain_ascii():
    lines = render_banner("pick an LLM", "0.3.0", use_color=False)
    # No ANSI escape codes anywhere.
    for line in lines:
        assert "\033" not in line
    # Contains the CONDUCTOR glyph-art markers somewhere.
    joined = "\n".join(lines)
    assert "__" in joined  # glyph bars


def test_render_with_color_wraps_lines_in_ansi():
    lines = render_banner("pick an LLM", "0.3.0", use_color=True)
    glyph_lines = [ln for ln in lines if "__" in ln]
    assert glyph_lines, "expected at least one glyph line"
    for gl in glyph_lines:
        assert "\033[" in gl  # starts a color code
        assert "\033[0m" in gl  # and resets it


def test_render_includes_subtitle_and_version_joined():
    lines = render_banner("for science", "1.2.3", use_color=False)
    sub_lines = [ln for ln in lines if "for science" in ln]
    assert sub_lines
    assert "v1.2.3" in sub_lines[0]


def test_render_without_subtitle_or_version_has_blank_padding():
    lines = render_banner(use_color=False)
    # First and last lines are blank for visual padding.
    assert lines[0] == ""
    assert lines[-1] == ""


def test_print_banner_writes_to_supplied_stream():
    buf = io.StringIO()
    print_banner("hi", "0.0.1", stream=buf)
    output = buf.getvalue()
    assert "hi" in output
    assert "v0.0.1" in output
    # StringIO isn't a TTY, so color should be suppressed.
    assert "\033[" not in output


def test_print_banner_respects_no_color_env(monkeypatch):
    class FakeTTY(io.StringIO):
        def isatty(self):
            return True

    buf = FakeTTY()
    monkeypatch.setenv("NO_COLOR", "1")
    print_banner("x", stream=buf)
    assert "\033[" not in buf.getvalue()
