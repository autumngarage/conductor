"""Named CLI profiles for ``conductor call`` / ``conductor exec``.

Profiles live in a user-local TOML file, defaulting to
``~/.config/conductor/profiles.toml``. Built-ins ship in code and user
entries override built-ins by name.

File schema:

    [profiles.my-coding]
    prefer = "best"
    effort = "medium"
    tags = "coding,tool-use"
    sandbox = "workspace-write"
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_ENV = "CONDUCTOR_PROFILES_FILE"
DEFAULT_PROFILES_PATH = Path.home() / ".config" / "conductor" / "profiles.toml"

VALID_PREFER_MODES = ("best", "cheapest", "fastest", "balanced")
VALID_SANDBOXES = ("read-only", "workspace-write", "strict", "none")
VALID_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "max")


@dataclass(frozen=True)
class ProfileSpec:
    name: str
    prefer: str | None = None
    effort: str | None = None
    tags: str | None = None
    sandbox: str | None = None
    source: str = "built-in"


class ProfileError(ValueError):
    """Raised when the profile config is malformed or a name is unknown."""


BUILTIN_PROFILES: dict[str, ProfileSpec] = {
    "coding": ProfileSpec(
        name="coding",
        prefer="best",
        # OpenAI's Codex prompting guide recommends medium as the default
        # reasoning effort; reserve high/xhigh-style settings for the
        # hardest tasks instead of making every autonomous coding run pay
        # the latency/cost tax by default.
        effort="medium",
        tags="coding,tool-use",
        sandbox="workspace-write",
        source="built-in",
    ),
    "review": ProfileSpec(
        name="review",
        prefer="balanced",
        effort="medium",
        tags="code-review",
        sandbox="read-only",
        source="built-in",
    ),
    "chat": ProfileSpec(
        name="chat",
        prefer="cheapest",
        effort="low",
        tags="cheap",
        sandbox="none",
        source="built-in",
    ),
}


def profiles_file_path() -> Path:
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_PROFILES_PATH


def load_profiles() -> dict[str, ProfileSpec]:
    """Return built-ins plus user overrides from ``profiles.toml``."""
    profiles = dict(BUILTIN_PROFILES)
    profiles.update(_load_user_profiles())
    return profiles


def get_profile(name: str) -> ProfileSpec:
    profiles = load_profiles()
    if name not in profiles:
        known = ", ".join(sorted(profiles))
        raise ProfileError(
            f"unknown profile {name!r}. Known profiles: {known}."
        )
    return profiles[name]


def _load_user_profiles() -> dict[str, ProfileSpec]:
    path = profiles_file_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(
            f"profile config {path} is not valid TOML: {e}. "
            "Fix the file or remove it to fall back to built-ins."
        ) from e

    raw_profiles = data.get("profiles") or {}
    if not isinstance(raw_profiles, dict):
        raise ProfileError(
            f"profile config {path}: `profiles` must be a TOML table."
        )

    parsed: dict[str, ProfileSpec] = {}
    for name, raw in raw_profiles.items():
        parsed[name] = _profile_from_dict(name, raw, source_path=path)
    return parsed


def _profile_from_dict(name: str, raw: object, *, source_path: Path) -> ProfileSpec:
    if not isinstance(name, str) or not name.strip():
        raise ProfileError(
            f"profile config {source_path}: profile names must be non-empty strings."
        )
    if not isinstance(raw, dict):
        raise ProfileError(
            f"profile config {source_path}: `profiles.{name}` must be a TOML table."
        )

    allowed = {"prefer", "effort", "tags", "sandbox"}
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise ProfileError(
            f"profile config {source_path}: `profiles.{name}` has unknown keys: {unknown}."
        )

    prefer = _optional_string(raw.get("prefer"), field="prefer", name=name, source_path=source_path)
    effort = _optional_string(
        raw.get("effort"),
        field="effort",
        name=name,
        source_path=source_path,
    )
    tags = _tags_string(raw.get("tags"), name=name, source_path=source_path)
    sandbox = _optional_string(
        raw.get("sandbox"),
        field="sandbox",
        name=name,
        source_path=source_path,
    )

    if prefer is not None and prefer not in VALID_PREFER_MODES:
        raise ProfileError(
            f"profile config {source_path}: `profiles.{name}.prefer` must be one of "
            f"{list(VALID_PREFER_MODES)}, got {prefer!r}."
        )
    if effort is not None:
        if effort.lstrip("-").isdigit():
            if int(effort) < 0:
                raise ProfileError(
                    f"profile config {source_path}: `profiles.{name}.effort` must be "
                    f">= 0 when given as an integer, got {effort!r}."
                )
        elif effort not in VALID_EFFORT_LEVELS:
            raise ProfileError(
                f"profile config {source_path}: `profiles.{name}.effort` must be one of "
                f"{list(VALID_EFFORT_LEVELS)} or a non-negative integer string, got "
                f"{effort!r}."
            )
    if sandbox is not None and sandbox not in VALID_SANDBOXES:
        raise ProfileError(
            f"profile config {source_path}: `profiles.{name}.sandbox` must be one of "
            f"{list(VALID_SANDBOXES)}, got {sandbox!r}."
        )

    return ProfileSpec(
        name=name,
        prefer=prefer,
        effort=effort,
        tags=tags,
        sandbox=sandbox,
        source=str(source_path),
    )


def _optional_string(
    value: object,
    *,
    field: str,
    name: str,
    source_path: Path,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ProfileError(
            f"profile config {source_path}: `profiles.{name}.{field}` must be a "
            "non-empty string."
        )
    return value.strip()


def _tags_string(value: object, *, name: str, source_path: Path) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            raise ProfileError(
                f"profile config {source_path}: `profiles.{name}.tags` must not be empty."
            )
        return stripped
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        tags = [item.strip() for item in value if item.strip()]
        if not tags:
            raise ProfileError(
                f"profile config {source_path}: `profiles.{name}.tags` must not be empty."
            )
        return ",".join(tags)
    raise ProfileError(
        f"profile config {source_path}: `profiles.{name}.tags` must be a comma-separated "
        "string or a list of strings."
    )
