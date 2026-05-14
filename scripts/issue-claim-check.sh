#!/usr/bin/env bash
#
# scripts/issue-claim-check.sh — enforce claim-before-dispatch locally and in CI.
#
# Usage:
#   bash scripts/issue-claim-check.sh --body-file <file> [--author <login>]
#   bash scripts/issue-claim-check.sh --pr-number <number> [--comment-pr]
#
set -euo pipefail

usage() {
  cat >&2 <<'EOF'
Usage:
  bash scripts/issue-claim-check.sh --body-file <file> [--author <login>]
  bash scripts/issue-claim-check.sh --pr-number <number> [--comment-pr]
EOF
}

require_gh() {
  if ! command -v gh >/dev/null 2>&1; then
    echo "ERROR: gh CLI is required for issue claim checks." >&2
    exit 2
  fi
}

resolve_current_repo() {
  if [ -n "${GH_REPO:-}" ]; then
    printf '%s\n' "$GH_REPO"
    return 0
  fi

  gh repo view --json nameWithOwner --jq '.nameWithOwner // empty' 2>/dev/null || true
}

extract_issue_refs() {
  local body_file="$1"
  local refs_file="$2"
  local match normalized issue_number target_repo current_repo
  local closing_keywords="(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)"

  current_repo="$(resolve_current_repo | tr '[:upper:]' '[:lower:]')"

  # Pattern 1: same-repo numeric refs including closes-issue.
  while IFS= read -r match; do
    issue_number="$(printf '%s' "$match" | sed -E 's/.*#([0-9]+).*/\1/')"
    if [ -n "$issue_number" ]; then
      printf '%s\n' "$issue_number" >>"$refs_file"
    fi
  done < <(grep -Eoi "\\b(${closing_keywords}|closes-issue):?[[:space:]]*#[0-9]+\\b" "$body_file" || true)

  # Pattern 2: GitHub closing keywords with optional owner/repo prefix.
  while IFS= read -r match; do
    normalized="$(printf '%s' "$match" | tr '[:upper:]' '[:lower:]')"
    target_repo="$(printf '%s' "$normalized" | sed -nE "s/^${closing_keywords}:?[[:space:]]*([[:alnum:]_.-]+\/[[:alnum:]_.-]+)#[0-9]+$/\\2/p")"
    if [ -n "$target_repo" ] && [ -n "$current_repo" ] && [ "$target_repo" != "$current_repo" ]; then
      echo "==> Skipping cross-repo reference: $match"
      continue
    fi
    issue_number="$(printf '%s' "$match" | sed -E 's/.*#([0-9]+).*/\1/')"
    if [ -n "$issue_number" ]; then
      printf '%s\n' "$issue_number" >>"$refs_file"
    fi
  done < <(grep -Eoi "\\b${closing_keywords}:?[[:space:]]*([[:alnum:]_.-]+/[[:alnum:]_.-]+)?#[0-9]+\\b" "$body_file" || true)
}

format_assignee_label() {
  local assignees="$1"
  if [ -z "$assignees" ]; then
    printf '(none)'
    return 0
  fi
  printf '%s\n' "$assignees" | awk '
    NF {
      if (out == "") {
        out = $0
      } else {
        out = out ", " $0
      }
    }
    END {
      if (out != "") {
        printf "%s", out
      }
    }
  '
}

write_failure_report() {
  local failures_file="$1"
  local pr_author="$2"
  local mode="$3"
  local issue_number assignees

  if [ "$mode" = "markdown" ]; then
    echo "Issue claim check failed"
    echo ""
    echo "This PR references issue(s) with closing keywords, but the PR author (@$pr_author) is not assigned to all open referenced issues:"
    echo ""
    while IFS='|' read -r issue_number assignees; do
      [ -n "$issue_number" ] || continue
      echo "- #$issue_number - current assignees: $assignees"
    done <"$failures_file"
    echo ""
    echo "### Remediation"
    echo "1. Claim each open referenced issue before dispatch:"
    # shellcheck disable=SC2016  # backticks are intentional Markdown.
    echo '   - `bash scripts/claim-issue.sh <issue-number>`'
    echo "2. Keep the closing keyword in this PR body once claimed, then push any update if needed."
    echo ""
    # shellcheck disable=SC2016  # backticks are intentional Markdown.
    echo 'If this is a legitimate exception (drive-by fix, emergency PR), add `[skip-claim-check]` to the PR body as a documented bypass.'
    return 0
  fi

  echo "ERROR: Issue claim check failed." >&2
  echo "This PR references issue(s) with closing keywords, but the PR author (@$pr_author) is not assigned to all open referenced issues:" >&2
  while IFS='|' read -r issue_number assignees; do
    [ -n "$issue_number" ] || continue
    echo "  - #$issue_number - current assignees: $assignees" >&2
  done <"$failures_file"
  echo "" >&2
  echo "Remediation:" >&2
  echo "  bash scripts/claim-issue.sh <issue-number>" >&2
  while IFS='|' read -r issue_number _; do
    [ -n "$issue_number" ] || continue
    echo "  bash scripts/claim-issue.sh $issue_number" >&2
  done <"$failures_file"
  echo "  Or add [skip-claim-check] to the PR body for a documented exception." >&2
}

