#!/usr/bin/env bash
#
# scripts/open-pr.sh — push the current branch and open a PR via gh.
#
# Refuses to run on the default branch. Sets upstream on first push.
# Idempotent: if a PR already exists for this branch it just prints the URL.
# Always uses the project's PR template if one exists.
#
# Usage:
#   bash scripts/open-pr.sh                          # title from last commit; base = default branch
#   bash scripts/open-pr.sh --auto-merge             # open + merge-gate review + squash-merge
#   bash scripts/open-pr.sh --auto-merge \
#                            --cleanup-worktree       # auto-merge, then remove this feature worktree
#   bash scripts/open-pr.sh --draft                  # same, opened as draft
#   bash scripts/open-pr.sh --base feat/X            # stacked PR: base this PR on feat/X, not main
#   bash scripts/open-pr.sh "Custom title"           # explicit title
#
# Exit contract (--auto-merge):
#   exit 0 ⇔ `gh pr view <n> --json mergedAt --jq .mergedAt` is non-empty.
#   Any other terminal state exits nonzero AND prints the PR URL with recovery
#   commands as the last lines of output. This prevents the "swarm-agent orphan
#   PR" failure mode where an agent's session ends mid-merge and leaves a
#   reviewed-but-unmerged PR open indefinitely.
#
#   Why local polling instead of `gh pr merge --auto`: native auto-merge fires
#   when GitHub's required-checks gate flips green. Touchstone's review gate is
#   the local Conductor review (run from merge-pr.sh), not a GitHub Action, so
#   a queued native auto-merge would never fire. Keeping the merge in-band lets
#   us positively confirm merge before reporting success.
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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_SYNC_GUARD="$SCRIPT_DIR/../lib/script-sync-guard.sh"
if [ -f "$SCRIPT_SYNC_GUARD" ]; then
  # shellcheck source=../lib/script-sync-guard.sh
  source "$SCRIPT_SYNC_GUARD"
  touchstone_script_sync_guard "$0" "$@"
fi
PREFLIGHT_SCRIPT="$SCRIPT_DIR/../lib/preflight.sh"
REVIEW_COMMENT_SCRIPT="$SCRIPT_DIR/../lib/review-comment.sh"
ISSUE_CLAIM_CHECK_SCRIPT="$SCRIPT_DIR/issue-claim-check.sh"
if [ -f "$SCRIPT_DIR/../lib/events.sh" ]; then
  # shellcheck source=../lib/events.sh
  source "$SCRIPT_DIR/../lib/events.sh"
else
  touchstone_emit_event() { :; }
fi
if [ -f "$PREFLIGHT_SCRIPT" ]; then
  # shellcheck source=../lib/preflight.sh
  source "$PREFLIGHT_SCRIPT"
fi
if [ -f "$REVIEW_COMMENT_SCRIPT" ]; then
  # shellcheck source=../lib/review-comment.sh
  source "$REVIEW_COMMENT_SCRIPT"
fi

# orphan_warning is set to a PR URL once we know one — any nonzero exit after
# that point prints recovery instructions as the script's last output, so the
# user (or future agent) can see exactly which PR is stuck.
ORPHAN_PR_URL=""
ORPHAN_PR_NUMBER=""
BODY_FILE=""
ADVISORY_AT_PR_OPEN=false
PREFLIGHT_REQUIRED=true

on_exit() {
  local rc="$?"
  # Always clean up the temp body file, no matter how we exit.
  if [ -n "$BODY_FILE" ] && [ -f "$BODY_FILE" ]; then
    rm -f "$BODY_FILE"
  fi
  print_orphan_warning "$rc"
  return "$rc"
}

print_orphan_warning() {
  local rc="$1"
  if [ "$rc" -eq 0 ]; then
    return 0
  fi
  if [ -z "$ORPHAN_PR_URL" ]; then
    return 0
  fi
  # Re-check merge state on exit — if the PR actually merged in flight (e.g.
  # we ran past the merge step but tripped on a follow-up like the local pull)
  # then this isn't an orphan. The exit code stays nonzero; we just suppress
  # the misleading orphan banner.
  if [ -n "$ORPHAN_PR_NUMBER" ] \
    && command -v gh >/dev/null 2>&1 \
    && [ -n "$(gh pr view "$ORPHAN_PR_NUMBER" --json mergedAt --jq '.mergedAt // empty' 2>/dev/null || true)" ]; then
    return 0
  fi
  touchstone_emit_event failed phase=open-pr reason=orphan-risk pr_number="$ORPHAN_PR_NUMBER"
  {
    echo ""
    echo "==> ORPHAN RISK: PR opened but not merged. Resolve manually:"
    echo "==>   $ORPHAN_PR_URL"
    if [ -n "$ORPHAN_PR_NUMBER" ]; then
      echo "==>   gh pr merge $ORPHAN_PR_NUMBER --squash --delete-branch    (if review passed)"
      echo "==>   gh pr close $ORPHAN_PR_NUMBER                              (if abandoning)"
    fi
  } >&2
}

