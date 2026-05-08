"""OpenRouter model catalog — fetch, parse, and cache the live model list.

The catalog is derived state. We persist it only as a time-bounded cache so
selection can run without hard-coded model slugs and without an HTTP round-trip
on every call. `conductor models refresh` is the explicit rebuild path. The
OpenRouter auto-selector requires a live refresh before building request
restrictions; stale cache fallback is reserved for listing and compatibility
shims where a stale model is better than losing the whole command.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from conductor.providers._http_client import provider_http_client
from conductor.providers.interface import ProviderHTTPError

if TYPE_CHECKING:
    from collections.abc import Callable

OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
OPENROUTER_CATALOG_TIMEOUT_SEC = 30.0
DEFAULT_CATALOG_TTL_HOURS = 24
CONDUCTOR_CATALOG_TTL_HOURS_ENV = "CONDUCTOR_CATALOG_TTL_HOURS"
OPENROUTER_CATALOG_CACHE_PATH = (
    Path.home() / ".cache" / "conductor" / "openrouter-catalog.json"
)


@dataclass(frozen=True)
class ModelEntry:
    id: str
    name: str
    created: int
    context_length: int
    pricing_prompt: float
    pricing_completion: float
    pricing_thinking: float | None
    supports_thinking: bool
    supports_tools: bool
    supports_vision: bool

    @property
    def total_price_per_1k(self) -> float:
        return self.pricing_prompt + self.pricing_completion


@dataclass(frozen=True)
class CatalogSnapshot:
    fetched_at: int
    models: list[ModelEntry]


def cache_path() -> Path:
    return OPENROUTER_CATALOG_CACHE_PATH


def cache_ttl_hours() -> int:
    raw = os.environ.get(CONDUCTOR_CATALOG_TTL_HOURS_ENV)
    if raw:
        try:
            ttl = int(raw)
            if ttl > 0:
                return ttl
        except ValueError:
            pass
    return DEFAULT_CATALOG_TTL_HOURS


def format_timestamp(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat().replace("+00:00", "Z")


def display_cache_path(path: Path | None = None) -> str:
    target = path or cache_path()
    home = Path.home()
    try:
        return f"~/{target.relative_to(home)}"
    except ValueError:
        return str(target)


def read_cached_catalog() -> CatalogSnapshot | None:
    path = cache_path()
    try:
        raw = path.read_text()
    except FileNotFoundError:
        return None
    except OSError as e:
        raise ProviderHTTPError(
            f"failed reading OpenRouter catalog cache {path}: {e}"
        ) from e

    try:
        payload = json.loads(raw)
    except ValueError as e:
        raise ProviderHTTPError(
            f"OpenRouter catalog cache {path} was not valid JSON: {e}"
        ) from e

    try:
        fetched_at = int(payload["fetched_at"])
        models = [_model_entry_from_cache(item) for item in payload["models"]]
    except (KeyError, TypeError, ValueError) as e:
        raise ProviderHTTPError(
            f"OpenRouter catalog cache {path} had an invalid schema: {e}"
        ) from e

    return CatalogSnapshot(fetched_at=fetched_at, models=models)


def load_catalog(
    force_refresh: bool = False,
    *,
    allow_stale_on_error: bool = True,
) -> list[ModelEntry]:
    return load_catalog_snapshot(
        force_refresh=force_refresh,
        allow_stale_on_error=allow_stale_on_error,
    ).models


def load_catalog_snapshot(
    force_refresh: bool = False,
    *,
    allow_stale_on_error: bool = True,
) -> CatalogSnapshot:
    cached = _read_cached_catalog_for_load()
    if cached is not None and not force_refresh and _is_fresh(cached):
        return cached

    try:
        fresh = _fetch_catalog()
        _write_cache(fresh)
        return fresh
    except ProviderHTTPError as e:
        if cached is None or not allow_stale_on_error:
            raise
        print(
            "[conductor] OpenRouter catalog refresh failed; "
            f"using stale cache from {format_timestamp(cached.fetched_at)}: {e}",
            file=sys.stderr,
        )
        return cached


def newest_matching_model_id(
    predicate: Callable[[ModelEntry], bool],
    *,
    fallback_model: str,
    label: str,
) -> str:
    """Return the newest catalog model matching ``predicate``.

    Shim providers use this to keep stable Conductor provider IDs while letting
    OpenRouter model slugs drift. The pinned ``fallback_model`` is only used
    when the catalog cannot be loaded or no matching model exists.
    """
    try:
        models = load_catalog()
    except ProviderHTTPError as e:
        print(
            f"[conductor] {label}: OpenRouter catalog unavailable; "
            f"using pinned fallback {fallback_model}: {e}",
            file=sys.stderr,
        )
        return fallback_model
    matches = [model for model in models if predicate(model)]
    if not matches:
        print(
            f"[conductor] {label}: no matching model found in OpenRouter catalog; "
            f"using pinned fallback {fallback_model}",
            file=sys.stderr,
        )
        return fallback_model
    return sorted(matches, key=lambda model: (-model.created, model.id))[0].id


def _read_cached_catalog_for_load() -> CatalogSnapshot | None:
    try:
        return read_cached_catalog()
    except ProviderHTTPError as e:
        print(
            f"[conductor] ignoring invalid OpenRouter catalog cache: {e}",
            file=sys.stderr,
        )
        return None


def _is_fresh(snapshot: CatalogSnapshot) -> bool:
    age_sec = max(0, time.time() - snapshot.fetched_at)
    return age_sec < cache_ttl_hours() * 3600


def _fetch_catalog() -> CatalogSnapshot:
    try:
        with provider_http_client(timeout=OPENROUTER_CATALOG_TIMEOUT_SEC) as client:
            response = client.get(OPENROUTER_MODELS_URL)
    except httpx.HTTPError as e:
        raise ProviderHTTPError(
            f"network error refreshing OpenRouter catalog: {e}"
        ) from e

    if response.status_code != 200:
        raise ProviderHTTPError(
            "OpenRouter catalog refresh returned "
            f"HTTP {response.status_code}: {response.text[:500]}"
        )

    try:
        body = response.json()
    except ValueError as e:
        raise ProviderHTTPError(
            f"OpenRouter catalog response was not JSON: {e}"
        ) from e

    try:
        models = [_parse_model_entry(entry) for entry in body["data"]]
    except (KeyError, TypeError, ValueError) as e:
        raise ProviderHTTPError(
            f"OpenRouter catalog response had an unexpected schema: {e}"
        ) from e

    return CatalogSnapshot(fetched_at=int(time.time()), models=models)


def _write_cache(snapshot: CatalogSnapshot) -> None:
    path = cache_path()
    payload = {
        "fetched_at": snapshot.fetched_at,
        "models": [asdict(model) for model in snapshot.models],
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    except OSError as e:
        raise ProviderHTTPError(
            f"failed writing OpenRouter catalog cache {path}: {e}"
        ) from e


def _model_entry_from_cache(raw: dict[str, Any]) -> ModelEntry:
    return ModelEntry(
        id=str(raw["id"]),
        name=str(raw["name"]),
        created=int(raw["created"]),
        context_length=int(raw["context_length"]),
        pricing_prompt=float(raw["pricing_prompt"]),
        pricing_completion=float(raw["pricing_completion"]),
        pricing_thinking=(
            None if raw["pricing_thinking"] is None else float(raw["pricing_thinking"])
        ),
        supports_thinking=bool(raw["supports_thinking"]),
        supports_tools=bool(raw["supports_tools"]),
        supports_vision=bool(raw["supports_vision"]),
    )


def _parse_model_entry(raw: dict[str, Any]) -> ModelEntry:
    supported_parameters = _supported_parameters(raw.get("supported_parameters"))
    architecture = raw.get("architecture") or {}
    pricing = raw.get("pricing") or {}

    return ModelEntry(
        id=str(raw["id"]),
        name=str(raw.get("name") or raw["id"]),
        created=int(raw["created"]),
        context_length=int(raw["context_length"]),
        pricing_prompt=_price_per_1k(pricing["prompt"]),
        pricing_completion=_price_per_1k(pricing["completion"]),
        pricing_thinking=(
            None
            if pricing.get("reasoning") is None
            else _price_per_1k(pricing["reasoning"])
        ),
        supports_thinking=bool(
            {"reasoning", "reasoning_effort"} & supported_parameters
        ),
        supports_tools="tools" in supported_parameters,
        supports_vision=_supports_vision(architecture),
    )


def _supported_parameters(raw: Any) -> set[str]:
    if not isinstance(raw, list):
        return set()

    normalized: set[str] = set()
    for item in raw:
        if isinstance(item, str):
            normalized.add(item.strip().lower())
            continue
        if isinstance(item, dict):
            for key in ("name", "id", "parameter"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    normalized.add(value.strip().lower())
                    break
    return normalized


def _supports_vision(architecture: Any) -> bool:
    if not isinstance(architecture, dict):
        return False

    modality = architecture.get("modality")
    if isinstance(modality, str):
        left_hand = modality.split("->", 1)[0].lower()
        return "image" in left_hand
    if isinstance(modality, list):
        return any(isinstance(item, str) and "image" in item.lower() for item in modality)
    return False


def _price_per_1k(raw: Any) -> float:
    return float(raw) * 1000.0
