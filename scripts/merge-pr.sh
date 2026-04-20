#!/usr/bin/env bash
#
# scripts/merge-pr.sh — squash-merge a PR and clean up.
#
# Usage:
#   bash scripts/merge-pr.sh <pr-number>
#
# What this does:
#   1. Verifies the PR is open and mergeable.
#   2. Runs AI code review as a merge gate.
#   3. Squash-merges and deletes the remote branch.
#   4. Checks out the default branch and pulls the updated state.
#
# Exit codes:
#   0 — merged cleanly
#   1 — merge failed (PR not mergeable, conflicts, etc.)
#   2 — usage / environment error
#
set -euo pipefail

PR_NUMBER="${1:-}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REVIEW_SCRIPT="$SCRIPT_DIR/codex-review.sh"
REVIEWED_HEAD_OID=""

if [ -z "$PR_NUMBER" ] || ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "Usage: bash scripts/merge-pr.sh <pr-number>" >&2
  exit 2
fi

if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: 'gh' is not installed." >&2
  exit 2
fi

# Resolve the default branch.
DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null || echo main)"

truthy() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

run_merge_review() {
  local current_branch default_base_ref local_head pr_head_branch pr_head_oid

  if ! pr_head_branch="$(gh pr view "$PR_NUMBER" --json headRefName --jq '.headRefName' 2>/dev/null)"; then
    echo "ERROR: Failed to resolve PR #$PR_NUMBER head branch." >&2
    exit 1
  fi
  if ! pr_head_oid="$(gh pr view "$PR_NUMBER" --json headRefOid --jq '.headRefOid' 2>/dev/null)"; then
    echo "ERROR: Failed to resolve PR #$PR_NUMBER head commit." >&2
    exit 1
  fi
  if [ -z "$pr_head_branch" ]; then
    echo "ERROR: PR #$PR_NUMBER head branch is empty." >&2
    exit 1
  fi
  if [ -z "$pr_head_oid" ]; then
    echo "ERROR: PR #$PR_NUMBER head commit is empty." >&2
    exit 1
  fi

  REVIEWED_HEAD_OID="$pr_head_oid"

  if truthy "${SKIP_REVIEW:-${SKIP_CODEX_REVIEW:-false}}"; then
    echo "==> Skipping merge review because SKIP_REVIEW is set."
    return 0
  fi

  if [ ! -f "$REVIEW_SCRIPT" ]; then
    echo "==> Review script not found at $REVIEW_SCRIPT — skipping review."
    return 0
  fi

  default_base_ref="origin/$DEFAULT_BRANCH"
  echo "==> Refreshing $default_base_ref for merge review ..."
  if ! git fetch origin "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"; then
    echo "ERROR: Failed to refresh $default_base_ref before merge review." >&2
    exit 1
  fi
  if ! git rev-parse --verify --quiet "$default_base_ref^{commit}" >/dev/null; then
    echo "ERROR: Could not verify $default_base_ref before merge review." >&2
    exit 1
  fi

  if [ -n "$(git status --porcelain)" ]; then
    echo "ERROR: Working tree has uncommitted changes; refusing to run review against an ambiguous tree." >&2
    exit 1
  fi

  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  local_head="$(git rev-parse HEAD)"
  if [ "$current_branch" != "$pr_head_branch" ] || [ "$local_head" != "$pr_head_oid" ]; then
    echo "==> Checking out PR #$PR_NUMBER head ($pr_head_branch) for merge review ..."
    gh pr checkout "$PR_NUMBER" --detach
    local_head="$(git rev-parse HEAD)"
  fi

  if [ "$local_head" != "$pr_head_oid" ]; then
    echo "ERROR: Local review checkout does not match PR #$PR_NUMBER head commit." >&2
    echo "       expected: $pr_head_oid" >&2
    echo "       actual:   $local_head" >&2
    exit 1
  fi

  echo "==> Running merge review ..."
  CODEX_REVIEW_BASE="$default_base_ref" \
    CODEX_REVIEW_FORCE=1 \
    CODEX_REVIEW_MODE=review-only \
    bash "$REVIEW_SCRIPT"
}

# 1. Sanity check the PR exists and is open.
if ! PR_STATE="$(gh pr view "$PR_NUMBER" --json state --jq '.state')"; then
  echo "ERROR: Failed to inspect PR #$PR_NUMBER state with gh." >&2
  exit 1
fi
if [ "$PR_STATE" != "OPEN" ]; then
  echo "ERROR: PR #$PR_NUMBER is not open (state: $PR_STATE)." >&2
  exit 1
fi

# 2. Check mergeability with retries (GitHub's status can lag after a push).
echo "==> Checking merge state for PR #$PR_NUMBER ..."
STATE=""
MERGEABLE=""
for attempt in 1 2 3 4 5; do
  MERGE_STATE="$(gh pr view "$PR_NUMBER" --json mergeStateStatus,mergeable --template '{{.mergeStateStatus}} {{.mergeable}}' 2>/dev/null || echo '')"
  STATE="${MERGE_STATE%% *}"
  MERGEABLE="${MERGE_STATE#* }"
  [ -n "$STATE" ] || STATE="UNKNOWN"
  [ -n "$MERGEABLE" ] || MERGEABLE="UNKNOWN"
  echo "    attempt $attempt: mergeStateStatus=$STATE mergeable=$MERGEABLE"
  if [ "$STATE" = "CLEAN" ] && [ "$MERGEABLE" = "MERGEABLE" ]; then
    break
  fi
  if [ "$STATE" = "DIRTY" ] || [ "$STATE" = "BEHIND" ]; then
    echo "ERROR: PR #$PR_NUMBER is $STATE — has conflicts or is out of date with base." >&2
    echo "       Rebase or resolve conflicts on the PR branch before merging." >&2
    exit 1
  fi
  sleep 3
done

if [ "$STATE" != "CLEAN" ] || [ "$MERGEABLE" != "MERGEABLE" ]; then
  echo "ERROR: PR #$PR_NUMBER is not cleanly mergeable (state=$STATE mergeable=$MERGEABLE)." >&2
  echo "       Inspect manually: gh pr view $PR_NUMBER --web" >&2
  exit 1
fi

# 3. Run AI review as the merge gate.
run_merge_review

# 4. Squash-merge and delete the branch.
echo "==> Squash-merging PR #$PR_NUMBER ..."
if [ -z "$REVIEWED_HEAD_OID" ]; then
  echo "ERROR: Cannot merge PR #$PR_NUMBER because no reviewed head commit was recorded." >&2
  exit 1
fi
gh pr merge "$PR_NUMBER" --squash --delete-branch --match-head-commit "$REVIEWED_HEAD_OID"

# 5. Sync local default branch.
echo "==> Merged. Updating local $DEFAULT_BRANCH ..."
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ]; then
  git checkout "$DEFAULT_BRANCH"
fi
git pull --rebase
echo "==> Done."
