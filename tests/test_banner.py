"""Tests for conductor.banner — the ASCII hero shared with init/doctor,
plus the caller-attribution banner emitted by `conductor call` / `exec`."""

from __future__ import annotations

import io

from conductor.banner import (
    _CALLER_REGISTRY,
    detect_caller,
    print_banner,
    print_caller_banner,
    render_banner,
)


def _strip_caller_env(monkeypatch) -> None:
    """Clear every env var the caller registry knows about.

    The test process inherits whatever the calling shell has set
    (e.g. CLAUDECODE=1 when these tests run inside Claude Code), so
    deterministic tests must wipe the registry's footprint first.
    """
    for env_var, _ in _CALLER_REGISTRY:
        monkeypatch.delenv(env_var, raising=False)


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


def test_render_includes_autumn_garage_attribution():
    lines = render_banner("pick an LLM", "0.3.0", use_color=True)
    # The attribution line ends with the literal text and lives in the
    # banner output regardless of subtitle/version.
    attribution = [ln for ln in lines if ln.rstrip("\033[0m").endswith("by Autumn Garage")]
    assert attribution, "expected a 'by Autumn Garage' line"


def test_render_attribution_is_plain_when_color_disabled():
    lines = render_banner(use_color=False)
    plain = [ln for ln in lines if ln.strip() == "by Autumn Garage"]
    assert plain, "expected a plain 'by Autumn Garage' line when use_color=False"
    # And no ANSI escapes anywhere in the rendered output.
    for line in lines:
        assert "\033" not in line


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


# --------------------------------------------------------------------------- #
# Caller-attribution banner
# --------------------------------------------------------------------------- #


class _FakeTTY(io.StringIO):
    """StringIO that reports as a TTY (and accepts a NO_COLOR-cleared env)."""

    def isatty(self):
        return True


def test_detect_caller_returns_none_when_no_markers(monkeypatch):
    _strip_caller_env(monkeypatch)
    assert detect_caller() is None


def test_detect_caller_returns_claude_code_when_claudecode_set(monkeypatch):
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    assert detect_caller() == "Claude Code"


def test_detect_caller_returns_verified_external_callers(monkeypatch):
    for env_var, caller_name in (
        ("CURSOR_AGENT", "Cursor"),
        ("CODEX_THREAD_ID", "Codex CLI"),
        ("GEMINI_CLI", "Gemini CLI"),
    ):
        _strip_caller_env(monkeypatch)
        monkeypatch.setenv(env_var, "1")
        assert detect_caller() == caller_name


def test_detect_caller_first_match_wins(monkeypatch):
    """When multiple markers are set, the registry's order decides."""
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("SENTINEL", "1")
    # Registry orders Claude Code before Sentinel; verify that holds.
    assert detect_caller() == "Claude Code"


def test_detect_caller_first_match_wins_for_external_callers(monkeypatch):
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("GEMINI_CLI", "1")
    monkeypatch.setenv("CODEX_THREAD_ID", "thread-id")
    monkeypatch.setenv("CURSOR_AGENT", "1")
    assert detect_caller() == "Cursor"


def test_print_caller_banner_named_caller_is_attributed(monkeypatch):
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("NO_COLOR", "1")  # suppress ANSI for cleaner asserts
    buf = io.StringIO()  # not a TTY, but caller-detection forces emit
    print_caller_banner("kimi", stream=buf)
    out = buf.getvalue()
    assert "Claude Code is using Conductor" in out
    assert "→ kimi" in out


def test_print_caller_banner_generic_line_on_tty_without_caller(monkeypatch):
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = _FakeTTY()
    print_caller_banner("kimi", stream=buf)
    out = buf.getvalue()
    assert "Conductor" in out
    assert "→ kimi" in out
    assert "is using" not in out  # no caller attribution


def test_print_caller_banner_silent_when_no_caller_and_not_tty(monkeypatch):
    _strip_caller_env(monkeypatch)
    buf = io.StringIO()  # not a TTY, no caller — neither attributable nor visible
    print_caller_banner("kimi", stream=buf)
    assert buf.getvalue() == ""


def test_print_caller_banner_silent_flag_overrides_caller(monkeypatch):
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    buf = io.StringIO()
    print_caller_banner("kimi", stream=buf, silent=True)
    assert buf.getvalue() == ""


def test_print_caller_banner_colors_conductor_word_on_tty(monkeypatch):
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("CLICOLOR", raising=False)
    buf = _FakeTTY()
    print_caller_banner("kimi", stream=buf)
    out = buf.getvalue()
    # The literal "Conductor" word is wrapped in the purple ANSI code
    # when stderr is a TTY; surrounding text stays plain.
    assert "\033[38;5;147mConductor\033[0m" in out


def test_print_caller_banner_does_not_crash_on_closed_stream(monkeypatch):
    """Branding must never fail a real call — closed/broken streams swallow."""
    _strip_caller_env(monkeypatch)
    monkeypatch.setenv("CLAUDECODE", "1")
    buf = io.StringIO()
    buf.close()
    # Should not raise.
    print_caller_banner("kimi", stream=buf)
