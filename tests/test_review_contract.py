from __future__ import annotations

import pytest

from conductor.providers.review_contract import ensure_requested_review_sentinel

STRICT_SENTINEL_PROMPT = """
## Output contract -- strict

The LAST line of your output must be exactly one of these three sentinels:

- CODEX_REVIEW_CLEAN -- no blocking issues found
- CODEX_REVIEW_FIXED -- you applied auto-fixes
- CODEX_REVIEW_BLOCKED -- blocking issues remain
"""


@pytest.mark.parametrize(
    "sentinel",
    [
        "CODEX_REVIEW_CLEAN",
        "CODEX_REVIEW_FIXED",
        "CODEX_REVIEW_BLOCKED",
    ],
)
def test_review_sentinel_contract_accepts_each_final_sentinel(sentinel: str):
    text = f"Review body.\n{sentinel}\n"

    repaired = ensure_requested_review_sentinel(
        provider_name="codex",
        prompt=STRICT_SENTINEL_PROMPT,
        text=text,
    )

    assert repaired == f"Review body.\n{sentinel}"


def test_review_sentinel_contract_repairs_prose_without_standalone_sentinel():
    repaired = ensure_requested_review_sentinel(
        provider_name="codex",
        prompt=STRICT_SENTINEL_PROMPT,
        text=(
            "I did not find a blocking issue. The reviewer should emit "
            "CODEX_REVIEW_CLEAN for that case."
        ),
    )

    assert repaired == (
        "I did not find a blocking issue. The reviewer should emit "
        "CODEX_REVIEW_CLEAN for that case.\nCODEX_REVIEW_BLOCKED"
    )
    assert repaired.splitlines()[-1] == "CODEX_REVIEW_BLOCKED"
    assert sum(1 for line in repaired.splitlines() if line.startswith("CODEX_REVIEW_")) == 1


def test_review_sentinel_contract_ignores_non_contract_assist_prompt():
    text = "Answer the primary reviewer. This peer response is advisory only."

    repaired = ensure_requested_review_sentinel(
        provider_name="codex",
        prompt=(
            "Answer the primary reviewer concisely and directly. Do not emit "
            "CODEX_REVIEW_CLEAN, CODEX_REVIEW_FIXED, or CODEX_REVIEW_BLOCKED."
        ),
        text=text,
    )

    assert repaired == text
