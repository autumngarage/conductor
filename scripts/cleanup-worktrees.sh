#!/usr/bin/env bash
#
# scripts/cleanup-worktrees.sh — safe git worktree hygiene tool.
#
# Usage:
#   bash scripts/cleanup-worktrees.sh              # dry-run (default)
#   bash scripts/cleanup-worktrees.sh --execute    # remove clean candidates
#   bash scripts/cleanup-worktrees.sh --unlock-stale --execute
#                                                     unlock dead-PID locks, then remove safe candidates
#   bash scripts/cleanup-worktrees.sh --force      # remove candidates even if dirty
#
# Safety guarantees:
#   - Default mode is DRY RUN.
#   - The main worktree is never removed.
#   - The current worktree is never removed.
#   - Clean worktrees are removable only when their branch is merged or
#     tree-equivalent to the default branch, or when the branch is gone.
#   - Dirty worktrees are refused unless --force is explicit.
#   - Locked worktrees are refused unless their lock reason contains a dead
#     `pid <number>` and --unlock-stale is explicit.
#   - git worktree prune is previewed before any actual prune.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if [ -f "$SCRIPT_DIR/../lib/events.sh" ]; then
  # shellcheck source=../lib/events.sh
  source "$SCRIPT_DIR/../lib/events.sh"
else
  touchstone_emit_event() { :; }
fi

DRY_RUN=1
FORCE=0
UNLOCK_STALE=0

usage() {
  awk 'NR>2 && !/^#/ { exit } NR>2 { sub(/^# ?/, ""); print }' "$0"
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --execute | -x)
      DRY_RUN=0
      shift
      ;;
    --force)
      DRY_RUN=0
      FORCE=1
      shift
      ;;
    --unlock-stale)
      UNLOCK_STALE=1
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h | --help)
      usage
      exit 0
      ;;
    *)
      echo "ERROR: unknown argument '$1'" >&2
      exit 1
      ;;
  esac
done

if ! REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null)"; then
  echo "ERROR: cleanup-worktrees.sh must run inside a git repository." >&2
  exit 1
fi

cd "$REPO_ROOT"

resolve_default_ref() {
  local origin_head ref branch
  origin_head="$(git symbolic-ref -q refs/remotes/origin/HEAD 2>/dev/null || true)"
  if [ -n "$origin_head" ]; then
    ref="$origin_head"
    ref="${ref#refs/remotes/}"
    if git rev-parse --verify --quiet "$ref" >/dev/null; then
      printf '%s\n' "$ref"
      return 0
    fi
  fi

  for branch in main master; do
    if git rev-parse --verify --quiet "origin/$branch" >/dev/null; then
      printf '%s\n' "origin/$branch"
      return 0
    fi
    if git rev-parse --verify --quiet "$branch" >/dev/null; then
      printf '%s\n' "$branch"
      return 0
    fi
  done

  echo "ERROR: could not resolve a default branch ref (origin/HEAD, main, or master)." >&2
  return 1
}

is_fully_applied() {
  local upstream="$1"
  local branch="$2"
  local base file

  base="$(git merge-base "$upstream" "$branch" 2>/dev/null)" || return 1
  [ -z "$base" ] && return 1

  while IFS= read -r -d '' file; do
    [ -z "$file" ] && continue
    git diff --quiet "$upstream" "$branch" -- "$file" 2>/dev/null || return 1
  done < <(git diff --name-only --no-renames -z "$base" "$branch" 2>/dev/null)

  return 0
}

DEFAULT_REF="$(resolve_default_ref)"
CURRENT_WORKTREE="$(git rev-parse --show-toplevel)"

WORKTREE_LIST="$(git worktree list --porcelain)"
MAIN_WORKTREE="$(printf '%s\n' "$WORKTREE_LIST" | awk '/^worktree /{print substr($0, 10); exit}')"

CANDIDATE_PATHS=()
FORCE_PATHS=()
UNLOCK_STALE_PATHS=()

