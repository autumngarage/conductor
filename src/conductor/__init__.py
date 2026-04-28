from __future__ import annotations

import logging
import subprocess
from pathlib import Path

_LOG = logging.getLogger(__name__)


def _generated_version() -> str | None:
    try:
        from conductor._version import __version__ as value
    except ImportError:
        return None
    return value


def _git_root(start: Path) -> Path | None:
    source_file = start.resolve()
    for candidate in (start, *start.parents):
        if (
            (candidate / ".git").exists()
            and (candidate / "src" / "conductor" / "__init__.py").resolve()
            == source_file
        ):
            return candidate
    return None


def _parse_git_describe_version(value: str) -> str | None:
    raw = value.strip()
    dirty = raw.endswith("-dirty")
    if dirty:
        raw = raw[: -len("-dirty")]
    try:
        tag, distance, sha = raw.rsplit("-", 2)
    except ValueError:
        return raw.removeprefix("v") if raw.startswith("v") else raw
    if not sha.startswith("g") or not distance.isdigit():
        return None

    base = tag.removeprefix("v")
    if distance == "0":
        return f"{base}+dirty" if dirty else base

    local = f"{distance}.{sha}"
    if dirty:
        local += ".dirty"
    return f"{base}+{local}"


def _version_from_git_checkout() -> str | None:
    root = _git_root(Path(__file__).resolve())
    if root is None:
        return None
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "describe",
                "--tags",
                "--long",
                "--dirty",
                "--match",
                "v[0-9]*",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=1,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        _LOG.debug("Unable to derive conductor version from git checkout", exc_info=True)
        return None
    return _parse_git_describe_version(result.stdout)


def _resolve_version() -> str:
    return _version_from_git_checkout() or _generated_version() or "0.0.0+unknown"


__version__ = _resolve_version()
