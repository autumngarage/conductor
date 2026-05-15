#!/usr/bin/env bash
#
# scripts/merge-pr.sh — squash-merge a PR and clean up.
#
# Usage:
#   bash scripts/merge-pr.sh <pr-number>
#   bash scripts/merge-pr.sh <pr-number> --bypass-with-disclosure="<reason>"
#   bash scripts/merge-pr.sh <pr-number> --bypass-with-disclosure="<reason>" --allow-fail-open-marker
#
# What this does:
#   1. Verifies the PR is open and mergeable.
#   2. Runs AI code review as a merge gate.
#   3. Squash-merges and deletes the remote branch.
#   4. Checks out/syncs the default branch where the local topology permits.
#   5. Deletes the verified-merged local feature branch when safe.
#   6. Removes the merged feature worktree when safe.
#
# Exit codes:
#   0 — merged cleanly
#   1 — merge failed (PR not mergeable, conflicts, etc.)
#   2 — usage / environment error
#
set -euo pipefail

PR_NUMBER=""
BYPASS_REASON=""
BYPASS_MARKER_SOURCE=""
BYPASS_MARKER_EVIDENCE=""
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SCRIPT_SYNC_GUARD="$SCRIPT_DIR/../lib/script-sync-guard.sh"
if [ -f "$SCRIPT_SYNC_GUARD" ]; then
  # shellcheck source=../lib/script-sync-guard.sh
  source "$SCRIPT_SYNC_GUARD"
  touchstone_script_sync_guard "$0" "$@"
fi
REVIEW_SCRIPT="$SCRIPT_DIR/conductor-review.sh"
if [ ! -f "$REVIEW_SCRIPT" ]; then
  REVIEW_SCRIPT="$SCRIPT_DIR/codex-review.sh"
fi
PREFLIGHT_SCRIPT="$SCRIPT_DIR/../lib/preflight.sh"
REVIEW_COMMENT_SCRIPT="$SCRIPT_DIR/../lib/review-comment.sh"
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
REVIEWED_HEAD_OID=""
PR_HEAD_BRANCH=""
BYPASS_REVIEW=false
ALLOW_FAIL_OPEN_MARKER=false
TOUCHSTONE_MERGE_FAILURE_REASON="nonzero-exit"
PREFLIGHT_REQUIRED=true
COMMENT_ON_CLEAN=true
COMMENT_FINDINGS_HISTORY=true
REVIEW_SUMMARY_FILE=""
PREFLIGHT_CACHE_KEY=""
PREFLIGHT_CACHE_FILE=""
PREFLIGHT_CACHE_INPUTS=""
PR_WORKTREE_PATH=""
TOUCHSTONE_REVIEW_LOG="${TOUCHSTONE_REVIEW_LOG-${HOME:-}/.touchstone-review-log}"
TOUCHSTONE_REVIEW_LOG_MAX_LINES="${TOUCHSTONE_REVIEW_LOG_MAX_LINES:-1000}"
TOUCHSTONE_FAIL_OPEN_BYPASS_WINDOW_HOURS="${TOUCHSTONE_FAIL_OPEN_BYPASS_WINDOW_HOURS:-24}"

on_merge_exit() {
  local rc="$?"
  if [ "$rc" -ne 0 ]; then
    touchstone_emit_event failed phase=merge reason="$TOUCHSTONE_MERGE_FAILURE_REASON" pr_number="$PR_NUMBER"
  fi
  return "$rc"
}

trap on_merge_exit EXIT

while [ "$#" -gt 0 ]; do
  case "$1" in
    --bypass-with-disclosure=*)
      BYPASS_REVIEW=true
      BYPASS_REASON="${1#*=}"
      shift
      ;;
    --bypass-with-disclosure)
      echo "ERROR: --bypass-with-disclosure requires a non-empty reason." >&2
      exit 2
      ;;
    --allow-fail-open-marker)
      ALLOW_FAIL_OPEN_MARKER=true
      shift
      ;;
    --*)
      echo "ERROR: Unknown option: $1" >&2
      exit 2
      ;;
    *)
      if [ -n "$PR_NUMBER" ]; then
        echo "ERROR: Unexpected extra argument: $1" >&2
        exit 2
      fi
      PR_NUMBER="$1"
      shift
      ;;
  esac
done

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

csv_contains() {
  local csv="$1"
  local wanted="$2"
  local item
  local -a csv_items

  if [ -n "$csv" ]; then
    IFS=',' read -r -a csv_items <<<"$csv"
    for item in "${csv_items[@]}"; do
      item="$(trim "$item")"
      if [ "$item" = "$wanted" ]; then
        return 0
      fi
    done
  fi
  return 1
}

csv_add_unique() {
  local csv="$1"
  local value="$2"

  value="$(trim "$value")"
  [ -n "$value" ] || {
    printf '%s' "$csv"
    return 0
  }
  [ "$value" != "unknown" ] || {
    printf '%s' "$csv"
    return 0
  }
  [ "$value" != "none" ] || {
    printf '%s' "$csv"
    return 0
  }
  if csv_contains "$csv" "$value"; then
    printf '%s' "$csv"
  elif [ -n "$csv" ]; then
    printf '%s,%s' "$csv" "$value"
  else
    printf '%s' "$value"
  fi
}

summary_string_field() {
  local field="$1"
  [ -n "$REVIEW_SUMMARY_FILE" ] || return 0
  [ -f "$REVIEW_SUMMARY_FILE" ] || return 0
  sed -nE 's/.*"'"$field"'"[[:space:]]*:[[:space:]]*"([^"]*)".*/\1/p' "$REVIEW_SUMMARY_FILE" 2>/dev/null | head -1
}

summary_number_field() {
  local field="$1"
  [ -n "$REVIEW_SUMMARY_FILE" ] || return 0
  [ -f "$REVIEW_SUMMARY_FILE" ] || return 0
  sed -nE 's/.*"'"$field"'"[[:space:]]*:[[:space:]]*([0-9]+).*/\1/p' "$REVIEW_SUMMARY_FILE" 2>/dev/null | head -1
}

review_output_has_concrete_findings() {
  local output_file="$1"
  [ -f "$output_file" ] || return 1
  grep -Eq 'CODEX_REVIEW_BLOCKED|^- ' "$output_file"
}

review_failure_is_infra() {
  local review_rc="$1"
  local output_file="$2"
  local findings exit_reason

  review_output_has_concrete_findings "$output_file" && return 1

  findings="$(summary_number_field findings)"
  if [ -n "$findings" ] && [ "$findings" != "0" ]; then
    return 1
  fi

  exit_reason="$(summary_string_field exit_reason)"
  case "$exit_reason" in
    timeout | error | provider-unavailable | dependency-missing | malformed-sentinel) return 0 ;;
    blocked | worktree-mutated | max-iterations) return 1 ;;
  esac

  [ "$review_rc" -eq 124 ] && return 0
  return 1
}

review_failed_provider_csv() {
  local csv field value item
  local -a provider_items

  csv=""
  for field in provider fallback_primary_provider fallback_retry_provider fallback_excluded_providers; do
    value="$(summary_string_field "$field")"
    if [ -n "$value" ]; then
      IFS=',' read -r -a provider_items <<<"$value"
      for item in "${provider_items[@]}"; do
        csv="$(csv_add_unique "$csv" "$item")"
      done
    fi
  done
  printf '%s' "$csv"
}

recommended_retry_provider() {
  local failed_csv="$1"
  local provider

  for provider in openrouter claude codex gemini kimi deepseek-chat deepseek-reasoner; do
    if ! csv_contains "$failed_csv" "$provider"; then
      printf '%s' "$provider"
      return 0
    fi
  done
  printf 'openrouter'
}

review_infra_retry_command() {
  local failed_csv retry_provider

  failed_csv="$(review_failed_provider_csv)"
  retry_provider="$(recommended_retry_provider "$failed_csv")"
  printf 'TOUCHSTONE_CONDUCTOR_WITH=%s bash scripts/merge-pr.sh %s' "$retry_provider" "$PR_NUMBER"
}

print_review_infra_retry_guidance() {
  local failed_csv exit_reason fallback_reason retry_command

  failed_csv="$(review_failed_provider_csv)"
  exit_reason="$(summary_string_field exit_reason)"
  fallback_reason="$(summary_string_field fallback_reason)"
  [ -n "$exit_reason" ] || exit_reason="reviewer-infrastructure"
  retry_command="$(review_infra_retry_command)"

  echo "" >&2
  echo "Provider/infrastructure outage details:" >&2
  echo "  deterministic preflight: clean" >&2
  echo "  concrete findings: 0" >&2
  echo "  review exit reason: $exit_reason" >&2
  if [ -n "$fallback_reason" ]; then
    echo "  fallback reason: $fallback_reason" >&2
  fi
  if [ -n "$failed_csv" ]; then
    echo "  failed/stalled provider(s): $failed_csv" >&2
  fi
  echo "  retry command: $retry_command" >&2
  echo "  alternate route: TOUCHSTONE_CONDUCTOR_WITH=<configured-hosted-provider> bash scripts/merge-pr.sh $PR_NUMBER" >&2
}

