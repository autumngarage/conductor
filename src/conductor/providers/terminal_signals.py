"""Detect provider-terminal outage/quota signals in CLI streams.

CLI-backed providers often print the decisive upstream failure to stderr or
as a structured error event before the child process exits. Waiting for the
generic stall watchdog after seeing that signal wastes the user's fallback
budget, so adapters use this module to turn those signals into retryable
``ProviderHTTPError`` failures immediately.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

FailureCategory = Literal["rate-limit", "5xx", "network", "provider-error"]

RECENT_TEXT_LIMIT = 4_000

_STATUS_KEYS = {
    "api_error_status",
    "status",
    "status_code",
    "http_status",
    "http_status_code",
}

_RATE_LIMIT_SIGNALS = (
    "rate limit",
    "rate_limit",
    "ratelimit",
    "rate-limit",
    "too many requests",
    "quota exceeded",
    "exceeded your current quota",
    "insufficient quota",
    "usage limit",
    "daily limit",
    "limit reached",
    "hit your limit",
    "out of tokens",
    "token quota",
    "credit balance",
    "insufficient credits",
    "billing quota",
)

_NETWORK_SIGNALS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection error",
    "connect call failed",
    "could not resolve",
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "network is unreachable",
    "network is down",
    "no route to host",
    "no address associated",
    "no such host",
    "host is down",
    "getaddrinfo failed",
)

_UPSTREAM_DOWN_SIGNALS = (
    "service unavailable",
    "bad gateway",
    "gateway timeout",
    "internal server error",
    "server error",
    "overloaded",
    "temporarily unavailable",
    "upstream unavailable",
    "upstream error",
    "provider unavailable",
    "api unavailable",
    "api is down",
)

_HTTP_429_RE = re.compile(
    r"\b(?:http|status|error|api error|code)[:\s-]*429\b"
    r"|\b429\b.*\b(rate|limit|quota)\b"
    r"|\b(rate|limit|quota)\b.*\b429\b"
)
_HTTP_5XX_RE = re.compile(
    r"\b(?:http|status|error|api error|code)[:\s-]*5\d\d\b"
)


@dataclass(frozen=True)
class ProviderFailureSignal:
    category: FailureCategory
    detail: str
    source: str
    status_code: int | None = None

    def error_message(self, provider: str) -> str:
        label = {
            "rate-limit": "rate limit",
            "5xx": "upstream unavailable",
            "network": "network failure",
            "provider-error": "provider error",
        }[self.category]
        status = f" HTTP {self.status_code}" if self.status_code is not None else ""
        return f"{provider} reported {label}{status} on {self.source}: {self.detail}"


def append_recent_text(current: str, chunk: str) -> str:
    """Append ``chunk`` and keep only the recent tail needed for detection."""
    combined = current + chunk
    if len(combined) <= RECENT_TEXT_LIMIT:
        return combined
    return combined[-RECENT_TEXT_LIMIT:]


def detect_retriable_provider_failure(
    text: str,
    *,
    source: str,
    structured_only: bool = False,
) -> ProviderFailureSignal | None:
    """Return a retryable terminal-failure signal found in provider output.

    ``structured_only`` is for stdout streams that carry normal model content.
    In that mode, arbitrary prose is ignored unless it is a structured error
    payload such as Codex NDJSON ``{"type": "error", "status": 429, ...}``.
    """
    raw = text.strip()
    if not raw:
        return None

    payload = _loads_json_object(raw)
    if structured_only and (
        payload is None or not _is_structured_failure_payload(payload)
    ):
        return None

    status_codes = _status_codes(payload) if payload is not None else []
    searchable = _searchable_text(raw, payload)
    lowered = searchable.lower()

    if (
        429 in status_codes
        or _HTTP_429_RE.search(lowered)
        or any(sig in lowered for sig in _RATE_LIMIT_SIGNALS)
    ):
        return ProviderFailureSignal(
            category="rate-limit",
            status_code=429 if 429 in status_codes else _first_status(status_codes),
            source=source,
            detail=_compact_detail(raw),
        )

    upstream_status = next((code for code in status_codes if 500 <= code <= 599), None)
    if (
        upstream_status is not None
        or _HTTP_5XX_RE.search(lowered)
        or any(sig in lowered for sig in _UPSTREAM_DOWN_SIGNALS)
    ):
        return ProviderFailureSignal(
            category="5xx",
            status_code=upstream_status,
            source=source,
            detail=_compact_detail(raw),
        )

    if any(sig in lowered for sig in _NETWORK_SIGNALS):
        return ProviderFailureSignal(
            category="network",
            status_code=_first_status(status_codes),
            source=source,
            detail=_compact_detail(raw),
        )

    if structured_only and _is_structured_failure_payload(payload):
        return ProviderFailureSignal(
            category="provider-error",
            status_code=_first_status(status_codes),
            source=source,
            detail=_compact_detail(raw),
        )

    return None


def _loads_json_object(raw: str) -> Any | None:
    candidates = [raw]
    if "{" in raw and "}" in raw:
        candidates.append(raw[raw.find("{") : raw.rfind("}") + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


def _is_structured_failure_payload(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if any(code >= 400 for code in _status_codes(payload)):
        return True
    marker = " ".join(
        str(payload.get(key) or "")
        for key in ("type", "kind", "event", "level", "status")
    ).lower()
    if any(sig in marker for sig in ("error", "failed", "failure", "exception")):
        return True
    error_value = payload.get("error")
    return error_value not in (None, "", False)


def _status_codes(value: Any) -> list[int]:
    codes: list[int] = []
    if isinstance(value, dict):
        for key, item in value.items():
            if key in _STATUS_KEYS:
                parsed = _parse_status_code(item)
                if parsed is not None:
                    codes.append(parsed)
            codes.extend(_status_codes(item))
    elif isinstance(value, list):
        for item in value:
            codes.extend(_status_codes(item))
    return codes


def _parse_status_code(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _first_status(codes: list[int]) -> int | None:
    return codes[0] if codes else None


def _searchable_text(raw: str, payload: Any | None) -> str:
    if payload is None:
        return raw
    scalar_values = _iter_scalar_strings(payload)
    if not scalar_values:
        return raw
    return raw + "\n" + "\n".join(scalar_values)


def _iter_scalar_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, int):
        return [str(value)]
    if isinstance(value, dict):
        found: list[str] = []
        for item in value.values():
            found.extend(_iter_scalar_strings(item))
        return found
    if isinstance(value, list):
        found = []
        for item in value:
            found.extend(_iter_scalar_strings(item))
        return found
    return []


def _compact_detail(raw: str) -> str:
    one_line = " ".join(raw.split())
    if len(one_line) <= 500:
        return one_line
    return one_line[:497] + "..."
