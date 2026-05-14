"""Conductor CLI — call, exec, list, smoke, doctor, init, update, route, config.

v0.2 surface (call/exec):
  conductor call --with <id> [--effort max] --brief "..."
  conductor call --auto [--tags a,b] [--prefer best] [--effort max] --brief "..."
  conductor exec --auto [--tools Read,Grep,Edit] --brief-file PATH

v0.1 surface (unchanged):
  conductor list [--json]
  conductor smoke [<id>] [--all] [--json]
  conductor doctor [--json]
  conductor init [--yes]
  conductor update [--dry-run] [--check] [--json]
  conductor update-all [--paths PATHS] [--config-file FILE]

v0.2 additions:
  conductor route --tags a,b [--prefer best] [--tools X,Y] [--dry-run]
  conductor config show
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal, NoReturn
from urllib.parse import urlparse

import click
from click.core import ParameterSource
from packaging.version import InvalidVersion
from packaging.version import parse as parse_version

import conductor.git_state as git_state
import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor import __version__, credentials, offline_mode
from conductor._issue_briefs import (
    IssueBriefError,
    append_operator_context,
    build_issue_brief,
)
from conductor._time_filter import parse_timestamp, since_cutoff
from conductor.banner import print_caller_banner
from conductor.brief_preprocessor import inject_auto_close
from conductor.delegation_ledger import (
    DelegationEvent,
    DelegationStatus,
    read_delegations,
    record_delegation,
)
from conductor.delegation_report import build_delegation_report
from conductor.exec_completion import brief_declares_read_only_text_output
from conductor.git_state import (
    DEFAULT_BRANCH_SCAN_LIMIT,
    DEFAULT_KEEP_WORKTREE_DAYS,
    GIT_STATE_COMMAND_TIMEOUT_SEC,
    AbandonedWorktree,
    BranchScanLimit,
    GitCleanupPlan,
    GitStateError,
    ProtectedRef,
    StaleBranch,
    scan_git_state,
)
from conductor.internal_config import InternalConfigError, internal_telemetry_enabled
from conductor.muted_providers import (
    MutedProvidersError,
    load_muted_provider_ids,
    mute_provider_ids,
    muted_providers_file_path,
    unmute_provider_ids,
)
from conductor.network_profile import (
    NETWORK_PROFILE_FALLBACK_TARGET,
    NetworkProfile,
    apply_scaling,
    get_network_profile,
    scaling_multiplier,
)
from conductor.openrouter_stack_audit import (
    StackAuditReport,
    audit_openrouter_coding_stacks,
)
from conductor.profiles import ProfileError, ProfileSpec, get_profile, load_profiles
from conductor.providers import (
    PROVIDER_RUNTIME_KINDS,
    PROVIDER_RUNTIME_STATEFUL_AGENT,
    PROVIDER_RUNTIME_STATELESS_TOOL_LOOP,
    PROVIDER_RUNTIME_TEXT_ONLY,
    QUALITY_TIERS,
    TIER_RANK,
    TOOL_NAMES,
    CallResponse,
    ClaudeProvider,
    CodexProvider,
    NativeReviewProvider,
    OllamaProvider,
    OpenRouterProvider,
    ProviderConfigError,
    ProviderError,
    ProviderExecutionError,
    ProviderHTTPError,
    ProviderStalledError,
    UnsupportedCapability,
    get_provider,
    known_providers,
    resolve_effort_tokens,
)
from conductor.providers.review_contract import (
    ReviewContextError,
    ReviewOutputContractError,
    build_review_task_prompt,
    validate_requested_review_sentinel,
)
from conductor.router import (
    DEFAULT_ESTIMATED_INPUT_TOKENS,
    DEFAULT_ESTIMATED_OUTPUT_TOKENS,
    VALID_PREFER_MODES,
    InvalidRouterRequest,
    NoConfiguredProvider,
    RankedCandidate,
    RouteDecision,
    mark_outcome,
    mark_rate_limited,
    pick,
)
from conductor.router_defaults import (
    RouterDefaultsError,
    load_home_tag_defaults,
    load_tag_defaults,
    repo_router_defaults_path,
    set_home_tag_default,
    unset_home_tag_default,
)
from conductor.semantic import (
    SEMANTIC_KINDS,
    SemanticCandidate,
    SemanticPlan,
    plan_for,
    with_candidate_override,
)
from conductor.session_log import (
    SessionLog,
    SessionLogError,
    SessionRecord,
    find_session_record,
    latest_active_session,
    list_session_records,
    sessions_dir,
)
from conductor.wizard import run_init_wizard

VALID_TOOLS = ("Read", "Grep", "Glob", "Edit", "Write", "Bash")
EXEC_PERMISSION_PROFILES: dict[str, frozenset[str]] = {
    "read-only": frozenset({"Read", "Grep", "Glob"}),
    "patch": frozenset({"Read", "Grep", "Glob", "Edit", "Write"}),
    "full": frozenset(VALID_TOOLS),
}
SANDBOX_DEPRECATION_WARNING = (
    "[conductor] --sandbox is deprecated and ignored; conductor exec runs "
    "unsandboxed. Use --permission-profile for an enforceable Conductor "
    "tool whitelist."
)
VALID_EFFORT_LEVELS = ("minimal", "low", "medium", "high", "max")
PROFILE_PRECEDENCE_TEXT = (
    "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
)
DEFAULT_EXEC_MAX_STALL_SEC = 360
MIN_EXEC_BRIEF_CHARS = 300
DEFAULT_EXEC_MAX_ITERATIONS = 10
EXEC_MAX_ITERATION_MULTIPLIERS = {
    "minimal": 1.0,
    "low": 1.5,
    "medium": 2.0,
    "high": 6.0,
    "max": 8.0,
}
EXEC_MAX_ITERATIONS_HELP = (
    "Maximum Conductor-managed tool-use loop iterations. Default scales with "
    "--effort from base 10: minimal=10, low=15, medium=20, high=60, max=80. "
    "If --effort is unset, preserves the legacy cap of 10."
)
EXEC_MAX_ITERATION_PROVIDER_IDS = frozenset({"codex", "openrouter", "ollama"})
GIT_RECOVERY_COMMAND_TIMEOUT_SEC = 2.0
GIT_RECOVERY_MAX_COMMITS = 5
GIT_RECOVERY_MAX_STATUS_PATHS = 8
NATIVE_REVIEW_TAG = "code-review"
LOCAL_PROVIDER_IDS = frozenset({"ollama"})
LOCAL_ONLINE_OPT_IN_ENV = "CONDUCTOR_ALLOW_LOCAL_ONLINE"
REVIEW_IGNORED_TASK_TAGS = frozenset({"tool-use"})
DEFAULT_SESSION_PRUNE_OLDER_THAN = "30d"
DEFAULT_SESSION_PRUNE_PROTECT_LAST = 50
REPO_INTEGRATION_KINDS = frozenset(
    {
        "agents-md-import",
        "gemini-md-import",
        "claude-md-repo-import",
        "cursor-rule",
    }
)
PRE_COMMIT_CONFIG = ".pre-commit-config.yaml"
CONDUCTOR_REFRESH_HOOK_ID = "conductor-refresh"
CONDUCTOR_REFRESH_HOOK_LINES = (
    "- repo: local",
    "  hooks:",
    "    - id: conductor-refresh",
    "      name: Refresh conductor integrations if stale",
    "      entry: conductor refresh-on-commit",
    "      language: system",
    "      pass_filenames: false",
    "      always_run: true",
    "      stages: [pre-commit]",
)
CONDUCTOR_REFRESH_HOOK_BLOCK = """- repo: local
  hooks:
    - id: conductor-refresh
      name: Refresh conductor integrations if stale
      entry: conductor refresh-on-commit
      language: system
      pass_filenames: false
      always_run: true
      stages: [pre-commit]
"""

if TYPE_CHECKING:
    from collections.abc import Callable

    from conductor.agent_wiring import AgentArtifact, RepoScopeVersionDecision

DEFAULT_REVIEW_MAX_FALLBACKS = 3
REVIEW_GATE_MAX_EXEC_ITERATIONS = 12
DEFAULT_COUNCIL_ROUNDS = 1
DEFAULT_COUNCIL_TIMEOUT_SEC = 180
DEFAULT_COUNCIL_MAX_OUTPUT_TOKENS = 6_000
DEFAULT_COUNCIL_MAX_COST_USD = 0.25
ESTIMATED_CHARS_PER_TOKEN = 4
AUTO_FALLBACK_STALL_FRACTION = 4
AUTO_FALLBACK_MIN_STALL_SEC = 30
REVIEW_GATE_BASE_TIMEOUT_SEC = 240
REVIEW_GATE_MIN_TIMEOUT_SEC = 300
REVIEW_GATE_MAX_TIMEOUT_SEC = 1_200
REVIEW_GATE_SECONDS_PER_1K_INPUT_TOKENS = 8
REVIEW_GATE_MAX_STALL_SEC = 180


@dataclass(frozen=True)
class BriefInput:
    body: str
    source: str
    attachments: tuple[Path, ...] = ()


@dataclass(frozen=True)
class BriefPhase:
    title: str
    body: str


@dataclass(frozen=True)
class CouncilCaps:
    timeout_sec: int | None
    max_output_tokens: int | None
    max_cost_usd: float | None


@dataclass(frozen=True)
class SessionPrunePath:
    path: Path
    size_bytes: int
    deleted: bool = False
    error: str | None = None


@dataclass(frozen=True)
class SessionPruneItem:
    kind: str
    session_id: str | None
    status: str | None
    updated_at: str
    paths: tuple[SessionPrunePath, ...]


@dataclass(frozen=True)
class SessionPrunePlan:
    dry_run: bool
    older_than: str | None
    keep_last: int | None
    protect_last: int
    total_items: int
    total_paths: int
    total_bytes: int
    items: tuple[SessionPruneItem, ...]


@dataclass(frozen=True)
class ReviewProviderStatus:
    provider: str
    status: str
    detail: str


@dataclass(frozen=True)
class ReviewProviderAttempt:
    provider: str
    model: str | None
    status: str
    failure_code: str | None
    elapsed_ms: int
    detail: str | None = None
    route_mode: str | None = None


class ReviewInfrastructureError(ProviderError):
    """Raised when review routing cannot produce a valid review verdict."""

    def __init__(
        self,
        message: str,
        *,
        exit_class: str,
        failure_code: str | None,
        attempts: list[dict[str, object]],
        selected_provider: str | None = None,
        selected_model: str | None = None,
        review_verdict: str = "unknown",
        excluded: list[dict[str, object]] | None = None,
        skipped_by_max_fallbacks: list[str] | None = None,
    ) -> None:
        review_payload: dict[str, object] = {
            "exit_class": exit_class,
            "failure_code": failure_code,
            "attempts": attempts,
            "selected_provider": selected_provider,
            "selected_model": selected_model,
            "review_verdict": review_verdict,
        }
        if excluded is not None:
            review_payload["excluded"] = excluded
        if skipped_by_max_fallbacks:
            review_payload["skipped_by_max_fallbacks"] = skipped_by_max_fallbacks
        self.error_response = {
            "error": (
                "review_no_viable_provider"
                if exit_class == "no_viable_provider"
                else "review_infrastructure_failed"
            ),
            "message": message,
            **review_payload,
            "review": review_payload,
        }
        super().__init__(message)


# --------------------------------------------------------------------------- #
# Shared helpers
# --------------------------------------------------------------------------- #


def _parameter_is_default(name: str) -> bool:
    ctx = click.get_current_context(silent=True)
    if ctx is None:
        return True
    return ctx.get_parameter_source(name) == ParameterSource.DEFAULT


def _network_target_for_provider(provider_id: str | None) -> str | None:
    if provider_id == "ollama":
        return os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434"
    if provider_id is None:
        return NETWORK_PROFILE_FALLBACK_TARGET
    return get_provider(provider_id).endpoint_url()


def _network_profile_warning(message: str) -> None:
    click.echo(message, err=True)


def _target_label(target: str) -> str:
    parsed = urlparse(target)
    return parsed.netloc or parsed.path or target


def _emit_network_scaling_notice(profile: NetworkProfile) -> None:
    multiplier = scaling_multiplier(profile)
    if multiplier == 1 or profile.rtt_ms is None:
        return
    click.echo(
        "[conductor] network: "
        f"{profile.rtt_ms:.0f}ms RTT to {_target_label(profile.target)} "
        f"→ timeouts scaled {multiplier}× (override with --timeout)",
        err=True,
    )


def _scale_dispatch_defaults(
    *,
    provider_id: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    timeout_is_default: bool,
    max_stall_is_default: bool,
) -> tuple[int | None, int | None]:
    if not (timeout_is_default or max_stall_is_default):
        return timeout_sec, max_stall_sec

    profile = get_network_profile(
        _network_target_for_provider(provider_id),
        warn=_network_profile_warning,
    )
    _emit_network_scaling_notice(profile)

    resolved_timeout: int | None = timeout_sec
    if timeout_is_default:
        scaled_timeout = apply_scaling(timeout_sec, profile)
        resolved_timeout = None if scaled_timeout is None else math.ceil(scaled_timeout)

    resolved_max_stall: int | None = max_stall_sec
    if max_stall_is_default:
        scaled_stall = apply_scaling(max_stall_sec, profile)
        resolved_max_stall = None if scaled_stall is None else math.ceil(scaled_stall)

    return resolved_timeout, resolved_max_stall


def _cap_default_auto_fallback_stall(
    *,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    max_stall_is_default: bool,
    candidate_count: int,
) -> int | None:
    """Keep default stall watchdogs short enough for auto fallback.

    Invariant: when the caller supplies a finite command timeout and leaves
    max-stall at its default, a silent primary provider must not consume the
    whole invocation budget before the router can try the next candidate.
    """
    if (
        not max_stall_is_default
        or timeout_sec is None
        or max_stall_sec is None
        or candidate_count < 2
    ):
        return max_stall_sec
    cap = max(AUTO_FALLBACK_MIN_STALL_SEC, timeout_sec // AUTO_FALLBACK_STALL_FRACTION)
    if cap >= timeout_sec:
        cap = max(1, timeout_sec - 1)
    return min(max_stall_sec, cap)


def _review_gate_timeout_sec(estimated_input_tokens: int) -> int:
    token_units = math.ceil(max(1, estimated_input_tokens) / 1_000)
    derived = (
        REVIEW_GATE_BASE_TIMEOUT_SEC
        + token_units * REVIEW_GATE_SECONDS_PER_1K_INPUT_TOKENS
    )
    return min(
        REVIEW_GATE_MAX_TIMEOUT_SEC,
        max(REVIEW_GATE_MIN_TIMEOUT_SEC, derived),
    )


def _review_gate_stall_sec(timeout_sec: int, *, candidate_count: int) -> int:
    effective_candidates = max(1, candidate_count)
    cap = max(
        AUTO_FALLBACK_MIN_STALL_SEC,
        timeout_sec // max(AUTO_FALLBACK_STALL_FRACTION, effective_candidates + 1),
    )
    if cap >= timeout_sec:
        cap = max(1, timeout_sec - 1)
    return min(REVIEW_GATE_MAX_STALL_SEC, cap)


def _apply_review_gate_auto_budget(
    *,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    timeout_is_default: bool,
    max_stall_is_default: bool,
    estimated_input_tokens: int,
    candidate_count: int,
    silent: bool,
) -> tuple[int | None, int | None]:
    budget_timeout_sec = _review_gate_timeout_sec(estimated_input_tokens)
    budget_stall_sec = _review_gate_stall_sec(
        budget_timeout_sec,
        candidate_count=candidate_count,
    )
    caller_timeout_sec = None if timeout_is_default else timeout_sec
    caller_stall_sec = None if max_stall_is_default else max_stall_sec
    timeout_sec = budget_timeout_sec
    max_stall_sec = budget_stall_sec
    if not silent:
        stall_label = "disabled" if max_stall_sec is None else f"{max_stall_sec}s"
        ignored_parts: list[str] = []
        if caller_timeout_sec is not None and caller_timeout_sec != timeout_sec:
            ignored_parts.append(f"timeout={caller_timeout_sec}s")
        if caller_stall_sec is not None and caller_stall_sec != max_stall_sec:
            ignored_parts.append(f"max-stall={caller_stall_sec}s")
        ignored = (
            " ignored caller " + " ".join(ignored_parts)
            if ignored_parts
            else ""
        )
        click.echo(
            "[conductor] review gate budget: "
            f"timeout={timeout_sec}s stall={stall_label} "
            f"candidates={candidate_count} est_input={estimated_input_tokens:,} tokens"
            f"{ignored}",
            err=True,
        )
    return timeout_sec, max_stall_sec


def _review_gate_deadline(timeout_sec: int | None) -> float | None:
    if timeout_sec is None:
        return None
    return time.monotonic() + timeout_sec


def _bounded_attempt_budget(
    *,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    deadline_monotonic: float | None,
    budget_label: str,
    provider_name: str,
) -> tuple[int | None, int | None]:
    """Return the per-attempt budget remaining under a shared route deadline.

    Invariant: a fallback chain with a finite deadline never hands a later
    provider more wall-clock or stall budget than remains in the chain.
    """
    if deadline_monotonic is None:
        return timeout_sec, max_stall_sec

    remaining = deadline_monotonic - time.monotonic()
    if remaining <= 0:
        raise ProviderStalledError(
            f"{budget_label} exhausted before trying {provider_name}"
        )
    remaining_sec = max(1, math.ceil(remaining))
    attempt_timeout_sec = (
        remaining_sec if timeout_sec is None else min(timeout_sec, remaining_sec)
    )
    attempt_max_stall_sec = max_stall_sec
    if attempt_max_stall_sec is not None:
        attempt_max_stall_sec = min(attempt_max_stall_sec, attempt_timeout_sec)
    return attempt_timeout_sec, attempt_max_stall_sec


def _bounded_review_attempt_budget(
    *,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    deadline_monotonic: float | None,
    provider_name: str,
    is_fallback: bool,
    remaining_fallback_count: int = 0,
) -> tuple[int | None, int | None]:
    """Return the review attempt budget under shared and per-fallback caps.

    Invariant: a review provider attempt cannot consume time reserved for
    later fallback providers. Once the route is in fallback, a later provider
    also cannot receive a wall-clock timeout larger than the derived review
    stall budget. This matters for HTTP providers that cannot emit progress
    and do not implement the no-output watchdog separately.
    """
    attempt_timeout_sec, attempt_max_stall_sec = _bounded_attempt_budget(
        timeout_sec=timeout_sec,
        max_stall_sec=max_stall_sec,
        deadline_monotonic=deadline_monotonic,
        budget_label="review gate budget",
        provider_name=provider_name,
    )
    if (
        remaining_fallback_count > 0
        and attempt_timeout_sec is not None
        and attempt_max_stall_sec is not None
    ):
        reserved_sec = remaining_fallback_count * attempt_max_stall_sec
        if reserved_sec >= attempt_timeout_sec:
            attempt_timeout_sec = max(
                1,
                attempt_timeout_sec // (remaining_fallback_count + 1),
            )
        else:
            attempt_timeout_sec = max(1, attempt_timeout_sec - reserved_sec)
    if is_fallback and attempt_max_stall_sec is not None:
        attempt_timeout_sec = (
            attempt_max_stall_sec
            if attempt_timeout_sec is None
            else min(attempt_timeout_sec, attempt_max_stall_sec)
        )
    if attempt_timeout_sec is not None and attempt_max_stall_sec is not None:
        attempt_max_stall_sec = min(attempt_max_stall_sec, attempt_timeout_sec)
    return attempt_timeout_sec, attempt_max_stall_sec


def _read_task(
    task: str | None,
    task_file: str | None,
    *,
    brief: str | None = None,
    brief_file: str | None = None,
    issue: str | None = None,
    issue_comment_limit: int = 10,
    cwd: str | None = None,
    attach: tuple[str, ...] = (),
) -> BriefInput:
    explicit_sources = [
        (name, value)
        for name, value in (
            ("--task", task),
            ("--brief", brief),
            ("--task-file", task_file),
            ("--brief-file", brief_file),
        )
        if value is not None
    ]
    if len(explicit_sources) > 1:
        got = ", ".join(name for name, _value in explicit_sources)
        raise click.UsageError(
            "brief source is ambiguous. Use exactly one of --brief, --brief-file, "
            f"--task, --task-file, or stdin; got {got}."
        )

    source = "stdin"
    operator_body: str | None = None
    operator_source: str | None = None
    if brief is not None:
        body = brief
        operator_body = body
        operator_source = "--brief"
    elif task is not None:
        body = task
        operator_body = body
        operator_source = "--task"
    elif brief_file is not None or task_file is not None:
        file_source = brief_file if brief_file is not None else task_file
        operator_source = "--brief-file" if brief_file is not None else "--task-file"
        assert file_source is not None
        if file_source == "-":
            body = sys.stdin.read()
        else:
            try:
                body = Path(file_source).read_text(encoding="utf-8")
            except OSError as e:
                raise click.UsageError(
                    f"could not read {operator_source} {file_source!r}: {e.strerror or e}"
                ) from e
        operator_body = body
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
        operator_body = body
        operator_source = "stdin"
    elif issue is not None:
        body = ""
    else:
        raise click.UsageError(
            "no brief provided. Pass --brief '...', --brief-file PATH, "
            "--task '...', --task-file PATH, or pipe content on stdin."
        )

    if issue is not None:
        try:
            body = append_operator_context(
                build_issue_brief(
                    issue,
                    comment_limit=issue_comment_limit,
                    cwd=cwd,
                ),
                operator_body,
            )
        except IssueBriefError as e:
            raise click.UsageError(str(e)) from e
        source = "--issue" if operator_source is None else f"--issue + {operator_source}"

    body = body.strip()
    if not body:
        raise click.UsageError("brief is empty after stripping whitespace.")

    attachments: tuple[Path, ...] = ()
    if attach:
        resolved: list[Path] = []
        for raw_path in attach:
            path = Path(raw_path).expanduser()
            if not path.is_file():
                raise click.UsageError(f"--attach path {raw_path!r} is not a readable file.")
            resolved.append(path.resolve())
        attachments = tuple(resolved)
    return BriefInput(body=body, source=source, attachments=attachments)


def _with_auto_close_instructions(brief_input: BriefInput) -> BriefInput:
    try:
        body = inject_auto_close(brief_input.body)
    except Exception as e:
        click.echo(
            f"[conductor] warning: could not preprocess auto-close instructions: {e}",
            err=True,
        )
        return brief_input
    if body == brief_input.body:
        return brief_input
    return replace(brief_input, body=body)


def _warn_if_short_exec_brief(
    brief_input: BriefInput,
    *,
    allow_short_brief: bool,
) -> None:
    if allow_short_brief or len(brief_input.body) >= MIN_EXEC_BRIEF_CHARS:
        return
    click.echo(
        "[conductor] brief is short "
        f"({len(brief_input.body)} chars). Delegated exec only sees this brief "
        "plus files it can inspect; for Claude/Codex handoffs, prefer "
        "--brief-file with goal, context, scope, constraints, expected output, "
        "and validation. Pass --allow-short-brief to silence this warning.",
        err=True,
    )


def _call_mode_side_effect_reason(body: str) -> str | None:
    if brief_declares_read_only_text_output(body):
        return None
    patterns = (
        (
            re.compile(
                r"(?is)\b(write|create|update|edit|modify)\b.{0,80}"
                r"\b(file|path|\.md|\.py|\.toml|AGENTS\.md|CLAUDE\.md)\b"
            ),
            "write local files",
        ),
        (
            re.compile(r"(?is)\bwrite\b.{0,80}\b(to|at)\s+`?[./~\w-]"),
            "write local files",
        ),
        (re.compile(r"(?i)\b(git\s+)?commit\b"), "commit changes"),
        (re.compile(r"(?i)\b(git\s+)?push\b"), "push a branch"),
        (
            re.compile(r"(?i)\b(open|create)\s+(a\s+)?(draft\s+)?(pr|pull request)\b|\bgh\s+pr\b"),
            "open a pull request",
        ),
        (
            re.compile(r"(?i)\b(create|switch|checkout)\b.{0,40}\bbranch\b|\bgit\s+checkout\b"),
            "change branches",
        ),
    )
    for pattern, reason in patterns:
        if pattern.search(body):
            return reason
    return None


def _reject_call_mode_side_effect_brief(kind: str, body: str) -> None:
    if brief_declares_read_only_text_output(body):
        return
    reason = _call_mode_side_effect_reason(body)
    if reason is None:
        return
    raise click.UsageError(
        f"`conductor ask --kind {kind}` uses call mode and cannot {reason}. "
        "Use `conductor ask --kind code --effort high`, `conductor exec`, "
        "or ask for text output on stdout."
    )


def _ensure_supports_attachments(
    provider_obj: object,
    attachments: tuple[Path, ...],
) -> None:
    """Raise UnsupportedCapability if attachments cannot route to this provider.

    The supported set is currently {codex}; surface a copy-pasteable fix so the
    operator doesn't have to guess which provider to switch to.
    """
    if not attachments:
        return
    if getattr(provider_obj, "supports_image_attachments", False):
        return
    name = getattr(provider_obj, "name", provider_obj.__class__.__name__)
    raise UnsupportedCapability(
        f"provider {name!r} does not accept image attachments. "
        "Re-run with `--with codex` (or `--auto` once another image-capable "
        "provider lands)."
    )


def _parse_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _review_tags(raw: str | None) -> list[str]:
    tags = [NATIVE_REVIEW_TAG]
    for tag in _parse_csv(raw):
        if tag not in tags:
            tags.append(tag)
    return tags


def _resolve_layered_value(
    cli_value: str | None,
    *,
    env_key: str,
    profile_value: str | None = None,
) -> str | None:
    if cli_value is not None:
        return cli_value
    env_value = os.environ.get(env_key)
    if env_value is not None:
        return env_value
    return profile_value


def _load_named_profile(name: str | None) -> ProfileSpec | None:
    if name is None:
        return None
    try:
        return get_profile(name)
    except ProfileError as e:
        raise click.UsageError(str(e)) from e


def _parse_effort(raw: str | None) -> str | int:
    if raw is None:
        return "medium"
    raw = raw.strip()
    if not raw:
        return "medium"
    # Integer budget override.
    if raw.lstrip("-").isdigit():
        n = int(raw)
        if n < 0:
            raise click.UsageError(f"--effort integer must be >= 0, got {n}")
        return n
    if raw not in VALID_EFFORT_LEVELS:
        hint = _closest(raw, VALID_EFFORT_LEVELS)
        raise click.UsageError(
            f"--effort={raw!r} is not valid. "
            f"Use one of: {list(VALID_EFFORT_LEVELS)} or an integer budget. "
            f"Did you mean '{hint}'?"
        )
    return raw


def _resolve_exec_max_iterations(
    explicit_max_iterations: int | None,
    *,
    raw_effort: str | None,
) -> int:
    if explicit_max_iterations is not None:
        return explicit_max_iterations
    if raw_effort is None:
        return DEFAULT_EXEC_MAX_ITERATIONS
    effort = raw_effort.strip()
    multiplier = EXEC_MAX_ITERATION_MULTIPLIERS.get(effort, 1.0)
    return int(DEFAULT_EXEC_MAX_ITERATIONS * multiplier)


def _provider_supports_exec_max_iterations(provider_id: str) -> bool:
    return provider_id in EXEC_MAX_ITERATION_PROVIDER_IDS


def _exec_max_iterations_unsupported_message(provider_id: str) -> str:
    supported = ", ".join(sorted(EXEC_MAX_ITERATION_PROVIDER_IDS))
    return (
        "--max-iterations only applies to Conductor-managed tool-use loops "
        f"({supported}); {provider_id} cannot honor it."
    )


def _validate_tools(raw: str | None) -> frozenset[str]:
    tools = _parse_csv(raw)
    unknown = [t for t in tools if t not in VALID_TOOLS]
    if unknown:
        raise click.UsageError(
            f"--tools contains unknown tool(s): {unknown}. Known: {list(VALID_TOOLS)}."
        )
    return frozenset(tools)


def _estimate_text_tokens(text: str) -> int:
    return max(1, (len(text) + ESTIMATED_CHARS_PER_TOKEN - 1) // ESTIMATED_CHARS_PER_TOKEN)


def _estimate_review_input_tokens(
    task: str,
    *,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None = None,
    cwd: str | None,
) -> int:
    estimated = _estimate_text_tokens(task)
    if not (base or commit or uncommitted):
        return estimated
    try:
        review_prompt = build_review_task_prompt(
            task,
            base=base,
            commit=commit,
            uncommitted=uncommitted,
            title=title,
            cwd=cwd,
            include_patch=True,
        )
    except ReviewContextError as e:
        click.echo(f"[conductor] could not estimate review patch size: {e}", err=True)
        return estimated
    return _estimate_text_tokens(review_prompt)


def _review_input_context_warning(
    *,
    estimated_input_tokens: int,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
) -> str | None:
    if base or commit or uncommitted:
        return None
    return (
        "[conductor] token estimate is prompt-only "
        f"({estimated_input_tokens:,} input tokens from the brief). "
        "No explicit review target was provided; native review providers may "
        "load repository or diff context outside this estimate. Pass --base, "
        "--commit, or --uncommitted for a review-target estimate."
    )


def _semantic_tool_context_warning(
    plan: SemanticPlan,
    *,
    cwd: str | None,
    issue: str | None,
    estimated_input_tokens: int,
) -> str | None:
    if plan.kind != "code" or plan.mode != "exec":
        return None
    if cwd is None and issue is None:
        return None
    context_sources = []
    if cwd is not None:
        context_sources.append("--cwd")
    if issue is not None:
        context_sources.append("--issue")
    source_text = " + ".join(context_sources)
    return (
        "[conductor] token estimate is prompt-only "
        f"({estimated_input_tokens:,} input tokens from the brief). "
        f"Because {source_text} routes to a tool-using code agent, repository "
        "reads and agent/tool context are not bounded by this estimate."
    )


def _ordered_tools_csv(tools: frozenset[str]) -> str:
    return ",".join(tool for tool in VALID_TOOLS if tool in tools)


def _validate_permission_profile(raw: str | None) -> str | None:
    if raw is None:
        return None
    if raw not in EXEC_PERMISSION_PROFILES:
        hint = _closest(raw, tuple(EXEC_PERMISSION_PROFILES))
        raise click.UsageError(
            f"--permission-profile={raw!r} is not valid. "
            f"Use one of: {list(EXEC_PERMISSION_PROFILES)}. "
            f"Did you mean '{hint}'?"
        )
    return raw


def _resolve_exec_tools(
    raw_tools: str | None,
    *,
    permission_profile: str | None,
) -> frozenset[str]:
    tools = _validate_tools(raw_tools)
    if permission_profile is None:
        return tools

    profile_tools = EXEC_PERMISSION_PROFILES[permission_profile]
    if raw_tools is not None and tools != profile_tools:
        raise click.UsageError(
            f"--tools conflicts with --permission-profile={permission_profile!r}; "
            "omit --tools or pass exactly "
            f"{_ordered_tools_csv(profile_tools)!r}."
        )
    return profile_tools


def _provider_enforces_exec_tool_permissions(provider_obj: object) -> bool:
    return bool(getattr(provider_obj, "enforces_exec_tool_permissions", False))


def _provider_runtime_kind(provider_obj: object) -> str:
    runtime_kind = getattr(provider_obj, "runtime_kind", PROVIDER_RUNTIME_TEXT_ONLY)
    if isinstance(runtime_kind, str) and runtime_kind in PROVIDER_RUNTIME_KINDS:
        return runtime_kind
    return PROVIDER_RUNTIME_TEXT_ONLY


def _provider_runtime_kind_by_name(provider_id: str) -> str:
    try:
        return _provider_runtime_kind(get_provider(provider_id))
    except KeyError:
        return PROVIDER_RUNTIME_TEXT_ONLY


def _permission_profile_excludes(
    raw_exclude: str | None,
    *,
    permission_profile: str | None,
) -> frozenset[str]:
    excluded = set(_parse_csv(raw_exclude))
    if permission_profile is None:
        return frozenset(excluded)

    for name in known_providers():
        if name in excluded:
            continue
        try:
            provider_obj = get_provider(name)
        except KeyError:
            continue
        if not _provider_enforces_exec_tool_permissions(provider_obj):
            excluded.add(name)
    return frozenset(excluded)


def _ensure_permission_profile_supported(
    provider_obj: object,
    *,
    provider_id: str,
    permission_profile: str | None,
    tools: frozenset[str],
) -> None:
    if permission_profile is None:
        return
    if not _provider_enforces_exec_tool_permissions(provider_obj):
        raise click.UsageError(
            f"--permission-profile={permission_profile!r} requires a provider "
            "that enforces Conductor exec tool whitelists; "
            f"provider {provider_id!r} does not."
        )

    supported_tools: frozenset[str] = getattr(
        provider_obj,
        "supported_tools",
        frozenset(),
    )
    missing = tools - supported_tools
    if missing:
        raise click.UsageError(
            f"--permission-profile={permission_profile!r} requires tools "
            f"{_ordered_tools_csv(tools)!r}, but provider {provider_id!r} "
            f"does not support {sorted(missing)}."
        )


def _validate_sandbox(raw: str | None, *, warn: bool = False) -> str:
    if raw is None:
        return "none"
    if warn:
        click.echo(SANDBOX_DEPRECATION_WARNING, err=True)
    return "none"


def _validate_prefer(raw: str | None) -> str:
    if raw is None:
        return "balanced"
    if raw not in VALID_PREFER_MODES:
        hint = _closest(raw, VALID_PREFER_MODES)
        raise click.UsageError(
            f"--prefer={raw!r} is not valid. "
            f"Use one of: {list(VALID_PREFER_MODES)}. "
            f"Did you mean '{hint}'?"
        )
    return raw


def _normalize_max_stall_sec(raw: int | None) -> int | None:
    if raw is None:
        return None
    if raw < 0:
        raise click.UsageError(f"--max-stall-seconds must be >= 0, got {raw}.")
    if raw == 0:
        return None
    return raw


def _normalize_start_timeout_sec(raw: float | None) -> float | None:
    if raw is None:
        return None
    if raw < 0:
        raise click.UsageError(f"--start-timeout must be >= 0, got {raw:g}.")
    if raw == 0:
        return None
    return raw


def _apply_offline_flag(
    *, offline: bool | None, provider_id: str | None, auto: bool
) -> tuple[str | None, bool]:
    """Translate ``--offline/--no-offline`` into the routing knobs.

    Returns ``(provider_id, auto)`` with offline semantics applied:

    - ``--offline`` (True): sets the sticky offline flag and forces
      ``--with ollama`` regardless of ``--auto`` / ``--tags`` / etc.
      Auto-routing doesn't compose with a force-local directive — if the
      router filters out ollama for any reason (exclude list, unmet tool
      capability, health cooldown), silently falling through to a remote
      provider would violate the documented "force local" contract. So
      ``--offline`` unconditionally rewrites the invocation to explicit
      ollama; ``--auto`` becomes a no-op in that case. Passing
      ``--with <non-ollama>`` alongside ``--offline`` is an error.
    - ``--no-offline`` (False): clears the sticky flag, then behaves normally.
    - ``None``: no-op.
    """
    if offline is None:
        return provider_id, auto
    if offline is False:
        offline_mode.clear()
        return provider_id, auto
    # offline is True
    if provider_id and provider_id != "ollama":
        raise click.UsageError(
            f"--offline forces the local provider; --with {provider_id} "
            "contradicts it. Use one or the other."
        )
    offline_mode.set_active()
    return "ollama", False


def _truthy_env(value: str | None) -> bool:
    return value is not None and value.strip().lower() not in {"", "0", "false", "no", "off"}


def _local_provider_opt_in_active(*, offline_requested: bool) -> bool:
    return (
        offline_requested
        or offline_mode.is_active()
        or _truthy_env(os.environ.get(LOCAL_ONLINE_OPT_IN_ENV))
    )


def _local_provider_online_policy_reason(
    provider_id: str | None,
    *,
    offline_requested: bool,
) -> str | None:
    if provider_id not in LOCAL_PROVIDER_IDS:
        return None
    if _local_provider_opt_in_active(offline_requested=offline_requested):
        return None

    profile = get_network_profile(
        NETWORK_PROFILE_FALLBACK_TARGET,
        warn=_network_profile_warning,
    )
    if profile.rtt_ms is None:
        return None

    return (
        f"provider '{provider_id}' is local/offline-only while network is available; "
        "pass --offline for intentional local/offline routing or set "
        f"{LOCAL_ONLINE_OPT_IN_ENV}=1 to allow local models while online."
    )


def _enforce_local_provider_opt_in(
    provider_id: str | None,
    *,
    offline_requested: bool,
) -> None:
    reason = _local_provider_online_policy_reason(
        provider_id,
        offline_requested=offline_requested,
    )
    if reason is not None:
        raise click.UsageError(reason)


def _local_provider_policy(provider_id: str) -> dict[str, object] | None:
    if provider_id not in LOCAL_PROVIDER_IDS:
        return None
    return {
        "scope": "local/offline-only",
        "online_opt_in_env": LOCAL_ONLINE_OPT_IN_ENV,
    }


def _provider_readiness_label(provider_id: str, configured: bool) -> str:
    if not configured:
        return "no"
    if provider_id in LOCAL_PROVIDER_IDS:
        return "local/offline-only"
    return "yes"


def _closest(query: str, options: tuple[str, ...]) -> str:
    from difflib import get_close_matches

    match = get_close_matches(query, options, n=1, cutoff=0.3)
    return match[0] if match else options[0]


# Message fragments that indicate a connectivity-level failure (DNS
# resolution, TCP reset, unreachable host, etc.). These are what httpx /
# urllib / subprocess tooling surface when the network is gone — the
# airplane-mode case. Matched case-insensitively. Kept conservative: a
# false positive merely cascades a fallback that would have failed anyway,
# but a false negative means we refuse to offer the local-model swap.
_NETWORK_ERROR_SIGNALS = (
    "connection refused",
    "connection reset",
    "connection aborted",
    "connection error",  # httpx ConnectError str()
    "connect call failed",  # asyncio
    "could not resolve",  # curl / some python stacks
    "name or service not known",
    "nodename nor servname",  # macOS getaddrinfo wording
    "temporary failure in name resolution",
    "network is unreachable",
    "network is down",  # macOS airplane mode, ENETDOWN
    "no route to host",
    "no address associated",
    "no such host",
    "host is down",
    "getaddrinfo failed",
)

_RATE_LIMIT_ERROR_SIGNALS = (
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

_UPSTREAM_DOWN_ERROR_SIGNALS = (
    "http 5",
    "returned http 5",
    "exited 5",
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

_EMPTY_RESPONSE_ERROR_SIGNALS = (
    "empty response",
    "empty final response",
    "empty response content",
    "produced empty stdout",
    "produced empty final",
)

_FALLBACK_ERROR_DETAIL_MAX_CHARS = 500


def _is_retryable(err: Exception) -> tuple[bool, str]:
    """Classify an error as retryable-with-fallback or fatal.

    Returns (retryable, category) — category is "rate-limit" | "5xx" |
    "timeout" | "network" | "transport" | "provider-error" | "other" for
    health-tracking and fallback-UX routing purposes. "network" is separate
    from "timeout" so the offline-mode prompt can fire on the real thing
    (DNS/TCP failure) rather than on a slow-but-reachable upstream.
    """
    if isinstance(err, ReviewOutputContractError):
        return True, "output-contract"
    if isinstance(err, ProviderStalledError):
        return True, "timeout"
    if isinstance(err, ProviderExecutionError):
        return True, "provider-error"
    if isinstance(err, ProviderHTTPError) and err.failure_reason:
        reason_categories = {
            "auth_quota": "rate-limit",
            "malformed_response": "provider-error",
            "provider_outage": "5xx",
            "transient_network": "network",
            "usage_config_error": "provider-error",
        }
        return True, reason_categories.get(err.failure_reason, "provider-error")
    msg = str(err).lower()
    if "429" in msg or any(sig in msg for sig in _RATE_LIMIT_ERROR_SIGNALS):
        return True, "rate-limit"
    if any(sig in msg for sig in _EMPTY_RESPONSE_ERROR_SIGNALS):
        return True, "empty-response"
    if any(sig in msg for sig in _NETWORK_ERROR_SIGNALS):
        return True, "network"
    if "timed out" in msg or "timeout" in msg or "stalled" in msg:
        return True, "timeout"
    # HTTP 5xx — check for " 5" preceded by "http" or a similar prefix so
    # we don't match arbitrary "5" digits. Cheap heuristic; acceptable.
    if any(sig in msg for sig in _UPSTREAM_DOWN_ERROR_SIGNALS):
        return True, "5xx"
    if isinstance(err, ProviderHTTPError):
        return True, "transport"
    return False, "other"


def _format_fallback_error_detail(err: Exception) -> str:
    """Return a compact one-line error detail for fallback diagnostics."""
    message = " ".join(str(err).strip().split())
    if not message:
        message = "(no message)"
    detail = f"{err.__class__.__name__}: {message}"
    if len(detail) <= _FALLBACK_ERROR_DETAIL_MAX_CHARS:
        return detail
    return detail[: _FALLBACK_ERROR_DETAIL_MAX_CHARS - 3].rstrip() + "..."


def _review_failure_mode(err: Exception) -> str:
    if isinstance(err, ReviewOutputContractError) and err.possible_findings:
        return "output-contract/quarantined-findings"
    if isinstance(err, ProviderStalledError):
        return "stall"
    _retryable, category = _is_retryable(err)
    return category


def _format_quarantined_review_outputs(
    quarantined: list[ReviewOutputContractError],
) -> str:
    details: list[str] = []
    for err in quarantined:
        preview = " ".join(err.output_preview.strip().split())
        detail = f"{err.provider_name} ({err.reason})"
        if preview:
            detail = f"{detail}: {preview}"
        details.append(detail)
    return "; ".join(details)


def _format_tried_providers(tried: list[tuple[str, str]]) -> str:
    return ", ".join(f"{name} ({mode})" for name, mode in tried)


def _format_review_status_summary(statuses: list[ReviewProviderStatus]) -> str:
    return "; ".join(
        f"{status.provider}: {status.status} - {status.detail}"
        for status in statuses
    )


def _format_max_fallbacks_skipped(
    skipped: list[str],
    *,
    max_fallbacks: int,
) -> str:
    if not skipped:
        return ""
    return (
        f"Skipped by --max-fallbacks={max_fallbacks}: "
        f"{', '.join(skipped)}."
    )


def _review_attempt_payloads(
    attempts: list[ReviewProviderAttempt],
) -> list[dict[str, object]]:
    return [asdict(attempt) for attempt in attempts]


def _review_failure_code(err: Exception, status: str) -> str:
    """Map provider/review failures to the stable JSON error taxonomy."""
    if isinstance(err, ReviewOutputContractError):
        return "malformed_output"
    if isinstance(err, ProviderHTTPError) and err.failure_reason == "malformed_response":
        return "malformed_output"
    if status in {"output-contract", "output-contract/quarantined-findings", "empty-response"}:
        return "malformed_output"
    if isinstance(err, UnsupportedCapability) or status == "unsupported":
        return "provider_lacks_tools"
    if isinstance(err, ProviderConfigError) or status == "config":
        reason_code = _route_reason_code(str(err))
        if reason_code in {"missing_native_review_extension", "provider_lacks_tools"}:
            return reason_code
        return "auth_missing"
    if isinstance(err, ProviderStalledError) or status == "stall":
        return "provider_stall"
    if status == "timeout":
        return "provider_timeout"
    if status == "rate-limit":
        return "rate_limited"
    if status in {"network", "transport", "5xx"}:
        return "provider_unreachable"
    if status == "review-context":
        return "review_context_error"
    return "unknown_provider_error"


def _review_exit_class_for_failure(failure_code: str | None) -> str:
    if failure_code == "malformed_output":
        return "malformed_output"
    return "infra_error"


def _review_verdict_from_text(text: str) -> str:
    for line in reversed(text.splitlines()):
        marker = line.strip()
        if marker == "CODEX_REVIEW_CLEAN":
            return "clean"
        if marker == "CODEX_REVIEW_FIXED":
            return "fixed"
        if marker == "CODEX_REVIEW_BLOCKED":
            return "blocked"
    return "unknown"


def _review_exit_class_for_verdict(verdict: str) -> str:
    if verdict == "blocked":
        return "findings"
    if verdict in {"clean", "fixed"}:
        return "clean"
    return "malformed_output"


def _review_candidate_model(
    provider_name: str,
    provider: object,
    models_by_provider: dict[str, tuple[str, ...]] | None,
) -> str | None:
    models = (models_by_provider or {}).get(provider_name)
    if models:
        return models[0]
    return _provider_default_model(provider)


def _review_response_with_metadata(
    response: CallResponse,
    *,
    statuses: list[ReviewProviderStatus],
    attempts: list[ReviewProviderAttempt],
) -> CallResponse:
    verdict = _review_verdict_from_text(response.text)
    review_payload: dict[str, object] = {
        "exit_class": _review_exit_class_for_verdict(verdict),
        "failure_code": None,
        "attempts": _review_attempt_payloads(attempts),
        "selected_provider": response.provider,
        "selected_model": response.model,
        "review_verdict": verdict,
    }
    raw: dict[str, object] = {
        **response.raw,
        "review_attempts": review_payload["attempts"],
        "conductor_review": review_payload,
    }
    if statuses:
        raw["review_provider_statuses"] = [asdict(status) for status in statuses]
    return replace(response, raw=raw)


def _review_attempt_log_state(status: str) -> str:
    availability_statuses = {
        "5xx",
        "config",
        "network",
        "rate-limit",
        "stall",
        "timeout",
        "transport",
    }
    return "unavailable" if status in availability_statuses else "failed"


def _large_review_input(decision: RouteDecision) -> bool:
    """Return whether review input is large enough to use scaled review budgets."""
    return (
        _review_gate_timeout_sec(decision.estimated_input_tokens)
        > REVIEW_GATE_MIN_TIMEOUT_SEC
    )


def _prioritize_openrouter_after_codex_review_failure(
    candidates: list[RankedCandidate],
    *,
    failed_index: int,
    decision: RouteDecision,
    err: Exception,
) -> None:
    """Move OpenRouter to the next fallback slot after large Codex review failures.

    Invariant: this only reorders candidates already admitted by routing and
    the caller's ``--max-fallbacks`` cap; it never expands the fallback set.
    """
    if (
        failed_index >= len(candidates)
        or candidates[failed_index].name != "codex"
        or not _large_review_input(decision)
    ):
        return

    retryable, category = _is_retryable(err)
    if not retryable:
        return
    if not isinstance(err, ReviewOutputContractError) and category != "timeout":
        return

    next_index = failed_index + 1
    for openrouter_index in range(next_index, len(candidates)):
        if candidates[openrouter_index].name == "openrouter":
            if openrouter_index != next_index:
                candidates.insert(next_index, candidates.pop(openrouter_index))
            return


def _validate_max_fallbacks(raw: int) -> int:
    if raw < 1:
        raise click.UsageError(f"--max-fallbacks must be >= 1, got {raw}")
    return raw


def _estimate_brief_tokens(task: str) -> int:
    # Pre-fallback context fitness check is approximate by design: ~4 chars per
    # token is the standard rough heuristic for English text and is sufficient
    # to spot-check "this brief obviously won't fit"; precise tokenization
    # would be per-provider and would couple this layer to provider details.
    return len(task) // 4


def _ollama_index(candidates: list) -> int | None:
    """Return the index of ollama in ``candidates`` (or None if absent)."""
    for i, c in enumerate(candidates):
        if c.name == "ollama":
            return i
    return None


def _reorder_ollama_first(candidates: list) -> bool:
    """Move ollama to the head of ``candidates``; return True if mutated."""
    idx = _ollama_index(candidates)
    if idx is None or idx == 0:
        return False
    candidates.insert(0, candidates.pop(idx))
    return True


def _stderr_is_tty() -> bool:
    """Best-effort check: are we talking to a human on stderr + stdin?

    click.confirm() prompts on stderr when ``err=True``. We also need
    stdin to be a TTY so the user can actually answer. Either one being
    non-interactive (pipes, CI, test harness) should skip the prompt.
    """
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _provider_for_preflight(provider_or_name):
    if hasattr(provider_or_name, "health_probe"):
        return provider_or_name
    return get_provider(str(provider_or_name))


def _run_exec_preflight(provider_or_name) -> tuple[bool, str | None]:
    provider = _provider_for_preflight(provider_or_name)
    return provider.health_probe()


def _review_provider_or_none(provider_or_name) -> NativeReviewProvider | None:
    provider = _provider_for_preflight(provider_or_name)
    if isinstance(provider, NativeReviewProvider):
        return provider
    return None


def _review_exclude_set(
    user_exclude: frozenset[str],
) -> tuple[frozenset[str], dict[str, str]]:
    """Return router excludes needed to keep review routing code-review-only."""
    excludes = set(user_exclude)
    reasons: dict[str, str] = {}
    for name in known_providers():
        if name in user_exclude:
            reasons[name] = "excluded by caller"
            continue
        try:
            provider = get_provider(name)
        except KeyError as e:
            excludes.add(name)
            reasons[name] = str(e)
            continue
        if NATIVE_REVIEW_TAG not in provider.tags:
            excludes.add(name)
            reasons[name] = "provider is not tagged code-review"
            continue
        ok, reason = (
            provider.review_configured()
            if isinstance(provider, NativeReviewProvider)
            else provider.configured()
        )
        if not ok:
            excludes.add(name)
            reasons[name] = reason or "code-review provider is not configured"
    return frozenset(excludes), reasons


def _native_review_unavailable_message(reasons: dict[str, str]) -> str:
    if not reasons:
        return "no code-review provider is available."
    lines = ["no code-review provider is available:"]
    for name in sorted(reasons):
        lines.append(f"  - {name}: {reasons[name]}")
    return "\n".join(lines)


def _review_excluded_payload(reasons: dict[str, str]) -> list[dict[str, object]]:
    return [
        {
            "provider": name,
            "reason_code": _route_reason_code(reason),
            "reason": reason,
        }
        for name, reason in sorted(reasons.items())
    ]


class _ReviewRouteSelectionError(RuntimeError):
    def __init__(
        self,
        cause: NoConfiguredProvider | InvalidRouterRequest | MutedProvidersError,
        review_reasons: dict[str, str],
    ) -> None:
        self.cause = cause
        self.review_reasons = review_reasons
        super().__init__(str(cause))


def _build_review_route_decision(
    *,
    effort: str | int,
    tags: tuple[str, ...],
    prefer: str | None,
    exclude: frozenset[str],
    estimated_input_tokens: int | None,
) -> tuple[RouteDecision, dict[str, str], SemanticPlan]:
    """Resolve the semantic review route used by review preflight and dispatch.

    Invariant: review route validation and real semantic review dispatch use
    one provider ordering path, so a no-token preflight cannot report a
    different selected provider than the gate will try first.
    """
    plan = _with_review_semantic_tags(plan_for("review", effort), tags)
    review_exclude, review_reasons = _review_exclude_set(exclude)
    exclude_set = _semantic_candidate_exclude_set(plan, review_exclude)
    try:
        _provider, decision = pick(
            list(plan.tags),
            prefer=prefer or plan.prefer,
            effort=effort,
            exclude=exclude_set,
            priority=_semantic_priority(plan),
            shadow=True,
            estimated_input_tokens=estimated_input_tokens,
        )
    except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
        raise _ReviewRouteSelectionError(e, review_reasons) from e
    return _apply_semantic_priority_to_decision(decision, plan), review_reasons, plan


def _review_no_viable_provider_error(
    review_reasons: dict[str, str],
    cause: Exception,
) -> ReviewInfrastructureError:
    message = f"{_native_review_unavailable_message(review_reasons)} {cause}"
    return ReviewInfrastructureError(
        message,
        exit_class="no_viable_provider",
        failure_code="no_viable_provider",
        attempts=[],
        review_verdict="unknown",
        excluded=_review_excluded_payload(review_reasons),
    )


def _emit_review_route_failure(
    review_reasons: dict[str, str],
    cause: Exception,
    *,
    as_json: bool,
) -> None:
    if as_json:
        _emit_provider_error(
            _review_no_viable_provider_error(review_reasons, cause),
            as_json=True,
        )
        return
    click.echo(f"conductor: {_native_review_unavailable_message(review_reasons)}", err=True)
    click.echo(f"conductor: {cause}", err=True)


def _semantic_candidate_exclude_set(
    plan: SemanticPlan,
    user_exclude: frozenset[str],
) -> frozenset[str]:
    allowed = {candidate.provider for candidate in plan.candidates}
    if not allowed:
        return user_exclude
    return frozenset(set(known_providers()) - allowed) | user_exclude


def _semantic_priority(plan: SemanticPlan) -> tuple[str, ...]:
    return tuple(candidate.provider for candidate in plan.candidates)


def _apply_semantic_priority_to_decision(
    decision: RouteDecision,
    plan: SemanticPlan,
) -> RouteDecision:
    """Make the semantic candidate order the dispatch order after router filters."""
    priority = {provider: idx for idx, provider in enumerate(_semantic_priority(plan))}
    ranked = tuple(
        sorted(decision.ranked, key=lambda candidate: priority.get(candidate.name, len(priority)))
    )
    if not ranked:
        return decision
    winner = ranked[0]
    return replace(
        decision,
        provider=winner.name,
        thinking_budget=winner.estimated_thinking_tokens,
        tier=winner.tier,
        matched_tags=winner.matched_tags,
        ranked=ranked,
        estimated_input_tokens=winner.estimated_input_tokens,
        estimated_output_tokens=winner.estimated_output_tokens,
        estimated_thinking_tokens=winner.estimated_thinking_tokens,
    )


def _requires_strong_code_provider(plan: SemanticPlan) -> bool:
    return plan.kind == "code" and plan.effort_bucket in {"high", "max"}


Candidate = SemanticCandidate | RankedCandidate
ExclusionPhase = Literal["planning", "runtime"]


@dataclass(frozen=True)
class PlanContext:
    semantic_plan: SemanticPlan | None = None
    user_tags: tuple[str, ...] = ()
    offline_requested: bool = False
    online_probe_reachable: bool | None = None
    provider: object | None = None
    brief_tokens: int | None = None
    fallback_index: int = 0


@dataclass(frozen=True)
class ExclusionRule:
    name: str
    predicate: Callable[[Candidate, PlanContext], bool]
    message_template: str
    structured_event: str
    when: ExclusionPhase
    event_data: Callable[[Candidate, PlanContext], dict[str, object]]


OLLAMA_ONLINE_EXCLUSION_MESSAGE = (
    "[conductor] excluding ollama from fallback chain "
    f"(online; ollama is offline-only — pass --offline or set {LOCAL_ONLINE_OPT_IN_ENV}=1)"
)
OLLAMA_PROBE_OFFLINE_INCLUSION_MESSAGE = (
    "[conductor] including ollama as local fallback "
    "(network probe found no reachable target; assuming offline)"
)


def _candidate_provider(candidate: Candidate) -> str:
    if isinstance(candidate, SemanticCandidate):
        return candidate.provider
    return candidate.name


def _format_exclusion_message(
    rule: ExclusionRule,
    candidate: Candidate,
    context: PlanContext,
) -> str:
    provider = _candidate_provider(candidate)
    max_ctx = getattr(context.provider, "max_context_tokens", None)
    return rule.message_template.format(
        name=provider,
        provider=provider,
        brief_tokens=context.brief_tokens,
        max_context_tokens=max_ctx,
    )


def _is_ollama_candidate(candidate: Candidate) -> bool:
    return _candidate_provider(candidate) == "ollama"


def _ollama_online_only_predicate(
    candidate: Candidate,
    context: PlanContext,
) -> bool:
    if not _is_ollama_candidate(candidate):
        return False
    if _local_provider_opt_in_active(offline_requested=context.offline_requested):
        return False
    return context.online_probe_reachable is True


def _code_high_requires_frontier_predicate(
    candidate: Candidate,
    context: PlanContext,
) -> bool:
    return (
        _is_ollama_candidate(candidate)
        and context.semantic_plan is not None
        and _requires_strong_code_provider(context.semantic_plan)
    )


def _context_fit_required_predicate(
    candidate: Candidate,
    context: PlanContext,
) -> bool:
    if context.fallback_index <= 0 or context.brief_tokens is None:
        return False
    max_ctx = getattr(context.provider, "max_context_tokens", None)
    return max_ctx is not None and context.brief_tokens > max_ctx


def _planning_exclusion_event_data(
    candidate: Candidate,
    context: PlanContext,
) -> dict[str, object]:
    data: dict[str, object] = {
        "provider": _candidate_provider(candidate),
        "phase": "planning",
    }
    if context.semantic_plan is not None:
        data.update(
            {
                "kind": context.semantic_plan.kind,
                "effort": context.semantic_plan.effort_bucket,
            }
        )
    return data


def _context_fit_event_data(
    candidate: Candidate,
    context: PlanContext,
) -> dict[str, object]:
    max_ctx = getattr(context.provider, "max_context_tokens", None)
    return {
        "provider": _candidate_provider(candidate),
        "reason": "brief_exceeds_context",
        "brief_tokens": context.brief_tokens,
        "max_context_tokens": max_ctx,
    }


OLLAMA_ONLINE_ONLY = ExclusionRule(
    name="ollama-online-only",
    predicate=_ollama_online_only_predicate,
    message_template=OLLAMA_ONLINE_EXCLUSION_MESSAGE,
    structured_event="planning_excluded",
    when="planning",
    event_data=_planning_exclusion_event_data,
)
CONTEXT_FIT_REQUIRED = ExclusionRule(
    name="context-fit-required",
    predicate=_context_fit_required_predicate,
    message_template=(
        "[conductor] skipping fallback {name}: "
        "brief ~{brief_tokens} tokens > model context {max_context_tokens}"
    ),
    structured_event="fallback_skipped",
    when="runtime",
    event_data=_context_fit_event_data,
)
CODE_HIGH_REQUIRES_FRONTIER = ExclusionRule(
    name="code-high-requires-frontier",
    predicate=_code_high_requires_frontier_predicate,
    message_template=OLLAMA_ONLINE_EXCLUSION_MESSAGE,
    structured_event="planning_excluded",
    when="planning",
    event_data=_planning_exclusion_event_data,
)
EXCLUSION_RULES: tuple[ExclusionRule, ...] = (
    OLLAMA_ONLINE_ONLY,
    CONTEXT_FIT_REQUIRED,
    CODE_HIGH_REQUIRES_FRONTIER,
)


def _exclusion_rules_for_phase(phase: ExclusionPhase) -> tuple[ExclusionRule, ...]:
    return tuple(rule for rule in EXCLUSION_RULES if rule.when == phase)


def _first_matching_exclusion_rule(
    candidate: Candidate,
    context: PlanContext,
    *,
    phase: ExclusionPhase,
) -> ExclusionRule | None:
    for rule in _exclusion_rules_for_phase(phase):
        if rule.predicate(candidate, context):
            return rule
    return None


def _with_user_semantic_tags(
    plan: SemanticPlan,
    user_tags: tuple[str, ...],
) -> SemanticPlan:
    if not user_tags:
        return plan
    tags = list(plan.tags)
    for tag in user_tags:
        if tag not in tags:
            tags.append(tag)
    return replace(plan, tags=tuple(tags))


def _with_review_semantic_tags(
    plan: SemanticPlan,
    user_tags: tuple[str, ...],
) -> SemanticPlan:
    plan = _with_user_semantic_tags(plan, user_tags)
    tags = tuple(tag for tag in plan.tags if tag not in REVIEW_IGNORED_TASK_TAGS)
    if NATIVE_REVIEW_TAG not in tags:
        tags = (NATIVE_REVIEW_TAG, *tags)
    return replace(plan, tags=tags)


def _semantic_plan_contains_ollama(plan: SemanticPlan) -> bool:
    return any(candidate.provider == "ollama" for candidate in plan.candidates)


def _first_online_candidate_provider(plan: SemanticPlan) -> str | None:
    for candidate in plan.candidates:
        if candidate.provider != "ollama":
            return candidate.provider
    return None


def _first_online_ranked_candidate_provider(
    candidates: tuple[RankedCandidate, ...],
) -> str | None:
    for candidate in candidates:
        if candidate.name != "ollama":
            return candidate.name
    return None


def _apply_ollama_offline_only_policy(
    plan: SemanticPlan,
    *,
    user_tags: tuple[str, ...],
    offline_requested: bool,
) -> tuple[SemanticPlan, str | None]:
    """Keep ollama in semantic fallback chains only for explicit offline intent.

    Invariant: an online semantic plan must not retain ollama as an implicit
    fallback. Local routing remains available through --offline, sticky offline
    mode, explicit ollama tags, and when the network probe cannot reach any
    target.
    """
    if not _semantic_plan_contains_ollama(plan):
        return plan, None
    if _local_provider_opt_in_active(offline_requested=offline_requested):
        return _apply_planning_exclusion_rules(
            plan,
            PlanContext(
                semantic_plan=plan,
                user_tags=user_tags,
                offline_requested=offline_requested,
                online_probe_reachable=False,
            ),
        )

    profile = get_network_profile(
        _network_target_for_provider(_first_online_candidate_provider(plan)),
        warn=None,
    )
    if profile.rtt_ms is None:
        plan, message = _apply_planning_exclusion_rules(
            plan,
            PlanContext(
                semantic_plan=plan,
                user_tags=user_tags,
                offline_requested=offline_requested,
                online_probe_reachable=False,
            ),
        )
        return plan, message or OLLAMA_PROBE_OFFLINE_INCLUSION_MESSAGE

    return _apply_planning_exclusion_rules(
        plan,
        PlanContext(
            semantic_plan=plan,
            user_tags=user_tags,
            offline_requested=offline_requested,
            online_probe_reachable=True,
        ),
    )


def _auto_route_plan_context(
    decision: RouteDecision,
    *,
    user_tags: tuple[str, ...],
    offline_requested: bool,
) -> tuple[PlanContext, str | None]:
    if not any(candidate.name == "ollama" for candidate in decision.ranked):
        return (
            PlanContext(
                user_tags=user_tags,
                offline_requested=offline_requested,
                online_probe_reachable=None,
            ),
            None,
        )
    if _local_provider_opt_in_active(offline_requested=offline_requested):
        return (
            PlanContext(
                user_tags=user_tags,
                offline_requested=offline_requested,
                online_probe_reachable=False,
            ),
            None,
        )

    profile = get_network_profile(
        _network_target_for_provider(_first_online_ranked_candidate_provider(decision.ranked)),
        warn=None,
    )
    if profile.rtt_ms is None:
        return (
            PlanContext(
                user_tags=user_tags,
                offline_requested=offline_requested,
                online_probe_reachable=False,
            ),
            OLLAMA_PROBE_OFFLINE_INCLUSION_MESSAGE,
        )
    return (
        PlanContext(
            user_tags=user_tags,
            offline_requested=offline_requested,
            online_probe_reachable=True,
        ),
        None,
    )


def _apply_auto_route_exclusion_rules(
    decision: RouteDecision,
    *,
    user_tags: tuple[str, ...],
    offline_requested: bool,
) -> tuple[RouteDecision, str | None]:
    context, inclusion_message = _auto_route_plan_context(
        decision,
        user_tags=user_tags,
        offline_requested=offline_requested,
    )
    messages: list[str] = []
    retained: list[RankedCandidate] = []
    skipped = list(decision.candidates_skipped)
    for candidate in decision.ranked:
        rule = _first_matching_exclusion_rule(
            candidate,
            context,
            phase="planning",
        )
        if rule is None:
            retained.append(candidate)
            continue
        message = _format_exclusion_message(rule, candidate, context)
        if message not in messages:
            messages.append(message)
        skipped.append((candidate.name, f"excluded by {rule.name}"))

    if len(retained) == len(decision.ranked):
        return decision, inclusion_message
    if not retained:
        detail = f"{'; '.join(messages)}. " if messages else ""
        raise NoConfiguredProvider(
            "no provider satisfies the routing request after planning exclusions. "
            f"{detail}Skipped: {skipped}"
        )

    winner = retained[0]
    return (
        replace(
            decision,
            provider=winner.name,
            thinking_budget=winner.estimated_thinking_tokens,
            tier=winner.tier,
            matched_tags=winner.matched_tags,
            ranked=tuple(retained),
            candidates_skipped=tuple(skipped),
            estimated_input_tokens=winner.estimated_input_tokens,
            estimated_output_tokens=winner.estimated_output_tokens,
            estimated_thinking_tokens=winner.estimated_thinking_tokens,
        ),
        "\n".join(messages),
    )


def _apply_planning_exclusion_rules(
    plan: SemanticPlan,
    context: PlanContext,
) -> tuple[SemanticPlan, str | None]:
    messages: list[str] = []
    retained: list[SemanticCandidate] = []
    for candidate in plan.candidates:
        rule = _first_matching_exclusion_rule(
            candidate,
            context,
            phase="planning",
        )
        if rule is None:
            retained.append(candidate)
            continue
        message = _format_exclusion_message(rule, candidate, context)
        if message not in messages:
            messages.append(message)

    candidates = tuple(retained)
    if len(candidates) == len(plan.candidates):
        return plan, None
    return replace(plan, candidates=candidates), "\n".join(messages)


def _format_strong_code_no_fallback_error(
    plan: SemanticPlan,
    provider: str,
    err: Exception,
) -> str:
    return (
        f"conductor: no usable fallback for --kind code --effort {plan.effort_bucket} "
        f"after primary {provider} failed ({err}).\n"
        "           Configure another frontier provider, or relax --effort to medium."
    )


def _format_semantic_plan_line(plan: SemanticPlan) -> str:
    stack = " > ".join(candidate.label() for candidate in plan.candidates)
    return (
        f"[conductor] ask kind={plan.kind} effort={plan.effort_bucket} "
        f"mode={plan.mode} stack={stack}"
    )


def _semantic_plan_payload(plan: SemanticPlan) -> dict[str, object]:
    return {
        "kind": plan.kind,
        "effort_bucket": plan.effort_bucket,
        "mode": plan.mode,
        "tags": list(plan.tags),
        "prefer": plan.prefer,
        "tools": sorted(plan.tools),
        "sandbox": plan.sandbox,
        "candidates": [
            {"provider": candidate.provider, "models": list(candidate.models)}
            for candidate in plan.candidates
        ],
        "council_member_models": list(plan.council_member_models),
        "council_synthesis_models": list(plan.council_synthesis_models),
    }


def _echo_preflight_failure(provider_or_name, reason: str | None) -> None:
    provider = _provider_for_preflight(provider_or_name)
    detail = reason or "provider health probe failed"
    click.echo(f"[conductor] preflight failed for {provider.name}: {detail}", err=True)
    fix = _provider_fix_command(provider, reason)
    if fix:
        click.echo(f"[conductor] try: {fix}", err=True)


def _provider_fix_command(provider, reason: str | None) -> str | None:
    fix_for_reason = getattr(provider, "fix_command_for_reason", None)
    if callable(fix_for_reason):
        return fix_for_reason(reason)
    return getattr(provider, "fix_command", None)


def _echo_offline_hint(failed_name: str, *, silent: bool) -> None:
    """Print a hint pointing at ollama when we couldn't prompt."""
    if silent:
        return
    click.echo(
        f"[conductor] {failed_name} is unreachable and no local fallback "
        "is available for automatic switching. If you are offline, run "
        "`conductor call --with ollama --brief '...'` (or pass --offline).",
        err=True,
    )


def _maybe_echo_explicit_network_hint(provider_id: str, err: Exception) -> None:
    """On a network-category failure in explicit (--with) mode, nudge local.

    The auto-mode path has its own prompt + sticky-flag dance. Explicit mode
    can't reroute silently (the user asked for this provider specifically),
    so the most helpful thing is a one-line suggestion. No-op when the user
    already picked ollama, or when the failure isn't network-shaped.
    """
    if provider_id == "ollama":
        return
    _, category = _is_retryable(err)
    if category != "network":
        return
    click.echo(
        f"[conductor] {provider_id} looks unreachable (network error). "
        "If you are offline: `conductor call --offline --brief '...'` "
        "or `conductor call --with ollama --brief '...'`.",
        err=True,
    )


def _maybe_switch_to_ollama(
    *,
    failed: str,
    candidates: list,
    cursor: int,
    silent: bool,
) -> bool | None:
    """Ask the user whether to skip ahead to ollama, then rewrite candidates.

    Returns:
      True  — user confirmed; ``candidates`` now has ollama at ``cursor + 1``
              and later remote candidates dropped. Sticky-flag setting is the
              caller's responsibility.
      False — user declined. ``candidates`` is unchanged; the normal cascade
              continues through whatever remote candidates are left.
      None  — we couldn't prompt (non-TTY, or ollama not in the remaining
              candidates, or ollama isn't actually reachable locally). Caller
              should treat this as "the offline fallback isn't wired up right
              now" and print a hint + re-raise.
    """
    remaining_idx = _ollama_index(candidates[cursor + 1 :])
    if remaining_idx is None:
        # Ollama isn't even in the ranking — nothing to offer.
        return None
    absolute_idx = cursor + 1 + remaining_idx

    ollama = get_provider("ollama")
    ok, reason = ollama.configured()
    if not ok:
        if not silent:
            click.echo(
                f"[conductor] {failed} is unreachable and ollama is not "
                f"running locally ({reason}). Start it with `ollama serve` "
                "or re-run with a different provider.",
                err=True,
            )
        return None

    if not _stderr_is_tty():
        return None

    default_model = _provider_default_model(ollama)
    click.echo("", err=True)
    click.echo(
        f"⚠ {failed} is unreachable — you appear to be offline.",
        err=True,
    )
    try:
        answer = click.confirm(
            f"  Fall back to local model ({default_model} via ollama)?",
            default=True,
            err=True,
        )
    except click.Abort:
        return False

    if not answer:
        # The user explicitly declined the local switch. Respect that —
        # drop ollama from the remaining ranking so a silent cascade
        # doesn't route through it anyway. The normal fallback chain
        # keeps trying any other remote candidates below, and re-raises
        # the original error if none are left.
        del candidates[absolute_idx]
        return False

    # Truncate: drop any remote candidates between the current cursor and
    # ollama, and drop anything after ollama too. The user opted for local,
    # so we don't want to keep trying other remotes if ollama itself fails.
    ollama_candidate = candidates[absolute_idx]
    del candidates[cursor + 1 :]
    candidates.append(ollama_candidate)
    return True


def _planning_excluded_ollama(decision: RouteDecision) -> bool:
    return any(
        name == "ollama" and reason.startswith("excluded by ollama-online-only")
        for name, reason in decision.candidates_skipped
    )


def _ollama_recovery_candidate(decision: RouteDecision) -> RankedCandidate:
    provider = get_provider("ollama")
    thinking_tokens = resolve_effort_tokens(
        decision.effort,
        provider.effort_to_thinking,
    )
    matched_tags = tuple(sorted(set(decision.task_tags) & set(provider.tags)))
    return RankedCandidate(
        name="ollama",
        tier=provider.quality_tier,
        tier_rank=TIER_RANK.get(provider.quality_tier, 0),
        matched_tags=matched_tags,
        tag_score=len(matched_tags),
        cost_score=0.0,
        latency_ms=provider.typical_p50_ms,
        health_penalty=0.0,
        combined_score=float("-inf"),
        estimated_input_tokens=decision.estimated_input_tokens,
        estimated_output_tokens=decision.estimated_output_tokens,
        estimated_thinking_tokens=thinking_tokens,
    )


def _invoke_with_fallback(
    decision: RouteDecision,
    *,
    mode: str,  # "call" | "exec"
    task: str,
    model: str | None,
    effort: str | int,
    tools: frozenset[str],
    sandbox: str,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    start_timeout_sec: float | None,
    silent: bool,
    retry_on_stall: int = 0,
    resume_session_id: str | None = None,
    session_log: SessionLog | None = None,
    models_by_provider: dict[str, tuple[str, ...]] | None = None,
    attachments: tuple[Path, ...] = (),
    max_iterations: int | None = None,
    max_iterations_explicit: bool = False,
    write_validation: bool = True,
    strict_stall: bool = False,
    allow_completion_stretch: bool = False,
    fallback_deadline_monotonic: float | None = None,
    fallback_budget_label: str = "fallback budget",
    cap_fallback_attempt_timeout_to_stall: bool = False,
) -> tuple[CallResponse, list[str]]:
    """Try the decision's ranked providers in order; fallback on retryable errors.

    Returns (response, fallbacks_used). fallbacks_used is the list of
    provider names attempted before the successful one (excluding the final).

    Raises the last ProviderError if every candidate fails.

    Offline-mode integration:
      - If ``offline_mode.is_active()`` and ollama is in the ranking, ollama
        is moved to the head of the list so we try local first.
      - On the first "network"-category failure we prompt (TTY only) to
        switch to ollama, truncating the remaining remote candidates on
        acceptance. Accepting also sets the sticky offline flag so subsequent
        invocations skip straight to local for the TTL window.
    """
    last_exc: Exception | None = None
    fallbacks: list[str] = []
    candidates = list(decision.ranked)

    if offline_mode.is_active():
        if _ollama_index(candidates) is None:
            # Offline mode promises local routing. If ollama is absent from
            # the ranking (excluded, unconfigured, or filtered out by
            # tools), silently cascading to a remote provider would
            # violate that promise — and the remote will almost certainly
            # fail with a network error anyway. Surface the contradiction
            # up front instead.
            raise ProviderConfigError(
                "offline mode is active but ollama is not in the routing "
                "candidates (excluded, not configured, or filtered out by "
                "--tools). Start ollama (`ollama serve`), relax the filters, "
                "or clear the flag with --no-offline."
            )
        _reorder_ollama_first(candidates)
        if not silent:
            remaining_m = max(1, (offline_mode.seconds_remaining() + 59) // 60)
            click.echo(
                f"[conductor] offline mode active (~{remaining_m}m left) · "
                "routing → ollama. Pass --no-offline to clear.",
                err=True,
            )

    prompted_offline = False
    brief_tokens = _estimate_brief_tokens(task)
    idx = 0
    while idx < len(candidates):
        candidate = candidates[idx]
        provider = get_provider(candidate.name)
        runtime_context = PlanContext(
            provider=provider,
            brief_tokens=brief_tokens,
            fallback_index=idx,
        )
        rule = _first_matching_exclusion_rule(
            candidate,
            runtime_context,
            phase="runtime",
        )
        if rule is not None:
            if not silent:
                click.echo(
                    _format_exclusion_message(rule, candidate, runtime_context),
                    err=True,
                )
            if session_log is not None:
                session_log.emit(
                    rule.structured_event,
                    rule.event_data(candidate, runtime_context),
                )
            fallbacks.append(candidate.name)
            idx += 1
            continue
        candidate_models = (models_by_provider or {}).get(candidate.name)
        if (
            mode == "exec"
            and max_iterations_explicit
            and not _provider_supports_exec_max_iterations(candidate.name)
        ):
            message = _exec_max_iterations_unsupported_message(candidate.name)
            if not silent:
                click.echo(f"[conductor] {candidate.name} skipped: {message}", err=True)
            if session_log is not None:
                session_log.emit(
                    "provider_skipped",
                    {"provider": candidate.name, "reason": message},
                )
            fallbacks.append(candidate.name)
            idx += 1
            continue
        if session_log is not None:
            session_log.bind_provider(candidate.name)
            session_log.emit(
                "provider_started",
                {
                    "provider": candidate.name,
                    "mode": mode,
                    "model": model,
                    "models": list(candidate_models or ()),
                    "tools": sorted(tools),
                    "sandbox": sandbox,
                    "cwd": cwd,
                    "resume_session_id": resume_session_id,
                },
            )
        attempt_timeout_sec, attempt_max_stall_sec = _bounded_attempt_budget(
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            deadline_monotonic=fallback_deadline_monotonic,
            budget_label=fallback_budget_label,
            provider_name=candidate.name,
        )
        if (
            cap_fallback_attempt_timeout_to_stall
            and idx > 0
            and attempt_max_stall_sec is not None
        ):
            attempt_timeout_sec = (
                attempt_max_stall_sec
                if attempt_timeout_sec is None
                else min(attempt_timeout_sec, attempt_max_stall_sec)
            )
            attempt_max_stall_sec = min(attempt_max_stall_sec, attempt_timeout_sec)
        try:
            if mode == "exec":
                if isinstance(provider, OpenRouterProvider):
                    response = provider.exec(
                        task,
                        model=model,
                        models=candidate_models,
                        effort=effort,
                        task_tags=list(decision.task_tags),
                        prefer=decision.prefer,
                        log_selection=not silent,
                        tools=tools,
                        sandbox=sandbox,
                        cwd=cwd,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        resume_session_id=resume_session_id,
                        session_log=session_log,
                        max_iterations=max_iterations,
                        allow_completion_stretch=allow_completion_stretch,
                        write_validation=write_validation,
                    )
                elif isinstance(provider, ClaudeProvider):
                    _ensure_supports_attachments(provider, attachments)
                    response = provider.exec(
                        task,
                        model=model,
                        effort=effort,
                        tools=tools,
                        sandbox=sandbox,
                        cwd=cwd,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        start_timeout_sec=start_timeout_sec,
                        resume_session_id=resume_session_id,
                        session_log=session_log,
                        retry_on_stall=retry_on_stall,
                    )
                elif isinstance(provider, CodexProvider):
                    response = provider.exec(
                        task,
                        model=model,
                        effort=effort,
                        tools=tools,
                        sandbox=sandbox,
                        cwd=cwd,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        resume_session_id=resume_session_id,
                        session_log=session_log,
                        attachments=attachments,
                        strict_stall=strict_stall,
                        max_iterations=max_iterations,
                    )
                elif isinstance(provider, OllamaProvider):
                    _ensure_supports_attachments(provider, attachments)
                    response = provider.exec(
                        task,
                        model=model,
                        effort=effort,
                        tools=tools,
                        sandbox=sandbox,
                        cwd=cwd,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        resume_session_id=resume_session_id,
                        session_log=session_log,
                        max_iterations=max_iterations,
                        allow_completion_stretch=allow_completion_stretch,
                        write_validation=write_validation,
                    )
                else:
                    _ensure_supports_attachments(provider, attachments)
                    response = provider.exec(
                        task,
                        model=model,
                        effort=effort,
                        tools=tools,
                        sandbox=sandbox,
                        cwd=cwd,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        resume_session_id=resume_session_id,
                        session_log=session_log,
                    )
            else:
                if isinstance(provider, OpenRouterProvider):
                    _ensure_supports_attachments(provider, attachments)
                    response = provider.call(
                        task,
                        model=model,
                        models=candidate_models,
                        effort=effort,
                        task_tags=list(decision.task_tags),
                        prefer=decision.prefer,
                        log_selection=not silent,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        resume_session_id=resume_session_id,
                    )
                elif isinstance(provider, CodexProvider):
                    response = provider.call(
                        task,
                        model=model,
                        effort=effort,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        resume_session_id=resume_session_id,
                        attachments=attachments,
                    )
                else:
                    _ensure_supports_attachments(provider, attachments)
                    response = provider.call(
                        task,
                        model=model,
                        effort=effort,
                        timeout_sec=attempt_timeout_sec,
                        max_stall_sec=attempt_max_stall_sec,
                        resume_session_id=resume_session_id,
                    )
            mark_outcome(candidate.name, "success")
            if session_log is not None:
                session_log.set_session_id(response.session_id)
                session_log.emit(
                    "provider_finished",
                    {
                        "provider": response.provider,
                        "model": response.model,
                        "duration_ms": response.duration_ms,
                        "session_id": response.session_id,
                    },
                )
            return response, fallbacks
        except ProviderConfigError:
            # Config problems don't recover with a different provider using
            # the same config. Re-raise immediately.
            raise
        except UnsupportedCapability:
            # Router filter should prevent this; if it leaks through, skip.
            fallbacks.append(candidate.name)
            idx += 1
            continue
        except ProviderError as e:
            retryable, category = _is_retryable(e)
            if category == "rate-limit":
                mark_rate_limited(candidate.name)
            mark_outcome(candidate.name, category)
            last_exc = e
            if session_log is not None:
                failure_event: dict[str, object] = {
                    "provider": candidate.name,
                    "category": category,
                    "error_class": e.__class__.__name__,
                    "error": str(e),
                }
                if isinstance(e, ProviderExecutionError):
                    failure_event["execution_status"] = e.status
                session_log.emit(
                    "provider_failed",
                    failure_event,
                )
            if not retryable:
                raise
            fallbacks.append(candidate.name)

            # First real connectivity failure in this invocation: prompt
            # (or use the sticky flag) to switch to ollama instead of
            # spraying timeouts across every remote in the ranking.
            if category == "network" and not prompted_offline:
                prompted_offline = True
                if _planning_excluded_ollama(decision) and _ollama_index(candidates) is None:
                    candidates.append(_ollama_recovery_candidate(decision))
                decision_flag = _maybe_switch_to_ollama(
                    failed=candidate.name,
                    candidates=candidates,
                    cursor=idx,
                    silent=silent,
                )
                if decision_flag is None:
                    # No fallback is actionable (ollama absent / not running /
                    # non-TTY). Don't silently cascade through more remotes
                    # that will also fail — surface the hint and re-raise.
                    _echo_offline_hint(candidate.name, silent=silent)
                    raise
                if decision_flag:
                    offline_mode.set_active()
                # If False (user declined), fall through to the normal
                # cascade — maybe it was a blip and claude works.

            if idx + 1 < len(candidates):
                next_name = candidates[idx + 1].name
                if not silent:
                    click.echo(
                        f"[conductor] {candidate.name} failed ({category}) · "
                        f"{_format_fallback_error_detail(e)} · "
                        f"falling through to {next_name} (falling back → {next_name})",
                        err=True,
                    )
            idx += 1
            continue

    # Exhausted every candidate; re-raise the last error for user visibility.
    assert last_exc is not None  # at least one attempt must have happened
    raise last_exc


def _invoke_review_with_fallback(
    decision: RouteDecision,
    *,
    task: str,
    effort: str | int,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None,
    silent: bool,
    max_fallbacks: int = DEFAULT_REVIEW_MAX_FALLBACKS,
    models_by_provider: dict[str, tuple[str, ...]] | None = None,
    fallback_deadline_monotonic: float | None = None,
) -> tuple[CallResponse, list[str]]:
    """Try code-review providers in route order."""
    last_exc: Exception | None = None
    fallbacks: list[str] = []
    tried: list[tuple[str, str]] = []
    statuses: list[ReviewProviderStatus] = []
    attempts: list[ReviewProviderAttempt] = []
    quarantined_contract_errors: list[ReviewOutputContractError] = []
    ranked_candidates = list(decision.ranked)
    candidates = ranked_candidates[:max_fallbacks]
    skipped_by_max_fallbacks = [
        candidate.name for candidate in ranked_candidates[max_fallbacks:]
    ]

    for idx, candidate in enumerate(candidates):
        provider = get_provider(candidate.name)
        attempt_model = _review_candidate_model(
            candidate.name,
            provider,
            models_by_provider,
        )
        attempt_route_mode = _route_provider_mode("review", provider)
        attempt_started_at = time.monotonic()
        contract_prompt = task
        try:
            attempt_timeout_sec, attempt_max_stall_sec = _bounded_review_attempt_budget(
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                deadline_monotonic=fallback_deadline_monotonic,
                provider_name=candidate.name,
                is_fallback=idx > 0,
                remaining_fallback_count=max(0, len(candidates) - idx - 1),
            )
            if isinstance(provider, NativeReviewProvider):
                response = provider.review(
                    task,
                    effort=effort,
                    cwd=cwd,
                    timeout_sec=attempt_timeout_sec,
                    max_stall_sec=attempt_max_stall_sec,
                    base=base,
                    commit=commit,
                    uncommitted=uncommitted,
                    title=title,
                )
            elif isinstance(provider, OpenRouterProvider):
                contract_prompt = build_review_task_prompt(
                    task,
                    base=base,
                    commit=commit,
                    uncommitted=uncommitted,
                    title=title,
                    cwd=cwd,
                    include_patch=True,
                )
                response = provider.call(
                    contract_prompt,
                    models=(models_by_provider or {}).get(candidate.name),
                    effort=effort,
                    task_tags=list(decision.task_tags),
                    prefer=decision.prefer,
                    log_selection=not silent,
                    timeout_sec=attempt_timeout_sec,
                    max_stall_sec=attempt_max_stall_sec,
                )
            else:
                contract_prompt = build_review_task_prompt(
                    task,
                    base=base,
                    commit=commit,
                    uncommitted=uncommitted,
                    title=title,
                    cwd=cwd,
                    include_patch=True,
                )
                response = provider.call(
                    contract_prompt,
                    effort=effort,
                    timeout_sec=attempt_timeout_sec,
                    max_stall_sec=attempt_max_stall_sec,
                )
            validated_text = validate_requested_review_sentinel(
                provider_name=response.provider,
                prompt=contract_prompt,
                text=response.text,
            )
            if validated_text != response.text:
                response = replace(response, text=validated_text)
            if quarantined_contract_errors:
                details = _format_quarantined_review_outputs(quarantined_contract_errors)
                raise ProviderError(
                    "code review found possible findings in malformed provider "
                    "output before a later provider returned a valid verdict. "
                    "The malformed output was not accepted as a review result; "
                    f"quarantined possible findings: {details}."
                )
            mark_outcome(candidate.name, "success")
            tried.append((candidate.name, "success"))
            attempts.append(
                ReviewProviderAttempt(
                    provider=candidate.name,
                    model=response.model or attempt_model,
                    status="success",
                    failure_code=None,
                    elapsed_ms=response.duration_ms,
                    route_mode=attempt_route_mode,
                )
            )
            response = _review_response_with_metadata(
                response,
                statuses=statuses,
                attempts=attempts,
            )
            if not silent and len(tried) > 1:
                click.echo(
                    f"[conductor] review tried providers: {_format_tried_providers(tried)}",
                    err=True,
                )
            return response, fallbacks
        except ProviderConfigError as e:
            last_exc = e
            mark_outcome(candidate.name, "config")
            fallbacks.append(candidate.name)
            tried.append((candidate.name, "config"))
            detail = _format_fallback_error_detail(e)
            statuses.append(
                ReviewProviderStatus(
                    candidate.name,
                    "config",
                    detail,
                )
            )
            attempts.append(
                ReviewProviderAttempt(
                    provider=candidate.name,
                    model=attempt_model,
                    status="config",
                    failure_code=_review_failure_code(e, "config"),
                    elapsed_ms=_council_elapsed_ms(attempt_started_at),
                    detail=detail,
                    route_mode=attempt_route_mode,
                )
            )
        except UnsupportedCapability as e:
            last_exc = e
            fallbacks.append(candidate.name)
            tried.append((candidate.name, "unsupported"))
            detail = _format_fallback_error_detail(e)
            statuses.append(
                ReviewProviderStatus(
                    candidate.name,
                    "unsupported",
                    detail,
                )
            )
            attempts.append(
                ReviewProviderAttempt(
                    provider=candidate.name,
                    model=attempt_model,
                    status="unsupported",
                    failure_code=_review_failure_code(e, "unsupported"),
                    elapsed_ms=_council_elapsed_ms(attempt_started_at),
                    detail=detail,
                    route_mode=attempt_route_mode,
                )
            )
        except ProviderError as e:
            retryable, category = _is_retryable(e)
            if isinstance(e, ReviewOutputContractError) and e.possible_findings:
                quarantined_contract_errors.append(e)
            if category == "rate-limit":
                mark_rate_limited(candidate.name)
            mark_outcome(candidate.name, category)
            last_exc = e
            if not retryable:
                raise
            fallbacks.append(candidate.name)
            mode = _review_failure_mode(e)
            tried.append((candidate.name, mode))
            detail = _format_fallback_error_detail(e)
            statuses.append(
                ReviewProviderStatus(
                    candidate.name,
                    mode,
                    detail,
                )
            )
            attempts.append(
                ReviewProviderAttempt(
                    provider=candidate.name,
                    model=attempt_model,
                    status=mode,
                    failure_code=_review_failure_code(e, mode),
                    elapsed_ms=_council_elapsed_ms(attempt_started_at),
                    detail=detail,
                    route_mode=attempt_route_mode,
                )
            )
            _prioritize_openrouter_after_codex_review_failure(
                candidates,
                failed_index=idx,
                decision=decision,
                err=e,
            )
        except ReviewContextError as e:
            last_exc = e
            mark_outcome(candidate.name, "review-context")
            fallbacks.append(candidate.name)
            tried.append((candidate.name, "review-context"))
            detail = _format_fallback_error_detail(e)
            statuses.append(
                ReviewProviderStatus(
                    candidate.name,
                    "review-context",
                    detail,
                )
            )
            attempts.append(
                ReviewProviderAttempt(
                    provider=candidate.name,
                    model=attempt_model,
                    status="review-context",
                    failure_code=_review_failure_code(e, "review-context"),
                    elapsed_ms=_council_elapsed_ms(attempt_started_at),
                    detail=detail,
                    route_mode=attempt_route_mode,
                )
            )

        if idx + 1 < len(candidates) and not silent:
            detail = (
                _format_fallback_error_detail(last_exc)
                if last_exc is not None
                else "unknown error"
            )
            click.echo(
                f"[conductor] {candidate.name} review "
                f"{_review_attempt_log_state(tried[-1][1])} ({tried[-1][1]}) · "
                f"{detail} · "
                f"falling back → {candidates[idx + 1].name}",
                err=True,
            )

    assert last_exc is not None
    if tried:
        trail = _format_tried_providers(tried)
        last_detail = _format_fallback_error_detail(last_exc)
        status_summary = _format_review_status_summary(statuses)
        quarantine = (
            " Quarantined possible findings: "
            f"{_format_quarantined_review_outputs(quarantined_contract_errors)}."
            if quarantined_contract_errors
            else ""
        )
        skipped_note = _format_max_fallbacks_skipped(
            skipped_by_max_fallbacks,
            max_fallbacks=max_fallbacks,
        )
        skipped_sentence = f" {skipped_note}" if skipped_note else ""
        direct_work = (
            "Conductor did not complete a review; continue the coding/review "
            "task directly in the driving agent, and do not treat this "
            "Conductor output as clean or complete"
        )
        operator_hint = (
            "Then inspect provider status, increase --max-fallbacks, exclude "
            "failing providers, or run `conductor list` to check configured "
            "code-review providers"
        )
        failure_payloads = _review_attempt_payloads(attempts)
        failure_code = (
            str(failure_payloads[-1]["failure_code"]) if failure_payloads else None
        )
        raise ReviewInfrastructureError(
            (
                f"review infrastructure failed before any provider returned a "
                f"valid verdict; no review findings were emitted. Tried providers: "
                f"{trail}. Provider status: {status_summary}. "
                f"Last error: {last_detail}.{quarantine}{skipped_sentence} "
                f"Next step: {direct_work}. "
                f"Operator follow-up: {operator_hint}."
            ),
            exit_class=_review_exit_class_for_failure(failure_code),
            failure_code=failure_code,
            attempts=failure_payloads,
            skipped_by_max_fallbacks=skipped_by_max_fallbacks,
        ) from last_exc
    raise last_exc


class CouncilCapError(ProviderError):
    """Raised when council caps stop fan-out before a complete synthesis."""

    def __init__(self, response: CallResponse) -> None:
        self.response = response
        council_raw = (response.raw or {}).get("conductor_council") or {}
        super().__init__(_format_council_cap_hit(council_raw.get("cap_hit") or {}))


def _invoke_council(
    plan: SemanticPlan,
    *,
    task: str,
    effort: str | int,
    timeout_sec: int | None,
    caps: CouncilCaps,
    rounds: int,
    silent: bool,
) -> CallResponse:
    """Run a deterministic OpenRouter council request.

    Council is intentionally OpenRouter-only: it fans out to the policy's
    member model stack, then asks a synthesis model to reconcile disagreements.
    Caps are checked before starting each upstream call so a known-over-budget
    council cannot silently continue spending time, tokens, or money.
    """
    if plan.candidates[0].provider != "openrouter":
        raise ProviderConfigError(
            "council policy invariant violated: council must route through openrouter."
        )
    if not plan.council_member_models:
        raise ProviderConfigError("council policy invariant violated: no member models configured.")

    provider = get_provider("openrouter")
    if not isinstance(provider, OpenRouterProvider):
        raise ProviderConfigError(
            "provider registry invariant violated: openrouter is not OpenRouterProvider."
        )

    started_at = time.monotonic()
    parent_delegation_id = DelegationEvent().delegation_id
    member_responses: list[CallResponse] = []
    member_events: list[dict] = []
    member_prompt = _council_member_prompt(task, rounds=rounds)
    synthesis_models = plan.council_synthesis_models or plan.council_member_models[:1]

    for idx, model in enumerate(plan.council_member_models, start=1):
        elapsed_ms = _council_elapsed_ms(started_at)
        _raise_if_council_cap_hit(
            plan=plan,
            caps=caps,
            effort=effort,
            rounds=rounds,
            member_responses=member_responses,
            synthesis=None,
            synthesis_models=synthesis_models,
            stage="before_member",
            model=model,
            elapsed_ms=elapsed_ms,
        )
        if not silent:
            click.echo(f"[conductor] council member {idx}: {model}", err=True)
        try:
            response = _openrouter_council_provider(
                provider=provider,
                timeout_sec=timeout_sec,
                remaining_sec=_council_remaining_sec(caps, elapsed_ms),
            ).call(
                member_prompt,
                model=model,
                effort=effort,
                task_tags=list(plan.tags),
                prefer=plan.prefer,
                log_selection=False,
                max_tokens=_council_remaining_output_tokens(caps, member_responses),
            )
        except ProviderError as exc:
            response = _council_member_failure_response(
                model=model,
                error=exc,
                elapsed_ms=_council_elapsed_ms(started_at),
            )
            if not silent:
                click.echo(
                    f"[conductor] council member {idx} failed: {type(exc).__name__}: {exc} "
                    "- continuing",
                    err=True,
                )
        member_responses.append(response)
        member_delegation_id = _record_response_delegation(
            "council",
            response,
            effort=effort,
            semantic_plan=plan,
            parent_delegation_id=parent_delegation_id,
            council_role="member",
        )
        member_events.append(
            {
                "delegation_id": member_delegation_id,
                "provider": response.provider,
                "model": response.model,
            }
        )
        _raise_if_council_cap_hit(
            plan=plan,
            caps=caps,
            effort=effort,
            rounds=rounds,
            member_responses=member_responses,
            synthesis=None,
            synthesis_models=synthesis_models,
            stage="after_member",
            model=model,
            elapsed_ms=_council_elapsed_ms(started_at),
        )

    if not any(_council_member_succeeded(response) for response in member_responses):
        errors = "; ".join(
            str((response.raw or {}).get("conductor_council_member_error") or response.model)
            for response in member_responses
        )
        raise ProviderError(f"council failed: all member calls failed ({errors})")

    elapsed_ms = _council_elapsed_ms(started_at)
    _raise_if_council_cap_hit(
        plan=plan,
        caps=caps,
        effort=effort,
        rounds=rounds,
        member_responses=member_responses,
        synthesis=None,
        synthesis_models=synthesis_models,
        stage="before_synthesis",
        model=",".join(synthesis_models),
        elapsed_ms=elapsed_ms,
    )
    if not silent:
        click.echo(
            "[conductor] council synthesis: " + ",".join(synthesis_models),
            err=True,
        )
    synthesis = _openrouter_council_provider(
        provider=provider,
        timeout_sec=timeout_sec,
        remaining_sec=_council_remaining_sec(caps, elapsed_ms),
    ).call(
        _council_synthesis_prompt(task, member_responses),
        models=synthesis_models,
        effort=effort,
        task_tags=list(plan.tags),
        prefer=plan.prefer,
        log_selection=False,
        max_tokens=_council_remaining_output_tokens(caps, member_responses),
    )
    synthesis_delegation_id = _record_response_delegation(
        "council",
        synthesis,
        effort=effort,
        semantic_plan=plan,
        parent_delegation_id=parent_delegation_id,
        council_role="synthesis",
    )

    raw, usage, cost_usd = _council_response_metadata(
        plan=plan,
        caps=caps,
        effort=effort,
        rounds=rounds,
        member_responses=member_responses,
        synthesis=synthesis,
        synthesis_models=synthesis_models,
        elapsed_ms=_council_elapsed_ms(started_at),
        cap_hit=None,
    )
    parent_response = replace(
        synthesis,
        usage=usage,
        cost_usd=cost_usd,
        raw={**(synthesis.raw or {}), **raw},
    )
    _record_response_delegation(
        "council",
        parent_response,
        effort=effort,
        semantic_plan=plan,
        delegation_id=parent_delegation_id,
        council_role="parent",
        members=member_events,
        synthesis_delegation_id=synthesis_delegation_id,
    )
    return parent_response


def _council_member_failure_response(
    *,
    model: str,
    error: ProviderError,
    elapsed_ms: int,
) -> CallResponse:
    error_payload = {
        "model": model,
        "type": type(error).__name__,
        "message": str(error),
    }
    return CallResponse(
        text=f"[council member failed: {error_payload['type']}: {error_payload['message']}]",
        provider="openrouter",
        model=model,
        duration_ms=elapsed_ms,
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "thinking_tokens": 0,
            "cached_tokens": 0,
        },
        cost_usd=None,
        raw={"conductor_council_member_error": error_payload},
    )


def _council_member_succeeded(response: CallResponse) -> bool:
    return "conductor_council_member_error" not in (response.raw or {})


def _openrouter_council_provider(
    *,
    provider: OpenRouterProvider,
    timeout_sec: int | None,
    remaining_sec: float | None,
) -> OpenRouterProvider:
    timeout_limits: list[float] = []
    if timeout_sec is not None:
        timeout_limits.append(float(timeout_sec))
    if remaining_sec is not None:
        timeout_limits.append(remaining_sec)
    if not timeout_limits:
        return provider
    return OpenRouterProvider(timeout_sec=max(0.001, min(timeout_limits)))


def _council_elapsed_ms(started_at: float) -> int:
    return int(max(0.0, time.monotonic() - started_at) * 1000)


def _council_remaining_sec(caps: CouncilCaps, elapsed_ms: int) -> float | None:
    if caps.timeout_sec is None:
        return None
    return max(0.0, float(caps.timeout_sec) - (elapsed_ms / 1000))


def _council_remaining_output_tokens(
    caps: CouncilCaps,
    member_responses: list[CallResponse],
) -> int | None:
    if caps.max_output_tokens is None:
        return None
    known_output_tokens, output_complete, _missing, _member_tokens, _synthesis_tokens = (
        _council_output_token_accounting(member_responses, None)
    )
    if not output_complete:
        return None
    return max(1, caps.max_output_tokens - known_output_tokens)


def _raise_if_council_cap_hit(
    *,
    plan: SemanticPlan,
    caps: CouncilCaps,
    effort: str | int,
    rounds: int,
    member_responses: list[CallResponse],
    synthesis: CallResponse | None,
    synthesis_models: tuple[str, ...],
    stage: str,
    model: str,
    elapsed_ms: int,
) -> None:
    cap_hit = _council_cap_hit_payload(
        plan=plan,
        caps=caps,
        member_responses=member_responses,
        synthesis=synthesis,
        stage=stage,
        model=model,
        elapsed_ms=elapsed_ms,
    )
    if cap_hit is None:
        return

    raw, usage, cost_usd = _council_response_metadata(
        plan=plan,
        caps=caps,
        effort=effort,
        rounds=rounds,
        member_responses=member_responses,
        synthesis=synthesis,
        synthesis_models=synthesis_models,
        elapsed_ms=elapsed_ms,
        cap_hit=cap_hit,
    )
    response = CallResponse(
        text=_council_partial_text(member_responses, cap_hit),
        provider="openrouter",
        model=_council_partial_model(member_responses, model),
        duration_ms=elapsed_ms,
        usage=usage,
        cost_usd=cost_usd,
        raw=raw,
    )
    raise CouncilCapError(response)


def _council_cap_hit_payload(
    *,
    plan: SemanticPlan,
    caps: CouncilCaps,
    member_responses: list[CallResponse],
    synthesis: CallResponse | None,
    stage: str,
    model: str,
    elapsed_ms: int,
) -> dict[str, object] | None:
    if caps.timeout_sec is not None and elapsed_ms >= caps.timeout_sec * 1000:
        return _council_cap_payload(
            "wall_clock",
            limit=caps.timeout_sec,
            observed=elapsed_ms / 1000,
            plan=plan,
            member_responses=member_responses,
            stage=stage,
            model=model,
            elapsed_ms=elapsed_ms,
        )

    if caps.max_output_tokens is not None:
        known_output_tokens, output_complete, _missing, _member_tokens, _synthesis_tokens = (
            _council_output_token_accounting(member_responses, synthesis)
        )
        if not output_complete:
            return _council_cap_payload(
                "output_tokens_unknown",
                limit=caps.max_output_tokens,
                observed=known_output_tokens,
                plan=plan,
                member_responses=member_responses,
                stage=stage,
                model=model,
                elapsed_ms=elapsed_ms,
            )
        if known_output_tokens >= caps.max_output_tokens:
            return _council_cap_payload(
                "output_tokens",
                limit=caps.max_output_tokens,
                observed=known_output_tokens,
                plan=plan,
                member_responses=member_responses,
                stage=stage,
                model=model,
                elapsed_ms=elapsed_ms,
            )

    if caps.max_cost_usd is not None:
        known_cost_usd = _council_known_cost_value(member_responses, synthesis)
        if known_cost_usd >= caps.max_cost_usd:
            return _council_cap_payload(
                "known_cost_usd",
                limit=caps.max_cost_usd,
                observed=known_cost_usd,
                plan=plan,
                member_responses=member_responses,
                stage=stage,
                model=model,
                elapsed_ms=elapsed_ms,
            )
    return None


def _council_cap_payload(
    kind: str,
    *,
    limit: int | float,
    observed: int | float,
    plan: SemanticPlan,
    member_responses: list[CallResponse],
    stage: str,
    model: str,
    elapsed_ms: int,
) -> dict[str, object]:
    completed_member_calls = len(member_responses)
    return {
        "kind": kind,
        "limit": limit,
        "observed": observed,
        "stage": stage,
        "model": model,
        "elapsed_ms": elapsed_ms,
        "elapsed_sec": elapsed_ms / 1000,
        "completed_member_calls": completed_member_calls,
        "total_member_calls": len(plan.council_member_models),
        "completed_member_models": [response.model for response in member_responses],
        "requested_completed_member_models": list(
            plan.council_member_models[:completed_member_calls]
        ),
        "skipped_member_models": list(plan.council_member_models[completed_member_calls:]),
        "synthesis_skipped": stage != "after_synthesis",
    }


def _council_response_metadata(
    *,
    plan: SemanticPlan,
    caps: CouncilCaps,
    effort: str | int,
    rounds: int,
    member_responses: list[CallResponse],
    synthesis: CallResponse | None,
    synthesis_models: tuple[str, ...],
    elapsed_ms: int,
    cap_hit: dict[str, object] | None,
) -> tuple[dict[str, object], dict[str, object], float | None]:
    (
        known_cost_usd,
        cost_accounting_complete,
        missing_costs,
        member_costs,
        synthesis_cost_usd,
    ) = _council_cost_accounting(member_responses, synthesis)
    (
        known_output_tokens,
        output_accounting_complete,
        missing_output_tokens,
        member_output_tokens,
        synthesis_output_tokens,
    ) = _council_output_token_accounting(member_responses, synthesis)
    council_raw = {
        "member_models": [response.model for response in member_responses],
        "requested_member_models": list(plan.council_member_models),
        "requested_synthesis_models": list(synthesis_models),
        "member_errors": [
            (response.raw or {}).get("conductor_council_member_error")
            for response in member_responses
            if (response.raw or {}).get("conductor_council_member_error") is not None
        ],
        "rounds": rounds,
        "member_usage": [response.usage for response in member_responses],
        "member_cost_usd": member_costs,
        "synthesis_cost_usd": synthesis_cost_usd,
        "known_cost_usd": known_cost_usd,
        "cost_accounting_complete": cost_accounting_complete,
        "missing_costs": missing_costs,
        "member_duration_ms": [response.duration_ms for response in member_responses],
        "elapsed_ms": elapsed_ms,
        "caps": _council_caps_payload(caps),
        "cap_hit": cap_hit,
        "complete": cap_hit is None and synthesis is not None,
        "known_output_tokens": known_output_tokens,
        "output_accounting_complete": output_accounting_complete,
        "missing_output_tokens": missing_output_tokens,
        "member_output_tokens": member_output_tokens,
        "synthesis_output_tokens": synthesis_output_tokens,
    }
    usage = _council_usage_payload(
        effort=effort,
        rounds=rounds,
        member_responses=member_responses,
        synthesis=synthesis,
        cap_hit=cap_hit,
        known_cost_usd=known_cost_usd,
        cost_accounting_complete=cost_accounting_complete,
        missing_costs=missing_costs,
        known_output_tokens=known_output_tokens,
        output_accounting_complete=output_accounting_complete,
        missing_output_tokens=missing_output_tokens,
        caps=caps,
    )
    cost_usd = known_cost_usd if cost_accounting_complete else None
    return {"conductor_council": council_raw}, usage, cost_usd


def _council_usage_payload(
    *,
    effort: str | int,
    rounds: int,
    member_responses: list[CallResponse],
    synthesis: CallResponse | None,
    cap_hit: dict[str, object] | None,
    known_cost_usd: float | None,
    cost_accounting_complete: bool,
    missing_costs: list[str],
    known_output_tokens: int,
    output_accounting_complete: bool,
    missing_output_tokens: list[str],
    caps: CouncilCaps,
) -> dict[str, object]:
    if synthesis is not None:
        usage: dict[str, object] = dict(synthesis.usage)
    else:
        usage = {
            "input_tokens": _council_known_usage_sum(member_responses, "input_tokens"),
            "output_tokens": (known_output_tokens if output_accounting_complete else None),
            "cached_tokens": _council_known_usage_sum(member_responses, "cached_tokens"),
            "thinking_tokens": _council_known_usage_sum(
                member_responses,
                "thinking_tokens",
            ),
            "effort": effort if isinstance(effort, str) else None,
            "thinking_budget": None,
        }
    usage.update(
        {
            "council_members": len(member_responses),
            "council_failed_members": sum(
                1 for response in member_responses if not _council_member_succeeded(response)
            ),
            "council_rounds": rounds,
            "council_complete": cap_hit is None and synthesis is not None,
            "council_cap_hit": cap_hit,
            "council_caps": _council_caps_payload(caps),
            "cost_accounting_complete": cost_accounting_complete,
            "known_cost_usd": known_cost_usd,
            "missing_costs": missing_costs,
            "council_known_output_tokens": known_output_tokens,
            "council_output_accounting_complete": output_accounting_complete,
            "council_missing_output_tokens": missing_output_tokens,
        }
    )
    return usage


def _council_cost_accounting(
    member_responses: list[CallResponse],
    synthesis: CallResponse | None,
) -> tuple[float | None, bool, list[str], list[float | None], float | None]:
    member_costs = [response.cost_usd for response in member_responses]
    missing_costs = [
        f"member[{idx}]" for idx, cost in enumerate(member_costs, start=1) if cost is None
    ]
    synthesis_cost = synthesis.cost_usd if synthesis is not None else None
    if synthesis is not None and synthesis_cost is None:
        missing_costs.append("synthesis")
    known_costs = [cost for cost in [*member_costs, synthesis_cost] if cost is not None]
    known_cost_usd = sum(known_costs) if known_costs else None
    return (
        known_cost_usd,
        not missing_costs,
        missing_costs,
        member_costs,
        synthesis_cost,
    )


def _council_known_cost_value(
    member_responses: list[CallResponse],
    synthesis: CallResponse | None,
) -> float:
    known_cost_usd, _complete, _missing, _member_costs, _synthesis_cost = _council_cost_accounting(
        member_responses, synthesis
    )
    return known_cost_usd or 0.0


def _council_output_token_accounting(
    member_responses: list[CallResponse],
    synthesis: CallResponse | None,
) -> tuple[int, bool, list[str], list[int | None], int | None]:
    known_output_tokens = 0
    missing_output_tokens: list[str] = []
    member_output_tokens: list[int | None] = []
    for idx, response in enumerate(member_responses, start=1):
        output_tokens = _response_output_tokens(response)
        member_output_tokens.append(output_tokens)
        if output_tokens is None:
            missing_output_tokens.append(f"member[{idx}]")
        else:
            known_output_tokens += output_tokens

    synthesis_output_tokens = None
    if synthesis is not None:
        synthesis_output_tokens = _response_output_tokens(synthesis)
        if synthesis_output_tokens is None:
            missing_output_tokens.append("synthesis")
        else:
            known_output_tokens += synthesis_output_tokens

    return (
        known_output_tokens,
        not missing_output_tokens,
        missing_output_tokens,
        member_output_tokens,
        synthesis_output_tokens,
    )


def _response_output_tokens(response: CallResponse) -> int | None:
    output_tokens = (response.usage or {}).get("output_tokens")
    if isinstance(output_tokens, int) and not isinstance(output_tokens, bool):
        return max(0, output_tokens)
    return None


def _council_known_usage_sum(
    responses: list[CallResponse],
    key: str,
) -> int | None:
    values: list[int] = []
    for response in responses:
        value = (response.usage or {}).get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            values.append(value)
    return sum(values) if values else None


def _council_caps_payload(caps: CouncilCaps) -> dict[str, int | float | None]:
    return {
        "timeout_sec": caps.timeout_sec,
        "max_output_tokens": caps.max_output_tokens,
        "max_cost_usd": caps.max_cost_usd,
    }


def _council_partial_model(member_responses: list[CallResponse], model: str) -> str:
    if member_responses:
        return member_responses[-1].model
    return model or "council-partial"


def _council_partial_text(
    member_responses: list[CallResponse],
    cap_hit: dict[str, object],
) -> str:
    lines = [
        _format_council_cap_hit(cap_hit) + ".",
        (
            "Completed member calls: "
            f"{cap_hit.get('completed_member_calls', 0)}/"
            f"{cap_hit.get('total_member_calls', 0)}."
        ),
    ]
    if not member_responses:
        lines.append("No member calls completed before the cap was reached.")
        return "\n".join(lines)

    lines.append("Partial member responses:")
    for idx, response in enumerate(member_responses, start=1):
        lines.append(
            f"\n## Member {idx}: {response.model}\n\n{_council_member_response_text(response)}"
        )
    return "\n".join(lines)


def _format_council_cap_hit(cap_hit: dict[str, object]) -> str:
    kind = str(cap_hit.get("kind", "unknown"))
    labels = {
        "wall_clock": "wall-clock cap",
        "output_tokens": "output-token cap",
        "output_tokens_unknown": "output-token accounting cap",
        "known_cost_usd": "known cost cap",
    }
    label = labels.get(kind, "cap")
    elapsed_raw = cap_hit.get("elapsed_sec")
    elapsed_sec = (
        float(elapsed_raw)
        if isinstance(elapsed_raw, int | float) and not isinstance(elapsed_raw, bool)
        else 0.0
    )
    stage = str(cap_hit.get("stage") or "council")
    model = str(cap_hit.get("model") or "unknown model")
    completed_raw = cap_hit.get("completed_member_calls")
    total_raw = cap_hit.get("total_member_calls")
    completed = (
        int(completed_raw)
        if isinstance(completed_raw, int | float) and not isinstance(completed_raw, bool)
        else 0
    )
    total = (
        int(total_raw)
        if isinstance(total_raw, int | float) and not isinstance(total_raw, bool)
        else 0
    )
    return (
        f"council {label} hit at {elapsed_sec:.1f}s during {stage} ({model}); "
        f"completed {completed}/{total} member calls"
    )


def _council_member_prompt(task: str, *, rounds: int) -> str:
    return (
        "You are one member of a multi-model council. Give an independent, "
        "critical answer to the request below. Name assumptions, risks, and "
        "where another strong model might disagree. Do not claim consensus.\n\n"
        f"Council rounds requested: {rounds}\n\n"
        "Request:\n"
        f"{task}"
    )


def _council_synthesis_prompt(task: str, member_responses: list[CallResponse]) -> str:
    sections = []
    for i, response in enumerate(member_responses, start=1):
        sections.append(
            f"## Member {i}: {response.model}\n\n{_council_member_response_text(response)}"
        )
    return (
        "You are synthesizing a multi-model council. Compare the independent "
        "responses, call out meaningful disagreements, resolve them when the "
        "evidence supports it, and give the final answer. Preserve uncertainty "
        "instead of flattening it into false consensus.\n\n"
        "Original request:\n"
        f"{task}\n\n"
        "Council member responses:\n\n" + "\n\n".join(sections)
    )


def _council_member_response_text(response: CallResponse) -> str:
    text = getattr(response, "text", None)
    if text is None:
        return "[empty response]"
    if not isinstance(text, str):
        text = str(text)
    stripped = text.strip()
    return stripped if stripped else "[empty response]"


def _emit_call(
    response: CallResponse,
    *,
    as_json: bool,
    decision: RouteDecision | None = None,
    semantic_plan: SemanticPlan | None = None,
    auth_prompts: list[dict] | None = None,
) -> None:
    if as_json:
        payload = asdict(response)
        effective_auth_prompts = auth_prompts or response.auth_prompts
        if effective_auth_prompts:
            payload["auth_prompts"] = effective_auth_prompts
        else:
            payload.pop("auth_prompts", None)
        review_payload = (response.raw or {}).get("conductor_review")
        if isinstance(review_payload, dict):
            payload["review"] = review_payload
            for key in (
                "exit_class",
                "failure_code",
                "attempts",
                "selected_provider",
                "selected_model",
                "review_verdict",
            ):
                payload[key] = review_payload.get(key)
        if decision is not None:
            payload["route"] = asdict(decision)
        if semantic_plan is not None:
            payload["semantic"] = _semantic_plan_payload(semantic_plan)
        click.echo(json.dumps(payload, default=str, indent=2))
    else:
        click.echo(response.text)


def _record_response_delegation(
    command: str,
    response: CallResponse,
    *,
    effort: str | int | None,
    decision: RouteDecision | None = None,
    semantic_plan: SemanticPlan | None = None,
    session_log: SessionLog | None = None,
    delegation_id: str | None = None,
    parent_delegation_id: str | None = None,
    council_role: str | None = None,
    members: list[dict] | None = None,
    synthesis_delegation_id: str | None = None,
    fallback_chain: list[str] | None = None,
) -> str:
    event = _delegation_event_from_response(
        command,
        response,
        effort=effort,
        decision=decision,
        semantic_plan=semantic_plan,
        session_log=session_log,
        delegation_id=delegation_id,
        parent_delegation_id=parent_delegation_id,
        council_role=council_role,
        members=members,
        synthesis_delegation_id=synthesis_delegation_id,
        fallback_chain=fallback_chain,
    )
    record_delegation(event)
    return event.delegation_id


def _delegation_event_from_response(
    command: str,
    response: CallResponse,
    *,
    effort: str | int | None,
    decision: RouteDecision | None = None,
    semantic_plan: SemanticPlan | None = None,
    session_log: SessionLog | None = None,
    delegation_id: str | None = None,
    parent_delegation_id: str | None = None,
    council_role: str | None = None,
    members: list[dict] | None = None,
    synthesis_delegation_id: str | None = None,
    fallback_chain: list[str] | None = None,
) -> DelegationEvent:
    usage = response.usage or {}
    route_payload, semantic_payload = _internal_delegation_metadata(
        decision=decision,
        semantic_plan=semantic_plan,
    )
    return DelegationEvent(
        delegation_id=delegation_id or DelegationEvent().delegation_id,
        command=command,
        provider=response.provider,
        model=response.model,
        effort=effort if isinstance(effort, str) else None,
        duration_ms=response.duration_ms,
        status="ok",
        error=None,
        input_tokens=_usage_int_or_none(usage.get("input_tokens")),
        output_tokens=_usage_int_or_none(usage.get("output_tokens")),
        thinking_tokens=_usage_int_or_none(usage.get("thinking_tokens")),
        cached_tokens=_usage_int_or_none(usage.get("cached_tokens")),
        cost_usd=response.cost_usd,
        tags=_delegation_tags(decision=decision, semantic_plan=semantic_plan),
        session_log_path=str(session_log.log_path) if session_log is not None else None,
        parent_delegation_id=parent_delegation_id,
        council_role=council_role,  # type: ignore[arg-type]
        members=members,
        synthesis_delegation_id=synthesis_delegation_id,
        route=route_payload,
        semantic=semantic_payload,
        fallback_chain=fallback_chain if fallback_chain else None,
    )


def _record_failed_delegation(
    command: str,
    *,
    provider_id: str | None,
    model: str | None,
    effort: str | int | None,
    started_at: float,
    error: Exception | str,
    decision: RouteDecision | None = None,
    semantic_plan: SemanticPlan | None = None,
    session_log: SessionLog | None = None,
) -> None:
    resolved_provider = provider_id or (decision.provider if decision is not None else None)
    resolved_model = model or _provider_default_model_by_id(resolved_provider)
    route_payload, semantic_payload = _internal_delegation_metadata(
        decision=decision,
        semantic_plan=semantic_plan,
    )
    record_delegation(
        DelegationEvent(
            command=command,
            provider=resolved_provider,
            model=resolved_model,
            effort=effort if isinstance(effort, str) else None,
            duration_ms=_council_elapsed_ms(started_at),
            status=_delegation_status_from_error(error),
            error=str(error),
            tags=_delegation_tags(decision=decision, semantic_plan=semantic_plan),
            session_log_path=(str(session_log.log_path) if session_log is not None else None),
            route=route_payload,
            semantic=semantic_payload,
        )
    )


_INTERNAL_CONFIG_WARNING_EMITTED = False


def _internal_delegation_metadata(
    *,
    decision: RouteDecision | None,
    semantic_plan: SemanticPlan | None,
) -> tuple[dict | None, dict | None]:
    if not _internal_route_capture_enabled():
        return None, None
    return (
        asdict(decision) if decision is not None else None,
        _semantic_plan_payload(semantic_plan) if semantic_plan is not None else None,
    )


def _internal_route_capture_enabled() -> bool:
    global _INTERNAL_CONFIG_WARNING_EMITTED
    try:
        return internal_telemetry_enabled()
    except InternalConfigError as e:
        if not _INTERNAL_CONFIG_WARNING_EMITTED:
            click.echo(f"[conductor] internal telemetry disabled: {e}", err=True)
            _INTERNAL_CONFIG_WARNING_EMITTED = True
        return False


def _delegation_tags(
    *,
    decision: RouteDecision | None,
    semantic_plan: SemanticPlan | None,
) -> list[str]:
    if semantic_plan is not None:
        return list(semantic_plan.tags)
    if decision is not None:
        return list(decision.task_tags)
    return []


def _delegation_status_from_error(error: Exception | str) -> DelegationStatus:
    if isinstance(error, ProviderStalledError):
        text = str(error).lower()
        if "timed out" in text or "timeout" in text:
            return "timeout"
        return "stalled"
    text = str(error).lower()
    if any(marker in text for marker in ("quota", "rate limit", "rate-limit", "429")):
        return "quota"
    if "stalled" in text:
        return "stalled"
    if "timed out" in text or "timeout" in text:
        return "timeout"
    return "error"


def _usage_int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _provider_default_model_by_id(provider_id: str | None) -> str | None:
    if provider_id is None:
        return None
    try:
        return _provider_default_model(get_provider(provider_id))
    except (KeyError, ProviderError, AttributeError):
        return None


def _emit_provider_error(err: ProviderError, *, as_json: bool) -> None:
    payload = getattr(err, "error_response", None)
    if as_json and isinstance(payload, dict):
        click.echo(json.dumps(payload, default=str, indent=2))
        return
    click.echo(f"conductor: {err}", err=True)


def _collect_session_auth_prompts(session_log: SessionLog | None) -> list[dict] | None:
    if session_log is None:
        return None
    try:
        lines = session_log.log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None

    prompts: list[dict] = []
    seen: set[tuple[str, str | None]] = set()
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("event") != "auth_prompt":
            continue
        data = event.get("data") or {}
        provider = data.get("provider")
        if not isinstance(provider, str) or not provider:
            continue
        key = (provider, data.get("url"))
        if key in seen:
            continue
        seen.add(key)
        prompts.append(data)
    return prompts or None


def _git_stdout(cwd: str, args: list[str], *, errors: list[str]) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_RECOVERY_COMMAND_TIMEOUT_SEC,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        errors.append(f"`git {' '.join(args)}` failed: {e}")
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _git_stall_recovery_lines(cwd: str | None) -> list[str]:
    if not cwd:
        return []

    errors: list[str] = []
    root = _git_stdout(cwd, ["rev-parse", "--show-toplevel"], errors=errors)
    if not root:
        if errors:
            return [f"git recovery unavailable: {errors[0]}"]
        return []

    lines = [f"repo: {root}"]

    branch = _git_stdout(cwd, ["branch", "--show-current"], errors=errors)
    if not branch:
        short_head = _git_stdout(cwd, ["rev-parse", "--short", "HEAD"], errors=errors)
        branch = f"detached at {short_head}" if short_head else "unknown"
    lines.append(f"branch: {branch}")

    upstream = _git_stdout(
        cwd,
        ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        errors=errors,
    )
    if upstream:
        lines.append(f"upstream: {upstream}")
        commits = _git_stdout(
            cwd,
            ["log", "--oneline", f"{upstream}..HEAD", "--"],
            errors=errors,
        )
        commit_lines = commits.splitlines() if commits else []
        if commit_lines:
            first = commit_lines[0]
            suffix = "" if len(commit_lines) == 1 else f"; latest: {first}"
            lines.append(f"unpushed commits: {len(commit_lines)}{suffix}")
        else:
            lines.append("unpushed commits: none")
    else:
        lines.append("upstream: none configured")
        commits = _git_stdout(
            cwd,
            ["log", "--oneline", f"-{GIT_RECOVERY_MAX_COMMITS}", "--"],
            errors=errors,
        )
        if commits:
            first = commits.splitlines()[0]
            lines.append(f"recent local commit: {first}")

    status = _git_stdout(cwd, ["status", "--porcelain"], errors=errors)
    if status:
        changed = status.splitlines()
        lines.append(f"working tree: {len(changed)} changed path(s)")
        for entry in changed[:GIT_RECOVERY_MAX_STATUS_PATHS]:
            lines.append(f"changed: {entry}")
        if len(changed) > GIT_RECOVERY_MAX_STATUS_PATHS:
            hidden = len(changed) - GIT_RECOVERY_MAX_STATUS_PATHS
            lines.append(f"changed: ... {hidden} more")
    elif status == "":
        lines.append("working tree: clean")

    open_pr_script = Path(root) / "scripts" / "open-pr.sh"
    if open_pr_script.exists():
        lines.append(
            "hint: inspect the diff, then run `bash scripts/open-pr.sh --auto-merge` "
            "if the branch is ready"
        )
    else:
        lines.append("hint: inspect with `git status`, then resume or finish the branch")
    return lines


def _maybe_echo_stall_recovery_hint(err: ProviderError, *, cwd: str | None) -> None:
    if not isinstance(err, ProviderStalledError):
        return
    lines = _git_stall_recovery_lines(cwd)
    if not lines:
        return
    click.echo("", err=True)
    click.echo("Recoverable git state:", err=True)
    for line in lines:
        click.echo(f"  - {line}", err=True)


def _format_route_log_line(decision: RouteDecision) -> str:
    """Single-line route summary for stderr observability."""
    tags_matched = ",".join(decision.matched_tags) or "none"
    effort_str = decision.effort if isinstance(decision.effort, str) else f"{decision.effort}tok"
    return (
        f"[conductor] {decision.prefer} (effort={effort_str}) → {decision.provider} "
        f"(tier: {decision.tier} · matched: {tags_matched} · "
        f"est: {decision.estimated_input_tokens:,} in/"
        f"{decision.estimated_output_tokens:,} out)"
    )


def _format_usage_line(response: CallResponse) -> str:
    """Token + cost summary for stderr observability."""
    usage = response.usage or {}
    tok_in = usage.get("input_tokens")
    tok_out = usage.get("output_tokens")
    tok_think = usage.get("thinking_tokens")

    parts = [f"{response.duration_ms / 1000:.1f}s"]
    if tok_in:
        parts.append(f"{tok_in:,} tok in")
    if tok_think:
        parts.append(f"{tok_think:,} tok thinking")
    if tok_out:
        parts.append(f"{tok_out:,} tok out")
    if response.cost_usd is not None:
        parts.append(f"${response.cost_usd:.4f}")
    return "[conductor] " + " · ".join(parts)


def _format_route_ranking(decision: RouteDecision) -> list[str]:
    """Verbose ranking table for --verbose-route."""
    lines = [f"[conductor] route decision (prefer={decision.prefer}, effort={decision.effort}):"]
    if decision.tag_default_considered:
        for tag, provider, status in decision.tag_default_considered:
            picked_note = ""
            if decision.provider == provider and status == "applied":
                picked_note = ", picked"
            lines.append(f"[conductor] tag_default: {tag} → {provider} ({status}{picked_note})")
    for i, c in enumerate(decision.ranked, start=1):
        marker = " ← picked" if i == 1 else ""
        tags = ",".join(c.matched_tags) or "none"
        lines.append(
            f"  {i}. {c.name:<8} "
            f"(tier={c.tier}[{c.tier_rank}] "
            f"tags=+{c.tag_score}:{tags} "
            f"cost≈${c.cost_score:.4f} "
            f"tokens≈{c.estimated_input_tokens:,}in/"
            f"{c.estimated_output_tokens:,}out/"
            f"{c.estimated_thinking_tokens:,}think "
            f"p50={c.latency_ms}ms"
            f"){marker}"
        )
    shadow_names = {c.name for c in decision.unconfigured_shadow}
    for c in decision.unconfigured_shadow:
        tags = ",".join(c.matched_tags) or "none"
        lines.append(
            f"  ?  {c.name:<8} "
            f"(tier={c.tier}[{c.tier_rank}] "
            f"tags=+{c.tag_score}:{tags} "
            f"cost≈${c.cost_score:.4f} "
            f"tokens≈{c.estimated_input_tokens:,}in/"
            f"{c.estimated_output_tokens:,}out/"
            f"{c.estimated_thinking_tokens:,}think "
            f"p50={c.latency_ms}ms"
            f") ← would rank if installed: {c.unconfigured_reason}"
        )
    # Don't duplicate unconfigured providers in the skipped list — they
    # already appear (with scores) in the shadow block above. Other skip
    # reasons (excluded, missing tools, health) still show.
    for name, reason in decision.candidates_skipped:
        if name in shadow_names:
            continue
        lines.append(f"  —  {name:<8} (skipped: {reason})")
    return lines


def _format_shadow_hint(decision: RouteDecision) -> str | None:
    """Return a stderr advisory if an unconfigured provider outranks the winner.

    Returns None when the unconfigured-shadow ranking is empty (no provider
    we couldn't actually call would have been preferable) or when the top
    shadow candidate's score isn't strictly higher than the picked provider's.
    Equal scores resolve in favor of the configured provider — there's no
    reason to nag the user about a tie.

    The advisory exists because auto-mode falling back silently to the only
    configured provider hides the cost of missing integrations. Surfacing
    this at call-time turns "I didn't know codex wasn't installed" into
    "I see codex would be a better fit; here's how to install it."
    """
    if not decision.unconfigured_shadow or not decision.ranked:
        return None
    top_shadow = decision.unconfigured_shadow[0]
    winner = decision.ranked[0]
    if top_shadow.combined_score <= winner.combined_score:
        return None
    reason = top_shadow.unconfigured_reason or "not configured"
    return (
        f"[conductor] heads-up: `{top_shadow.name}` would rank above "
        f"`{winner.name}` if configured — {reason} "
        f"(run `conductor list` for the fix)"
    )


def _emit_route_log(
    decision: RouteDecision,
    *,
    verbose: bool,
    silent: bool,
) -> None:
    if silent:
        return
    if verbose:
        for line in _format_route_ranking(decision):
            click.echo(line, err=True)
    else:
        click.echo(_format_route_log_line(decision), err=True)
    hint = _format_shadow_hint(decision)
    if hint is not None:
        click.echo(hint, err=True)


def _emit_usage_log(response: CallResponse, *, silent: bool) -> None:
    if silent:
        return
    click.echo(_format_usage_line(response), err=True)


def _start_exec_session_log(
    *,
    log_file: str | None,
    resume_session_id: str | None,
) -> SessionLog:
    try:
        return SessionLog(
            path=Path(log_file).expanduser() if log_file else None,
            session_id=resume_session_id,
        )
    except SessionLogError as e:
        raise click.ClickException(str(e)) from e


def _emit_session_route_decision(
    session_log: SessionLog | None,
    decision: RouteDecision,
) -> None:
    if session_log is None:
        return
    session_log.emit(
        "route_decision",
        {
            "provider": decision.provider,
            "prefer": decision.prefer,
            "effort": decision.effort,
            "thinking_budget": decision.thinking_budget,
            "task_tags": list(decision.task_tags),
            "matched_tags": list(decision.matched_tags),
            "tools_requested": list(decision.tools_requested),
            "sandbox": decision.sandbox,
            "estimated_input_tokens": decision.estimated_input_tokens,
            "estimated_output_tokens": decision.estimated_output_tokens,
            "estimated_thinking_tokens": decision.estimated_thinking_tokens,
            "tag_default_applied": decision.tag_default_applied,
            "tag_default_considered": [
                {"tag": tag, "provider": provider, "status": status}
                for tag, provider, status in decision.tag_default_considered
            ],
            "ranked": [asdict(candidate) for candidate in decision.ranked],
        },
    )


def _emit_session_usage(
    session_log: SessionLog | None,
    response: CallResponse,
) -> None:
    if session_log is None:
        return
    session_log.emit(
        "usage",
        {
            "provider": response.provider,
            "model": response.model,
            "session_id": response.session_id,
            "usage": response.usage,
            "cost_usd": response.cost_usd,
            "duration_ms": response.duration_ms,
        },
    )


def _emit_grounding_warnings(text: str, worktree: str) -> None:
    try:
        from conductor.grounding import format_grounding_warning, ground_citations

        report = ground_citations(text, worktree)
        warning = format_grounding_warning(report)
        if warning:
            click.echo(warning, err=True)
    except Exception as e:  # noqa: BLE001 - guardrail must not change exec outcome.
        click.echo(f"[conductor] grounding check error: {e}", err=True)


def _tail_record(record: SessionRecord) -> None:
    offset = 0
    current_path = record.log_path
    current_status = record.status
    while True:
        if current_path.exists():
            with current_path.open("r", encoding="utf-8") as fh:
                fh.seek(offset)
                chunk = fh.read()
                if chunk:
                    click.echo(chunk, nl=False)
                offset = fh.tell()
        if current_status != "running":
            return
        time.sleep(0.1)
        refreshed = find_session_record(record.session_id) or find_session_record(record.run_id)
        if refreshed is None:
            return
        current_path = refreshed.log_path
        current_status = refreshed.status


def _openrouter_catalog_or_exit() -> openrouter_catalog.CatalogSnapshot:
    try:
        snapshot = openrouter_catalog.read_cached_catalog()
    except ProviderHTTPError as e:
        raise click.ClickException(str(e)) from e
    if snapshot is None:
        raise click.ClickException(
            "OpenRouter catalog cache not found. Run `conductor models refresh` first."
        )
    return snapshot


def _model_capabilities(model: openrouter_catalog.ModelEntry) -> str:
    caps = []
    if model.supports_thinking:
        caps.append("thinking")
    if model.supports_tools:
        caps.append("tools")
    if model.supports_vision:
        caps.append("vision")
    return ",".join(caps) or "-"


def _advisory_emission_allowed() -> bool:
    """Return True when interactive coaching messages may be emitted on stderr.

    The agent-wiring freshness notice is human-coaching, not a hard error.
    Programmatic consumers (CliRunner tests, `conductor call --json | jq`,
    Touchstone, scripts piping to other tools) must not see it on stderr —
    parsing pipelines that capture both streams will choke, and the JSON
    consumer contract guarantees strict stderr silence on success.

    Suppress in two cases:
    - stderr is not a TTY → caller is programmatic / piped / non-interactive.
    - `--json` is in argv → caller is the JSON consumer contract, even when
      they kept stderr attached to a terminal (the contract is silence on
      success, not silence-when-redirected).
    """
    if not bool(getattr(sys.stderr, "isatty", lambda: False)()):
        return False
    return "--json" not in sys.argv


def _maybe_emit_agent_wiring_notice(ctx: click.Context) -> None:
    if ctx.invoked_subcommand in {None, "init"}:
        return
    if os.environ.get("CONDUCTOR_AGENT_WIRING_NOTICE") == "0":
        return
    if not _advisory_emission_allowed():
        return

    try:
        from conductor.agent_wiring import (
            agent_wiring_notice,
            should_emit_agent_wiring_notice,
        )

        notice = agent_wiring_notice(
            current_version=__version__,
            include_missing=True,
        )
        if notice is None:
            return
        key, message = notice
        if should_emit_agent_wiring_notice(key):
            click.echo(message, err=True)
    except Exception:
        # Advisory wiring freshness checks must never break the command the
        # user actually asked conductor to run.
        return


AUTO_REFRESH_COMMANDS = frozenset(
    {
        "ask",
        "call",
        "doctor",
        "exec",
        "init",
        "refresh-consumers",
        "route",
        "update-all",
    }
)
REPO_SCOPE_AUTO_REFRESH_ENV = "CONDUCTOR_AUTO_REFRESH_REPO_SCOPE"
AUTO_REFRESH_VIA_PR_ENV = "CONDUCTOR_AUTO_REFRESH_VIA_PR"
AUTO_REFRESH_VIA_PR_MODES = frozenset({"auto", "always", "never"})


def _env_flag_enabled(name: str) -> bool:
    value = os.environ.get(name)
    if value is None:
        return False
    return value.strip().lower() not in {"", "0", "false", "no", "off"}


def _auto_refresh_via_pr_mode() -> tuple[str, str | None]:
    raw = os.environ.get(AUTO_REFRESH_VIA_PR_ENV, "auto").strip().lower()
    if raw in AUTO_REFRESH_VIA_PR_MODES:
        return raw, None
    return (
        "auto",
        f"{AUTO_REFRESH_VIA_PR_ENV}={raw!r} is invalid; expected auto, always, or never",
    )


def _repo_scope_auto_refresh_uses_branch(cwd: Path, mode: str) -> tuple[bool, str | None]:
    if mode == "never":
        return False, None
    if mode == "always":
        return True, None
    try:
        current = git_state.current_branch(cwd=cwd)
        default = git_state.default_branch(cwd=cwd)
    except GitStateError as e:
        return False, f"could not determine default branch; refreshing in place: {e}"
    return current is not None and current == default, None


def _maybe_auto_refresh(ctx: click.Context) -> None:
    command = ctx.invoked_subcommand
    if command not in AUTO_REFRESH_COMMANDS:
        return
    if any(arg in {"--help", "-h", "--version"} for arg in sys.argv[1:]):
        return
    if _env_flag_enabled("CONDUCTOR_NO_AUTO_REFRESH"):
        return

    debug = _env_flag_enabled("CONDUCTOR_DEBUG_AUTO_REFRESH")
    try:
        from conductor import agent_wiring

        decisions = agent_wiring.user_scope_version_decisions(binary_version=__version__)
        if debug:
            for decision in decisions:
                version = decision.version or "-"
                click.echo(
                    "[conductor] auto-refresh scan: "
                    f"{decision.kind} {decision.path} version={version} "
                    f"stale={decision.stale} reason={decision.reason}",
                    err=True,
                )
        if any(decision.stale for decision in decisions):
            agent_wiring.wire_claude_code(__version__, patch_claude_md=True)
            click.echo(
                "[conductor] refreshed user-scope integration files "
                f"to v{__version__.split('+', 1)[0]}",
                err=True,
            )
    except Exception as e:
        click.echo(
            f"[conductor] auto-refresh warning: failed to refresh "
            f"user-scope integration files: {e}",
            err=True,
        )
    if not _env_flag_enabled(REPO_SCOPE_AUTO_REFRESH_ENV):
        return
    try:
        from conductor import agent_wiring

        cwd = Path.cwd()
        repo_check = _run_repo_command(cwd, ["git", "rev-parse", "--is-inside-work-tree"])
        if repo_check.returncode != 0:
            return
        repo_decisions = agent_wiring.repo_scope_version_decisions(
            cwd,
            binary_version=__version__,
        )
        if debug:
            for repo_decision in repo_decisions:
                version = repo_decision.version or "-"
                click.echo(
                    "[conductor] auto-refresh scan: "
                    f"{repo_decision.kind} {repo_decision.path} version={version} "
                    f"stale={repo_decision.stale} reason={repo_decision.reason}",
                    err=True,
                )
        if not any(repo_decision.stale for repo_decision in repo_decisions):
            return
        mode, mode_warning = _auto_refresh_via_pr_mode()
        if mode_warning:
            click.echo(f"[conductor] auto-refresh warning: {mode_warning}", err=True)
        via_branch, branch_warning = _repo_scope_auto_refresh_uses_branch(cwd, mode)
        if branch_warning:
            click.echo(f"[conductor] auto-refresh warning: {branch_warning}", err=True)
        if via_branch:
            version = __version__.split("+", 1)[0]
            branch = f"chore/conductor-refresh-v{version}"
            result = _refresh_current_repo_scope_on_branch(
                cwd,
                branch=branch,
                version=__version__,
            )
            if result.status in {"failed", "needs-attention", "skipped"}:
                click.echo(
                    "[conductor] auto-refresh warning: "
                    f"{result.status}: {result.detail}",
                    err=True,
                )
                return
            if result.status == "committed":
                click.echo(
                    "[conductor] auto-refreshed repo-scope integration files "
                    f"on {branch} ({result.detail})",
                    err=True,
                )
            elif result.status == "unchanged":
                click.echo(
                    "[conductor] repo-scope integration refresh branch "
                    f"{branch} is already current",
                    err=True,
                )
            return
        report = agent_wiring.refresh_repo_scope(cwd, version=__version__)
        for path, reason in report.skipped:
            click.echo(
                f"[conductor] auto-refresh warning: skipped {path}: {reason}",
                err=True,
            )
        if report.refreshed:
            click.echo(
                "[conductor] refreshed repo-scope integration files "
                f"in {cwd} to v{__version__.split('+', 1)[0]}",
                err=True,
            )
    except Exception as e:
        click.echo(
            f"[conductor] auto-refresh warning: failed to refresh "
            f"repo-scope integration files: {e}",
            err=True,
        )


@click.group()
@click.version_option(__version__, prog_name="conductor")
@click.pass_context
def main(ctx: click.Context) -> None:
    """Pick an LLM, give it a job."""
    _maybe_auto_refresh(ctx)
    _maybe_emit_agent_wiring_notice(ctx)


# --------------------------------------------------------------------------- #
# ask — semantic intent API
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--kind",
    required=True,
    type=click.Choice(SEMANTIC_KINDS),
    help=(
        "Semantic work category. research/code are cheap single-model defaults; "
        "council is capped OpenRouter multi-model fan-out."
    ),
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option(
    "--tags",
    default=None,
    help="Comma-separated task tags to add to the semantic route.",
)
@click.option("--cwd", default=None, help="Repository working directory for code/review.")
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help=(
        "Wall-clock timeout in seconds for review/exec provider calls. "
        "Unbounded by default. Review-tagged auto routes derive their own "
        "provider budget. Council uses --council-timeout for its total cap."
    ),
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help=(
        "Kill streaming exec/review providers after this many silent seconds. "
        "Review-tagged auto routes derive their own stall budget. Set 0 to disable."
    ),
)
@click.option(
    "--council-timeout",
    "council_timeout_sec",
    default=DEFAULT_COUNCIL_TIMEOUT_SEC,
    type=click.IntRange(min=1),
    show_default=True,
    help=(
        "For council: total wall-clock cap in seconds across members and synthesis. "
        "--timeout remains the per-call provider timeout."
    ),
)
@click.option(
    "--council-max-output-tokens",
    default=DEFAULT_COUNCIL_MAX_OUTPUT_TOKENS,
    type=click.IntRange(min=1),
    show_default=True,
    help="For council: stop before more calls once reported output tokens reach this total.",
)
@click.option(
    "--council-max-cost-usd",
    default=DEFAULT_COUNCIL_MAX_COST_USD,
    type=click.FloatRange(min=0.0),
    show_default=True,
    help=(
        "For council: stop before more calls once total known OpenRouter cost "
        "reaches this USD budget."
    ),
)
@click.option("--base", default=None, help="For review: compare changes against this ref.")
@click.option("--commit", default=None, help="For review: review one commit.")
@click.option(
    "--uncommitted",
    is_flag=True,
    default=False,
    help="For review: review staged, unstaged, and untracked changes.",
)
@click.option("--title", default=None, help="For review: optional review title.")
@click.option("--task", default=None, help="The task / prompt. Alias: --brief.")
@click.option(
    "--task-file",
    default=None,
    help="Read task / prompt from a UTF-8 file. Alias: --brief-file.",
)
@click.option("--brief", default=None, help="Delegation brief / prompt.")
@click.option(
    "--brief-file",
    default=None,
    help="Read the delegation brief from a UTF-8 file. Use '-' to read stdin.",
)
@click.option(
    "--issue",
    default=None,
    help=("Use a GitHub issue as the seed brief. Accepts N for the current repo or owner/repo#N."),
)
@click.option(
    "--issue-comment-limit",
    default=10,
    type=click.IntRange(min=0),
    show_default=True,
    help="Number of recent GitHub issue comments to include with --issue.",
)
@click.option(
    "--attach",
    "attach",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Attach a file to the brief. Repeat for multiple. Today only `codex` "
        "accepts attachments; review and council kinds do not pass them through."
    ),
)
@click.option("--log-file", default=None, help="For exec: write structured NDJSON events.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.option("--verbose-route", is_flag=True, default=False)
@click.option("--silent-route", is_flag=True, default=False)
@click.option(
    "--offline/--no-offline",
    "offline",
    default=None,
    help="--offline: force local ollama for supported semantic kinds. --no-offline clears it.",
)
@click.option(
    "--preflight/--no-preflight",
    "preflight",
    default=True,
    help="For exec: run a provider health probe before forwarding the task.",
)
@click.option(
    "--allow-short-brief",
    is_flag=True,
    default=False,
    help="Suppress the short-brief warning when semantic code routes to exec.",
)
def ask(
    kind: str,
    effort: str | None,
    tags: str | None,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    council_timeout_sec: int,
    council_max_output_tokens: int,
    council_max_cost_usd: float,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: str | None,
    issue: str | None,
    issue_comment_limit: int,
    attach: tuple[str, ...],
    log_file: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    offline: bool | None,
    preflight: bool,
    allow_short_brief: bool,
) -> None:
    """Run a task through Conductor's deterministic semantic routing matrix."""
    timeout_is_default = _parameter_is_default("timeout_sec")
    max_stall_is_default = _parameter_is_default("max_stall_sec")
    effort_value = _parse_effort(effort)
    plan = plan_for(kind, effort_value)
    user_tags = tuple(_parse_csv(tags))
    if plan.kind == "review":
        plan = _with_review_semantic_tags(plan, user_tags)
    else:
        plan = _with_user_semantic_tags(plan, user_tags)
    council_caps = CouncilCaps(
        timeout_sec=council_timeout_sec,
        max_output_tokens=council_max_output_tokens,
        max_cost_usd=council_max_cost_usd,
    )

    if offline is False:
        offline_mode.clear()
    elif offline is True:
        if kind == "council":
            raise click.UsageError(
                "council always routes through OpenRouter; --offline contradicts it."
            )
        if kind == "review":
            raise click.UsageError("review uses native review modes; --offline is not supported.")
        offline_mode.set_active()
        plan = with_candidate_override(plan, provider="ollama")

    plan, ollama_policy_message = _apply_ollama_offline_only_policy(
        plan,
        user_tags=user_tags,
        offline_requested=offline is True,
    )

    if kind == "council" and plan.candidates[0].provider != "openrouter":
        raise click.UsageError("council always routes through OpenRouter.")

    review_target_count = sum(1 for value in (base, commit, uncommitted) if value)
    if review_target_count > 1:
        raise click.UsageError("use only one of --base, --commit, or --uncommitted.")

    brief_input = _read_task(
        task,
        task_file,
        brief=brief,
        brief_file=brief_file,
        issue=issue,
        issue_comment_limit=issue_comment_limit,
        cwd=cwd,
        attach=attach,
    )
    if plan.mode == "call":
        _reject_call_mode_side_effect_brief(kind, brief_input.body)
    if plan.mode == "exec":
        _warn_if_short_exec_brief(brief_input, allow_short_brief=allow_short_brief)
    if plan.mode not in {"review", "council"}:
        brief_input = _with_auto_close_instructions(brief_input)
    body = brief_input.body
    attachments = brief_input.attachments
    estimated_input_tokens = _estimate_text_tokens(body)

    # `ask --kind {review,council}` doesn't have a path for attachments —
    # review forwards a diff to the provider's native review mode (no attach
    # API), and council always routes through OpenRouter. Reject up front
    # rather than silently dropping the user's files.
    if attachments and plan.mode in {"review", "council"}:
        raise click.UsageError(
            f"--attach is not supported for --kind {plan.mode}; "
            "use `conductor exec --with codex --attach ...` instead."
        )

    if ollama_policy_message and not silent_route:
        click.echo(ollama_policy_message, err=True)
    if not (silent_route or as_json):
        click.echo(_format_semantic_plan_line(plan), err=True)

    if plan.mode == "council":
        try:
            response = _invoke_council(
                plan,
                task=body,
                effort=effort_value,
                timeout_sec=timeout_sec,
                caps=council_caps,
                rounds=DEFAULT_COUNCIL_ROUNDS,
                silent=silent_route or as_json,
            )
        except CouncilCapError as e:
            _record_response_delegation(
                "council",
                e.response,
                effort=effort_value,
                semantic_plan=plan,
                council_role="parent",
            )
            click.echo(f"conductor: {e}", err=True)
            _emit_usage_log(e.response, silent=silent_route or as_json)
            _emit_call(e.response, as_json=as_json, semantic_plan=plan)
            sys.exit(1)
        except ProviderConfigError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
        _emit_usage_log(response, silent=silent_route or as_json)
        _emit_call(response, as_json=as_json, semantic_plan=plan)
        return

    dispatch_started_at = time.monotonic()
    if plan.mode == "review":
        review_estimated_input_tokens = _estimate_review_input_tokens(
            body,
            base=base,
            commit=commit,
            uncommitted=uncommitted,
            title=title,
            cwd=cwd,
        )
        review_estimate_warning = _review_input_context_warning(
            estimated_input_tokens=review_estimated_input_tokens,
            base=base,
            commit=commit,
            uncommitted=uncommitted,
        )
        if review_estimate_warning and not (silent_route or as_json):
            click.echo(review_estimate_warning, err=True)
        try:
            decision, review_reasons, plan = _build_review_route_decision(
                effort=effort_value,
                tags=user_tags,
                prefer=None,
                exclude=frozenset(),
                estimated_input_tokens=review_estimated_input_tokens,
            )
        except _ReviewRouteSelectionError as e:
            _emit_review_route_failure(e.review_reasons, e.cause, as_json=as_json)
            sys.exit(2)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
        timeout_sec, max_stall_sec = _scale_dispatch_defaults(
            provider_id=decision.provider,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
        )
        timeout_sec, max_stall_sec = _apply_review_gate_auto_budget(
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
            estimated_input_tokens=review_estimated_input_tokens,
            candidate_count=len(decision.ranked),
            silent=silent_route or as_json,
        )
        max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
        review_deadline = _review_gate_deadline(timeout_sec)
        try:
            response, fallbacks = _invoke_review_with_fallback(
                decision,
                task=body,
                effort=effort_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                base=base,
                commit=commit,
                uncommitted=uncommitted,
                title=title,
                silent=silent_route or as_json,
                models_by_provider={
                    candidate.provider: candidate.models
                    for candidate in plan.candidates
                    if candidate.models
                },
                fallback_deadline_monotonic=review_deadline,
            )
        except ProviderConfigError as e:
            _record_failed_delegation(
                "ask",
                provider_id=decision.provider,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
                semantic_plan=plan,
            )
            _emit_provider_error(e, as_json=as_json)
            sys.exit(2)
        except ProviderError as e:
            _record_failed_delegation(
                "ask",
                provider_id=decision.provider,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
                semantic_plan=plan,
            )
            _emit_provider_error(e, as_json=as_json)
            sys.exit(1)
        _emit_usage_log(response, silent=silent_route or as_json)
        _record_response_delegation(
            "ask",
            response,
            effort=effort_value,
            decision=decision,
            semantic_plan=plan,
            fallback_chain=fallbacks,
        )
        _emit_call(response, as_json=as_json, decision=decision, semantic_plan=plan)
        return

    tools_set = plan.tools
    sandbox_value = plan.sandbox
    if plan.mode == "exec" and brief_declares_read_only_text_output(body):
        read_only_tools = EXEC_PERMISSION_PROFILES["read-only"]
        narrowed = tools_set & read_only_tools
        if narrowed:
            tools_set = narrowed
            sandbox_value = "read-only"
            if not (silent_route or as_json):
                click.echo(
                    "[conductor] read-only brief detected; restricting exec "
                    f"tools to {_ordered_tools_csv(tools_set)}",
                    err=True,
                )
    exclude_set = _semantic_candidate_exclude_set(plan, frozenset())
    try:
        provider, decision = pick(
            list(plan.tags),
            prefer=plan.prefer,
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            exclude=exclude_set,
            priority=_semantic_priority(plan),
            shadow=True,
            attachments_required=bool(attachments),
            estimated_input_tokens=estimated_input_tokens,
        )
    except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)

    timeout_sec, max_stall_sec = _scale_dispatch_defaults(
        provider_id=decision.provider,
        timeout_sec=timeout_sec,
        max_stall_sec=max_stall_sec,
        timeout_is_default=timeout_is_default,
        max_stall_is_default=max_stall_is_default,
    )
    max_stall_sec = _cap_default_auto_fallback_stall(
        timeout_sec=timeout_sec,
        max_stall_sec=max_stall_sec,
        max_stall_is_default=max_stall_is_default,
        candidate_count=len(decision.ranked),
    )
    max_stall_sec = _normalize_max_stall_sec(max_stall_sec)

    session_log: SessionLog | None = None
    if plan.mode == "exec":
        session_log = _start_exec_session_log(log_file=log_file, resume_session_id=None)
        _emit_session_route_decision(session_log, decision)
    print_caller_banner(decision.provider, silent=silent_route or as_json)
    _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
    token_warning = _semantic_tool_context_warning(
        plan,
        cwd=cwd,
        issue=issue,
        estimated_input_tokens=estimated_input_tokens,
    )
    if token_warning is not None and not (silent_route or as_json):
        click.echo(token_warning, err=True)
    if plan.mode == "exec" and preflight:
        provider_obj = _provider_for_preflight(provider)
        ok, reason = provider_obj.health_probe()
        if not ok:
            if session_log is not None:
                session_log.bind_provider(provider_obj.name)
                session_log.emit(
                    "provider_failed",
                    {"provider": provider_obj.name, "error": reason or "preflight failed"},
                )
                session_log.mark_finished()
            _record_failed_delegation(
                "ask",
                provider_id=provider_obj.name,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=reason or "preflight failed",
                decision=decision,
                semantic_plan=plan,
                session_log=session_log,
            )
            _echo_preflight_failure(provider_obj, reason)
            sys.exit(2)

    models_by_provider = {
        candidate.provider: candidate.models for candidate in plan.candidates if candidate.models
    }
    try:
        response, fallbacks = _invoke_with_fallback(
            decision,
            mode=plan.mode,
            task=body,
            model=None,
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            cwd=cwd,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            start_timeout_sec=None,
            retry_on_stall=0,
            silent=silent_route or as_json,
            session_log=session_log,
            models_by_provider=models_by_provider,
            attachments=attachments,
        )
    except UnsupportedCapability as e:
        if session_log is not None:
            session_log.emit("provider_failed", {"error": str(e)})
            session_log.mark_finished()
        _record_failed_delegation(
            "ask",
            provider_id=decision.provider,
            model=None,
            effort=effort_value,
            started_at=dispatch_started_at,
            error=e,
            decision=decision,
            semantic_plan=plan,
            session_log=session_log,
        )
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)
    except ProviderConfigError as e:
        if session_log is not None:
            session_log.emit("provider_failed", {"error": str(e)})
            session_log.mark_finished()
        _record_failed_delegation(
            "ask",
            provider_id=decision.provider,
            model=None,
            effort=effort_value,
            started_at=dispatch_started_at,
            error=e,
            decision=decision,
            semantic_plan=plan,
            session_log=session_log,
        )
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)
    except ProviderError as e:
        if session_log is not None:
            session_log.mark_finished()
        _record_failed_delegation(
            "ask",
            provider_id=decision.provider,
            model=None,
            effort=effort_value,
            started_at=dispatch_started_at,
            error=e,
            decision=decision,
            semantic_plan=plan,
            session_log=session_log,
        )
        if (
            _requires_strong_code_provider(plan)
            and len(decision.ranked) == 1
            and decision.provider == decision.ranked[0].name
        ):
            click.echo(
                _format_strong_code_no_fallback_error(plan, decision.provider, e),
                err=True,
            )
        else:
            click.echo(f"conductor: {e}", err=True)
        _maybe_echo_stall_recovery_hint(e, cwd=cwd)
        sys.exit(1)

    _emit_usage_log(response, silent=silent_route or as_json)
    _emit_session_usage(session_log, response)
    if session_log is not None:
        session_log.mark_finished()
    _record_response_delegation(
        "ask",
        response,
        effort=effort_value,
        decision=decision,
        semantic_plan=plan,
        session_log=session_log,
        fallback_chain=fallbacks,
    )
    _emit_call(
        response,
        as_json=as_json,
        decision=decision,
        semantic_plan=plan,
        auth_prompts=_collect_session_auth_prompts(session_log),
    )


# --------------------------------------------------------------------------- #
# call — single-turn send-a-task-to-a-provider
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--with",
    "provider_id",
    default=None,
    help=(
        "Provider identifier "
        "(kimi, claude, codex, deepseek-chat, deepseek-reasoner, gemini, ollama, openrouter). "
        "Mutually exclusive with --auto."
    ),
)
@click.option(
    "--profile",
    default=None,
    help=(
        "Apply defaults from a named profile before env vars and explicit flags. "
        "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
    ),
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick based on --tags, --prefer, and configured providers.",
)
@click.option(
    "--tags",
    default=None,
    help="Comma-separated task tags for --auto routing (e.g. 'long-context,cheap').",
)
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)} (default: balanced).",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget (default: medium).",
)
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help=(
        "Wall-clock timeout in seconds. Unbounded by default. Review-tagged "
        "auto routes derive their own provider budget."
    ),
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help=(
        "Kill streaming CLI-backed calls after this many silent seconds. "
        "Default: 360, scaled up on slow networks. Review-tagged auto routes "
        "derive their own stall budget. Set 0 to disable."
    ),
)
@click.option(
    "--exclude",
    default=None,
    help="Comma-separated providers to exclude from --auto routing.",
)
@click.option(
    "--task",
    default=None,
    help="The task / prompt. Reads stdin if omitted.\n"
    "For delegation, prefer --brief or --brief-file.\n"
    "For long briefs, prefer --brief-file or stdin to keep the prompt\n"
    "out of `ps aux`.",
)
@click.option(
    "--task-file",
    default=None,
    help="Read the task / prompt from a UTF-8 file. Alias: --brief-file.",
)
@click.option(
    "--brief",
    default=None,
    help="Delegation brief / prompt. Alias for --task with clearer intent.",
)
@click.option(
    "--brief-file",
    default=None,
    help="Read the delegation brief from a UTF-8 file. Use '-' to read stdin.",
)
@click.option(
    "--issue",
    default=None,
    help=("Use a GitHub issue as the seed brief. Accepts N for the current repo or owner/repo#N."),
)
@click.option(
    "--issue-comment-limit",
    default=10,
    type=click.IntRange(min=0),
    show_default=True,
    help="Number of recent GitHub issue comments to include with --issue.",
)
@click.option(
    "--attach",
    "attach",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Attach a file to the brief. Repeat for multiple. Today only `codex` "
        "accepts attachments; `--auto` will route accordingly."
    ),
)
@click.option(
    "--model",
    default=None,
    help="Override the provider's default model.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full CallResponse as JSON (with routing info when --auto).",
)
@click.option(
    "--verbose-route",
    is_flag=True,
    default=False,
    help="Print the full routing decision (ranking table) to stderr.",
)
@click.option(
    "--silent-route",
    is_flag=True,
    default=False,
    help="Suppress the route-log line and caller-attribution banner "
    "(useful for clean stdout piping).",
)
@click.option(
    "--resume",
    "resume_session_id",
    default=None,
    help="Resume a prior session by ID (claude/codex/gemini only). Requires --with.",
)
@click.option(
    "--offline/--no-offline",
    "offline",
    default=None,
    help="--offline: force local (ollama) routing and set the sticky offline "
    "flag. --no-offline: clear the sticky flag before running.",
)
def call(
    provider_id: str | None,
    profile: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    exclude: str | None,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: str | None,
    issue: str | None,
    issue_comment_limit: int,
    attach: tuple[str, ...],
    model: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    resume_session_id: str | None,
    offline: bool | None,
) -> None:
    """Send a task to a provider and print the response."""
    timeout_is_default = _parameter_is_default("timeout_sec")
    max_stall_is_default = _parameter_is_default("max_stall_sec")
    explicit_prefer = prefer
    profile_spec = _load_named_profile(profile)
    provider_id = _resolve_layered_value(provider_id, env_key="CONDUCTOR_WITH")
    tags = _resolve_layered_value(
        tags,
        env_key="CONDUCTOR_TAGS",
        profile_value=profile_spec.tags if profile_spec else None,
    )
    prefer = _resolve_layered_value(
        prefer,
        env_key="CONDUCTOR_PREFER",
        profile_value=profile_spec.prefer if profile_spec else None,
    )
    effort = _resolve_layered_value(
        effort,
        env_key="CONDUCTOR_EFFORT",
        profile_value=profile_spec.effort if profile_spec else None,
    )
    exclude = _resolve_layered_value(exclude, env_key="CONDUCTOR_EXCLUDE")
    provider_id, auto = _apply_offline_flag(offline=offline, provider_id=provider_id, auto=auto)
    if offline is True and provider_id == "ollama":
        prefer = None
    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")
    if resume_session_id and auto:
        raise click.UsageError(
            "--resume requires --with <provider> (sessions are provider-specific)."
        )

    # When --with is used with --exclude, it's a contradiction:
    if provider_id and exclude and provider_id in _parse_csv(exclude):
        raise click.UsageError(
            f"--with {provider_id} and --exclude {exclude} contradict each other."
        )
    if provider_id and not auto:
        _enforce_local_provider_opt_in(provider_id, offline_requested=offline is True)

    brief_input = _read_task(
        task,
        task_file,
        brief=brief,
        brief_file=brief_file,
        issue=issue,
        issue_comment_limit=issue_comment_limit,
        attach=attach,
    )
    body = brief_input.body
    attachments = brief_input.attachments
    effort_value = _parse_effort(effort)
    estimated_input_tokens = _estimate_text_tokens(body)
    dispatch_started_at = time.monotonic()

    decision: RouteDecision | None = None
    fallbacks: list[str] = []
    if auto:
        try:
            provider, decision = pick(
                _parse_csv(tags),
                prefer=_validate_prefer(prefer),
                effort=effort_value,
                exclude=frozenset(_parse_csv(exclude)),
                shadow=True,
                attachments_required=bool(attachments),
                estimated_input_tokens=estimated_input_tokens,
            )
            decision, exclusion_message = _apply_auto_route_exclusion_rules(
                decision,
                user_tags=tuple(_parse_csv(tags)),
                offline_requested=offline is True,
            )
            provider = get_provider(decision.provider)
        except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        if exclusion_message and not silent_route:
            click.echo(exclusion_message, err=True)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
        timeout_sec, max_stall_sec = _scale_dispatch_defaults(
            provider_id=decision.provider,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
        )
        review_deadline = None
        if NATIVE_REVIEW_TAG in decision.task_tags:
            timeout_sec, max_stall_sec = _apply_review_gate_auto_budget(
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                timeout_is_default=timeout_is_default,
                max_stall_is_default=max_stall_is_default,
                estimated_input_tokens=estimated_input_tokens,
                candidate_count=len(decision.ranked),
                silent=silent_route or as_json,
            )
            review_deadline = _review_gate_deadline(timeout_sec)
        else:
            max_stall_sec = _cap_default_auto_fallback_stall(
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                max_stall_is_default=max_stall_is_default,
                candidate_count=len(decision.ranked),
            )
        max_stall_sec = _normalize_max_stall_sec(max_stall_sec)

        try:
            response, fallbacks = _invoke_with_fallback(
                decision,
                mode="call",
                task=body,
                model=model,
                effort=effort_value,
                tools=frozenset(),
                sandbox="none",
                cwd=None,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                start_timeout_sec=None,
                retry_on_stall=0,
                silent=silent_route or as_json,
                attachments=attachments,
                fallback_deadline_monotonic=review_deadline,
                fallback_budget_label="review gate budget",
                cap_fallback_attempt_timeout_to_stall=review_deadline is not None,
            )
        except ProviderConfigError as e:
            _record_failed_delegation(
                "call",
                provider_id=decision.provider,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
            )
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            _record_failed_delegation(
                "call",
                provider_id=decision.provider,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
            )
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)
    else:
        if explicit_prefer is not None and provider_id != "openrouter":
            raise click.UsageError("--prefer is only meaningful with --auto.")
        # Earlier guard `if not auto and not provider_id: raise` makes this
        # narrowing safe; the assert documents it for mypy and future readers.
        assert provider_id is not None
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        try:
            _ensure_supports_attachments(provider, attachments)
        except UnsupportedCapability as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        print_caller_banner(provider_id, silent=silent_route or as_json)
        timeout_sec, max_stall_sec = _scale_dispatch_defaults(
            provider_id=provider_id,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
        )
        max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
        try:
            if isinstance(provider, OpenRouterProvider):
                response = provider.call(
                    body,
                    model=model,
                    effort=effort_value,
                    task_tags=_parse_csv(tags),
                    prefer=_validate_prefer(prefer),
                    log_selection=not (silent_route or as_json),
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                )
            elif isinstance(provider, CodexProvider):
                response = provider.call(
                    body,
                    model=model,
                    effort=effort_value,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                    attachments=attachments,
                )
            else:
                response = provider.call(
                    body,
                    model=model,
                    effort=effort_value,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                )
        except ProviderConfigError as e:
            _record_failed_delegation(
                "call",
                provider_id=provider_id,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
            )
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except UnsupportedCapability as e:
            _record_failed_delegation(
                "call",
                provider_id=provider_id,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
            )
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
        except ProviderError as e:
            _record_failed_delegation(
                "call",
                provider_id=provider_id,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
            )
            click.echo(f"conductor: {e}", err=True)
            _maybe_echo_explicit_network_hint(provider_id, e)
            sys.exit(1)

    if auto and not as_json:
        _emit_usage_log(response, silent=silent_route)
    _record_response_delegation(
        "call",
        response,
        effort=effort_value,
        decision=decision,
        fallback_chain=fallbacks,
    )
    _emit_call(response, as_json=as_json, decision=decision)


# --------------------------------------------------------------------------- #
# review — first-class read-only code review
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--with",
    "provider_id",
    default=None,
    help=(
        "Review provider identifier. Native review providers use their review "
        "entrypoint; openrouter uses a hosted review prompt."
    ),
)
@click.option(
    "--profile",
    default=None,
    help=(
        "Apply defaults from a named profile before env vars and explicit flags. "
        "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
    ),
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick among configured native review providers.",
)
@click.option(
    "--tags",
    default=None,
    help=(
        "Comma-separated review tags. code-review is always included; "
        "tool-use is ignored for this read-only subcommand."
    ),
)
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)} (default: best).",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option(
    "--exclude",
    default=None,
    help="Comma-separated providers to exclude from --auto routing.",
)
@click.option("--cwd", default=None, help="Repository working directory.")
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help=(
        "Wall-clock timeout in seconds for the native review command. "
        "Review auto-routing derives its own provider budget."
    ),
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help=(
        "Kill streaming review providers if they produce no output for this "
        "many seconds. Review auto-routing derives its own stall budget."
    ),
)
@click.option(
    "--max-fallbacks",
    default=DEFAULT_REVIEW_MAX_FALLBACKS,
    type=int,
    help=(
        "Maximum code-review providers to try in total for --auto review "
        f"(default: {DEFAULT_REVIEW_MAX_FALLBACKS})."
    ),
)
@click.option(
    "--base",
    default=None,
    help="Review changes against this base branch/ref.",
)
@click.option(
    "--commit",
    default=None,
    help="Review the changes introduced by one commit.",
)
@click.option(
    "--uncommitted",
    is_flag=True,
    default=False,
    help="Review staged, unstaged, and untracked changes.",
)
@click.option(
    "--title",
    default=None,
    help="Optional review title passed to providers that support it.",
)
@click.option(
    "--task",
    default=None,
    help="Review instructions. Reads stdin if omitted. Alias: --brief.",
)
@click.option(
    "--task-file",
    default=None,
    help="Read review instructions from a UTF-8 file. Alias: --brief-file.",
)
@click.option(
    "--brief",
    default=None,
    help="Review instructions. Alias for --task.",
)
@click.option(
    "--brief-file",
    default=None,
    help="Read review instructions from a UTF-8 file. Use '-' to read stdin.",
)
@click.option(
    "--issue",
    default=None,
    help=("Use a GitHub issue as the seed brief. Accepts N for the current repo or owner/repo#N."),
)
@click.option(
    "--issue-comment-limit",
    default=10,
    type=click.IntRange(min=0),
    show_default=True,
    help="Number of recent GitHub issue comments to include with --issue.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full CallResponse as JSON (with routing info when --auto).",
)
@click.option(
    "--verbose-route",
    is_flag=True,
    default=False,
    help="Print the full native-review routing decision to stderr.",
)
@click.option(
    "--silent-route",
    is_flag=True,
    default=False,
    help="Suppress route-log output and caller-attribution banner.",
)
def review(
    provider_id: str | None,
    profile: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    exclude: str | None,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    max_fallbacks: int,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: str | None,
    issue: str | None,
    issue_comment_limit: int,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
) -> None:
    """Run a read-only code review through a provider's native review mode."""
    timeout_is_default = _parameter_is_default("timeout_sec")
    max_stall_is_default = _parameter_is_default("max_stall_sec")
    profile_spec = _load_named_profile(profile)
    provider_id = _resolve_layered_value(provider_id, env_key="CONDUCTOR_WITH")
    tags = _resolve_layered_value(
        tags,
        env_key="CONDUCTOR_TAGS",
        profile_value=profile_spec.tags if profile_spec else None,
    )
    prefer = _resolve_layered_value(
        prefer,
        env_key="CONDUCTOR_PREFER",
        profile_value=profile_spec.prefer if profile_spec else None,
    )
    effort = _resolve_layered_value(
        effort,
        env_key="CONDUCTOR_EFFORT",
        profile_value=profile_spec.effort if profile_spec else None,
    )
    exclude = _resolve_layered_value(exclude, env_key="CONDUCTOR_EXCLUDE")

    implicit_auto = provider_id is None
    auto_route = auto or implicit_auto

    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if provider_id and exclude and provider_id in _parse_csv(exclude):
        raise click.UsageError(
            f"--with {provider_id} and --exclude {exclude} contradict each other."
        )
    review_target_count = sum(1 for value in (base, commit, uncommitted) if value)
    if review_target_count > 1:
        raise click.UsageError("use only one of --base, --commit, or --uncommitted.")

    brief_input = _read_task(
        task,
        task_file,
        brief=brief,
        brief_file=brief_file,
        issue=issue,
        issue_comment_limit=issue_comment_limit,
        cwd=cwd,
    )
    body = brief_input.body
    effort_value = _parse_effort(effort)
    estimated_input_tokens = _estimate_review_input_tokens(
        body,
        base=base,
        commit=commit,
        uncommitted=uncommitted,
        title=title,
        cwd=cwd,
    )
    review_estimate_warning = _review_input_context_warning(
        estimated_input_tokens=estimated_input_tokens,
        base=base,
        commit=commit,
        uncommitted=uncommitted,
    )
    if review_estimate_warning and not (silent_route or as_json):
        click.echo(review_estimate_warning, err=True)
    dispatch_started_at = time.monotonic()
    max_fallbacks = _validate_max_fallbacks(max_fallbacks)
    prefer_value = _validate_prefer(prefer) if prefer is not None else None

    decision: RouteDecision | None = None
    fallbacks: list[str] = []
    if auto_route:
        user_tags = tuple(_parse_csv(tags))
        user_exclude = frozenset(_parse_csv(exclude))
        try:
            decision, review_reasons, plan = _build_review_route_decision(
                effort=effort_value,
                tags=user_tags,
                prefer=prefer_value,
                exclude=user_exclude,
                estimated_input_tokens=estimated_input_tokens,
            )
        except _ReviewRouteSelectionError as e:
            _emit_review_route_failure(e.review_reasons, e.cause, as_json=as_json)
            sys.exit(2)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
        timeout_sec, max_stall_sec = _scale_dispatch_defaults(
            provider_id=decision.provider,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
        )
        timeout_sec, max_stall_sec = _apply_review_gate_auto_budget(
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
            estimated_input_tokens=estimated_input_tokens,
            candidate_count=min(max_fallbacks, len(decision.ranked)),
            silent=silent_route or as_json,
        )
        max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
        review_deadline = _review_gate_deadline(timeout_sec)
        try:
            response, fallbacks = _invoke_review_with_fallback(
                decision,
                task=body,
                effort=effort_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                base=base,
                commit=commit,
                uncommitted=uncommitted,
                title=title,
                silent=silent_route or as_json,
                max_fallbacks=max_fallbacks,
                models_by_provider={
                    candidate.provider: candidate.models
                    for candidate in plan.candidates
                    if candidate.models
                },
                fallback_deadline_monotonic=review_deadline,
            )
        except ProviderConfigError as e:
            _record_failed_delegation(
                "review",
                provider_id=decision.provider,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
            )
            _emit_provider_error(e, as_json=as_json)
            sys.exit(2)
        except ProviderError as e:
            _record_failed_delegation(
                "review",
                provider_id=decision.provider,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
            )
            _emit_provider_error(e, as_json=as_json)
            sys.exit(1)
    else:
        assert provider_id is not None
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        print_caller_banner(provider_id, silent=silent_route or as_json)
        timeout_sec, max_stall_sec = _scale_dispatch_defaults(
            provider_id=provider_id,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
        )
        max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
        try:
            if isinstance(provider, OpenRouterProvider):
                plan = _with_review_semantic_tags(
                    plan_for("review", effort_value),
                    tuple(_parse_csv(tags)),
                )
                models_by_provider = {
                    candidate.provider: candidate.models
                    for candidate in plan.candidates
                    if candidate.models
                }
                contract_prompt = build_review_task_prompt(
                    body,
                    base=base,
                    commit=commit,
                    uncommitted=uncommitted,
                    title=title,
                    cwd=cwd,
                    include_patch=True,
                )
                response = provider.call(
                    contract_prompt,
                    models=models_by_provider.get(provider_id),
                    effort=effort_value,
                    task_tags=list(plan.tags),
                    prefer=prefer_value or plan.prefer,
                    log_selection=not (silent_route or as_json),
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                )
                validated_text = validate_requested_review_sentinel(
                    provider_name=response.provider,
                    prompt=contract_prompt,
                    text=response.text,
                )
                if validated_text != response.text:
                    response = replace(response, text=validated_text)
            else:
                review_provider = _review_provider_or_none(provider)
                if review_provider is None:
                    raise click.UsageError(
                        f"provider {provider_id!r} does not expose native code review."
                    )
                ok, reason = review_provider.review_configured()
                if not ok:
                    click.echo(
                        f"conductor: {reason or 'native review is not configured'}",
                        err=True,
                    )
                    sys.exit(2)
                response = review_provider.review(
                    body,
                    effort=effort_value,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    base=base,
                    commit=commit,
                    uncommitted=uncommitted,
                    title=title,
                )
        except ProviderConfigError as e:
            _record_failed_delegation(
                "review",
                provider_id=provider_id,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
            )
            _emit_provider_error(e, as_json=as_json)
            sys.exit(2)
        except ProviderError as e:
            _record_failed_delegation(
                "review",
                provider_id=provider_id,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
            )
            _emit_provider_error(e, as_json=as_json)
            sys.exit(1)
        except ReviewContextError as e:
            _record_failed_delegation(
                "review",
                provider_id=provider_id,
                model=None,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
            )
            click.echo(f"conductor: {e}", err=True)
            sys.exit(1)

    if auto_route and not as_json:
        _emit_usage_log(response, silent=silent_route)
    _record_response_delegation(
        "review",
        response,
        effort=effort_value,
        decision=decision,
        fallback_chain=fallbacks,
    )
    _emit_call(response, as_json=as_json, decision=decision)


class _ExecPhaseError(Exception):
    def __init__(self, *, exit_code: int, exit_status: str, message: str) -> None:
        self.exit_code = exit_code
        self.exit_status = exit_status
        self.message = message
        super().__init__(message)


def _exec_phase_exit(
    code: int,
    *,
    raise_on_error: bool,
    status: str = "error",
    message: str = "exec phase failed",
) -> NoReturn:
    if raise_on_error:
        raise _ExecPhaseError(exit_code=code, exit_status=status, message=message)
    sys.exit(code)


def _exec_failure_status(error: Exception | str) -> str:
    if isinstance(error, ProviderExecutionError):
        if error.status.get("state") == "iteration-cap":
            return "cap-exit"
        return str(error.status.get("state") or "error")
    if isinstance(error, ProviderStalledError):
        return "stalled"
    return _delegation_status_from_error(error)


_EXEC_PHASE_GIT_TIMEOUT_SEC = 10.0


def _git_phase_head(cwd: str | None) -> str | None:
    worktree = cwd or os.getcwd()
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=_EXEC_PHASE_GIT_TIMEOUT_SEC,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        click.echo(
            f"[conductor] warning: could not capture phase git HEAD in {worktree}: {e}",
            err=True,
        )
        return None
    return result.stdout.strip()


def _git_phase_output(cwd: str | None, args: list[str]) -> str | None:
    worktree = cwd or os.getcwd()
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=_EXEC_PHASE_GIT_TIMEOUT_SEC,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        click.echo(
            f"[conductor] warning: git {' '.join(args)} failed in {worktree}: {e}",
            err=True,
        )
        return None
    return result.stdout.strip()


def _git_phase_commit_count(cwd: str | None, phase_start: str | None) -> int:
    if phase_start is None:
        return 0
    output = _git_phase_output(cwd, ["rev-list", "--count", f"{phase_start}..HEAD"])
    if output is None:
        return 0
    try:
        return int(output)
    except ValueError:
        click.echo(
            f"[conductor] warning: unexpected git rev-list count output: {output!r}",
            err=True,
        )
        return 0


def _git_phase_summary(cwd: str | None, phase_number: int, phase_start: str | None) -> str:
    if phase_start is None:
        return f"## Phase {phase_number} results\n\nGit summary unavailable."

    files = _git_phase_output(cwd, ["diff", "--name-status", f"{phase_start}..HEAD"])
    commits = _git_phase_output(cwd, ["log", "--oneline", f"{phase_start}..HEAD"])
    files_text = files if files else "No file changes detected."
    commits_text = commits if commits else "No new commits."
    return (
        f"## Phase {phase_number} results\n\n"
        "Files changed:\n"
        f"{files_text}\n\n"
        "New commits:\n"
        f"{commits_text}"
    )


def _append_previous_phase_results(body: str, summaries: list[str]) -> str:
    if not summaries:
        return body
    return f"{body}\n\n" + "\n\n".join(summaries)


SWARM_SUCCESS_STATUSES = frozenset({"shipped", "no-changes"})
SWARM_PROVIDER_FAILED_STATUS = "provider-failed"
SWARM_CONFLICT_STATUS = "needs-human-conflict-resolution"
SWARM_VALIDATION_FAILED_STATUS = "validation-failed"
SWARM_DELIVERY_CONTRACT = """
Conductor swarm delivery contract:
- Commit all intended changes before your final answer.
- Leave the worktree clean.
- Do not open a pull request; conductor swarm opens and merges PRs after exec succeeds.
- If no code changes are needed, say that explicitly in the final answer.
""".strip()
SWARM_BRANCH_PREFIX = "feat/swarm"
SWARM_WORKTREE_LOCK = threading.Lock()
SWARM_MANIFEST_LOCK = threading.Lock()
SWARM_GIT_TIMEOUT_SEC = 30.0
SWARM_GH_TIMEOUT_SEC = 30.0
SWARM_SHIP_TIMEOUT_SEC = 1800.0
SWARM_MANIFEST_SCHEMA_VERSION = 1
SWARM_METRICS_SCHEMA_VERSION = 1
SWARM_PREFLIGHT_SCHEMA_VERSION = 1
SWARM_MERGE_PLAN_SCHEMA_VERSION = 1
SWARM_CORTEX_BOOKKEEPING_SCHEMA_VERSION = 1


def _with_swarm_delivery_contract(body: str) -> str:
    if "Conductor swarm delivery contract:" in body:
        return body
    return f"{body.rstrip()}\n\n{SWARM_DELIVERY_CONTRACT}"


SWARM_PREFLIGHT_SUBSYSTEMS = frozenset(
    {
        "swarm",
        "provider",
        "router",
        "credentials",
        "cortex",
        "review",
        "openrouter",
        "cli",
    }
)
_SWARM_FENCED_CODE_BLOCK_RE = re.compile(r"(?ms)^```.*?^```")
_SWARM_PREFLIGHT_PATH_RE = re.compile(
    r"(?<![\w./-])(?P<path>(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.[A-Za-z0-9_+-]+)"
)
_SWARM_CLOSING_LINE_RE = re.compile(
    r"(?im)^\s*(?:Closes|Fixes|Resolves)\s+"
    r"(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/)?"
    r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#|#)?"
    r"(?P<number>[1-9]\d*)\s*\.?\s*$"
)
_SWARM_ISSUE_LINE_RE = re.compile(
    r"(?im)^\s*Issue:\s+"
    r"(?:https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/)?"
    r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#|#)?"
    r"(?P<number>[1-9]\d*)\s*\.?\s*$"
)
_SWARM_GENERATED_ISSUE_HEADER_RE = re.compile(
    r"(?im)^\s*#\s+Issue:.*\([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+#"
    r"(?P<number>[1-9]\d*)\)\s*$"
)
_SWARM_ISSUE_PATH_SEGMENT_RE = re.compile(r"(?i)^issue[-_](?P<number>[1-9]\d*)\b")
_SWARM_DEPENDS_ON_RE = re.compile(r"(?im)^\s*Depends-On:\s*(?P<target>.+?)\s*$")
_SWARM_PROVIDER_TRANSIENT_SIGNALS = (
    "websocket",
    "connection closed",
    "connection reset",
    "connection refused",
    "connection aborted",
    "connection error",
    "disconnect",
    "disconnected",
    "network error",
    "network failure",
    "network is unreachable",
    "network is down",
    "no route to host",
    "could not resolve",
    "temporary failure in name resolution",
    "getaddrinfo failed",
    "model refresh",
    "refresh models",
    "refreshing model",
    "rate limit",
    "rate_limit",
    "ratelimit",
    "rate-limit",
    "too many requests",
    "quota",
    "insufficient credits",
    "http 429",
    "status 429",
    "provider http",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "status 500",
    "status 502",
    "status 503",
    "status 504",
    "bad gateway",
    "gateway timeout",
    "service unavailable",
    "internal server error",
    "provider unavailable",
    "provider timed out",
    "provider timeout",
    "api timed out",
    "request timed out",
    "read timed out",
    "deadline exceeded",
    "stalled",
    "no output",
)


@dataclass(frozen=True)
class SwarmCortexBookkeeping:
    required: bool
    status: str
    context_path: str | None
    follow_up_command: str | None
    merged_prs: list[dict[str, object]]
    residual_risks: list[str]

    def to_json(self) -> dict[str, object]:
        return {
            "schema_version": SWARM_CORTEX_BOOKKEEPING_SCHEMA_VERSION,
            "required": self.required,
            "status": self.status,
            "context_path": self.context_path,
            "follow_up_command": self.follow_up_command,
            "merged_prs": self.merged_prs,
            "residual_risks": self.residual_risks,
        }


@dataclass
class SwarmTaskResult:
    brief: str
    branch: str
    worktree: str
    status: str
    commits: int
    pr_url: str | None
    merge_sha: str | None
    duration_ms: int
    merge_status: str | None = None
    inspection_warning: str | None = None
    cleanup_warning: str | None = None
    failure_reason: str | None = None
    validation_failure: dict[str, object] | None = None
    provider: str | None = None
    selected_provider: str | None = None
    fallback_provider: str | None = None
    fallback_reason: str | None = None
    last_phase: str | None = None
    last_progress_at: str | None = None
    phase_history: list[dict[str, object]] | None = None

    def to_json(self, *, include_provider_details: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "brief": self.brief,
            "branch": self.branch,
            "worktree": self.worktree,
            "status": self.status,
            "commits": self.commits,
            "pr_url": self.pr_url,
            "merge_sha": self.merge_sha,
            "duration_ms": self.duration_ms,
        }
        if self.failure_reason is not None:
            payload["failure_reason"] = self.failure_reason
        payload["merge_status"] = self.merge_status
        payload["inspection_warning"] = self.inspection_warning
        payload["cleanup_warning"] = self.cleanup_warning
        if self.last_phase is not None:
            payload["last_phase"] = self.last_phase
        if self.last_progress_at is not None:
            payload["last_progress_at"] = self.last_progress_at
        if self.phase_history is not None:
            payload["phase_history"] = self.phase_history
        if self.validation_failure is not None:
            payload["validation_failure"] = self.validation_failure
        if include_provider_details:
            payload["provider"] = self.provider
            payload["selected_provider"] = self.selected_provider
            payload["fallback_provider"] = self.fallback_provider
            payload["fallback_reason"] = self.fallback_reason
        return payload


@dataclass(frozen=True)
class SwarmShipOutcome:
    status: str
    pr_url: str | None
    detail: str | None
    merge_status: str | None = None
    inspection_warning: str | None = None
    validation_failure: dict[str, object] | None = None

    def __iter__(self):
        yield self.status
        yield self.pr_url
        yield self.detail


def _sanitize_swarm_stem(path: str) -> str:
    raw = Path(path).stem.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", raw).strip("-")
    slug = re.sub(r"-+", "-", slug)
    if not slug:
        raise click.UsageError(f"could not derive a branch slug from --brief {path!r}.")
    return slug


def _swarm_branch_for_brief(path: str) -> str:
    return f"{SWARM_BRANCH_PREFIX}/{_sanitize_swarm_stem(path)}"


def _swarm_worktree_for_brief(path: str, *, repo_root: Path) -> Path:
    return (repo_root / ".cache" / "conductor" / "swarm" / _sanitize_swarm_stem(path)).resolve()


def _swarm_runs_dir(repo_root: Path) -> Path:
    return repo_root / ".cache" / "conductor" / "swarm" / "runs"


def _swarm_bookkeeping_dir(repo_root: Path) -> Path:
    return repo_root / ".cache" / "conductor" / "swarm" / "bookkeeping"


def _new_swarm_run_id(started_at: datetime) -> str:
    return f"{started_at.strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex[:8]}"


def _swarm_now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _append_unique_issue_ref(refs: list[str], seen: set[str], number: str) -> None:
    ref = f"#{number}"
    if ref in seen:
        return
    seen.add(ref)
    refs.append(ref)


def _swarm_github_repo_slug(repo_root: Path) -> str | None:
    remote_url = _run_swarm_git(
        ["config", "--get", "remote.origin.url"],
        cwd=repo_root,
        check=False,
    ).stdout.strip()
    match = re.search(
        r"github\.com[:/](?P<owner>[A-Za-z0-9_.-]+)/"
        r"(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?/?$",
        remote_url,
    )
    if match is None:
        return None
    return f"{match.group('owner')}/{match.group('repo')}".lower()


def _swarm_issue_ref_matches_repo(match_text: str, repo_slug: str | None) -> bool:
    url_match = re.search(
        r"github\.com/(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/issues/[1-9]\d*",
        match_text,
        flags=re.IGNORECASE,
    )
    shorthand_match = re.search(
        r"(?<![\w.-])(?P<slug>[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#[1-9]\d*",
        match_text,
        flags=re.IGNORECASE,
    )
    repo_match = url_match or shorthand_match
    if repo_match is None:
        return True
    return repo_slug is not None and repo_match.group("slug").lower() == repo_slug


def _swarm_issue_closing_refs(
    brief_body: str,
    brief_path: str | Path | None = None,
    *,
    repo_slug: str | None = None,
) -> list[str]:
    """Return explicit issue refs that should be copied into the swarm PR body."""
    refs: list[str] = []
    seen: set[str] = set()
    searchable_body = _SWARM_FENCED_CODE_BLOCK_RE.sub("", brief_body)
    for pattern in (
        _SWARM_CLOSING_LINE_RE,
        _SWARM_ISSUE_LINE_RE,
        _SWARM_GENERATED_ISSUE_HEADER_RE,
    ):
        for match in pattern.finditer(searchable_body):
            if _swarm_issue_ref_matches_repo(match.group(0), repo_slug):
                _append_unique_issue_ref(refs, seen, match.group("number"))

    if brief_path is None:
        return refs
    path = Path(brief_path)
    parts = path.parts
    for index, part in enumerate(parts):
        if part.lower() == "issues" and index + 1 < len(parts):
            stem = Path(parts[index + 1]).stem
            if stem.isdecimal() and not stem.startswith("0"):
                _append_unique_issue_ref(refs, seen, stem)
        path_match = _SWARM_ISSUE_PATH_SEGMENT_RE.match(Path(part).stem)
        if path_match:
            _append_unique_issue_ref(refs, seen, path_match.group("number"))
    return refs


def _swarm_pr_number(pr_url: object) -> str | None:
    if not isinstance(pr_url, str) or not pr_url:
        return None
    tail = pr_url.rstrip("/").rsplit("/", 1)[-1]
    if tail.isdecimal() and not tail.startswith("0"):
        return tail
    return None


def _swarm_cortex_post_merge_required(repo_root: Path) -> bool:
    cortex_dir = repo_root / ".cortex"
    if not cortex_dir.is_dir():
        return False
    template_path = cortex_dir / "templates" / "journal" / "pr-merged.md"
    if template_path.is_file():
        return True
    protocol_path = cortex_dir / "protocol.md"
    if not protocol_path.is_file():
        return False
    try:
        protocol = protocol_path.read_text(encoding="utf-8")
    except OSError as e:
        click.echo(
            f"[conductor] warning: could not inspect Cortex protocol at {protocol_path}: {e}",
            err=True,
        )
        return False
    return "T1.9" in protocol and "journal/pr-merged.md" in protocol


def _swarm_cortex_hook_path(repo_root: Path) -> Path | None:
    hook_path = repo_root / "scripts" / "cortex-pr-merged-hook.sh"
    if hook_path.is_file():
        return hook_path
    return None


def _swarm_read_brief_for_bookkeeping(brief_path: object, *, repo_root: Path) -> str:
    if not isinstance(brief_path, str) or not brief_path:
        return ""
    path = Path(brief_path)
    if not path.is_absolute():
        path = repo_root / path
    try:
        return path.read_text(encoding="utf-8")
    except OSError as e:
        click.echo(
            f"[conductor] warning: could not read swarm brief for Cortex bookkeeping "
            f"at {path}: {e}",
            err=True,
        )
        return ""


def _swarm_task_validation_status(task: dict[str, object]) -> str:
    if task.get("status") != "shipped":
        return f"not merged by swarm; status={task.get('status', 'unknown')}"
    if task.get("merge_sha"):
        return "swarm auto-merge completed; merge-gate details are in the PR/checks"
    return "swarm auto-merge completed; merge commit SHA was not captured"


def _swarm_task_residual_risk(task: dict[str, object]) -> str:
    reason = task.get("failure_reason")
    if isinstance(reason, str) and reason:
        return reason
    return "none recorded by swarm"


def _swarm_bookkeeping_string_values(entry: dict[str, object], key: str) -> tuple[str, ...]:
    values = entry.get(key)
    if not isinstance(values, list):
        return ()
    return tuple(str(value) for value in values)


def _swarm_cortex_bookkeeping_entry(
    task: dict[str, object],
    *,
    repo_root: Path,
    repo_slug: str | None,
) -> dict[str, object] | None:
    if task.get("status") != "shipped":
        return None
    pr_number = _swarm_pr_number(task.get("pr_url"))
    if pr_number is None:
        return None
    brief_path = task.get("brief")
    brief_ref: str | Path | None = brief_path if isinstance(brief_path, str) else None
    brief_body = _swarm_read_brief_for_bookkeeping(brief_path, repo_root=repo_root)
    closed_issues = _swarm_issue_closing_refs(
        brief_body,
        brief_ref,
        repo_slug=repo_slug,
    )
    return {
        "pr_number": pr_number,
        "pr_url": task.get("pr_url"),
        "branch": task.get("branch"),
        "brief": task.get("brief"),
        "merge_sha": task.get("merge_sha"),
        "commits": _as_nonnegative_int(task.get("commits")),
        "closed_issues": closed_issues,
        "validation_status": _swarm_task_validation_status(task),
        "residual_risks": [_swarm_task_residual_risk(task)],
    }


def _swarm_cortex_manual_commands(
    *,
    run_id: str,
    manifest_path: Path,
    merged_prs: list[dict[str, object]],
    hook_path: Path | None,
) -> list[str]:
    commands = [f"conductor swarm status {shlex.quote(str(manifest_path))}"]
    if hook_path is not None:
        for entry in merged_prs:
            commands.append(
                "TOUCHSTONE_MERGED_PR={pr} bash {hook}".format(
                    pr=shlex.quote(str(entry["pr_number"])),
                    hook=shlex.quote(str(hook_path)),
                )
            )
        return commands

    today = datetime.now(UTC).date().isoformat()
    commands.extend(
        [
            "git checkout -b "
            + shlex.quote(f"docs/cortex-swarm-{run_id[:24]}"),
            "mkdir -p .cortex/journal",
        ]
    )
    for entry in merged_prs:
        path = f".cortex/journal/{today}-pr-{entry['pr_number']}-merged.md"
        quoted_path = shlex.quote(path)
        commands.append(
            "test ! -e {path} || {{ echo {message} >&2; exit 1; }}".format(
                path=quoted_path,
                message=shlex.quote(
                    f"ERROR: {path} already exists; inspect it before writing"
                ),
            )
        )
        commands.append(
            "cp .cortex/templates/journal/pr-merged.md " + quoted_path
        )
        commands.append("${EDITOR:-vi} " + shlex.quote(path))
    commands.extend(
        [
            "cortex refresh-state --path .",
            "bash scripts/touchstone-run.sh validate",
            "git add .cortex/journal .cortex/state.md",
            "git commit -m "
            + shlex.quote(f"Record Cortex post-merge notes for swarm {run_id}"),
            "bash scripts/open-pr.sh --auto-merge",
        ]
    )
    return commands


def _swarm_write_cortex_bookkeeping_brief(
    *,
    repo_root: Path,
    run_id: str,
    manifest_path: Path,
    base_branch: object,
    base_ref: object,
    merged_prs: list[dict[str, object]],
    non_merged_tasks: list[dict[str, object]],
    hook_path: Path | None,
) -> tuple[Path, str]:
    brief_path = _swarm_bookkeeping_dir(repo_root) / f"{run_id}-cortex-post-merge.md"
    commands = _swarm_cortex_manual_commands(
        run_id=run_id,
        manifest_path=manifest_path,
        merged_prs=merged_prs,
        hook_path=hook_path,
    )
    lines = [
        f"# Cortex post-merge bookkeeping for swarm run {run_id}",
        "",
        "Goal",
        "Create the repo-level Cortex post-merge bookkeeping PR for this swarm run.",
        "Write append-only `journal/pr-merged.md` entries for every merged PR below and "
        "refresh generated state only through the repo's normal Cortex workflow.",
        "",
        "Constraints",
        "- Preserve append-only Cortex journal history.",
        "- Do not hand-edit protected/generated files except via the normal Cortex "
        "refresh command.",
        "- Include merged PRs, closed issues, commit SHAs, validation status, and residual risks.",
        "",
        "Context",
        f"- Manifest: {manifest_path}",
        f"- Repo root: {repo_root}",
        f"- Base: {base_branch}@{base_ref}",
        "- Cortex trigger: T1.9, pull request merged to the default branch -> "
        "`journal/pr-merged.md`.",
        "",
        "Merged PRs",
    ]
    for entry in merged_prs:
        closed = (
            ", ".join(_swarm_bookkeeping_string_values(entry, "closed_issues"))
            or "none detected"
        )
        risks = "; ".join(_swarm_bookkeeping_string_values(entry, "residual_risks"))
        lines.extend(
            [
                f"- PR #{entry['pr_number']}: {entry['pr_url']}",
                f"  - branch: {entry['branch']}",
                f"  - brief: {entry['brief']}",
                f"  - merge commit: {entry['merge_sha'] or 'not captured'}",
                f"  - worker commits: {entry['commits']}",
                f"  - closes issues: {closed}",
                f"  - validation status: {entry['validation_status']}",
                f"  - known residual risks: {risks}",
            ]
        )
    if non_merged_tasks:
        lines.extend(["", "Non-Merged Swarm Tasks"])
        for task in non_merged_tasks:
            lines.append(
                "- {status}: {brief} -> {branch}; reason: {reason}".format(
                    status=task.get("status", "unknown"),
                    brief=task.get("brief", ""),
                    branch=task.get("branch", ""),
                    reason=task.get("failure_reason") or "none recorded",
                )
            )
    lines.extend(["", "Exact Follow-Up Commands"])
    lines.extend(f"```bash\n{command}\n```" for command in commands)
    lines.append("")
    lines.append("Expected output")
    lines.append(
        "A follow-up PR that adds one `.cortex/journal/YYYY-MM-DD-pr-N-merged.md` "
        "entry per merged PR and refreshes `.cortex/state.md` when the project "
        "workflow requires it."
    )

    brief_path.parent.mkdir(parents=True, exist_ok=True)
    brief_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    follow_up_command = (
        "conductor swarm --provider codex --brief "
        + shlex.quote(str(brief_path))
        + " --auto-merge"
    )
    return brief_path, follow_up_command


def _swarm_build_cortex_bookkeeping(
    *,
    repo_root: Path,
    run_id: str,
    manifest_path: Path,
    payload: dict[str, object],
) -> SwarmCortexBookkeeping | None:
    tasks = _swarm_manifest_tasks(payload, path=manifest_path)
    repo_slug = _swarm_github_repo_slug(repo_root)
    merged_prs = [
        entry
        for task in tasks
        if (
            entry := _swarm_cortex_bookkeeping_entry(
                task,
                repo_root=repo_root,
                repo_slug=repo_slug,
            )
        )
        is not None
    ]
    if not merged_prs:
        return None
    required = _swarm_cortex_post_merge_required(repo_root)
    if not required:
        return SwarmCortexBookkeeping(
            required=False,
            status="not-configured",
            context_path=None,
            follow_up_command=None,
            merged_prs=merged_prs,
            residual_risks=[],
        )

    non_merged_tasks = [task for task in tasks if task.get("status") != "shipped"]
    hook_path = _swarm_cortex_hook_path(repo_root)
    context_path, follow_up_command = _swarm_write_cortex_bookkeeping_brief(
        repo_root=repo_root,
        run_id=run_id,
        manifest_path=manifest_path,
        base_branch=payload.get("base_branch", ""),
        base_ref=payload.get("base_ref", ""),
        merged_prs=merged_prs,
        non_merged_tasks=non_merged_tasks,
        hook_path=hook_path,
    )
    residual_risks = [
        f"{task.get('brief')}: {task.get('failure_reason') or task.get('status')}"
        for task in non_merged_tasks
    ]
    return SwarmCortexBookkeeping(
        required=True,
        status="follow-up-brief-queued",
        context_path=str(context_path),
        follow_up_command=follow_up_command,
        merged_prs=merged_prs,
        residual_risks=residual_risks,
    )


def _swarm_payload_with_cortex_bookkeeping(
    *,
    repo_root: Path,
    run_id: str,
    manifest_path: Path,
    payload: dict[str, object],
) -> tuple[dict[str, object], SwarmCortexBookkeeping | None]:
    bookkeeping = _swarm_build_cortex_bookkeeping(
        repo_root=repo_root,
        run_id=run_id,
        manifest_path=manifest_path,
        payload=payload,
    )
    if bookkeeping is None:
        return payload, None
    updated = dict(payload)
    updated["post_merge_bookkeeping"] = bookkeeping.to_json()
    return updated, bookkeeping


def _emit_swarm_cortex_bookkeeping_notice(
    bookkeeping: SwarmCortexBookkeeping | None,
    *,
    err: bool,
) -> None:
    if bookkeeping is None or not bookkeeping.required:
        return
    click.echo(
        "[conductor] Cortex post-merge bookkeeping required for merged swarm PRs.",
        err=err,
    )
    if bookkeeping.context_path is not None:
        click.echo(f"[conductor] Bookkeeping brief: {bookkeeping.context_path}", err=err)
    if bookkeeping.follow_up_command is not None:
        click.echo(f"[conductor] Follow-up command: {bookkeeping.follow_up_command}", err=err)


def _append_swarm_closing_refs_to_head_commit(worktree: Path, refs: list[str]) -> None:
    if not refs:
        return
    existing_body = _run_swarm_git(["log", "-1", "--format=%b"], cwd=worktree).stdout
    missing = [
        ref
        for ref in refs
        if not re.search(rf"(?im)^\s*Closes\s+{re.escape(ref)}\s*\.?\s*$", existing_body)
    ]
    if not missing:
        return

    message = _run_swarm_git(["log", "-1", "--format=%B"], cwd=worktree).stdout.rstrip()
    updated_message = f"{message}\n\n" + "\n".join(f"Closes {ref}" for ref in missing) + "\n"
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="conductor-swarm-commit-",
            suffix=".msg",
            delete=False,
        ) as handle:
            handle.write(updated_message)
            temp_path = Path(handle.name)
        _run_swarm_git(["commit", "--amend", "-F", str(temp_path)], cwd=worktree)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def _validate_swarm_briefs(briefs: tuple[str, ...], *, repo_root: Path) -> None:
    seen_branches: dict[str, str] = {}
    for brief_path in briefs:
        stem = _sanitize_swarm_stem(brief_path)
        if stem == "runs":
            raise click.UsageError(
                f"--brief {brief_path!r} maps to reserved swarm worktree slug 'runs'; "
                "rename the brief file."
            )
        branch = _swarm_branch_for_brief(brief_path)
        if branch in seen_branches:
            raise click.UsageError(
                f"--brief {brief_path!r} and {seen_branches[branch]!r} both map to "
                f"{branch!r}; rename one brief so swarm branches are unique."
            )
        seen_branches[branch] = brief_path

        brief = Path(brief_path)
        if not brief.is_absolute():
            brief = (repo_root / brief).resolve()
        try:
            body = brief.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise click.UsageError(
                f"could not read --brief {brief_path!r}: {e.strerror or e}"
            ) from e
        if not body:
            raise click.UsageError(f"brief is empty after stripping whitespace: {brief_path!r}")


def _swarm_preflight_lane(
    brief_path: str,
    *,
    repo_root: Path,
    repo_slug: str | None,
) -> dict[str, object]:
    path = Path(brief_path)
    resolved_path = path if path.is_absolute() else (repo_root / path).resolve()
    body = resolved_path.read_text(encoding="utf-8")
    searchable_body = _SWARM_FENCED_CODE_BLOCK_RE.sub("", body)
    file_paths = sorted(
        {
            match.group("path").strip("`'\"),.;:")
            for match in _SWARM_PREFLIGHT_PATH_RE.finditer(searchable_body)
        }
    )
    lowered = searchable_body.lower()
    subsystems = sorted(
        subsystem
        for subsystem in SWARM_PREFLIGHT_SUBSYSTEMS
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(subsystem)}(?![A-Za-z0-9_-])", lowered)
    )
    issue_refs = _swarm_issue_closing_refs(body, resolved_path, repo_slug=repo_slug)
    depends_on = sorted(
        {
            target.strip("`'\" ")
            for match in _SWARM_DEPENDS_ON_RE.finditer(searchable_body)
            for target in re.split(r"[, ]+", match.group("target"))
            if target.strip("`'\" ")
        }
    )
    return {
        "brief": brief_path,
        "file_paths": file_paths,
        "subsystems": subsystems,
        "issue_refs": issue_refs,
        "depends_on": depends_on,
    }


def _swarm_preflight_duplicates(
    lanes: list[dict[str, object]],
    key: str,
) -> list[dict[str, object]]:
    by_value: dict[str, list[str]] = {}
    for lane in lanes:
        brief = lane.get("brief")
        values = lane.get(key)
        if not isinstance(brief, str) or not isinstance(values, list):
            continue
        for value in values:
            if isinstance(value, str):
                by_value.setdefault(value, []).append(brief)
    return [
        {"value": value, "briefs": sorted(briefs)}
        for value, briefs in sorted(by_value.items())
        if len(set(briefs)) > 1
    ]


def _swarm_preflight_plan(
    briefs: tuple[str, ...],
    *,
    repo_root: Path,
) -> dict[str, object]:
    repo_slug = _swarm_github_repo_slug(repo_root)
    lanes = [
        _swarm_preflight_lane(brief_path, repo_root=repo_root, repo_slug=repo_slug)
        for brief_path in briefs
    ]
    duplicate_file_paths = _swarm_preflight_duplicates(lanes, "file_paths")
    shared_subsystems = _swarm_preflight_duplicates(lanes, "subsystems")
    duplicate_issue_refs = _swarm_preflight_duplicates(lanes, "issue_refs")
    warning_count = (
        len(duplicate_file_paths) + len(shared_subsystems) + len(duplicate_issue_refs)
    )
    return {
        "schema_version": SWARM_PREFLIGHT_SCHEMA_VERSION,
        "lane_count": len(briefs),
        "lanes": lanes,
        "overlaps": {
            "file_paths": duplicate_file_paths,
            "subsystems": shared_subsystems,
            "issue_refs": duplicate_issue_refs,
        },
        "warning_count": warning_count,
        "has_warnings": warning_count > 0,
    }


def _format_swarm_preflight_warning(plan: dict[str, object]) -> str | None:
    if not plan.get("has_warnings"):
        return None
    overlaps = plan.get("overlaps")
    if not isinstance(overlaps, dict):
        return None
    parts: list[str] = []
    labels = {
        "file_paths": "file path",
        "subsystems": "subsystem",
        "issue_refs": "issue ref",
    }
    for key, label in labels.items():
        entries = overlaps.get(key)
        if isinstance(entries, list) and entries:
            values = [entry.get("value") for entry in entries if isinstance(entry, dict)]
            display = ", ".join(str(value) for value in values[:3] if isinstance(value, str))
            if len(values) > 3:
                display += f", +{len(values) - 3} more"
            parts.append(f"{len(entries)} {label} overlap(s): {display}")
    if not parts:
        return None
    return "[conductor] swarm preflight overlap warning: " + "; ".join(parts)


def _format_swarm_preflight_plan(plan: dict[str, object]) -> list[str]:
    lines = [
        f"swarm preflight: {plan.get('lane_count', 0)} lane(s), "
        f"{plan.get('warning_count', 0)} warning(s)"
    ]
    overlaps = plan.get("overlaps")
    if not isinstance(overlaps, dict):
        return lines
    labels = {
        "file_paths": "file paths",
        "subsystems": "subsystems",
        "issue_refs": "issue refs",
    }
    for key, label in labels.items():
        entries = overlaps.get(key)
        if not isinstance(entries, list) or not entries:
            continue
        lines.append(f"{label}:")
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            value = entry.get("value")
            briefs = entry.get("briefs")
            if isinstance(value, str) and isinstance(briefs, list):
                lines.append(f"  - {value}: {', '.join(str(brief) for brief in briefs)}")
    return lines


def _run_swarm_git(
    args: list[str],
    *,
    cwd: str | Path | None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=SWARM_GIT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise RuntimeError(
            f"`git {' '.join(args)}` timed out after {SWARM_GIT_TIMEOUT_SEC:.0f}s "
            f"in {cwd}"
        ) from e
    except OSError as e:
        raise RuntimeError(f"`git {' '.join(args)}` failed to start in {cwd}: {e}") from e
    if check and result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"`git {' '.join(args)}` failed in {cwd}{suffix}")
    return result


def _swarm_repo_root() -> Path:
    return Path(_run_swarm_git(["rev-parse", "--show-toplevel"], cwd=None).stdout.strip())


def _swarm_commit_count(worktree: Path, base_branch: str) -> int:
    output = _run_swarm_git(
        ["rev-list", "--count", f"{base_branch}..HEAD"],
        cwd=worktree,
    ).stdout.strip()
    try:
        return int(output)
    except ValueError as e:
        raise RuntimeError(f"unexpected git rev-list output in {worktree}: {output!r}") from e


def _swarm_dirty_paths(worktree: Path) -> list[str]:
    output = _run_swarm_git(["status", "--porcelain"], cwd=worktree).stdout
    return [line for line in output.splitlines() if line.strip()]


def _swarm_commit_dirty_worktree(
    worktree: Path,
    *,
    brief_path: str,
    body: str,
) -> bool:
    _run_swarm_git(["add", "-A"], cwd=worktree)
    staged = _run_swarm_git(["diff", "--cached", "--quiet"], cwd=worktree, check=False)
    if staged.returncode == 0:
        return False
    if staged.returncode != 1:
        detail = (staged.stderr or staged.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"`git diff --cached --quiet` failed in {worktree}{suffix}")
    _run_swarm_git(
        [
            "commit",
            "-m",
            _swarm_owned_commit_subject(brief_path),
            "-m",
            _swarm_owned_commit_body(brief_path, body),
        ],
        cwd=worktree,
    )
    return True


def _swarm_owned_commit_subject(brief_path: str) -> str:
    stem = Path(brief_path).stem or "task"
    cleaned = re.sub(r"[^A-Za-z0-9._ -]+", "-", stem).strip(" ._-") or "task"
    return f"swarm: {cleaned}"[:72]


def _swarm_owned_commit_body(brief_path: str, body: str) -> str:
    brief = body.strip()
    max_body_chars = 2_000
    if len(brief) > max_body_chars:
        brief = f"{brief[:max_body_chars].rstrip()}\n\n[brief truncated]"
    return f"Conductor swarm task: {brief_path}\n\n{brief}"


def _swarm_changed_files(worktree: Path, base_ref: str) -> list[str]:
    output = _run_swarm_git(
        ["diff", "--name-only", f"{base_ref}...HEAD"],
        cwd=worktree,
    ).stdout
    return sorted(line for line in output.splitlines() if line.strip())


def _swarm_unmerged_files(worktree: Path) -> list[str]:
    output = _run_swarm_git(
        ["diff", "--name-only", "--diff-filter=U"],
        cwd=worktree,
        check=False,
    ).stdout
    return sorted({line for line in output.splitlines() if line.strip()})


def _remove_swarm_worktree(worktree: Path, *, repo_root: Path) -> None:
    if not worktree.exists():
        return
    with SWARM_WORKTREE_LOCK:
        _run_swarm_git(["worktree", "remove", str(worktree)], cwd=repo_root)


def _swarm_branch_worktrees(branch: str, *, repo_root: Path) -> list[str]:
    output = _run_swarm_git(["worktree", "list", "--porcelain"], cwd=repo_root).stdout
    attached: list[str] = []
    current_path: str | None = None
    for raw_line in output.splitlines():
        if not raw_line:
            current_path = None
            continue
        key, _, value = raw_line.partition(" ")
        if key == "worktree":
            current_path = value
        elif key == "branch" and value == f"refs/heads/{branch}" and current_path is not None:
            attached.append(current_path)
    return attached


def _delete_shipped_swarm_branch(
    branch: str,
    *,
    repo_root: Path,
    merge_sha: str | None,
) -> str | None:
    if not branch.startswith(f"{SWARM_BRANCH_PREFIX}/"):
        return f"refusing to delete non-swarm branch {branch!r}"
    if merge_sha is None:
        return f"preserving local branch {branch!r}: shipped PR has no merge commit SHA"

    branch_ref = f"refs/heads/{branch}"
    branch_exists = _run_swarm_git(
        ["show-ref", "--verify", "--quiet", branch_ref],
        cwd=repo_root,
        check=False,
    )
    if branch_exists.returncode != 0:
        return None

    try:
        branch_tree = _run_swarm_git(
            ["rev-parse", f"{branch_ref}^{{tree}}"],
            cwd=repo_root,
        ).stdout.strip()
        merge_tree = _run_swarm_git(
            ["rev-parse", f"{merge_sha}^{{tree}}"],
            cwd=repo_root,
        ).stdout.strip()
    except RuntimeError as e:
        return f"preserving local branch {branch!r}: could not verify shipped tree: {e}"

    if branch_tree != merge_tree:
        return (
            f"preserving local branch {branch!r}: branch tree {branch_tree} does not "
            f"match merge commit {merge_sha} tree {merge_tree}"
        )

    attached_worktrees = _swarm_branch_worktrees(branch, repo_root=repo_root)
    if attached_worktrees:
        return (
            f"preserving local branch {branch!r}: still checked out in "
            + ", ".join(attached_worktrees)
        )

    try:
        _run_swarm_git(["branch", "-D", branch], cwd=repo_root)
    except RuntimeError as e:
        return f"could not delete local branch {branch!r} after shipped cleanup: {e}"
    return None


def _extract_pr_url(output: str) -> str | None:
    match = re.search(r"https://github\.com/[^\s]+/pull/\d+", output)
    return match.group(0) if match else None


def _swarm_pr_inspection(
    cwd: Path,
    pr_url: str | None,
) -> tuple[str | None, str | None, str | None]:
    if pr_url is None:
        return None, None, None
    pr_number = pr_url.rstrip("/").rsplit("/", 1)[-1]
    if not pr_number.isdigit():
        return None, None, f"could not parse PR number from {pr_url}"
    try:
        result = subprocess.run(
            [
                "gh",
                "pr",
                "view",
                pr_number,
                "--json",
                "state,mergeCommit",
            ],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=SWARM_GH_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        return (
            None,
            None,
            f"gh pr view timed out after {SWARM_GH_TIMEOUT_SEC:.0f}s for {pr_url}: {e}",
        )
    except OSError as e:
        return None, None, f"could not inspect PR state for {pr_url}: {e}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        return None, None, f"gh pr view failed for {pr_url}: {detail}"
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        return None, None, f"gh pr view returned invalid JSON for {pr_url}: {e}"
    if not isinstance(payload, dict):
        return None, None, f"gh pr view returned non-object JSON for {pr_url}"
    merge_commit = payload.get("mergeCommit")
    merge_sha = None
    if isinstance(merge_commit, dict):
        oid = merge_commit.get("oid")
        if isinstance(oid, str) and oid:
            merge_sha = oid
    state = payload.get("state")
    return (state if isinstance(state, str) and state else None), merge_sha, None


def _swarm_pr_merge_sha(worktree: Path, pr_url: str | None) -> str | None:
    _merge_status, merge_sha, warning = _swarm_pr_inspection(worktree, pr_url)
    if warning is not None:
        click.echo(f"[conductor] warning: {warning}", err=True)
    return merge_sha


def _coerce_swarm_ship_outcome(value: object) -> SwarmShipOutcome:
    if isinstance(value, SwarmShipOutcome):
        return value
    if isinstance(value, tuple) and len(value) == 3:
        status, pr_url, detail = value
        return SwarmShipOutcome(
            str(status),
            pr_url if isinstance(pr_url, str) else None,
            detail if isinstance(detail, str) else None,
        )
    raise TypeError(f"invalid swarm ship result: {value!r}")


def _ensure_swarm_branch_checked_out(worktree: Path, branch: str) -> None:
    current = _run_swarm_git(
        ["branch", "--show-current"],
        cwd=worktree,
    ).stdout.strip()
    if current == branch:
        return
    if current:
        raise RuntimeError(
            f"swarm worktree is on {current!r}, expected {branch!r}; "
            "refusing to retarget a named branch"
        )
    head_sha = _run_swarm_git(["rev-parse", "HEAD"], cwd=worktree).stdout.strip()
    _run_swarm_git(["branch", "-f", branch, head_sha], cwd=worktree)
    _run_swarm_git(["checkout", branch], cwd=worktree)


def _require_swarm_branch_checked_out(worktree: Path, branch: str, *, selector: str) -> None:
    current = _run_swarm_git(
        ["branch", "--show-current"],
        cwd=worktree,
    ).stdout.strip()
    if current == branch:
        return
    actual = current or "detached HEAD"
    raise click.ClickException(
        f"swarm task {selector} worktree is on {actual!r}, expected {branch!r}; "
        "retry-ship refuses to retarget the saved branch"
    )


_SWARM_VALIDATION_COMMAND_RE = re.compile(
    r"(?im)^\s*(?:failed command|failed check|validation command|command)\s*:\s*(?P<cmd>.+?)\s*$"
)
_SWARM_RUN_HINT_RE = re.compile(r"(?im)^\s*(?:run|retry|try)\s*:?\s*`(?P<cmd>[^`]+)`")
_SWARM_KNOWN_CHECK_RE = re.compile(
    r"(?im)^\s*(?:\$ )?(?P<cmd>"
    r"bash scripts/touchstone-run\.sh validate\b[^\n]*|"
    r"uv run (?:pytest|ruff|mypy|pyright)\b[^\n]*|"
    r"(?:pytest|ruff|mypy|pyright)\b[^\n]*|"
    r"(?:npm|pnpm|yarn) (?:test|run (?:test|lint|typecheck|build))\b[^\n]*|"
    r"cargo (?:test|check|fmt -- --check|clippy)\b[^\n]*|"
    r"go test\b[^\n]*"
    r")\s*$"
)


def _swarm_output_tail(output: str, *, max_lines: int = 40, max_chars: int = 4000) -> str:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    tail = "\n".join(lines[-max_lines:]).strip()
    if len(tail) <= max_chars:
        return tail
    return tail[-max_chars:].lstrip()


def _swarm_validation_command_from_output(output: str) -> str | None:
    for pattern in (
        _SWARM_VALIDATION_COMMAND_RE,
        _SWARM_RUN_HINT_RE,
        _SWARM_KNOWN_CHECK_RE,
    ):
        match = pattern.search(output)
        if match is None:
            continue
        command = match.group("cmd").strip()
        if command:
            return command
    return None


def _swarm_validation_failure_from_output(
    *,
    output: str,
    worktree: Path,
    branch: str,
    push_command: str,
) -> dict[str, object] | None:
    lowered = output.lower()
    validation_signal = (
        "pre-push" in lowered
        or "prepush" in lowered
        or ("validation" in lowered and ("failed" in lowered or "error" in lowered))
        or ("touchstone" in lowered and ("failed" in lowered or "error" in lowered))
    )
    if not validation_signal:
        return None
    failed_command = _swarm_validation_command_from_output(output) or push_command
    retry_command = f"cd {shlex.quote(str(worktree))} && {failed_command}"
    recovery_context = (
        "Validation failed while shipping a swarm branch. Fix the check output, "
        "commit any required changes, then rerun the retry command."
    )
    provider_recovery_command = (
        "conductor exec --with codex "
        f"--cwd {shlex.quote(str(worktree))} "
        "--brief "
        + shlex.quote(
            f"{recovery_context}\n\nBranch: {branch}\n"
            f"Failed check: {failed_command}\n\nOutput tail:\n"
            f"{_swarm_output_tail(output)}"
        )
    )
    return {
        "schema_version": 1,
        "failed_command": failed_command,
        "failed_check": failed_command,
        "output_tail": _swarm_output_tail(output),
        "retry_command": retry_command,
        "suggested_next_command": retry_command,
        "recovery_command": provider_recovery_command,
        "recovery_context": recovery_context,
    }


def _ship_swarm_pr(
    worktree: Path,
    *,
    base_branch: str,
    branch: str,
    auto_merge: bool,
) -> SwarmShipOutcome:
    repo_root = _swarm_repo_root()
    script = repo_root / "scripts" / "open-pr.sh"
    _ensure_swarm_branch_checked_out(worktree, branch)
    args = ["bash", str(script), "--base", base_branch]
    push_command = f"git push -u origin {shlex.quote(branch)}"
    if auto_merge:
        args.append("--auto-merge")
    try:
        result = subprocess.run(
            args,
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=SWARM_SHIP_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        output = "\n".join(
            part
            for part in (_timeout_output_text(e.stdout), _timeout_output_text(e.stderr))
            if part
        ).strip()
        pr_url = _extract_pr_url(output)
        status = "review-blocked" if auto_merge and pr_url else "failed"
        reason = (
            f"open-pr.sh timed out after {SWARM_SHIP_TIMEOUT_SEC:.0f}s"
            + (f": {output}" if output else "")
        )
        validation_failure = _swarm_validation_failure_from_output(
            output=output,
            worktree=worktree,
            branch=branch,
            push_command=push_command,
        )
        if validation_failure is not None:
            status = SWARM_VALIDATION_FAILED_STATUS
        return SwarmShipOutcome(
            status,
            pr_url,
            reason,
            validation_failure=validation_failure,
        )
    except OSError as e:
        return SwarmShipOutcome("failed", None, f"open-pr.sh failed to start: {e}")
    output = f"{result.stdout}\n{result.stderr}".strip()
    pr_url = _extract_pr_url(output)
    merge_status: str | None = None
    merge_sha: str | None = None
    inspection_warning: str | None = None
    if auto_merge and pr_url is not None:
        merge_status, merge_sha, inspection_warning = _swarm_pr_inspection(repo_root, pr_url)
        if inspection_warning is not None:
            click.echo(f"[conductor] warning: {inspection_warning}", err=True)
    if result.returncode != 0:
        if auto_merge and pr_url is not None and merge_status == "MERGED":
            return SwarmShipOutcome(
                "shipped",
                pr_url,
                merge_sha,
                merge_status=merge_status,
                inspection_warning=inspection_warning,
            )
        status = "review-blocked" if auto_merge and pr_url else "failed"
        reason = output or f"open-pr.sh exited {result.returncode}"
        validation_failure = _swarm_validation_failure_from_output(
            output=output,
            worktree=worktree,
            branch=branch,
            push_command=push_command,
        )
        if validation_failure is not None:
            status = SWARM_VALIDATION_FAILED_STATUS
        return SwarmShipOutcome(
            status,
            pr_url,
            reason,
            merge_status=merge_status,
            inspection_warning=inspection_warning,
            validation_failure=validation_failure,
        )
    status = "shipped" if auto_merge else "pushed-not-merged"
    return SwarmShipOutcome(
        status,
        pr_url,
        merge_sha,
        merge_status=merge_status,
        inspection_warning=inspection_warning,
    )


def _timeout_output_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _swarm_failed_statuses() -> set[str]:
    return {"failed", SWARM_CONFLICT_STATUS, SWARM_VALIDATION_FAILED_STATUS}


def _swarm_summary(results: list[SwarmTaskResult]) -> dict[str, object]:
    shipped_count = sum(1 for result in results if result.status == "shipped")
    failed_count = sum(1 for result in results if result.status in _swarm_failed_statuses())
    no_changes_count = sum(1 for result in results if result.status == "no-changes")
    ok = all(result.status in SWARM_SUCCESS_STATUSES for result in results)
    return {
        "tasks": [
            result.to_json(include_provider_details=False)
            for result in results
        ],
        "ok": ok,
        "shipped_count": shipped_count,
        "failed_count": failed_count,
        "no_changes_count": no_changes_count,
    }


def _swarm_summary_from_task_records(tasks: list[dict[str, object]]) -> dict[str, object]:
    shipped_count = sum(1 for task in tasks if task.get("status") == "shipped")
    failed_count = sum(1 for task in tasks if task.get("status") in _swarm_failed_statuses())
    no_changes_count = sum(1 for task in tasks if task.get("status") == "no-changes")
    ok = all(task.get("status") in SWARM_SUCCESS_STATUSES for task in tasks)
    public_tasks = []
    for task in tasks:
        public_tasks.append(
            {
                key: value
                for key, value in task.items()
                if key
                in {
                    "brief",
                    "branch",
                    "worktree",
                    "status",
                    "commits",
                    "pr_url",
                    "merge_sha",
                    "duration_ms",
                    "failure_reason",
                }
            }
        )
    return {
        "tasks": public_tasks,
        "ok": ok,
        "shipped_count": shipped_count,
        "failed_count": failed_count,
        "no_changes_count": no_changes_count,
    }


def _as_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    return 0


def _swarm_metrics_from_task_records(
    tasks: list[dict[str, object]],
    *,
    duration_ms: int | None,
) -> dict[str, object]:
    status_counts: dict[str, int] = {}
    total_commits = 0
    for task in tasks:
        status = task.get("status")
        if isinstance(status, str) and status:
            status_counts[status] = status_counts.get(status, 0) + 1
        total_commits += _as_nonnegative_int(task.get("commits"))

    shipped_count = status_counts.get("shipped", 0)
    no_changes_count = status_counts.get("no-changes", 0)
    completed_count = shipped_count + no_changes_count
    throughput = 0.0
    if duration_ms is not None and duration_ms > 0:
        throughput = completed_count / (duration_ms / 3_600_000)

    return {
        "schema_version": SWARM_METRICS_SCHEMA_VERSION,
        "total_tasks": len(tasks),
        "shipped_count": shipped_count,
        "failed_count": status_counts.get("failed", 0)
        + status_counts.get(SWARM_CONFLICT_STATUS, 0)
        + status_counts.get(SWARM_VALIDATION_FAILED_STATUS, 0),
        "provider_failed_count": status_counts.get(SWARM_PROVIDER_FAILED_STATUS, 0),
        "no_changes_count": no_changes_count,
        "review_blocked_count": status_counts.get("review-blocked", 0),
        "needs_human_conflict_resolution_count": status_counts.get(SWARM_CONFLICT_STATUS, 0),
        "pushed_not_merged_count": status_counts.get("pushed-not-merged", 0),
        "duration_ms": duration_ms,
        "per_status_counts": dict(sorted(status_counts.items())),
        "total_commits": total_commits,
        "completed_tasks_per_hour": throughput,
    }


def _is_swarm_provider_failure(error: BaseException | str) -> bool:
    if isinstance(error, ProviderStalledError | ProviderHTTPError):
        return True
    if isinstance(error, ProviderExecutionError):
        return error.status.get("state") != "iteration-cap"
    text = str(error).lower()
    if text.startswith("`git "):
        return False
    return any(signal in text for signal in _SWARM_PROVIDER_TRANSIENT_SIGNALS)


def _swarm_failure_status(error: BaseException | str) -> str:
    if _is_swarm_provider_failure(error):
        return SWARM_PROVIDER_FAILED_STATUS
    return "failed"


def _swarm_initial_task_records(
    briefs: tuple[str, ...],
    *,
    repo_root: Path,
    provider_id: str | None = None,
) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for brief_path in briefs:
        tasks.append(
            {
                "brief": brief_path,
                "branch": _swarm_branch_for_brief(brief_path),
                "worktree": str(_swarm_worktree_for_brief(brief_path, repo_root=repo_root)),
                "status": "pending",
                "commits": 0,
                "pr_url": None,
                "merge_sha": None,
                "merge_status": None,
                "inspection_warning": None,
                "duration_ms": 0,
                "failure_reason": None,
                "cleanup_status": "pending",
                "last_phase": "starting",
                "last_progress_at": None,
                "phase_history": [],
                "provider": None,
                "selected_provider": (
                    None if provider_id is None or provider_id in {"auto", "pool"} else provider_id
                ),
                "fallback_provider": None,
                "fallback_reason": None,
            }
        )
    return tasks


def _swarm_manifest_payload(
    *,
    run_id: str,
    repo_root: Path,
    base_branch: str,
    base_ref: str | None,
    started_at: str,
    ended_at: str | None,
    duration_ms: int | None,
    tasks: list[dict[str, object]],
    preflight: dict[str, object] | None = None,
) -> dict[str, object]:
    shipped_count = sum(1 for task in tasks if task.get("status") == "shipped")
    failed_count = sum(1 for task in tasks if task.get("status") in _swarm_failed_statuses())
    provider_failed_count = sum(
        1 for task in tasks if task.get("status") == SWARM_PROVIDER_FAILED_STATUS
    )
    no_changes_count = sum(1 for task in tasks if task.get("status") == "no-changes")
    ok = bool(tasks) and all(task.get("status") in SWARM_SUCCESS_STATUSES for task in tasks)
    metrics = _swarm_metrics_from_task_records(tasks, duration_ms=duration_ms)
    payload: dict[str, object] = {
        "schema_version": SWARM_MANIFEST_SCHEMA_VERSION,
        "run_id": run_id,
        "repo_root": str(repo_root),
        "base_branch": base_branch,
        "base_ref": base_ref,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_ms": duration_ms,
        "tasks": tasks,
        "ok": ok,
        "shipped_count": shipped_count,
        "failed_count": failed_count,
        "provider_failed_count": provider_failed_count,
        "no_changes_count": no_changes_count,
        "metrics": metrics,
    }
    if preflight is not None:
        payload["preflight"] = preflight
    return payload


def _swarm_list_values(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return sorted(str(item) for item in value if isinstance(item, str) and item)


def _swarm_dict_list(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        return []
    items: list[dict[str, object]] = []
    for item in value:
        if isinstance(item, dict):
            items.append({str(key): val for key, val in item.items()})
    return items


def _swarm_preflight_lane_for_brief(
    preflight: dict[str, object] | None,
    brief: object,
) -> dict[str, object]:
    if not isinstance(brief, str) or not isinstance(preflight, dict):
        return {}
    lanes = preflight.get("lanes")
    if not isinstance(lanes, list):
        return {}
    for lane in lanes:
        if isinstance(lane, dict) and lane.get("brief") == brief:
            return lane
    return {}


def _swarm_dependency_targets(
    task: dict[str, object],
    *,
    preflight: dict[str, object] | None,
) -> set[str]:
    value = task.get("depends_on")
    if value is None:
        value = _swarm_preflight_lane_for_brief(preflight, task.get("brief")).get("depends_on")
    if isinstance(value, str) and value:
        return {value}
    if not isinstance(value, list):
        return set()
    return {str(item) for item in value if isinstance(item, str) and item}


def _swarm_merge_order(
    tasks: list[dict[str, object]],
    *,
    preflight: dict[str, object] | None,
) -> list[int]:
    ready_indexes = [
        index
        for index, task in enumerate(tasks)
        if task.get("status") == "ready-to-ship"
    ]
    remaining = set(ready_indexes)
    ordered: list[int] = []

    def task_tokens(index: int) -> set[str]:
        task = tasks[index]
        return {
            str(value)
            for value in (task.get("brief"), task.get("branch"))
            if isinstance(value, str) and value
        }

    def shared_issue_group(index: int) -> str:
        task = tasks[index]
        lane = _swarm_preflight_lane_for_brief(preflight, task.get("brief"))
        issue_refs = set(_swarm_list_values(lane.get("issue_refs")))
        shared_issues: set[str] = set()
        for other in sorted(remaining):
            if other == index:
                continue
            other_task = tasks[other]
            other_lane = _swarm_preflight_lane_for_brief(
                preflight,
                other_task.get("brief"),
            )
            shared_issues.update(issue_refs.intersection(_swarm_list_values(other_lane.get("issue_refs"))))
        if not shared_issues:
            return ""
        return sorted(shared_issues)[0]

    while remaining:
        blocked_by_deps: set[int] = set()
        for index in remaining:
            deps = _swarm_dependency_targets(tasks[index], preflight=preflight)
            if not deps:
                continue
            for other in remaining:
                if other != index and deps.intersection(task_tokens(other)):
                    blocked_by_deps.add(index)
                    break
        candidates = sorted(remaining - blocked_by_deps)
        if not candidates:
            candidates = sorted(remaining)

        def score(index: int) -> tuple[int, str, int]:
            task = tasks[index]
            changed_files = set(_swarm_list_values(task.get("changed_files")))
            overlaps = 0
            for other in remaining:
                if other == index:
                    continue
                other_task = tasks[other]
                if changed_files.intersection(_swarm_list_values(other_task.get("changed_files"))):
                    overlaps += 1
            issue_group = shared_issue_group(index)
            return (-overlaps, issue_group, index)

        selected = min(candidates, key=score)
        ordered.append(selected)
        remaining.remove(selected)
    return ordered


def _swarm_merge_plan(
    tasks: list[dict[str, object]],
    *,
    preflight: dict[str, object] | None,
) -> dict[str, object]:
    order = _swarm_merge_order(tasks, preflight=preflight)
    plan_lanes: list[dict[str, object]] = []
    for position, index in enumerate(order, start=1):
        task = tasks[index]
        lane = _swarm_preflight_lane_for_brief(preflight, task.get("brief"))
        changed_files = _swarm_list_values(task.get("changed_files"))
        overlaps = sorted(
            {
                str(tasks[other].get("brief"))
                for other in order
                if other != index
                and set(changed_files).intersection(
                    _swarm_list_values(tasks[other].get("changed_files"))
                )
            }
        )
        plan_lanes.append(
            {
                "position": position,
                "task_index": index + 1,
                "brief": task.get("brief"),
                "branch": task.get("branch"),
                "changed_files": changed_files,
                "depends_on": sorted(
                    _swarm_dependency_targets(task, preflight=preflight)
                ),
                "issue_refs": _swarm_list_values(lane.get("issue_refs")),
                "overlaps_with": overlaps,
            }
        )
    return {
        "schema_version": SWARM_MERGE_PLAN_SCHEMA_VERSION,
        "strategy": "dependency-aware-file-overlap-order",
        "generated_from": "changed_files, preflight issue grouping, depends_on",
        "lanes": plan_lanes,
    }


def _write_swarm_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(path)
    finally:
        temp_path.unlink(missing_ok=True)


def _apply_swarm_task_phase(task: dict[str, object], *, phase: str, at: str) -> None:
    task["last_phase"] = phase
    task["last_progress_at"] = at
    if task.get("status") == "pending":
        task["status"] = "running"
    history = task.get("phase_history")
    if not isinstance(history, list):
        history = []
    history.append({"phase": phase, "at": at})
    task["phase_history"] = history[-12:]


def _record_swarm_task_phase(
    *,
    manifest_path: Path,
    brief_path: str,
    phase: str,
) -> None:
    now = _swarm_now_iso()
    try:
        with SWARM_MANIFEST_LOCK:
            payload = _read_swarm_manifest(manifest_path)
            tasks = _swarm_manifest_tasks(payload, path=manifest_path)
            for task in tasks:
                if task.get("brief") != brief_path:
                    continue
                _apply_swarm_task_phase(task, phase=phase, at=now)
                _write_swarm_manifest(manifest_path, payload)
                return
            click.echo(
                "[conductor] warning: could not record swarm phase "
                f"{phase!r}; no task matched brief {brief_path!r} in {manifest_path}",
                err=True,
            )
    except Exception as e:
        click.echo(
            "[conductor] warning: could not record swarm phase "
            f"{phase!r} for {brief_path!r} in {manifest_path}: {e}",
            err=True,
        )


def _swarm_manifest_task_records(results: list[SwarmTaskResult]) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for result in results:
        task = result.to_json()
        task.setdefault("merge_status", None)
        task.setdefault("inspection_warning", None)
        task.setdefault("cleanup_warning", None)
        task["cleanup_status"] = "removed" if not Path(result.worktree).exists() else "preserved"
        tasks.append(task)
    return tasks


def _swarm_conflict_recovery(
    *,
    worktree: Path,
    branch: str,
    manifest_path: Path,
    selector: str,
    conflicted_files: list[str],
) -> dict[str, object]:
    return {
        "status": SWARM_CONFLICT_STATUS,
        "conflicted_files": conflicted_files,
        "recovery_commands": [
            f"cd {shlex.quote(str(worktree))}",
            "git status --short --branch",
            "git diff --name-only --diff-filter=U",
            "# resolve conflicts, then: git add <resolved-files> && git rebase --continue",
            (
                "conductor swarm retry-ship "
                f"{shlex.quote(str(manifest_path))} {shlex.quote(selector)}"
            ),
        ],
        "abort_command": f"cd {shlex.quote(str(worktree))} && git rebase --abort",
        "branch": branch,
    }


def _swarm_mark_conflict(
    task: dict[str, object],
    *,
    worktree: Path,
    branch: str,
    manifest_path: Path,
    selector: str,
    reason: str,
) -> None:
    conflicted_files = _swarm_unmerged_files(worktree)
    task["status"] = SWARM_CONFLICT_STATUS
    task["failure_reason"] = reason
    task["cleanup_status"] = "preserved"
    task["conflict_state"] = _swarm_conflict_recovery(
        worktree=worktree,
        branch=branch,
        manifest_path=manifest_path,
        selector=selector,
        conflicted_files=conflicted_files,
    )


def _swarm_rebase_ready_task(
    task: dict[str, object],
    *,
    base_ref: str,
    manifest_path: Path,
    selector: str,
) -> bool:
    branch = task.get("branch")
    raw_worktree = task.get("worktree")
    if not isinstance(branch, str) or not branch:
        task["status"] = "failed"
        task["failure_reason"] = "swarm merge orchestration missing branch"
        return False
    if not isinstance(raw_worktree, str) or not raw_worktree:
        task["status"] = "failed"
        task["failure_reason"] = "swarm merge orchestration missing worktree"
        return False
    worktree = Path(raw_worktree)
    if not worktree.exists():
        task["status"] = "failed"
        task["failure_reason"] = f"swarm merge orchestration missing worktree: {worktree}"
        return False
    try:
        _require_swarm_branch_checked_out(worktree, branch, selector=selector)
        dirty_paths = _swarm_dirty_paths(worktree)
    except RuntimeError as e:
        task["status"] = "failed"
        task["failure_reason"] = str(e)
        return False
    except click.ClickException as e:
        task["status"] = "failed"
        task["failure_reason"] = e.message
        return False
    if dirty_paths:
        task["status"] = "failed"
        task["failure_reason"] = (
            "swarm merge orchestration found uncommitted changes before rebase: "
            + ", ".join(path[:200] for path in dirty_paths[:10])
        )
        return False
    result = _run_swarm_git(["rebase", base_ref], cwd=worktree, check=False)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        _swarm_mark_conflict(
            task,
            worktree=worktree,
            branch=branch,
            manifest_path=manifest_path,
            selector=selector,
            reason=(
                f"automatic rebase onto {base_ref} conflicted"
                + (f": {detail}" if detail else "")
            ),
        )
        return False
    try:
        task["commits"] = _swarm_commit_count(worktree, base_ref)
        task["changed_files"] = _swarm_changed_files(worktree, base_ref)
    except RuntimeError as e:
        task["status"] = "failed"
        task["failure_reason"] = str(e)
        return False
    if task["commits"] == 0:
        task["status"] = "no-changes"
        task["cleanup_status"] = "preserved"
        return False
    return True


def _orchestrate_swarm_auto_merge(
    *,
    tasks: list[dict[str, object]],
    repo_root: Path,
    base_branch: str,
    manifest_path: Path,
    preflight: dict[str, object] | None,
    phase_recorder: Callable[[str, str], None] | None = None,
) -> dict[str, object] | None:
    def mark_phase(task: dict[str, object], phase: str) -> None:
        _apply_swarm_task_phase(task, phase=phase, at=_swarm_now_iso())
        brief = task.get("brief")
        if phase_recorder is not None and isinstance(brief, str):
            phase_recorder(brief, phase)

    for task in tasks:
        if task.get("status") != "ready-to-ship":
            continue
        raw_worktree = task.get("worktree")
        base_ref = _run_swarm_git(
            ["rev-parse", "--verify", base_branch],
            cwd=repo_root,
        ).stdout.strip()
        if isinstance(raw_worktree, str) and raw_worktree:
            worktree = Path(raw_worktree)
            if worktree.exists():
                try:
                    task["changed_files"] = _swarm_changed_files(worktree, base_ref)
                except RuntimeError as e:
                    task["status"] = "failed"
                    task["failure_reason"] = str(e)
    merge_plan = _swarm_merge_plan(tasks, preflight=preflight)
    order: list[int] = []
    for lane in _swarm_dict_list(merge_plan.get("lanes")):
        task_index = lane.get("task_index")
        if isinstance(task_index, int):
            order.append(task_index - 1)
    for index in order:
        task = tasks[index]
        if task.get("status") != "ready-to-ship":
            continue
        selector = str(index + 1)
        mark_phase(task, "reviewing")
        current_base = _run_swarm_git(
            ["rev-parse", "--verify", base_branch],
            cwd=repo_root,
        ).stdout.strip()
        if not _swarm_rebase_ready_task(
            task,
            base_ref=current_base,
            manifest_path=manifest_path,
            selector=selector,
        ):
            continue
        branch = _swarm_task_string(
            task,
            "branch",
            path=manifest_path,
            selector=selector,
        )
        worktree = Path(
            _swarm_task_string(
                task,
                "worktree",
                path=manifest_path,
                selector=selector,
            )
        )
        mark_phase(task, "shipping")
        ship_outcome = _coerce_swarm_ship_outcome(
            _ship_swarm_pr(
                worktree,
                base_branch=base_branch,
                branch=branch,
                auto_merge=True,
            )
        )
        shipped_status = ship_outcome.status
        shipped_pr_url = ship_outcome.pr_url
        shipped_detail = ship_outcome.detail
        task["status"] = shipped_status
        task["pr_url"] = shipped_pr_url
        task["merge_status"] = ship_outcome.merge_status
        task["inspection_warning"] = ship_outcome.inspection_warning
        task["validation_failure"] = ship_outcome.validation_failure
        if shipped_status == "shipped":
            task["merge_sha"] = shipped_detail
            task["failure_reason"] = None
            try:
                _remove_swarm_worktree(worktree, repo_root=repo_root)
                cleanup_warning = _delete_shipped_swarm_branch(
                    branch,
                    repo_root=repo_root,
                    merge_sha=shipped_detail,
                )
                if cleanup_warning is not None:
                    click.echo(f"[conductor] warning: {cleanup_warning}", err=True)
                    task["failure_reason"] = cleanup_warning
            except RuntimeError as e:
                warning = f"cleanup failed after merge: {e}"
                task["cleanup_warning"] = warning
                click.echo(f"[conductor] warning: {warning}", err=True)
                if ship_outcome.merge_status != "MERGED":
                    task["status"] = "failed"
                    task["failure_reason"] = warning
            updated_base = _run_swarm_git(
                ["rev-parse", "--verify", base_branch],
                cwd=repo_root,
            ).stdout.strip()
            for remaining_index in order:
                if remaining_index == index:
                    continue
                remaining_task = tasks[remaining_index]
                if remaining_task.get("status") != "ready-to-ship":
                    continue
                _swarm_rebase_ready_task(
                    remaining_task,
                    base_ref=updated_base,
                    manifest_path=manifest_path,
                    selector=str(remaining_index + 1),
                )
        elif shipped_status in {"failed", "review-blocked", SWARM_VALIDATION_FAILED_STATUS}:
            task["failure_reason"] = shipped_detail
        task["cleanup_status"] = "removed" if not worktree.exists() else "preserved"

    return merge_plan if merge_plan["lanes"] else None


def _read_swarm_manifest(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise click.ClickException(f"could not read swarm manifest {path}: {e}") from e
    except json.JSONDecodeError as e:
        raise click.ClickException(f"invalid swarm manifest JSON at {path}: {e}") from e
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"invalid swarm manifest at {path}: top-level value is not an object"
        )
    return payload


def _swarm_manifest_type_error(path: Path, location: str, expected: str) -> click.ClickException:
    return click.ClickException(
        f"invalid swarm manifest at {path}: {location} is not {expected}"
    )


def _swarm_manifest_string(
    record: dict[str, object],
    key: str,
    *,
    path: Path,
    location: str,
    default: str = "",
    required: bool = False,
) -> str:
    value = record.get(key)
    if value is None:
        if required:
            raise click.ClickException(
                f"invalid swarm manifest at {path}: {location}.{key} is missing"
            )
        return default
    if not isinstance(value, str):
        raise _swarm_manifest_type_error(path, f"{location}.{key}", "a string")
    if required and not value:
        raise click.ClickException(f"invalid swarm manifest at {path}: {location}.{key} is empty")
    return value


def _swarm_manifest_int(
    record: dict[str, object],
    key: str,
    *,
    path: Path,
    location: str,
    default: int | None = None,
    required: bool = False,
) -> int | None:
    value = record.get(key)
    if value is None:
        if required:
            raise click.ClickException(
                f"invalid swarm manifest at {path}: {location}.{key} is missing"
            )
        return default
    if not isinstance(value, int) or isinstance(value, bool):
        raise _swarm_manifest_type_error(path, f"{location}.{key}", "an int")
    return value


def _swarm_manifest_string_list(
    record: dict[str, object],
    key: str,
    *,
    path: Path,
    location: str,
) -> list[str]:
    value = record.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise _swarm_manifest_type_error(path, f"{location}.{key}", "a string list")
    strings: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise _swarm_manifest_type_error(
                path,
                f"{location}.{key}[{index}]",
                "a string",
            )
        strings.append(item)
    return strings


def _swarm_manifest_dict(
    record: dict[str, object],
    key: str,
    *,
    path: Path,
    location: str,
) -> dict[str, object] | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise _swarm_manifest_type_error(path, f"{location}.{key}", "an object")
    return {str(item_key): item_value for item_key, item_value in value.items()}


def _swarm_manifest_dict_list(
    record: dict[str, object],
    key: str,
    *,
    path: Path,
    location: str,
    required: bool = False,
) -> list[dict[str, object]]:
    value = record.get(key)
    if value is None:
        if required:
            raise click.ClickException(
                f"invalid swarm manifest at {path}: {location}.{key} is missing"
            )
        return []
    if not isinstance(value, list):
        raise _swarm_manifest_type_error(path, f"{location}.{key}", "a list")
    items: list[dict[str, object]] = []
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise _swarm_manifest_type_error(
                path,
                f"{location}.{key}[{index}]",
                "an object",
            )
        items.append({str(item_key): item_value for item_key, item_value in item.items()})
    return items


def _latest_swarm_manifest_path(repo_root: Path) -> Path:
    runs_dir = _swarm_runs_dir(repo_root)
    manifests = sorted(runs_dir.glob("*.json"), key=lambda path: path.stem, reverse=True)
    if not manifests:
        raise click.ClickException(f"no swarm manifests found in {runs_dir}")
    return manifests[0]


def _resolve_swarm_manifest_path(run_id_or_path: str | None, *, latest: bool) -> Path:
    if latest:
        return _latest_swarm_manifest_path(_swarm_repo_root())
    if run_id_or_path is None:
        raise click.UsageError("pass --latest or a run id/path.")
    candidate = Path(run_id_or_path).expanduser()
    if candidate.exists():
        return candidate
    repo_root = _swarm_repo_root()
    run_id_path = _swarm_runs_dir(repo_root) / f"{run_id_or_path.removesuffix('.json')}.json"
    if run_id_path.exists():
        return run_id_path
    raise click.ClickException(f"swarm manifest not found: {run_id_or_path}")


def _swarm_manifest_tasks(payload: dict[str, object], *, path: Path) -> list[dict[str, object]]:
    return _swarm_manifest_dict_list(
        payload,
        "tasks",
        path=path,
        location="manifest",
        required=True,
    )


def _select_swarm_manifest_task(
    tasks: list[dict[str, object]],
    selector: str,
    *,
    path: Path,
) -> tuple[int, dict[str, object]]:
    if selector.isdecimal():
        index = int(selector) - 1
        if 0 <= index < len(tasks):
            return index, tasks[index]
        raise click.ClickException(
            f"swarm task selector {selector!r} is out of range; "
            f"expected a 1-based index from 1 to {len(tasks)}"
        )

    matches = [
        (index, task)
        for index, task in enumerate(tasks)
        if _swarm_manifest_string(
            task,
            "branch",
            path=path,
            location=f"tasks[{index}]",
        )
        == selector
        or _swarm_manifest_string(
            task,
            "brief",
            path=path,
            location=f"tasks[{index}]",
        )
        == selector
    ]
    if not matches:
        raise click.ClickException(
            f"swarm task selector {selector!r} matched no task; use a 1-based index, "
            "exact branch, or exact brief path from the manifest"
        )
    if len(matches) > 1:
        raise click.ClickException(f"swarm task selector {selector!r} matched multiple tasks")
    return matches[0]


def _swarm_task_string(
    task: dict[str, object],
    key: str,
    *,
    path: Path,
    selector: str,
) -> str:
    try:
        return _swarm_manifest_string(
            task,
            key,
            path=path,
            location=f"tasks[{int(selector) - 1}]",
            required=True,
        )
    except ValueError:
        return _swarm_manifest_string(
            task,
            key,
            path=path,
            location=f"task {selector!r}",
            required=True,
        )


def _swarm_worktree_branch_state(worktree: Path) -> str:
    current = _run_swarm_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=worktree).stdout.strip()
    if current != "HEAD":
        return current
    head_sha = _run_swarm_git(["rev-parse", "--short", "HEAD"], cwd=worktree).stdout.strip()
    return f"detached at {head_sha}"


def _swarm_readonly_worktree_state(worktree: Path | None) -> tuple[bool, str, str]:
    if worktree is None or not worktree.exists():
        return False, "unknown (worktree missing)", "unknown (worktree missing)"
    try:
        dirty_paths = _swarm_dirty_paths(worktree)
        dirty = "yes" if dirty_paths else "no"
        if dirty_paths:
            dirty += f" ({', '.join(path[:200] for path in dirty_paths[:10])})"
    except RuntimeError as e:
        dirty = f"unknown ({e})"
    try:
        branch_state = _swarm_worktree_branch_state(worktree)
    except RuntimeError as e:
        branch_state = f"unknown ({e})"
    return True, dirty, branch_state


def _format_swarm_resume(
    *,
    payload: dict[str, object],
    path: Path,
    task_index: int,
    task: dict[str, object],
) -> list[str]:
    selector = str(task_index + 1)
    task_location = f"tasks[{task_index}]"
    run_id = _swarm_manifest_string(
        payload,
        "run_id",
        path=path,
        location="manifest",
        default=path.stem,
    )
    brief = _swarm_manifest_string(task, "brief", path=path, location=task_location)
    branch = _swarm_manifest_string(task, "branch", path=path, location=task_location)
    worktree_value = _swarm_manifest_string(task, "worktree", path=path, location=task_location)
    worktree = Path(worktree_value) if worktree_value else None
    exists, dirty, branch_state = _swarm_readonly_worktree_state(worktree)
    worktree_display = str(worktree) if worktree is not None else ""
    commits = _swarm_manifest_int(task, "commits", path=path, location=task_location, default=0)
    assert commits is not None
    retry_available = exists and commits > 0
    retry_command = (
        f"conductor swarm retry-ship {shlex.quote(str(path))} {shlex.quote(selector)}"
    )
    provider_display = (
        _swarm_manifest_string(task, "provider", path=path, location=task_location)
        or _swarm_manifest_string(task, "selected_provider", path=path, location=task_location)
        or _swarm_manifest_string(payload, "provider", path=path, location="manifest")
        or "auto"
    )
    rerun_parts = ["conductor", "swarm", "--provider", "codex"]
    base_branch = _swarm_manifest_string(payload, "base_branch", path=path, location="manifest")
    if base_branch:
        rerun_parts.extend(["--base-branch", base_branch])
    if brief:
        rerun_parts.extend(["--brief", brief])
    rerun_command = " ".join(shlex.quote(part) for part in rerun_parts)

    lines = [
        f"swarm resume {run_id} task {task_index + 1}",
        f"manifest: {path}",
        f"run id: {run_id}",
        f"task index: {task_index + 1}",
        "status: "
        + _swarm_manifest_string(
            task,
            "status",
            path=path,
            location=task_location,
            default="unknown",
        ),
        f"provider: {provider_display}",
        "fallback provider: "
        + _swarm_manifest_string(task, "fallback_provider", path=path, location=task_location),
        "fallback reason: "
        + _swarm_manifest_string(task, "fallback_reason", path=path, location=task_location),
        f"brief: {brief}",
        f"branch: {branch}",
        f"worktree: {worktree_display}",
        f"commits: {commits}",
        "pr url: " + _swarm_manifest_string(task, "pr_url", path=path, location=task_location),
        "failure reason: "
        + _swarm_manifest_string(task, "failure_reason", path=path, location=task_location),
        "base: {branch}@{ref}".format(
            branch=base_branch,
            ref=_swarm_manifest_string(payload, "base_ref", path=path, location="manifest"),
        ),
        f"worktree exists: {'yes' if exists else 'no'}",
        f"worktree dirty: {dirty}",
        f"worktree branch: {branch_state}",
        "suggested commands:",
    ]
    validation_failure = _swarm_manifest_dict(
        task,
        "validation_failure",
        path=path,
        location=task_location,
    )
    if validation_failure is not None:
        failed_check = _swarm_manifest_string(
            validation_failure,
            "failed_check",
            path=path,
            location=f"{task_location}.validation_failure",
        ) or _swarm_manifest_string(
            validation_failure,
            "failed_command",
            path=path,
            location=f"{task_location}.validation_failure",
        )
        if failed_check:
            lines.append(f"failed check: {failed_check}")
        output_tail = _swarm_manifest_string(
            validation_failure,
            "output_tail",
            path=path,
            location=f"{task_location}.validation_failure",
        )
        if output_tail:
            lines.append("validation output tail:")
            lines.extend(f"  {line}" for line in output_tail.splitlines())
        validation_retry = _swarm_manifest_string(
            validation_failure,
            "retry_command",
            path=path,
            location=f"{task_location}.validation_failure",
        ) or _swarm_manifest_string(
            validation_failure,
            "suggested_next_command",
            path=path,
            location=f"{task_location}.validation_failure",
        )
        if validation_retry:
            lines.append(f"  retry validation: {validation_retry}")
        recovery_command = _swarm_manifest_string(
            validation_failure,
            "recovery_command",
            path=path,
            location=f"{task_location}.validation_failure",
        )
        if recovery_command:
            lines.append(f"  re-enter worker: {recovery_command}")
    conflict_state = _swarm_manifest_dict(
        task,
        "conflict_state",
        path=path,
        location=task_location,
    )
    if conflict_state is not None:
        files = _swarm_manifest_string_list(
            conflict_state,
            "conflicted_files",
            path=path,
            location=f"{task_location}.conflict_state",
        )
        lines.append("conflicted files: " + (", ".join(files) or "unknown"))
        commands = _swarm_manifest_string_list(
            conflict_state,
            "recovery_commands",
            path=path,
            location=f"{task_location}.conflict_state",
        )
        if commands:
            lines.append("conflict recovery:")
            lines.extend(f"  {command}" for command in commands)
    if worktree is not None:
        lines.append(
            f"  inspect worktree: cd {shlex.quote(str(worktree))} && git status --short --branch"
        )
    if retry_available:
        lines.append(f"  retry shipping: {retry_command}")
    else:
        lines.append("  retry shipping: unavailable until the worktree exists and has commits")
    if branch:
        lines.append(f"  inspect branch: git log --oneline {shlex.quote(branch)} --")
    lines.append(f"  rerun implementation: {rerun_command}")
    return lines


def _retry_ship_swarm_task(
    *,
    manifest_path: Path,
    payload: dict[str, object],
    task_index: int,
    auto_merge: bool,
) -> dict[str, object]:
    tasks = _swarm_manifest_tasks(payload, path=manifest_path)
    task = tasks[task_index]
    selector = str(task_index + 1)
    repo_root_text = _swarm_manifest_string(
        payload,
        "repo_root",
        path=manifest_path,
        location="manifest",
    )
    repo_root = Path(repo_root_text) if repo_root_text else _swarm_repo_root()
    base_branch = _swarm_manifest_string(
        payload,
        "base_branch",
        path=manifest_path,
        location="manifest",
        required=True,
    )
    base_ref = _swarm_manifest_string(
        payload,
        "base_ref",
        path=manifest_path,
        location="manifest",
        required=True,
    )
    if (
        _swarm_manifest_string(
            task,
            "status",
            path=manifest_path,
            location=f"tasks[{task_index}]",
        )
        == SWARM_CONFLICT_STATUS
    ):
        base_ref = _run_swarm_git(
            ["rev-parse", "--verify", base_branch],
            cwd=repo_root,
        ).stdout.strip()

    branch = _swarm_task_string(task, "branch", path=manifest_path, selector=selector)
    worktree = Path(_swarm_task_string(task, "worktree", path=manifest_path, selector=selector))
    if not worktree.exists():
        raise click.ClickException(f"swarm task {selector} worktree does not exist: {worktree}")

    started_at = time.monotonic()
    status = "failed"
    pr_url: str | None = None
    merge_sha: str | None = None
    merge_status: str | None = None
    inspection_warning: str | None = None
    failure_reason: str | None = None
    validation_failure: dict[str, object] | None = None
    commits = 0
    cleanup_warning: str | None = None
    try:
        _require_swarm_branch_checked_out(worktree, branch, selector=selector)
        dirty_paths = _swarm_dirty_paths(worktree)
        if dirty_paths:
            raise click.ClickException(
                "swarm task {selector} worktree has uncommitted changes: {paths}".format(
                    selector=selector,
                    paths=", ".join(path[:200] for path in dirty_paths[:10]),
                )
            )
        commits = _swarm_commit_count(worktree, base_ref)
        if commits == 0:
            raise click.ClickException(
                f"swarm task {selector} has no commits ahead of manifest base_ref {base_ref}"
            )
        ship_outcome = _coerce_swarm_ship_outcome(
            _ship_swarm_pr(
                worktree,
                base_branch=base_branch,
                branch=branch,
                auto_merge=auto_merge,
            )
        )
        status = ship_outcome.status
        pr_url = ship_outcome.pr_url
        merge_status = ship_outcome.merge_status
        inspection_warning = ship_outcome.inspection_warning
        validation_failure = ship_outcome.validation_failure
        if status == "shipped":
            merge_sha = ship_outcome.detail
        elif status in {"failed", "review-blocked", SWARM_VALIDATION_FAILED_STATUS}:
            failure_reason = ship_outcome.detail
    except click.ClickException:
        raise
    except RuntimeError as e:
        status = "failed"
        failure_reason = str(e)
    duration_ms = int((time.monotonic() - started_at) * 1000)

    if status in SWARM_SUCCESS_STATUSES:
        try:
            _remove_swarm_worktree(worktree, repo_root=repo_root)
            if status == "shipped":
                cleanup_warning = _delete_shipped_swarm_branch(
                    branch,
                    repo_root=repo_root,
                    merge_sha=merge_sha,
                )
                if cleanup_warning is not None:
                    click.echo(f"[conductor] warning: {cleanup_warning}", err=True)
                    failure_reason = cleanup_warning
        except RuntimeError as e:
            cleanup_warning = f"cleanup failed: {e}"
            click.echo(f"[conductor] warning: {cleanup_warning}", err=True)
            if merge_status != "MERGED":
                status = "failed"
                failure_reason = f"{failure_reason or status}; {cleanup_warning}"

    updated_task = dict(task)
    updated_task.update(
        {
            "status": status,
            "commits": commits,
            "pr_url": pr_url,
            "merge_sha": merge_sha,
            "merge_status": merge_status,
            "inspection_warning": inspection_warning,
            "duration_ms": duration_ms,
            "failure_reason": failure_reason,
            "validation_failure": validation_failure,
            "cleanup_warning": cleanup_warning,
            "cleanup_status": "removed" if not worktree.exists() else "preserved",
        }
    )
    tasks[task_index] = updated_task

    raw_duration = _swarm_manifest_int(
        payload,
        "duration_ms",
        path=manifest_path,
        location="manifest",
    )
    run_duration_ms = duration_ms
    if raw_duration is not None:
        run_duration_ms = max(0, raw_duration) + duration_ms

    preflight = _swarm_manifest_dict(
        payload,
        "preflight",
        path=manifest_path,
        location="manifest",
    )
    return _swarm_manifest_payload(
        run_id=_swarm_manifest_string(
            payload,
            "run_id",
            path=manifest_path,
            location="manifest",
            default=manifest_path.stem,
        ),
        repo_root=repo_root,
        base_branch=base_branch,
        base_ref=base_ref,
        started_at=_swarm_manifest_string(
            payload,
            "started_at",
            path=manifest_path,
            location="manifest",
            default=_swarm_now_iso(),
        ),
        ended_at=_swarm_now_iso(),
        duration_ms=run_duration_ms,
        tasks=tasks,
        preflight=preflight,
    )


def _run_swarm_retry_ship_command(
    *,
    run_id_or_path: str,
    task_selector: str,
    auto_merge: bool,
) -> None:
    path = _resolve_swarm_manifest_path(run_id_or_path, latest=False)
    payload = _read_swarm_manifest(path)
    tasks = _swarm_manifest_tasks(payload, path=path)
    task_index, task = _select_swarm_manifest_task(tasks, task_selector, path=path)
    branch = _swarm_manifest_string(task, "branch", path=path, location=f"tasks[{task_index}]")
    brief = _swarm_manifest_string(task, "brief", path=path, location=f"tasks[{task_index}]")
    updated = _retry_ship_swarm_task(
        manifest_path=path,
        payload=payload,
        task_index=task_index,
        auto_merge=auto_merge,
    )
    repo_root_value = _swarm_manifest_string(
        updated,
        "repo_root",
        path=path,
        location="manifest",
    )
    repo_root = Path(repo_root_value) if repo_root_value else _swarm_repo_root()
    run_id = _swarm_manifest_string(
        updated,
        "run_id",
        path=path,
        location="manifest",
        default=path.stem,
    )
    updated, bookkeeping = _swarm_payload_with_cortex_bookkeeping(
        repo_root=repo_root,
        run_id=run_id,
        manifest_path=path,
        payload=updated,
    )
    _write_swarm_manifest(path, updated)
    updated_task = _swarm_manifest_tasks(updated, path=path)[task_index]
    detail = "{status}: {brief} -> {branch}".format(
        status=_swarm_manifest_string(
            updated_task,
            "status",
            path=path,
            location=f"tasks[{task_index}]",
            default="unknown",
        ),
        brief=brief,
        branch=branch,
    )
    pr_url = _swarm_manifest_string(
        updated_task,
        "pr_url",
        path=path,
        location=f"tasks[{task_index}]",
    )
    if pr_url:
        detail += f" ({pr_url})"
    click.echo(detail)
    _emit_swarm_cortex_bookkeeping_notice(bookkeeping, err=False)
    if (
        _swarm_manifest_string(
            updated_task,
            "status",
            path=path,
            location=f"tasks[{task_index}]",
        )
        not in SWARM_SUCCESS_STATUSES
    ):
        sys.exit(1)


def _swarm_task_phase_display(task: dict[str, object], *, running: bool) -> str:
    phase = task.get("last_phase")
    if not isinstance(phase, str) or not phase:
        return ""
    progress_at = task.get("last_progress_at")
    if not isinstance(progress_at, str) or not progress_at:
        return f" phase={phase}"
    if not running:
        return f" phase={phase} last_progress_at={progress_at}"
    age_seconds = (datetime.now(UTC) - parse_timestamp(progress_at)).total_seconds()
    activity = "stalled" if age_seconds > DEFAULT_EXEC_MAX_STALL_SEC else "active"
    return f" {activity} phase={phase} last_progress_at={progress_at}"


def _swarm_manifest_optional_bool(
    record: dict[str, object],
    key: str,
    *,
    path: Path,
    location: str,
) -> bool | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise _swarm_manifest_type_error(path, f"{location}.{key}", "a bool")
    return value


def _format_swarm_manifest_status(payload: dict[str, object], *, path: Path) -> list[str]:
    run_id = _swarm_manifest_string(
        payload,
        "run_id",
        path=path,
        location="manifest",
        default=path.stem,
    )
    ok = _swarm_manifest_optional_bool(payload, "ok", path=path, location="manifest")
    ended_at = _swarm_manifest_string(payload, "ended_at", path=path, location="manifest")
    if ended_at == "":
        status = "running"
    else:
        status = "ok" if ok is True else "failed" if ok is False else "unknown"
    tasks = _swarm_manifest_tasks(payload, path=path)
    duration_ms = _swarm_manifest_int(
        payload,
        "duration_ms",
        path=path,
        location="manifest",
    )
    metrics = _swarm_metrics_from_task_records(tasks, duration_ms=duration_ms)
    manifest_metrics = _swarm_manifest_dict(
        payload,
        "metrics",
        path=path,
        location="manifest",
    )
    if manifest_metrics is not None:
        metrics = {**metrics, **manifest_metrics}
    throughput = metrics.get("completed_tasks_per_hour", 0.0)
    if not isinstance(throughput, int | float) or isinstance(throughput, bool):
        throughput = 0.0
    lines = [
        f"swarm run {run_id} ({status})",
        f"manifest: {path}",
        (
            "repo: {repo} base: {branch}@{ref}".format(
                repo=_swarm_manifest_string(
                    payload,
                    "repo_root",
                    path=path,
                    location="manifest",
                ),
                branch=_swarm_manifest_string(
                    payload,
                    "base_branch",
                    path=path,
                    location="manifest",
                ),
                ref=_swarm_manifest_string(payload, "base_ref", path=path, location="manifest"),
            )
        ),
        (
            "started: {started} ended: {ended} duration_ms={duration}".format(
                started=_swarm_manifest_string(
                    payload,
                    "started_at",
                    path=path,
                    location="manifest",
                ),
                ended=ended_at or "running",
                duration=duration_ms,
            )
        ),
        (
            "ok={ok} shipped={shipped} failed={failed} no-changes={no_changes} "
            "provider-failed={provider_failed}".format(
                ok=ok,
                shipped=_swarm_manifest_int(
                    payload,
                    "shipped_count",
                    path=path,
                    location="manifest",
                    default=0,
                ),
                failed=_swarm_manifest_int(
                    payload,
                    "failed_count",
                    path=path,
                    location="manifest",
                    default=0,
                ),
                no_changes=_swarm_manifest_int(
                    payload,
                    "no_changes_count",
                    path=path,
                    location="manifest",
                    default=0,
                ),
                provider_failed=_swarm_manifest_int(
                    payload,
                    "provider_failed_count",
                    path=path,
                    location="manifest",
                    default=0,
                ),
            )
        ),
        (
            "metrics: total={total} review-blocked={review_blocked} "
            "pushed-not-merged={pushed_not_merged} commits={commits} "
            "completed/hour={throughput:.2f} provider-failed={provider_failed}".format(
                total=metrics.get("total_tasks", len(tasks)),
                review_blocked=metrics.get("review_blocked_count", 0),
                pushed_not_merged=metrics.get("pushed_not_merged_count", 0),
                commits=metrics.get("total_commits", 0),
                throughput=float(throughput),
                provider_failed=metrics.get("provider_failed_count", 0),
            )
        ),
    ]
    conflict_count = metrics.get("needs_human_conflict_resolution_count", 0)
    if isinstance(conflict_count, int) and not isinstance(conflict_count, bool) and conflict_count:
        lines.append(f"conflicts: needs-human-conflict-resolution={conflict_count}")
    merge_plan = _swarm_manifest_dict(payload, "merge_plan", path=path, location="manifest")
    if merge_plan is not None:
        lanes = _swarm_manifest_dict_list(
            merge_plan,
            "lanes",
            path=path,
            location="manifest.merge_plan",
        )
        order = [
            str(task_index)
            for lane_index, lane in enumerate(lanes)
            if (
                task_index := _swarm_manifest_int(
                    lane,
                    "task_index",
                    path=path,
                    location=f"manifest.merge_plan.lanes[{lane_index}]",
                )
            )
            is not None
        ]
        if order:
            lines.append("merge-plan: " + " -> ".join(order))
    if tasks:
        per_status = metrics.get("per_status_counts")
        if isinstance(per_status, dict) and per_status:
            status_parts = [
                f"{key}={value}"
                for key, value in per_status.items()
                if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
            ]
            if status_parts:
                lines.append("status-counts: " + " ".join(status_parts))
        for task_index, raw_task in enumerate(tasks):
            task_location = f"tasks[{task_index}]"
            provider_detail = _swarm_manifest_string(
                raw_task,
                "provider",
                path=path,
                location=task_location,
            ) or _swarm_manifest_string(
                raw_task,
                "selected_provider",
                path=path,
                location=task_location,
            )
            line = (
                "- {status}: {brief} -> {branch}".format(
                    status=_swarm_manifest_string(
                        raw_task,
                        "status",
                        path=path,
                        location=task_location,
                        default="unknown",
                    ),
                    brief=_swarm_manifest_string(
                        raw_task,
                        "brief",
                        path=path,
                        location=task_location,
                    ),
                    branch=_swarm_manifest_string(
                        raw_task,
                        "branch",
                        path=path,
                        location=task_location,
                    ),
                )
            )
            line += _swarm_task_phase_display(
                raw_task,
                running=ended_at == "",
            )
            if provider_detail:
                line += f" provider={provider_detail}"
            fallback_provider = _swarm_manifest_string(
                raw_task,
                "fallback_provider",
                path=path,
                location=task_location,
            )
            if fallback_provider:
                line += f" fallback={fallback_provider}"
            fallback_reason = _swarm_manifest_string(
                raw_task,
                "fallback_reason",
                path=path,
                location=task_location,
            )
            if fallback_reason:
                line += f" fallback_reason={fallback_reason}"
            pr_url = _swarm_manifest_string(
                raw_task,
                "pr_url",
                path=path,
                location=task_location,
            )
            if pr_url:
                line += f" ({pr_url})"
            failure_reason = _swarm_manifest_string(
                raw_task,
                "failure_reason",
                path=path,
                location=task_location,
            )
            if failure_reason:
                line += f" reason={failure_reason}"
            lines.append(line)
            validation_failure = _swarm_manifest_dict(
                raw_task,
                "validation_failure",
                path=path,
                location=task_location,
            )
            if validation_failure is not None:
                validation_location = f"{task_location}.validation_failure"
                failed_check = _swarm_manifest_string(
                    validation_failure,
                    "failed_check",
                    path=path,
                    location=validation_location,
                ) or _swarm_manifest_string(
                    validation_failure,
                    "failed_command",
                    path=path,
                    location=validation_location,
                )
                if failed_check:
                    lines.append(f"  failed check: {failed_check}")
                retry_command = _swarm_manifest_string(
                    validation_failure,
                    "retry_command",
                    path=path,
                    location=validation_location,
                ) or _swarm_manifest_string(
                    validation_failure,
                    "suggested_next_command",
                    path=path,
                    location=validation_location,
                )
                if retry_command:
                    lines.append(f"  retry command: {retry_command}")
            conflict_state = _swarm_manifest_dict(
                raw_task,
                "conflict_state",
                path=path,
                location=task_location,
            )
            if conflict_state is not None:
                conflict_location = f"{task_location}.conflict_state"
                files = _swarm_manifest_string_list(
                    conflict_state,
                    "conflicted_files",
                    path=path,
                    location=conflict_location,
                )
                lines.append(
                    "  conflicted files: "
                    + (", ".join(files) or "unknown")
                )
                commands = _swarm_manifest_string_list(
                    conflict_state,
                    "recovery_commands",
                    path=path,
                    location=conflict_location,
                )
                if commands:
                    lines.append("  recovery commands:")
                    lines.extend(f"    {command}" for command in commands)
    bookkeeping = _swarm_manifest_dict(
        payload,
        "post_merge_bookkeeping",
        path=path,
        location="manifest",
    )
    if bookkeeping is not None:
        required = _swarm_manifest_optional_bool(
            bookkeeping,
            "required",
            path=path,
            location="manifest.post_merge_bookkeeping",
        )
        if required is True:
            lines.append(
                "cortex-post-merge: {status}".format(
                    status=_swarm_manifest_string(
                        bookkeeping,
                        "status",
                        path=path,
                        location="manifest.post_merge_bookkeeping",
                        default="unknown",
                    ),
                )
            )
            context_path = _swarm_manifest_string(
                bookkeeping,
                "context_path",
                path=path,
                location="manifest.post_merge_bookkeeping",
            )
            if context_path:
                lines.append(f"  context: {context_path}")
            follow_up_command = _swarm_manifest_string(
                bookkeeping,
                "follow_up_command",
                path=path,
                location="manifest.post_merge_bookkeeping",
            )
            if follow_up_command:
                lines.append(f"  command: {follow_up_command}")
    return lines


def _run_swarm_task(
    *,
    brief_path: str,
    provider_id: str | None,
    tags: str | None,
    base_branch: str,
    base_ref: str,
    auto_merge: bool,
    max_iterations: int | None,
    max_stall_sec: int | None,
    timeout_sec: int | None,
    keep_worktrees_on_failure: bool,
    as_json: bool,
    defer_ship: bool = False,
    phase_recorder: Callable[[str, str], None] | None = None,
) -> SwarmTaskResult:
    started_at = time.monotonic()
    repo_root = _swarm_repo_root()
    branch = _swarm_branch_for_brief(brief_path)
    worktree = _swarm_worktree_for_brief(brief_path, repo_root=repo_root)
    brief = Path(brief_path)
    if not brief.is_absolute():
        brief = (repo_root / brief).resolve()
    try:
        body = brief.read_text(encoding="utf-8").strip()
    except OSError as e:
        raise click.UsageError(f"could not read --brief {brief_path!r}: {e.strerror or e}") from e
    if not body:
        raise click.UsageError(f"brief is empty after stripping whitespace: {brief_path!r}")
    closing_refs = _swarm_issue_closing_refs(
        body,
        brief_path=brief,
        repo_slug=_swarm_github_repo_slug(repo_root),
    )
    dispatch_body = _with_swarm_delivery_contract(body)

    created_worktree = False
    status = "failed"
    commits = 0
    pr_url: str | None = None
    merge_sha: str | None = None
    merge_status: str | None = None
    inspection_warning: str | None = None
    cleanup_warning: str | None = None
    failure_reason: str | None = None
    provider_candidates: list[str] = []
    selected_provider: str | None = None
    provider_used: str | None = None
    fallback_provider: str | None = None
    fallback_reason: str | None = None
    last_phase: str | None = None
    last_progress_at: str | None = None
    phase_history: list[dict[str, object]] = []
    validation_failure: dict[str, object] | None = None

    def mark_phase(phase: str) -> None:
        nonlocal last_phase, last_progress_at
        last_phase = phase
        last_progress_at = _swarm_now_iso()
        phase_history.append({"phase": phase, "at": last_progress_at})
        del phase_history[:-12]
        if phase_recorder is not None:
            phase_recorder(brief_path, phase)

    try:
        mark_phase("starting")
        provider_pool = _is_swarm_provider_pool(provider_id)
        provider_candidates, _route_decision = _swarm_provider_candidates(
            provider_id=provider_id,
            tags=tags,
            body=body,
            max_iterations=max_iterations,
        )
        selected_provider = provider_candidates[0]
        dispatch_tools = _ordered_tools_csv(frozenset(VALID_TOOLS)) if provider_pool else None
        worktree.parent.mkdir(parents=True, exist_ok=True)
        with SWARM_WORKTREE_LOCK:
            _run_swarm_git(
                ["worktree", "add", str(worktree), "-b", branch, base_ref],
                cwd=repo_root,
            )
        created_worktree = True
        if not as_json:
            click.echo(f"[conductor] swarm task started: {brief_path} -> {branch}", err=True)

        exec_succeeded = False
        for index, candidate_provider in enumerate(provider_candidates):
            provider_used = candidate_provider
            try:
                mark_phase("editing")
                _run_exec_phase_dispatch(
                    provider_id=candidate_provider,
                    auto=False,
                    tags=tags,
                    prefer=None,
                    effort=None,
                    tools=dispatch_tools,
                    permission_profile=None,
                    sandbox=None,
                    exclude=None,
                    cwd=str(worktree),
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    strict_stall=False,
                    start_timeout_sec=None,
                    max_iterations=max_iterations,
                    allow_completion_stretch=True,
                    retry_on_stall=1,
                    body=dispatch_body,
                    attachments=(),
                    model=None,
                    log_file=None,
                    as_json=False,
                    verbose_route=False,
                    silent_route=True,
                    resume_session_id=None,
                    offline=None,
                    preflight=True,
                    write_validation=True,
                    timeout_is_default=timeout_sec is None,
                    max_stall_is_default=max_stall_sec == DEFAULT_EXEC_MAX_STALL_SEC,
                    raise_on_error=True,
                )
                exec_succeeded = True
                break
            except _ExecPhaseError as e:
                candidate_status = _swarm_failure_status(f"{e.exit_status} {e.message}")
                if (
                    candidate_status == SWARM_PROVIDER_FAILED_STATUS
                    and index + 1 < len(provider_candidates)
                ):
                    can_fallback, guard_reason = _swarm_can_fallback_after_provider_failure(
                        worktree,
                        base_ref=base_ref,
                    )
                    if can_fallback:
                        if fallback_reason is None:
                            fallback_reason = e.message
                        fallback_provider = provider_candidates[index + 1]
                        click.echo(
                            "[conductor] swarm provider fallback: "
                            f"{candidate_provider} "
                            f"({_provider_runtime_kind_by_name(candidate_provider)}) -> "
                            f"{fallback_provider} "
                            f"({_provider_runtime_kind_by_name(fallback_provider)}); "
                            f"reason: {e.message}",
                            err=True,
                        )
                        continue
                    failure_reason = guard_reason
                status = candidate_status
                failure_reason = failure_reason or e.message
                if created_worktree:
                    try:
                        commits = _swarm_commit_count(worktree, base_ref)
                    except RuntimeError as count_error:
                        click.echo(f"[conductor] warning: {count_error}", err=True)
                break
            except RuntimeError as e:
                candidate_status = _swarm_failure_status(e)
                if (
                    candidate_status == SWARM_PROVIDER_FAILED_STATUS
                    and index + 1 < len(provider_candidates)
                ):
                    can_fallback, guard_reason = _swarm_can_fallback_after_provider_failure(
                        worktree,
                        base_ref=base_ref,
                    )
                    if can_fallback:
                        if fallback_reason is None:
                            fallback_reason = str(e)
                        fallback_provider = provider_candidates[index + 1]
                        click.echo(
                            "[conductor] swarm provider fallback: "
                            f"{candidate_provider} "
                            f"({_provider_runtime_kind_by_name(candidate_provider)}) -> "
                            f"{fallback_provider} "
                            f"({_provider_runtime_kind_by_name(fallback_provider)}); "
                            f"reason: {e}",
                            err=True,
                        )
                        continue
                    failure_reason = guard_reason
                status = candidate_status
                failure_reason = failure_reason or str(e)
                break
        if exec_succeeded:
            if fallback_provider is not None:
                fallback_provider = provider_used
            mark_phase("testing")
            dirty_paths = _swarm_dirty_paths(worktree)
            committing_marked = False
            if dirty_paths:
                mark_phase("committing")
                committing_marked = True
                _swarm_commit_dirty_worktree(
                    worktree,
                    brief_path=brief_path,
                    body=body,
                )
            commits = _swarm_commit_count(worktree, base_ref)
            if commits == 0:
                status = "no-changes"
            elif defer_ship:
                if not committing_marked:
                    mark_phase("committing")
                _append_swarm_closing_refs_to_head_commit(worktree, closing_refs)
                status = "ready-to-ship"
            else:
                if not committing_marked:
                    mark_phase("committing")
                _append_swarm_closing_refs_to_head_commit(worktree, closing_refs)
                mark_phase("shipping")
                ship_outcome = _coerce_swarm_ship_outcome(
                    _ship_swarm_pr(
                        worktree,
                        base_branch=base_branch,
                        branch=branch,
                        auto_merge=auto_merge,
                    )
                )
                status = ship_outcome.status
                pr_url = ship_outcome.pr_url
                merge_status = ship_outcome.merge_status
                inspection_warning = ship_outcome.inspection_warning
                validation_failure = ship_outcome.validation_failure
                if status == "shipped":
                    merge_sha = ship_outcome.detail
                elif status in {"failed", "review-blocked", SWARM_VALIDATION_FAILED_STATUS}:
                    failure_reason = ship_outcome.detail
        elif status == "failed":
            failure_reason = failure_reason or "provider dispatch did not complete"
    except _ExecPhaseError as e:
        status = _swarm_failure_status(f"{e.exit_status} {e.message}")
        failure_reason = e.message
        if created_worktree:
            try:
                commits = _swarm_commit_count(worktree, base_ref)
            except RuntimeError as count_error:
                click.echo(f"[conductor] warning: {count_error}", err=True)
    except RuntimeError as e:
        status = _swarm_failure_status(e)
        failure_reason = str(e)
    finally:
        duration_ms = int((time.monotonic() - started_at) * 1000)

    result = SwarmTaskResult(
        brief=brief_path,
        branch=branch,
        worktree=str(worktree),
        status=status,
        commits=commits,
        pr_url=pr_url,
        merge_sha=merge_sha,
        duration_ms=duration_ms,
        merge_status=merge_status,
        inspection_warning=inspection_warning,
        cleanup_warning=cleanup_warning,
        failure_reason=failure_reason,
        provider=provider_used,
        selected_provider=selected_provider,
        fallback_provider=fallback_provider,
        fallback_reason=fallback_reason,
        last_phase=last_phase,
        last_progress_at=last_progress_at,
        phase_history=phase_history,
        validation_failure=validation_failure,
    )
    if (
        created_worktree
        and result.status != SWARM_PROVIDER_FAILED_STATUS
        and result.status != SWARM_VALIDATION_FAILED_STATUS
        and result.status != "ready-to-ship"
        and (result.status in SWARM_SUCCESS_STATUSES or not keep_worktrees_on_failure)
    ):
        try:
            _remove_swarm_worktree(worktree, repo_root=repo_root)
            if result.status == "shipped":
                cleanup_warning = _delete_shipped_swarm_branch(
                    branch,
                    repo_root=repo_root,
                    merge_sha=merge_sha,
                )
                if cleanup_warning is not None:
                    click.echo(f"[conductor] warning: {cleanup_warning}", err=True)
                    result.cleanup_warning = cleanup_warning
                    result.failure_reason = cleanup_warning
        except RuntimeError as e:
            cleanup_warning = f"cleanup failed: {e}"
            result.cleanup_warning = cleanup_warning
            click.echo(f"[conductor] warning: {cleanup_warning}", err=True)
            if result.merge_status != "MERGED":
                result.status = "failed"
                result.failure_reason = (
                    f"{result.failure_reason or result.status}; {cleanup_warning}"
                )
    if result.status in {"failed", SWARM_PROVIDER_FAILED_STATUS, SWARM_VALIDATION_FAILED_STATUS}:
        click.echo(
            f"[conductor] swarm task {result.status}: {brief_path}; inspect {worktree}; "
            f"reason: {result.failure_reason}",
            err=True,
        )
    elif result.status == "review-blocked":
        click.echo(
            f"[conductor] swarm task review-blocked: {brief_path}; inspect {worktree}",
            err=True,
        )
    elif result.status == "ready-to-ship":
        click.echo(
            f"[conductor] swarm task ready-to-ship: {brief_path}; inspect {worktree}",
            err=True,
        )
    elif not as_json:
        click.echo(
            f"[conductor] swarm task {result.status}: {brief_path} "
            f"({result.commits} commits)",
            err=True,
        )
    return result


def _failed_swarm_thread_result(brief_path: str, error: BaseException) -> SwarmTaskResult:
    repo_root = _swarm_repo_root()
    branch = _swarm_branch_for_brief(brief_path)
    worktree = _swarm_worktree_for_brief(brief_path, repo_root=repo_root)
    message = f"internal swarm task error: {error}"
    now = _swarm_now_iso()
    click.echo(f"[conductor] swarm task crashed: {brief_path}; {message}", err=True)
    return SwarmTaskResult(
        brief=brief_path,
        branch=branch,
        worktree=str(worktree),
        status=_swarm_failure_status(error),
        commits=0,
        pr_url=None,
        merge_sha=None,
        duration_ms=0,
        failure_reason=message,
        last_phase="starting",
        last_progress_at=now,
        phase_history=[{"phase": "starting", "at": now}],
    )


def _is_swarm_provider_pool(provider_id: str | None) -> bool:
    return provider_id is None or provider_id in {"auto", "pool"}


def _swarm_task_tags(raw_tags: str | None) -> list[str]:
    tags = ["tool-use"]
    for tag in _parse_csv(raw_tags):
        if tag not in tags:
            tags.append(tag)
    return tags


_SWARM_RUNTIME_PRIORITY = {
    PROVIDER_RUNTIME_STATEFUL_AGENT: 0,
    PROVIDER_RUNTIME_STATELESS_TOOL_LOOP: 1,
    PROVIDER_RUNTIME_TEXT_ONLY: 2,
}


def _swarm_runtime_ordered_candidates(candidates: list[str]) -> list[str]:
    original_index = {name: index for index, name in enumerate(candidates)}
    return sorted(
        candidates,
        key=lambda name: (
            _SWARM_RUNTIME_PRIORITY.get(
                _provider_runtime_kind_by_name(name),
                _SWARM_RUNTIME_PRIORITY[PROVIDER_RUNTIME_TEXT_ONLY],
            ),
            original_index[name],
        ),
    )


def _swarm_provider_candidates(
    *,
    provider_id: str | None,
    tags: str | None,
    body: str,
    max_iterations: int | None = None,
) -> tuple[list[str], RouteDecision | None]:
    if not _is_swarm_provider_pool(provider_id):
        assert provider_id is not None
        return [provider_id], None

    try:
        _provider, decision = pick(
            _swarm_task_tags(tags),
            prefer="balanced",
            effort=_parse_effort(None),
            tools=frozenset(VALID_TOOLS),
            sandbox="none",
            shadow=True,
            estimated_input_tokens=_estimate_text_tokens(body),
        )
    except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
        raise click.UsageError(str(e)) from e
    candidates = [candidate.name for candidate in decision.ranked]
    if max_iterations is not None:
        candidates = [
            candidate
            for candidate in candidates
            if _provider_supports_exec_max_iterations(candidate)
        ]
        if not candidates:
            raise click.UsageError(
                "--max-iterations only applies to Conductor-managed tool-use "
                "providers; no routed swarm candidate can honor it."
            )
    return _swarm_runtime_ordered_candidates(candidates), decision


def _swarm_can_fallback_after_provider_failure(
    worktree: Path,
    *,
    base_ref: str,
) -> tuple[bool, str | None]:
    try:
        dirty_paths = _swarm_dirty_paths(worktree)
        if dirty_paths:
            return (
                False,
                "provider failed after leaving uncommitted changes; "
                "not falling back across providers",
            )
        commits = _swarm_commit_count(worktree, base_ref)
        if commits > 0:
            return (
                False,
                "provider failed after creating commits; not falling back across providers",
            )
    except RuntimeError as e:
        return False, f"could not verify worktree before fallback: {e}"
    return True, None


DEFAULT_AUTO_PHASE_ANCHORS: tuple[str, ...] = (
    "## Tests",
    "## Validation",
)
DEFAULT_AUTO_PHASE_REGEX_ANCHORS: tuple[re.Pattern[str], ...] = (
    re.compile(r"^## Phase \d+\s*$"),
)


def _phase_heading_title(line: str) -> str:
    return line.lstrip("#").strip()


def _auto_phase_anchor_matcher(extra_anchors: tuple[str, ...]) -> re.Pattern[str]:
    literal_anchors = [
        anchor.strip() for anchor in (*DEFAULT_AUTO_PHASE_ANCHORS, *extra_anchors) if anchor.strip()
    ]
    exact_patterns = [rf"^{re.escape(anchor)}\s*$" for anchor in literal_anchors]
    regex_patterns = [pattern.pattern for pattern in DEFAULT_AUTO_PHASE_REGEX_ANCHORS]
    return re.compile("|".join([*exact_patterns, *regex_patterns]))


def _split_brief_into_auto_phases(
    body: str,
    *,
    extra_anchors: tuple[str, ...] = (),
) -> list[BriefPhase]:
    anchor_pattern = _auto_phase_anchor_matcher(extra_anchors)
    lines = body.splitlines(keepends=True)
    anchor_indexes = [
        idx for idx, line in enumerate(lines) if anchor_pattern.match(line.rstrip("\r\n"))
    ]
    if not anchor_indexes:
        return [BriefPhase(title="Brief", body=body)]

    phases: list[BriefPhase] = []
    first_anchor = anchor_indexes[0]
    intro = "".join(lines[:first_anchor]).strip()
    if intro:
        phases.append(BriefPhase(title="Intro", body=intro))

    for anchor_position, start in enumerate(anchor_indexes):
        end = (
            anchor_indexes[anchor_position + 1]
            if anchor_position + 1 < len(anchor_indexes)
            else len(lines)
        )
        chunk = "".join(lines[start:end]).strip()
        if chunk:
            phases.append(BriefPhase(title=_phase_heading_title(lines[start]), body=chunk))

    return phases


def _run_exec_phase_dispatch(
    *,
    provider_id: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    tools: str | None,
    permission_profile: str | None,
    sandbox: str | None,
    exclude: str | None,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    strict_stall: bool,
    start_timeout_sec: float | None,
    max_iterations: int | None,
    allow_completion_stretch: bool,
    retry_on_stall: int,
    body: str,
    attachments: tuple[Path, ...],
    model: str | None,
    log_file: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    resume_session_id: str | None,
    offline: bool | None,
    preflight: bool,
    write_validation: bool,
    timeout_is_default: bool,
    max_stall_is_default: bool,
    raise_on_error: bool,
) -> tuple[CallResponse, RouteDecision | None, SessionLog | None]:
    estimated_input_tokens = _estimate_text_tokens(body)
    permission_profile_value = _validate_permission_profile(permission_profile)
    tools_set = _resolve_exec_tools(
        tools,
        permission_profile=permission_profile_value,
    )
    sandbox_value = _validate_sandbox(sandbox, warn=sandbox is not None)
    max_iterations_value = _resolve_exec_max_iterations(
        max_iterations,
        raw_effort=effort,
    )
    max_iterations_explicit = max_iterations is not None
    effort_value = _parse_effort(effort)
    start_timeout_sec = _normalize_start_timeout_sec(start_timeout_sec)
    dispatch_started_at = time.monotonic()

    decision: RouteDecision | None = None
    session_log: SessionLog | None = None
    fallbacks: list[str] = []
    if auto:
        try:
            provider, decision = pick(
                _parse_csv(tags),
                prefer=_validate_prefer(prefer),
                effort=effort_value,
                tools=tools_set,
                sandbox=sandbox_value,
                exclude=_permission_profile_excludes(
                    exclude,
                    permission_profile=permission_profile_value,
                ),
                shadow=True,
                attachments_required=bool(attachments),
                estimated_input_tokens=estimated_input_tokens,
            )
            decision, exclusion_message = _apply_auto_route_exclusion_rules(
                decision,
                user_tags=tuple(_parse_csv(tags)),
                offline_requested=offline is True,
            )
            provider = get_provider(decision.provider)
        except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
            click.echo(f"conductor: {e}", err=True)
            _exec_phase_exit(2, raise_on_error=raise_on_error, message=str(e))
        if max_iterations_explicit and not _provider_supports_exec_max_iterations(
            decision.provider
        ):
            raise click.UsageError(_exec_max_iterations_unsupported_message(decision.provider))
        if exclusion_message and not silent_route:
            click.echo(exclusion_message, err=True)
        _ensure_permission_profile_supported(
            _provider_for_preflight(provider),
            provider_id=decision.provider,
            permission_profile=permission_profile_value,
            tools=tools_set,
        )
        session_log = _start_exec_session_log(
            log_file=log_file,
            resume_session_id=resume_session_id,
        )
        _emit_session_route_decision(session_log, decision)
        print_caller_banner(decision.provider, silent=silent_route or as_json)
        _emit_route_log(decision, verbose=verbose_route, silent=silent_route or as_json)
        review_gate_route = NATIVE_REVIEW_TAG in decision.task_tags
        if review_gate_route and not max_iterations_explicit:
            max_iterations_value = min(
                max_iterations_value,
                REVIEW_GATE_MAX_EXEC_ITERATIONS,
            )
        if _provider_supports_exec_max_iterations(decision.provider) and not (
            silent_route or as_json
        ):
            click.echo(
                f"[conductor] agent loop iteration cap: {max_iterations_value}",
                err=True,
            )
        timeout_sec, max_stall_sec = _scale_dispatch_defaults(
            provider_id=decision.provider,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
        )
        review_deadline = None
        if review_gate_route:
            timeout_sec, max_stall_sec = _apply_review_gate_auto_budget(
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                timeout_is_default=timeout_is_default,
                max_stall_is_default=max_stall_is_default,
                estimated_input_tokens=estimated_input_tokens,
                candidate_count=len(decision.ranked),
                silent=silent_route or as_json,
            )
            session_log.emit(
                "review_gate_budget",
                {
                    "timeout_sec": timeout_sec,
                    "max_stall_sec": max_stall_sec,
                    "estimated_input_tokens": estimated_input_tokens,
                    "candidate_count": len(decision.ranked),
                },
            )
            review_deadline = _review_gate_deadline(timeout_sec)
        else:
            max_stall_sec = _cap_default_auto_fallback_stall(
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                max_stall_is_default=max_stall_is_default,
                candidate_count=len(decision.ranked),
            )
        max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
        if preflight:
            # `provider` may arrive as a Provider object (real `pick()` return)
            # or as a string (test fixtures, and any caller passing the name).
            # Resolve once via the helper so downstream `.name` access is safe.
            provider_obj = _provider_for_preflight(provider)
            ok, reason = provider_obj.health_probe()
            if not ok:
                if session_log is not None:
                    session_log.bind_provider(provider_obj.name)
                    session_log.emit(
                        "provider_failed",
                        {"provider": provider_obj.name, "error": reason or "preflight failed"},
                    )
                    session_log.mark_finished()
                _record_failed_delegation(
                    "exec",
                    provider_id=provider_obj.name,
                    model=model,
                    effort=effort_value,
                    started_at=dispatch_started_at,
                    error=reason or "preflight failed",
                    decision=decision,
                    session_log=session_log,
                )
                _echo_preflight_failure(provider_obj, reason)
                _exec_phase_exit(
                    2,
                    raise_on_error=raise_on_error,
                    message=reason or "preflight failed",
                )

        try:
            response, fallbacks = _invoke_with_fallback(
                decision,
                mode="exec",
                task=body,
                model=model,
                effort=effort_value,
                tools=tools_set,
                sandbox=sandbox_value,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                start_timeout_sec=start_timeout_sec,
                retry_on_stall=retry_on_stall,
                silent=silent_route or as_json,
                resume_session_id=resume_session_id,
                session_log=session_log,
                attachments=attachments,
                max_iterations=max_iterations_value,
                max_iterations_explicit=max_iterations_explicit,
                allow_completion_stretch=allow_completion_stretch,
                write_validation=write_validation,
                strict_stall=strict_stall,
                fallback_deadline_monotonic=review_deadline,
                fallback_budget_label="review gate budget",
                cap_fallback_attempt_timeout_to_stall=review_deadline is not None,
            )
        except UnsupportedCapability as e:
            if session_log is not None:
                session_log.emit("provider_failed", {"error": str(e)})
                session_log.mark_finished()
            _record_failed_delegation(
                "exec",
                provider_id=decision.provider,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
                session_log=session_log,
            )
            click.echo(f"conductor: {e}", err=True)
            _exec_phase_exit(2, raise_on_error=raise_on_error, message=str(e))
        except ProviderConfigError as e:
            if session_log is not None:
                session_log.emit("provider_failed", {"error": str(e)})
                session_log.mark_finished()
            _record_failed_delegation(
                "exec",
                provider_id=decision.provider,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
                session_log=session_log,
            )
            click.echo(f"conductor: {e}", err=True)
            _exec_phase_exit(2, raise_on_error=raise_on_error, message=str(e))
        except ProviderError as e:
            if session_log is not None:
                session_log.mark_finished()
            _record_failed_delegation(
                "exec",
                provider_id=decision.provider,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                decision=decision,
                session_log=session_log,
            )
            _emit_provider_error(e, as_json=as_json)
            _maybe_echo_stall_recovery_hint(e, cwd=cwd)
            _exec_phase_exit(
                1,
                raise_on_error=raise_on_error,
                status=_exec_failure_status(e),
                message=str(e),
            )
    else:
        if prefer is not None and provider_id != "openrouter":
            raise click.UsageError("--prefer is only meaningful with --auto.")
        # Same narrowing as in `call()` — the early guard rejects the case
        # where neither --auto nor --with was passed.
        assert provider_id is not None
        _enforce_local_provider_opt_in(provider_id, offline_requested=offline is True)
        try:
            provider = get_provider(provider_id)
        except KeyError as e:
            raise click.UsageError(str(e)) from e
        if max_iterations_explicit and not _provider_supports_exec_max_iterations(provider_id):
            raise click.UsageError(_exec_max_iterations_unsupported_message(provider_id))
        _ensure_permission_profile_supported(
            provider,
            provider_id=provider_id,
            permission_profile=permission_profile_value,
            tools=tools_set,
        )
        try:
            _ensure_supports_attachments(provider, attachments)
        except UnsupportedCapability as e:
            click.echo(f"conductor: {e}", err=True)
            _exec_phase_exit(2, raise_on_error=raise_on_error, message=str(e))
        session_log = _start_exec_session_log(
            log_file=log_file,
            resume_session_id=resume_session_id,
        )
        session_log.bind_provider(provider_id)
        print_caller_banner(provider_id, silent=silent_route or as_json)
        if _provider_supports_exec_max_iterations(provider_id) and not (silent_route or as_json):
            click.echo(
                f"[conductor] agent loop iteration cap: {max_iterations_value}",
                err=True,
            )
        timeout_sec, max_stall_sec = _scale_dispatch_defaults(
            provider_id=provider_id,
            timeout_sec=timeout_sec,
            max_stall_sec=max_stall_sec,
            timeout_is_default=timeout_is_default,
            max_stall_is_default=max_stall_is_default,
        )
        max_stall_sec = _normalize_max_stall_sec(max_stall_sec)
        if preflight:
            ok, reason = _run_exec_preflight(provider)
            if not ok:
                session_log.emit(
                    "provider_failed",
                    {"provider": provider_id, "error": reason or "preflight failed"},
                )
                session_log.mark_finished()
                _record_failed_delegation(
                    "exec",
                    provider_id=provider_id,
                    model=model,
                    effort=effort_value,
                    started_at=dispatch_started_at,
                    error=reason or "preflight failed",
                    session_log=session_log,
                )
                _echo_preflight_failure(provider, reason)
                _exec_phase_exit(
                    2,
                    raise_on_error=raise_on_error,
                    message=reason or "preflight failed",
                )
        try:
            session_log.emit(
                "provider_started",
                {
                    "provider": provider_id,
                    "mode": "exec",
                    "model": model,
                    "tools": sorted(tools_set),
                    "sandbox": sandbox_value,
                    "cwd": cwd,
                    "resume_session_id": resume_session_id,
                },
            )
            if isinstance(provider, OpenRouterProvider):
                response = provider.exec(
                    body,
                    model=model,
                    effort=effort_value,
                    task_tags=_parse_csv(tags),
                    prefer=_validate_prefer(prefer),
                    log_selection=not (silent_route or as_json),
                    tools=tools_set,
                    sandbox=sandbox_value,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                    session_log=session_log,
                    max_iterations=max_iterations_value,
                    allow_completion_stretch=allow_completion_stretch,
                    write_validation=write_validation,
                )
            elif isinstance(provider, ClaudeProvider):
                response = provider.exec(
                    body,
                    model=model,
                    effort=effort_value,
                    tools=tools_set,
                    sandbox=sandbox_value,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    start_timeout_sec=start_timeout_sec,
                    resume_session_id=resume_session_id,
                    session_log=session_log,
                    retry_on_stall=retry_on_stall,
                )
            elif isinstance(provider, CodexProvider):
                response = provider.exec(
                    body,
                    model=model,
                    effort=effort_value,
                    tools=tools_set,
                    sandbox=sandbox_value,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                    session_log=session_log,
                    attachments=attachments,
                    strict_stall=strict_stall,
                    max_iterations=max_iterations_value,
                )
            elif isinstance(provider, OllamaProvider):
                response = provider.exec(
                    body,
                    model=model,
                    effort=effort_value,
                    tools=tools_set,
                    sandbox=sandbox_value,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                    session_log=session_log,
                    max_iterations=max_iterations_value,
                    allow_completion_stretch=allow_completion_stretch,
                    write_validation=write_validation,
                )
            else:
                response = provider.exec(
                    body,
                    model=model,
                    effort=effort_value,
                    tools=tools_set,
                    sandbox=sandbox_value,
                    cwd=cwd,
                    timeout_sec=timeout_sec,
                    max_stall_sec=max_stall_sec,
                    resume_session_id=resume_session_id,
                    session_log=session_log,
                )
        except UnsupportedCapability as e:
            session_log.emit("provider_failed", {"provider": provider_id, "error": str(e)})
            session_log.mark_finished()
            _record_failed_delegation(
                "exec",
                provider_id=provider_id,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                session_log=session_log,
            )
            click.echo(f"conductor: {e}", err=True)
            _exec_phase_exit(2, raise_on_error=raise_on_error, message=str(e))
        except ProviderConfigError as e:
            session_log.emit("provider_failed", {"provider": provider_id, "error": str(e)})
            session_log.mark_finished()
            _record_failed_delegation(
                "exec",
                provider_id=provider_id,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                session_log=session_log,
            )
            click.echo(f"conductor: {e}", err=True)
            _exec_phase_exit(2, raise_on_error=raise_on_error, message=str(e))
        except ProviderError as e:
            session_log.emit("provider_failed", {"provider": provider_id, "error": str(e)})
            session_log.mark_finished()
            _record_failed_delegation(
                "exec",
                provider_id=provider_id,
                model=model,
                effort=effort_value,
                started_at=dispatch_started_at,
                error=e,
                session_log=session_log,
            )
            _emit_provider_error(e, as_json=as_json)
            _maybe_echo_stall_recovery_hint(e, cwd=cwd)
            _maybe_echo_explicit_network_hint(provider_id, e)
            _exec_phase_exit(
                1,
                raise_on_error=raise_on_error,
                status=_exec_failure_status(e),
                message=str(e),
            )
        session_log.set_session_id(response.session_id)
        session_log.emit(
            "provider_finished",
            {
                "provider": response.provider,
                "model": response.model,
                "duration_ms": response.duration_ms,
                "session_id": response.session_id,
            },
        )

    if auto and not as_json:
        _emit_usage_log(response, silent=silent_route)
    _emit_session_usage(session_log, response)
    if session_log is not None:
        session_log.mark_finished()
    _record_response_delegation(
        "exec",
        response,
        effort=effort_value,
        decision=decision,
        session_log=session_log,
        fallback_chain=fallbacks,
    )
    return response, decision, session_log

# --------------------------------------------------------------------------- #
# exec — multi-turn agent session with tool access
# --------------------------------------------------------------------------- #


@main.command(name="exec")
@click.option(
    "--with",
    "provider_id",
    default=None,
    help="Provider identifier. Mutually exclusive with --auto.",
)
@click.option(
    "--profile",
    default=None,
    help=(
        "Apply defaults from a named profile before env vars and explicit flags. "
        "Resolution order: profile defaults < CONDUCTOR_* env vars < explicit CLI flags."
    ),
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick based on --tags, --prefer, and --tools.",
)
@click.option("--tags", default=None, help="Comma-separated task tags.")
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)}.",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option(
    "--tools",
    default=None,
    help=f"Comma-separated tool set: {','.join(VALID_TOOLS)}.",
)
@click.option(
    "--permission-profile",
    default=None,
    help=(
        "Enforce an exec tool whitelist: "
        f"{' | '.join(EXEC_PERMISSION_PROFILES)}. "
        "Excludes providers that cannot honor Conductor tool limits."
    ),
)
@click.option(
    "--sandbox",
    default=None,
    help="Deprecated and ignored; exec always runs unsandboxed.",
)
@click.option(
    "--exclude",
    default=None,
    help="Comma-separated providers to exclude from --auto routing.",
)
@click.option(
    "--cwd",
    default=None,
    help="Working directory for file operations.",
)
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help=(
        "Wall-clock timeout in seconds. Unbounded by default. Set explicitly "
        "(e.g. --timeout 600) for non-review CI or unattended runs that need "
        "a fixed bound; review-tagged auto routes derive their own provider budget."
    ),
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help=(
        "Kill the underlying provider if it produces no output for this many "
        "seconds. Default: 360, just past codex's 5-minute internal websocket "
        "idle (openai/codex#17003) so codex gets one retry attempt before "
        "conductor kills it. Review-tagged auto routes derive their own stall "
        "budget. Set 0 to disable."
    ),
)
@click.option(
    "--strict-stall",
    is_flag=True,
    default=False,
    help=(
        "Codex exec only. Reset --max-stall-seconds only on tool-use/tool-result "
        "events and stderr, ignoring assistant text and turn-boundary events."
    ),
)
@click.option(
    "--start-timeout",
    "start_timeout_sec",
    default=None,
    type=float,
    help=(
        "Startup watchdog in seconds for providers that may cold-load before "
        "their first byte. Set 0 to disable. After first output, "
        "--max-stall-seconds is the only stall watchdog."
    ),
)
@click.option(
    "--max-iterations",
    default=None,
    type=click.IntRange(min=1),
    help=EXEC_MAX_ITERATIONS_HELP,
)
@click.option(
    "--allow-completion-stretch",
    is_flag=True,
    default=False,
    help=(
        "When a managed exec loop hits --max-iterations with detected unfinished "
        "brief deliverables, grant exactly one final clarifying turn."
    ),
)
@click.option(
    "--retry-on-stall",
    default=1,
    type=click.IntRange(min=0),
    help=(
        "Claude exec only. Default: 1. Retry once if the provider stalls before "
        "its first output byte. The retry is safe: no observable side effects "
        "can have occurred before first output. Set to 0 to disable."
    ),
)
@click.option(
    "--task",
    default=None,
    help="The task / prompt. Reads stdin if omitted.\n"
    "For delegation, prefer --brief or --brief-file.\n"
    "For long briefs, prefer --brief-file or stdin to keep the prompt\n"
    "out of `ps aux`.",
)
@click.option(
    "--task-file",
    default=None,
    help="Read the task / prompt from a UTF-8 file. Alias: --brief-file.",
)
@click.option(
    "--brief",
    default=None,
    help="Delegation brief / prompt. Alias for --task with clearer intent.",
)
@click.option(
    "--brief-file",
    default=(),
    multiple=True,
    help=(
        "Read the delegation brief from a UTF-8 file. Repeat for sequential "
        "phases, e.g. --brief-file implement.md --brief-file tests.md. "
        "Single use is unchanged. Use '-' to read stdin."
    ),
)
@click.option(
    "--auto-phase",
    is_flag=True,
    default=False,
    help=(
        "Split one brief into sequential phases on documented anchors "
        "(e.g. ## Tests, ## Validation, ## Phase 2)."
    ),
)
@click.option(
    "--phase-anchor",
    multiple=True,
    help=(
        "Additional exact heading anchor for --auto-phase, e.g. "
        "--phase-anchor '## Custom'. Repeat to add more."
    ),
)
@click.option(
    "--issue",
    default=None,
    help=("Use a GitHub issue as the seed brief. Accepts N for the current repo or owner/repo#N."),
)
@click.option(
    "--issue-comment-limit",
    default=10,
    type=click.IntRange(min=0),
    show_default=True,
    help="Number of recent GitHub issue comments to include with --issue.",
)
@click.option(
    "--attach",
    "attach",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help=(
        "Attach a file to the brief. Repeat for multiple. Today only `codex` "
        "accepts attachments; `--auto` will route accordingly."
    ),
)
@click.option("--model", default=None, help="Override the provider's default model.")
@click.option(
    "--log-file",
    default=None,
    help=(
        "Write structured NDJSON progress events to PATH. Defaults to "
        "~/.cache/conductor/sessions/<session_id>.ndjson."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full CallResponse as JSON.",
)
@click.option("--verbose-route", is_flag=True, default=False)
@click.option("--silent-route", is_flag=True, default=False)
@click.option(
    "--resume",
    "resume_session_id",
    default=None,
    help="Resume a prior session by ID (claude/codex/gemini only). Requires --with.",
)
@click.option(
    "--offline/--no-offline",
    "offline",
    default=None,
    help="--offline: force local (ollama) routing and set the sticky offline "
    "flag. --no-offline: clear the sticky flag before running.",
)
@click.option(
    "--preflight/--no-preflight",
    "preflight",
    default=True,
    help="Run a provider health probe before forwarding the task.",
)
@click.option(
    "--allow-short-brief",
    is_flag=True,
    default=False,
    help=(
        "Suppress the short-brief warning for exec delegation. Use when the "
        "brief is intentionally tiny or all context is supplied through files."
    ),
)
@click.option(
    "--ground-citations",
    is_flag=True,
    default=False,
    help="Warn when post-dispatch citation references do not resolve in the worktree.",
)
@click.option(
    "--write-validation/--no-write-validation",
    default=True,
    help=(
        "Validate Conductor-owned Edit/Write content before writing. "
        "Disable only for intentional corrupt-byte fixtures."
    ),
)
def exec_cmd(
    provider_id: str | None,
    profile: str | None,
    auto: bool,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    tools: str | None,
    permission_profile: str | None,
    sandbox: str | None,
    exclude: str | None,
    cwd: str | None,
    timeout_sec: int | None,
    max_stall_sec: int | None,
    strict_stall: bool,
    start_timeout_sec: float | None,
    max_iterations: int | None,
    allow_completion_stretch: bool,
    retry_on_stall: int,
    task: str | None,
    task_file: str | None,
    brief: str | None,
    brief_file: tuple[str, ...],
    auto_phase: bool,
    phase_anchor: tuple[str, ...],
    issue: str | None,
    issue_comment_limit: int,
    attach: tuple[str, ...],
    model: str | None,
    log_file: str | None,
    as_json: bool,
    verbose_route: bool,
    silent_route: bool,
    resume_session_id: str | None,
    offline: bool | None,
    preflight: bool,
    allow_short_brief: bool,
    ground_citations: bool,
    write_validation: bool,
) -> None:
    """Run a task as an agent session with tool access (exec mode)."""
    timeout_is_default = _parameter_is_default("timeout_sec")
    max_stall_is_default = _parameter_is_default("max_stall_sec")
    profile_spec = _load_named_profile(profile)
    provider_id = _resolve_layered_value(provider_id, env_key="CONDUCTOR_WITH")
    tags = _resolve_layered_value(
        tags,
        env_key="CONDUCTOR_TAGS",
        profile_value=profile_spec.tags if profile_spec else None,
    )
    prefer = _resolve_layered_value(
        prefer,
        env_key="CONDUCTOR_PREFER",
        profile_value=profile_spec.prefer if profile_spec else None,
    )
    effort = _resolve_layered_value(
        effort,
        env_key="CONDUCTOR_EFFORT",
        profile_value=profile_spec.effort if profile_spec else None,
    )
    permission_profile = _resolve_layered_value(
        permission_profile,
        env_key="CONDUCTOR_PERMISSION_PROFILE",
    )
    sandbox = _resolve_layered_value(
        sandbox,
        env_key="CONDUCTOR_SANDBOX",
        profile_value=profile_spec.sandbox if profile_spec else None,
    )
    exclude = _resolve_layered_value(exclude, env_key="CONDUCTOR_EXCLUDE")
    provider_id, auto = _apply_offline_flag(offline=offline, provider_id=provider_id, auto=auto)
    if offline is True and provider_id == "ollama":
        prefer = None
    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")
    if resume_session_id and auto:
        raise click.UsageError(
            "--resume requires --with <provider> (sessions are provider-specific)."
        )
    if provider_id and not auto:
        _enforce_local_provider_opt_in(provider_id, offline_requested=offline is True)

    brief_files = tuple(brief_file)
    if auto_phase and len(brief_files) > 1:
        raise click.UsageError(
            "use `--brief-file` repeated OR `--auto-phase` on a single brief, not both."
        )
    if len(brief_files) > 1 and any(path == "-" for path in brief_files):
        raise click.UsageError("multiple --brief-file phases cannot read from stdin ('-').")
    if len(brief_files) > 1 and any(value is not None for value in (task, task_file, brief)):
        raise click.UsageError(
            "brief source is ambiguous. Use repeated --brief-file without --brief, "
            "--task, or --task-file."
        )
    if len(brief_files) > 1 and issue is not None:
        raise click.UsageError("--issue cannot be combined with multiple --brief-file phases.")

    phase_inputs: list[tuple[str, BriefInput]] = []
    if len(brief_files) <= 1:
        brief_input = _read_task(
            task,
            task_file,
            brief=brief,
            brief_file=brief_files[0] if brief_files else None,
            issue=issue,
            issue_comment_limit=issue_comment_limit,
            cwd=cwd,
            attach=attach,
        )
        if auto_phase:
            auto_phases = _split_brief_into_auto_phases(
                brief_input.body,
                extra_anchors=phase_anchor,
            )
            if len(auto_phases) == 1 and auto_phases[0].title == "Brief":
                click.echo(
                    "[conductor] --auto-phase: no anchor headers found in brief; "
                    "running as single phase.",
                    err=True,
                )
                phase_inputs.append(
                    (brief_files[0] if brief_files else brief_input.source, brief_input)
                )
            else:
                phase_inputs.extend(
                    (
                        phase.title,
                        replace(brief_input, body=phase.body, source=phase.title),
                    )
                    for phase in auto_phases
                )
        else:
            phase_inputs.append(
                (brief_files[0] if brief_files else brief_input.source, brief_input)
            )
    else:
        for path in brief_files:
            phase_inputs.append(
                (
                    path,
                    _read_task(
                        None,
                        None,
                        brief_file=path,
                        cwd=cwd,
                        attach=attach,
                    ),
                )
            )

    phase_results: list[dict[str, object]] = []
    phase_summaries: list[str] = []
    final_response: CallResponse | None = None
    final_decision: RouteDecision | None = None
    final_session_log: SessionLog | None = None
    multi_phase = len(phase_inputs) > 1

    for idx, (brief_label, raw_brief_input) in enumerate(phase_inputs, start=1):
        brief_input = replace(
            raw_brief_input,
            body=_append_previous_phase_results(raw_brief_input.body, phase_summaries),
        )
        _warn_if_short_exec_brief(
            brief_input,
            allow_short_brief=allow_short_brief,
        )
        brief_input = _with_auto_close_instructions(brief_input)
        phase_started_at = time.monotonic()
        phase_start_head = _git_phase_head(cwd) if multi_phase else None
        try:
            response, decision, session_log = _run_exec_phase_dispatch(
                provider_id=provider_id,
                auto=auto,
                tags=tags,
                prefer=prefer,
                effort=effort,
                tools=tools,
                permission_profile=permission_profile,
                sandbox=sandbox,
                exclude=exclude,
                cwd=cwd,
                timeout_sec=timeout_sec,
                max_stall_sec=max_stall_sec,
                strict_stall=strict_stall,
                start_timeout_sec=start_timeout_sec,
                max_iterations=max_iterations,
                allow_completion_stretch=allow_completion_stretch,
                retry_on_stall=retry_on_stall,
                body=brief_input.body,
                attachments=brief_input.attachments,
                model=model,
                log_file=log_file,
                as_json=as_json and not multi_phase,
                verbose_route=verbose_route,
                silent_route=silent_route or (as_json and multi_phase),
                resume_session_id=resume_session_id,
                offline=offline,
                preflight=preflight,
                write_validation=write_validation,
                timeout_is_default=timeout_is_default,
                max_stall_is_default=max_stall_is_default,
                raise_on_error=multi_phase,
            )
        except _ExecPhaseError as e:
            duration_ms = int((time.monotonic() - phase_started_at) * 1000)
            phase_results.append(
                {
                    "brief": brief_label,
                    "exit": e.exit_status,
                    "commits": _git_phase_commit_count(cwd, phase_start_head),
                    "duration_ms": duration_ms,
                }
            )

            click.echo(
                f"conductor: phase {idx} failed ({e.exit_status}): {e.message}",
                err=True,
            )

            if as_json:
                click.echo(json.dumps({"phases": phase_results, "ok": False}, indent=2))
            sys.exit(e.exit_code)



        final_response = response
        final_decision = decision
        final_session_log = session_log
        duration_ms = int((time.monotonic() - phase_started_at) * 1000)
        commits = _git_phase_commit_count(cwd, phase_start_head)
        if multi_phase:
            phase_results.append(
                {
                    "brief": brief_label,
                    "exit": "ok",
                    "commits": commits,
                    "duration_ms": duration_ms,
                }
            )
            phase_summaries.append(_git_phase_summary(cwd, idx, phase_start_head))
            if not (silent_route or as_json):
                click.echo(f"[conductor] phase {idx} complete: {brief_label}", err=True)

    assert final_response is not None
    if ground_citations:
        _emit_grounding_warnings(final_response.text, cwd or os.getcwd())
    if multi_phase and as_json:
        click.echo(json.dumps({"phases": phase_results, "ok": True}, indent=2))
    else:
        _emit_call(
            final_response,
            as_json=as_json,
            decision=final_decision,
            auth_prompts=_collect_session_auth_prompts(final_session_log),
        )


# --------------------------------------------------------------------------- #
# swarm — sequential multi-task coding supervisor
# --------------------------------------------------------------------------- #


@main.group(name="swarm", invoke_without_command=True)
@click.pass_context
@click.option(
    "--brief",
    "briefs",
    multiple=True,
    help="Path to one task brief. Repeat for multiple independent tasks.",
)
@click.option(
    "--provider",
    "provider_id",
    help=(
        "Provider to run for every task. Omit, or pass auto/pool, to route "
        "each lane across compatible providers with operational fallback."
    ),
)
@click.option(
    "--tags",
    default=None,
    help="Comma-separated task tags for provider-pool routing.",
)
@click.option(
    "--base-branch",
    default="main",
    show_default=True,
    help="Branch each task worktree forks from.",
)
@click.option(
    "--auto-merge",
    is_flag=True,
    default=False,
    help="Open, review, and squash-merge each task PR.",
)
@click.option(
    "--max-iterations",
    default=None,
    type=click.IntRange(min=1),
    help=EXEC_MAX_ITERATIONS_HELP,
)
@click.option(
    "--max-stall-seconds",
    "max_stall_sec",
    default=DEFAULT_EXEC_MAX_STALL_SEC,
    type=int,
    help="Per-task no-output watchdog in seconds. Set 0 to disable.",
)
@click.option(
    "--timeout",
    "timeout_sec",
    default=None,
    type=int,
    help="Per-task wall-clock timeout in seconds. Unbounded by default.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit structured swarm results as JSON.",
)
@click.option(
    "--preflight-only",
    is_flag=True,
    default=False,
    help="Inspect likely brief overlap, write the manifest, and exit before workers run.",
)
@click.option(
    "--keep-worktrees-on-failure/--remove-worktrees-on-failure",
    default=True,
    show_default=True,
    help="Preserve failed task worktrees for inspection.",
)
@click.option(
    "--max-parallel",
    default=1,
    type=click.IntRange(min=1),
    show_default=True,
    help="Maximum number of swarm tasks to run concurrently.",
)
def swarm(
    ctx: click.Context,
    briefs: tuple[str, ...],
    provider_id: str | None,
    tags: str | None,
    base_branch: str,
    auto_merge: bool,
    max_iterations: int | None,
    max_stall_sec: int | None,
    timeout_sec: int | None,
    as_json: bool,
    preflight_only: bool,
    keep_worktrees_on_failure: bool,
    max_parallel: int,
) -> None:
    """Run independent coding briefs under Conductor's supervisor."""
    if ctx.invoked_subcommand is not None:
        return
    if not briefs:
        raise click.UsageError("pass at least one --brief.")

    results: list[SwarmTaskResult] = []
    repo_root = _swarm_repo_root()
    _validate_swarm_briefs(briefs, repo_root=repo_root)
    preflight = _swarm_preflight_plan(briefs, repo_root=repo_root)
    if len(briefs) > 1:
        warning = _format_swarm_preflight_warning(preflight)
        if warning is not None:
            click.echo(warning, err=True)
    try:
        base_ref = _run_swarm_git(
            ["rev-parse", "--verify", base_branch],
            cwd=repo_root,
        ).stdout.strip()
    except RuntimeError as e:
        raise click.UsageError(str(e)) from e
    run_started_monotonic = time.monotonic()
    started_datetime = datetime.now(UTC)
    started_at = started_datetime.isoformat().replace("+00:00", "Z")
    run_id = _new_swarm_run_id(started_datetime)
    manifest_path = _swarm_runs_dir(repo_root) / f"{run_id}.json"
    manifest_tasks = _swarm_initial_task_records(
        briefs,
        repo_root=repo_root,
        provider_id=provider_id,
    )
    _write_swarm_manifest(
        manifest_path,
        _swarm_manifest_payload(
            run_id=run_id,
            repo_root=repo_root,
            base_branch=base_branch,
            base_ref=base_ref,
            started_at=started_at,
            ended_at=None,
            duration_ms=None,
            tasks=manifest_tasks,
            preflight=preflight,
        ),
    )
    if preflight_only:
        for line in _format_swarm_preflight_plan(preflight):
            click.echo(line, err=as_json)
        if as_json:
            click.echo(json.dumps({"ok": True, "preflight": preflight}, indent=2))
        return

    def record_phase(brief_path: str, phase: str) -> None:
        _record_swarm_task_phase(
            manifest_path=manifest_path,
            brief_path=brief_path,
            phase=phase,
        )

    def run_one(brief_path: str) -> SwarmTaskResult:
        return _run_swarm_task(
            brief_path=brief_path,
            provider_id=provider_id,
            tags=tags,
            base_branch=base_branch,
            base_ref=base_ref,
            auto_merge=auto_merge,
            max_iterations=max_iterations,
            max_stall_sec=max_stall_sec,
            timeout_sec=timeout_sec,
            keep_worktrees_on_failure=keep_worktrees_on_failure,
            as_json=as_json,
            defer_ship=auto_merge,
            phase_recorder=record_phase,
        )

    if max_parallel == 1 or len(briefs) == 1:
        for brief_path in briefs:
            try:
                results.append(run_one(brief_path))
            except Exception as e:
                results.append(_failed_swarm_thread_result(brief_path, e))
    else:
        result_slots: list[SwarmTaskResult | None] = [None] * len(briefs)
        with ThreadPoolExecutor(max_workers=max_parallel) as executor:
            futures = {
                executor.submit(run_one, brief_path): index
                for index, brief_path in enumerate(briefs)
            }
            for future in as_completed(futures):
                index = futures[future]
                brief_path = briefs[index]
                try:
                    result_slots[index] = future.result()
                except Exception as e:
                    result_slots[index] = _failed_swarm_thread_result(brief_path, e)
        results = [result for result in result_slots if result is not None]

    ended_at = _swarm_now_iso()
    duration_ms = int((time.monotonic() - run_started_monotonic) * 1000)
    manifest_tasks = _swarm_manifest_task_records(results)
    _write_swarm_manifest(
        manifest_path,
        _swarm_manifest_payload(
            run_id=run_id,
            repo_root=repo_root,
            base_branch=base_branch,
            base_ref=base_ref,
            started_at=started_at,
            ended_at=None,
            duration_ms=None,
            tasks=manifest_tasks,
            preflight=preflight,
        ),
    )
    merge_plan = None
    if auto_merge:
        merge_plan = _orchestrate_swarm_auto_merge(
            tasks=manifest_tasks,
            repo_root=repo_root,
            base_branch=base_branch,
            manifest_path=manifest_path,
            preflight=preflight,
            phase_recorder=record_phase,
        )
    manifest_payload = _swarm_manifest_payload(
        run_id=run_id,
        repo_root=repo_root,
        base_branch=base_branch,
        base_ref=base_ref,
        started_at=started_at,
        ended_at=ended_at,
        duration_ms=duration_ms,
        tasks=manifest_tasks,
        preflight=preflight,
    )
    if merge_plan is not None:
        manifest_payload["merge_plan"] = merge_plan
    manifest_payload, cortex_bookkeeping = _swarm_payload_with_cortex_bookkeeping(
        repo_root=repo_root,
        run_id=run_id,
        manifest_path=manifest_path,
        payload=manifest_payload,
    )
    _write_swarm_manifest(manifest_path, manifest_payload)

    payload = _swarm_summary_from_task_records(manifest_tasks)
    if as_json:
        _emit_swarm_cortex_bookkeeping_notice(cortex_bookkeeping, err=True)
        click.echo(json.dumps(payload, indent=2))
    else:
        for task in manifest_tasks:
            detail = "{status}: {brief} -> {branch}".format(
                status=task.get("status", "unknown"),
                brief=task.get("brief", ""),
                branch=task.get("branch", ""),
            )
            pr_url = task.get("pr_url")
            if isinstance(pr_url, str) and pr_url:
                detail += f" ({pr_url})"
            click.echo(detail)
        summary_display = {
            **payload,
            "provider_failed_count": sum(
                1 for task in manifest_tasks if task.get("status") == SWARM_PROVIDER_FAILED_STATUS
            ),
        }
        click.echo(
            "ok={ok} shipped={shipped_count} failed={failed_count} "
            "no-changes={no_changes_count} "
            "provider-failed={provider_failed_count}".format(**summary_display)
        )
        _emit_swarm_cortex_bookkeeping_notice(cortex_bookkeeping, err=False)
    if not payload["ok"]:
        sys.exit(1)


@swarm.command(name="status")
@click.argument("run_id_or_path", required=False)
@click.option(
    "--latest",
    is_flag=True,
    default=False,
    help="Inspect the newest manifest in .cache/conductor/swarm/runs.",
)
def swarm_status(run_id_or_path: str | None, latest: bool) -> None:
    """Print a concise status view for a swarm run manifest."""
    path = _resolve_swarm_manifest_path(run_id_or_path, latest=latest)
    payload = _read_swarm_manifest(path)
    for line in _format_swarm_manifest_status(payload, path=path):
        click.echo(line)


@swarm.command(name="resume")
@click.argument("run_id_or_path")
@click.argument("task_selector")
@click.option(
    "--retry-ship",
    is_flag=True,
    default=False,
    help="Delegate to swarm retry-ship for the selected task.",
)
@click.option(
    "--auto-merge/--no-auto-merge",
    default=True,
    show_default=True,
    help="When --retry-ship is set, retry shipping with open-pr.sh auto-merge enabled.",
)
def swarm_resume(
    run_id_or_path: str,
    task_selector: str,
    retry_ship: bool,
    auto_merge: bool,
) -> None:
    """Orient recovery for one manifest task without rerunning implementation.

    TASK_SELECTOR is a 1-based task index, exact branch name, or exact brief path
    from the manifest.
    """
    if retry_ship:
        _run_swarm_retry_ship_command(
            run_id_or_path=run_id_or_path,
            task_selector=task_selector,
            auto_merge=auto_merge,
        )
        return
    path = _resolve_swarm_manifest_path(run_id_or_path, latest=False)
    payload = _read_swarm_manifest(path)
    tasks = _swarm_manifest_tasks(payload, path=path)
    task_index, task = _select_swarm_manifest_task(tasks, task_selector, path=path)
    for line in _format_swarm_resume(
        payload=payload,
        path=path,
        task_index=task_index,
        task=task,
    ):
        click.echo(line)


@swarm.command(name="retry-ship")
@click.argument("run_id_or_path")
@click.argument("task_selector")
@click.option(
    "--auto-merge/--no-auto-merge",
    default=True,
    show_default=True,
    help="Retry shipping with open-pr.sh auto-merge enabled.",
)
def swarm_retry_ship(run_id_or_path: str, task_selector: str, auto_merge: bool) -> None:
    """Retry shipping one manifest task without rerunning implementation.

    TASK_SELECTOR is a 1-based task index, exact branch name, or exact brief path
    from the manifest.
    """
    _run_swarm_retry_ship_command(
        run_id_or_path=run_id_or_path,
        task_selector=task_selector,
        auto_merge=auto_merge,
    )


# --------------------------------------------------------------------------- #
# route — dry-run the router and print what would happen
# --------------------------------------------------------------------------- #


VALID_ROUTE_KINDS = ("call", "exec", "review")


def _route_reason_code(reason: str) -> str:
    lowered = reason.lower()
    if "local/offline-only" in lowered or LOCAL_ONLINE_OPT_IN_ENV.lower() in lowered:
        return "local_provider_requires_opt_in"
    if "gemini native review requires" in lowered or "code review extension" in lowered:
        return "missing_native_review_extension"
    if "does not support tools" in lowered:
        return "provider_lacks_tools"
    if "excluded" in lowered:
        return "excluded_by_user"
    if "muted" in lowered:
        return "muted"
    if "rate-limit" in lowered or "rate limited" in lowered:
        return "rate_limited_recently"
    if "not configured" in lowered or "not found" in lowered or "install" in lowered:
        return "missing_credentials"
    if "not tagged" in lowered:
        return "provider_wrong_mode"
    return "provider_not_viable"


def _route_provider_mode(kind: str, provider_obj: object) -> str:
    if kind == "review":
        if isinstance(provider_obj, NativeReviewProvider):
            return "native_review"
        return "hosted_review"
    if kind == "exec":
        return "exec"
    return "call"


def _route_provider_assessment(
    name: str,
    *,
    kind: str,
    tools: frozenset[str],
    excluded: frozenset[str],
    enforce_local_online_policy: bool = False,
) -> dict[str, object]:
    try:
        provider_obj = get_provider(name)
    except KeyError:
        return {
            "provider": name,
            "viable": False,
            "reason_code": "unknown_provider",
            "reason": "unknown provider",
        }
    base = {
        "provider": name,
        "tags": sorted(getattr(provider_obj, "tags", ())),
        "runtime_kind": getattr(provider_obj, "runtime_kind", PROVIDER_RUNTIME_TEXT_ONLY),
        "supported_tools": sorted(getattr(provider_obj, "supported_tools", frozenset())),
        "mode": _route_provider_mode(kind, provider_obj),
    }
    if name in excluded:
        reason = "excluded by caller"
        return {
            **base,
            "viable": False,
            "reason_code": _route_reason_code(reason),
            "reason": reason,
        }
    if kind == "review" and NATIVE_REVIEW_TAG not in getattr(provider_obj, "tags", ()):
        reason = "provider is not tagged code-review"
        return {
            **base,
            "viable": False,
            "reason_code": _route_reason_code(reason),
            "reason": reason,
        }
    ok, reason = (
        provider_obj.review_configured()
        if kind == "review" and isinstance(provider_obj, NativeReviewProvider)
        else provider_obj.configured()
    )
    if not ok:
        reason = reason or f"{name} is not configured"
        return {
            **base,
            "viable": False,
            "reason_code": _route_reason_code(reason),
            "reason": reason,
        }
    if enforce_local_online_policy:
        reason = _local_provider_online_policy_reason(
            name,
            offline_requested=False,
        )
        if reason is not None:
            return {
                **base,
                "viable": False,
                "reason_code": _route_reason_code(reason),
                "reason": reason,
            }
    if kind == "exec" and tools and not tools.issubset(
        getattr(provider_obj, "supported_tools", frozenset())
    ):
        missing = sorted(tools - getattr(provider_obj, "supported_tools", frozenset()))
        reason = f"does not support tools: {missing}"
        return {
            **base,
            "viable": False,
            "reason_code": "provider_lacks_tools",
            "reason": reason,
        }
    return {
        **base,
        "viable": True,
        "reason_code": None,
        "reason": None,
        "model": getattr(provider_obj, "default_model", None),
    }


def _route_validation_payload(
    *,
    kind: str,
    provider_id: str | None,
    tags: tuple[str, ...],
    prefer: str | None,
    effort: str | int,
    tools: frozenset[str],
    sandbox: str,
    exclude: frozenset[str],
    base: str | None,
    cwd: str | None,
    estimated_input_tokens: int | None,
    estimated_output_tokens: int | None,
) -> tuple[dict[str, object], int]:
    if kind not in VALID_ROUTE_KINDS:
        raise click.UsageError(
            f"--kind={kind!r} is not valid. Use one of: {list(VALID_ROUTE_KINDS)}."
        )
    requested_tags = tuple(sorted({*tags, *(("code-review",) if kind == "review" else ())}))
    requested = {
        "kind": kind,
        "provider": provider_id,
        "tags": list(requested_tags),
        "tools": sorted(tools),
        "base": base,
        "cwd": str(Path(cwd).expanduser()) if cwd else None,
    }
    provider_names = tuple(known_providers())
    if provider_id is not None and provider_id not in provider_names:
        payload = {
            "requested": requested,
            "viable": False,
            "selected_provider": None,
            "selected_model": None,
            "route_mode": None,
            "candidates": [],
            "excluded": [
                {
                    "provider": provider_id,
                    "viable": False,
                    "reason_code": "unknown_provider",
                    "reason": "unknown provider",
                }
            ],
        }
        return payload, 2

    candidate_names = (provider_id,) if provider_id else provider_names
    assessments = [
        _route_provider_assessment(
            name,
            kind=kind,
            tools=tools,
            excluded=exclude,
            enforce_local_online_policy=True,
        )
        for name in candidate_names
    ]
    viable_assessments = [entry for entry in assessments if entry["viable"]]

    decision_payload: dict[str, object] | None = None
    selected_provider: str | None = None
    selected_model: str | None = None
    route_mode: str | None = None
    ordered_candidates = viable_assessments

    if provider_id is not None:
        if viable_assessments:
            selected = viable_assessments[0]
            selected_provider = str(selected["provider"])
            selected_model = str(selected["model"])
            route_mode = str(selected["mode"])
    elif kind == "review":
        try:
            decision, _review_reasons, _plan = _build_review_route_decision(
                effort=effort,
                tags=tuple(tags),
                prefer=prefer,
                exclude=exclude,
                estimated_input_tokens=estimated_input_tokens,
            )
            selected_provider = decision.provider
            selected_assessment = next(
                entry for entry in viable_assessments if entry["provider"] == decision.provider
            )
            selected_model = str(selected_assessment["model"])
            route_mode = str(selected_assessment["mode"])
            ranked_order = [candidate.name for candidate in decision.ranked]
            by_name = {str(entry["provider"]): entry for entry in viable_assessments}
            ordered_candidates = [by_name[name] for name in ranked_order if name in by_name]
            decision_payload = asdict(decision)
        except _ReviewRouteSelectionError:
            selected_provider = None
    else:
        try:
            _provider, decision = pick(
                list(requested_tags),
                prefer=prefer or "balanced",
                effort=effort,
                tools=tools if kind == "exec" else frozenset(),
                sandbox=sandbox,
                exclude=exclude,
                shadow=True,
                estimated_input_tokens=estimated_input_tokens,
                estimated_output_tokens=estimated_output_tokens,
            )
            decision, _exclusion_message = _apply_auto_route_exclusion_rules(
                decision,
                user_tags=tuple(tags),
                offline_requested=False,
            )
            selected_provider = decision.provider
            selected_assessment = next(
                entry for entry in viable_assessments if entry["provider"] == decision.provider
            )
            selected_model = str(selected_assessment["model"])
            route_mode = str(selected_assessment["mode"])
            ranked_order = [candidate.name for candidate in decision.ranked]
            by_name = {str(entry["provider"]): entry for entry in viable_assessments}
            ordered_candidates = [by_name[name] for name in ranked_order if name in by_name]
            decision_payload = asdict(decision)
        except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError):
            selected_provider = None

    excluded_entries = [entry for entry in assessments if not entry["viable"]]
    payload = {
        "requested": requested,
        "viable": selected_provider is not None,
        "selected_provider": selected_provider,
        "selected_model": selected_model,
        "route_mode": route_mode,
        "candidates": ordered_candidates,
        "excluded": excluded_entries,
        "decision": decision_payload,
    }
    return payload, 0 if selected_provider is not None else 2


def _emit_route_validation_text(payload: dict[str, object]) -> None:
    if payload["viable"]:
        click.echo(
            f"route viable: {payload['selected_provider']} "
            f"({payload['route_mode']})"
        )
        return
    click.echo("route not viable")
    for entry in payload["excluded"]:
        click.echo(
            f"  - {entry['provider']}: {entry['reason_code']} — {entry['reason']}"
        )


def _provider_capability_matrix(name: str) -> dict[str, object]:
    return {
        "call": _route_provider_assessment(
            name,
            kind="call",
            tools=frozenset(),
            excluded=frozenset(),
        ),
        "review": _route_provider_assessment(
            name,
            kind="review",
            tools=frozenset(),
            excluded=frozenset(),
        ),
        "exec_read_only": _route_provider_assessment(
            name,
            kind="exec",
            tools=EXEC_PERMISSION_PROFILES["read-only"],
            excluded=frozenset(),
        ),
        "exec_full": _route_provider_assessment(
            name,
            kind="exec",
            tools=EXEC_PERMISSION_PROFILES["full"],
            excluded=frozenset(),
        ),
    }


@main.command()
@click.option(
    "--kind",
    default=None,
    help="Validate a route kind without provider tokens: call | exec | review.",
)
@click.option("--with", "provider_id", default=None, help="Validate a pinned provider.")
@click.option("--base", default=None, help="For review validation: base ref context.")
@click.option("--cwd", default=None, help="Working directory context for validation.")
@click.option("--tags", default=None, help="Comma-separated task tags.")
@click.option(
    "--prefer",
    default=None,
    help=f"Routing preference: {' | '.join(VALID_PREFER_MODES)}.",
)
@click.option(
    "--effort",
    default=None,
    help=f"Thinking depth: {' | '.join(VALID_EFFORT_LEVELS)} or integer budget.",
)
@click.option("--tools", default=None, help="Comma-separated tool set.")
@click.option(
    "--permission-profile",
    default=None,
    help="Exec tool whitelist profile: read-only | patch | full.",
)
@click.option("--sandbox", default=None, help="Deprecated and ignored.")
@click.option("--exclude", default=None, help="Comma-separated providers to exclude.")
@click.option(
    "--estimated-input-tokens",
    default=None,
    type=click.IntRange(min=0),
    help=(
        "Estimated prompt/diff input tokens for cost scoring. "
        f"Default: {DEFAULT_ESTIMATED_INPUT_TOKENS}."
    ),
)
@click.option(
    "--estimated-output-tokens",
    default=None,
    type=click.IntRange(min=0),
    help=(f"Estimated output tokens for cost scoring. Default: {DEFAULT_ESTIMATED_OUTPUT_TOKENS}."),
)
@click.option("--json", "as_json", is_flag=True, default=False)
def route(
    kind: str | None,
    provider_id: str | None,
    base: str | None,
    cwd: str | None,
    tags: str | None,
    prefer: str | None,
    effort: str | None,
    tools: str | None,
    permission_profile: str | None,
    sandbox: str | None,
    exclude: str | None,
    estimated_input_tokens: int | None,
    estimated_output_tokens: int | None,
    as_json: bool,
) -> None:
    """Dry-run the router: show which provider would be picked and why.

    Makes no upstream calls. Used for sanity-checking config + routing
    before a real `call` or `exec`.
    """
    permission_profile_value = _validate_permission_profile(permission_profile)
    tools_set = _resolve_exec_tools(
        tools,
        permission_profile=permission_profile_value,
    )
    sandbox_value = _validate_sandbox(sandbox, warn=sandbox is not None)
    effort_value = _parse_effort(effort)
    tag_values = tuple(_parse_csv(tags))
    exclude_values = _permission_profile_excludes(
        exclude,
        permission_profile=permission_profile_value,
    )

    if kind is not None:
        payload, exit_code = _route_validation_payload(
            kind=kind,
            provider_id=provider_id,
            tags=tag_values,
            prefer=_validate_prefer(prefer) if prefer is not None else None,
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            exclude=exclude_values,
            base=base,
            cwd=cwd,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )
        if as_json:
            click.echo(json.dumps(payload, default=str, indent=2))
        else:
            _emit_route_validation_text(payload)
        sys.exit(exit_code)

    if provider_id is not None:
        raise click.UsageError("--with requires --kind for route validation.")

    try:
        _provider, decision = pick(
            list(tag_values),
            prefer=_validate_prefer(prefer),
            effort=effort_value,
            tools=tools_set,
            sandbox=sandbox_value,
            exclude=exclude_values,
            shadow=True,
            estimated_input_tokens=estimated_input_tokens,
            estimated_output_tokens=estimated_output_tokens,
        )
        decision, exclusion_message = _apply_auto_route_exclusion_rules(
            decision,
            user_tags=tuple(_parse_csv(tags)),
            offline_requested=False,
        )
    except (NoConfiguredProvider, InvalidRouterRequest, MutedProvidersError) as e:
        if as_json:
            click.echo(json.dumps({"error": str(e)}, indent=2))
        else:
            click.echo(f"conductor: {e}", err=True)
        sys.exit(2)
    if exclusion_message:
        click.echo(exclusion_message, err=True)

    if as_json:
        click.echo(json.dumps(asdict(decision), default=str, indent=2))
        return

    click.echo(f"→ would pick: {decision.provider}")
    click.echo(
        f"  tier: {decision.tier}"
        f"  ·  prefer: {decision.prefer}"
        f"  ·  effort: {decision.effort}"
        f" (thinking budget: {decision.thinking_budget} tokens)"
    )
    click.echo(
        "  cost estimate: "
        f"{decision.estimated_input_tokens:,} input tokens · "
        f"{decision.estimated_output_tokens:,} output tokens · "
        f"{decision.estimated_thinking_tokens:,} thinking tokens"
    )
    if decision.matched_tags:
        click.echo(f"  matched tags: {','.join(decision.matched_tags)}")
    if decision.tools_requested:
        click.echo(f"  tools requested: {','.join(decision.tools_requested)}")
    click.echo("")
    click.echo("Full ranking:")
    for line in _format_route_ranking(decision):
        click.echo("  " + line.removeprefix("[conductor] "))


# --------------------------------------------------------------------------- #
# router defaults — manage persistent tag-default preferences
# --------------------------------------------------------------------------- #


@main.group(name="router")
def router_cmd() -> None:
    """Manage persistent auto-router defaults."""


@router_cmd.group(name="defaults")
def router_defaults_cmd() -> None:
    """Inspect or edit tag → provider defaults for auto-routing."""


@router_defaults_cmd.command(name="list")
def router_defaults_list() -> None:
    """Print the effective tag-default mappings."""
    try:
        home_defaults = load_home_tag_defaults()
        effective = load_tag_defaults()
        repo_path = repo_router_defaults_path()
        repo_defaults = {}
        if repo_path.exists():
            repo_merged = load_tag_defaults(cwd=repo_path.parent.parent)
            repo_defaults = {
                tag: provider
                for tag, provider in repo_merged.items()
                if home_defaults.get(tag) != provider or tag not in home_defaults
            }
    except RouterDefaultsError as e:
        raise click.ClickException(str(e)) from e

    if not effective:
        click.echo("(no router defaults)")
        return

    for tag, provider in sorted(effective.items()):
        if tag in repo_defaults:
            source = "repo override"
        elif tag in home_defaults:
            source = "home"
        else:
            source = "effective"
        click.echo(f"{tag} = {provider} ({source})")


@router_defaults_cmd.command(name="set")
@click.argument("tag")
@click.argument("provider")
def router_defaults_set(tag: str, provider: str) -> None:
    """Write a home-level tag-default mapping."""
    if provider not in known_providers():
        raise click.UsageError(
            f"unknown provider {provider!r}. Known providers: {', '.join(known_providers())}."
        )
    try:
        path = set_home_tag_default(tag, provider)
    except RouterDefaultsError as e:
        raise click.UsageError(str(e)) from e
    click.echo(f"set {tag} → {provider} in {path}")


@router_defaults_cmd.command(name="unset")
@click.argument("tag")
def router_defaults_unset(tag: str) -> None:
    """Remove a home-level tag-default mapping."""
    try:
        path, existed = unset_home_tag_default(tag)
    except RouterDefaultsError as e:
        raise click.UsageError(str(e)) from e
    if existed:
        click.echo(f"unset {tag} from {path}")
        return
    click.echo(f"no router default for {tag} in {path}")


# --------------------------------------------------------------------------- #
# config — show effective configuration
# --------------------------------------------------------------------------- #


@main.command()
@click.argument("subcommand", type=click.Choice(["show"]))
@click.option("--json", "as_json", is_flag=True, default=False)
def config(subcommand: str, as_json: bool) -> None:
    """Inspect conductor configuration (currently: `show` only)."""
    if subcommand != "show":
        raise click.UsageError(f"unknown config subcommand: {subcommand}")

    # Effective config is derived from env vars (no config file in v0.2 yet).
    env_overrides = {
        "CONDUCTOR_PREFER": os.environ.get("CONDUCTOR_PREFER"),
        "CONDUCTOR_EFFORT": os.environ.get("CONDUCTOR_EFFORT"),
        "CONDUCTOR_TAGS": os.environ.get("CONDUCTOR_TAGS"),
        "CONDUCTOR_SANDBOX": os.environ.get("CONDUCTOR_SANDBOX"),
        "CONDUCTOR_PERMISSION_PROFILE": os.environ.get("CONDUCTOR_PERMISSION_PROFILE"),
        "CONDUCTOR_WITH": os.environ.get("CONDUCTOR_WITH"),
        "CONDUCTOR_EXCLUDE": os.environ.get("CONDUCTOR_EXCLUDE"),
    }
    effective = {
        "prefer": env_overrides["CONDUCTOR_PREFER"] or "balanced",
        "effort": env_overrides["CONDUCTOR_EFFORT"] or "medium",
        "tags": _parse_csv(env_overrides["CONDUCTOR_TAGS"]),
        "sandbox": env_overrides["CONDUCTOR_SANDBOX"] or "none",
        "permission_profile": env_overrides["CONDUCTOR_PERMISSION_PROFILE"] or None,
        "with": env_overrides["CONDUCTOR_WITH"] or None,
        "exclude": _parse_csv(env_overrides["CONDUCTOR_EXCLUDE"]),
    }

    sources: dict[str, str] = {
        key: ("env" if val is not None else "default") for key, val in env_overrides.items()
    }
    payload = {
        "version": __version__,
        "effective": effective,
        "sources": sources,
        "known_providers": known_providers(),
    }

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"conductor v{payload['version']} — effective config")
    click.echo("")
    for key, val in effective.items():
        src = sources[f"CONDUCTOR_{key.upper()}"]
        if isinstance(val, list):
            val_str = ",".join(val) or "(none)"
        elif val is None:
            val_str = "(unset)"
        else:
            val_str = val
        click.echo(f"  {key:<8} = {val_str:<20}  (from: {src})")
    click.echo("")
    click.echo(f"Known providers: {', '.join(payload['known_providers'])}")
    click.echo("Run `conductor list` for per-provider configured status.")


# --------------------------------------------------------------------------- #
# profiles — inspect built-in + user-defined defaults
# --------------------------------------------------------------------------- #


@main.group(name="profiles")
def profiles_cmd() -> None:
    """Inspect named profiles for call/exec defaults."""


@profiles_cmd.command(name="list")
def profiles_list() -> None:
    """List built-in and user-defined profiles."""
    try:
        profiles = load_profiles()
    except ProfileError as e:
        raise click.UsageError(str(e)) from e

    for name in sorted(profiles):
        spec = profiles[name]
        click.echo(
            f"{name:<12} "
            f"prefer={spec.prefer or '-'} "
            f"effort={spec.effort or '-'} "
            f"tags={spec.tags or '-'} "
            f"sandbox={spec.sandbox or '-'} "
            f"[{spec.source}]"
        )


@profiles_cmd.command(name="show")
@click.argument("name")
def profiles_show(name: str) -> None:
    """Show one profile and the precedence rules around it."""
    try:
        spec = get_profile(name)
    except ProfileError as e:
        raise click.UsageError(str(e)) from e

    click.echo(f"{spec.name} [{spec.source}]")
    click.echo(f"  prefer   = {spec.prefer or '(unset)'}")
    click.echo(f"  effort   = {spec.effort or '(unset)'}")
    click.echo(f"  tags     = {spec.tags or '(unset)'}")
    click.echo(f"  sandbox  = {spec.sandbox or '(unset)'}")
    click.echo("")
    click.echo(PROFILE_PRECEDENCE_TEXT)


# --------------------------------------------------------------------------- #
# list — show provider menu + configured status
# --------------------------------------------------------------------------- #


def _tools_label(provider) -> str:
    """Compact tool-support label for display: 'all', 'none', or sorted list."""
    tools: frozenset[str] = getattr(provider, "supported_tools", frozenset())
    if tools == TOOL_NAMES:
        return "all"
    if not tools:
        return "none"
    return ",".join(sorted(tools))


def _provider_rows() -> list[dict]:
    muted = set(load_muted_provider_ids(known=set(known_providers())))
    rows = []
    for name in known_providers():
        provider = get_provider(name)
        ok, reason = provider.configured()
        local_policy = _local_provider_policy(name)
        rows.append(
            {
                "provider": name,
                "configured": ok,
                "readiness": _provider_readiness_label(name, ok),
                "local_policy": local_policy,
                "reason": None if ok else reason,
                # Copy-pasteable shell one-liner that takes the user from
                # "not configured" to "configured". None for providers
                # without a canonical recipe (e.g. user-defined shell
                # providers).
                "fix_command": (None if ok else _provider_fix_command(provider, reason)),
                "default_model": _provider_default_model(provider),
                "tags": list(provider.tags),
                "tier": provider.quality_tier,
                "runtime": _provider_runtime_kind(provider),
                "muted": name in muted,
                "tools": _tools_label(provider),
                "capabilities": _provider_capability_matrix(name),
            }
        )
    return rows


@main.command(name="list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the provider list as JSON.",
)
def list_cmd(as_json: bool) -> None:
    """Show every known provider and whether it's configured."""
    try:
        rows = _provider_rows()
    except MutedProvidersError as e:
        raise click.ClickException(str(e)) from e
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return

    name_w = max(len("PROVIDER"), max(len(r["provider"]) for r in rows))
    model_w = max(len("DEFAULT MODEL"), max(len(r["default_model"]) for r in rows))
    ready_w = max(len("READY"), max(len(r["readiness"]) for r in rows))
    tier_w = max(len("TIER"), max(len(r["tier"]) for r in rows))
    runtime_w = max(len("RUNTIME"), max(len(r["runtime"]) for r in rows))
    tags_w = max(len("TAGS"), max(len(",".join(r["tags"])) for r in rows))
    header = (
        f"{'PROVIDER':<{name_w}}  "
        f"{'READY':<{ready_w}}  "
        f"{'TIER':<{tier_w}}  "
        f"{'RUNTIME':<{runtime_w}}  "
        f"{'DEFAULT MODEL':<{model_w}}  "
        f"{'TAGS':<{tags_w}}  TOOLS"
    )
    click.echo(header)
    click.echo("-" * len(header))
    for r in rows:
        tags = ",".join(r["tags"])
        click.echo(
            f"{r['provider']:<{name_w}}  "
            f"{r['readiness']:<{ready_w}}  "
            f"{r['tier']:<{tier_w}}  "
            f"{r['runtime']:<{runtime_w}}  "
            f"{r['default_model']:<{model_w}}  "
            f"{tags:<{tags_w}}  "
            f"{r['tools']}"
        )
        if not r["configured"] and r["reason"]:
            click.echo(f"{'':<{name_w}}  {'':<{ready_w}}  └─ {r['reason']}")
        if not r["configured"] and r["fix_command"]:
            click.echo(f"{'':<{name_w}}  {'':<{ready_w}}  → fix: {r['fix_command']}")


# --------------------------------------------------------------------------- #
# smoke — run one or all providers' smoke tests
# --------------------------------------------------------------------------- #


class _SmokeDeadlineExpiredError(RuntimeError):
    pass


@contextlib.contextmanager
def _smoke_deadline(timeout_sec: float):
    if (
        timeout_sec <= 0
        or not hasattr(signal, "setitimer")
        or threading.current_thread() is not threading.main_thread()
    ):
        yield
        return

    previous_handler = signal.getsignal(signal.SIGALRM)
    previous_timer = signal.getitimer(signal.ITIMER_REAL)

    def raise_timeout(_signum, _frame) -> None:
        raise _SmokeDeadlineExpiredError

    signal.signal(signal.SIGALRM, raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, timeout_sec)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous_handler)
        if previous_timer[0] > 0:
            signal.setitimer(signal.ITIMER_REAL, previous_timer[0], previous_timer[1])


def _smoke_provider(name: str, *, provider_timeout: float) -> dict[str, object]:
    provider = get_provider(name)
    try:
        with _smoke_deadline(provider_timeout):
            ok, reason = provider.smoke()
    except _SmokeDeadlineExpiredError:
        ok, reason = False, f"smoke timed out after {provider_timeout:g}s"
    except Exception as exc:  # noqa: BLE001 - smoke reports every provider failure.
        ok = False
        reason = f"smoke raised {type(exc).__name__}: {exc}"
    return {"provider": name, "ok": ok, "reason": reason}


def _smoke_configured_result(
    name: str, *, provider_timeout: float
) -> tuple[bool, dict[str, object] | None]:
    provider = get_provider(name)
    try:
        with _smoke_deadline(provider_timeout):
            ok, _reason = provider.configured()
    except _SmokeDeadlineExpiredError:
        return False, {
            "provider": name,
            "ok": False,
            "reason": f"configured check timed out after {provider_timeout:g}s",
        }
    except Exception as exc:  # noqa: BLE001 - smoke reports every provider failure.
        return False, {
            "provider": name,
            "ok": False,
            "reason": f"configured check raised {type(exc).__name__}: {exc}",
        }
    return ok, None


@main.command()
@click.argument("provider_id", required=False)
@click.option(
    "--all",
    "run_all",
    is_flag=True,
    default=False,
    help="Run smoke tests for every configured provider.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit results as JSON.",
)
@click.option(
    "--provider-timeout",
    type=click.FloatRange(min=0.1),
    default=30.0,
    show_default=True,
    help="Maximum seconds to wait for each provider smoke test.",
)
def smoke(
    provider_id: str | None,
    run_all: bool,
    as_json: bool,
    provider_timeout: float,
) -> None:
    """Prove a provider's auth + endpoint actually work."""
    if provider_id and run_all:
        raise click.UsageError("pass a provider id OR --all, not both.")
    if not provider_id and not run_all:
        raise click.UsageError("pass a provider id or --all.")

    if provider_id:
        if provider_id not in known_providers():
            raise click.UsageError(f"unknown provider {provider_id!r}; known: {known_providers()}")
        targets = [provider_id]
    else:
        targets = []
        results = []
        any_failed = False
        for name in known_providers():
            configured, failure = _smoke_configured_result(
                name,
                provider_timeout=provider_timeout,
            )
            if failure is not None:
                results.append(failure)
                any_failed = True
            elif configured:
                targets.append(name)

    if provider_id:
        results = []
        any_failed = False
    for name in targets:
        result = _smoke_provider(name, provider_timeout=provider_timeout)
        results.append(result)
        if not result["ok"]:
            any_failed = True

    if as_json:
        click.echo(json.dumps(results, indent=2))
    else:
        if not results:
            click.echo("no configured providers to smoke-test.")
        for r in results:
            symbol = "✓" if r["ok"] else "✗"
            click.echo(f"{symbol} {r['provider']}")
            if not r["ok"] and r["reason"]:
                click.echo(f"  {r['reason']}")

    if any_failed:
        sys.exit(1)


# --------------------------------------------------------------------------- #
# doctor — diagnostic report (install + env + keychain)
# --------------------------------------------------------------------------- #


_DIAGNOSTIC_ENV_VARS = (
    "OLLAMA_BASE_URL",
    "CONDUCTOR_OLLAMA_MODEL",
    "OPENROUTER_API_KEY",
)

_HTTP_PROVIDER_CREDENTIAL_ENV_VARS = {
    "deepseek-chat": "OPENROUTER_API_KEY",
    "deepseek-reasoner": "OPENROUTER_API_KEY",
    "kimi": "OPENROUTER_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
}


def _credential_fingerprint(value: str) -> str:
    """Return a non-secret fingerprint for a resolved credential."""
    if len(value) <= 4:
        return value
    return f"{value[:-4]}...{value[-4:]}"


def _provider_default_model(provider: object) -> str:
    resolver = getattr(provider, "resolved_default_model", None)
    if callable(resolver):
        return str(resolver())
    return str(getattr(provider, "default_model", ""))


def _active_credential_row(provider: object, *, configured: bool) -> dict | None:
    """Summarize the credential Conductor would use for one provider.

    Only configured providers get a row. The doctor report remains derived
    from the same configured() gate the router uses, while this helper adds
    the provider-specific credential detail that was previously missing.
    """
    provider_name = getattr(provider, "name", None)
    if not configured or not isinstance(provider_name, str):
        return None

    if provider_name == "ollama":
        detail = "no credential (local)"
        return {
            "provider": provider_name,
            "kind": "local",
            "source": "local",
            "env_var": None,
            "fingerprint": None,
            "detail": detail,
        }

    # CLI-backed providers own auth inside the external CLI session; Conductor
    # does not resolve or persist a secret for them directly.
    if hasattr(provider, "auth_login_command"):
        cli_name = getattr(provider, "_cli", provider_name)
        detail = f"OAuth via `{cli_name}` CLI session (no env var)"
        return {
            "provider": provider_name,
            "kind": "cli_session",
            "source": "cli_session",
            "env_var": None,
            "fingerprint": None,
            "detail": detail,
        }

    env_var = _HTTP_PROVIDER_CREDENTIAL_ENV_VARS.get(provider_name)
    if env_var is None:
        return None

    value, source = credentials.resolve_with_source(env_var)
    if value is None or source is None:
        return None

    fingerprint = _credential_fingerprint(value)
    detail = f"{env_var} ({source}, {fingerprint})"
    return {
        "provider": provider_name,
        "kind": "env_var",
        "source": source,
        "env_var": env_var,
        "fingerprint": fingerprint,
        "detail": detail,
    }


def _diagnostic_payload() -> dict:
    muted_list = load_muted_provider_ids(known=set(known_providers()))
    muted = set(muted_list)
    providers_info = []
    active_credentials = []
    warnings: list[dict] = []
    for name in known_providers():
        provider = get_provider(name)
        ok, reason = provider.configured()
        local_policy = _local_provider_policy(name)
        provider_warnings: list[str] = []

        # Provider-specific health probes: daemon up but default model missing,
        # token nearly expired, etc. Kept in the CLI layer so each provider's
        # core interface stays minimal.
        if ok and hasattr(provider, "default_model_available"):
            model_ok, model_reason = provider.default_model_available()
            if not model_ok:
                provider_warnings.append(model_reason or "default model unavailable")
                warnings.append({"provider": name, "level": "warning", "message": model_reason})

        providers_info.append(
            {
                "provider": name,
                "configured": ok,
                "readiness": _provider_readiness_label(name, ok),
                "local_policy": local_policy,
                "reason": None if ok else reason,
                "fix_command": (None if ok else _provider_fix_command(provider, reason)),
                "default_model": _provider_default_model(provider),
                "tags": list(provider.tags),
                "quality_tier": provider.quality_tier,
                "runtime": _provider_runtime_kind(provider),
                "supports_effort": provider.supports_effort,
                "supported_tools": sorted(getattr(provider, "supported_tools", frozenset())),
                "capabilities": _provider_capability_matrix(name),
                "warnings": provider_warnings,
                "muted": name in muted,
            }
        )
        active = _active_credential_row(provider, configured=ok)
        if active is not None:
            active_credentials.append(active)

    env_info = []
    key_commands = credentials.load_key_commands()
    for var in _DIAGNOSTIC_ENV_VARS:
        in_env = var in os.environ
        in_keychain = credentials.keychain_has(var)
        has_key_command = var in key_commands
        if in_env:
            source = "env"
        elif has_key_command:
            source = "key_command"
        elif in_keychain:
            source = "keychain"
        else:
            source = None
        env_info.append(
            {
                "name": var,
                "in_env": in_env,
                "in_keychain": in_keychain,
                "has_key_command": has_key_command,
                "source": source,
            }
        )

    openrouter_value, _ = credentials.resolve_with_source("OPENROUTER_API_KEY")
    legacy_kimi_detected = any(
        (
            var in os.environ
            or var in credentials.load_key_commands()
            or credentials.keychain_has(var)
        )
        for var in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID")
    )

    if "DEEPSEEK_API_KEY" in os.environ and openrouter_value is None:
        warnings.append(
            {
                "provider": "deepseek-chat",
                "level": "warning",
                "message": (
                    "DEEPSEEK_API_KEY is deprecated for deepseek-chat and "
                    "deepseek-reasoner. Set OPENROUTER_API_KEY and run "
                    "`conductor init --only openrouter`."
                ),
            }
        )
    if legacy_kimi_detected and openrouter_value is None:
        warnings.append(
            {
                "provider": "kimi",
                "level": "warning",
                "message": (
                    "kimi now routes through OpenRouter; CLOUDFLARE_* credentials "
                    "are no longer used. Set OPENROUTER_API_KEY and run "
                    "`conductor init --only openrouter`."
                ),
            }
        )

    return {
        "version": __version__,
        "platform": sys.platform,
        "python": sys.version.split()[0],
        "providers": providers_info,
        "muted": muted_list,
        "credentials": env_info,
        "active_credentials": active_credentials,
        "agent_integration": _agent_integration_payload(),
        "git_state": _git_state_doctor_payload(),
        "warnings": warnings,
    }


def _agent_integration_payload() -> dict:
    """Summarize the state of agent-integration wiring (see agent_wiring.py)."""
    from conductor import agent_wiring

    detection = agent_wiring.detect()
    kinds = {a.kind for a in detection.managed}
    managed_files = [
        {"path": str(a.path), "kind": a.kind, "version": a.version} for a in detection.managed
    ]
    user_version_skew_files = _integration_version_skew_files(
        [f for f in managed_files if f["kind"] not in REPO_INTEGRATION_KINDS],
        binary_version=__version__,
    )
    repo_integration_status = _repo_integration_status_payloads(
        cwd=Path.cwd(),
        binary_version=__version__,
    )
    repo_version_skew_files = [
        str(entry["path"]) for entry in repo_integration_status if entry["stale"]
    ]
    version_skew_files = [*user_version_skew_files, *repo_version_skew_files]
    return {
        "claude_detected": detection.claude_detected,
        "claude_cli_on_path": detection.claude_cli_on_path,
        "claude_home": str(detection.claude_home),
        "claude_home_exists": detection.claude_home_exists,
        "conductor_home": str(detection.conductor_home),
        "agents_md_path": str(detection.agents_md),
        "agents_md_exists": detection.agents_md_exists,
        "agents_md_wired": "agents-md-import" in kinds,
        "gemini_md_path": str(detection.gemini_md),
        "gemini_md_exists": detection.gemini_md_exists,
        "gemini_md_wired": "gemini-md-import" in kinds,
        "claude_md_repo_path": str(detection.claude_md_repo),
        "claude_md_repo_exists": detection.claude_md_repo_exists,
        "claude_md_repo_wired": "claude-md-repo-import" in kinds,
        "cursor_rules_dir": str(detection.cursor_rules_dir),
        "cursor_rules_dir_exists": detection.cursor_rules_dir_exists,
        "cursor_rule_wired": "cursor-rule" in kinds,
        "managed_files": managed_files,
        "version_skew": bool(version_skew_files),
        "version_skew_files": version_skew_files,
        "user_version_skew_files": user_version_skew_files,
        "repo_version_skew_files": repo_version_skew_files,
        "repo_integration_status": repo_integration_status,
    }


def _repo_integration_status_payloads(
    *,
    cwd: Path,
    binary_version: str,
) -> list[dict[str, object]]:
    """Return exact repo integration status for machine consumers.

    Invariant: the same repo-scope decision engine powers human doctor
    warnings and JSON status, so an import-only CLAUDE.md block cannot be
    reported stale while `conductor update` says there is nothing to do.
    """
    from conductor import agent_wiring

    return [
        _repo_integration_decision_payload(decision, cwd=cwd, binary_version=binary_version)
        for decision in agent_wiring.repo_scope_version_decisions(
            cwd,
            binary_version=binary_version,
        )
    ]


def _repo_integration_decision_payload(
    decision: RepoScopeVersionDecision,
    *,
    cwd: Path,
    binary_version: str,
) -> dict[str, object]:
    expected = str(binary_version).split("+", 1)[0]
    status = decision.reason
    managed = status not in {"missing", "not conductor-managed"}
    requires_attention = status == "malformed sentinel" or status.startswith("read-error:")
    if status.startswith("read-error:"):
        update_command = "fix file permissions, then conductor update"
    elif status == "malformed sentinel":
        update_command = "repair malformed sentinel, then conductor init -y"
    elif decision.stale:
        update_command = "conductor update"
    elif status in {"missing", "not conductor-managed", "malformed sentinel"}:
        update_command = "conductor init -y"
    else:
        update_command = None
    return {
        "path": str(decision.path),
        "display_path": _repo_relative_path(decision.path, cwd=cwd),
        "kind": decision.kind,
        "scope": "repo",
        "exists": decision.path.is_file(),
        "managed": managed,
        "status": status,
        "stale": decision.stale,
        "requires_attention": requires_attention,
        "installed_version": decision.version,
        "expected_version": expected,
        "update_command": update_command,
    }


def _integration_version_skew_files(
    managed_files: list[dict],
    *,
    binary_version: str,
) -> list[str]:
    """Return managed integration files older than the running binary version.

    Invariant: only versioned managed files participate in skew detection.
    Legacy entries without a version are skipped because their version
    boundary is unknown; malformed versions are treated as refresh-worthy.
    """
    # Agent artifacts intentionally persist the public release version, not
    # local build metadata such as `+dirty`, so compare the same boundary that
    # the artifact writer can reproduce.
    current = parse_version(str(binary_version).split("+", 1)[0])
    skewed: list[str] = []
    for entry in managed_files:
        version = entry.get("version")
        if version is None:
            continue
        try:
            parsed = parse_version(str(version))
        except InvalidVersion:
            skewed.append(str(entry["path"]))
            continue
        if parsed < current:
            skewed.append(str(entry["path"]))
    return skewed


def _integration_version_skew_entries(
    managed_files: list[dict],
    *,
    binary_version: str,
) -> list[dict]:
    skewed_paths = set(
        _integration_version_skew_files(
            managed_files,
            binary_version=binary_version,
        )
    )
    return [entry for entry in managed_files if str(entry["path"]) in skewed_paths]


def _managed_version_is_older(version: str | None, *, binary_version: str) -> bool:
    """Return True when a managed artifact version predates this binary.

    Invariant: local build metadata never defines a persisted artifact boundary.
    """
    if version is None:
        return False
    current = parse_version(str(binary_version).split("+", 1)[0])
    try:
        artifact_version = parse_version(str(version))
    except InvalidVersion:
        return True
    return artifact_version < current


def _stale_branch_payload(branch: StaleBranch) -> dict[str, str]:
    return {
        "name": branch.name,
        "reason": branch.reason,
        "last_commit": branch.last_commit,
    }


def _abandoned_worktree_payload(worktree: AbandonedWorktree) -> dict[str, object]:
    return {
        "path": str(worktree.path),
        "branch": worktree.branch,
        "reason": worktree.reason,
        "last_commit": worktree.last_commit,
    }


def _protected_ref_payload(item: ProtectedRef) -> dict[str, str]:
    return {
        "kind": item.kind,
        "name": item.name,
        "reason": item.reason,
    }


def _git_cleanup_payload(plan: GitCleanupPlan) -> dict[str, object]:
    return {
        "stale_branches": [_stale_branch_payload(branch) for branch in plan.stale_branches],
        "abandoned_worktrees": [
            _abandoned_worktree_payload(worktree) for worktree in plan.abandoned_worktrees
        ],
        "protected": [_protected_ref_payload(item) for item in plan.protected],
        "branch_scan": {
            "checked": plan.branch_scan.checked,
            "total": plan.branch_scan.total,
            "limit": plan.branch_scan.limit,
            "capped": plan.branch_scan.capped,
        },
    }


def _git_state_doctor_payload() -> dict[str, object]:
    try:
        plan = scan_git_state(
            keep_worktree_days=DEFAULT_KEEP_WORKTREE_DAYS,
            branch_scan_limit=DEFAULT_BRANCH_SCAN_LIMIT,
        )
    except GitStateError as e:
        return {
            "stale_branches": [],
            "abandoned_worktrees": [],
            "branch_scan": {
                "checked": 0,
                "total": 0,
                "limit": DEFAULT_BRANCH_SCAN_LIMIT,
                "capped": False,
            },
            "error": str(e),
        }
    return {
        "stale_branches": [_stale_branch_payload(branch) for branch in plan.stale_branches],
        "abandoned_worktrees": [
            _abandoned_worktree_payload(worktree) for worktree in plan.abandoned_worktrees
        ],
        "branch_scan": {
            "checked": plan.branch_scan.checked,
            "total": plan.branch_scan.total,
            "limit": plan.branch_scan.limit,
            "capped": plan.branch_scan.capped,
        },
        "error": None,
    }


def _format_worktree_age(worktree: AbandonedWorktree) -> str:
    if worktree.last_commit_age_days is None:
        return "last commit age unknown"
    days = worktree.last_commit_age_days
    unit = "day" if days == 1 else "days"
    return f"last commit {days} {unit} ago"


def _echo_branch_scan_cap(
    branch_scan: dict[str, object] | BranchScanLimit,
    *,
    indent: str = "",
) -> None:
    if isinstance(branch_scan, dict):
        capped = bool(branch_scan.get("capped"))
        checked = branch_scan.get("checked")
        total = branch_scan.get("total")
    else:
        capped = branch_scan.capped
        checked = branch_scan.checked
        total = branch_scan.total
    if capped:
        click.echo(f"{indent}(top {checked} of {total} branches checked)")


def _run_git_cleanup_command(args: list[str], *, cwd: str | Path | None = None) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=GIT_STATE_COMMAND_TIMEOUT_SEC,
        )
    except subprocess.TimeoutExpired:
        return False, (
            f"`git {' '.join(args)}` timed out after "
            f"{GIT_STATE_COMMAND_TIMEOUT_SEC:.0f}s"
        )
    except OSError as e:
        return False, f"`git {' '.join(args)}` failed to start: {e}"
    detail = (result.stderr or result.stdout or "").strip()
    return result.returncode == 0, detail


@main.command("git-cleanup")
@click.option(
    "--execute",
    is_flag=True,
    default=False,
    help="Actually delete stale branches and abandoned worktrees. Dry-run by default.",
)
@click.option(
    "--branches-only",
    is_flag=True,
    default=False,
    help="Only report or delete stale local branches.",
)
@click.option(
    "--worktrees-only",
    is_flag=True,
    default=False,
    help="Only report or remove abandoned worktrees.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit structured cleanup summary as JSON.",
)
@click.option(
    "--keep-worktree-days",
    default=DEFAULT_KEEP_WORKTREE_DAYS,
    show_default=True,
    type=click.IntRange(min=0),
    help="Protect clean worktrees whose latest commit is newer than this many days.",
)
@click.option(
    "--branch-scan-limit",
    default=DEFAULT_BRANCH_SCAN_LIMIT,
    show_default=True,
    type=click.IntRange(min=1),
    help="Check only this many recently updated local branches for tree equivalence.",
)
def git_cleanup(
    execute: bool,
    branches_only: bool,
    worktrees_only: bool,
    as_json: bool,
    keep_worktree_days: int,
    branch_scan_limit: int,
) -> None:
    """Clean stale local branches and abandoned worktrees."""
    if branches_only and worktrees_only:
        raise click.UsageError("--branches-only conflicts with --worktrees-only")
    try:
        plan = scan_git_state(
            keep_worktree_days=keep_worktree_days,
            branch_scan_limit=branch_scan_limit,
        )
    except GitStateError as e:
        raise click.ClickException(str(e)) from e

    stale_branches = [] if worktrees_only else plan.stale_branches
    abandoned_worktrees = [] if branches_only else plan.abandoned_worktrees
    payload_plan = GitCleanupPlan(
        default_branch=plan.default_branch,
        current_path=plan.current_path,
        current_branch=plan.current_branch,
        stale_branches=stale_branches,
        abandoned_worktrees=abandoned_worktrees,
        protected=plan.protected,
        branch_scan=plan.branch_scan,
    )

    deleted_branches: list[str] = []
    removed_worktrees: list[str] = []
    errors: list[dict[str, str]] = []
    if execute:
        for item in plan.protected:
            if item.kind == "worktree" and item.reason == "uncommitted changes":
                click.echo(
                    f"[conductor] cleanup warning: protected dirty worktree "
                    f"{item.name} (uncommitted changes)",
                    err=True,
                )
        for branch in stale_branches:
            ok, detail = _run_git_cleanup_command(["branch", "-D", branch.name])
            if ok:
                deleted_branches.append(branch.name)
            else:
                msg = f"git branch -D {branch.name} failed"
                if detail:
                    msg = f"{msg}: {detail}"
                click.echo(f"[conductor] cleanup error: {msg}", err=True)
                errors.append({"target": branch.name, "error": msg})
        for worktree in abandoned_worktrees:
            ok, detail = _run_git_cleanup_command(["worktree", "remove", str(worktree.path)])
            if ok:
                removed_worktrees.append(str(worktree.path))
            else:
                msg = f"git worktree remove {worktree.path} failed"
                if detail:
                    msg = f"{msg}: {detail}"
                click.echo(f"[conductor] cleanup error: {msg}", err=True)
                errors.append({"target": str(worktree.path), "error": msg})

    if as_json:
        payload = _git_cleanup_payload(payload_plan)
        payload["dry_run"] = not execute
        payload["deleted_branches"] = deleted_branches
        payload["removed_worktrees"] = removed_worktrees
        payload["errors"] = errors
        click.echo(json.dumps(payload, indent=2))
        return

    click.echo(f"Stale branches ({len(stale_branches)}):")
    _echo_branch_scan_cap(plan.branch_scan, indent="  ")
    if stale_branches:
        for branch in stale_branches:
            click.echo(f"  - {branch.name} ({branch.reason} → {branch.last_commit})")
    else:
        click.echo("  (none)")

    click.echo("")
    click.echo(f"Abandoned worktrees ({len(abandoned_worktrees)}):")
    if abandoned_worktrees:
        for worktree in abandoned_worktrees:
            branch_label = worktree.branch or "detached"
            click.echo(
                f"  - {worktree.path} (branch {branch_label}, "
                f"{_format_worktree_age(worktree)}, clean)"
            )
    else:
        click.echo("  (none)")

    click.echo("")
    click.echo("Protected (won't touch):")
    if plan.protected:
        for item in plan.protected:
            click.echo(f"  - {item.name} ({item.reason})")
    else:
        click.echo("  (none)")

    click.echo("")
    if execute:
        click.echo(
            f"Deleted {len(deleted_branches)} branches and removed "
            f"{len(removed_worktrees)} worktrees."
        )
        if errors:
            click.echo(f"{len(errors)} cleanup operations failed; see stderr.")
    else:
        click.echo("Run with --execute to actually delete.")


@main.command()
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit diagnostic report as JSON.",
)
def doctor(as_json: bool) -> None:
    """Diagnose what's configured, what's missing, and where to look."""
    try:
        payload = _diagnostic_payload()
    except MutedProvidersError as e:
        raise click.ClickException(str(e)) from e

    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return

    from conductor.banner import SUBTITLE_DOCTOR, print_banner

    print_banner(SUBTITLE_DOCTOR, payload["version"])
    click.echo(f"{payload['platform']}  ·  python {payload['python']}")
    click.echo("")
    configured = [p for p in payload["providers"] if p["configured"]]
    unconfigured = [p for p in payload["providers"] if not p["configured"] and not p["muted"]]
    active = [p for p in payload["providers"] if not p["muted"]]
    muted = payload["muted"]

    def _provider_line(p: dict) -> None:
        symbol = "✓" if p["configured"] else "✗"
        effort_note = "" if p["supports_effort"] else " (no thinking mode)"
        click.echo(
            f"    {symbol} {p['provider']:<8}  "
            f"tier={p['quality_tier']:<8}  "
            f"default={p['default_model']}{effort_note}"
        )
        if p["configured"]:
            click.echo(f"        Verify end-to-end: conductor smoke {p['provider']}")
        if not p["configured"]:
            click.echo(f"        └─ {p['reason']}")
            if p.get("fix_command"):
                click.echo(f"        → fix: {p['fix_command']}")
        for w in p.get("warnings") or []:
            click.echo(f"        ⚠ {w}")

    click.echo(
        f"Providers ({len([p for p in configured if not p['muted']])}/{len(active)} active, "
        f"{len(muted)} muted):"
    )
    if configured:
        click.echo("  Configured:")
        for p in configured:
            _provider_line(p)
    if unconfigured:
        if configured:
            click.echo("")
        click.echo("  Available (not configured):")
        for p in unconfigured:
            _provider_line(p)
    if muted:
        click.echo("")
        click.echo(f"  Muted: {', '.join(muted)}")

    click.echo("")
    click.echo("Credentials (active source per env-var):")
    for c in payload["credentials"]:
        source_label = {
            "env": "✓ env",
            "key_command": "✓ key_command (secret manager)",
            "keychain": "✓ keychain",
            None: "—",
        }.get(c["source"], "—")
        click.echo(f"  {c['name']:<24}  {source_label}")

    if payload["warnings"]:
        click.echo("")
        click.echo("Warnings:")
        for warning in payload["warnings"]:
            click.echo(f"  ⚠ {warning['message']}")

    click.echo("")
    click.echo("Active credentials (per provider):")
    for row in payload["active_credentials"]:
        click.echo(f"  {row['provider']:<14} {row['detail']}")

    click.echo("")
    click.echo("Agent integration:")
    ai = payload["agent_integration"]

    user_managed = [f for f in ai["managed_files"] if f["kind"] not in REPO_INTEGRATION_KINDS]
    if not ai["claude_detected"]:
        click.echo("  Claude Code:  not detected")
    elif not user_managed:
        click.echo("  Claude Code:  detected, not wired (run `conductor init`)")
    else:
        click.echo(f"  Claude Code:  wired — {len(user_managed)} user-scope files")
        for f in user_managed:
            version_note = f" v{f['version']}" if f["version"] else ""
            click.echo(f"    {f['kind']:<18}  {f['path']}{version_note}")

    def _repo_line(
        label: str,
        file_kind: str,
        exists_key: str,
        wired_key: str,
        path_key: str,
    ) -> None:
        if not ai[exists_key] and not ai[wired_key]:
            click.echo(f"  {label}  no {label.split(':')[0].strip()} in current directory")
            return
        if ai[wired_key]:
            entry = next(
                (f for f in ai["managed_files"] if f["kind"] == file_kind),
                None,
            )
            version_note = f" v{entry['version']}" if entry and entry["version"] else ""
            click.echo(f"  {label}  wired — {ai[path_key]}{version_note}")
        else:
            # The file itself is loaded normally by its host agent; only
            # Conductor's per-repo delegation block is missing. Spell that out
            # so "present but not wired" doesn't read as "the file is broken".
            click.echo(f"  {label}  no Conductor delegation block — {ai[path_key]}")
            click.echo(
                "                (file still loads normally for its agent; "
                "Conductor would add per-repo"
            )
            click.echo("                routing hints via `conductor init`.)")

    _repo_line(
        "AGENTS.md:   ", "agents-md-import", "agents_md_exists", "agents_md_wired", "agents_md_path"
    )
    _repo_line(
        "GEMINI.md:   ", "gemini-md-import", "gemini_md_exists", "gemini_md_wired", "gemini_md_path"
    )
    _repo_line(
        "CLAUDE.md:   ",
        "claude-md-repo-import",
        "claude_md_repo_exists",
        "claude_md_repo_wired",
        "claude_md_repo_path",
    )

    # Cursor is a fully-managed file inside a conventional directory, not a
    # sentinel-block patch. Its detection story is "does .cursor/rules/ exist".
    if not ai["cursor_rules_dir_exists"] and not ai["cursor_rule_wired"]:
        click.echo("  Cursor:       no .cursor/rules/ in current directory")
    elif ai["cursor_rule_wired"]:
        entry = next(
            (f for f in ai["managed_files"] if f["kind"] == "cursor-rule"),
            None,
        )
        version_note = f" v{entry['version']}" if entry and entry["version"] else ""
        click.echo(f"  Cursor:       rule wired{version_note}")
    else:
        click.echo(
            "  Cursor:       no Conductor rule in .cursor/rules/ (run `conductor init` to add one)"
        )

    if ai["user_version_skew_files"]:
        click.echo("")
        click.echo(f"⚠ Integration files behind binary (v{payload['version']}). Refresh with:")
        click.echo("    conductor init -y --remaining")
    if ai["repo_version_skew_files"]:
        repo_skew_entries = [
            entry for entry in ai["repo_integration_status"] if entry["stale"]
        ]
        click.echo("")
        click.echo(f"⚠ Repo integration files behind binary (v{payload['version']}):")
        for entry in repo_skew_entries:
            path = Path(entry["path"])
            try:
                display = path.relative_to(Path.cwd())
            except ValueError:
                display = path
            version_note = (
                f" (v{entry['installed_version']})"
                if entry["installed_version"]
                else ""
            )
            click.echo(f"    {display}{version_note}")
        click.echo("  Auto refresh paths:")
        click.echo("    brew upgrade conductor       # CLAUDE.md @-import self-heals on upgrade")
        click.echo("    conductor init               # installs refresh hook by default")
        click.echo("  Immediate manual fallback:")
        click.echo("    conductor update             # refresh this repo's embedded integrations")
        click.echo("    conductor update-all         # force-refresh configured consumer repos")
        click.echo("  Prefer the auto paths unless an immediate cross-repo refresh is needed.")

    git_state = payload["git_state"]
    stale_branches = git_state["stale_branches"]
    abandoned_worktrees = git_state["abandoned_worktrees"]
    if git_state.get("error"):
        click.echo("")
        click.echo("⚠ Local git state could not be checked:")
        click.echo(f"    {git_state['error']}")
    if stale_branches or abandoned_worktrees:
        click.echo("")
        click.echo("⚠ Local git state has drift:")
        click.echo(f"    Stale branches ({len(stale_branches)}):")
        _echo_branch_scan_cap(git_state["branch_scan"], indent="      ")
        for branch in stale_branches:
            click.echo(f"      - {branch['name']} ({branch['reason']} → {branch['last_commit']})")
        click.echo(f"    Abandoned worktrees ({len(abandoned_worktrees)}):")
        for worktree in abandoned_worktrees:
            branch = worktree["branch"] or "detached"
            click.echo(f"      - {worktree['path']} (branch {branch}, {worktree['reason']})")
        click.echo("  Refresh with:")
        click.echo("    conductor git-cleanup           # dry-run (default)")
        click.echo("    conductor git-cleanup --execute # actually delete")

    click.echo("")
    click.echo("Next steps:")
    not_configured = [p for p in payload["providers"] if not p["configured"] and not p["muted"]]
    if not not_configured:
        if payload["muted"]:
            click.echo(
                "  all remaining providers are either configured or muted. "
                "try `conductor smoke --all`."
            )
        else:
            click.echo("  everything is configured. try `conductor smoke --all`.")
    else:
        click.echo("  run `conductor init` to configure missing providers interactively,")
        click.echo("  or set the env vars listed above and re-run `conductor doctor`.")


# --------------------------------------------------------------------------- #
# init — interactive setup wizard
# --------------------------------------------------------------------------- #


@main.command()
@click.option(
    "--yes",
    "-y",
    "accept_defaults",
    is_flag=True,
    default=False,
    help="Accept all defaults without prompting (non-TTY friendly).",
)
@click.option(
    "--only",
    default=None,
    help="Configure only the named provider (skips others).",
)
@click.option(
    "--remaining",
    is_flag=True,
    default=False,
    help="Resume setup with only the not-yet-configured providers.",
)
@click.option(
    "--quiet",
    is_flag=True,
    default=False,
    help="Suppress informational output. Useful for `post_install` automation.",
)
@click.option(
    "--wire-agents",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Wire conductor into detected agent tools (Claude Code today). "
    "Default: yes for unscoped init; pass no to opt out.",
)
@click.option(
    "--patch-claude-md",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Add the delegation-guidance @import line to ~/.claude/CLAUDE.md. "
    "Default: yes when Claude Code is detected; pass no to opt out.",
)
@click.option(
    "--patch-agents-md",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Inject a conductor delegation block into ./AGENTS.md "
    "(Codex / Cursor / Zed convention). Default: yes for unscoped init.",
)
@click.option(
    "--patch-gemini-md",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Inject a conductor delegation block into ./GEMINI.md "
    "(Gemini CLI convention). Default: yes when GEMINI.md exists.",
)
@click.option(
    "--patch-claude-md-repo",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Inject @import into repo-scope ./CLAUDE.md (parallel to "
    "--patch-claude-md for user-scope). Default: yes when CLAUDE.md exists.",
)
@click.option(
    "--wire-cursor",
    "wire_cursor_flag",
    type=click.Choice(["yes", "no", "ask"]),
    default=None,
    help="Write a managed Cursor rule at .cursor/rules/conductor-delegation.mdc. "
    "Default: yes when .cursor/rules/ exists.",
)
@click.option(
    "--unwire",
    is_flag=True,
    default=False,
    help="Remove every conductor-managed agent integration artifact "
    "(user-scope + repo-scope sentinel blocks + Cursor rule) and exit.",
)
@click.option(
    "--hooks/--no-hooks",
    default=True,
    help=(
        "Install pre-commit refresh hook for embed-only files "
        "(default: yes; pass --no-hooks to skip)."
    ),
)
def init(
    accept_defaults: bool,
    only: str | None,
    remaining: bool,
    quiet: bool,
    wire_agents: str | None,
    patch_claude_md: str | None,
    patch_agents_md: str | None,
    patch_gemini_md: str | None,
    patch_claude_md_repo: str | None,
    wire_cursor_flag: str | None,
    unwire: bool,
    hooks: bool,
) -> None:
    """Interactively configure Conductor for first use."""
    if unwire:
        wiring_flags = (
            wire_agents,
            patch_claude_md,
            patch_agents_md,
            patch_gemini_md,
            patch_claude_md_repo,
            wire_cursor_flag,
        )
        if only or remaining or any(f is not None for f in wiring_flags):
            raise click.UsageError("--unwire can't be combined with provider or wiring flags.")
        sys.exit(_run_unwire())

    if only and remaining:
        raise click.UsageError("--only and --remaining are mutually exclusive.")
    if only and only not in known_providers():
        raise click.UsageError(f"unknown provider {only!r}; known: {known_providers()}")
    exit_code = run_init_wizard(
        accept_defaults=accept_defaults,
        only=only,
        remaining=remaining,
        quiet=quiet,
        wire_agents=wire_agents,
        patch_claude_md=patch_claude_md,
        patch_agents_md=patch_agents_md,
        patch_gemini_md=patch_gemini_md,
        patch_claude_md_repo=patch_claude_md_repo,
        wire_cursor_flag=wire_cursor_flag,
    )
    if exit_code == 0 and hooks:
        exit_code = _run_install_hooks(quiet=quiet)
    sys.exit(exit_code)


def _run_unwire() -> int:
    """Remove every managed agent-integration artifact. Returns an exit code."""
    from conductor.agent_wiring import unwire

    report = unwire()
    if not report.removed and not report.skipped:
        click.echo("No conductor-managed agent integration files found.")
        return 0

    if report.removed:
        click.echo("Removed:")
        for p in report.removed:
            click.echo(f"  {p}")
    if report.skipped:
        click.echo("")
        click.echo("Skipped (not conductor-managed):")
        for path, reason in report.skipped:
            click.echo(f"  {path}  — {reason}")
    return 0


def _run_install_hooks(*, quiet: bool = False) -> int:
    """Install the local pre-commit hook entry for refresh-on-commit."""
    config_path = Path.cwd() / PRE_COMMIT_CONFIG
    try:
        changed = _install_refresh_pre_commit_hook(config_path)
    except OSError as e:
        raise click.ClickException(f"failed to update {config_path}: {e}") from e
    if quiet:
        return 0
    if changed:
        click.echo(
            f"==> Installed conductor-refresh pre-commit hook in {PRE_COMMIT_CONFIG}."
        )
    else:
        click.echo(
            f"==> conductor-refresh pre-commit hook already present in {PRE_COMMIT_CONFIG}."
        )
    return 0


def _install_refresh_pre_commit_hook(config_path: Path) -> bool:
    """Install the documented local hook block if it is not already present.

    The invariant is that the hook lives inside pre-commit's root `repos`
    list. We keep this as a narrow text insertion so existing comments,
    anchors, formatting, and top-level settings are preserved.
    """
    try:
        existing = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        existing = ""
    if f"id: {CONDUCTOR_REFRESH_HOOK_ID}" in existing:
        return False

    updated = _pre_commit_config_with_refresh_hook(existing)
    config_path.write_text(updated, encoding="utf-8")
    return True


def _pre_commit_config_with_refresh_hook(existing: str) -> str:
    if not existing.strip():
        return f"repos:\n{_pre_commit_refresh_hook_block(item_indent='')}"

    lines = existing.splitlines(keepends=True)
    repos_index = _pre_commit_repos_line_index(lines)
    if repos_index is None:
        separator = "" if existing.endswith("\n") else "\n"
        return f"{existing}{separator}repos:\n{_pre_commit_refresh_hook_block(item_indent='')}"

    line = lines[repos_index]
    stripped = line.strip()
    item_indent = _pre_commit_repos_item_indent(lines, repos_index)
    if stripped == "repos: []":
        line_ending = "\n" if line.endswith("\n") else ""
        lines[repos_index] = f"repos:{line_ending}"
        insert_at = repos_index + 1
        item_indent = ""
    else:
        insert_at = _pre_commit_repos_section_end(lines, repos_index)

    if insert_at > 0 and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] = f"{lines[insert_at - 1]}\n"
    lines[insert_at:insert_at] = [_pre_commit_refresh_hook_block(item_indent=item_indent)]
    return "".join(lines)


def _pre_commit_repos_line_index(lines: list[str]) -> int | None:
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "repos:" or stripped == "repos: []":
            return index
    return None


def _pre_commit_repos_section_end(lines: list[str], repos_index: int) -> int:
    repos_indent = len(lines[repos_index]) - len(lines[repos_index].lstrip(" "))
    for index in range(repos_index + 1, len(lines)):
        line = lines[index]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if indent <= repos_indent and not stripped.startswith("-"):
            return index
    return len(lines)


def _pre_commit_repos_item_indent(lines: list[str], repos_index: int) -> str:
    repos_indent = len(lines[repos_index]) - len(lines[repos_index].lstrip(" "))
    for line in lines[repos_index + 1 :]:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent_len = len(line) - len(line.lstrip(" "))
        if indent_len <= repos_indent and not stripped.startswith("-"):
            return ""
        if stripped.startswith("-"):
            return line[:indent_len]
    return ""


def _pre_commit_refresh_hook_block(*, item_indent: str) -> str:
    return "".join(f"{item_indent}{line}\n" for line in CONDUCTOR_REFRESH_HOOK_LINES)


@main.command("refresh-on-commit")
def refresh_on_commit() -> None:
    """Refresh stale embedded Conductor repo integrations and stage changes."""
    sys.exit(_run_refresh_on_commit())


@main.command("update")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Show stale repo integrations without writing files.",
)
@click.option(
    "--check",
    is_flag=True,
    default=False,
    help="Exit 1 if repo integrations are stale; do not write files.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable repo integration status as JSON.",
)
def update(dry_run: bool, check: bool, as_json: bool) -> None:
    """Refresh stale embedded Conductor repo integrations in this repo."""
    sys.exit(_run_update_current_repo(dry_run=dry_run, check=check, as_json=as_json))


def _run_update_current_repo(*, dry_run: bool, check: bool, as_json: bool = False) -> int:
    cwd = Path.cwd()
    repo_check = _run_repo_command(cwd, ["git", "rev-parse", "--is-inside-work-tree"])
    if repo_check.returncode != 0:
        detail = (repo_check.stderr or repo_check.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        raise click.ClickException(f"not a git repository{suffix}")

    stale = _stale_repo_integration_decisions(cwd)
    attention = _repo_attention_integration_decisions(cwd)
    if as_json:
        status = _repo_update_status_payload(cwd, refreshed=())
        if attention:
            click.echo(json.dumps(status, indent=2))
            return 0 if dry_run else 1
        if not stale and not attention:
            click.echo(json.dumps(status, indent=2))
            return 0
        if dry_run or check:
            click.echo(json.dumps(status, indent=2))
            return 1 if check else 0

    if attention:
        click.echo("Conductor repo integrations need manual repair:")
        _echo_repo_attention_plan(attention, cwd=cwd)
        click.echo("Repair the files above, then run `conductor init -y`.")
        return 0 if dry_run else 1

    if not stale:
        click.echo("Conductor repo integrations are current.")
        return 0

    if dry_run or check:
        heading = (
            "Conductor repo integrations are stale:"
            if check
            else "Would refresh Conductor repo integrations:"
        )
        click.echo(heading)
        _echo_repo_update_plan(stale, cwd=cwd)
        if check:
            click.echo("Run `conductor update` to refresh them.")
            return 1
        return 0

    try:
        touched = _refresh_stale_repo_integrations(cwd)
    except OSError as e:
        raise click.ClickException(f"refresh failed: {e}") from e
    except Exception as e:
        raise click.ClickException(f"internal refresh error: {e}") from e

    if not touched:
        if as_json:
            click.echo(json.dumps(_repo_update_status_payload(cwd, refreshed=()), indent=2))
            return 0
        click.echo("Conductor repo integrations are current.")
        return 0

    add = _run_repo_command(
        cwd,
        ["git", "add", "--", *[_repo_relative_path(path, cwd=cwd) for path in touched]],
    )
    if add.returncode != 0:
        raise click.ClickException(_command_failure_detail(add, "git add"))

    if as_json:
        click.echo(json.dumps(_repo_update_status_payload(cwd, refreshed=touched), indent=2))
        return 0

    click.echo("Refreshed Conductor repo integrations:")
    for path in touched:
        click.echo(f"  {_repo_relative_path(path, cwd=cwd)}")
    click.echo("Staged refreshed paths.")
    return 0


def _stale_repo_integration_decisions(cwd: Path) -> tuple[RepoScopeVersionDecision, ...]:
    from conductor import agent_wiring

    return tuple(
        decision
        for decision in agent_wiring.repo_scope_version_decisions(
            cwd,
            binary_version=__version__,
        )
        if decision.stale
    )


def _repo_attention_integration_decisions(cwd: Path) -> tuple[RepoScopeVersionDecision, ...]:
    from conductor import agent_wiring

    return tuple(
        decision
        for decision in agent_wiring.repo_scope_version_decisions(
            cwd,
            binary_version=__version__,
        )
        if decision.reason == "malformed sentinel"
        or decision.reason.startswith("read-error:")
    )


def _repo_update_status_payload(
    cwd: Path,
    *,
    refreshed: tuple[Path, ...],
) -> dict[str, object]:
    integrations = _repo_integration_status_payloads(cwd=cwd, binary_version=__version__)
    stale = [entry for entry in integrations if entry["stale"]]
    needs_attention = [entry for entry in integrations if entry["requires_attention"]]
    return {
        "version": __version__.split("+", 1)[0],
        "repo": str(cwd.resolve()),
        "current": not stale and not needs_attention,
        "stale": bool(stale),
        "needs_attention": bool(needs_attention),
        "update_command": "conductor update" if stale else None,
        "refreshed": [_repo_relative_path(path, cwd=cwd) for path in refreshed],
        "integrations": integrations,
    }


def _echo_repo_update_plan(
    decisions: tuple[RepoScopeVersionDecision, ...],
    *,
    cwd: Path,
) -> None:
    target_version = __version__.split("+", 1)[0]
    for decision in decisions:
        version = decision.version or "unknown"
        click.echo(
            f"  {_repo_relative_path(decision.path, cwd=cwd)} "
            f"(v{version} -> v{target_version})"
        )


def _echo_repo_attention_plan(
    decisions: tuple[RepoScopeVersionDecision, ...],
    *,
    cwd: Path,
) -> None:
    for decision in decisions:
        click.echo(
            f"  {_repo_relative_path(decision.path, cwd=cwd)} "
            f"({decision.reason})"
        )


def _repo_relative_path(path: Path, *, cwd: Path) -> str:
    root = cwd.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        return str(candidate.resolve().relative_to(root))
    except ValueError:
        return str(path)


def _run_refresh_on_commit() -> int:
    cwd = Path.cwd()
    repo_check = _run_repo_command(cwd, ["git", "rev-parse", "--is-inside-work-tree"])
    if repo_check.returncode != 0:
        detail = (repo_check.stderr or repo_check.stdout or "").strip()
        suffix = f": {detail}" if detail else ""
        click.echo(
            f"[conductor] refresh-on-commit: not a git repo, no-op{suffix}",
            err=True,
        )
        return 0

    try:
        touched = _refresh_stale_repo_integrations(cwd)
    except OSError as e:
        click.echo(
            f"[conductor] refresh-on-commit warning: refresh failed: {e}",
            err=True,
        )
        return 1
    except Exception as e:
        click.echo(
            f"[conductor] refresh-on-commit warning: internal error: {e}",
            err=True,
        )
        return 1

    if not touched:
        return 0

    add = _run_repo_command(cwd, ["git", "add", "--", *[str(path) for path in touched]])
    if add.returncode != 0:
        click.echo(
            f"[conductor] refresh-on-commit warning: {_command_failure_detail(add, 'git add')}",
            err=True,
        )
        return 1
    return 0


def _refresh_stale_repo_integrations(cwd: Path) -> tuple[Path, ...]:
    from conductor import agent_wiring

    detection = agent_wiring.detect(cwd=cwd)
    touched: list[Path] = []
    for artifact in detection.managed:
        if artifact.kind not in REPO_INTEGRATION_KINDS:
            continue
        if not _managed_version_is_older(artifact.version, binary_version=__version__):
            continue
        if not _repo_integration_is_embedded(artifact):
            continue
        before = _read_path_bytes(artifact.path)
        _refresh_repo_integration(artifact.kind, cwd=cwd, version=__version__)
        after = _read_path_bytes(artifact.path)
        if after != before:
            touched.append(artifact.path)
    return tuple(touched)


def _repo_integration_is_embedded(artifact: AgentArtifact) -> bool:
    """Skip import-only sentinel blocks; refresh embedded repo artifacts only."""
    if artifact.kind == "cursor-rule":
        return True
    try:
        text = artifact.path.read_text(encoding="utf-8")
    except OSError:
        raise
    from conductor.agent_wiring import SENTINEL_BEGIN_PREFIX, SENTINEL_END

    begin = text.find(SENTINEL_BEGIN_PREFIX)
    end = text.find(SENTINEL_END, begin)
    if begin == -1 or end == -1:
        return False
    marker_end = text.find("-->", begin)
    if marker_end == -1 or marker_end > end:
        return False
    body = text[marker_end + len("-->") : end].strip()
    return not body.startswith("@")


def _refresh_repo_integration(kind: str, *, cwd: Path, version: str) -> None:
    from conductor import agent_wiring

    if kind == "agents-md-import":
        agent_wiring.wire_agents_md(cwd=cwd, version=version)
        return
    if kind == "gemini-md-import":
        agent_wiring.wire_gemini_md(cwd=cwd, version=version)
        return
    if kind == "claude-md-repo-import":
        agent_wiring.wire_claude_md_repo(cwd=cwd, version=version)
        return
    if kind == "cursor-rule":
        agent_wiring.wire_cursor(cwd=cwd, version=version)
        return
    raise ValueError(f"unsupported repo integration kind: {kind}")


def _read_path_bytes(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


# --------------------------------------------------------------------------- #
# update-all / refresh-consumers — refresh explicit downstream repo wiring
# --------------------------------------------------------------------------- #


_REFRESH_CONSUMERS_DEPRECATION = (
    "[conductor] `refresh-consumers` is deprecated; use `conductor update-all`."
)


@dataclass(frozen=True)
class _ConsumerRefreshResult:
    path: Path
    status: str
    detail: str
    branch: str | None = None
    commit: str | None = None
    # "none" — repo was clean, no stash involved.
    # "popped" — repo was dirty; auto-stashed; pop succeeded (operator's changes restored).
    # "preserved" — repo was dirty; auto-stashed; pop conflicted; stash@{0} kept for operator.
    stash_status: str = "none"


def _consumer_refresh_options(function):
    function = click.option(
        "--no-auto-stash",
        "no_auto_stash",
        is_flag=True,
        default=False,
        help=(
            "Skip repos with uncommitted changes instead of auto-stashing them. "
            "Default: auto-stash uncommitted changes, refresh, then pop the stash back."
        ),
    )(function)
    function = click.option(
        "--branch",
        "branch_name",
        default=None,
        help="Branch name to create or reuse in each repo.",
    )(function)
    function = click.option(
        "--config-file",
        type=click.Path(path_type=Path, dir_okay=False, exists=True),
        default=None,
        help=(
            "TOML file with operator-owned consumer paths, for example "
            '`paths = ["~/repos/Sentinel"]`.'
        ),
    )(function)
    function = click.option(
        "--paths",
        default=None,
        help="Comma-separated consumer repo paths to refresh. Defaults to empty.",
    )(function)
    return function


@main.command("update-all")
@_consumer_refresh_options
def update_all(
    paths: str | None,
    config_file: Path | None,
    branch_name: str | None,
    no_auto_stash: bool,
) -> None:
    """Refresh Conductor integration blocks in configured consumer repos.

    Canonical batch operator. Walks explicit consumer paths and refreshes each
    repo on a review branch so operators can inspect the integration updates.

    Repos with uncommitted changes are auto-stashed by default: stash → refresh
    → pop. If the pop conflicts (rare; happens when operator changes overlap
    conductor-managed sentinel blocks), the stash entry is preserved at
    `stash@{0}` for manual resolution. Pass `--no-auto-stash` to revert to the
    older skip-on-dirty behavior.
    """
    _run_update_all(
        paths=paths,
        config_file=config_file,
        branch_name=branch_name,
        no_auto_stash=no_auto_stash,
    )


@main.command("refresh-consumers")
@_consumer_refresh_options
def refresh_consumers(
    paths: str | None,
    config_file: Path | None,
    branch_name: str | None,
    no_auto_stash: bool,
) -> None:
    """Deprecated alias for `conductor update-all`.

    The alias remains for compatibility while scripts migrate to the shared
    Autumn Garage `update-all` verb.
    """
    click.echo(_REFRESH_CONSUMERS_DEPRECATION, err=True)
    _run_update_all(
        paths=paths,
        config_file=config_file,
        branch_name=branch_name,
        no_auto_stash=no_auto_stash,
    )


def _run_update_all(
    *,
    paths: str | None,
    config_file: Path | None,
    branch_name: str | None,
    no_auto_stash: bool,
) -> None:
    """Refresh Conductor integration blocks in explicitly configured repos."""
    try:
        consumer_paths = _resolve_consumer_repo_paths(paths, config_file)
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    if not consumer_paths:
        click.echo("No consumer repos configured.")
        return

    branch = branch_name or f"chore/conductor-refresh-v{__version__.split('+', 1)[0]}"
    auto_stash = not no_auto_stash
    results = [
        _refresh_one_consumer_repo(path, branch=branch, auto_stash=auto_stash)
        for path in consumer_paths
    ]

    failed = False
    for result in results:
        click.echo(f"{result.path}: {result.status} — {result.detail}")
        if result.branch:
            click.echo(f"  branch: {result.branch}")
        if result.commit:
            click.echo(f"  commit: {result.commit}")
        if result.stash_status == "preserved":
            click.echo("  stash: stash@{0} preserved — resolve manually with `git stash pop`")
        if result.status in {"failed", "skipped", "needs-attention"}:
            failed = True

    if failed:
        sys.exit(1)


def _resolve_consumer_repo_paths(
    paths: str | None,
    config_file: Path | None,
) -> tuple[Path, ...]:
    raw_paths: list[str] = []
    if paths:
        raw_paths.extend(part.strip() for part in paths.split(",") if part.strip())
    if config_file is not None:
        raw_paths.extend(_consumer_paths_from_config(config_file))

    resolved: list[Path] = []
    seen: set[Path] = set()
    for raw in raw_paths:
        path = Path(raw).expanduser().resolve()
        if path in seen:
            continue
        seen.add(path)
        resolved.append(path)
    return tuple(resolved)


def _consumer_paths_from_config(config_file: Path) -> tuple[str, ...]:
    try:
        data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise ValueError(f"failed to read consumer config {config_file}: {e}") from e

    paths: list[str] = []
    top_level_paths = data.get("paths", [])
    if not isinstance(top_level_paths, list):
        raise ValueError("consumer config `paths` must be a list")
    for item in top_level_paths:
        if not isinstance(item, str):
            raise ValueError("consumer config `paths` entries must be strings")
        paths.append(item)

    for key in ("consumers", "repositories", "repos"):
        entries = data.get(key, [])
        if not isinstance(entries, list):
            raise ValueError(f"consumer config `{key}` must be a list")
        for entry in entries:
            if isinstance(entry, str):
                paths.append(entry)
                continue
            if isinstance(entry, dict) and isinstance(entry.get("path"), str):
                paths.append(entry["path"])
                continue
            raise ValueError(
                f"consumer config `{key}` entries must be strings or tables with `path`"
            )
    return tuple(paths)


def _refresh_current_repo_scope_on_branch(
    path: Path,
    *,
    branch: str,
    version: str,
) -> _ConsumerRefreshResult:
    if not path.is_dir():
        return _ConsumerRefreshResult(path, "failed", "path is not a directory")
    if _run_repo_command(path, ["git", "rev-parse", "--is-inside-work-tree"]).returncode != 0:
        return _ConsumerRefreshResult(path, "failed", "path is not a git repository")

    before = _git_status_porcelain(path)
    if before is None:
        return _ConsumerRefreshResult(path, "failed", "could not read git status")

    orig_branch_proc = _run_repo_command(path, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    orig_branch = orig_branch_proc.stdout.strip() if orig_branch_proc.returncode == 0 else None
    if not orig_branch:
        return _ConsumerRefreshResult(path, "failed", "could not read current branch")

    stashed = False
    if before:
        stash_msg = f"conductor-refresh-v{version.split('+', 1)[0]} auto-stash"
        stash = _run_repo_command(path, ["git", "stash", "push", "-u", "-m", stash_msg])
        if stash.returncode != 0:
            return _ConsumerRefreshResult(
                path, "failed", _command_failure_detail(stash, "git stash push")
            )
        stashed = True

    def _restore(result: _ConsumerRefreshResult) -> _ConsumerRefreshResult:
        back = _run_repo_command(path, ["git", "checkout", orig_branch])
        if back.returncode != 0:
            detail = (
                f"refresh ran but could not return to {orig_branch}; "
                "stash@{0} preserved for manual resolution"
                if stashed
                else f"refresh ran but could not return to {orig_branch}; "
                "manual cleanup required"
            )
            return _ConsumerRefreshResult(
                result.path,
                "needs-attention",
                detail,
                branch=result.branch,
                commit=result.commit,
                stash_status="preserved" if stashed else "none",
            )
        if not stashed:
            if result.status == "committed":
                return replace(result, detail=f"no changes left on {orig_branch}")
            return result
        pop = _run_repo_command(path, ["git", "stash", "pop"])
        if pop.returncode != 0:
            return _ConsumerRefreshResult(
                result.path,
                "needs-attention",
                f"refresh committed on {result.branch}; auto-stash pop "
                f"conflicted on {orig_branch}; stash@{{0}} preserved for "
                "manual resolution",
                branch=result.branch,
                commit=result.commit,
                stash_status="preserved",
            )
        detail = (
            f"operator changes restored on {orig_branch}"
            if result.status == "committed"
            else f"{result.detail} (auto-stashed and restored)"
        )
        return replace(result, detail=detail, stash_status="popped")

    checkout = _checkout_refresh_branch(path, branch)
    if checkout is not None:
        return _restore(_ConsumerRefreshResult(path, "failed", checkout, branch=branch))

    from conductor import agent_wiring

    report = agent_wiring.refresh_repo_scope(path, version=version)
    if report.skipped and not report.refreshed:
        skipped = "; ".join(f"{item}: {reason}" for item, reason in report.skipped)
        return _restore(
            _ConsumerRefreshResult(
                path,
                "needs-attention",
                f"refresh skipped: {skipped}",
                branch=branch,
            )
        )

    after = _git_status_porcelain(path)
    if after is None:
        return _restore(
            _ConsumerRefreshResult(path, "failed", "could not read git status", branch=branch)
        )
    if not after:
        return _restore(
            _ConsumerRefreshResult(
                path,
                "unchanged",
                "integration files already current",
                branch=branch,
            )
        )

    add = _run_repo_command(path, ["git", "add", "--all"])
    if add.returncode != 0:
        return _restore(
            _ConsumerRefreshResult(
                path,
                "failed",
                _command_failure_detail(add, "git add --all"),
                branch=branch,
            )
        )
    message = f"Refresh conductor integrations to v{version.split('+', 1)[0]}"
    commit = _run_repo_command(path, ["git", "commit", "-m", message])
    if commit.returncode != 0:
        return _restore(
            _ConsumerRefreshResult(
                path,
                "failed",
                _command_failure_detail(commit, "git commit"),
                branch=branch,
            )
        )
    sha = _run_repo_command(path, ["git", "rev-parse", "--short", "HEAD"])
    commit_sha = sha.stdout.strip() if sha.returncode == 0 else None
    return _restore(
        _ConsumerRefreshResult(
            path,
            "committed",
            f"no changes left on {orig_branch}",
            branch=branch,
            commit=commit_sha,
        )
    )


def _refresh_one_consumer_repo(
    path: Path, *, branch: str, auto_stash: bool = True
) -> _ConsumerRefreshResult:
    if not path.is_dir():
        return _ConsumerRefreshResult(path, "failed", "path is not a directory")
    if _run_repo_command(path, ["git", "rev-parse", "--is-inside-work-tree"]).returncode != 0:
        return _ConsumerRefreshResult(path, "failed", "path is not a git repository")
    before = _git_status_porcelain(path)
    if before is None:
        return _ConsumerRefreshResult(path, "failed", "could not read git status")

    # Capture the operator's current branch before we switch to the refresh
    # branch — we'll return to it before popping the stash so the operator's
    # in-flight work lands back where they made it.
    orig_branch_proc = _run_repo_command(path, ["git", "rev-parse", "--abbrev-ref", "HEAD"])
    orig_branch = orig_branch_proc.stdout.strip() if orig_branch_proc.returncode == 0 else None
    if not orig_branch:
        return _ConsumerRefreshResult(path, "failed", "could not read current branch")

    stashed = False
    if before:
        if not auto_stash:
            return _ConsumerRefreshResult(path, "skipped", "repo has pre-existing changes")
        stash_msg = f"conductor-refresh-v{__version__.split('+', 1)[0]} auto-stash"
        stash = _run_repo_command(path, ["git", "stash", "push", "-u", "-m", stash_msg])
        if stash.returncode != 0:
            return _ConsumerRefreshResult(
                path, "failed", _command_failure_detail(stash, "git stash push")
            )
        stashed = True

    def _pop_or_preserve(result: _ConsumerRefreshResult) -> _ConsumerRefreshResult:
        """If we stashed, return to orig_branch and pop. Augment result with
        stash_status; on pop conflict, preserve the stash and downgrade
        status to "needs-attention"."""
        if not stashed:
            return result
        # Best-effort return to the operator's original branch before popping.
        back = _run_repo_command(path, ["git", "checkout", orig_branch])
        if back.returncode != 0:
            return _ConsumerRefreshResult(
                result.path,
                "needs-attention",
                f"refresh ran but could not return to {orig_branch}; "
                "stash@{0} preserved for manual resolution",
                branch=result.branch,
                commit=result.commit,
                stash_status="preserved",
            )
        pop = _run_repo_command(path, ["git", "stash", "pop"])
        if pop.returncode != 0:
            return _ConsumerRefreshResult(
                result.path,
                "needs-attention",
                f"refresh committed on {result.branch}; auto-stash pop "
                f"conflicted on {orig_branch}; stash@{{0}} preserved for "
                "manual resolution",
                branch=result.branch,
                commit=result.commit,
                stash_status="preserved",
            )
        # Pop succeeded — augment detail to record the auto-stash.
        augmented_detail = f"{result.detail} (auto-stashed and restored)"
        return _ConsumerRefreshResult(
            result.path,
            result.status,
            augmented_detail,
            branch=result.branch,
            commit=result.commit,
            stash_status="popped",
        )

    checkout = _checkout_refresh_branch(path, branch)
    if checkout is not None:
        return _pop_or_preserve(_ConsumerRefreshResult(path, "failed", checkout))

    init_result = _run_repo_command(
        path,
        [sys.executable, "-m", "conductor.cli", "init", "-y", "--remaining"],
    )
    if init_result.returncode != 0:
        detail = _command_failure_detail(init_result, "conductor init -y --remaining")
        return _pop_or_preserve(
            _ConsumerRefreshResult(path, "failed", detail, branch=branch)
        )

    after = _git_status_porcelain(path)
    if after is None:
        return _pop_or_preserve(
            _ConsumerRefreshResult(path, "failed", "could not read git status", branch=branch)
        )
    if not after:
        return _pop_or_preserve(
            _ConsumerRefreshResult(
                path,
                "unchanged",
                "integration files already current",
                branch=branch,
            )
        )

    add = _run_repo_command(path, ["git", "add", "--all"])
    if add.returncode != 0:
        return _pop_or_preserve(
            _ConsumerRefreshResult(
                path,
                "failed",
                _command_failure_detail(add, "git add --all"),
                branch=branch,
            )
        )
    message = f"Refresh conductor integrations to v{__version__.split('+', 1)[0]}"
    commit = _run_repo_command(path, ["git", "commit", "-m", message])
    if commit.returncode != 0:
        return _pop_or_preserve(
            _ConsumerRefreshResult(
                path,
                "failed",
                _command_failure_detail(commit, "git commit"),
                branch=branch,
            )
        )
    sha = _run_repo_command(path, ["git", "rev-parse", "--short", "HEAD"])
    commit_sha = sha.stdout.strip() if sha.returncode == 0 else None
    return _pop_or_preserve(
        _ConsumerRefreshResult(
            path,
            "committed",
            "refresh commit ready for operator review",
            branch=branch,
            commit=commit_sha,
        )
    )


def _git_status_porcelain(path: Path) -> str | None:
    result = _run_repo_command(path, ["git", "status", "--porcelain"])
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _checkout_refresh_branch(path: Path, branch: str) -> str | None:
    exists = _run_repo_command(path, ["git", "rev-parse", "--verify", branch])
    if exists.returncode == 0:
        checkout = _run_repo_command(path, ["git", "checkout", branch])
    else:
        checkout = _run_repo_command(path, ["git", "checkout", "-b", branch])
    if checkout.returncode != 0:
        return _command_failure_detail(checkout, f"git checkout {branch}")
    return None


def _run_repo_command(
    cwd: Path,
    args: list[str],
    *,
    timeout: float | None = 120,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args=args, returncode=1, stdout="", stderr=str(e))


def _command_failure_detail(
    result: subprocess.CompletedProcess[str],
    command: str,
) -> str:
    stderr = result.stderr.strip()
    stdout = result.stdout.strip()
    detail = stderr or stdout or f"exit {result.returncode}"
    return f"`{command}` failed: {detail}"


# --------------------------------------------------------------------------- #
# delegations — inspect unified delegation ledger
# --------------------------------------------------------------------------- #


@main.group()
def delegations() -> None:
    """Inspect the append-only delegation ledger."""


@delegations.command("list")
@click.option(
    "--last",
    "last_n",
    default=20,
    type=click.IntRange(min=1),
    show_default=True,
)
@click.option("--since", default=None, help="Only show events since 1h, 24h, 7d, etc.")
@click.option(
    "--command",
    "command_filter",
    default=None,
    type=click.Choice(["ask", "call", "review", "exec", "council"]),
)
@click.option("--provider", "provider_filter", default=None)
@click.option("--include-members", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False)
def delegations_list(
    last_n: int,
    since: str | None,
    command_filter: str | None,
    provider_filter: str | None,
    include_members: bool,
    as_json: bool,
) -> None:
    """List recent delegation ledger events."""
    try:
        events = list(
            read_delegations(
                last=last_n,
                since=since,
                command=command_filter,
                provider=provider_filter,
                include_members=include_members,
            )
        )
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    if as_json:
        click.echo(json.dumps(events, default=str, indent=2))
        return

    if not events:
        click.echo("(no delegations)")
        return

    click.echo(
        "TIME              PROVIDER MODEL              CMD     STATUS  DURATION TOKENS_IN/OUT COST"
    )
    click.echo(
        "-----------------------------------------------------------------------------------------"
    )
    for event in events:
        click.echo(_format_delegation_row(event))


@delegations.command("report")
@click.option(
    "--since",
    default="7d",
    show_default=True,
    help="Only include events since 1h, 24h, 7d, etc.",
)
@click.option(
    "--last",
    "last_n",
    default=None,
    type=click.IntRange(min=1),
    help="Only inspect the N most recent matching events.",
)
@click.option(
    "--command",
    "command_filter",
    default=None,
    type=click.Choice(["ask", "call", "review", "exec", "council"]),
)
@click.option("--provider", "provider_filter", default=None)
@click.option("--tag", "tag_filter", default=None, help="Only include events with this tag.")
@click.option("--include-members", is_flag=True, default=False)
@click.option("--json", "as_json", is_flag=True, default=False)
def delegations_report(
    since: str | None,
    last_n: int | None,
    command_filter: str | None,
    provider_filter: str | None,
    tag_filter: str | None,
    include_members: bool,
    as_json: bool,
) -> None:
    """Internal diagnostic report for routing and token efficiency.

    Output is intentionally not part of the stable consumer contract.
    """
    try:
        events = list(
            read_delegations(
                since=since,
                command=command_filter,
                provider=provider_filter,
                include_members=include_members,
            )
        )
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    report = build_delegation_report(events, since=since, tag=tag_filter, last=last_n)
    if as_json:
        click.echo(json.dumps(report, default=str, indent=2))
        return
    _print_delegation_report(report)


@delegations.command("show")
@click.argument("delegation_id")
@click.option("--json", "as_json", is_flag=True, default=False)
def delegations_show(delegation_id: str, as_json: bool) -> None:
    """Show one delegation event by id."""
    events = list(read_delegations(delegation_id=delegation_id, include_members=True))
    if not events:
        raise click.ClickException(f"unknown delegation {delegation_id!r}")
    event = next(
        (candidate for candidate in events if candidate.get("delegation_id") == delegation_id),
        events[0],
    )
    if as_json:
        click.echo(json.dumps(event, default=str))
        return
    click.echo(json.dumps(event, default=str, indent=2))


def _format_delegation_row(event: dict) -> str:
    timestamp = _compact_delegation_time(event.get("timestamp"))
    provider = _truncate_cell(event.get("provider") or "-", 8)
    model = _truncate_cell(event.get("model") or "-", 18)
    command = _truncate_cell(event.get("command") or "-", 7)
    status = _truncate_cell(event.get("status") or "-", 7)
    duration = _format_duration_ms(event.get("duration_ms"))
    tokens = (
        f"{_ledger_value(event.get('input_tokens'))}/{_ledger_value(event.get('output_tokens'))}"
    )
    cost = _format_cost(event.get("cost_usd"))
    return (
        f"{timestamp:<17} {provider:<8} {model:<18} {command:<7} "
        f"{status:<7} {duration:>8} {tokens:>13} {cost:>8}"
    )


def _print_delegation_report(report: dict) -> None:
    window = report.get("window") or {}
    since = window.get("since") or "all"
    events = window.get("events") or 0
    tag = window.get("tag_filter")
    heading = f"Delegation report since {since}: {events} events"
    if tag:
        heading += f" (tag={tag})"
    click.echo(heading)
    click.echo("Internal diagnostic output; not a stable consumer API.")
    click.echo("")
    if not events:
        click.echo("(no matching delegations)")
        return
    fallbacks = report.get("route_fallbacks") or {}
    if fallbacks:
        formatted = ", ".join(f"{route}={count}" for route, count in sorted(fallbacks.items()))
        click.echo(f"Fallbacks: {formatted}")
        click.echo("")

    click.echo("By provider:")
    click.echo(
        "PROVIDER  CALLS OK/ERR  MED_DUR  IN/OUT/THINK TOKENS  OUT/1K_IN  IN/OUT  MS/OUT  COST"
    )
    for row in report.get("providers") or []:
        click.echo(_format_report_provider_row(row))

    models = report.get("models") or []
    if models:
        click.echo("")
        click.echo("By model:")
        click.echo(
            "PROVIDER  MODEL              CALLS OK/ERR  OUT/1K_IN  IN/OUT  MS/OUT  COST/1K_OUT"
        )
        for row in models[:20]:
            click.echo(_format_report_model_row(row))


def _format_report_provider_row(row: dict) -> str:
    provider = _truncate_cell(row.get("provider") or "-", 8)
    calls = _ledger_value(row.get("calls"))
    ok_err = f"{_ledger_value(row.get('ok'))}/{_ledger_value(row.get('non_ok'))}"
    duration = _format_duration_ms(row.get("median_duration_ms"))
    tokens = (
        f"{_ledger_value(row.get('input_tokens'))}/"
        f"{_ledger_value(row.get('output_tokens'))}/"
        f"{_ledger_value(row.get('thinking_tokens'))}"
    )
    out_per_in = _format_float(row.get("output_tokens_per_1k_input"))
    in_per_out = _format_float(row.get("input_tokens_per_output_token"))
    ms_per_out = _format_float(row.get("ms_per_output_token"))
    cost = _format_cost(row.get("cost_usd"))
    return (
        f"{provider:<8} {calls:>5} {ok_err:>6} {duration:>8} "
        f"{tokens:>20} {out_per_in:>10} {in_per_out:>7} {ms_per_out:>7} {cost:>8}"
    )


def _format_report_model_row(row: dict) -> str:
    provider = _truncate_cell(row.get("provider") or "-", 8)
    model = _truncate_cell(row.get("model") or "-", 18)
    calls = _ledger_value(row.get("calls"))
    ok_err = f"{_ledger_value(row.get('ok'))}/{_ledger_value(row.get('non_ok'))}"
    out_per_in = _format_float(row.get("output_tokens_per_1k_input"))
    in_per_out = _format_float(row.get("input_tokens_per_output_token"))
    ms_per_out = _format_float(row.get("ms_per_output_token"))
    cost_per_out = _format_cost(row.get("cost_per_1k_output_tokens"))
    return (
        f"{provider:<8} {model:<18} {calls:>5} {ok_err:>6} "
        f"{out_per_in:>10} {in_per_out:>7} {ms_per_out:>7} {cost_per_out:>11}"
    )


def _compact_delegation_time(value: object) -> str:
    if not isinstance(value, str):
        return "-"
    return value.replace("T", " ")[:16]


def _truncate_cell(value: object, width: int) -> str:
    text = str(value)
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


def _format_duration_ms(value: object) -> str:
    if not isinstance(value, int):
        return "-"
    if value < 1000:
        return f"{value}ms"
    return f"{value / 1000:.1f}s"


def _ledger_value(value: object) -> str:
    return str(value) if isinstance(value, int) else "-"


def _format_cost(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"${value:.4f}"


def _format_float(value: object) -> str:
    if not isinstance(value, (int, float)):
        return "-"
    return f"{value:.2f}"


# --------------------------------------------------------------------------- #
# sessions — inspect structured exec logs
# --------------------------------------------------------------------------- #


@main.group()
def sessions() -> None:
    """Inspect structured session logs for `conductor exec`."""


@sessions.command("list")
@click.option(
    "--last",
    "last_n",
    default=20,
    type=click.IntRange(min=0),
    show_default=True,
    help="Show the N most recently updated sessions; 0 disables the cap.",
)
@click.option(
    "--since",
    default=None,
    help="Only show sessions updated since 1h, 24h, 7d, etc.",
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    help="Only show sessions with this status.",
)
@click.option(
    "--provider",
    "provider_filter",
    default=None,
    help="Only show sessions for this provider.",
)
@click.option("--all", "show_all", is_flag=True, default=False, help="Show all matching sessions.")
@click.option("--json", "as_json", is_flag=True, default=False)
@click.pass_context
def sessions_list(
    ctx: click.Context,
    last_n: int,
    since: str | None,
    status_filter: str | None,
    provider_filter: str | None,
    show_all: bool,
    as_json: bool,
) -> None:
    """List known session logs with their latest status."""
    records = list_session_records()
    if records:
        _validate_session_filter("status", status_filter, {record.status for record in records})
        _validate_session_filter(
            "provider",
            provider_filter,
            {record.provider for record in records if record.provider is not None},
        )

    try:
        cutoff = since_cutoff(since)
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    records = _filter_session_records(
        records,
        cutoff=cutoff,
        status_filter=status_filter,
        provider_filter=provider_filter,
    )
    if show_all:
        if ctx.get_parameter_source("last_n") == ParameterSource.COMMANDLINE:
            click.echo("[conductor] --all ignores --last", err=True)
    elif last_n:
        records = records[-last_n:]

    if as_json:
        click.echo(json.dumps([asdict(record) for record in records], default=str, indent=2))
        return

    if not records:
        click.echo("(no session logs)")
        return

    click.echo(
        "SESSION ID                           STATUS    UPDATED                      PROVIDER"
    )
    click.echo(
        "--------------------------------------------------------------------------------------"
    )
    for record in reversed(records):
        provider = record.provider or "-"
        click.echo(
            f"{record.session_id:<36}  {record.status:<8}  {record.updated_at:<27}  {provider}"
        )


@sessions.command("prune")
@click.option(
    "--older-than",
    default=DEFAULT_SESSION_PRUNE_OLDER_THAN,
    show_default=True,
    help="Prune sessions older than a relative age like 1d, 7d, or 24h.",
)
@click.option(
    "--keep-last",
    type=click.IntRange(min=0),
    default=None,
    help="Keep the N most recent sessions and prune older sessions regardless of age.",
)
@click.option(
    "--protect-last",
    type=click.IntRange(min=0),
    default=DEFAULT_SESSION_PRUNE_PROTECT_LAST,
    show_default=True,
    help="Never prune the N most recent sessions unless --keep-last is set.",
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    help="Only prune sessions with this status. 'done' is accepted as 'finished'.",
)
@click.option(
    "--execute",
    is_flag=True,
    default=False,
    help="Delete files. Without this flag, prune is a dry-run.",
)
@click.option("--json", "as_json", is_flag=True, default=False)
def sessions_prune(
    older_than: str,
    keep_last: int | None,
    protect_last: int,
    status_filter: str | None,
    execute: bool,
    as_json: bool,
) -> None:
    """Delete old session logs and related cache artifacts."""
    try:
        plan = _build_session_prune_plan(
            older_than=older_than,
            keep_last=keep_last,
            protect_last=protect_last,
            status_filter=status_filter,
            dry_run=not execute,
        )
    except ValueError as e:
        raise click.UsageError(str(e)) from e

    if execute:
        plan = _execute_session_prune_plan(plan)

    if as_json:
        click.echo(json.dumps(asdict(plan), default=str, indent=2))
        return

    _print_session_prune_plan(plan)


def _validate_session_filter(
    name: str,
    value: str | None,
    known_values: set[str],
) -> None:
    if value is None or value in known_values:
        return
    choices = ", ".join(sorted(known_values)) or "none"
    raise click.UsageError(f"unknown session {name} {value!r}; known values: {choices}")


def _filter_session_records(
    records: list[SessionRecord],
    *,
    cutoff: datetime | None,
    status_filter: str | None,
    provider_filter: str | None,
) -> list[SessionRecord]:
    filtered: list[SessionRecord] = []
    for record in records:
        if status_filter is not None and record.status != status_filter:
            continue
        if provider_filter is not None and record.provider != provider_filter:
            continue
        if cutoff is not None and parse_timestamp(record.updated_at) < cutoff:
            continue
        filtered.append(record)
    return filtered


def _build_session_prune_plan(
    *,
    older_than: str,
    keep_last: int | None,
    protect_last: int,
    status_filter: str | None,
    dry_run: bool,
) -> SessionPrunePlan:
    records = list_session_records()
    normalized_status = _normalize_session_prune_status(status_filter)
    cutoff = None if keep_last is not None else since_cutoff(older_than)
    protected_records = set(_protected_session_record_keys(records, keep_last, protect_last))
    seen_paths: set[Path] = set()
    items: list[SessionPruneItem] = []

    for record in records:
        if record.status == "running":
            continue
        if normalized_status is not None and record.status != normalized_status:
            continue
        if record.run_id in protected_records or record.session_id in protected_records:
            continue
        if (
            keep_last is None
            and cutoff is not None
            and parse_timestamp(record.updated_at) >= cutoff
        ):
            continue
        paths = _session_prune_record_paths(record, seen_paths)
        if not paths:
            continue
        items.append(
            SessionPruneItem(
                kind="session",
                session_id=record.session_id,
                status=record.status,
                updated_at=record.updated_at,
                paths=paths,
            )
        )

    if normalized_status is None:
        items.extend(
            _session_prune_artifact_items(
                cutoff=cutoff,
                keep_last=keep_last,
                protect_last=protect_last,
                seen_paths=seen_paths,
            )
        )

    return _session_prune_plan(
        dry_run=dry_run,
        older_than=None if keep_last is not None else older_than,
        keep_last=keep_last,
        protect_last=keep_last if keep_last is not None else protect_last,
        items=items,
    )


def _normalize_session_prune_status(status: str | None) -> str | None:
    if status is None:
        return None
    if status == "done":
        return "finished"
    return status


def _protected_session_record_keys(
    records: list[SessionRecord],
    keep_last: int | None,
    protect_last: int,
) -> set[str]:
    protected_count = keep_last if keep_last is not None else protect_last
    if protected_count <= 0:
        return set()
    protected: set[str] = set()
    for record in records[-protected_count:]:
        protected.add(record.run_id)
        protected.add(record.session_id)
    return protected


def _session_prune_record_paths(
    record: SessionRecord,
    seen_paths: set[Path],
) -> tuple[SessionPrunePath, ...]:
    cache_dir = offline_mode._cache_dir()
    codex_artifacts = [
        path
        for path in (
            cache_dir / f"codex-exec-{record.session_id}.json",
            cache_dir / f"codex-exec-{record.run_id}.json",
        )
        if path.exists()
    ]
    candidates = [
        sessions_dir() / f"{record.run_id}.meta.json",
        record.log_path,
        *codex_artifacts,
    ]
    return _session_prune_paths(candidates, seen_paths=seen_paths)


def _session_prune_artifact_items(
    *,
    cutoff: datetime | None,
    keep_last: int | None,
    protect_last: int,
    seen_paths: set[Path],
) -> list[SessionPruneItem]:
    artifacts = _session_prune_cache_artifacts(seen_paths)
    protected_count = keep_last if keep_last is not None else protect_last
    protected = {path for path, _ in artifacts[-protected_count:]} if protected_count else set()
    items: list[SessionPruneItem] = []
    for path, updated_at in artifacts:
        if path in protected:
            continue
        if keep_last is None and cutoff is not None and parse_timestamp(updated_at) >= cutoff:
            continue
        paths = _session_prune_paths([path], seen_paths=seen_paths)
        if not paths:
            continue
        items.append(
            SessionPruneItem(
                kind="artifact",
                session_id=None,
                status=None,
                updated_at=updated_at,
                paths=paths,
            )
        )
    return items


def _session_prune_cache_artifacts(seen_paths: set[Path]) -> list[tuple[Path, str]]:
    cache_dir = offline_mode._cache_dir()
    candidates: set[Path] = set()
    for pattern in ("codex-exec-*.json", "codex-*.json"):
        candidates.update(cache_dir.glob(pattern))
    artifacts: list[tuple[Path, str]] = []
    for path in candidates:
        if path in seen_paths or not path.is_file():
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError as e:
            click.echo(f"[conductor] prune: could not stat {path}: {e}", err=True)
            continue
        artifacts.append((path, datetime.fromtimestamp(mtime, UTC).isoformat()))
    return sorted(artifacts, key=lambda item: (item[1], str(item[0])))


def _session_prune_paths(
    paths: list[Path],
    *,
    seen_paths: set[Path],
) -> tuple[SessionPrunePath, ...]:
    planned: list[SessionPrunePath] = []
    for path in paths:
        resolved = path.expanduser()
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        planned.append(SessionPrunePath(path=resolved, size_bytes=_path_size(resolved)))
    return tuple(planned)


def _path_size(path: Path) -> int:
    try:
        if not path.is_file():
            return 0
        return path.stat().st_size
    except OSError as e:
        click.echo(f"[conductor] prune: could not stat {path}: {e}", err=True)
        return 0


def _session_prune_plan(
    *,
    dry_run: bool,
    older_than: str | None,
    keep_last: int | None,
    protect_last: int,
    items: list[SessionPruneItem],
) -> SessionPrunePlan:
    total_paths = sum(len(item.paths) for item in items)
    total_bytes = sum(path.size_bytes for item in items for path in item.paths)
    return SessionPrunePlan(
        dry_run=dry_run,
        older_than=older_than,
        keep_last=keep_last,
        protect_last=protect_last,
        total_items=len(items),
        total_paths=total_paths,
        total_bytes=total_bytes,
        items=tuple(items),
    )


def _execute_session_prune_plan(plan: SessionPrunePlan) -> SessionPrunePlan:
    executed_items: list[SessionPruneItem] = []
    for item in plan.items:
        executed_paths: list[SessionPrunePath] = []
        for planned_path in item.paths:
            try:
                planned_path.path.unlink(missing_ok=True)
                click.echo(f"[conductor] prune deleted {planned_path.path}", err=True)
                executed_paths.append(replace(planned_path, deleted=True))
            except OSError as e:
                click.echo(
                    f"[conductor] prune could not delete {planned_path.path}: {e}",
                    err=True,
                )
                executed_paths.append(replace(planned_path, error=str(e)))
        executed_items.append(replace(item, paths=tuple(executed_paths)))
    return _session_prune_plan(
        dry_run=False,
        older_than=plan.older_than,
        keep_last=plan.keep_last,
        protect_last=plan.protect_last,
        items=executed_items,
    )


def _print_session_prune_plan(plan: SessionPrunePlan) -> None:
    mode = "would delete" if plan.dry_run else "deleted"
    click.echo(
        f"session prune ({'dry-run' if plan.dry_run else 'execute'}): "
        f"{mode} {plan.total_paths} paths across {plan.total_items} items "
        f"({_format_bytes(plan.total_bytes)})"
    )
    if plan.keep_last is not None:
        click.echo(f"criteria: keep-last={plan.keep_last}")
    else:
        click.echo(
            f"criteria: older-than={plan.older_than}, "
            f"protect-last={plan.protect_last}, never status=running"
        )
    if not plan.items:
        return
    for item in plan.items:
        label = item.session_id or item.kind
        status = item.status or "-"
        click.echo(f"{label}  {status}  updated={item.updated_at}")
        for planned_path in item.paths:
            state = ""
            if planned_path.error is not None:
                state = f" error={planned_path.error}"
            elif planned_path.deleted:
                state = " deleted"
            click.echo(f"  {_format_bytes(planned_path.size_bytes):>9}  {planned_path.path}{state}")


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    if size < 1024 * 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MiB"
    return f"{size / (1024 * 1024 * 1024):.1f} GiB"


@sessions.command("tail")
@click.argument("session_id", required=False)
def sessions_tail(session_id: str | None) -> None:
    """Print a session log and follow it while the session is running."""
    if session_id is None:
        record = latest_active_session()
        if record is None:
            click.echo("no active session")
            return
    else:
        record = find_session_record(session_id)
        if record is None:
            raise click.ClickException(f"unknown session {session_id!r}")

    _tail_record(record)


# --------------------------------------------------------------------------- #
# models — inspect and refresh the OpenRouter catalog cache
# --------------------------------------------------------------------------- #


@main.group()
def models() -> None:
    """Inspect and refresh the cached OpenRouter model catalog."""


@models.command("refresh")
def models_refresh() -> None:
    """Fetch the live OpenRouter catalog and rewrite the local cache."""
    try:
        snapshot = openrouter_catalog.load_catalog_snapshot(force_refresh=True)
    except ProviderHTTPError as e:
        raise click.ClickException(str(e)) from e

    click.echo(
        f"Refreshed OpenRouter catalog at "
        f"{openrouter_catalog.format_timestamp(snapshot.fetched_at)}"
    )
    click.echo(
        f"  {len(snapshot.models)} models · cache TTL "
        f"{openrouter_catalog.cache_ttl_hours()}h · written to "
        f"{openrouter_catalog.display_cache_path()}"
    )


@models.command("validate-stacks")
@click.option(
    "--no-refresh",
    is_flag=True,
    help="Use the cached OpenRouter catalog instead of fetching the live catalog.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def models_validate_stacks(no_refresh: bool, as_json: bool) -> None:
    """Validate curated OpenRouter stacks against the catalog."""
    try:
        snapshot = openrouter_catalog.load_catalog_snapshot(
            force_refresh=not no_refresh,
            allow_stale_on_error=no_refresh,
        )
    except ProviderHTTPError as e:
        raise click.ClickException(str(e)) from e

    report = audit_openrouter_coding_stacks(snapshot)
    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True))
    else:
        _emit_stack_audit_report(report)

    if report.has_errors:
        sys.exit(1)


@models.command("list")
def models_list() -> None:
    """Print the cached OpenRouter catalog summary."""
    snapshot = _openrouter_catalog_or_exit()
    click.echo(
        f"{len(snapshot.models)} models indexed, last refresh: "
        f"{openrouter_catalog.format_timestamp(snapshot.fetched_at)}"
    )
    click.echo(
        f"  cache TTL {openrouter_catalog.cache_ttl_hours()}h · "
        f"cache file {openrouter_catalog.display_cache_path()}"
    )
    click.echo("")

    sorted_models = sorted(snapshot.models, key=lambda model: model.id)
    if not sorted_models:
        click.echo("(catalog cache is empty — run `conductor models refresh` to populate)")
        return
    id_w = max(len("MODEL"), max(len(model.id) for model in sorted_models))
    ctx_w = max(len("CTX"), max(len(f"{model.context_length:,}") for model in sorted_models))
    header = f"{'MODEL':<{id_w}}  {'CTX':>{ctx_w}}  {'IN/1K':>10}  {'OUT/1K':>10}  CAPS"
    click.echo(header)
    click.echo("-" * len(header))
    for model in sorted_models:
        click.echo(
            f"{model.id:<{id_w}}  "
            f"{model.context_length:>{ctx_w},}  "
            f"{model.pricing_prompt:>10.6f}  "
            f"{model.pricing_completion:>10.6f}  "
            f"{_model_capabilities(model)}"
        )


@models.command("show")
@click.argument("slug")
def models_show(slug: str) -> None:
    """Print one cached OpenRouter model's parsed details."""
    snapshot = _openrouter_catalog_or_exit()
    model = next((entry for entry in snapshot.models if entry.id == slug), None)
    if model is None:
        raise click.ClickException(
            f"OpenRouter model {slug!r} was not found in the local cache. "
            "Run `conductor models refresh`."
        )

    thinking_price = (
        "n/a" if model.pricing_thinking is None else f"{model.pricing_thinking:.6f} USD / 1k"
    )
    click.echo(model.id)
    click.echo(f"  name: {model.name}")
    click.echo(f"  created: {openrouter_catalog.format_timestamp(model.created)}")
    click.echo(f"  context length: {model.context_length:,}")
    click.echo(f"  prompt price: {model.pricing_prompt:.6f} USD / 1k")
    click.echo(f"  completion price: {model.pricing_completion:.6f} USD / 1k")
    click.echo(f"  thinking price: {thinking_price}")
    click.echo(
        "  capabilities: "
        f"thinking={'yes' if model.supports_thinking else 'no'} · "
        f"tools={'yes' if model.supports_tools else 'no'} · "
        f"vision={'yes' if model.supports_vision else 'no'}"
    )


def _emit_stack_audit_report(report: StackAuditReport) -> None:
    click.echo(
        "OpenRouter coding stack audit "
        f"(version {report.stack_version}, catalog "
        f"{openrouter_catalog.format_timestamp(report.catalog_fetched_at)})"
    )
    click.echo(report.policy)
    click.echo("")
    if not report.findings:
        click.echo("No stack issues found.")
        return

    errors = [finding for finding in report.findings if finding.severity == "error"]
    warnings = [finding for finding in report.findings if finding.severity == "warning"]
    click.echo(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    for finding in report.findings:
        click.echo(
            f"- {finding.severity.upper()} {finding.stack} {finding.model}: "
            f"{finding.code} - {finding.message}"
        )


# --------------------------------------------------------------------------- #
# providers — manage user-local custom (shell-command) providers
# --------------------------------------------------------------------------- #


@main.group()
def providers() -> None:
    """Manage user-local provider state (custom integrations + muting).

    Custom providers let you register an arbitrary CLI — your own
    internal LLM wrapper, a different model's inference script, a local
    model server's CLI frontend — as a first-class Conductor provider.
    Once registered, it appears in `conductor list`, participates in
    auto-routing, and is callable via `conductor call --with <name>`.

    Custom providers are single-turn (no tool-use) and stateless (no
    resume). For CLIs that run their own agent loop internally, that
    happens inside the shell command, not through Conductor's router.

    Muting is persistent: muted providers are hidden from doctor's
    "Available" section and excluded from auto-routing until unmuted.
    """


@providers.command("add")
@click.option(
    "--name",
    required=True,
    help="Identifier used for --with and auto-routing. Must be unique, not a built-in name.",
)
@click.option(
    "--shell",
    required=True,
    help="The shell command to run. First token must be on PATH (shutil.which). "
    "Supports quoted arguments via standard shell quoting.",
)
@click.option(
    "--accepts",
    type=click.Choice(["stdin", "argv"]),
    default="stdin",
    show_default=True,
    help="How the prompt reaches the command. `stdin`: piped on stdin (default). "
    "`argv`: appended as the last positional argument.",
)
@click.option(
    "--tags",
    default="",
    help="Comma-separated capability tags for auto-routing (e.g. 'code-review,offline').",
)
@click.option(
    "--tier",
    type=click.Choice(list(QUALITY_TIERS)),
    default="local",
    show_default=True,
    help="Quality tier for prefer=best scoring.",
)
@click.option(
    "--cost-per-1k-in",
    type=float,
    default=0.0,
    help="Input cost in USD per 1,000 tokens (for prefer=cheapest scoring).",
)
@click.option(
    "--cost-per-1k-out",
    type=float,
    default=0.0,
    help="Output cost in USD per 1,000 tokens.",
)
@click.option(
    "--typical-p50-ms",
    type=int,
    default=3000,
    show_default=True,
    help="Typical p50 latency in milliseconds (for prefer=fastest scoring).",
)
def providers_add(
    name: str,
    shell: str,
    accepts: str,
    tags: str,
    tier: str,
    cost_per_1k_in: float,
    cost_per_1k_out: float,
    typical_p50_ms: int,
) -> None:
    """Register a custom shell-command provider."""
    from conductor.custom_providers import CustomProviderError, add_spec
    from conductor.providers.shell import ShellProviderSpec

    try:
        spec = ShellProviderSpec(
            name=name,
            shell=shell,
            accepts=accepts,  # type: ignore[arg-type]
            tags=tuple(t.strip() for t in tags.split(",") if t.strip()),
            quality_tier=tier,
            cost_per_1k_in=cost_per_1k_in,
            cost_per_1k_out=cost_per_1k_out,
            typical_p50_ms=typical_p50_ms,
        )
    except (TypeError, ValueError) as e:
        raise click.UsageError(f"invalid provider spec: {e}") from e

    # Guard against shadowing built-ins — the loader does the same check
    # when reading the file, but catching it here gives a friendlier error
    # before the file is touched.
    if name in {
        "kimi",
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "ollama",
    }:
        raise click.UsageError(
            f"`{name}` is a built-in provider identifier. Pick a different name."
        )

    try:
        path = add_spec(spec)
    except CustomProviderError as e:
        raise click.UsageError(str(e)) from e

    click.echo(f"==> registered custom provider `{name}`")
    click.echo(f"    shell:   {shell}")
    click.echo(f"    accepts: {accepts}")
    click.echo(f"    tier:    {tier}")
    if spec.tags:
        click.echo(f"    tags:    {', '.join(spec.tags)}")
    click.echo(f"    file:    {path}")
    click.echo("")
    click.echo(f"Try it: conductor smoke {name}")
    click.echo(f"Use it: conductor call --with {name} --brief 'hello'")


@providers.command("remove")
@click.argument("name")
def providers_remove(name: str) -> None:
    """Remove a custom provider by name."""
    from conductor.custom_providers import remove_spec

    path, removed = remove_spec(name)
    if not removed:
        click.echo(f"conductor: no custom provider `{name}` (check {path})", err=True)
        sys.exit(1)
    click.echo(f"==> removed custom provider `{name}` from {path}")


@providers.command("mute")
@click.argument("names", nargs=-1, required=True)
def providers_mute(names: tuple[str, ...]) -> None:
    """Persistently mute one or more providers."""
    try:
        path, added = mute_provider_ids(list(names), known=set(known_providers()))
    except MutedProvidersError as e:
        raise click.UsageError(str(e)) from e

    if added:
        click.echo(f"==> muted: {', '.join(added)}")
    else:
        click.echo("==> no changes; all requested providers were already muted")
    click.echo(f"    file: {path}")


@providers.command("unmute")
@click.argument("names", nargs=-1, required=True)
def providers_unmute(names: tuple[str, ...]) -> None:
    """Remove one or more providers from the persistent mute list."""
    try:
        path, removed = unmute_provider_ids(list(names), known=set(known_providers()))
    except MutedProvidersError as e:
        raise click.UsageError(str(e)) from e

    if removed:
        click.echo(f"==> unmuted: {', '.join(removed)}")
    else:
        click.echo("==> no changes; none of the requested providers were muted")
    click.echo(f"    file: {path}")


@providers.command("list")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the custom-provider list as JSON.",
)
def providers_list(as_json: bool) -> None:
    """Show persistent muted state plus registered custom providers."""
    from conductor.custom_providers import load_specs, providers_file_path

    try:
        muted = load_muted_provider_ids(known=set(known_providers()))
    except MutedProvidersError as e:
        raise click.ClickException(str(e)) from e

    specs = load_specs()
    if as_json:
        payload = [
            {
                "name": s.name,
                "shell": s.shell,
                "accepts": s.accepts,
                "tags": list(s.tags),
                "tier": s.quality_tier,
                "runtime": PROVIDER_RUNTIME_TEXT_ONLY,
                "cost_per_1k_in": s.cost_per_1k_in,
                "cost_per_1k_out": s.cost_per_1k_out,
                "typical_p50_ms": s.typical_p50_ms,
                "muted": s.name in muted,
            }
            for s in specs
        ]
        click.echo(json.dumps(payload, indent=2))
        return

    path = providers_file_path()
    muted_path = muted_providers_file_path()
    click.echo("Muted providers: " + (", ".join(muted) if muted else "(none)"))
    click.echo(f"file: {muted_path} {'(not yet created)' if not muted_path.exists() else ''}")
    click.echo("")

    if not specs:
        click.echo("(no custom providers; register via `conductor providers add`)")
        click.echo(f"file: {path} {'(not yet created)' if not path.exists() else ''}")
        return

    click.echo(f"Custom providers ({path}):")
    click.echo("")
    for s in specs:
        muted_note = "  [muted]" if s.name in muted else ""
        click.echo(f"  {s.name}{muted_note}")
        click.echo(f"    shell:    {s.shell}")
        click.echo(f"    accepts:  {s.accepts}")
        click.echo(f"    tier:     {s.quality_tier}")
        click.echo(f"    runtime:  {PROVIDER_RUNTIME_TEXT_ONLY}")
        if s.tags:
            click.echo(f"    tags:     {', '.join(s.tags)}")
        if s.cost_per_1k_in or s.cost_per_1k_out:
            click.echo(f"    cost:     ${s.cost_per_1k_in}/1k in, ${s.cost_per_1k_out}/1k out")


if __name__ == "__main__":
    main()