BYPASS_REASON="$(trim "$(printf '%s' "$BYPASS_REASON" | tr '\r\n\t' '   ')")"

if [ -z "$PR_NUMBER" ] || ! [[ "$PR_NUMBER" =~ ^[0-9]+$ ]]; then
  echo "Usage: bash scripts/merge-pr.sh <pr-number> [--bypass-with-disclosure=\"<reason>\" [--allow-fail-open-marker]]" >&2
  exit 2
fi
if [ "$BYPASS_REVIEW" = true ] && [ -z "$BYPASS_REASON" ]; then
  echo "ERROR: --bypass-with-disclosure requires a non-empty reason." >&2
  exit 2
fi
if [ "$ALLOW_FAIL_OPEN_MARKER" = true ] && [ "$BYPASS_REVIEW" != true ]; then
  echo "ERROR: --allow-fail-open-marker requires --bypass-with-disclosure=\"<reason>\"." >&2
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

load_merge_review_config() {
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

  merge_pr_toml_callback() {
    local section="$1"
    local key="$2"
    local value="$3"

    if [ "$section" = "review" ] && [ "$key" = "preflight_required" ]; then
      PREFLIGHT_REQUIRED="$(normalize_bool "$value")"
    elif [ "$section" = "review" ] && [ "$key" = "comment_on_clean" ]; then
      COMMENT_ON_CLEAN="$(normalize_bool "$value")"
    elif [ "$section" = "review" ] && [ "$key" = "comment_findings_history" ]; then
      COMMENT_FINDINGS_HISTORY="$(normalize_bool "$value")"
    fi
  }

  toml_parse "$config_file" merge_pr_toml_callback
}

review_clean_marker_key() {
  local branch="$1"
  printf '%s' "$branch" | sed 's/[^A-Za-z0-9._-]/_/g'
}

review_clean_marker_file() {
  local branch="$1"
  printf '%s/%s.clean' \
    "$(git rev-parse --git-path touchstone/reviewer-clean)" \
    "$(review_clean_marker_key "$branch")"
}

review_findings_history_file() {
  local branch="$1"
  printf '%s/%s.jsonl' \
    "$(git rev-parse --git-path touchstone/reviewer-findings-history)" \
    "$(review_clean_marker_key "$branch")"
}

marker_field() {
  local field="$1"
  local marker="$2"
  awk -F= -v key="$field" '$1 == key { sub(/^[^=]*=/, ""); print; exit }' "$marker"
}

preflight_hash_stream() {
  shasum -a 256 | awk '{ print $1 }'
}

preflight_hash_file() {
  local path="$1"

  if [ -f "$path" ]; then
    shasum -a 256 "$path" | awk '{ print $1 }'
  else
    printf 'missing'
  fi
}

preflight_hash_paths() {
  local repo_root="$1"
  shift
  local rel path

  for rel in "$@"; do
    path="$repo_root/$rel"
    printf '%s\t%s\n' "$rel" "$(preflight_hash_file "$path")"
  done | preflight_hash_stream
}

preflight_hash_changed_paths() {
  local repo_root="$1"
  shift
  local rel path

  for rel in "$@"; do
    [ -n "$rel" ] || continue
    path="$repo_root/$rel"
    if [ -f "$path" ]; then
      printf '%s\t%s\n' "$rel" "$(preflight_hash_file "$path")"
    else
      printf '%s\tmissing\n' "$rel"
    fi
  done | preflight_hash_stream
}

preflight_hash_file_list() {
  local label path

  while [ "$#" -gt 0 ]; do
    label="$1"
    path="$2"
    shift 2
    printf '%s\t%s\n' "$label" "$(preflight_hash_file "$path")"
  done | preflight_hash_stream
}

preflight_changed_paths() {
  local repo_root="$1"
  local base_ref="$2"

  (cd "$repo_root" && git diff --name-only "$base_ref"...HEAD) 2>/dev/null | sort -u
}

preflight_worktree_hash() {
  local repo_root="$1"
  local base_ref="$2"
  local -a paths=()
  local path

  while IFS= read -r path; do
    [ -n "$path" ] || continue
    paths+=("$path")
  done < <(preflight_changed_paths "$repo_root" "$base_ref")

  if [ "${#paths[@]}" -eq 0 ]; then
    printf 'no-changed-paths\n' | preflight_hash_stream
    return
  fi

  (
    cd "$repo_root" || exit 1
    git status --porcelain --untracked-files=all -- "${paths[@]}"
    printf '\n-- worktree diff --\n'
    git diff --binary -- "${paths[@]}"
    printf '\n-- index diff --\n'
    git diff --cached --binary -- "${paths[@]}"
    printf '\n-- untracked files --\n'
    while IFS= read -r -d '' rel; do
      printf 'path\t%s\n' "$rel"
      if [ -f "$rel" ]; then
        printf 'sha256\t%s\n' "$(preflight_hash_file "$rel")"
      else
        printf 'sha256\tmissing\n'
      fi
    done < <(git ls-files --others --exclude-standard -z -- "${paths[@]}")
  ) 2>/dev/null | preflight_hash_stream
}

preflight_changed_paths_hash() {
  local repo_root="$1"
  local base_ref="$2"
  local -a paths=()
  local path

  while IFS= read -r path; do
    [ -n "$path" ] || continue
    paths+=("$path")
  done < <(preflight_changed_paths "$repo_root" "$base_ref")

  preflight_hash_changed_paths "$repo_root" "${paths[@]}"
}

preflight_tool_fingerprint() {
  local tool path version_hash

  for tool in shellcheck shfmt markdownlint-cli2 markdownlint actionlint; do
    path="$(command -v "$tool" 2>/dev/null || true)"
    if [ -n "$path" ]; then
      version_hash="$({ "$tool" --version 2>&1 || true; } | preflight_hash_stream)"
      printf '%s\t%s\t%s\n' "$tool" "$path" "$version_hash"
    else
      printf '%s\tmissing\tmissing\n' "$tool"
    fi
  done | preflight_hash_stream
}

preflight_env_fingerprint() {
  {
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_SCRIPT=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_SCRIPT:-}"
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_COMMAND=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_COMMAND:-}"
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_LANE=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_LANE:-}"
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_AFFECTED_COMMAND=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_AFFECTED_COMMAND:-}"
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_SMOKE_COMMAND=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_SMOKE_COMMAND:-}"
    printf 'TOUCHSTONE_PREFLIGHT_VALIDATE_FULL_COMMAND=%s\n' "${TOUCHSTONE_PREFLIGHT_VALIDATE_FULL_COMMAND:-}"
  } | preflight_hash_stream
}

preflight_cache_inputs() {
  local base_ref="$1"
  local repo_root head_sha base_sha merge_base changed_paths_hash
  local checker_hash config_hash worktree_hash tool_hash env_hash

  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  repo_root="$(cd "$repo_root" && pwd)" || return 1
  head_sha="$(git -C "$repo_root" rev-parse HEAD 2>/dev/null)" || return 1
  base_sha="$(git -C "$repo_root" rev-parse --verify "$base_ref^{commit}" 2>/dev/null)" || return 1
  merge_base="$(git -C "$repo_root" merge-base "$base_ref" "$head_sha" 2>/dev/null)" || return 1
  changed_paths_hash="$(preflight_changed_paths_hash "$repo_root" "$base_ref")" || return 1
  checker_hash="$(preflight_hash_file_list \
    "lib/preflight.sh" "$PREFLIGHT_SCRIPT" \
    "lib/preflight-scope.sh" "$(dirname "$PREFLIGHT_SCRIPT")/preflight-scope.sh" \
    "scripts/touchstone-run.sh" "$SCRIPT_DIR/touchstone-run.sh")"
  config_hash="$(preflight_hash_paths "$repo_root" \
    ".touchstone-review.toml" \
    ".codex-review.toml" \
    ".touchstone-config" \
    ".touchstone-version" \
    ".pre-commit-config.yaml" \
    ".markdownlint.json")"
  worktree_hash="$(preflight_worktree_hash "$repo_root" "$base_ref")" || return 1
  tool_hash="$(preflight_tool_fingerprint)"
  env_hash="$(preflight_env_fingerprint)"

  printf 'version=4\n'
  printf 'repo_root=%s\n' "$repo_root"
  printf 'scope=diff\n'
  printf 'base_ref=%s\n' "$base_ref"
  printf 'base_sha=%s\n' "$base_sha"
  printf 'head_sha=%s\n' "$head_sha"
  printf 'merge_base=%s\n' "$merge_base"
  printf 'changed_files_hash=%s\n' "$changed_paths_hash"
  printf 'checker_hash=%s\n' "$checker_hash"
  printf 'config_hash=%s\n' "$config_hash"
  printf 'worktree_hash=%s\n' "$worktree_hash"
  printf 'tool_hash=%s\n' "$tool_hash"
  printf 'env_hash=%s\n' "$env_hash"
}

