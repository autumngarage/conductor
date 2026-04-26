#!/usr/bin/env bash
#
# scripts/open-pr.sh — push the current branch and open a PR via gh.
#
# Refuses to run on the default branch. Sets upstream on first push.
# Idempotent: if a PR already exists for this branch it just prints the URL.
# Always uses the project's PR template if one exists.
#
# Usage:
#   bash scripts/open-pr.sh                # title from last commit; base = default branch
#   bash scripts/open-pr.sh --auto-merge   # open + Codex review + squash-merge
#   bash scripts/open-pr.sh --draft        # same, opened as draft
#   bash scripts/open-pr.sh --base feat/X  # stacked PR: base this PR on feat/X, not main
#   bash scripts/open-pr.sh "Custom title" # explicit title
#
# ⚠ Stacked PRs — read this before using --base:
#   Stacking a PR on another PR's branch is useful when work naturally
#   splits into a chain (parent PR ships primitive, child PR ships the
#   consumer that depends on it). GitHub does NOT auto-rebase the child
#   onto main when the parent squash-merges; it closes the child's branch
#   instead. So stacked PRs work well with a merge commit or rebase merge,
#   but the `--auto-merge` default (squash) will orphan the child.
#
#   For simpler review, prefer bundling related work into one PR over
#   stacks when you can. See principles/git-workflow.md.
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
TEMPLATE_PATH="$REPO_ROOT/.github/pull_request_template.md"

# Fail fast if gh is missing or unauthenticated.
if ! command -v gh >/dev/null 2>&1; then
  echo "ERROR: 'gh' (GitHub CLI) is not installed. Install it before opening PRs." >&2
  exit 1
fi
if ! DEFAULT_BRANCH="$(gh repo view --json defaultBranchRef --jq '.defaultBranchRef.name' 2>/dev/null)"; then
  echo "ERROR: Failed to resolve default branch via 'gh'. Is gh authenticated?" >&2
  echo "       Run: gh auth status" >&2
  exit 1
fi

CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"

if [ "$CURRENT_BRANCH" = "$DEFAULT_BRANCH" ] || [ "$CURRENT_BRANCH" = "master" ]; then
  echo "ERROR: You are on '$CURRENT_BRANCH'. Code changes must go through a feature branch + PR." >&2
  echo "  git checkout -b feat/short-description   # or fix/, chore/, refactor/, docs/" >&2
  exit 1
fi

# Warn on uncommitted changes.
UNTRACKED="$(git ls-files --others --exclude-standard)"
if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$UNTRACKED" ]; then
  echo "WARNING: working tree has uncommitted changes — they will NOT be included in this PR." >&2
  if [ -n "$UNTRACKED" ]; then
    echo "         Untracked files detected:" >&2
    while IFS= read -r untracked_file; do
      printf '           %s\n' "$untracked_file" >&2
    done <<< "$UNTRACKED"
  fi
  echo "         Commit them first if they should be part of the PR." >&2
  read -r -p "         Continue anyway? [y/N] " answer
  case "$answer" in
    y|Y|yes|YES) ;;
    *) echo "Aborted."; exit 1 ;;
  esac
fi

# Parse flags early (needed before the existing-PR check).
DRAFT_FLAG=""
AUTO_MERGE=false
BASE_OVERRIDE=""
POSITIONAL=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --draft) DRAFT_FLAG="--draft"; shift ;;
    --auto-merge) AUTO_MERGE=true; shift ;;
    --base)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --base requires a branch name." >&2
        exit 1
      fi
      BASE_OVERRIDE="$2"
      shift 2
      ;;
    *) POSITIONAL+=("$1"); shift ;;
  esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"

# Resolve the actual base branch: --base overrides the repo default.
BASE_BRANCH="${BASE_OVERRIDE:-$DEFAULT_BRANCH}"
if [ "$BASE_BRANCH" = "$CURRENT_BRANCH" ]; then
  echo "ERROR: --base $BASE_BRANCH cannot equal the current branch." >&2
  exit 1