echo "==> Worktrees"
echo "    default ref: $DEFAULT_REF"

current_path=""
current_head=""
current_branch=""
current_locked=0
current_lock_reason=""

pid_is_alive() {
  local pid="$1"

  [ -n "$pid" ] || return 1
  case "$pid" in
    *[!0-9]*) return 1 ;;
  esac

  if kill -0 "$pid" 2>/dev/null; then
    return 0
  fi
  # If kill(2) is denied but the process exists, treat it as alive.
  ps -p "$pid" >/dev/null 2>&1
}

pid_is_current_shell_parent() {
  local pid="$1"

  [ -n "$pid" ] || return 1
  [ -n "${PPID:-}" ] || return 1
  [ "$pid" = "$PPID" ]
}

classify_lock() {
  local reason="$1"
  local pid

  if [[ "$reason" =~ [Pp][Ii][Dd][[:space:]]+([0-9]+) ]]; then
    pid="${BASH_REMATCH[1]}"
    if pid_is_current_shell_parent "$pid"; then
      printf 'stale|%s|stale (pid %s is current shell parent)' "$pid" "$pid"
    elif pid_is_alive "$pid"; then
      printf 'alive|%s|alive (pid %s)' "$pid" "$pid"
    else
      printf 'stale|%s|stale (pid %s dead)' "$pid" "$pid"
    fi
  else
    printf 'unknown||present, no PID'
  fi
}

path_needs_stale_unlock() {
  local target="$1"
  local path

  for path in ${UNLOCK_STALE_PATHS[@]+"${UNLOCK_STALE_PATHS[@]}"}; do
    [ "$path" = "$target" ] && return 0
  done
  return 1
}

flush_worktree() {
  [ -n "$current_path" ] || return 0

  local branch_label dirty_status dirty_label reason removable branch_name
  local lock_state lock_pid lock_label unlock_before_remove
  branch_label="${current_branch:-detached}"
  lock_state="none"
  lock_pid=""
  lock_label=""
  unlock_before_remove=0

  if dirty_status="$(git -C "$current_path" status --porcelain 2>/dev/null)"; then
    if [ -n "$dirty_status" ]; then
      dirty_label="dirty"
    else
      dirty_label="clean"
    fi
  else
    dirty_label="missing"
  fi

  printf '  - path: %s\n' "$current_path"
  printf '    branch: %s\n' "$branch_label"
  printf '    head: %s\n' "${current_head:-unknown}"
  printf '    status: %s\n' "$dirty_label"
  if [ "$current_locked" -eq 1 ]; then
    IFS='|' read -r lock_state lock_pid lock_label <<<"$(classify_lock "$current_lock_reason")"
    printf '    lock: %s\n' "$lock_label"
  fi

  removable=0
  reason=""

  if [ "$current_path" = "$MAIN_WORKTREE" ]; then
    reason="main worktree"
  elif [ "$current_path" = "$CURRENT_WORKTREE" ]; then
    reason="current worktree"
  elif [ "$dirty_label" = "dirty" ] && [ "$FORCE" -ne 1 ]; then
    reason="dirty; use --force to remove"
  elif [ "$dirty_label" = "missing" ]; then
    reason="missing; git worktree prune handles this"
  elif [ "$current_locked" -eq 1 ] && [ "$lock_state" = "alive" ]; then
    reason="locked by live process (pid $lock_pid); not removing"
  elif [ "$current_locked" -eq 1 ] && [ "$lock_state" = "unknown" ]; then
    reason="locked without PID; unlock manually after inspection"
  elif [ "$current_locked" -eq 1 ] && [ "$lock_state" = "stale" ] && [ "$UNLOCK_STALE" -ne 1 ]; then
    reason="locked by dead process (pid $lock_pid); pass --unlock-stale --execute to remove"
  else
    if [ "$current_locked" -eq 1 ] && [ "$lock_state" = "stale" ]; then
      unlock_before_remove=1
    fi

    if [ -z "$current_branch" ]; then
      if [ -z "$current_head" ] || [ "$current_head" = "unknown" ]; then
        reason="detached HEAD missing; investigate manually"
      elif git merge-base --is-ancestor "$current_head" "$DEFAULT_REF" 2>/dev/null; then
        removable=1
        reason="detached HEAD merged into default"
      elif is_fully_applied "$DEFAULT_REF" "$current_head"; then
        removable=1
        reason="detached HEAD tree-equivalent to default"
      else
        reason="detached HEAD has unique work; use --force to remove"
      fi
    else
      branch_name="${current_branch#refs/heads/}"
      if ! git show-ref --verify --quiet "refs/heads/$branch_name"; then
        removable=1
        reason="branch gone"
      elif git merge-base --is-ancestor "$branch_name" "$DEFAULT_REF" 2>/dev/null; then
        removable=1
        reason="branch merged into default"
      elif is_fully_applied "$DEFAULT_REF" "$branch_name"; then
        removable=1
        reason="branch tree-equivalent to default"
      else
        reason="branch has unique work"
      fi
    fi
  fi

  printf '    decision: %s\n' "$reason"
  if [ "$removable" -eq 1 ]; then
    CANDIDATE_PATHS+=("$current_path")
    if [ "$dirty_label" = "dirty" ]; then
      FORCE_PATHS+=("$current_path")
    fi
    if [ "$unlock_before_remove" -eq 1 ]; then
      UNLOCK_STALE_PATHS+=("$current_path")
    fi
  fi
}

