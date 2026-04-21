"""Credential resolution for Conductor providers.

Provider adapters call ``credentials.get("CLOUDFLARE_API_TOKEN")`` instead of
``os.environ.get`` directly. Resolution order:

  1. Environment variable — wins if set. Covers CI runners, ``direnv``,
     ``op run``, and any shell that exported the variable.
  2. macOS Keychain via ``security find-generic-password`` under service
     ``conductor``. Populated by ``conductor init`` when the user picks the
     Keychain storage option.
  3. (Future) 1Password CLI via ``op read``. Deferred — requires its own
     sign-in handling and item layout.

The module is macOS-specific today (Keychain via ``security``). On other
platforms the Keychain path is a no-op; only env vars work until a
platform-appropriate backend lands.

The design preserves Conductor's "no-silent-failures" principle: callers
get None when nothing is found and surface a readable error; this module
never guesses a credential.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import Optional

CONDUCTOR_KEYCHAIN_SERVICE = "conductor"


def get(key: str) -> Optional[str]:
    """Resolve a credential by name; return None if not found anywhere.

    Order: env var, then macOS Keychain (if available), then None.
    """
    if value := os.environ.get(key):
        return value
    if sys.platform == "darwin":
        return _keychain_find(key)
    return None


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
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Keychain write failed ({result.returncode}): {result.stderr.strip()}"
        )


def delete_from_keychain(key: str) -> None:
    """Remove a Conductor Keychain entry; silently OK if it doesn't exist."""
    if sys.platform != "darwin" or not shutil.which("security"):
        return
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
    )


def keychain_has(key: str) -> bool:
    """Return True if the Keychain has a Conductor entry for ``key``."""
    return _keychain_find(key) is not None


def _keychain_find(key: str) -> Optional[str]:
    if not shutil.which("security"):
        return None
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
    )
    if result.returncode != 0:
        return None
    value = result.stdout.rstrip("\n")
    return value or None
