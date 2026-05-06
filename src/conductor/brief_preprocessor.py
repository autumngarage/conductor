"""Pure-string preprocessing for delegation briefs."""

from __future__ import annotations

import re

AUTO_CLOSE_HEADER = "## Auto-close"

# The verb list is intentionally narrow. These are the only contexts where the
# delegate is explicitly being asked to complete work tied to an issue:
# - fix/close/resolve: GitHub-recognized completion language or its imperative.
# - address: project planning language that means the issue should be handled.
# - for: common issue-scoped task phrasing, as in "work for #123".
# - issue: only accepted when the issue reference is immediately followed by an
#   action verb, so a bare mention like "see issue #123" does not auto-close.
_ACTION_VERB = r"(?:fix(?:es)?|close(?:s)?|resolve(?:s)?|address(?:es)?)"
_REF = r"(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?\#\d+"
_FENCED_CODE_BLOCK_RE = re.compile(r"(?ms)^```.*?^```")
_AUTO_CLOSE_HEADER_RE = re.compile(r"(?im)^##\s+Auto-close\s*$")
_FULL_LINE_CLOSE_RE = re.compile(
    r"(?im)^Closes\s+(?P<ref>(?:[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?#\d+)\s*\.?\s*$"
)
_ISSUE_REF_RE = re.compile(
    rf"""
    (?:
        \b(?P<verb>{_ACTION_VERB})\b
        (?:\s+issue)?
        \s+
        (?P<verb_ref>{_REF})
    )
    |
    (?:
        \b(?P<for>for)\b
        \s+
        (?P<for_ref>{_REF})
    )
    |
    (?:
        \b(?P<issue>issue)\b
        \s+
        (?P<issue_ref>{_REF})
        (?=[\s,.;:)-]+\b(?P<after>{_ACTION_VERB})\b)
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _brief_without_fenced_code(brief: str) -> str:
    return _FENCED_CODE_BLOCK_RE.sub("", brief)


def _detected_refs(brief: str) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()
    for match in _ISSUE_REF_RE.finditer(_brief_without_fenced_code(brief)):
        ref = match.group("verb_ref") or match.group("for_ref") or match.group("issue_ref")
        key = ref.casefold()
        if key in seen:
            continue
        seen.add(key)
        refs.append(ref)
    return refs


def _existing_full_line_closes(brief: str) -> set[str]:
    return {match.group("ref").casefold() for match in _FULL_LINE_CLOSE_RE.finditer(brief)}


def inject_auto_close(brief: str) -> str:
    """Detect issue refs in a delegation brief and append explicit
    auto-close instructions for the delegate to copy verbatim.
    """
    refs = _detected_refs(brief)
    if not refs:
        return brief
    if _AUTO_CLOSE_HEADER_RE.search(brief):
        return brief

    existing_closes = _existing_full_line_closes(brief)
    if all(ref.casefold() in existing_closes for ref in refs):
        return brief

    close_lines = "\n".join(f"Closes {ref}" for ref in refs)
    section = (
        f"{AUTO_CLOSE_HEADER}\n\n"
        "When you open the pull request for this work, include the\n"
        "following line(s) verbatim in the PR body (NOT just the title)\n"
        "so GitHub's auto-close fires on merge:\n\n"
        f"{close_lines}"
    )
    return f"{brief.rstrip()}\n\n{section}"
