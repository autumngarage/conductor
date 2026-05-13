#!/usr/bin/env bash
#
# lib/events.sh — optional NDJSON lifecycle events for UI clients.
#
# Event schema:
#   worktree_created: branch, worktree_path, base_branch, repo_root
#   pr_opened: pr_url, pr_number, branch, base_branch, head_sha
#   review_started: pr_number, mode
#   review_clean: pr_number, head_sha
#   review_blocked: pr_number, head_sha
#   review_bypass: pr_number, reason
#   merged: pr_number, merged_at, head_sha
#   cleanup_started: worktree_path
#   cleanup_done: worktree_path, result
#   failed: phase, reason, pr_number
#   worker_spawned: branch, worktree_path, task
#   worker_state_changed: worktree_path, from, to
#   worker_abandoned: worktree_path, branch

touchstone_json_string() {
  local value="${1-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//$'\n'/\\n}"
  value="${value//$'\t'/\\t}"
  value="${value//$'\r'/\\r}"
  printf '"%s"' "$value"
}

touchstone_event_value() {
  local value="${1-}"
  case "$value" in
    '' | *[!0-9]*)
      touchstone_json_string "$value"
      ;;
    *)
      printf '%s' "$value"
      ;;
  esac
}

touchstone_emit_event() {
  local event="${1-}"
  shift || true

  [ -n "${TOUCHSTONE_EVENTS_FILE:-}" ] || return 0
  [ -n "$event" ] || return 0

  local events_file="$TOUCHSTONE_EVENTS_FILE"
  local script_name="${TOUCHSTONE_EVENTS_SCRIPT:-$(basename "$0")}"
  local ts line arg key value

  ts="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || printf '')"
  line="{\"event\":$(touchstone_json_string "$event"),\"ts\":$(touchstone_json_string "$ts"),\"script\":$(touchstone_json_string "$script_name")"

  for arg in "$@"; do
    key="${arg%%=*}"
    value="${arg#*=}"
    [ -n "$key" ] || continue
    line="${line},\"$key\":$(touchstone_event_value "$value")"
  done
  line="${line}}"

  if ! printf '%s\n' "$line" >>"$events_file" 2>/dev/null; then
    printf 'WARNING: [events] could not append to %s\n' "$events_file" >&2
  fi
}