preflight_cache_prepare() {
  local base_ref="$1"
  local cache_dir

  PREFLIGHT_CACHE_KEY=""
  PREFLIGHT_CACHE_FILE=""
  PREFLIGHT_CACHE_INPUTS=""

  if truthy "${TOUCHSTONE_PREFLIGHT_DISABLE_CACHE:-false}"; then
    return 1
  fi

  PREFLIGHT_CACHE_INPUTS="$(preflight_cache_inputs "$base_ref")" || return 1
  PREFLIGHT_CACHE_KEY="$(printf '%s\n' "$PREFLIGHT_CACHE_INPUTS" | preflight_hash_stream)"
  cache_dir="$(git rev-parse --git-path touchstone/preflight-clean 2>/dev/null)" || return 1
  PREFLIGHT_CACHE_FILE="$cache_dir/$PREFLIGHT_CACHE_KEY.clean"
}

preflight_cache_short_key() {
  printf '%s' "${PREFLIGHT_CACHE_KEY:0:12}"
}

preflight_cache_hit() {
  local marker_inputs

  [ -n "$PREFLIGHT_CACHE_FILE" ] || return 1
  [ -f "$PREFLIGHT_CACHE_FILE" ] || return 1
  grep -q '^result=preflight_clean$' "$PREFLIGHT_CACHE_FILE" || return 1
  marker_inputs="$(sed '1,2d' "$PREFLIGHT_CACHE_FILE")"
  [ "$marker_inputs" = "$PREFLIGHT_CACHE_INPUTS" ]
}

write_preflight_clean_cache() {
  local cache_dir tmp

  [ -n "$PREFLIGHT_CACHE_FILE" ] || return 0
  [ -n "$PREFLIGHT_CACHE_INPUTS" ] || return 0
  cache_dir="$(dirname "$PREFLIGHT_CACHE_FILE")"
  if ! mkdir -p "$cache_dir" 2>/dev/null; then
    echo "WARNING: could not create preflight cache directory $cache_dir; continuing without cache." >&2
    return 0
  fi

  tmp="$PREFLIGHT_CACHE_FILE.$$"
  if {
    printf 'result=preflight_clean\n'
    printf 'created_at=%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"
    printf '%s\n' "$PREFLIGHT_CACHE_INPUTS"
  } >"$tmp" 2>/dev/null && mv "$tmp" "$PREFLIGHT_CACHE_FILE" 2>/dev/null; then
    return 0
  fi

  rm -f "$tmp" 2>/dev/null || true
  echo "WARNING: could not write preflight cache marker $PREFLIGHT_CACHE_FILE; continuing without cache." >&2
  return 0
}

worktree_path_for_branch() {
  local branch="$1"
  local current_path=""
  local current_branch=""
  local line key value

  git worktree list --porcelain | while IFS= read -r line || [ -n "$line" ]; do
    if [ -z "$line" ]; then
      if [ "$current_branch" = "refs/heads/$branch" ]; then
        printf '%s\n' "$current_path"
        exit 0
      fi
      current_path=""
      current_branch=""
      continue
    fi

    key="${line%% *}"
    value="${line#* }"
    case "$key" in
      worktree) current_path="$value" ;;
      branch) current_branch="$value" ;;
    esac
  done

  if [ "$current_branch" = "refs/heads/$branch" ]; then
    printf '%s\n' "$current_path"
  fi
  return 0
}

branch_has_clean_review_marker() {
  local branch="$1"
  local head_oid="$2"
  local merge_base="$3"
  local marker marker_branch marker_head marker_merge_base live_branch_head
  marker="$(review_clean_marker_file "$branch")"
  [ -f "$marker" ] || return 1
  grep -q '^result=CODEX_REVIEW_CLEAN$' "$marker" || return 1
  marker_branch="$(marker_field branch "$marker")"
  marker_head="$(marker_field head "$marker")"
  marker_merge_base="$(marker_field merge_base "$marker")"

  if ! live_branch_head="$(git rev-parse "$branch" 2>/dev/null)"; then
    live_branch_head="$(git rev-parse HEAD 2>/dev/null || echo "")"
  fi

  # Invariant: A clean-review marker is valid only when its `head` field equals the current branch HEAD.
  [ "$marker_branch" = "$branch" ] \
    && [ -n "$live_branch_head" ] \
    && [ "$live_branch_head" = "$head_oid" ] \
    && [ "$marker_head" = "$live_branch_head" ] \
    && [ "$marker_merge_base" = "$merge_base" ]
}

is_positive_integer() {
  case "${1:-}" in
    "" | *[!0-9]*) return 1 ;;
    *) [ "$1" -gt 0 ] ;;
  esac
}

timestamp_to_epoch() {
  local timestamp="$1"

  date -j -f "%Y-%m-%dT%H:%M:%S%z" "$timestamp" "+%s" 2>/dev/null \
    || date -d "$timestamp" "+%s" 2>/dev/null
}

head_oid_matches_logged_sha() {
  local head_oid="$1"
  local logged_sha="$2"

  [ -n "$head_oid" ] || return 1
  [ -n "$logged_sha" ] || return 1

  case "$head_oid" in
    "$logged_sha"*) return 0 ;;
  esac
  case "$logged_sha" in
    "$head_oid"*) return 0 ;;
  esac
  return 1
}

bypass_reason_mentions_fail_open() {
  local reason_lower
  reason_lower="$(printf '%s' "$BYPASS_REASON" | tr '[:upper:]' '[:lower:]')"

  case "$reason_lower" in
    *fail-open* | *"fail open"* | *provider* | *infra* | *outage* | *timeout* | *"timed out"* | *"reviewer unavailable"* | *"reviewer error"*)
      return 0
      ;;
    *) return 1 ;;
  esac
}

branch_has_recent_fail_open_marker() {
  local branch="$1"
  local head_oid="$2"
  local log_file="$TOUCHSTONE_REVIEW_LOG"
  local window_hours="$TOUCHSTONE_FAIL_OPEN_BYPASS_WINDOW_HOURS"
  local window_seconds now_epoch tab
  local timestamp repo_path log_branch logged_sha reason detail
  local event_epoch age

  [ -n "$log_file" ] || return 1
  [ "$log_file" != "/dev/null" ] || return 1
  [ -f "$log_file" ] || return 1
  is_positive_integer "$window_hours" || return 1

  window_seconds=$((window_hours * 3600))
  now_epoch="$(date "+%s" 2>/dev/null)" || return 1
  tab="$(printf '\t')"

  while IFS="$tab" read -r timestamp repo_path log_branch logged_sha reason detail || [ -n "$timestamp" ]; do
    [ "$log_branch" = "$branch" ] || continue
    head_oid_matches_logged_sha "$head_oid" "$logged_sha" || continue
    case "$reason" in
      FAIL_OPEN_*) ;;
      *) continue ;;
    esac
    case "$detail" in
      fail-open:*) ;;
      *) continue ;;
    esac
    event_epoch="$(timestamp_to_epoch "$timestamp" 2>/dev/null || true)"
    [ -n "$event_epoch" ] || continue
    age=$((now_epoch - event_epoch))
    # Allow a small future skew between the review hook and merge machine clocks.
    [ "$age" -ge -300 ] || continue
    [ "$age" -le "$window_seconds" ] || continue

    BYPASS_MARKER_EVIDENCE="timestamp=$timestamp; repo=$repo_path; branch=$log_branch; sha=$logged_sha; reason=$reason; detail=$detail"
    return 0
  done <"$log_file"

  return 1
}

sanitize_review_log_field() {
  printf '%s' "$1" | tr '\t\n' '  '
}

