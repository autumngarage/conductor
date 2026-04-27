"""Persistent provider muting for doctor and auto-routing.

Muted providers live in ``~/.conductor/muted-providers.toml`` so users can
persistently opt out of providers they never want to configure or auto-pick.

File schema:

    muted = ["kimi", "ollama"]
"""

from __future__ import annotations

import tomllib
from typing import TYPE_CHECKING

from conductor.agent_wiring import conductor_home

if TYPE_CHECKING:
    from pathlib import Path


class MutedProvidersError(ValueError):
    """Raised when the muted-providers file is malformed."""


def muted_providers_file_path() -> Path:
    """Return the canonical muted-providers TOML path."""
    return conductor_home() / "muted-providers.toml"


def load_muted_provider_ids(*, known: set[str]) -> list[str]:
    """Load the muted provider list from disk.

    Missing file means "no muted providers". Invalid TOML or unknown provider
    identifiers are surfaced as explicit errors so broken local state doesn't
    silently change doctor or routing behavior.
    """
    path = muted_providers_file_path()
    if not path.exists():
        return []

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise MutedProvidersError(
            f"muted providers file {path} is not valid TOML: {e}. "
            "Fix by hand or remove the file to reset."
        ) from e

    raw = data.get("muted", [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise MutedProvidersError(
            f"muted providers file {path}: `muted` must be a list of provider IDs."
        )

    muted: list[str] = []
    seen: set[str] = set()
    for name in raw:
        if name not in known:
            raise MutedProvidersError(
                f"muted providers file {path}: unknown provider ID `{name}`. "
                f"Known: {sorted(known)}."
            )
        if name in seen:
            continue
        seen.add(name)
        muted.append(name)
    return muted


def save_muted_provider_ids(names: list[str]) -> Path:
    """Persist the exact muted provider list atomically."""
    path = muted_providers_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    unique_sorted = sorted(set(names))

    with tmp.open("w", encoding="utf-8") as f:
        f.write("# Conductor muted providers — managed by `conductor providers`.\n")
        f.write("# Providers listed here are hidden from doctor's available list\n")
        f.write("# and excluded from persistent auto-routing candidates.\n\n")
        muted_lit = ", ".join(f'"{_escape(name)}"' for name in unique_sorted)
        f.write(f"muted = [{muted_lit}]\n")

    tmp.replace(path)
    return path


def mute_provider_ids(names: list[str], *, known: set[str]) -> tuple[Path, list[str]]:
    """Add providers to the muted set. Returns (path, newly_muted)."""
    _validate_names(names, known=known)
    current = load_muted_provider_ids(known=known)
    current_set = set(current)
    added = [name for name in names if name not in current_set]
    path = save_muted_provider_ids(current + added)
    return path, added


def unmute_provider_ids(names: list[str], *, known: set[str]) -> tuple[Path, list[str]]:
    """Remove providers from the muted set. Returns (path, removed)."""
    _validate_names(names, known=known)
    current = load_muted_provider_ids(known=known)
    remove = set(names)
    kept = [name for name in current if name not in remove]
    removed = [name for name in current if name in remove]
    path = save_muted_provider_ids(kept)
    return path, removed


def _validate_names(names: list[str], *, known: set[str]) -> None:
    for name in names:
        if name not in known:
            raise MutedProvidersError(
                f"unknown provider {name!r}; known: {sorted(known)}"
            )


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
