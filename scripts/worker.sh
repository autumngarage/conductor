#!/usr/bin/env bash
#
# scripts/worker.sh — first-class Touchstone worker lifecycle commands.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TOUCHSTONE_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# shellcheck source=../lib/worker-state.sh
source "$TOUCHSTONE_ROOT/lib/worker-state.sh"
if [ -f "$TOUCHSTONE_ROOT/lib/events.sh" ]; then
  # shellcheck source=../lib/events.sh
  source "$TOUCHSTONE_ROOT/lib/events.sh"
else
  touchstone_emit_event() { :; }
  touchstone_json_string() {
    local value="${1-}"
    value="${value//\\/\\\\}"
    value="${value//\"/\\\"}"
    value="${value//$'\n'/\\n}"
    value="${value//$'\t'/\\t}"
    value="${value//$'\r'/\\r}"
    printf '"%s"' "$value"
  }
fi

usage() {
  cat <<'EOF'
Usage:
  touchstone worker spawn --task "<description>" --type fix|feat|chore|refactor|docs [--json]
  touchstone worker status --worktree <path> [--json]
  touchstone worker ship --worktree <path> [--auto-merge] [--cleanup] [--events-json <path>]
  touchstone worker abandon --worktree <path> [--dry-run] [--force]
  touchstone worker list [--repo <path>] [--json]
EOF
}

json_bool() {
  if [ -n "${1:-}" ]; then
    printf 'true'
  else
    printf 'false'
  fi
}

json_field() {
  local key="$1" value="$2"
  printf '"%s":%s' "$key" "$(touchstone_json_string "$value")"
}

json_number_or_null_field() {
  local key="$1" value="$2"
  if [ -n "$value" ]; then
    printf '"%s":%s' "$key" "$value"
  else
    printf '"%s":null' "$key"
  fi
}

sanitize_task_slug() {
  local raw="$1" slug
  slug="$(printf '%s' "$raw" \
    | tr '[:upper:]' '[:lower:]' \
    | sed 's/[^a-z0-9]/-/g; s/--*/-/g; s/^-//; s/-$//')"
  printf '%s' "${slug:-worker-task}"
}

require_worker_type() {
  case "$1" in
    fix | feat | chore | refactor | docs) return 0 ;;
    *)
      echo "ERROR: --type must be one of fix, feat, chore, refactor, docs." >&2
      return 1
      ;;
  esac
}

repo_root_or_die() {
  if ! git rev-parse --show-toplevel 2>/dev/null; then
    echo "ERROR: must run inside a git repository." >&2
    return 1
  fi
}

worker_branch() {
  git -C "$1" rev-parse --abbrev-ref HEAD 2>/dev/null || true
}

worker_head_sha() {
  git -C "$1" rev-parse HEAD 2>/dev/null || true
}

worker_has_uncommitted() {
  git -C "$1" status --porcelain 2>/dev/null || true
}

worker_pr_field() {
  local branch="$1" field="$2"
  command -v gh >/dev/null 2>&1 || return 0
  gh pr list --head "$branch" --state all --json number,url,state,mergedAt \
    --jq ".[0].$field // empty" 2>/dev/null || true
}

worker_status_json() {
  local worktree_path="$1"
  local state branch head_sha has_uncommitted pr_number pr_url merged_at

  state="$(derive_worker_state "$worktree_path")"
  branch=""
  head_sha=""
  has_uncommitted=""
  pr_number=""
  pr_url=""
  merged_at=""

  if [ -d "$worktree_path" ]; then
    branch="$(worker_branch "$worktree_path")"
    head_sha="$(worker_head_sha "$worktree_path")"
    has_uncommitted="$(worker_has_uncommitted "$worktree_path")"
    if [ -n "$branch" ] && [ "$branch" != "HEAD" ]; then
      pr_number="$(worker_pr_field "$branch" number)"
      pr_url="$(worker_pr_field "$branch" url)"
      merged_at="$(worker_pr_field "$branch" mergedAt)"
    fi
  fi

  printf '{'
  json_field state "$state"
  printf ','
  json_field branch "$branch"
  printf ','
  json_field head_sha "$head_sha"
  printf ',"has_uncommitted":'
  json_bool "$has_uncommitted"
  printf ','
  json_number_or_null_field pr_number "$pr_number"
  if [ -n "$pr_url" ]; then
    printf ','
    json_field pr_url "$pr_url"
  fi
  if [ -n "$merged_at" ]; then
    printf ','
    json_field merged_at "$merged_at"
  fi
  printf '}\n'
}