append_review_log_event() {
  local reason="$1"
  local detail="$2"
  local log_file="$TOUCHSTONE_REVIEW_LOG"
  local timestamp repo_root branch sha tmp_dir tmp_file line_count keep_lines

  [ -n "$log_file" ] || return 0
  [ "$log_file" != "/dev/null" ] || return 0

  timestamp="$(date '+%Y-%m-%dT%H:%M:%S%z' 2>/dev/null || echo unknown)"
  repo_root="$(git rev-parse --show-toplevel 2>/dev/null || echo unknown)"
  branch="${PR_HEAD_BRANCH:-$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)}"
  sha="${REVIEWED_HEAD_OID:-$(git rev-parse HEAD 2>/dev/null || echo unknown)}"
  if [ "$sha" != "unknown" ] && [ "${#sha}" -gt 12 ]; then
    sha="${sha:0:12}"
  fi

  reason="$(sanitize_review_log_field "$reason")"
  detail="$(sanitize_review_log_field "$detail")"
  branch="$(sanitize_review_log_field "$branch")"

  tmp_dir="${TMPDIR:-/tmp}"
  tmp_dir="${tmp_dir%/}"
  tmp_file="$tmp_dir/touchstone-merge-review-log.$$.tmp"

  if ! mkdir -p "$(dirname "$log_file")" 2>/dev/null; then
    echo "WARNING: Could not create review audit log directory for $log_file." >&2
    return 0
  fi

  if [ -f "$log_file" ]; then
    line_count="$(wc -l <"$log_file" 2>/dev/null | tr -d ' ')" || line_count=0
    line_count="${line_count:-0}"
    if is_positive_integer "$TOUCHSTONE_REVIEW_LOG_MAX_LINES" && [ "$line_count" -ge "$TOUCHSTONE_REVIEW_LOG_MAX_LINES" ]; then
      keep_lines=$((TOUCHSTONE_REVIEW_LOG_MAX_LINES - 1))
      tail -n "$keep_lines" "$log_file" >"$tmp_file" 2>/dev/null || : >"$tmp_file"
    else
      cat "$log_file" >"$tmp_file" 2>/dev/null || : >"$tmp_file"
    fi
  else
    : >"$tmp_file"
  fi

  printf '%s\t%s\t%s\t%s\t%s\t%s\n' \
    "$timestamp" "$repo_root" "$branch" "$sha" "$reason" "$detail" \
    >>"$tmp_file" 2>/dev/null || {
    rm -f "$tmp_file" 2>/dev/null
    echo "WARNING: Could not append review audit log entry for $log_file." >&2
    return 0
  }

  if ! mv "$tmp_file" "$log_file" 2>/dev/null; then
    rm -f "$tmp_file" 2>/dev/null
    echo "WARNING: Could not update review audit log $log_file." >&2
  fi
}

record_bypass_audit_log() {
  local detail
  detail="reason=$BYPASS_REASON; marker=$BYPASS_MARKER_SOURCE"
  if [ -n "$BYPASS_MARKER_EVIDENCE" ]; then
    detail="$detail; evidence=[$BYPASS_MARKER_EVIDENCE]"
  fi
  append_review_log_event review-bypass "$detail"
}

sync_default_branch_after_merge() {
  local current_branch current_worktree default_worktree

  echo "==> Merged. Updating local $DEFAULT_BRANCH ..."
  current_branch="$(git rev-parse --abbrev-ref HEAD)"

  if [ "$current_branch" = "$DEFAULT_BRANCH" ]; then
    if ! git pull --rebase; then
      echo "WARNING: PR #$PR_NUMBER merged remotely, but local $DEFAULT_BRANCH could not pull --rebase." >&2
      echo "WARNING: Run this when convenient: git pull --rebase" >&2
    fi
    return 0
  fi

  current_worktree="$(git rev-parse --show-toplevel)"
  default_worktree="$(worktree_path_for_branch "$DEFAULT_BRANCH" | head -n 1)"
  if [ -n "$default_worktree" ] && [ "$default_worktree" != "$current_worktree" ]; then
    if [ ! -d "$default_worktree" ]; then
      echo "WARNING: $DEFAULT_BRANCH is recorded as checked out in a missing worktree: $default_worktree" >&2
      echo "WARNING: This is stale git worktree metadata, usually from deleting the directory directly." >&2
      echo "WARNING: Run 'git worktree prune' from a remaining checkout, then rerun local sync if needed." >&2
      return 0
    fi
    echo "==> $DEFAULT_BRANCH is checked out in sibling worktree: $default_worktree"
    echo "==> Fast-forwarding that worktree after remote merge ..."
    if git -C "$default_worktree" pull --ff-only; then
      return 0
    fi
    echo "WARNING: PR #$PR_NUMBER merged remotely, but sibling worktree '$default_worktree' could not fast-forward." >&2
    echo "WARNING: Run this when convenient: git -C '$default_worktree' pull --ff-only" >&2
    return 0
  fi

  if ! git checkout "$DEFAULT_BRANCH"; then
    echo "WARNING: PR #$PR_NUMBER merged remotely, but this worktree could not check out $DEFAULT_BRANCH." >&2
    echo "WARNING: Run this when convenient: git checkout '$DEFAULT_BRANCH' && git pull --rebase" >&2
    return 0
  fi
  if ! git pull --rebase; then
    echo "WARNING: PR #$PR_NUMBER merged remotely, but local $DEFAULT_BRANCH could not pull --rebase." >&2
    echo "WARNING: Run this when convenient: git pull --rebase" >&2
  fi
}

checkout_default_ref_for_cleanup() {
  local branch="$1"
  local reviewed_head="$2"
  local current_branch current_head current_worktree default_worktree

  current_branch="$(git rev-parse --abbrev-ref HEAD)"
  current_head="$(git rev-parse HEAD 2>/dev/null || echo "")"
  if [ "$current_branch" != "$branch" ]; then
    if [ "$current_branch" != "HEAD" ] || [ "$current_head" != "$reviewed_head" ]; then
      return 0
    fi
  fi

  current_worktree="$(git rev-parse --show-toplevel)"
  default_worktree="$(worktree_path_for_branch "$DEFAULT_BRANCH" | head -n 1)"
  if [ -n "$default_worktree" ] && [ "$default_worktree" != "$current_worktree" ]; then
    echo "==> $DEFAULT_BRANCH is checked out elsewhere; detaching this worktree at $DEFAULT_BRANCH before local branch cleanup ..."
    if git checkout --detach "$DEFAULT_BRANCH"; then
      return 0
    fi
    echo "WARNING: Could not detach this worktree at $DEFAULT_BRANCH; leaving local branch '$branch' intact." >&2
    return 1
  fi

  if git checkout "$DEFAULT_BRANCH"; then
    return 0
  fi
  if git checkout --detach "$DEFAULT_BRANCH"; then
    echo "==> Detached this worktree at $DEFAULT_BRANCH before local branch cleanup."
    return 0
  fi
  echo "WARNING: Could not move off local branch '$branch'; leaving it intact." >&2
  return 1
}

cleanup_local_pr_branch_after_merge() {
  local branch="$PR_HEAD_BRANCH"
  local reviewed_head="$REVIEWED_HEAD_OID"
  local local_head pr_state

  if [ -z "$branch" ] || [ -z "$reviewed_head" ]; then
    echo "WARNING: Missing reviewed PR head metadata; skipping local branch cleanup." >&2
    return 0
  fi
  if [ "$branch" = "$DEFAULT_BRANCH" ] || [ "$branch" = "main" ] || [ "$branch" = "master" ]; then
    echo "WARNING: Refusing to delete protected branch '$branch' after PR #$PR_NUMBER." >&2
    return 0
  fi
  if ! git show-ref --verify --quiet "refs/heads/$branch"; then
    echo "==> Local branch '$branch' is already absent."
    return 0
  fi
  if ! local_head="$(git rev-parse "$branch" 2>/dev/null)"; then
    echo "WARNING: Could not resolve local branch '$branch'; leaving it intact." >&2
    return 0
  fi
  if [ "$local_head" != "$reviewed_head" ]; then
    echo "WARNING: Local branch '$branch' is at $local_head, not reviewed PR head $reviewed_head; leaving it intact." >&2
    return 0
  fi
  [ -n "$PR_WORKTREE_PATH" ] || PR_WORKTREE_PATH="$(worktree_path_for_branch "$branch" | head -n 1)"
  pr_state="$(gh pr view "$PR_NUMBER" --json state --jq '.state' 2>/dev/null || echo "")"
  if [ "$pr_state" != "MERGED" ]; then
    echo "WARNING: PR #$PR_NUMBER is not confirmed MERGED (state: ${pr_state:-unknown}); leaving local branch '$branch' intact." >&2
    return 0
  fi

  if ! checkout_default_ref_for_cleanup "$branch" "$reviewed_head"; then
    return 0
  fi

  echo "==> Deleting local branch '$branch' after verified squash merge of $reviewed_head ..."
  if git branch -D "$branch"; then
    echo "==> Local branch '$branch' deleted."
  else
    echo "WARNING: Could not delete local branch '$branch' after verified merge." >&2
    echo "WARNING: Run this when convenient after moving off the branch: git branch -D '$branch'" >&2
  fi
}

