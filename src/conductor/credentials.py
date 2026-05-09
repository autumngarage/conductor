"""Credential resolution for Conductor providers.

Provider adapters call ``credentials.get("OPENROUTER_API_KEY")`` instead of
``os.environ.get`` directly. Resolution order:

  1. Environment variable — wins if set. Covers CI runners, ``direnv``,
     ``op run``, and any shell that exported the variable.
  2. ``key_command`` indirection from ``~/.config/conductor/credentials.toml``.
     The configured shell command is executed; its stdout is the credential.
     This is the secret-manager path: ``op read``, ``doppler secrets get``,
     ``vault kv get``, ``bw get``, or any user-supplied script. The secret
     is fetched just-in-time and never persists to local disk.
  3. macOS Keychain via ``security find-generic-password`` under service
     ``conductor``. Populated by ``conductor init`` when the user picks the
     Keychain storage option.

A configured ``key_command`` that fails (non-zero exit, missing CLI) does
NOT silently fall through to keychain. The failure is logged to stderr and
the resolver returns None, so the operator notices that their secret-manager
configuration is broken instead of unknowingly using a stale local cache.

The Keychain path is macOS-specific. On other platforms the Keychain check
is a no-op; env vars and ``key_command`` work everywhere.

The design preserves Conductor's "no-silent-failures" principle: callers
get None when nothing is found and surface a readable error; this module
never guesses a credential.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Literal

CONDUCTOR_KEYCHAIN_SERVICE = "conductor"
CONDUCTOR_SECRET_SERVICE = "conductor"
CREDENTIALS_FILE_ENV = "CONDUCTOR_CREDENTIALS_FILE"
DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "conductor" / "credentials.toml"
KEY_COMMAND_TIMEOUT_SEC = 30.0
CREDENTIAL_HELPER_TIMEOUT_SEC = 15.0

CredentialSource = Literal["env", "key_command", "keychain"]

# Per-process result cache for key_command resolutions. Avoids re-prompting
# Touch ID on every adapter call within a single CLI invocation. Cleared
# between processes (which is the point — secret-manager indirection
# explicitly does not persist to disk across sessions).
#
# Keyed by (env_var, command) so changing the command for the same var
# is a cache miss by construction. This matters during `conductor init`:
# an earlier provider.configured() call may have populated the cache
# with the OLD command's resolved value; the wizard's test-resolve of a
# new candidate command must NOT reuse that stale entry.
_KEY_COMMAND_CACHE: dict[tuple[str, str], str] = {}


def credentials_file_path() -> Path:
    """Resolve the credentials TOML path, honoring env override."""
    override = os.environ.get(CREDENTIALS_FILE_ENV)
    if override:
        return Path(override).expanduser()
    return DEFAULT_CREDENTIALS_PATH


def get(key: str) -> str | None:
    """Resolve a credential by name; return None if not found anywhere.

    Order: env var → ``key_command`` → macOS Keychain → None.
    """
    value, _ = resolve_with_source(key)
    return value


def resolve_with_source(key: str) -> tuple[str | None, CredentialSource | None]:
    """Resolve a credential and report which source supplied it.

    Used by ``conductor doctor`` to show provenance per credential. Same
    resolution order as ``get()``.
    """
    if value := os.environ.get(key):
        return value, "env"

    commands = load_key_commands()
    if key in commands:
        # An explicitly-configured key_command failure is a hard stop for
        # this source — do NOT fall through to keychain. The operator
        # needs to know their secret-manager wiring is broken; a silent
        # fall-back to a possibly-stale keychain value is exactly the
        # kind of bug the no-silent-failures principle is here to prevent.
        value = _run_key_command(key, commands[key])
        if value is not None:
            return value, "key_command"
        return None, None

    if sys.platform == "darwin":
        kc = _keychain_find(key)
        if kc is not None:
            return kc, "keychain"
    return None, None


# --------------------------------------------------------------------------- #
# key_command — credentials.toml load / save / resolve.
# --------------------------------------------------------------------------- #


def load_key_commands() -> dict[str, str]:
    """Read ``[key_commands]`` from the credentials TOML.

    Returns an empty dict when the file is missing or has no
    ``[key_commands]`` section. Invalid TOML is logged and treated as
    empty rather than raising — a corrupt credentials file shouldn't
    brick every adapter call.
    """
    path = credentials_file_path()
    if not path.exists():
        return {}
    try:
        with path.open("rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(
            f"conductor: warning: credentials file {path} is not valid TOML: {e}. "
            "Falling back to env / keychain only.",
            file=sys.stderr,
        )
        return {}
    raw = data.get("key_commands") or {}
    if not isinstance(raw, dict):
        print(
            f"conductor: warning: credentials file {path}: `key_commands` must be a "
            "TOML table mapping env-var name → shell command. Ignoring.",
            file=sys.stderr,
        )
        return {}
    out: dict[str, str] = {}
    for k, v in raw.items():
        if isinstance(k, str) and isinstance(v, str) and v.strip():
            out[k] = v
    return out


def save_key_command(key: str, command: str) -> Path:
    """Write a single key_command entry, creating / merging the file.

    Convenience wrapper for the single-credential case. Most callers
    should use ``set_key_commands`` instead — when multiple credentials
    need to be written together, looping ``save_key_command`` produces
    intermediate file states that can't be cleanly rolled back if a later
    write fails.
    """
    return set_key_commands({key: command})


def set_key_commands(updates: dict[str, str]) -> Path:
    """Merge ``updates`` into the credentials file in one atomic write.

    Either every entry lands or none does — the temp+rename is one
    syscall, so the file's prior contents survive a mid-write crash.
    Existing entries not in ``updates`` are preserved. Sets file mode
    to 0600 so other users on the host can't read the (still
    secret-bearing) command lines.
    """
    if not updates:
        raise ValueError("updates must be non-empty")
    for k, v in updates.items():
        if not k or not isinstance(k, str):
            raise ValueError(f"key must be a non-empty string (got {k!r})")
        if not isinstance(v, str) or not v.strip():
            raise ValueError(f"command for {k!r} must be a non-empty string")
    path = credentials_file_path()
    merged = {**load_key_commands(), **updates}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write("# Conductor credentials — managed by `conductor init`.\n")
        f.write(
            "# Each entry maps an env-var name to a shell command whose\n"
            "# stdout is the credential. Secrets are NOT stored here; the\n"
            "# command is executed just-in-time per call.\n\n"
        )
        f.write("[key_commands]\n")
        for k, v in sorted(merged.items()):
            # TOML basic-string escape: backslashes and double-quotes only.
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            f.write(f'{k} = "{escaped}"\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    # Drop cache entries for updated keys regardless of which command
    # produced them — once the file changes, any cached value tied to
    # a now-replaced command is stale.
    for key in updates:
        for cache_key in [ck for ck in _KEY_COMMAND_CACHE if ck[0] == key]:
            _KEY_COMMAND_CACHE.pop(cache_key, None)
    return path


def delete_key_command(key: str) -> bool:
    """Remove a key_command entry. Returns True if a row was removed."""
    path = credentials_file_path()
    if not path.exists():
        return False
    existing = load_key_commands()
    if key not in existing:
        return False
    existing.pop(key)
    cache_keys = [ck for ck in _KEY_COMMAND_CACHE if ck[0] == key]
    if not existing:
        path.unlink()
        for ck in cache_keys:
            _KEY_COMMAND_CACHE.pop(ck, None)
        return True
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write("# Conductor credentials — managed by `conductor init`.\n")
        f.write("[key_commands]\n")
        for k, v in sorted(existing.items()):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            f.write(f'{k} = "{escaped}"\n')
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    for ck in cache_keys:
        _KEY_COMMAND_CACHE.pop(ck, None)
    return True


def clear_key_command_cache() -> None:
    """Drop the per-process resolution cache. Test-only helper."""
    _KEY_COMMAND_CACHE.clear()


def run_key_command(key: str, command: str) -> str | None:
    """Public alias for the key_command runner.

    Used by ``conductor init`` to test-resolve a candidate command before
    persisting it — bypassing the env/keychain layers so a stray env var
    can't mask a broken ``op read`` and let the wizard report success on
    a credential it never actually fetched. Populates the per-process
    cache on success so an immediately-following smoke test reuses the
    same value (one prompt, not two).
    """
    return _run_key_command(key, command)


def libsecret_available() -> bool:
    """Return True when the Linux secret-tool CLI is available."""
    return sys.platform.startswith("linux") and shutil.which("secret-tool") is not None


def libsecret_lookup_command(key: str) -> str:
    """Return the key_command string that reads ``key`` from libsecret."""
    return (
        f"secret-tool lookup service {shlex.quote(CONDUCTOR_SECRET_SERVICE)} "
        f"account {shlex.quote(key)}"
    )


def _run_key_command(key: str, command: str) -> str | None:
    """Execute a key_command and return stdout (or None on failure).

    Parses the command via ``shlex.split`` and runs with ``shell=False`` —
    no shell interpolation, no injection surface even if the credentials
    file is somehow corrupted. Failures print to stderr (never silent).
    """
    cache_key = (key, command)
    if cache_key in _KEY_COMMAND_CACHE:
        return _KEY_COMMAND_CACHE[cache_key]
    try:
        argv = shlex.split(command)
    except ValueError as e:
        print(
            f"conductor: key_command for {key} is not parseable: {e}",
            file=sys.stderr,
        )
        return None
    if not argv:
        return None
    if not shutil.which(argv[0]):
        print(
            f"conductor: key_command for {key}: `{argv[0]}` not found on PATH. "
            f"Install it or fix the credentials file at {credentials_file_path()}.",
            file=sys.stderr,
        )
        return None
    try:
        result = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=KEY_COMMAND_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(
            f"conductor: key_command for {key} timed out after "
            f"{KEY_COMMAND_TIMEOUT_SEC:.0f}s.",
            file=sys.stderr,
        )
        return None
    except OSError as e:
        print(f"conductor: key_command for {key} failed to launch: {e}", file=sys.stderr)
        return None
    if result.returncode != 0:
        print(
            f"conductor: key_command for {key} exited {result.returncode}: "
            f"{result.stderr.strip()[:500]}",
            file=sys.stderr,
        )
        return None
    value = result.stdout.rstrip("\n")
    if not value:
        print(
            f"conductor: key_command for {key} produced empty output.",
            file=sys.stderr,
        )
        return None
    _KEY_COMMAND_CACHE[cache_key] = value
    return value


# --------------------------------------------------------------------------- #
# Keychain — macOS Keychain wrappers.
# --------------------------------------------------------------------------- #


def set_in_keychain(key: str, value: str) -> None:
    """Store a credential in the macOS Keychain under service ``conductor``.

    Uses ``security add-generic-password -U`` to update-if-exists. Raises
    RuntimeError if ``security`` is unavailable or the call fails so the
    caller can surface the error explicitly.
    """
    if sys.platform != "darwin":
        raise RuntimeError(
            "Keychain storage is macOS-only. On this platform, "
            "export the variable in your shell rc or a direnv .envrc."
        )
    if not shutil.which("security"):
        raise RuntimeError(
            "`security` command not found (expected at /usr/bin/security on macOS)"
        )
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-s",
                CONDUCTOR_KEYCHAIN_SERVICE,
                "-a",
                key,
                "-w",
                value,
                "-U",
            ],
            capture_output=True,
            text=True,
            timeout=CREDENTIAL_HELPER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"Keychain write timed out after {CREDENTIAL_HELPER_TIMEOUT_SEC:.0f}s"
        ) from e
    except OSError as e:
        raise RuntimeError(f"Keychain write failed to launch: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(
            f"Keychain write failed ({result.returncode}): {result.stderr.strip()}"
        )


def probe_keychain_read(key: str) -> bool:
    """Probe-read a Keychain item so macOS can attach the access decision."""
    if sys.platform != "darwin":
        return False
    return _keychain_find(key) is not None


def set_in_libsecret(key: str, value: str) -> None:
    """Store a credential in libsecret via ``secret-tool store``."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("libsecret storage is only available on Linux.")
    if not shutil.which("secret-tool"):
        raise RuntimeError(
            "`secret-tool` not found. Install libsecret-tools or use env / 1Password."
        )
    try:
        result = subprocess.run(
            [
                "secret-tool",
                "store",
                "--label",
                f"Conductor {key}",
                "service",
                CONDUCTOR_SECRET_SERVICE,
                "account",
                key,
            ],
            input=value,
            capture_output=True,
            text=True,
            timeout=CREDENTIAL_HELPER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"libsecret write timed out after {CREDENTIAL_HELPER_TIMEOUT_SEC:.0f}s"
        ) from e
    except OSError as e:
        raise RuntimeError(f"libsecret write failed to launch: {e}") from e
    if result.returncode != 0:
        raise RuntimeError(
            f"libsecret write failed ({result.returncode}): {result.stderr.strip()}"
        )


