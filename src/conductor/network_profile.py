"""Cheap network RTT profiling for CLI dispatch timeouts.

The profile is derived state. It is cached briefly so a shell loop does not
probe on every invocation, and corrupt cache content is deleted and rebuilt.
"""

from __future__ import annotations

import json
import statistics
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx

from conductor.offline_mode import _cache_dir

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

NETWORK_PROFILE_CACHE_NAME = "network_profile"
NETWORK_PROFILE_TTL_SEC = 600
NETWORK_PROFILE_MAX_WALL_SEC = 1.0
NETWORK_PROFILE_SAMPLES = 3
NETWORK_PROFILE_FALLBACK_TARGET = "https://1.1.1.1"
NETWORK_PROFILE_SCALING_TABLE: tuple[tuple[float, int], ...] = (
    (100.0, 1),
    (250.0, 2),
    (float("inf"), 3),
)


@dataclass(frozen=True)
class NetworkProfile:
    rtt_ms: float | None
    target: str
    timestamp: float


def apply_scaling(timeout: float | None, profile: NetworkProfile) -> float | None:
    """Scale a timeout-like value by the RTT tier.

    ``None`` stays ``None`` so callers can preserve explicit "disabled"
    semantics.
    """
    if timeout is None:
        return None
    return timeout * scaling_multiplier(profile)


def scaling_multiplier(profile: NetworkProfile) -> int:
    if profile.rtt_ms is None:
        return 1
    for upper_bound_ms, multiplier in NETWORK_PROFILE_SCALING_TABLE:
        if profile.rtt_ms < upper_bound_ms:
            return multiplier
    return 1


def get_network_profile(
    target: str | None,
    *,
    now: float | None = None,
    warn: Callable[[str], None] | None = None,
) -> NetworkProfile:
    """Return a cached or freshly probed RTT profile for ``target``.

    Probe failures are surfaced through ``warn`` and return an unscaled profile
    so dispatch can proceed with the normal defaults.
    """
    current_time = time.time() if now is None else now
    normalized_target = _normalize_target(target)
    cached = _read_cache(current_time, normalized_target, warn=warn)
    if cached is not None:
        return cached

    try:
        profile = _probe_profile(normalized_target, current_time)
    except Exception as e:
        _warn(warn, f"[conductor] network: probe failed for {normalized_target}: {e}")
        return NetworkProfile(
            rtt_ms=None,
            target=normalized_target,
            timestamp=current_time,
        )

    _write_cache(profile, warn=warn)
    return profile


def _normalize_target(target: str | None) -> str:
    if target is None or not target.strip():
        return NETWORK_PROFILE_FALLBACK_TARGET
    return target.strip().rstrip("/")


def _cache_path() -> Path:
    return _cache_dir() / NETWORK_PROFILE_CACHE_NAME


def _read_cache(
    now: float,
    target: str,
    *,
    warn: Callable[[str], None] | None,
) -> NetworkProfile | None:
    path = _cache_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        _warn(warn, f"[conductor] network: could not read cache {path}: {e}")
        return None

    try:
        data = json.loads(raw)
        profile = NetworkProfile(
            rtt_ms=float(data["rtt_ms"]),
            target=str(data["target"]),
            timestamp=float(data["timestamp"]),
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as e:
        _delete_bad_cache(path, warn=warn, reason=e)
        return None

    if profile.target != target:
        return None
    if now - profile.timestamp > NETWORK_PROFILE_TTL_SEC:
        return None
    if profile.rtt_ms is None or profile.rtt_ms < 0:
        _delete_bad_cache(path, warn=warn, reason=ValueError("invalid rtt_ms"))
        return None
    return profile


def _write_cache(profile: NetworkProfile, *, warn: Callable[[str], None] | None) -> None:
    if profile.rtt_ms is None:
        return
    path = _cache_path()
    payload = {
        "rtt_ms": profile.rtt_ms,
        "target": profile.target,
        "timestamp": profile.timestamp,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    except OSError as e:
        _warn(warn, f"[conductor] network: could not write cache {path}: {e}")


def _delete_bad_cache(
    path: Path,
    *,
    warn: Callable[[str], None] | None,
    reason: Exception,
) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError as e:
        _warn(
            warn,
            f"[conductor] network: corrupt cache {path} could not be deleted: {e}",
        )
        return
    _warn(warn, f"[conductor] network: deleted corrupt cache {path}: {reason}")


def _probe_profile(target: str, timestamp: float) -> NetworkProfile:
    deadline = time.monotonic() + NETWORK_PROFILE_MAX_WALL_SEC
    primary_rtts = _sample_rtts(target, deadline)
    if primary_rtts:
        return NetworkProfile(
            rtt_ms=round(statistics.median(primary_rtts), 3),
            target=target,
            timestamp=timestamp,
        )

    fallback_rtts = _sample_rtts(NETWORK_PROFILE_FALLBACK_TARGET, deadline)
    if fallback_rtts:
        return NetworkProfile(
            rtt_ms=round(statistics.median(fallback_rtts), 3),
            target=NETWORK_PROFILE_FALLBACK_TARGET,
            timestamp=timestamp,
        )

    raise RuntimeError(
        f"no successful RTT samples for {target} or {NETWORK_PROFILE_FALLBACK_TARGET}"
    )


def _sample_rtts(target: str, deadline: float) -> list[float]:
    samples: list[float] = []
    with httpx.Client(follow_redirects=True) as client:
        for _ in range(NETWORK_PROFILE_SAMPLES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            timeout = min(remaining, NETWORK_PROFILE_MAX_WALL_SEC)
            started = time.perf_counter()
            try:
                client.head(target, timeout=timeout)
            except httpx.HTTPError:
                continue
            samples.append((time.perf_counter() - started) * 1000)
    return samples


def _warn(warn: Callable[[str], None] | None, message: str) -> None:
    if warn is not None:
        warn(message)