cleanup_pr_worktree_after_merge() {
  local pr_worktree="$PR_WORKTREE_PATH"
  local current_worktree default_worktree dirty_status

  [ -n "$pr_worktree" ] || return 0
  if [ ! -d "$pr_worktree" ]; then
    echo "WARNING: Merged PR worktree '$pr_worktree' is missing; run 'git worktree prune' if Git still records it." >&2
    return 0
  fi

  current_worktree="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
  default_worktree="$(worktree_path_for_branch "$DEFAULT_BRANCH" | head -n 1)"
  if [ -z "$default_worktree" ] || [ ! -d "$default_worktree" ]; then
    default_worktree="$current_worktree"
  fi
  if [ -z "$default_worktree" ] || [ "$default_worktree" = "$pr_worktree" ]; then
    echo "WARNING: No separate default-branch worktree is available to remove merged PR worktree '$pr_worktree'." >&2
    echo "         Run 'bash scripts/cleanup-worktrees.sh --execute' after moving to a different worktree." >&2
    return 0
  fi

  dirty_status="$(git -C "$pr_worktree" status --porcelain 2>/dev/null || printf 'status-failed\n')"
  if [ -n "$dirty_status" ]; then
    echo "WARNING: Merged PR worktree '$pr_worktree' has uncommitted changes; leaving it in place." >&2
    echo "         Inspect it, then run 'bash scripts/cleanup-worktrees.sh --execute' when it is clean." >&2
    return 0
  fi

  echo "==> Removing merged PR worktree '$pr_worktree' ..."
  touchstone_emit_event cleanup_started worktree_path="$pr_worktree"
  if git -C "$default_worktree" worktree remove "$pr_worktree"; then
    echo "==> Merged PR worktree removed."
    touchstone_emit_event cleanup_done worktree_path="$pr_worktree" result=removed
  else
    echo "WARNING: Could not remove merged PR worktree '$pr_worktree'." >&2
    echo "         Run 'bash scripts/cleanup-worktrees.sh --execute' from $default_worktree to inspect and clean up." >&2
    touchstone_emit_event cleanup_done worktree_path="$pr_worktree" result=failed
  fi
}

print_bypass_banner() {
  cat <<EOF

!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!
!! BYPASSING REVIEWER GATE
!! marker: $BYPASS_MARKER_SOURCE
!! reason: $BYPASS_REASON
!! This bypass is recorded on the PR and squash commit.
!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!

EOF
}

# Append a squash-merge record to .git/touchstone/squash-map.jsonl so
# scripts/cleanup-branches.sh can recognize a branch as squash-merged even
# after $DEFAULT_BRANCH evolves past it (later commits on the same files
# break the tree-equivalence heuristic).
#
# The record carries:
#   - branch       : the head ref name of the merged PR
#   - pr           : PR number
#   - branch_oid   : tip of the branch at merge time (so cleanup can detect
#                    "branch picked up new commits after the squash" and
#                    fall through to the existing tree check)
#   - squash_commit: the squash commit on the default branch (best effort —
#                    empty string if gh cannot resolve it yet)
#   - ts           : UTC ISO timestamp
#
# I/O is best-effort. A failure to write must not fail the merge: the merge
# already succeeded server-side, and the squash-map is an optimization for
# later cleanup, not a correctness boundary. Any failure is logged to stderr.
record_squash_merge() {
  local branch="$1"
  local pr="$2"
  local branch_oid="$3"
  local squash_commit="${4:-}"
  local map_path map_dir ts

  if [ -z "$branch" ] || [ -z "$pr" ] || [ -z "$branch_oid" ]; then
    echo "WARNING: record_squash_merge: missing branch/pr/oid, skipping squash-map write." >&2
    return 0
  fi

  if ! map_path="$(git rev-parse --git-path touchstone/squash-map.jsonl 2>/dev/null)" \
    || [ -z "$map_path" ]; then
    echo "WARNING: record_squash_merge: could not resolve squash-map path; skipping." >&2
    return 0
  fi
  map_dir="$(dirname "$map_path")"
  if ! mkdir -p "$map_dir" 2>/dev/null; then
    echo "WARNING: record_squash_merge: could not create $map_dir; skipping squash-map write." >&2
    return 0
  fi

  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo "")"

  # JSON-encode each string field. Bash can produce safe JSON for these
  # tightly constrained values (branch names exclude " and \, OIDs are hex,
  # ts is ISO-8601) by simply quoting — no escaping needed in practice.
  # Defense in depth: refuse to record if any field contains a quote or
  # backslash, rather than emit malformed JSON.
  local field
  for field in "$branch" "$pr" "$branch_oid" "$squash_commit" "$ts"; do
    case "$field" in
      *\"* | *\\*)
        echo "WARNING: record_squash_merge: field contains quote/backslash, skipping squash-map write." >&2
        return 0
        ;;
    esac
  done

  local line
  line="{\"branch\":\"$branch\",\"pr\":\"$pr\",\"branch_oid\":\"$branch_oid\",\"squash_commit\":\"$squash_commit\",\"ts\":\"$ts\"}"
  if ! printf '%s\n' "$line" >>"$map_path" 2>/dev/null; then
    echo "WARNING: record_squash_merge: could not append to $map_path; skipping squash-map write." >&2
    return 0
  fi
  echo "==> Recorded squash-merge metadata for '$branch' -> $map_path"
}

record_bypass_comment() {
  local body
  body="Reviewer bypassed via \`--bypass-with-disclosure\`. Marker: $BYPASS_MARKER_SOURCE. Reason: $BYPASS_REASON"
  if [ -n "$BYPASS_MARKER_EVIDENCE" ]; then
    body="$body

Fail-open evidence: $BYPASS_MARKER_EVIDENCE"
  fi
  gh pr comment "$PR_NUMBER" --body "$body"
}

post_clean_review_comment() {
  local summary_file="$1"
  local summary_json comment exit_reason

  if ! truthy "$COMMENT_ON_CLEAN"; then
    echo "==> Clean-review PR comment disabled by [review].comment_on_clean=false."
    return 0
  fi
  if [ "$BYPASS_REVIEW" = true ]; then
    return 0
  fi
  if ! declare -F format_clean_review_comment >/dev/null 2>&1 \
    || ! declare -F post_pr_review_comment >/dev/null 2>&1; then
    echo "WARNING: review comment helper not found at $REVIEW_COMMENT_SCRIPT; skipping clean-review comment." >&2
    return 0
  fi
  if [ -z "$summary_file" ] || [ ! -f "$summary_file" ]; then
    echo "WARNING: clean review summary file missing; skipping clean-review comment." >&2
    return 0
  fi

  summary_json="$(tail -n 1 "$summary_file" 2>/dev/null || true)"
  if [ -z "$summary_json" ]; then
    echo "WARNING: clean review summary file is empty; skipping clean-review comment." >&2
    return 0
  fi

  exit_reason="$(review_comment_json_field "$summary_json" exit_reason 2>/dev/null || true)"
  if [ -n "$exit_reason" ] && [ "$exit_reason" != "clean" ]; then
    if declare -F format_review_failure_comment >/dev/null 2>&1; then
      comment="$(format_review_failure_comment "$summary_json" "" "" "")"
      if post_pr_review_comment "$PR_NUMBER" "$comment"; then
        echo "==> Posted non-clean review summary PR comment (exit_reason=$exit_reason)."
        return 0
      fi
      echo "WARNING: failed to post non-clean review summary comment for PR #$PR_NUMBER." >&2
    else
      echo "WARNING: review summary exit_reason=$exit_reason; skipping clean-review comment." >&2
    fi
    return 0
  fi

  comment="$(format_clean_review_comment "$summary_json")"
  if post_pr_review_comment "$PR_NUMBER" "$comment"; then
    echo "==> Posted clean-review PR comment."
    return 0
  fi

  echo "WARNING: failed to post clean-review PR comment for PR #$PR_NUMBER." >&2
  return 0
}

post_review_failure_comment() {
  local review_output_file="$1"
  local infra_failure="$2"
  local summary_json output comment retry_command failed_csv

  if [ "$BYPASS_REVIEW" = true ]; then
    return 0
  fi
  if ! declare -F format_review_failure_comment >/dev/null 2>&1 \
    || ! declare -F post_pr_review_comment >/dev/null 2>&1; then
    echo "WARNING: review comment helper not found at $REVIEW_COMMENT_SCRIPT; skipping review-failure comment." >&2
    return 0
  fi

  if [ -n "$REVIEW_SUMMARY_FILE" ] && [ -f "$REVIEW_SUMMARY_FILE" ]; then
    summary_json="$(tail -n 1 "$REVIEW_SUMMARY_FILE" 2>/dev/null || true)"
  else
    summary_json='{"reviewer":"Conductor","provider":"unknown","model":"unknown","peer_provider":"none","iterations":0,"mode":"fix","findings":0,"exit_reason":"reviewer-infrastructure"}'
  fi
  [ -n "$summary_json" ] || summary_json='{"reviewer":"Conductor","provider":"unknown","model":"unknown","peer_provider":"none","iterations":0,"mode":"fix","findings":0,"exit_reason":"reviewer-infrastructure"}'

  output="$(cat "$review_output_file" 2>/dev/null || true)"
  retry_command=""
  failed_csv=""
  if [ "$infra_failure" = true ]; then
    retry_command="$(review_infra_retry_command)"
    failed_csv="$(review_failed_provider_csv)"
  fi

  comment="$(format_review_failure_comment "$summary_json" "$output" "$retry_command" "$failed_csv")"
  if post_pr_review_comment "$PR_NUMBER" "$comment"; then
    echo "==> Posted review-failure PR comment."
    return 0
  fi

  echo "WARNING: failed to post review-failure PR comment for PR #$PR_NUMBER." >&2
  return 0
}