# Verify the PR actually merged. Returns 0 if mergedAt is non-empty, 1 otherwise.
# Used as the post-merge sanity check that turns the script's exit contract from
# "merge-pr.sh exited 0" (proxy) into "GitHub says it's merged" (truth).
verify_pr_merged() {
  local pr_number="$1"
  local merged_at
  merged_at="$(gh pr view "$pr_number" --json mergedAt --jq '.mergedAt // empty' 2>/dev/null || echo "")"
  if [ -n "$merged_at" ]; then
    echo "==> Verified: PR #$pr_number merged at $merged_at"
    return 0
  fi
  return 1
}

run_issue_claim_preflight() {
  local label="$1"
  shift

  if [ ! -f "$ISSUE_CLAIM_CHECK_SCRIPT" ]; then
    echo "ERROR: issue-claim-check.sh not found at $ISSUE_CLAIM_CHECK_SCRIPT." >&2
    echo "       Run touchstone update so scripts/open-pr.sh and its helpers stay in sync." >&2
    exit 2
  fi

  echo "==> Running local issue claim preflight ($label) ..."
  bash "$ISSUE_CLAIM_CHECK_SCRIPT" "$@"
}

find_pr_body_protocol_checker() {
  local rel

  for rel in scripts/check-api-boundary-protocol.py scripts/check-pr-body-protocol.py; do
    if [ -f "$REPO_ROOT/$rel" ]; then
      printf '%s\n' "$REPO_ROOT/$rel"
      return 0
    fi
  done

  return 1
}

run_pr_body_protocol_preflight() {
  local label="$1" pr_number="$2"
  local checker body checker_rel rc

  checker="$(find_pr_body_protocol_checker)" || return 0
  checker_rel="${checker#"$REPO_ROOT/"}"

  if ! body="$(gh pr view "$pr_number" --json body --jq '.body // ""' 2>/dev/null)"; then
    echo "ERROR: failed to read PR #$pr_number body for protocol preflight." >&2
    exit 1
  fi

  echo "==> Running PR body protocol preflight ($label): $checker_rel"
  rc=0
  if [ -x "$checker" ]; then
    API_BOUNDARY_PR_BODY="$body" PR_BODY="$body" "$checker" || rc=$?
  elif [ "${checker##*.}" = "py" ]; then
    if ! command -v python3 >/dev/null 2>&1; then
      echo "ERROR: $checker_rel requires python3, but python3 was not found." >&2
      exit 1
    fi
    API_BOUNDARY_PR_BODY="$body" PR_BODY="$body" python3 "$checker" || rc=$?
  else
    API_BOUNDARY_PR_BODY="$body" PR_BODY="$body" bash "$checker" || rc=$?
  fi

  if [ "$rc" -ne 0 ]; then
    echo "ERROR: PR body protocol preflight failed for PR #$pr_number." >&2
    echo "       Edit the PR body, then rerun: bash scripts/open-pr.sh --auto-merge" >&2
    exit "$rc"
  fi
}

run_deterministic_preflight_for_advisory() {
  local base_ref="$1"
  local repo_root cache_key_short

  repo_root="$(git rev-parse --show-toplevel)"

  if declare -F touchstone_preflight_cache_prepare >/dev/null 2>&1 \
    && touchstone_preflight_cache_prepare "$base_ref" \
    && touchstone_preflight_cache_hit; then
    cache_key_short="$(touchstone_preflight_cache_short_key)"
    echo "==> Deterministic preflight clean (cached=true, key=$cache_key_short; before advisory review, diff vs $base_ref)."
    return 0
  fi

  echo "==> Running deterministic preflight before advisory review ..."
  if touchstone_preflight_main_sanitized --diff "$base_ref" "$repo_root"; then
    if declare -F touchstone_preflight_write_clean_cache >/dev/null 2>&1; then
      touchstone_preflight_write_clean_cache
    fi
    if [ -n "${TOUCHSTONE_PREFLIGHT_CACHE_KEY:-}" ] \
      && declare -F touchstone_preflight_cache_short_key >/dev/null 2>&1; then
      cache_key_short="$(touchstone_preflight_cache_short_key)"
      echo "==> Deterministic preflight clean (cached=false, key=$cache_key_short)."
    else
      echo "==> Deterministic preflight clean (cached=false)."
    fi
    return 0
  fi

  return 1
}

