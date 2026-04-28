"""ASCII hero banner for `conductor init`, `doctor`, and other splash moments.

Mirrors touchstone's ``tk_hero`` pattern but in Python and without the
figlet/gum runtime dependency: we ship the banner as a string literal
so it always renders, then ANSI-color it when a TTY is attached. Call
``render_banner()`` to get the lines as a list (testable) or
``print_banner()`` to write to stderr (doesn't interfere with stdout
capture in scripts).
"""

from __future__ import annotations

import os
import sys

# Rendered once from `figlet -f standard CONDUCTOR` and embedded here so
# we don't take a dependency on figlet at runtime. Keep the glyphs
# aligned — editors that strip trailing whitespace may break the art.
_CONDUCTOR_GLYPHS: tuple[str, ...] = (
    r"   ___                _            _               ",
    r"  / __\___  _ __   __| |_   _  ___| |_ ___  _ __   ",
    r" / /  / _ \| '_ \ / _` | | | |/ __| __/ _ \| '__|  ",
    r"/ /__| (_) | | | | (_| | |_| | (__| || (_) | |     ",
    r"\____/\___/|_| |_|\__,_|\__,_|\___|\__\___/|_|     ",
)

# Deep purple — distinguishes conductor from touchstone's orange and
# cortex's cyan/green when the four tools print side by side.
_ANSI_PURPLE = "\033[1;38;5;99m"
_ANSI_DIM = "\033[2m"
_ANSI_RESET = "\033[0m"


def _color_enabled(stream) -> bool:
    """True when the stream is a TTY and NO_COLOR is unset."""
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("CLICOLOR") == "0":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def render_banner(
    subtitle: str | None = None,
    version: str | None = None,
    *,
    use_color: bool = True,
) -> list[str]:
    """Return the banner as a list of lines (no trailing newline per line).

    When ``use_color`` is True, each glyph line is wrapped with the
    conductor purple ANSI code; subtitle/version lines use the dim code.
    Callers writing to a non-TTY should pass ``use_color=False``.
    """
    lines: list[str] = [""]
    for glyph in _CONDUCTOR_GLYPHS:
        if use_color:
            lines.append(f"  {_ANSI_PURPLE}{glyph}{_ANSI_RESET}")
        else:
            lines.append(f"  {glyph}")

    sub_parts: list[str] = []
    if subtitle:
        sub_parts.append(subtitle)
    if version:
        sub_parts.append(f"v{version}")
    if sub_parts:
        sub_text = "  ·  ".join(sub_parts)
        if use_color:
            lines.append(f"  {_ANSI_DIM}{sub_text}{_ANSI_RESET}")
        else:
            lines.append(f"  {sub_text}")
    lines.append("")
    return lines


def print_banner(
    subtitle: str | None = None,
    version: str | None = None,
    *,
    stream=None,
) -> None:
    """Write the banner to ``stream`` (default: stderr).

    Picks color based on whether ``stream`` is a TTY. Scripts that
    capture conductor's stdout are not disturbed because the banner
    lives on stderr.
    """
    target = stream if stream is not None else sys.stderr
    use_color = _color_enabled(target)
    for line in render_banner(subtitle, version, use_color=use_color):
        print(line, file=target)


def conductor_version() -> str | None:
    """Return the resolved conductor version for banner display."""
    from conductor import __version__

    return str(__version__) if __version__ else None


SUBTITLE_INIT = "pick an LLM, give it a job"
SUBTITLE_DOCTOR = "provider health check"


# --------------------------------------------------------------------------- #
# Caller-attribution banner — a one-liner emitted at the top of
# `conductor call` / `conductor exec` to announce that Conductor is doing
# the work, and (when detectable) name the LLM tool that invoked us.
#
# Goal: when Claude Code, Sentinel, etc. shell out to conductor, the user
# watching that tool's transcript sees "▸ Claude Code is using Conductor
# → kimi" — branding without breaking subprocess parsing (we write to
# stderr; consumers parse stdout).
# --------------------------------------------------------------------------- #

# (env-var, display-name). Order is the precedence when multiple markers
# are set (rare; first match wins). Status of each entry:
#   verified   — confirmed against the actual tool's environment.
#   convention — autumn-garage peer; we control both sides of the contract.
#
# Add new callers only after confirming their env footprint, so the
# registry never claims coverage we haven't tested.
_CALLER_REGISTRY: tuple[tuple[str, str], ...] = (
    ("CLAUDECODE", "Claude Code"),     # verified
    ("SENTINEL", "Sentinel"),          # convention (autumn-garage peer)
    ("TOUCHSTONE", "Touchstone"),      # convention (autumn-garage peer)
    # ("CURSOR_AGENT", "Cursor"),      # TODO: verify env footprint
    # ("AIDER_AGENT", "Aider"),        # TODO: verify env footprint
    # ("CODEX_CLI", "Codex CLI"),      # TODO: verify env footprint
    # ("GEMINI_CLI", "Gemini CLI"),    # TODO: verify env footprint
)


def detect_caller() -> str | None:
    """Return the display name of the calling LLM tool, or None.

    Probes env vars set by known callers (Claude Code, trio peers) in
    registry order; first match wins. Returns None when no marker is set
    — caller is unknown or conductor was invoked by a plain shell.
    """
    for env_var, name in _CALLER_REGISTRY:
        if os.environ.get(env_var):
            return name
    return None


def print_caller_banner(
    provider: str,
    *,
    stream=None,
    silent: bool = False,
) -> None:
    """Emit a one-line caller-attribution banner to stderr.

    Format:
      `▸ <Caller> is using Conductor → <provider>` when a caller is detected.
      `▸ Conductor → <provider>` when stderr is a TTY but no caller is
      detected (a human is watching but we don't know their tool).

    Stays silent when ``silent=True``, when no caller is detected AND
    stderr is not a TTY (the line would be neither attributable nor
    visible), or when writing fails (closed stream, broken pipe).
    Branding must never fail a real call — every error path is swallowed.
    """
    if silent:
        return
    target = stream if stream is not None else sys.stderr
    caller = detect_caller()

    try:
        is_tty = bool(target.isatty())
    except (AttributeError, ValueError):
        is_tty = False

    if caller is None and not is_tty:
        return

    use_color = _color_enabled(target)
    name = f"{_ANSI_PURPLE}Conductor{_ANSI_RESET}" if use_color else "Conductor"
    line = (
        f"▸ {caller} is using {name} → {provider}"
        if caller
        else f"▸ {name} → {provider}"
    )

    try:
        print(line, file=target)
    except (OSError, ValueError):
        return