post_findings_history_comment() {
  local branch="$1"
  local history_file comment

  if ! truthy "$COMMENT_FINDINGS_HISTORY"; then
    echo "==> Findings-history PR comment disabled by [review].comment_findings_history=false."
    return 0
  fi
  if [ "$BYPASS_REVIEW" = true ]; then
    return 0
  fi
  if ! declare -F format_findings_history_comment >/dev/null 2>&1 \
    || ! declare -F post_pr_review_comment >/dev/null 2>&1; then
    echo "WARNING: review comment helper not found at $REVIEW_COMMENT_SCRIPT; skipping findings-history comment." >&2
    return 0
  fi
  if [ -z "$branch" ]; then
    echo "WARNING: PR head branch missing; skipping findings-history comment." >&2
    return 0
  fi

  history_file="$(review_findings_history_file "$branch")"
  if ! comment="$(format_findings_history_comment "$history_file")"; then
    echo "==> No actionable review findings history to comment."
    return 0
  fi

  if post_pr_review_comment "$PR_NUMBER" "$comment"; then
    echo "==> Posted findings-history PR comment."
    return 0
  fi

  echo "WARNING: failed to post findings-history PR comment for PR #$PR_NUMBER." >&2
  return 0
}

failed_checks() {
  gh pr checks "$PR_NUMBER" \
    --json name,bucket,state,link \
    --template '{{range .}}{{if eq .bucket "fail"}}{{.name}}{{"\t"}}{{.state}}{{"\t"}}{{.link}}{{"\n"}}{{end}}{{end}}' \
    2>/dev/null || true
}

print_failed_checks_and_exit() {
  local failed_checks="$1"
  local name state link

  [ -n "$failed_checks" ] || return 1

  echo "ERROR: PR #$PR_NUMBER has failed check(s); stopping automerge." >&2
  while IFS="$(printf '\t')" read -r name state link || [ -n "$name" ]; do
    [ -n "$name" ] || continue
    if [ -n "$link" ]; then
      echo "       - $name (${state:-failed}): $link" >&2
    else
      echo "       - $name (${state:-failed})" >&2
    fi
  done <<<"$failed_checks"
  TOUCHSTONE_MERGE_FAILURE_REASON="check-failed"
  exit 1
}

wait_for_clean_merge_state() {
  local attempt max_attempts sleep_seconds

  echo "==> Checking merge state for PR #$PR_NUMBER ..."
  STATE=""
  MERGEABLE=""
  MERGE_STATE_RETRY_DELAYS=(1 2 5 10 30 30 30 30 30)
  max_attempts="${MERGE_PR_STATE_MAX_ATTEMPTS:-30}"
  if ! [[ "$max_attempts" =~ ^[0-9]+$ ]] || [ "$max_attempts" -lt 1 ]; then
    max_attempts=30
  fi
  attempt=1
  while [ "$attempt" -le "$max_attempts" ]; do
    MERGE_STATE="$(gh pr view "$PR_NUMBER" --json mergeStateStatus,mergeable --template '{{.mergeStateStatus}} {{.mergeable}}' 2>/dev/null || echo '')"
    STATE="${MERGE_STATE%% *}"
    MERGEABLE="${MERGE_STATE#* }"
    [ -n "$STATE" ] || STATE="UNKNOWN"
    [ -n "$MERGEABLE" ] || MERGEABLE="UNKNOWN"
    echo "    attempt $attempt: mergeStateStatus=$STATE mergeable=$MERGEABLE"
    if [ "$STATE" = "CLEAN" ] && [ "$MERGEABLE" = "MERGEABLE" ]; then
      return 0
    fi
    FAILED_CHECKS="$(failed_checks)"
    if [ -n "$FAILED_CHECKS" ]; then
      print_failed_checks_and_exit "$FAILED_CHECKS"
    fi
    if [ "$MERGEABLE" = "CONFLICTING" ] || [ "$STATE" = "DIRTY" ] || [ "$STATE" = "BEHIND" ] || [ "$STATE" = "CONFLICTING" ]; then
      echo "ERROR: PR #$PR_NUMBER is $STATE — has conflicts or is out of date with base." >&2
      echo "       Final merge state: mergeStateStatus=$STATE mergeable=$MERGEABLE." >&2
      echo "       Rebase or resolve conflicts on the PR branch before merging." >&2
      TOUCHSTONE_MERGE_FAILURE_REASON="not-mergeable"
      exit 1
    fi
    if [ "$attempt" -lt "$max_attempts" ]; then
      sleep_seconds="${MERGE_STATE_RETRY_DELAYS[$((attempt - 1))]:-30}"
      # Tests may set MERGE_PR_SLEEP_OVERRIDE=0 to exercise retry behavior
      # without waiting for the production backoff schedule.
      if [ -n "${MERGE_PR_SLEEP_OVERRIDE+x}" ]; then
        sleep_seconds="$MERGE_PR_SLEEP_OVERRIDE"
      fi
      sleep "$sleep_seconds"
    fi
    attempt=$((attempt + 1))
  done

  echo "ERROR: PR #$PR_NUMBER is not cleanly mergeable (state=$STATE mergeable=$MERGEABLE)." >&2
  echo "       Required checks may still be pending; waited $max_attempts merge-state attempts." >&2
  echo "       Inspect manually: gh pr view $PR_NUMBER --web" >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="not-mergeable"
  exit 1
}

wait_for_pr_head() {
  local expected_head="$1"
  local actual_head sleep_seconds

  for attempt in 1 2 3 4 5; do
    actual_head="$(gh pr view "$PR_NUMBER" --json headRefOid --jq '.headRefOid' 2>/dev/null || echo "")"
    if [ "$actual_head" = "$expected_head" ]; then
      return 0
    fi
    echo "    waiting for PR head update (attempt $attempt): ${actual_head:-unknown}"
    if [ "$attempt" -lt 5 ]; then
      sleep_seconds="${MERGE_PR_SLEEP_OVERRIDE:-2}"
      sleep "$sleep_seconds"
    fi
  done

  echo "ERROR: PR #$PR_NUMBER head did not update to reviewed commit $expected_head." >&2
  echo "       Last observed head: ${actual_head:-unknown}" >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="head-not-updated"
  exit 1
}

run_preflight_gate() {
  local base_ref="$1"
  local label="${2:-before merge review}"
  local event_mode="${3:-merge}"
  local head_sha cache_key_short

  if ! truthy "$PREFLIGHT_REQUIRED"; then
    echo "==> Preflight disabled by [review].preflight_required=false."
    return 0
  fi
  if truthy "${TOUCHSTONE_NO_PREFLIGHT:-false}"; then
    echo "==> Skipping preflight because TOUCHSTONE_NO_PREFLIGHT=1."
    return 0
  fi
  if ! declare -F touchstone_preflight_main >/dev/null 2>&1; then
    echo "==> Preflight helper not found at $PREFLIGHT_SCRIPT — skipping preflight."
    return 0
  fi

  head_sha="$(git rev-parse HEAD 2>/dev/null || echo "")"
  if preflight_cache_prepare "$base_ref" "$event_mode" && preflight_cache_hit; then
    cache_key_short="$(preflight_cache_short_key)"
    echo "==> Deterministic preflight clean (cached=true, key=$cache_key_short; $label, diff vs $base_ref)."
    touchstone_emit_event preflight_clean pr_number="$PR_NUMBER" head_sha="$head_sha" cached=true cache_key="$PREFLIGHT_CACHE_KEY"
    return 0
  fi

  echo "==> Running deterministic preflight $label (diff vs $base_ref) ..."
  touchstone_emit_event preflight_started pr_number="$PR_NUMBER" mode="$event_mode" cached=false
  if touchstone_preflight_main_sanitized --diff "$base_ref" "$(git rev-parse --show-toplevel)"; then
    head_sha="$(git rev-parse HEAD 2>/dev/null || echo "")"
    write_preflight_clean_cache
    if [ -n "$PREFLIGHT_CACHE_KEY" ]; then
      cache_key_short="$(preflight_cache_short_key)"
      echo "==> Deterministic preflight clean (cached=false, key=$cache_key_short)."
      touchstone_emit_event preflight_clean pr_number="$PR_NUMBER" head_sha="$head_sha" cached=false cache_key="$PREFLIGHT_CACHE_KEY"
    else
      echo "==> Deterministic preflight clean (cached=false)."
      touchstone_emit_event preflight_clean pr_number="$PR_NUMBER" head_sha="$head_sha" cached=false
    fi
    return 0
  fi

  echo "ERROR: Deterministic preflight failed; refusing to spend provider tokens on review." >&2
  echo "       Fix the preflight failure or set TOUCHSTONE_NO_PREFLIGHT=1 for an emergency bypass." >&2
  head_sha="$(git rev-parse HEAD 2>/dev/null || echo "")"
  touchstone_emit_event preflight_blocked pr_number="$PR_NUMBER" head_sha="$head_sha"
  TOUCHSTONE_MERGE_FAILURE_REASON="preflight-blocked"
  return 1
}