fi

# Warn when stacking + auto-merge combine — the user is likely about to
# orphan their stack. --auto-merge squashes the parent, which closes (not
# rebases) stacked children.
if [ -n "$BASE_OVERRIDE" ] && [ "$AUTO_MERGE" = true ]; then
  echo "WARNING: --base $BASE_OVERRIDE with --auto-merge stacks this PR on another branch" >&2
  echo "         AND will squash-merge it, which orphans any later stacked children." >&2
  echo "         Either drop --auto-merge (open stack, merge manually in order)" >&2
  echo "         or drop --base (bundle into one PR on $DEFAULT_BRANCH)." >&2
fi

# Push (set upstream on first push, plain push afterwards).
if git rev-parse --abbrev-ref --symbolic-full-name '@{u}' >/dev/null 2>&1; then
  echo "==> Pushing $CURRENT_BRANCH ..."
  git push
else
  echo "==> Pushing $CURRENT_BRANCH (setting upstream) ..."
  git push -u origin "$CURRENT_BRANCH"
fi

# If a PR already exists for this branch, just print the URL (and auto-merge if requested).
EXISTING_PR_URL="$(gh pr list --head "$CURRENT_BRANCH" --author "@me" --state open --json url --jq '.[0].url // empty' 2>/dev/null || echo "")"
if [ -n "$EXISTING_PR_URL" ]; then
  echo "==> PR already open for $CURRENT_BRANCH: $EXISTING_PR_URL"
  if [ "$AUTO_MERGE" = true ]; then
    PR_NUMBER="$(basename "$EXISTING_PR_URL")"
    MERGE_SCRIPT="$(dirname "$0")/merge-pr.sh"
    if [ -f "$MERGE_SCRIPT" ]; then
      echo ""
      echo "==> Auto-merging PR #$PR_NUMBER ..."
      exec bash "$MERGE_SCRIPT" "$PR_NUMBER"
    fi
  fi
  exit 0
fi

if [ "$#" -gt 0 ]; then
  TITLE="$1"
else
  TITLE="$(git log -1 --format=%s)"
fi

COMMIT_BODY="$(git log -1 --format=%b)"

# Build body from commit body + PR template (if present).
BODY_FILE="$(mktemp -t touchstone-pr-body.XXXXXX.md)"
trap 'rm -f "$BODY_FILE"' EXIT

{
  if [ -n "$COMMIT_BODY" ]; then
    printf '%s\n\n---\n\n' "$COMMIT_BODY"
  fi
  if [ -f "$TEMPLATE_PATH" ]; then
    cat "$TEMPLATE_PATH"
  fi
} > "$BODY_FILE"

echo "==> Opening PR against $BASE_BRANCH ..."
if [ -n "$DRAFT_FLAG" ]; then
  PR_URL="$(gh pr create --base "$BASE_BRANCH" --title "$TITLE" --body-file "$BODY_FILE" --draft)"
else
  PR_URL="$(gh pr create --base "$BASE_BRANCH" --title "$TITLE" --body-file "$BODY_FILE")"
fi

echo "$PR_URL"

if [ -n "$DRAFT_FLAG" ]; then
  echo "    Opened as draft. Mark ready on github.com when ready to merge."
fi

# Auto-merge: extract PR number and run merge-pr.sh.
if [ "$AUTO_MERGE" = true ] && [ -z "$DRAFT_FLAG" ]; then
  PR_NUMBER="$(basename "$PR_URL")"
  MERGE_SCRIPT="$(dirname "$0")/merge-pr.sh"
  if [ -f "$MERGE_SCRIPT" ]; then
    echo ""
    echo "==> Auto-merging PR #$PR_NUMBER ..."
    exec bash "$MERGE_SCRIPT" "$PR_NUMBER"
  else
    echo "WARNING: merge-pr.sh not found at $MERGE_SCRIPT — skipping auto-merge." >&2
  fi
fi
