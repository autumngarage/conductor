"""User-local custom providers — TOML persistence.

Custom providers (``conductor providers add --shell '<cmd>'``) live in a
user-local TOML file, defaulting to ``~/.config/conductor/providers.toml``
on macOS/Linux. The path is overridable via ``CONDUCTOR_PROVIDERS_FILE``
for testing and for users with non-default XDG layouts.

File schema (one entry per ``[[providers]]`` table):

    [[providers]]
    name = "my-local"
    shell = "lm-studio-cli"
    accepts = "stdin"        # or "argv"
    tags = ["code-review", "offline"]
    tier = "local"            # frontier|strong|standard|local
    cost_per_1k_in = 0.0
    cost_per_1k_out = 0.0
    typical_p50_ms = 3000

The file is managed via the ``conductor providers`` command group. Hand
editing is supported — the format is stable — but the CLI's add/remove
subcommands are the expected path.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path

from conductor.providers.interface import QUALITY_TIERS
from conductor.providers.shell import ShellProviderSpec

CONFIG_ENV = "CONDUCTOR_PROVIDERS_FILE"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "conductor" / "providers.toml"


class CustomProviderError(ValueError):
    """Raised when a custom-provider spec is malformed."""


def providers_file_path() -> Path:
    """Resolve the custom-providers TOML path, honoring env override."""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_CONFIG_PATH


def load_specs() -> list[ShellProviderSpec]:
    """Read the user-local custom-providers file.

    Returns an empty list when the file doesn't exist — a fresh install
    just has no custom providers, not a misconfiguration. Invalid TOML
    raises CustomProviderError with a pointer at the file.
    """
    path = providers_file_path()
    if not path.exists():
        return []
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise CustomProviderError(
            f"custom providers file {path} is not valid TOML: {e}. "
            "Fix by hand or remove the file to reset."
        ) from e

    entries = data.get("providers") or []
    if not isinstance(entries, list):
        raise CustomProviderError(
            f"custom providers file {path}: `providers` must be a TOML array-of-tables, "
            "got a single table or scalar."
        )

    specs: list[ShellProviderSpec] = []
    seen: set[str] = set()
    for raw in entries:
        spec = _spec_from_dict(raw, source_path=path)
        if spec.name in seen:
            raise CustomProviderError(
                f"custom providers file {path}: duplicate provider name `{spec.name}`."
            )
        seen.add(spec.name)
        specs.append(spec)
    return specs


def save_specs(specs: list[ShellProviderSpec]) -> Path:
    """Persist the given specs, replacing file contents entirely.

    Creates parent directory if missing. Atomic write via temp+rename so
    a crash mid-save can't corrupt the file.
    """
    path = providers_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write("# Conductor custom providers — managed by `conductor providers`.\n")
        f.write("# See `conductor providers add --help` for the add/remove surface.\n")
        f.write("# Hand editing works; the schema is stable.\n\n")
        for i, spec in enumerate(specs):
            if i > 0:
                f.write("\n")
            _write_spec(f, spec)
    tmp.replace(path)
    return path


def add_spec(new: ShellProviderSpec) -> Path:
    """Append a new spec, erroring on name collision."""
    specs = load_specs()
    for existing in specs:
        if existing.name == new.name:
            raise CustomProviderError(
                f"custom provider `{new.name}` already exists. "
                f"Remove it first with `conductor providers remove {new.name}` "
                "or edit the providers file directly."
            )
    specs.append(new)
    return save_specs(specs)


def remove_spec(name: str) -> tuple[Path, bool]:
    """Remove a spec by name. Returns (path, was_removed)."""
    specs = load_specs()
    keep = [s for s in specs if s.name != name]
    if len(keep) == len(specs):
        return providers_file_path(), False
    path = save_specs(keep)
    return path, True


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #


def _spec_from_dict(raw: dict, *, source_path: Path) -> ShellProviderSpec:
    if not isinstance(raw, dict):
        raise CustomProviderError(
            f"custom providers file {source_path}: each entry must be a TOML table, "
            f"got {type(raw).__name__}."
        )
    required = ("name", "shell")
    for field in required:
        if field not in raw:
            raise CustomProviderError(
                f"custom providers file {source_path}: entry missing required field "
                f"`{field}`. Got keys: {sorted(raw.keys())}."
            )
    name = raw["name"]
    if not isinstance(name, str) or not name.strip():
        raise CustomProviderError(
            f"custom providers file {source_path}: `name` must be a non-empty string."
        )
    if name in _BUILTIN_NAMES:
        raise CustomProviderError(
            f"custom providers file {source_path}: `{name}` is a built-in provider "
            "identifier. Pick a different name for your custom provider."
        )
    shell = raw["shell"]
    if not isinstance(shell, str) or not shell.strip():
        raise CustomProviderError(
            f"custom providers file {source_path}: `shell` must be a non-empty string."
        )
    accepts = raw.get("accepts", "stdin")
    if accepts not in ("stdin", "argv"):
        raise CustomProviderError(
            f"custom providers file {source_path}: `accepts` for `{name}` must be "
            f"'stdin' or 'argv', got {accepts!r}."
        )
    tier = raw.get("tier", "local")
    if tier not in QUALITY_TIERS:
        raise CustomProviderError(
            f"custom providers file {source_path}: `tier` for `{name}` must be one of "
            f"{list(QUALITY_TIERS)}, got {tier!r}."
        )
    tags_raw = raw.get("tags", [])
    if not isinstance(tags_raw, list) or not all(isinstance(t, str) for t in tags_raw):
        raise CustomProviderError(
            f"custom providers file {source_path}: `tags` for `{name}` must be a "
            "list of strings."
        )

    return ShellProviderSpec(
        name=name,
        shell=shell.strip(),
        accepts=accepts,  # type: ignore[arg-type]  (Literal check above)
        tags=tuple(tags_raw),
        quality_tier=tier,
        cost_per_1k_in=float(raw.get("cost_per_1k_in", 0.0)),
        cost_per_1k_out=float(raw.get("cost_per_1k_out", 0.0)),
        typical_p50_ms=int(raw.get("typical_p50_ms", 3000)),
    )


def _write_spec(f, spec: ShellProviderSpec) -> None:
    f.write("[[providers]]\n")
    f.write(f'name = "{_escape(spec.name)}"\n')
    f.write(f'shell = "{_escape(spec.shell)}"\n')
    f.write(f'accepts = "{spec.accepts}"\n')
    tags_lit = ", ".join(f'"{_escape(t)}"' for t in spec.tags)
    f.write(f"tags = [{tags_lit}]\n")
    f.write(f'tier = "{spec.quality_tier}"\n')
    if spec.cost_per_1k_in or spec.cost_per_1k_out:
        f.write(f"cost_per_1k_in = {spec.cost_per_1k_in}\n")
        f.write(f"cost_per_1k_out = {spec.cost_per_1k_out}\n")
    if spec.typical_p50_ms != 3000:
        f.write(f"typical_p50_ms = {spec.typical_p50_ms}\n")


def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


# Built-in names that custom entries MUST NOT shadow. Kept here (and not
# derived from providers/__init__._REGISTRY) to avoid a circular import —
# the values change only when a new first-party provider ships.
_BUILTIN_NAMES = frozenset({"kimi", "claude", "codex", "gemini", "ollama"})
