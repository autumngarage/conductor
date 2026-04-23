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
from collections.abc import Sequence

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
    """Return the installed conductor version, or None if unknown.

    ``hatch-vcs`` writes ``_version.py`` at build time. Tests and editable
    installs may not have it, so we fail soft.
    """
    try:
        from conductor._version import __version__
        return str(__version__)
    except Exception:
        return None


SUBTITLE_INIT = "pick an LLM, give it a job"
SUBTITLE_DOCTOR = "provider health check"