def delete_from_libsecret(key: str) -> None:
    """Remove a Conductor libsecret entry; silently OK if it doesn't exist."""
    if not libsecret_available():
        return
    try:
        subprocess.run(
            [
                "secret-tool",
                "clear",
                "service",
                CONDUCTOR_SECRET_SERVICE,
                "account",
                key,
            ],
            capture_output=True,
            text=True,
            timeout=CREDENTIAL_HELPER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(
            f"conductor: warning: libsecret delete for {key} timed out after "
            f"{CREDENTIAL_HELPER_TIMEOUT_SEC:.0f}s.",
            file=sys.stderr,
        )
    except OSError as e:
        print(
            f"conductor: warning: libsecret delete for {key} failed to launch: {e}",
            file=sys.stderr,
        )


def delete_from_keychain(key: str) -> None:
    """Remove a Conductor Keychain entry; silently OK if it doesn't exist."""
    if sys.platform != "darwin" or not shutil.which("security"):
        return
    try:
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-s",
                CONDUCTOR_KEYCHAIN_SERVICE,
                "-a",
                key,
            ],
            capture_output=True,
            timeout=CREDENTIAL_HELPER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(
            f"conductor: warning: Keychain delete for {key} timed out after "
            f"{CREDENTIAL_HELPER_TIMEOUT_SEC:.0f}s.",
            file=sys.stderr,
        )
    except OSError as e:
        print(
            f"conductor: warning: Keychain delete for {key} failed to launch: {e}",
            file=sys.stderr,
        )


def keychain_has(key: str) -> bool:
    """Return True if the Keychain has a Conductor entry for ``key``."""
    return _keychain_find(key) is not None


def _keychain_find(key: str) -> str | None:
    if not shutil.which("security"):
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                CONDUCTOR_KEYCHAIN_SERVICE,
                "-a",
                key,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=CREDENTIAL_HELPER_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        print(
            f"conductor: warning: Keychain lookup for {key} timed out after "
            f"{CREDENTIAL_HELPER_TIMEOUT_SEC:.0f}s.",
            file=sys.stderr,
        )
        return None
    except OSError as e:
        print(
            f"conductor: warning: Keychain lookup for {key} failed to launch: {e}",
            file=sys.stderr,
        )
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.rstrip("\n")
    return value or None
