"""Helpers for native review output contracts."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

from conductor.providers.interface import ProviderError

_REVIEW_SENTINEL_RE = re.compile(r"^\s*(CODEX_REVIEW_(?:CLEAN|FIXED|BLOCKED))\s*$")
_MISSING_REVIEW_CONTEXT_RE = re.compile(
    r"\b("
    r"no files? (?:were )?provided|"
    r"no (?:code )?changes? (?:were )?provided|"
    r"no diff (?:was )?provided|"
    r"provide (?:the )?(?:code changes?|diff|file diffs?|files? themselves)"
    r")\b",
    re.IGNORECASE,
)
_REVIEW_SENTINELS = (
    "CODEX_REVIEW_CLEAN",
    "CODEX_REVIEW_FIXED",
    "CODEX_REVIEW_BLOCKED",
)
_SAFE_BLOCKED_SENTINEL = "CODEX_REVIEW_BLOCKED"
_DEFAULT_PATCH_CONTEXT_MAX_BYTES = 200_000
_REVIEW_GIT_TIMEOUT_SEC = 30.0
_CONTRACT_ERROR_PREVIEW_CHARS = 240


class ReviewContextError(RuntimeError):
    """Raised when generic review fallback cannot build required patch context."""


class ReviewOutputContractError(ProviderError):
    """Raised when a review provider violates the requested verdict contract."""

    def __init__(
        self,
        *,
        provider_name: str,
        reason: str,
        output_preview: str,
    ) -> None:
        self.provider_name = provider_name
        self.reason = reason
        self.output_preview = output_preview
        preview = f"; output tail: {output_preview!r}" if output_preview else ""
        super().__init__(
            f"{provider_name} review output did not match the expected sentinel "
            f"contract ({reason}); expected exactly one final standalone "
            "CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, or CODEX_REVIEW_BLOCKED line"
            f"{preview}"
        )


def build_review_task_prompt(
    task: str,
    *,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    title: str | None,
    cwd: str | None = None,
    include_patch: bool = False,
    max_patch_bytes: int = _DEFAULT_PATCH_CONTEXT_MAX_BYTES,
) -> str:
    """Attach review target metadata to a generic provider prompt.

    Invariant: fallback reviewers receive the same target selection data that
    native review providers receive through their own prompt builders. Generic
    call()-based fallback reviewers also receive the patch text because they
    have no native repository-review entrypoint or tool access.
    """
    target_lines: list[str] = []
    if base:
        target_lines.append(f"- Review changes against base branch/ref: {base}")
    if commit:
        target_lines.append(f"- Review commit: {commit}")
    if uncommitted:
        target_lines.append("- Include staged, unstaged, and untracked changes.")
    if title:
        target_lines.append(f"- Review title: {title}")
    prompt_parts: list[str] = []
    if target_lines:
        prompt_parts.append("Review target:\n" + "\n".join(target_lines))
    if include_patch:
        patch = build_review_patch_context(
            base=base,
            commit=commit,
            uncommitted=uncommitted,
            cwd=cwd,
            max_bytes=max_patch_bytes,
        )
        patch = (
            "Use the embedded patch below as the review input. Do not ask the "
            "caller to provide files or a diff.\n\n"
            f"{patch}"
        )
        prompt_parts.append(patch)
    prompt_parts.append(task)
    return "\n\n".join(prompt_parts)


def build_review_patch_context(
    *,
    base: str | None,
    commit: str | None,
    uncommitted: bool,
    cwd: str | None,
    max_bytes: int = _DEFAULT_PATCH_CONTEXT_MAX_BYTES,
) -> str:
    """Build inline patch context for non-native review providers."""
    repo = Path(cwd) if cwd is not None else Path.cwd()
    chunks: list[str] = []
    if base:
        chunks.append(_run_git(["diff", "--binary", f"{base}..HEAD"], cwd=repo))
    elif commit:
        chunks.append(
            _run_git(
                ["show", "--format=medium", "--stat", "--patch", "--binary", commit],
                cwd=repo,
            )
        )
    elif uncommitted:
        chunks.extend(_uncommitted_patch_chunks(cwd=repo))
    else:
        chunks.append(
            "No explicit review target was provided. Review the repository context "
            "described in the brief."
        )

    patch_text = "\n".join(chunk for chunk in chunks if chunk.strip()).strip()
    if not patch_text:
        patch_text = "<empty patch>"
    patch_text, truncated = _truncate_utf8(patch_text, max_bytes=max_bytes)
    suffix = (
        "\n\n[conductor] Patch context truncated at "
        f"{max_bytes} bytes for this generic review fallback."
        if truncated
        else ""
    )
    return (
        "Patch context for generic review fallback:\n"
        "```diff\n"
        f"{patch_text}\n"
        "```"
        f"{suffix}"
    )


def _uncommitted_patch_chunks(*, cwd: Path) -> list[str]:
    chunks = [
        _run_git(["diff", "--cached", "--binary"], cwd=cwd),
        _run_git(["diff", "--binary"], cwd=cwd),
    ]
    untracked = _run_git(["ls-files", "--others", "--exclude-standard", "-z"], cwd=cwd)
    for raw_path in untracked.split("\0"):
        path = raw_path.strip()
        if not path:
            continue
        chunks.append(
            _run_git_no_index(
                ["diff", "--no-index", "--binary", "--", "/dev/null", path],
                cwd=cwd,
            )
        )
    return chunks


def _run_git(args: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_REVIEW_GIT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ReviewContextError(
            f"`git {' '.join(args)}` timed out after {_REVIEW_GIT_TIMEOUT_SEC:.0f}s "
            "while building review patch context"
        ) from e
    except OSError as e:
        raise ReviewContextError(
            f"`git {' '.join(args)}` failed to start while building review patch "
            f"context: {e}"
        ) from e
    if result.returncode != 0:
        raise ReviewContextError(
            f"could not build review patch context from `git {' '.join(args)}`: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    return result.stdout


def _run_git_no_index(args: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_REVIEW_GIT_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired as e:
        raise ReviewContextError(
            f"`git {' '.join(args)}` timed out after {_REVIEW_GIT_TIMEOUT_SEC:.0f}s "
            "while building untracked-file review patch"
        ) from e
    except OSError as e:
        raise ReviewContextError(
            f"`git {' '.join(args)}` failed to start while building untracked-file "
            f"review patch: {e}"
        ) from e
    if result.returncode not in {0, 1}:
        raise ReviewContextError(
            f"could not build untracked-file review patch from `git {' '.join(args)}`: "
            f"{(result.stderr or result.stdout).strip()[:500]}"
        )
    return result.stdout


def _truncate_utf8(text: str, *, max_bytes: int) -> tuple[str, bool]:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text, False
    return encoded[:max_bytes].decode("utf-8", errors="replace"), True


def ensure_requested_review_sentinel(
    *,
    provider_name: str,
    prompt: str,
    text: str,
) -> str:
    """Guarantee the Touchstone sentinel when the caller requested it.

    Invariant: if the input prompt contains the Touchstone sentinel contract,
    the returned text has exactly one standalone sentinel line, and it is the
    final non-empty line. Ambiguous provider output fails closed as BLOCKED.
    """
    if not _prompt_requests_review_sentinel(prompt):
        return text

    reason, stripped, lines = _review_sentinel_violation(text)
    if reason is None:
        return stripped

    print(
        f"[conductor] {provider_name} review repaired {reason} "
        f"Touchstone sentinel; appending {_SAFE_BLOCKED_SENTINEL}",
        file=sys.stderr,
    )
    body_lines = [
        line for line in lines if not _REVIEW_SENTINEL_RE.match(line)
    ]
    body = "\n".join(body_lines).rstrip()
    if body:
        return f"{body}\n{_SAFE_BLOCKED_SENTINEL}"
    return _SAFE_BLOCKED_SENTINEL


def validate_requested_review_sentinel(
    *,
    provider_name: str,
    prompt: str,
    text: str,
) -> str:
    """Validate the Touchstone sentinel when the caller requested it.

    Invariant: requested review verdicts are accepted only when the provider
    emits exactly one standalone sentinel. The accepted text is normalized so
    the sentinel is the final line, matching Touchstone's shell extractor while
    keeping downstream consumers' simpler final-line contract.
    """
    if not _prompt_requests_review_sentinel(prompt):
        return text

    context_reason = _review_context_failure_reason(prompt=prompt, text=text)
    if context_reason is not None:
        raise ReviewOutputContractError(
            provider_name=provider_name,
            reason=context_reason,
            output_preview=_contract_error_preview(text),
        )

    reason, stripped, _lines = _review_sentinel_violation(text)
    if reason is not None:
        raise ReviewOutputContractError(
            provider_name=provider_name,
            reason=reason,
            output_preview=_contract_error_preview(text),
        )
    return stripped


def _review_context_failure_reason(*, prompt: str, text: str) -> str | None:
    if not _prompt_contains_non_empty_patch(prompt):
        return None
    if _MISSING_REVIEW_CONTEXT_RE.search(text):
        return "missing-context"
    return None


def _prompt_contains_non_empty_patch(prompt: str) -> bool:
    marker = "Patch context for generic review fallback:\n```diff\n"
    start = prompt.find(marker)
    if start == -1:
        return False
    patch_start = start + len(marker)
    end = prompt.find("\n```", patch_start)
    if end == -1:
        return False
    patch = prompt[patch_start:end].strip()
    return bool(
        patch
        and patch != "<empty patch>"
        and not patch.startswith("No explicit review target was provided.")
    )


def _review_sentinel_violation(text: str) -> tuple[str | None, str, list[str]]:
    stripped = text.strip()
    lines = stripped.splitlines()
    sentinel_indexes = [
        idx for idx, line in enumerate(lines) if _REVIEW_SENTINEL_RE.match(line)
    ]
    if len(sentinel_indexes) == 1:
        sentinel_idx = sentinel_indexes[0]
        sentinel = lines[sentinel_idx].strip()
        body_lines = [
            line for idx, line in enumerate(lines)
            if idx != sentinel_idx
        ]
        body = "\n".join(body_lines).rstrip()
        normalized = f"{body}\n{sentinel}" if body else sentinel
        return None, normalized, lines
    if not sentinel_indexes:
        return "missing", stripped, lines
    return "multiple", stripped, lines


def _contract_error_preview(text: str) -> str:
    normalized = " ".join(text.strip().split())
    if len(normalized) <= _CONTRACT_ERROR_PREVIEW_CHARS:
        return normalized
    return normalized[-_CONTRACT_ERROR_PREVIEW_CHARS:]


def _prompt_requests_review_sentinel(prompt: str) -> bool:
    """Return True only for prompts that ask for a final review sentinel."""
    if "CODEX_REVIEW_CLEAN" not in prompt:
        return False
    normalized = prompt.lower()
    return any(
        phrase in normalized
        for phrase in (
            "last line",
            "final standalone",
            "end with",
            "ends with",
            "end your output",
        )
    )