truthy() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

normalize_bool() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | on) printf 'true' ;;
    false | 0 | no | off) printf 'false' ;;
    *) printf '%s' "$1" ;;
  esac
}

load_open_pr_review_config() {
  local config_file
  local repo_root
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || true)"
  [ -n "$repo_root" ] || return 0
  if [ -f "$repo_root/.touchstone-review.toml" ]; then
    config_file="$repo_root/.touchstone-review.toml"
  else
    config_file="$repo_root/.codex-review.toml"
  fi
  [ -f "$config_file" ] || return 0
  [ -f "$SCRIPT_DIR/../lib/toml.sh" ] || return 0

  # shellcheck source=../lib/toml.sh
  source "$SCRIPT_DIR/../lib/toml.sh"

  open_pr_toml_callback() {
    local section="$1"
    local key="$2"
    local value="$3"

    if [ "$section" = "review" ] && [ "$key" = "advisory_at_pr_open" ]; then
      ADVISORY_AT_PR_OPEN="$(normalize_bool "$value")"
    elif [ "$section" = "review" ] && [ "$key" = "preflight_required" ]; then
      PREFLIGHT_REQUIRED="$(normalize_bool "$value")"
    fi
  }

  toml_parse "$config_file" open_pr_toml_callback
}

run_advisory_review_at_pr_open() {
  local pr_number="$1"
  local base_branch="$2"
  local review_script summary_file output_file review_rc summary_json comment
  local advisory_preflight_passed=false

  if ! truthy "$ADVISORY_AT_PR_OPEN"; then
    echo "==> Advisory review at PR open disabled; merge-gate review still runs during auto-merge."
    return 0
  fi

  if ! declare -F post_pr_review_comment >/dev/null 2>&1 \
    || ! declare -F format_clean_review_comment >/dev/null 2>&1 \
    || ! declare -F format_advisory_findings_comment >/dev/null 2>&1; then
    echo "==> Review comment helper not found at $REVIEW_COMMENT_SCRIPT; skipping advisory review."
    return 0
  fi

  if truthy "$PREFLIGHT_REQUIRED" && ! truthy "${TOUCHSTONE_NO_PREFLIGHT:-false}"; then
    if declare -F touchstone_preflight_main >/dev/null 2>&1; then
      if ! run_deterministic_preflight_for_advisory "origin/$base_branch"; then
        echo "WARNING: preflight failed; skipping non-blocking advisory review to avoid spending provider tokens." >&2
        return 0
      fi
      advisory_preflight_passed=true
    else
      echo "==> Preflight helper not found at $PREFLIGHT_SCRIPT — skipping preflight."
    fi
  else
    echo "==> Preflight disabled before advisory review."
  fi

  review_script="$SCRIPT_DIR/conductor-review.sh"
  if [ ! -f "$review_script" ]; then
    review_script="$SCRIPT_DIR/codex-review.sh"
    if [ ! -f "$review_script" ]; then
      echo "WARNING: conductor review script not found at $SCRIPT_DIR/conductor-review.sh or $SCRIPT_DIR/codex-review.sh; skipping advisory review." >&2
      return 0
    fi
  fi

  summary_file="$(git rev-parse --git-path "touchstone/review-summary-pr-${pr_number}-advisory.json" 2>/dev/null || echo "")"
  output_file="$(mktemp -t touchstone-advisory-review.XXXXXX.txt)"
  if [ -n "$summary_file" ]; then
    mkdir -p "$(dirname "$summary_file")" 2>/dev/null || true
    rm -f "$summary_file" 2>/dev/null || true
  fi

  echo "==> Running advisory conductor review for PR #$pr_number ..."
  review_rc=0
  CODEX_REVIEW_BASE="origin/$base_branch" \
    CODEX_REVIEW_BRANCH_NAME="$CURRENT_BRANCH" \
    CODEX_REVIEW_FORCE=1 \
    CODEX_REVIEW_MODE=review-only \
    TOUCHSTONE_PREFLIGHT_ALREADY_RAN="$advisory_preflight_passed" \
    CODEX_REVIEW_SUMMARY_FILE="$summary_file" \
    bash "$review_script" >"$output_file" 2>&1 || review_rc=$?

  summary_json="$(tail -n 1 "$summary_file" 2>/dev/null || true)"
  if [ -z "$summary_json" ]; then
    echo "WARNING: advisory review summary missing; skipping advisory PR comment." >&2
    rm -f "$output_file"
    return 0
  fi

  if [ "$review_rc" -eq 0 ]; then
    comment="$(format_clean_review_comment "$summary_json")"
  else
    comment="$(format_advisory_findings_comment "$summary_json" "$(cat "$output_file" 2>/dev/null || true)")"
  fi

  if post_pr_review_comment "$pr_number" "$comment"; then
    echo "==> Posted advisory review PR comment."
  else
    echo "WARNING: failed to post advisory review PR comment for PR #$pr_number." >&2
  fi
  rm -f "$output_file"
  return 0
}