cmd_spawn() {
  local task="" type="" json=false slug branch repo_root worktree_path base_ref base_branch output

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --task)
        [ "$#" -ge 2 ] || {
          echo "ERROR: --task requires a value." >&2
          return 2
        }
        task="$2"
        shift 2
        ;;
      --type)
        [ "$#" -ge 2 ] || {
          echo "ERROR: --type requires a value." >&2
          return 2
        }
        type="$2"
        shift 2
        ;;
      --json)
        json=true
        shift
        ;;
      -h | --help)
        usage
        return 0
        ;;
      *)
        echo "ERROR: unknown worker spawn argument '$1'." >&2
        return 2
        ;;
    esac
  done

  [ -n "$task" ] || {
    echo "ERROR: worker spawn requires --task." >&2
    return 2
  }
  [ -n "$type" ] || {
    echo "ERROR: worker spawn requires --type." >&2
    return 2
  }
  require_worker_type "$type"

  repo_root="$(repo_root_or_die)"
  slug="$(sanitize_task_slug "$task")"
  branch="$type/$slug"
  base_ref="$(cd "$repo_root" && touchstone_worker_default_ref)"
  base_branch="${base_ref#origin/}"

  output="$(cd "$repo_root" && bash "$TOUCHSTONE_ROOT/scripts/spawn-worktree.sh" "$branch")"
  worktree_path="$(printf '%s\n' "$output" | awk '/^[[:space:]]*path:[[:space:]]*/ { sub(/^[[:space:]]*path:[[:space:]]*/, ""); value=$0 } END { print value }')"
  if [ -n "$worktree_path" ]; then
    worktree_path="$(cd "$repo_root" && cd "$worktree_path" && pwd)"
  fi

  touchstone_emit_event worker_spawned branch="$branch" worktree_path="$worktree_path" task="$task"

  if [ "$json" = true ]; then
    printf '{'
    json_field branch "$branch"
    printf ','
    json_field worktree_path "$worktree_path"
    printf ','
    json_field base_branch "$base_branch"
    printf '}\n'
  else
    printf '%s\n' "$output"
  fi
}

cmd_status() {
  local worktree_path="" json=false state branch head_sha has_uncommitted

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --worktree)
        [ "$#" -ge 2 ] || {
          echo "ERROR: --worktree requires a path." >&2
          return 2
        }
        worktree_path="$2"
        shift 2
        ;;
      --json)
        json=true
        shift
        ;;
      -h | --help)
        usage
        return 0
        ;;
      *)
        echo "ERROR: unknown worker status argument '$1'." >&2
        return 2
        ;;
    esac
  done

  [ -n "$worktree_path" ] || {
    echo "ERROR: worker status requires --worktree." >&2
    return 2
  }

  if [ "$json" = true ]; then
    worker_status_json "$worktree_path"
    return 0
  fi

  state="$(derive_worker_state "$worktree_path")"
  branch=""
  head_sha=""
  has_uncommitted=""
  if [ -d "$worktree_path" ]; then
    branch="$(worker_branch "$worktree_path")"
    head_sha="$(worker_head_sha "$worktree_path")"
    has_uncommitted="$(worker_has_uncommitted "$worktree_path")"
  fi
  echo "Worker state: $state"
  [ -n "$branch" ] && echo "Branch: $branch"
  [ -n "$head_sha" ] && echo "Head: $head_sha"
  if [ -n "$has_uncommitted" ]; then
    echo "Uncommitted changes: yes"
  else
    echo "Uncommitted changes: no"
  fi
}

cmd_ship() {
  local worktree_path="" cleanup=false events_json="" args
  args=(--auto-merge)

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --worktree)
        [ "$#" -ge 2 ] || {
          echo "ERROR: --worktree requires a path." >&2
          return 2
        }
        worktree_path="$2"
        shift 2
        ;;
      --auto-merge) shift ;;
      --cleanup)
        cleanup=true
        shift
        ;;
      --events-json)
        [ "$#" -ge 2 ] || {
          echo "ERROR: --events-json requires a path." >&2
          return 2
        }
        events_json="$2"
        shift 2
        ;;
      -h | --help)
        usage
        return 0
        ;;
      *)
        echo "ERROR: unknown worker ship argument '$1'." >&2
        return 2
        ;;
    esac
  done

  [ -n "$worktree_path" ] || {
    echo "ERROR: worker ship requires --worktree." >&2
    return 2
  }
  [ -d "$worktree_path" ] || {
    echo "ERROR: worktree does not exist: $worktree_path" >&2
    return 1
  }
  if [ "$cleanup" = true ]; then
    args+=(--cleanup-worktree)
  fi
  if [ -n "$events_json" ]; then
    TOUCHSTONE_EVENTS_FILE="$events_json" \
      bash -c 'cd "$1" && shift && bash scripts/open-pr.sh "$@"' _ "$worktree_path" "${args[@]}"
  else
    (cd "$worktree_path" && bash scripts/open-pr.sh "${args[@]}")
  fi
}

branch_has_open_or_closed_pr() {
  local branch="$1" number
  number="$(worker_pr_field "$branch" number)"
  [ -n "$number" ]
}

remote_branch_exists() {
  local repo_path="$1" branch="$2"
  git -C "$repo_path" ls-remote --exit-code --heads origin "$branch" >/dev/null 2>&1
}

worktree_manager_path() {
  local worktree_path="$1"
  git -C "$worktree_path" worktree list --porcelain \
    | awk '/^worktree / { print substr($0, length("worktree ") + 1); exit }'
}

