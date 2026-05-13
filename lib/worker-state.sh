#!/usr/bin/env bash
#
# lib/worker-state.sh — derive Touchstone worker lifecycle state.
#
# Source this file and call:
#   derive_worker_state <worktree_path>
#
# The helper is intentionally read-only: it derives state from filesystem,
# git, gh, and existing review markers without writing cache files.

touchstone_worker_default_ref() {
  local origin_head ref branch default_branch

  origin_head="$(git symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [ -n "$origin_head" ]; then
    ref="${origin_head#refs/remotes/}"
    if git rev-parse --verify --quiet "$ref^{commit}" >/dev/null 2>&1; then
      printf '%s\n' "$ref"
      return 0
    fi
  fi

  default_branch="$(git config --get init.defaultBranch 2>/dev/null || true)"
  for branch in "$default_branch" main master; do
    [ -n "$branch" ] || continue
    if git rev-parse --verify --quiet "origin/$branch^{commit}" >/dev/null 2>&1; then
      printf '%s\n' "origin/$branch"
      return 0
    fi
    if git rev-parse --verify --quiet "$branch^{commit}" >/dev/null 2>&1; then
      printf '%s\n' "$branch"
      return 0
    fi
  done

  printf 'origin/main\n'
}

touchstone_worker_review_marker_key() {
  printf '%s' "$1" | sed 's/[^A-Za-z0-9._-]/_/g'
}

touchstone_worker_has_clean_marker() {
  local branch="$1" common_dir marker
  common_dir="$(git rev-parse --git-common-dir 2>/dev/null || true)"
  [ -n "$common_dir" ] || return 1
  marker="$common_dir/touchstone/reviewer-clean/$(touchstone_worker_review_marker_key "$branch").clean"
  [ -f "$marker" ]
}

touchstone_worker_has_blocked_signal() {
  local branch="$1" common_dir key marker log_file
  common_dir="$(git rev-parse --git-common-dir 2>/dev/null || true)"
  key="$(touchstone_worker_review_marker_key "$branch")"
  if [ -n "$common_dir" ]; then
    for marker in \
      "$common_dir/touchstone/reviewer-blocked/$key.blocked" \
      "$common_dir/touchstone/worker-blocked/$key.blocked"; do
      [ -f "$marker" ] && return 0
    done
  fi

  log_file="${TOUCHSTONE_REVIEW_LOG-$HOME/.touchstone-review-log}"
  [ -n "$log_file" ] && [ -f "$log_file" ] || return 1
  awk -F '\t' -v branch="$branch" '
    $3 == branch && ($5 == "ran" || $5 == "review-blocked") && $6 ~ /blocked/ { found = 1 }
    END { exit found ? 0 : 1 }
  ' "$log_file" 2>/dev/null
}

touchstone_worker_pr_field() {
  local branch="$1" field="$2"
  command -v gh >/dev/null 2>&1 || return 0
  gh pr list --head "$branch" --state all --json number,url,state,mergedAt \
    --jq ".[0].$field // empty" 2>/dev/null || true
}

derive_worker_state() {
  local worktree_path="${1:-}"
  local base branch has_commits has_uncommitted pr_state merged_at

  if [ -z "$worktree_path" ] || [ ! -d "$worktree_path" ]; then
    echo "abandoned"
    return 0
  fi

  (
    cd "$worktree_path" || exit 0

    base="$(touchstone_worker_default_ref)"
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
    [ -n "$branch" ] && [ "$branch" != "HEAD" ] || {
      echo "abandoned"
      exit 0
    }

    has_commits="$(git log "$base..HEAD" --oneline 2>/dev/null | head -1 || true)"
    has_uncommitted="$(git status --porcelain 2>/dev/null || true)"

    if [ -z "$has_commits" ]; then
      echo "spawned"
      exit 0
    fi

    pr_state="$(touchstone_worker_pr_field "$branch" state)"
    merged_at="$(touchstone_worker_pr_field "$branch" mergedAt)"

    if [ "$pr_state" = "MERGED" ] || { [ "$pr_state" = "CLOSED" ] && [ -n "$merged_at" ]; }; then
      echo "cleanup_failed"
      exit 0
    fi

    if [ "$pr_state" = "CLOSED" ]; then
      if touchstone_worker_has_blocked_signal "$branch"; then
        echo "review_blocked"
      else
        echo "abandoned"
      fi
      exit 0
    fi

    if [ "$pr_state" = "OPEN" ]; then
      if touchstone_worker_has_clean_marker "$branch"; then
        echo "reviewing"
      else
        echo "pr_opened"
      fi
      exit 0
    fi

    if [ -n "$has_uncommitted" ]; then
      echo "dirty"
    else
      echo "working"
    fi
  )
}