# Locate the worktree that has the default branch checked out, by parsing
# `git worktree list --porcelain`. Returns empty when no sibling worktree
# owns the default branch (single-checkout case).
default_branch_worktree_path() {
  local default_branch="$1"
  local current_path=""
  awk -v target="refs/heads/$default_branch" '
    /^worktree / { path = substr($0, length("worktree ") + 1) }
    /^branch /   { if ($2 == target) { print path; exit } }
  ' < <(git worktree list --porcelain)
}

# Remove the current feature worktree from the default-branch worktree.
# Called after a successful auto-merge when --cleanup-worktree is set.
# The cleanup is a best-effort convenience: failures are reported but do
# not fail the script — the merge already happened, and a leftover
# worktree is recoverable via `scripts/cleanup-worktrees.sh`.
cleanup_feature_worktree() {
  local current_path default_path
  current_path="$(git rev-parse --show-toplevel)"
  default_path="$(default_branch_worktree_path "$DEFAULT_BRANCH")"

  if [ -z "$default_path" ]; then
    echo "==> --cleanup-worktree: no sibling worktree owns $DEFAULT_BRANCH; nothing to remove."
    return 0
  fi
  if [ "$current_path" = "$default_path" ]; then
    echo "==> --cleanup-worktree: already in $DEFAULT_BRANCH worktree; nothing to remove."
    return 0
  fi

  echo "==> Removing feature worktree $current_path (from $default_path) ..."
  touchstone_emit_event cleanup_started worktree_path="$current_path"
  if (cd "$default_path" && git worktree remove "$current_path"); then
    echo "==> Worktree removed."
    touchstone_emit_event cleanup_done worktree_path="$current_path" result=removed
  else
    echo "WARNING: git worktree remove failed for $current_path." >&2
    echo "         Run 'bash scripts/cleanup-worktrees.sh' from $default_path to inspect and clean up." >&2
    touchstone_emit_event cleanup_done worktree_path="$current_path" result=failed
  fi
}

find_base_merge_commit() {
  local base_branch="$1"
  local ref
  for ref in "origin/$base_branch" "$base_branch"; do
    if git rev-parse --verify "$ref^{commit}" >/dev/null 2>&1; then
      git merge-base HEAD "$ref"
      return 0
    fi
  done
  return 1
}