run_merge_review() {
  local current_branch current_worktree default_base_ref default_worktree local_head pr_head_branch pr_head_oid

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

  PR_HEAD_BRANCH="$pr_head_branch"
  REVIEWED_HEAD_OID="$pr_head_oid"
  current_branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
  if [ "$current_branch" = "$PR_HEAD_BRANCH" ]; then
    current_worktree="$(git rev-parse --show-toplevel 2>/dev/null || echo "")"
    default_worktree="$(worktree_path_for_branch "$DEFAULT_BRANCH" | head -n 1)"
    if [ -n "$current_worktree" ] && [ -n "$default_worktree" ] && [ "$current_worktree" != "$default_worktree" ]; then
      PR_WORKTREE_PATH="$current_worktree"
    fi
  else
    PR_WORKTREE_PATH="$(worktree_path_for_branch "$PR_HEAD_BRANCH" | head -n 1)"
  fi
  default_base_ref="origin/$DEFAULT_BRANCH"

  if [ "$BYPASS_REVIEW" = true ]; then
    echo "==> Refreshing $default_base_ref before reviewer bypass validation ..."
    if ! git fetch origin "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"; then
      echo "ERROR: Failed to refresh $default_base_ref before reviewer bypass validation." >&2
      exit 1
    fi
    if ! git rev-parse --verify --quiet "$default_base_ref^{commit}" >/dev/null; then
      echo "ERROR: Could not verify $default_base_ref before reviewer bypass validation." >&2
      exit 1
    fi
    if ! git cat-file -e "$pr_head_oid^{commit}" 2>/dev/null; then
      echo "==> Checking out PR #$PR_NUMBER head ($pr_head_branch) for reviewer bypass validation ..."
      gh pr checkout "$PR_NUMBER" --detach
    fi
    local current_merge_base
    if ! current_merge_base="$(git merge-base "$default_base_ref" "$pr_head_oid" 2>/dev/null)"; then
      echo "ERROR: Could not compute merge base for PR #$PR_NUMBER head against $default_base_ref." >&2
      exit 1
    fi
    BYPASS_MARKER_SOURCE=""
    BYPASS_MARKER_EVIDENCE=""
    if branch_has_clean_review_marker "$pr_head_branch" "$pr_head_oid" "$current_merge_base"; then
      BYPASS_MARKER_SOURCE="clean-review"
    elif [ "$ALLOW_FAIL_OPEN_MARKER" = true ]; then
      if ! bypass_reason_mentions_fail_open; then
        echo "ERROR: Refusing reviewer bypass for PR #$PR_NUMBER." >&2
        echo "       --allow-fail-open-marker requires a disclosure reason that cites the fail-open reviewer/provider outage." >&2
        exit 1
      fi
      if ! is_positive_integer "$TOUCHSTONE_FAIL_OPEN_BYPASS_WINDOW_HOURS"; then
        echo "ERROR: TOUCHSTONE_FAIL_OPEN_BYPASS_WINDOW_HOURS must be a positive integer." >&2
        exit 2
      fi
      if ! branch_has_recent_fail_open_marker "$pr_head_branch" "$pr_head_oid"; then
        echo "ERROR: Refusing reviewer bypass for PR #$PR_NUMBER." >&2
        echo "       No recent fail-open review-log marker matches branch '$pr_head_branch' at head '$pr_head_oid'." >&2
        echo "       Looked in: ${TOUCHSTONE_REVIEW_LOG:-<disabled>} (window: ${TOUCHSTONE_FAIL_OPEN_BYPASS_WINDOW_HOURS}h)." >&2
        echo "       Expected a FAIL_OPEN_* entry with detail 'fail-open:*' for the current branch head." >&2
        exit 1
      fi
      BYPASS_MARKER_SOURCE="fail-open"
    else
      echo "ERROR: Refusing reviewer bypass for PR #$PR_NUMBER." >&2
      echo "       No prior clean review marker matches branch '$pr_head_branch' at head '$pr_head_oid' and merge base '$current_merge_base'." >&2
      echo "       Run the reviewer cleanly once before using --bypass-with-disclosure, or pass --allow-fail-open-marker after a recent fail-open review-log event for this branch head." >&2
      exit 1
    fi
    touchstone_emit_event review_bypass pr_number="$PR_NUMBER" head_sha="$pr_head_oid" reason="$BYPASS_REASON" marker="$BYPASS_MARKER_SOURCE" evidence="$BYPASS_MARKER_EVIDENCE"
    print_bypass_banner
    record_bypass_comment
    record_bypass_audit_log
    return 0
  fi

  if truthy "${SKIP_REVIEW:-${SKIP_CODEX_REVIEW:-false}}"; then
    echo "==> Skipping merge review because SKIP_REVIEW is set."
    return 0
  fi

  if [ ! -f "$REVIEW_SCRIPT" ]; then
    echo "==> Review script not found at $REVIEW_SCRIPT — skipping review."
    return 0
  fi

  echo "==> Refreshing $default_base_ref for merge review ..."
  if ! git fetch origin "+refs/heads/$DEFAULT_BRANCH:refs/remotes/origin/$DEFAULT_BRANCH"; then
    echo "ERROR: Failed to refresh $default_base_ref before merge review." >&2
    exit 1
  fi
  if ! git rev-parse --verify --quiet "$default_base_ref^{commit}" >/dev/null; then
    echo "ERROR: Could not verify $default_base_ref before merge review." >&2
    exit 1
  fi

  # The reviewer reads the committed diff against the default base; uncommitted
  # changes in unrelated paths do not affect that view. Only refuse when at
  # least one dirty path overlaps the PR's diff against the default base, which
  # is the actual ambiguous-tree case. Refusing on any dirty path false-positives
  # whenever the operator has unrelated WIP they aren't ready to commit.
  local dirty_status diff_paths dirty_paths overlap
  dirty_status="$(git status --porcelain)"
  if [ -n "$dirty_status" ]; then
    if ! diff_paths="$(git diff --name-only "$default_base_ref"...HEAD 2>/dev/null | sort -u)"; then
      echo "ERROR: Could not compute diff against $default_base_ref to evaluate dirty-tree overlap." >&2
      exit 1
    fi
    # Parse `git status --porcelain` robustly: rename entries have the form
    # `R<space|index> old -> new`, others are `XY path`. We want the path that
    # actually exists in the working tree, which is the post-rename path.
    dirty_paths="$(printf '%s\n' "$dirty_status" \
      | awk '{
          line = substr($0, 4)
          idx = index(line, " -> ")
          if (idx > 0) {
            print substr(line, idx + 4)
          } else {
            print line
          }
        }' \
      | sort -u)"
    if [ -n "$diff_paths" ] && [ -n "$dirty_paths" ]; then
      overlap="$(comm -12 <(printf '%s\n' "$diff_paths") <(printf '%s\n' "$dirty_paths"))"
    else
      overlap=""
    fi
    if [ -n "$overlap" ]; then
      echo "ERROR: Working tree has uncommitted changes that overlap PR #$PR_NUMBER's diff vs $default_base_ref;" >&2
      echo "       refusing to run review against an ambiguous tree. Overlapping paths:" >&2
      printf '%s\n' "$overlap" | sed 's/^/         /' >&2
      exit 1
    fi
    if [ -n "$dirty_paths" ]; then
      echo "==> Working tree has uncommitted changes outside PR #$PR_NUMBER's diff vs $default_base_ref; proceeding."
    fi
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

  run_preflight_gate "$default_base_ref" "before merge review" "merge" || return $?

  echo "==> Running merge review ..."
  local review_rc=0
  local review_output_file
  local review_infra_failure
  local reviewed_head_after
  review_output_file="$(mktemp -t touchstone-merge-review.XXXXXX.txt)"
  REVIEW_SUMMARY_FILE="$(git rev-parse --git-path "touchstone/review-summary-pr-${PR_NUMBER}.json" 2>/dev/null || echo "")"
  if [ -n "$REVIEW_SUMMARY_FILE" ]; then
    mkdir -p "$(dirname "$REVIEW_SUMMARY_FILE")" 2>/dev/null || true
    rm -f "$REVIEW_SUMMARY_FILE" 2>/dev/null || true
  fi
  touchstone_emit_event review_started pr_number="$PR_NUMBER" mode=fix
  set +e
  CODEX_REVIEW_BASE="$default_base_ref" \
    CODEX_REVIEW_BRANCH_NAME="$pr_head_branch" \
    CODEX_REVIEW_PR_NUMBER="$PR_NUMBER" \
    CODEX_REVIEW_FORCE=1 \
    CODEX_REVIEW_MODE=fix \
    CODEX_REVIEW_ON_ERROR=fail-closed \
    TOUCHSTONE_PREFLIGHT_ALREADY_RAN=1 \
    CODEX_REVIEW_SUMMARY_FILE="$REVIEW_SUMMARY_FILE" \
    bash "$REVIEW_SCRIPT" 2>&1 | tee "$review_output_file"
  review_rc="${PIPESTATUS[0]}"
  set -e

  if [ "$review_rc" -eq 0 ]; then
    rm -f "$review_output_file"
    reviewed_head_after="$(git rev-parse HEAD 2>/dev/null || echo "")"
    if [ -z "$reviewed_head_after" ]; then
      echo "ERROR: Could not resolve reviewed HEAD after merge review." >&2
      TOUCHSTONE_MERGE_FAILURE_REASON="missing-reviewed-head"
      return 1
    fi
    REVIEWED_HEAD_OID="$reviewed_head_after"
    if [ "$reviewed_head_after" != "$pr_head_oid" ]; then
      echo "==> Merge review changed HEAD:"
      echo "    before: $pr_head_oid"
      echo "    after:  $reviewed_head_after"
      echo "==> Running deterministic postflight after review fixes ..."
      run_preflight_gate "$default_base_ref" "after review fixes" "post-review" || return $?
      echo "==> Pushing review fix commit(s) to PR branch $pr_head_branch ..."
      if ! git push origin "HEAD:refs/heads/$pr_head_branch"; then
        echo "ERROR: Failed to push review fix commit(s) to PR branch $pr_head_branch." >&2
        TOUCHSTONE_MERGE_FAILURE_REASON="push-review-fixes"
        return 1
      fi
      wait_for_pr_head "$reviewed_head_after"
      wait_for_clean_merge_state
    fi
    touchstone_emit_event review_clean pr_number="$PR_NUMBER" head_sha="$reviewed_head_after"
    return 0
  fi

  echo "" >&2
  echo "ERROR: Merge review exited $review_rc; merge-gate review fails closed." >&2
  review_infra_failure=false
  if review_failure_is_infra "$review_rc" "$review_output_file"; then
    review_infra_failure=true
    echo "       No concrete review findings were reported; this is a provider/infrastructure outage path." >&2
    print_review_infra_retry_guidance
  else
    echo "       Concrete review findings were reported; fix the findings, then rerun the merge gate." >&2
  fi
  echo "       Emergency bypass requires an explicit --bypass-with-disclosure reason and either a matching prior clean review marker or --allow-fail-open-marker with recent fail-open evidence." >&2
  post_review_failure_comment "$review_output_file" "$review_infra_failure"

  rm -f "$review_output_file"
  touchstone_emit_event review_blocked pr_number="$PR_NUMBER" head_sha="$pr_head_oid"
  TOUCHSTONE_MERGE_FAILURE_REASON="review-blocked"
  return "$review_rc"
}

