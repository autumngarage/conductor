"""Audit curated OpenRouter model stacks against the live catalog.

The audit is deliberately a report, not an auto-updater. It validates catalog
availability and capabilities while keeping quality ordering in the explicit
policy file.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Literal

from conductor import openrouter_model_stacks

if TYPE_CHECKING:
    from conductor.providers.openrouter_catalog import CatalogSnapshot, ModelEntry

STACK_CONTEXT_WARNING_THRESHOLD = 100_000

Severity = Literal["error", "warning"]


@dataclass(frozen=True)
class StackDefinition:
    name: str
    effort: str
    models: tuple[str, ...]


@dataclass(frozen=True)
class StackFinding:
    severity: Severity
    stack: str
    model: str
    code: str
    message: str


@dataclass(frozen=True)
class StackModelAudit:
    stack: str
    position: int
    model: str
    catalog_available: bool
    direct_sendable: bool
    supports_tools: bool | None
    supports_thinking: bool | None
    context_length: int | None
    caveats: tuple[str, ...]
    rationale: str | None


@dataclass(frozen=True)
class StackAuditReport:
    stack_version: str
    policy: str
    catalog_fetched_at: int
    models: tuple[StackModelAudit, ...]
    findings: tuple[StackFinding, ...]

    @property
    def has_errors(self) -> bool:
        return any(finding.severity == "error" for finding in self.findings)

    @property
    def has_warnings(self) -> bool:
        return any(finding.severity == "warning" for finding in self.findings)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def default_openrouter_coding_stacks() -> tuple[StackDefinition, ...]:
    return (
        StackDefinition(
            name="OPENROUTER_CODING_HIGH",
            effort="high",
            models=openrouter_model_stacks.OPENROUTER_CODING_HIGH,
        ),
        StackDefinition(
            name="OPENROUTER_CODING_MAX",
            effort="max",
            models=openrouter_model_stacks.OPENROUTER_CODING_MAX,
        ),
    )


def audit_openrouter_coding_stacks(
    snapshot: CatalogSnapshot,
    *,
    stacks: tuple[StackDefinition, ...] | None = None,
) -> StackAuditReport:
    by_id = {model.id: model for model in snapshot.models}
    stack_defs = stacks or default_openrouter_coding_stacks()
    audited_models: list[StackModelAudit] = []
    findings: list[StackFinding] = []

    for stack in stack_defs:
        for position, model_id in enumerate(stack.models, start=1):
            catalog_model = by_id.get(model_id)
            direct_sendable = model_id in by_id and not model_id.startswith("~")
            caveats = _model_caveats(model_id, catalog_model)
            audited_models.append(
                StackModelAudit(
                    stack=stack.name,
                    position=position,
                    model=model_id,
                    catalog_available=catalog_model is not None,
                    direct_sendable=direct_sendable,
                    supports_tools=(
                        None if catalog_model is None else catalog_model.supports_tools
                    ),
                    supports_thinking=(
                        None if catalog_model is None else catalog_model.supports_thinking
                    ),
                    context_length=(
                        None if catalog_model is None else catalog_model.context_length
                    ),
                    caveats=tuple(caveats),
                    rationale=openrouter_model_stacks.OPENROUTER_CODING_MODEL_EVIDENCE.get(
                        model_id
                    ),
                )
            )
            findings.extend(_findings_for_model(stack, model_id, catalog_model))

    return StackAuditReport(
        stack_version=openrouter_model_stacks.OPENROUTER_CODING_STACK_VERSION,
        policy=openrouter_model_stacks.OPENROUTER_CODING_STACK_POLICY,
        catalog_fetched_at=snapshot.fetched_at,
        models=tuple(audited_models),
        findings=tuple(findings),
    )


def _findings_for_model(
    stack: StackDefinition,
    model_id: str,
    catalog_model: ModelEntry | None,
) -> list[StackFinding]:
    findings: list[StackFinding] = []
    if model_id.startswith("~"):
        findings.append(
            _finding(
                "error",
                stack,
                model_id,
                "alias-not-sendable",
                "moving OpenRouter aliases are policy labels, not direct request slugs",
            )
        )
        return findings

    if catalog_model is None:
        findings.append(
            _finding(
                "error",
                stack,
                model_id,
                "missing-from-catalog",
                "model is absent from the live OpenRouter catalog",
            )
        )
        return findings

    if _is_free_tier(model_id):
        findings.append(
            _finding(
                "error",
                stack,
                model_id,
                "free-tier-model",
                "free-tier models are not eligible for curated coding fallback stacks",
            )
        )
    if not catalog_model.supports_tools:
        findings.append(
            _finding(
                "error",
                stack,
                model_id,
                "missing-tool-support",
                "model does not advertise OpenRouter tools support",
            )
        )
    if not catalog_model.supports_thinking:
        findings.append(
            _finding(
                "warning",
                stack,
                model_id,
                "missing-reasoning-support",
                "model does not advertise reasoning/reasoning_effort support",
            )
        )
    if catalog_model.context_length < STACK_CONTEXT_WARNING_THRESHOLD:
        findings.append(
            _finding(
                "warning",
                stack,
                model_id,
                "short-context",
                f"context length is below {STACK_CONTEXT_WARNING_THRESHOLD:,}",
            )
        )
    if "preview" in model_id.lower():
        findings.append(
            _finding(
                "warning",
                stack,
                model_id,
                "preview-model",
                "preview models can change behavior or disappear without notice",
            )
        )
    if model_id not in openrouter_model_stacks.OPENROUTER_CODING_MODEL_EVIDENCE:
        findings.append(
            _finding(
                "warning",
                stack,
                model_id,
                "missing-policy-evidence",
                "curated stack entry has no recorded quality-policy rationale",
            )
        )
    return findings


def _finding(
    severity: Severity,
    stack: StackDefinition,
    model_id: str,
    code: str,
    message: str,
) -> StackFinding:
    return StackFinding(
        severity=severity,
        stack=stack.name,
        model=model_id,
        code=code,
        message=message,
    )


def _model_caveats(model_id: str, catalog_model: ModelEntry | None) -> list[str]:
    caveats: list[str] = []
    if model_id.startswith("~"):
        caveats.append("alias")
    if _is_free_tier(model_id):
        caveats.append("free-tier")
    if "preview" in model_id.lower():
        caveats.append("preview")
    if catalog_model is not None and not catalog_model.supports_tools:
        caveats.append("no-tools")
    if catalog_model is not None and not catalog_model.supports_thinking:
        caveats.append("no-reasoning")
    return caveats


def _is_free_tier(model_id: str) -> bool:
    return model_id.lower().endswith(":free") or ":free/" in model_id.lower()
