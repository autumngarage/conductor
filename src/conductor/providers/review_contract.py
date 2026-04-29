"""Helpers for native review output contracts."""

from __future__ import annotations

import re
import sys

_REVIEW_SENTINEL_RE = re.compile(r"^\s*(CODEX_REVIEW_(?:CLEAN|FIXED|BLOCKED))\s*$")
_SAFE_BLOCKED_SENTINEL = "CODEX_REVIEW_BLOCKED"


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
    if "CODEX_REVIEW_CLEAN" not in prompt:
        return text

    stripped = text.strip()
    lines = stripped.splitlines()
    sentinel_indexes: list[int] = []
    for idx, line in enumerate(lines):
        if _REVIEW_SENTINEL_RE.match(line):
            sentinel_indexes.append(idx)

    if len(sentinel_indexes) == 1 and sentinel_indexes[0] == len(lines) - 1:
        return stripped

    reason = "missing"
    if sentinel_indexes:
        reason = "misplaced" if len(sentinel_indexes) == 1 else "multiple"
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
