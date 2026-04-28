from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(".github/workflows/nightly-smoke.yml")


def test_nightly_smoke_skips_missing_secrets_without_failure_issue():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "::error::$name secret is required for live subprocess smoke" not in text
    assert "::notice::$name secret is not configured; skipping live subprocess smoke" in text
    assert "touch live-smoke-skipped.txt" in text
    assert re.search(
        r'if \[ "\$\{#missing\[@\]\}" -ne 0 \]; then.*?exit 0',
        text,
        flags=re.S,
    )


def test_nightly_smoke_dedupes_failure_issues_by_label():
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "gh issue list" in text
    assert "--state open" in text
    assert "--label live-smoke-failure" in text
    assert ".[0].number // empty" in text
    assert 'gh issue comment "$existing_issue" --body-file live-smoke-issue.md' in text
    assert "gh issue create" in text
