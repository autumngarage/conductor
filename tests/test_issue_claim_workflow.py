from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(".github/workflows/issue-claim-check.yml")


def test_issue_claim_workflow_uses_trusted_base_helper() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "ref: ${{ github.event.pull_request.base.sha }}" in text
    assert "path: .touchstone-claim-base" in text
    assert 'helper=".touchstone-claim-base/scripts/issue-claim-check.sh"' in text
    assert 'bash "$helper" --pr-number "$PR_NUMBER" --comment-pr' in text


def test_issue_claim_workflow_does_not_execute_pr_controlled_helper() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "bash scripts/issue-claim-check.sh" not in text
    assert "Trusted base helper is unavailable" in text


def test_issue_claim_workflow_permissions_stay_minimal() -> None:
    text = WORKFLOW.read_text(encoding="utf-8")

    assert "issues: read" in text
    assert "pull-requests: write" in text
    assert "contents: read" in text
    assert "contents: write" not in text
    assert "actions: write" not in text
