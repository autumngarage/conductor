#!/usr/bin/env bash
#
# lib/script-sync-guard.sh — keep project-local workflow scripts current.
#
# This guard is sourced by high-risk project-local scripts before they do PR
# work. If the project was bootstrapped from an older Touchstone install, it
# ships a dedicated Touchstone update PR from the default branch when that is
# safe, or updates the current feature branch and re-execs the script.

touchstone_script_sync_guard_truthy() {
  case "$(printf '%s' "${1:-}" | tr '[:upper:]' '[:lower:]')" in
    1 | true | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

touchstone_script_sync_guard_resolve_script() {
  local script_path="$1"
  local script_dir script_name

  case "$script_path" in
    /*) ;;
    *)
      script_dir="$(dirname "$script_path")"
      script_name="$(basename "$script_path")"
      script_dir="$(cd "$script_dir" 2>/dev/null && pwd -P)" || return 1
      script_path="$script_dir/$script_name"
      ;;
  esac

  printf '%s\n' "$script_path"
}

touchstone_script_sync_guard_default_branch() {
  local project_dir="$1"
  local default_branch=""

  default_branch="$(git -C "$project_dir" symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null \
    | sed 's#^origin/##' || true)"
  if [ -z "$default_branch" ]; then
    if git -C "$project_dir" show-ref --verify --quiet refs/heads/main; then
      default_branch="main"
    elif git -C "$project_dir" show-ref --verify --quiet refs/heads/master; then
      default_branch="master"
    fi
  fi

  printf '%s\n' "$default_branch"
}

touchstone_script_sync_guard() {
  local script_path="${1:-}"
  shift || true

  if [ -z "$script_path" ]; then
    return 0
  fi
  if touchstone_script_sync_guard_truthy "${TOUCHSTONE_NO_SCRIPT_SYNC:-}" \
    || touchstone_script_sync_guard_truthy "${TOUCHSTONE_SCRIPT_SYNC_GUARD_DISABLE:-}" \
    || touchstone_script_sync_guard_truthy "${TOUCHSTONE_NO_AUTO_UPDATE:-}" \
    || touchstone_script_sync_guard_truthy "${TOUCHSTONE_NO_AUTO_PROJECT_SYNC:-}" \
    || touchstone_script_sync_guard_truthy "${TOUCHSTONE_SCRIPT_SYNC_GUARD_DONE:-}"; then
    return 0
  fi
  if ! command -v touchstone >/dev/null 2>&1; then
    return 0
  fi

  local resolved_script script_dir project_dir
  resolved_script="$(touchstone_script_sync_guard_resolve_script "$script_path")" || return 0
  script_dir="$(cd "$(dirname "$resolved_script")" 2>/dev/null && pwd -P)" || return 0
  project_dir="$(cd "$script_dir/.." 2>/dev/null && pwd -P)" || return 0
  [ -f "$project_dir/.touchstone-version" ] || return 0

  if ! git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    return 0
  fi

  local check_output check_file
  check_file="$(mktemp -t touchstone-script-sync-check.XXXXXX)"
  if ! (cd "$project_dir" && touchstone update --check) >"$check_file" 2>&1; then
    echo "ERROR: Touchstone script sync check failed before running $resolved_script:" >&2
    sed 's/^/       /' "$check_file" >&2
    rm -f "$check_file"
    exit 2
  fi
  check_output="$(cat "$check_file")"
  rm -f "$check_file"

  case "$check_output" in
    *"Already up to date."*) return 0 ;;
    *"Needs update."*) ;;
    *) return 0 ;;
  esac

  local current_branch default_branch update_file
  current_branch="$(git -C "$project_dir" branch --show-current 2>/dev/null || true)"
  default_branch="$(touchstone_script_sync_guard_default_branch "$project_dir")"
  if [ -n "$current_branch" ] \
    && { [ "$current_branch" = "$default_branch" ] || [ "$current_branch" = "main" ] || [ "$current_branch" = "master" ]; }; then
    local ship_file
    ship_file="$(mktemp -t touchstone-script-sync-ship.XXXXXX)"
    echo "==> Touchstone script sync: project-local workflow files are stale; shipping a Touchstone update PR before continuing." >&2
    if ! (cd "$project_dir" && touchstone update --ship) >"$ship_file" 2>&1; then
      echo "ERROR: Touchstone script sync ship failed before running $resolved_script:" >&2
      sed 's/^/       /' "$ship_file" >&2
      echo "       Retry: cd $project_dir && touchstone update --ship" >&2
      rm -f "$ship_file"
      exit 2
    fi
    sed 's/^/    /' "$ship_file" >&2
    rm -f "$ship_file"

    echo "==> Touchstone script sync: restarting $resolved_script" >&2
    TOUCHSTONE_SCRIPT_SYNC_GUARD_DONE=1 exec bash "$resolved_script" "$@"
  fi
  if [ -z "$current_branch" ]; then
    echo "ERROR: Touchstone project files are stale, but $resolved_script is running from a detached HEAD." >&2
    echo "       Check out a feature branch, run touchstone update --in-place, then rerun this command." >&2
    exit 2
  fi

  update_file="$(mktemp -t touchstone-script-sync-update.XXXXXX)"
  echo "==> Touchstone script sync: project-local workflow files are stale; updating this feature branch before continuing." >&2
  if [ -n "$default_branch" ]; then
    echo "    Cleaner dedicated update path: git switch $default_branch && touchstone update --ship" >&2
  else
    echo "    Cleaner dedicated update path: touchstone update --ship from the default branch" >&2
  fi
  if ! (cd "$project_dir" && touchstone update --in-place) >"$update_file" 2>&1; then
    echo "ERROR: Touchstone script sync update failed before running $resolved_script:" >&2
    sed 's/^/       /' "$update_file" >&2
    echo "       Retry: cd $project_dir && touchstone update --in-place" >&2
    rm -f "$update_file"
    exit 2
  fi
  sed 's/^/    /' "$update_file" >&2
  rm -f "$update_file"

  echo "==> Touchstone script sync: restarting $resolved_script" >&2
  TOUCHSTONE_SCRIPT_SYNC_GUARD_DONE=1 exec bash "$resolved_script" "$@"
}