BODY_FILE=""
PR_NUMBER=""
PR_AUTHOR=""
COMMENT_PR=false
OWNED_BODY_FILE=""

while [ "$#" -gt 0 ]; do
  case "$1" in
    --body-file)
      [ "$#" -ge 2 ] || {
        usage
        exit 2
      }
      BODY_FILE="$2"
      shift 2
      ;;
    --pr-number)
      [ "$#" -ge 2 ] || {
        usage
        exit 2
      }
      PR_NUMBER="$2"
      shift 2
      ;;
    --author)
      [ "$#" -ge 2 ] || {
        usage
        exit 2
      }
      PR_AUTHOR="$2"
      shift 2
      ;;
    --comment-pr)
      COMMENT_PR=true
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument '$1'" >&2
      usage
      exit 2
      ;;
  esac
done

if [ -z "$BODY_FILE" ] && [ -z "$PR_NUMBER" ]; then
  usage
  exit 2
fi
if [ -n "$BODY_FILE" ] && [ ! -f "$BODY_FILE" ]; then
  echo "ERROR: PR body file not found: $BODY_FILE" >&2
  exit 2
fi
if [ "$COMMENT_PR" = true ] && [ -z "$PR_NUMBER" ]; then
  echo "ERROR: --comment-pr requires --pr-number." >&2
  exit 2
fi

require_gh

# shellcheck disable=SC2329  # invoked by EXIT trap.
cleanup() {
  if [ -n "$OWNED_BODY_FILE" ]; then
    rm -f "$OWNED_BODY_FILE"
  fi
}
trap cleanup EXIT

if [ -n "$PR_NUMBER" ]; then
  if [ -z "$BODY_FILE" ]; then
    OWNED_BODY_FILE="$(mktemp -t touchstone-claim-body.XXXXXX)"
    BODY_FILE="$OWNED_BODY_FILE"
    gh pr view "$PR_NUMBER" --json body,author --jq '.body // ""' >"$BODY_FILE"
  fi
fi

if grep -Eqi '\[skip-claim-check\]' "$BODY_FILE"; then
  echo "[skip-claim-check] token found in PR body; bypassing issue claim check."
  exit 0
fi

issue_refs_file="$(mktemp -t touchstone-claim-refs.XXXXXX)"
failures_file="$(mktemp -t touchstone-claim-failures.XXXXXX)"
comment_file=""
trap 'rm -f "$issue_refs_file" "$failures_file" "$comment_file"; cleanup' EXIT

extract_issue_refs "$BODY_FILE" "$issue_refs_file"

if [ ! -s "$issue_refs_file" ]; then
  echo "No closing issue references found in PR body; nothing to enforce."
  exit 0
fi

if [ -n "$PR_NUMBER" ] && [ -z "$PR_AUTHOR" ]; then
  PR_AUTHOR="$(gh pr view "$PR_NUMBER" --json body,author --jq '.author.login // empty')"
fi
if [ -z "$PR_AUTHOR" ]; then
  PR_AUTHOR="$(gh api user --jq '.login' 2>/dev/null || true)"
fi
if [ -z "$PR_AUTHOR" ]; then
  echo "ERROR: could not resolve PR author for issue claim check." >&2
  exit 2
fi

unique_issues="$(sort -u "$issue_refs_file")"
echo "==> Checking issue ownership for PR author @$PR_AUTHOR"

while IFS= read -r issue_number; do
  [ -n "$issue_number" ] || continue
  echo "==> Checking issue #$issue_number"

  issue_state="$(gh issue view "$issue_number" --json state --jq '.state')"
  if [ "$issue_state" = "CLOSED" ]; then
    echo "    issue is closed; skipping"
    continue
  fi

  assignees="$(gh issue view "$issue_number" --json assignees --jq '.assignees | map(.login) | join("\n")')"
  if printf '%s\n' "$assignees" | grep -Fxq "$PR_AUTHOR"; then
    echo "    pass: @$PR_AUTHOR is assigned"
    continue
  fi

  printf '%s|%s\n' "$issue_number" "$(format_assignee_label "$assignees")" >>"$failures_file"
done <<<"$unique_issues"

if [ ! -s "$failures_file" ]; then
  echo "All referenced issues are claimed by the PR author."
  exit 0
fi

write_failure_report "$failures_file" "$PR_AUTHOR" "terminal"

if [ "$COMMENT_PR" = true ]; then
  comment_file="$(mktemp -t touchstone-claim-comment.XXXXXX)"
  write_failure_report "$failures_file" "$PR_AUTHOR" "markdown" >"$comment_file"
  gh pr comment "$PR_NUMBER" --body-file "$comment_file"
  echo "Issue claim check failed; remediation comment posted to PR #$PR_NUMBER."
fi

exit 1