find_issue_closing_refs() {
  local base_branch="$1"
  local merge_base
  if ! merge_base="$(find_base_merge_commit "$base_branch")"; then
    echo "WARNING: could not find merge-base for $base_branch; skipping linked-issue detection" >&2
    return 0
  fi

  # Invariant: only commits unique to this PR branch are scanned; base-branch
  # history must not contribute stale issue references to new PR bodies.
  git log "$merge_base..HEAD" --format='%b' | awk '
    {
      line = tolower($0)
      should_scan = 0
      if (line ~ /^[[:space:]]*(closes-issue|closes|fixes|resolves):[[:space:]]*/) {
        should_scan = 1
      }
      if (line ~ /(^|[^[:alnum:]_-])(close|closes|closed|fix|fixes|fixed|resolve|resolves|resolved)[[:space:]]+#[0-9]+/) {
        should_scan = 1
      }
      if (should_scan) {
        rest = line
        while (match(rest, /#[0-9]+/)) {
          issue = substr(rest, RSTART + 1, RLENGTH - 1)
          if (!seen[issue]++) {
            print issue
          }
          rest = substr(rest, RSTART + RLENGTH)
        }
      }
    }
  '
}

REPO_ROOT="$(git rev-parse --show-toplevel)"
TEMPLATE_PATH="$REPO_ROOT/.github/pull_request_template.md"
load_open_pr_review_config

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
UNTRACKED="$(git -C "$REPO_ROOT" ls-files --others --exclude-standard)"
if ! git -C "$REPO_ROOT" diff --quiet || ! git -C "$REPO_ROOT" diff --cached --quiet || [ -n "$UNTRACKED" ]; then
  echo "WARNING: working tree has uncommitted changes — they will NOT be included in this PR." >&2
  if [ -n "$UNTRACKED" ]; then
    echo "         Untracked files detected:" >&2
    while IFS= read -r untracked_file; do
      printf '           %s\n' "$untracked_file" >&2
    done <<<"$UNTRACKED"
  fi
  echo "         Commit them first if they should be part of the PR." >&2
  read -r -p "         Continue anyway? [y/N] " answer
  case "$answer" in
    y | Y | yes | YES) ;;
    *)
      echo "Aborted."
      exit 1
      ;;
  esac
fi

# Parse flags early (needed before the existing-PR check).
DRAFT_FLAG=""
AUTO_MERGE=false
CLEANUP_WORKTREE=false
BASE_OVERRIDE=""
POSITIONAL=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --draft)
      DRAFT_FLAG="--draft"
      shift
      ;;
    --auto-merge)
      AUTO_MERGE=true
      shift
      ;;
    --cleanup-worktree)
      CLEANUP_WORKTREE=true
      shift
      ;;
    --base)
      if [ "$#" -lt 2 ]; then
        echo "ERROR: --base requires a branch name." >&2
        exit 1
      fi
      BASE_OVERRIDE="$2"
      shift 2
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done
set -- "${POSITIONAL[@]+"${POSITIONAL[@]}"}"

# --cleanup-worktree is only meaningful with --auto-merge: without a merge
# there is nothing to clean up to. Reject the combination loudly so the user
# notices instead of getting silent no-op behavior.
if [ "$CLEANUP_WORKTREE" = true ] && [ "$AUTO_MERGE" != true ]; then
  echo "ERROR: --cleanup-worktree requires --auto-merge (cleanup runs only after a successful merge)." >&2
  exit 1
fi

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

# Push. The "do I already have an upstream?" check is name-aware: a fresh
# `git checkout -b <branch> origin/main` sets upstream to `origin/main`,
# which makes `git push` (without `-u`) fail with "upstream does not match
# the name of your current branch." Treat any upstream that doesn't point
# at `origin/<current-branch>` the same as "no upstream yet" and rewrite
# it on first push, so the workflow works regardless of how the branch
# was created.
EXISTING_UPSTREAM="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
EXPECTED_UPSTREAM="origin/$CURRENT_BRANCH"
if [ -n "$EXISTING_UPSTREAM" ] && [ "$EXISTING_UPSTREAM" = "$EXPECTED_UPSTREAM" ]; then
  echo "==> Pushing $CURRENT_BRANCH ..."
  git push
else
  if [ -n "$EXISTING_UPSTREAM" ] && [ "$EXISTING_UPSTREAM" != "$EXPECTED_UPSTREAM" ]; then
    echo "==> Existing upstream '$EXISTING_UPSTREAM' does not match '$EXPECTED_UPSTREAM'; resetting on first push." >&2
  else
    echo "==> Pushing $CURRENT_BRANCH (setting upstream) ..."
  fi
  git push -u origin "$CURRENT_BRANCH"
fi

# Install the cleanup/orphan-warning trap now — every later exit path may
# already have a PR URL we need to surface to the user, and the trap also
# handles temp-file cleanup once BODY_FILE is set further down.
trap on_exit EXIT

