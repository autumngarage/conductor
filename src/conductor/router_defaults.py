"""Persistent tag-default preferences for auto-routing.

Home config lives at ``~/.config/conductor/router.toml`` and may be
overridden per repo via ``./.conductor/router.toml``. The repo-local file
loads after the home file, so matching keys replace the home defaults.

File schema:

    [tag_defaults]
    code-review = "codex"
    long-context = "gemini"
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

HOME_CONFIG_ENV = "CONDUCTOR_ROUTER_DEFAULTS_FILE"
REPO_CONFIG_ENV = "CONDUCTOR_REPO_ROUTER_DEFAULTS_FILE"
DEFAULT_REPO_PATH = Path(".conductor") / "router.toml"


class RouterDefaultsError(ValueError):
    """Raised when router-default config is malformed."""


def home_router_defaults_path() -> Path:
    override = os.environ.get(HOME_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "conductor" / "router.toml"


def repo_router_defaults_path(cwd: Path | None = None) -> Path:
    override = os.environ.get(REPO_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    root = cwd or Path.cwd()
    return root / DEFAULT_REPO_PATH


def load_tag_defaults(*, cwd: Path | None = None) -> dict[str, str]:
    """Load layered tag defaults, with repo-local entries overriding home."""
    merged: dict[str, str] = {}
    for path in (home_router_defaults_path(), repo_router_defaults_path(cwd)):
        if not path.exists():
            continue
        merged.update(_load_one(path))
    return merged


def save_home_tag_defaults(tag_defaults: dict[str, str]) -> Path:
    """Replace the home router-default config atomically."""
    path = home_router_defaults_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write("# Conductor router defaults — managed by `conductor router defaults`.\n")
        f.write("# Matching task tags prefer the named provider when it is available.\n\n")
        f.write("[tag_defaults]\n")
        for tag, provider in sorted(tag_defaults.items()):
            f.write(f'{tag} = "{_escape(provider)}"\n')
    tmp.replace(path)
    return path


def set_home_tag_default(tag: str, provider: str) -> Path:
    tag = _normalize_key(tag, field="tag")
    provider = _normalize_key(provider, field="provider")
    defaults = load_home_tag_defaults()
    defaults[tag] = provider
    return save_home_tag_defaults(defaults)


def unset_home_tag_default(tag: str) -> tuple[Path, bool]:
    tag = _normalize_key(tag, field="tag")
    defaults = load_home_tag_defaults()
    existed = tag in defaults
    if existed:
        del defaults[tag]
    path = save_home_tag_defaults(defaults)
    return path, existed


def load_home_tag_defaults() -> dict[str, str]:
    path = home_router_defaults_path()
    if not path.exists():
        return {}
    return _load_one(path)


def _load_one(path: Path) -> dict[str, str]:
    from conductor.providers import known_providers

    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise RouterDefaultsError(
            f"router defaults file {path} is not valid TOML: {e}. "
            "Fix by hand or remove the file to reset."
        ) from e

    raw = data.get("tag_defaults", {})
    if not isinstance(raw, dict):
        raise RouterDefaultsError(
            f"router defaults file {path}: `tag_defaults` must be a TOML table."
        )

    parsed: dict[str, str] = {}
    known = set(known_providers())
    for tag, provider in raw.items():
        if not isinstance(tag, str) or not tag.strip():
            raise RouterDefaultsError(
                f"router defaults file {path}: tag names must be non-empty strings."
            )
        if not isinstance(provider, str) or not provider.strip():
            raise RouterDefaultsError(
                f"router defaults file {path}: `tag_defaults.{tag}` must be a non-empty string."
            )
        normalized_tag = tag.strip()
        normalized_provider = provider.strip()
        if normalized_provider not in known:
            raise RouterDefaultsError(
                f"router defaults file {path}: `tag_defaults.{normalized_tag}` names unknown "
                f"provider `{normalized_provider}`. Known: {sorted(known)}."
            )
        parsed[normalized_tag] = normalized_provider
    return parsed


def _normalize_key(value: str, *, field: str) -> str:
    stripped = value.strip()
    if not stripped:
        raise RouterDefaultsError(f"{field} must be a non-empty string.")
    return stripped


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')
