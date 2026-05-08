"""Shared HTTP client helpers for provider adapters."""

from __future__ import annotations

import logging
import ssl

import httpx

from conductor.providers.interface import ProviderHTTPError

_LOG = logging.getLogger(__name__)


def provider_http_client(*, timeout: float | int | httpx.Timeout | None) -> httpx.Client:
    """Create an httpx client, recovering from stale CA bundle paths.

    httpx normally loads certifi's CA bundle while constructing the client. In
    packaged installs that bundle path can occasionally point at a file that no
    longer exists after an upgrade. That raises ``FileNotFoundError`` before a
    request exists, so provider-level ``httpx.HTTPError`` handling never runs.
    Retrying with an explicit OS-default SSL context preserves verification
    while avoiding the stale certifi path.
    """
    try:
        return httpx.Client(timeout=timeout)
    except FileNotFoundError as e:
        verify_context = _fallback_verify_context(e)
        try:
            return httpx.Client(timeout=timeout, verify=verify_context)
        except FileNotFoundError as fallback_error:
            raise ProviderHTTPError(_missing_ca_message(fallback_error)) from fallback_error


def _fallback_verify_context(error: FileNotFoundError) -> ssl.SSLContext:
    try:
        context = ssl.create_default_context(cafile=None, capath=None, cadata=None)
    except OSError as fallback_error:
        raise ProviderHTTPError(_missing_ca_message(error)) from fallback_error

    filename = getattr(error, "filename", None)
    location = f" at {filename}" if filename else ""
    _LOG.warning(
        "httpx could not load the configured CA bundle%s; retrying with the OS default trust store",
        location,
    )
    return context


def _missing_ca_message(error: FileNotFoundError) -> str:
    filename = getattr(error, "filename", None)
    location = f" at {filename}" if filename else ""
    return f"failed to load TLS CA bundle{location}: {error}"