# If a PR already exists for this branch, just print the URL (and auto-merge if requested).
EXISTING_PR_URL="$(gh pr list --head "$CURRENT_BRANCH" --author "@me" --state open --json url --jq '.[0].url // empty' 2>/dev/null || echo "")"
if [ -n "$EXISTING_PR_URL" ]; then
  echo "==> PR already open for $CURRENT_BRANCH: $EXISTING_PR_URL"
  if [ "$AUTO_MERGE" = true ]; then
    PR_NUMBER="$(basename "$EXISTING_PR_URL")"
    ORPHAN_PR_URL="$EXISTING_PR_URL"
    ORPHAN_PR_NUMBER="$PR_NUMBER"
    run_issue_claim_preflight "existing PR #$PR_NUMBER" --pr-number "$PR_NUMBER"
    run_pr_body_protocol_preflight "existing PR #$PR_NUMBER" "$PR_NUMBER"
    MERGE_SCRIPT="$SCRIPT_DIR/merge-pr.sh"
    if [ ! -f "$MERGE_SCRIPT" ]; then
      echo "ERROR: merge-pr.sh not found at $MERGE_SCRIPT — cannot auto-merge." >&2
      exit 1
    fi
    echo ""
    echo "==> Auto-merging PR #$PR_NUMBER ..."
    # Don't exec — we need to verify mergedAt after merge-pr.sh returns.
    if ! bash "$MERGE_SCRIPT" "$PR_NUMBER"; then
      echo "ERROR: merge-pr.sh failed for PR #$PR_NUMBER." >&2
      exit 1
    fi
    if ! verify_pr_merged "$PR_NUMBER"; then
      echo "ERROR: merge-pr.sh exited 0 but PR #$PR_NUMBER is not merged on GitHub." >&2
      exit 1
    fi
    if [ "$CLEANUP_WORKTREE" = true ]; then
      cleanup_feature_worktree
    fi
    exit 0
  fi
  exit 0
fi

if [ "$#" -gt 0 ]; then
  TITLE="$1"
else
  TITLE="$(git log -1 --format=%s)"
fi

COMMIT_BODY="$(git log -1 --format=%b)"
LINKED_ISSUES="$(find_issue_closing_refs "$BASE_BRANCH")"

# ---------------------------------------------------------------------------
# Sentinel-cycle PR body: when the current branch was authored by a sentinel
# agent, pull the PR body from the cycle artifact's anchored region instead
# of from commit messages.  Falls back to commit-message behavior silently if
# no anchors are found or the run file is missing.
# ---------------------------------------------------------------------------

# Returns 0 (truthy) when .sentinel/runs/ contains at least one .md artifact.
is_sentinel_authored_branch() {
  [ -n "$(find .sentinel/runs -maxdepth 1 -name "*.md" 2>/dev/null | head -1)" ]
}

