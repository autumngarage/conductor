"""Provider capability facade used by shared routing and lifecycle code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from conductor.providers.interface import (
    PROVIDER_RUNTIME_KINDS,
    PROVIDER_RUNTIME_TEXT_ONLY,
    TOOL_NAMES,
)


@dataclass(frozen=True)
class ProviderCapabilities:
    provider_id: str
    tags: frozenset[str]
    quality_tier: str
    runtime_kind: str
    supported_tools: frozenset[str]
    enforces_exec_tool_permissions: bool
    supports_effort: bool
    supports_image_attachments: bool
    supports_native_review: bool
    supports_exec_iteration_cap: bool
    streams_output: bool
    model_family: str | None
    max_context_tokens: int | None


def capabilities_for(provider: Any) -> ProviderCapabilities:
    """Return the normalized capability view for a provider instance or class.

    Provider adapters own their quirks through class attributes. This helper is
    the shared-code boundary: call sites ask capability questions instead of
    branching on provider identifiers.
    """
    provider_id = str(getattr(provider, "name", "") or "")
    runtime_kind = getattr(provider, "runtime_kind", PROVIDER_RUNTIME_TEXT_ONLY)
    if runtime_kind not in PROVIDER_RUNTIME_KINDS:
        runtime_kind = PROVIDER_RUNTIME_TEXT_ONLY
    supported_tools = getattr(provider, "supported_tools", frozenset())
    if not isinstance(supported_tools, frozenset):
        supported_tools = frozenset(supported_tools or ())
    supported_tools = frozenset(tool for tool in supported_tools if tool in TOOL_NAMES)
    return ProviderCapabilities(
        provider_id=provider_id,
        tags=frozenset(getattr(provider, "tags", ()) or ()),
        quality_tier=str(getattr(provider, "quality_tier", "standard") or "standard"),
        runtime_kind=str(runtime_kind),
        supported_tools=supported_tools,
        enforces_exec_tool_permissions=bool(
            getattr(provider, "enforces_exec_tool_permissions", False)
        ),
        supports_effort=bool(getattr(provider, "supports_effort", False)),
        supports_image_attachments=bool(getattr(provider, "supports_image_attachments", False)),
        supports_native_review=bool(getattr(provider, "supports_native_review", False)),
        supports_exec_iteration_cap=bool(getattr(provider, "supports_exec_iteration_cap", False)),
        streams_output=bool(
            getattr(provider, "streams_output", runtime_kind != PROVIDER_RUNTIME_TEXT_ONLY)
        ),
        model_family=_optional_str(getattr(provider, "model_family", None)),
        max_context_tokens=_optional_int(getattr(provider, "max_context_tokens", None)),
    )


def _optional_str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None