cmd_abandon() {
  local worktree_path="" dry_run=false force=false branch base unique_commits manager_path

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --worktree)
        [ "$#" -ge 2 ] || {
          echo "ERROR: --worktree requires a path." >&2
          return 2
        }
        worktree_path="$2"
        shift 2
        ;;
      --dry-run)
        dry_run=true
        shift
        ;;
      --force)
        force=true
        shift
        ;;
      -h | --help)
        usage
        return 0
        ;;
      *)
        echo "ERROR: unknown worker abandon argument '$1'." >&2
        return 2
        ;;
    esac
  done

  [ -n "$worktree_path" ] || {
    echo "ERROR: worker abandon requires --worktree." >&2
    return 2
  }
  [ -d "$worktree_path" ] || {
    echo "ERROR: worktree does not exist: $worktree_path" >&2
    return 1
  }

  branch="$(worker_branch "$worktree_path")"
  [ -n "$branch" ] && [ "$branch" != "HEAD" ] || {
    echo "ERROR: cannot abandon detached worktree." >&2
    return 1
  }
  base="$(cd "$worktree_path" && touchstone_worker_default_ref)"
  unique_commits="$(git -C "$worktree_path" log "$base..HEAD" --oneline 2>/dev/null || true)"

  if [ -n "$unique_commits" ] && [ "$force" != true ]; then
    echo "ERROR: refusing to abandon $worktree_path; branch '$branch' has commits not merged into $base." >&2
    echo "       Use --force only after confirming the work is disposable." >&2
    return 1
  fi

  if [ "$dry_run" = true ]; then
    echo "Would remove worktree: $worktree_path"
    if branch_has_open_or_closed_pr "$branch"; then
      echo "Would keep remote branch because a PR exists for: $branch"
    elif remote_branch_exists "$worktree_path" "$branch"; then
      echo "Would delete remote branch: origin/$branch"
    fi
    return 0
  fi

  manager_path="$(worktree_manager_path "$worktree_path")"
  [ -n "$manager_path" ] || {
    echo "ERROR: could not find a git worktree manager for $worktree_path" >&2
    return 1
  }
  git -C "$manager_path" worktree remove --force "$worktree_path"
  if branch_has_open_or_closed_pr "$branch"; then
    echo "Kept remote branch because a PR exists for: $branch"
  elif remote_branch_exists "$manager_path" "$branch"; then
    git -C "$manager_path" push origin --delete "$branch"
  fi
  touchstone_emit_event worker_abandoned worktree_path="$worktree_path" branch="$branch"
}

cmd_list() {
  local repo_path="" json=false repo_root list_output first=true path="" branch_ref="" branch=""

  while [ "$#" -gt 0 ]; do
    case "$1" in
      --repo)
        [ "$#" -ge 2 ] || {
          echo "ERROR: --repo requires a path." >&2
          return 2
        }
        repo_path="$2"
        shift 2
        ;;
      --json)
        json=true
        shift
        ;;
      -h | --help)
        usage
        return 0
        ;;
      *)
        echo "ERROR: unknown worker list argument '$1'." >&2
        return 2
        ;;
    esac
  done

  if [ -n "$repo_path" ]; then
    repo_root="$(cd "$repo_path" && git rev-parse --show-toplevel)"
  else
    repo_root="$(repo_root_or_die)"
  fi
  list_output="$(git -C "$repo_root" worktree list --porcelain)"

  if [ "$json" = true ]; then
    printf '['
  fi

  while IFS= read -r line || [ -n "$line" ]; do
    if [ -z "$line" ]; then
      if [ -n "$path" ] && [ -n "$branch_ref" ]; then
        branch="${branch_ref#refs/heads/}"
        case "$branch" in
          feat/* | fix/* | chore/* | refactor/* | docs/*)
            if [ "$json" = true ]; then
              [ "$first" = true ] || printf ','
              worker_status_json "$path" | tr -d '\n'
              first=false
            else
              printf '%s  %s\n' "$(derive_worker_state "$path")" "$path"
            fi
            ;;
        esac
      fi
      path=""
      branch_ref=""
      continue
    fi
    case "$line" in
      worktree\ *) path="${line#worktree }" ;;
      branch\ *) branch_ref="${line#branch }" ;;
    esac
  done <<<"$list_output"

  if [ -n "$path" ] && [ -n "$branch_ref" ]; then
    branch="${branch_ref#refs/heads/}"
    case "$branch" in
      feat/* | fix/* | chore/* | refactor/* | docs/*)
        if [ "$json" = true ]; then
          [ "$first" = true ] || printf ','
          worker_status_json "$path" | tr -d '\n'
        else
          printf '%s  %s\n' "$(derive_worker_state "$path")" "$path"
        fi
        ;;
    esac
  fi

  if [ "$json" = true ]; then
    printf ']\n'
  fi
}

command="${1:-help}"
shift 2>/dev/null || true

case "$command" in
  spawn) cmd_spawn "$@" ;;
  status) cmd_status "$@" ;;
  ship) cmd_ship "$@" ;;
  abandon) cmd_abandon "$@" ;;
  list) cmd_list "$@" ;;
  help | -h | --help) usage ;;
  *)
    echo "ERROR: unknown worker command '$command'." >&2
    usage >&2
    exit 2
    ;;
esac