# Prints the path of the most-recently-modified sentinel run artifact.
find_latest_sentinel_run() {
  # ls -t is the simplest portable mtime sort; filenames here are controlled.
  # shellcheck disable=SC2012
  ls -t .sentinel/runs/*.md 2>/dev/null | head -1
}

# Reads the schema-version from the YAML frontmatter of a run artifact.
get_schema_version() {
  local run_file="$1"
  awk '/^---$/{f=1-f; next} f && /^schema-version:/{print $2; exit}' "$run_file"
}

# Extracts lines between <!-- pr-body-start --> and <!-- pr-body-end -->.
extract_pr_body_from_run() {
  local run_file="$1"
  awk '/<!-- pr-body-start -->/{flag=1; next} /<!-- pr-body-end -->/{flag=0} flag' "$run_file"
}

SENTINEL_BODY=""
if is_sentinel_authored_branch; then
  SENTINEL_RUN="$(find_latest_sentinel_run)"
  if [ -n "$SENTINEL_RUN" ]; then
    SCHEMA_VER="$(get_schema_version "$SENTINEL_RUN")"
    if [ -n "$SCHEMA_VER" ]; then
      major="${SCHEMA_VER%%.*}"
      if [ "$major" -ge 2 ] 2>/dev/null; then
        echo "WARNING: sentinel run schema-version $SCHEMA_VER not recognized; attempting 1.x extraction" >&2
      fi
    fi
    SENTINEL_BODY="$(extract_pr_body_from_run "$SENTINEL_RUN")"
    if [ -z "$SENTINEL_BODY" ]; then
      echo "WARNING: sentinel cycle artifact found but PR-body anchors are empty — falling back to commit-message body" >&2
    fi
  fi
fi

# Build body from commit body + PR template (if present). The unified EXIT
# trap installed above (`on_exit`) will rm the file regardless of how we exit.
BODY_FILE="$(mktemp -t touchstone-pr-body.XXXXXX.md)"

{
  if [ -n "$LINKED_ISSUES" ]; then
    printf '## Linked Issues\n\n'
    while IFS= read -r issue_number; do
      [ -n "$issue_number" ] || continue
      printf 'Closes #%s\n' "$issue_number"
    done <<<"$LINKED_ISSUES"
    printf '\n'
  fi
  if [ -n "$SENTINEL_BODY" ]; then
    printf '%s\n' "$SENTINEL_BODY"
  else
    if [ -n "$COMMIT_BODY" ]; then
      printf '%s\n\n---\n\n' "$COMMIT_BODY"
    fi
    if [ -f "$TEMPLATE_PATH" ]; then
      cat "$TEMPLATE_PATH"
    fi
  fi
} >"$BODY_FILE"

run_issue_claim_preflight "new PR body" --body-file "$BODY_FILE"

echo "==> Opening PR against $BASE_BRANCH ..."
if [ -n "$DRAFT_FLAG" ]; then
  PR_URL="$(gh pr create --base "$BASE_BRANCH" --title "$TITLE" --body-file "$BODY_FILE" --draft)"
else
  PR_URL="$(gh pr create --base "$BASE_BRANCH" --title "$TITLE" --body-file "$BODY_FILE")"
fi

echo "$PR_URL"

# Capture the PR for the orphan-warning trap — anything that exits nonzero
# from here on is a stuck-PR risk.
ORPHAN_PR_URL="$PR_URL"
ORPHAN_PR_NUMBER="$(basename "$PR_URL")"
HEAD_SHA="$(git rev-parse HEAD)"
touchstone_emit_event pr_opened \
  pr_url="$PR_URL" \
  pr_number="$ORPHAN_PR_NUMBER" \
  branch="$CURRENT_BRANCH" \
  base_branch="$BASE_BRANCH" \
  head_sha="$HEAD_SHA"

run_pr_body_protocol_preflight "new PR #$ORPHAN_PR_NUMBER" "$ORPHAN_PR_NUMBER"
run_advisory_review_at_pr_open "$ORPHAN_PR_NUMBER" "$BASE_BRANCH"

if [ -n "$DRAFT_FLAG" ]; then
  echo "    Opened as draft. Mark ready on github.com when ready to merge."
  if [ "$AUTO_MERGE" = true ]; then
    # --auto-merge + --draft is a contradiction (drafts can't merge). Don't
    # claim success silently — the user explicitly asked for a merge.
    echo "WARNING: --auto-merge ignored because --draft was passed; PR opened as draft only." >&2
  fi
  # Draft path: PR is intentionally open and not merged. That's not an orphan.
  ORPHAN_PR_URL=""
  ORPHAN_PR_NUMBER=""
  exit 0
fi

# Auto-merge: extract PR number and run merge-pr.sh, then positively verify
# the PR actually reached MERGED state on GitHub before claiming success.
if [ "$AUTO_MERGE" = true ]; then
  PR_NUMBER="$(basename "$PR_URL")"
  MERGE_SCRIPT="$SCRIPT_DIR/merge-pr.sh"
  if [ ! -f "$MERGE_SCRIPT" ]; then
    echo "ERROR: merge-pr.sh not found at $MERGE_SCRIPT — cannot auto-merge." >&2
    exit 1
  fi
  echo ""
  echo "==> Auto-merging PR #$PR_NUMBER ..."
  # Don't exec — we need to verify mergedAt after merge-pr.sh returns. The
  # earlier `exec bash "$MERGE_SCRIPT"` form propagated merge-pr.sh's exit
  # code but never positively confirmed merge happened, so any silent failure
  # post-review (network blip on `gh pr merge`, etc.) could end with exit 0
  # and a still-open PR. The new flow always asks GitHub.
  if ! bash "$MERGE_SCRIPT" "$PR_NUMBER"; then
    echo "ERROR: merge-pr.sh failed for PR #$PR_NUMBER." >&2
    exit 1
  fi
  if ! verify_pr_merged "$PR_NUMBER"; then
    echo "ERROR: merge-pr.sh exited 0 but PR #$PR_NUMBER is not merged on GitHub." >&2
    exit 1
  fi

  if [ "$CLEANUP_WORKTREE" = true ]; then
    cleanup_feature_worktree
  fi
fi

# Reached the natural end with no failures — clear the orphan markers so the
# EXIT trap stays quiet on a clean exit 0.
ORPHAN_PR_URL=""
ORPHAN_PR_NUMBER=""
