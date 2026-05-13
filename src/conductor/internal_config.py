"""Internal diagnostic configuration.

This module intentionally keeps internal telemetry opt-in and outside the
stable consumer contract. The config only controls richer local ledger fields;
basic delegation accounting remains on for normal Conductor operation.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

INTERNAL_TELEMETRY_ENV = "CONDUCTOR_INTERNAL_TELEMETRY"
INTERNAL_CONFIG_ENV = "CONDUCTOR_INTERNAL_CONFIG"
DEFAULT_REPO_INTERNAL_PATH = Path(".conductor") / "internal.toml"


class InternalConfigError(ValueError):
    """Raised when internal diagnostic config is malformed."""


def home_internal_config_path() -> Path:
    override = os.environ.get(INTERNAL_CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "conductor" / "internal.toml"


def repo_internal_config_path(cwd: Path | None = None) -> Path:
    root = cwd or Path.cwd()
    return root / DEFAULT_REPO_INTERNAL_PATH


def internal_telemetry_enabled(*, cwd: Path | None = None) -> bool:
    """Return whether richer internal telemetry capture is enabled.

    Precedence is env override, repo-local config, user config, default off.
    Config schema:

        [telemetry]
        capture_route_decisions = true
    """
    env = os.environ.get(INTERNAL_TELEMETRY_ENV)
    if env is not None:
        return _parse_bool(env, source=INTERNAL_TELEMETRY_ENV)

    for path in (repo_internal_config_path(cwd), home_internal_config_path()):
        if not path.exists():
            continue
        value = _load_capture_route_decisions(path)
        if value is not None:
            return value
    return False


def _load_capture_route_decisions(path: Path) -> bool | None:
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except tomllib.TOMLDecodeError as e:
        raise InternalConfigError(f"internal config {path} is not valid TOML: {e}") from e
    except OSError as e:
        raise InternalConfigError(
            f"could not read internal config {path}: {e.strerror or e}"
        ) from e

    telemetry = data.get("telemetry")
    if telemetry is None:
        return None
    if not isinstance(telemetry, dict):
        raise InternalConfigError(f"internal config {path}: `telemetry` must be a table.")

    for key in ("capture_route_decisions", "delegation_report", "enabled"):
        if key not in telemetry:
            continue
        value = telemetry[key]
        if not isinstance(value, bool):
            raise InternalConfigError(
                f"internal config {path}: `telemetry.{key}` must be true or false."
            )
        return value
    return None


def _parse_bool(value: str, *, source: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise InternalConfigError(
        f"{source} must be a boolean value: 1/0, true/false, yes/no, or on/off."
    )