while IFS= read -r line || [ -n "$line" ]; do
  case "$line" in
    worktree\ *)
      flush_worktree
      current_path="${line#worktree }"
      current_head=""
      current_branch=""
      current_locked=0
      current_lock_reason=""
      ;;
    HEAD\ *)
      current_head="${line#HEAD }"
      ;;
    branch\ *)
      current_branch="${line#branch }"
      ;;
    locked\ *)
      current_locked=1
      current_lock_reason="${line#locked }"
      ;;
    locked)
      current_locked=1
      current_lock_reason=""
      ;;
    "")
      flush_worktree
      current_path=""
      current_head=""
      current_branch=""
      current_locked=0
      current_lock_reason=""
      ;;
  esac
done <<<"$WORKTREE_LIST"
flush_worktree

echo ""
echo "==> Prune preview"
git worktree prune --dry-run --verbose || true

if [ "${#CANDIDATE_PATHS[@]}" -eq 0 ]; then
  echo ""
  echo "==> No removable worktrees found."
  exit 0
fi

echo ""
echo "==> Removable worktrees"
for path in "${CANDIDATE_PATHS[@]}"; do
  echo "  - $path"
done

if [ "$DRY_RUN" -eq 1 ]; then
  echo ""
  echo "==> Dry run. Pass --execute to remove the clean candidates listed above."
  exit 0
fi

echo ""
echo "==> Removing worktrees"
for path in "${CANDIDATE_PATHS[@]}"; do
  touchstone_emit_event cleanup_started worktree_path="$path"
  if path_needs_stale_unlock "$path"; then
    if ! git worktree unlock "$path"; then
      touchstone_emit_event cleanup_done worktree_path="$path" result=failed
      exit 1
    fi
    echo "    unlocked stale lock: $path"
  fi
  if [ "$FORCE" -eq 1 ]; then
    if ! git worktree remove --force "$path"; then
      touchstone_emit_event cleanup_done worktree_path="$path" result=failed
      exit 1
    fi
  else
    if ! git worktree remove "$path"; then
      touchstone_emit_event cleanup_done worktree_path="$path" result=failed
      exit 1
    fi
  fi
  echo "    removed: $path"
  touchstone_emit_event cleanup_done worktree_path="$path" result=removed
done

echo ""
echo "==> Pruning stale worktree metadata"
git worktree prune --verbose

echo ""
echo "==> Done. Run without --execute next time to dry-run."
