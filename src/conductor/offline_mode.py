"""Offline-mode session flag — sticky 'I'm on a plane' bit with a TTL.

Motivation: the auto-router picks remote providers first (kimi, claude,
codex, gemini). On a network-less laptop every `conductor call --auto`
hits the same connection failure, falls back to ollama after the user
confirms, and then forgets the choice. Five invocations in a row means
five prompts. This module persists "user chose local fallback" across
process boundaries so the prompt fires once per offline session.

Storage is a single file — unix timestamp as ASCII text — under the
platform cache dir. The directory is created lazily; if it can't be
written (read-only FS, no $HOME, etc.) every function becomes a no-op
and the caller falls back to in-process behavior. No exceptions leak
from this module by design: a broken cache must never break a call.

TTL is deliberately short (10 min default). On a real provider outage
we don't want to silently mask the problem for hours — the flag expires,
the next call tries the primary, and if it succeeds the flag stays
lapsed. If it fails the user is re-prompted.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

# 10 minutes — long enough for a plane-work session, short enough that a
# real outage doesn't silently divert traffic to local for an entire day.
DEFAULT_OFFLINE_TTL_SEC = 600
CONDUCTOR_OFFLINE_TTL_ENV = "CONDUCTOR_OFFLINE_TTL_SEC"


def _resolve_ttl_sec() -> int:
    """Read the TTL override from env, else the default."""
    raw = os.environ.get(CONDUCTOR_OFFLINE_TTL_ENV)
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return DEFAULT_OFFLINE_TTL_SEC


def _cache_dir() -> Path:
    """Return the conductor cache directory.

    Honors $XDG_CACHE_HOME per the XDG Base Directory spec; falls back to
    ~/.cache on Linux/macOS (and the user's profile on Windows, where
    XDG_CACHE_HOME is not standard but is respected if set).
    """
    xdg = os.environ.get("XDG_CACHE_HOME")
    root = Path(xdg) if xdg else Path.home() / ".cache"
    return root / "conductor"


def _flag_path() -> Path:
    return _cache_dir() / "offline_until"


def expiry_timestamp() -> float | None:
    """Return the flag's expiry as a unix timestamp, or None if unset/invalid.

    Callers that just want a boolean should use ``is_active`` instead;
    this is here so UX layers can render "expires in 3m" strings.
    """
    path = _flag_path()
    try:
        raw = path.read_text().strip()
    except (FileNotFoundError, OSError):
        return None
    try:
        return float(raw)
    except ValueError:
        # Corrupt file — treat as absent. We don't rewrite it here because
        # a stale bad file is harmless (is_active returns False) and the
        # next set() will overwrite it atomically.
        return None


def is_active() -> bool:
    """True if the offline-mode flag is set and has not expired."""
    expiry = expiry_timestamp()
    if expiry is None:
        return False
    if time.time() >= expiry:
        # Expired; clear lazily so callers don't see a stale flag if they
        # re-check later in the same process.
        clear()
        return False
    return True


def seconds_remaining() -> int:
    """Seconds until the flag expires, or 0 if inactive."""
    expiry = expiry_timestamp()
    if expiry is None:
        return 0
    remaining = int(expiry - time.time())
    return max(0, remaining)


def set_active(ttl_sec: int | None = None) -> bool:
    """Set the offline-mode flag; return True on success, False on no-op.

    Returns False (rather than raising) when the cache dir isn't writable
    — callers treat that as "the flag is in-process only for this run,"
    which is the correct degraded behavior. Every path that writes the
    file is wrapped so a permissions error, disk-full condition, or a
    $HOME pointing somewhere read-only never surfaces as a traceback.
    """
    ttl = ttl_sec if ttl_sec is not None else _resolve_ttl_sec()
    expiry = time.time() + ttl
    path = _flag_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{expiry:.0f}\n")
        return True
    except OSError:
        return False


def clear() -> None:
    """Remove the flag. Silent no-op if it doesn't exist or can't be removed."""
    path = _flag_path()
    try:
        path.unlink()
    except (FileNotFoundError, OSError):
        return