load_merge_review_config

# 1. Sanity check the PR exists and is open.
if ! PR_STATE="$(gh pr view "$PR_NUMBER" --json state --jq '.state')"; then
  echo "ERROR: Failed to inspect PR #$PR_NUMBER state with gh." >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="pr-state"
  exit 1
fi
if [ "$PR_STATE" != "OPEN" ]; then
  echo "ERROR: PR #$PR_NUMBER is not open (state: $PR_STATE)." >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="pr-not-open"
  exit 1
fi

# 2. Check mergeability with retries (GitHub's status can lag after a push).
wait_for_clean_merge_state

# 3. Run AI review as the merge gate.
run_merge_review

# 4. Squash-merge and delete the branch.
echo "==> Squash-merging PR #$PR_NUMBER ..."
if [ -z "$REVIEWED_HEAD_OID" ]; then
  echo "ERROR: Cannot merge PR #$PR_NUMBER because no reviewed head commit was recorded." >&2
  TOUCHSTONE_MERGE_FAILURE_REASON="missing-reviewed-head"
  exit 1
fi
gh_merge_exit=0
if [ "$BYPASS_REVIEW" = true ]; then
  gh pr merge "$PR_NUMBER" --squash --delete-branch --match-head-commit "$REVIEWED_HEAD_OID" \
    --body "Reviewer-bypass: $BYPASS_REASON" || gh_merge_exit=$?
else
  gh pr merge "$PR_NUMBER" --squash --delete-branch --match-head-commit "$REVIEWED_HEAD_OID" \
    || gh_merge_exit=$?
fi

# `gh pr merge --delete-branch` does the squash AND tries to delete the
# local feature branch. The local-delete fails when the branch is checked
# out in the current worktree (the common case for parallel-worktree work).
# When that happens, the remote merge succeeded server-side — only the
# local cleanup didn't. Verify by asking the API; if MERGED, treat as
# success with a warning so the script doesn't claim the PR failed.
if [ "$gh_merge_exit" -ne 0 ]; then
  pr_state="$(gh pr view "$PR_NUMBER" --json state --jq '.state' 2>/dev/null || echo "")"
  if [ "$pr_state" = "MERGED" ]; then
    echo "WARNING: gh pr merge exited $gh_merge_exit, but PR #$PR_NUMBER is MERGED on GitHub."
    echo "         Likely cause: local feature branch is checked out in a worktree,"
    echo "         or stale worktree metadata still records it there. Remote branch is gone."
    echo "         Use 'git worktree remove <path>' or 'bash scripts/cleanup-worktrees.sh --execute' for normal cleanup."
    echo "         If the directory was deleted directly, run 'git worktree prune' from a remaining checkout."
  else
    echo "ERROR: gh pr merge exited $gh_merge_exit and PR #$PR_NUMBER is not MERGED." >&2
    TOUCHSTONE_MERGE_FAILURE_REASON="gh-pr-merge"
    exit "$gh_merge_exit"
  fi
fi

MERGED_AT="$(gh pr view "$PR_NUMBER" --json mergedAt --jq '.mergedAt // empty' 2>/dev/null || echo "")"
touchstone_emit_event merged pr_number="$PR_NUMBER" merged_at="$MERGED_AT" head_sha="$REVIEWED_HEAD_OID"
post_clean_review_comment "$REVIEW_SUMMARY_FILE"
post_findings_history_comment "$PR_HEAD_BRANCH"

# Record squash-merge metadata for cleanup-branches.sh. The merge has
# succeeded on GitHub; this is best-effort persistence for later cleanup.
SQUASH_COMMIT_OID="$(gh pr view "$PR_NUMBER" --json mergeCommit --jq '.mergeCommit.oid' 2>/dev/null || echo "")"
record_squash_merge "$PR_HEAD_BRANCH" "$PR_NUMBER" "$REVIEWED_HEAD_OID" "$SQUASH_COMMIT_OID"

# 5. Sync local default branch.
sync_default_branch_after_merge

# 6. Cortex post-merge hook (T1.9). Fires only when the project meets the
# activation criteria documented in scripts/cortex-pr-merged-hook.sh.
# Activation is the hook's job — we always invoke and let it self-gate.
# The hook may produce a follow-up journal branch/PR; the journal commit
# is created with --no-verify so it doesn't recurse through this script's
# review gates. Failures inside the hook surface as visible stderr; we
# don't fail the overall merge over a journal-write hiccup.
CORTEX_HOOK_SCRIPT=""
for candidate_hook in \
  "$SCRIPT_DIR/cortex-pr-merged-hook.sh" \
  "$(git rev-parse --show-toplevel 2>/dev/null)/scripts/cortex-pr-merged-hook.sh"; do
  if [ -n "$candidate_hook" ] && [ -f "$candidate_hook" ]; then
    CORTEX_HOOK_SCRIPT="$candidate_hook"
    break
  fi
done

if [ -n "$CORTEX_HOOK_SCRIPT" ]; then
  hook_status=0
  TOUCHSTONE_MERGED_PR="$PR_NUMBER" bash "$CORTEX_HOOK_SCRIPT" || hook_status=$?
  if [ "$hook_status" -ne 0 ]; then
    echo "WARNING: cortex-pr-merged-hook exited $hook_status (see above)." >&2
    echo "         The PR merged cleanly; only the auto-draft journal step had a problem." >&2
  fi
fi

cleanup_local_pr_branch_after_merge
cleanup_pr_worktree_after_merge

echo "==> Done."
