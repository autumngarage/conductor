#!/usr/bin/env bash
#
# scripts/touchstone-run.sh — run project profile tasks from .touchstone-config.
#
# Usage:
#   bash scripts/touchstone-run.sh detect
#   bash scripts/touchstone-run.sh lint
#   bash scripts/touchstone-run.sh typecheck
#   bash scripts/touchstone-run.sh build
#   bash scripts/touchstone-run.sh test
#   bash scripts/touchstone-run.sh validate
#
set -euo pipefail

ACTION="${1:-validate}"
HOOK_PRE_COMMIT_REMOTE_BRANCH="${PRE_COMMIT_REMOTE_BRANCH:-}"
HOOK_PRE_COMMIT_REMOTE_NAME="${PRE_COMMIT_REMOTE_NAME:-origin}"

clear_git_hook_env() {
  unset GIT_DIR
  unset GIT_WORK_TREE
  unset GIT_INDEX_FILE
  unset GIT_OBJECT_DIRECTORY
  unset GIT_COMMON_DIR
  unset GIT_NAMESPACE
  unset GIT_PREFIX
  unset GIT_INTERNAL_GETTEXT_SH_SCHEME
  unset PRE_COMMIT
  unset PRE_COMMIT_FROM_REF
  unset PRE_COMMIT_TO_REF
  unset PRE_COMMIT_LOCAL_BRANCH
  unset PRE_COMMIT_REMOTE_BRANCH
  unset PRE_COMMIT_REMOTE_NAME
  unset PRE_COMMIT_REMOTE_URL
}

clear_review_env() {
  unset TOUCHSTONE_REVIEWER
  unset TOUCHSTONE_LOCAL_REVIEWER_COMMAND
  unset TOUCHSTONE_PREFLIGHT_ALREADY_RAN
  unset TOUCHSTONE_CONDUCTOR_WITH
  unset TOUCHSTONE_CONDUCTOR_PREFER
  unset TOUCHSTONE_CONDUCTOR_EFFORT
  unset TOUCHSTONE_CONDUCTOR_TAGS
  unset TOUCHSTONE_CONDUCTOR_EXCLUDE
  unset CODEX_REVIEW_ENABLED
  unset CODEX_REVIEW_MODE
  unset CODEX_REVIEW_BASE
  unset CODEX_REVIEW_BRANCH_NAME
  unset CODEX_REVIEW_FORCE
  unset CODEX_REVIEW_NO_AUTOFIX
  unset CODEX_REVIEW_MAX_ITERATIONS
  unset CODEX_REVIEW_MAX_DIFF_LINES
  unset CODEX_REVIEW_CACHE_CLEAN
  unset CODEX_REVIEW_DISABLE_CACHE
  unset CODEX_REVIEW_TIMEOUT
  unset CODEX_REVIEW_ON_ERROR
  unset CODEX_REVIEW_CONTEXT_MODE
  unset CODEX_REVIEW_CONTEXT_SMALL_MAX_DIFF_LINES
  unset CODEX_REVIEW_CONTEXT_SMALL_MAX_FILES
  unset CODEX_REVIEW_LOCK_WAIT_SECONDS
  unset CODEX_REVIEW_LOCK_STALE_SECONDS
  unset CODEX_REVIEW_DISABLE_LOCK
  unset CODEX_REVIEW_IN_PROGRESS
  unset CODEX_REVIEW_ASSIST
  unset CODEX_REVIEW_ASSIST_TIMEOUT
  unset CODEX_REVIEW_ASSIST_MAX_ROUNDS
  unset CODEX_REVIEW_ASSIST_WITH
  unset CODEX_REVIEW_ASSIST_PREFER
  unset CODEX_REVIEW_ASSIST_EFFORT
  unset CODEX_REVIEW_ASSIST_TAGS
  unset CODEX_REVIEW_SUMMARY_FILE
  unset CODEX_REVIEW_DIAGNOSTICS_FILE
  unset CODEX_REVIEW_FINDINGS_HISTORY_FILE
  unset CODEX_REVIEW_SUPPRESS_LEGACY_WARNINGS
}

# tests/test-find-python-bin.sh sources this script with
# TOUCHSTONE_RUN_SOURCE_ONLY=1 to call helpers directly without running the
# action dispatcher at the bottom. Tests pass TOUCHSTONE_RUN_TEST_REPO_ROOT
# to fix REPO_ROOT explicitly so they can construct fixture filesystems
# without needing a real git repo.
if [ "${TOUCHSTONE_RUN_SOURCE_ONLY:-0}" = "1" ]; then
  REPO_ROOT="${TOUCHSTONE_RUN_TEST_REPO_ROOT:-$(pwd)}"
else
  clear_git_hook_env
  clear_review_env
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  cd "$REPO_ROOT"
fi

CONFIG_FILE="${TOUCHSTONE_CONFIG_FILE:-.touchstone-config}"

PROJECT_TYPE=""
PACKAGE_MANAGER=""
MONOREPO=""
TARGETS=""
LINT_COMMAND=""
TYPECHECK_COMMAND=""
TYPECHECK_COMMAND_AUTO=false
BUILD_COMMAND=""
TEST_COMMAND=""
VALIDATE_COMMAND=""

info() { printf '==> %s\n' "$*"; }
ok() { printf '  OK %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }

usage() {
  sed -n '3,12p' "$0" | sed 's/^# \{0,1\}//'
}

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

truthy() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true | 1 | yes | on) return 0 ;;
    *) return 1 ;;
  esac
}

short_ref_name() {
  local ref="$1"
  local remote="${2:-origin}"

  case "$ref" in
    refs/heads/*) ref="${ref#refs/heads/}" ;;
    refs/remotes/"$remote"/*) ref="${ref#refs/remotes/$remote/}" ;;
    "$remote"/*) ref="${ref#"$remote/"}" ;;
  esac
  printf '%s' "$ref"
}

default_branch_for_remote() {
  local remote="${1:-origin}"
  local ref

  ref="$(git symbolic-ref --quiet --short "refs/remotes/$remote/HEAD" 2>/dev/null || true)"
  if [ -n "$ref" ]; then
    printf '%s\n' "${ref#"$remote/"}"
    return 0
  fi

  return 1
}

should_skip_feature_push_validate() {
  local remote_branch default_branch

  truthy "${TOUCHSTONE_VALIDATE_SKIP_FEATURE_PUSH:-false}" || return 1
  [ "$ACTION" = "validate" ] || return 1
  [ -n "$HOOK_PRE_COMMIT_REMOTE_BRANCH" ] || return 1

  remote_branch="$(short_ref_name "$HOOK_PRE_COMMIT_REMOTE_BRANCH" "$HOOK_PRE_COMMIT_REMOTE_NAME")"
  [ -n "$remote_branch" ] || return 1
  default_branch="$(default_branch_for_remote "$HOOK_PRE_COMMIT_REMOTE_NAME" || true)"
  [ -n "$default_branch" ] || return 1

  [ "$remote_branch" != "$default_branch" ] \
    && [ "$remote_branch" != "main" ] \
    && [ "$remote_branch" != "master" ]
}

load_config() {
  local line key value

  [ -f "$CONFIG_FILE" ] || return 0

  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim "$line")"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac

    key="$(trim "${line%%=*}")"
    value="$(trim "${line#*=}")"

    case "$key" in
      project_type | profile) PROJECT_TYPE="$value" ;;
      package_manager) PACKAGE_MANAGER="$value" ;;
      monorepo) MONOREPO="$value" ;;
      targets) TARGETS="$value" ;;
      lint_command) LINT_COMMAND="$value" ;;
      typecheck_command)
        if [ "$value" = "auto" ]; then
          TYPECHECK_COMMAND=""
          TYPECHECK_COMMAND_AUTO=true
        else
          TYPECHECK_COMMAND="$value"
          TYPECHECK_COMMAND_AUTO=false
        fi
        ;;
      build_command) BUILD_COMMAND="$value" ;;
      test_command) TEST_COMMAND="$value" ;;
      validate_command) VALIDATE_COMMAND="$value" ;;
    esac
  done <"$CONFIG_FILE"
}

detect_node_package_manager() {
  local dir="${1:-.}" package_manager

  if [ -f "$dir/package.json" ]; then
    package_manager="$(sed -n 's/.*"packageManager"[[:space:]]*:[[:space:]]*"\([^@"]*\)@.*/\1/p' "$dir/package.json" | head -1)"
    if [ -z "$package_manager" ]; then
      package_manager="$(sed -n 's/.*"packageManager"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$dir/package.json" | head -1)"
    fi
    if [ -n "$package_manager" ]; then
      printf '%s\n' "$package_manager"
      return 0
    fi
  fi

  if [ -f "$dir/pnpm-lock.yaml" ] || [ -f "$dir/pnpm-workspace.yaml" ]; then
    printf 'pnpm\n'
  elif [ -f "$dir/yarn.lock" ]; then
    printf 'yarn\n'
  elif [ -f "$dir/bun.lock" ] || [ -f "$dir/bun.lockb" ]; then
    printf 'bun\n'
  else
    printf 'npm\n'
  fi
}

detect_profile() {
  local dir="${1:-.}"

  if [ -f "$dir/pnpm-workspace.yaml" ]; then
    printf 'node\n'
  elif [ -f "$dir/package.json" ] || [ -f "$dir/tsconfig.json" ]; then
    printf 'node\n'
  elif [ -f "$dir/Cargo.toml" ]; then
    printf 'rust\n'
  elif [ -f "$dir/Package.swift" ]; then
    printf 'swift\n'
  elif [ -f "$dir/go.mod" ]; then
    printf 'go\n'
  elif [ -f "$dir/uv.lock" ] || [ -f "$dir/pyproject.toml" ] || [ -f "$dir/requirements.txt" ]; then
    printf 'python\n'
  else
    printf 'generic\n'
  fi
}

detect_monorepo() {
  local dir="${1:-.}"

  if [ -f "$dir/pnpm-workspace.yaml" ]; then
    printf 'true\n'
  elif [ -f "$dir/Cargo.toml" ] && grep -q '^\[workspace\]' "$dir/Cargo.toml" 2>/dev/null; then
    printf 'true\n'
  elif [ -f "$dir/package.json" ] && grep -q '"workspaces"' "$dir/package.json" 2>/dev/null; then
    printf 'true\n'
  else
    printf 'false\n'
  fi
}

detect_targets() {
  local root="${1:-.}" base target_dir profile targets=""

  for base in apps packages services; do
    [ -d "$root/$base" ] || continue
    for target_dir in "$root/$base"/*; do
      [ -d "$target_dir" ] || continue
      profile="$(detect_profile "$target_dir")"
      [ "$profile" = "generic" ] && continue
      if [ -n "$targets" ]; then
        targets="${targets},"
      fi
      targets="${targets}$(basename "$target_dir"):$base/$(basename "$target_dir"):$profile"
    done
  done

  printf '%s\n' "$targets"
}

has_package_script() {
  local script="$1"
  [ -f package.json ] || return 1
  grep -Eq "\"$script\"[[:space:]]*:" package.json
}

run_shell_command() {
  local command="$1"
  info "$command"
  env \
    -u GIT_DIR -u GIT_WORK_TREE -u GIT_INDEX_FILE \
    -u GIT_OBJECT_DIRECTORY -u GIT_COMMON_DIR -u GIT_NAMESPACE \
    -u GIT_PREFIX -u GIT_INTERNAL_GETTEXT_SH_SCHEME \
    -u PRE_COMMIT -u PRE_COMMIT_FROM_REF -u PRE_COMMIT_TO_REF \
    -u PRE_COMMIT_LOCAL_BRANCH -u PRE_COMMIT_REMOTE_BRANCH \
    -u PRE_COMMIT_REMOTE_NAME -u PRE_COMMIT_REMOTE_URL \
    -u TOUCHSTONE_REVIEWER -u TOUCHSTONE_LOCAL_REVIEWER_COMMAND \
    -u TOUCHSTONE_PREFLIGHT_ALREADY_RAN \
    -u TOUCHSTONE_CONDUCTOR_WITH -u TOUCHSTONE_CONDUCTOR_PREFER \
    -u TOUCHSTONE_CONDUCTOR_EFFORT -u TOUCHSTONE_CONDUCTOR_TAGS \
    -u TOUCHSTONE_CONDUCTOR_EXCLUDE \
    -u CODEX_REVIEW_ENABLED -u CODEX_REVIEW_MODE -u CODEX_REVIEW_BASE \
    -u CODEX_REVIEW_BRANCH_NAME -u CODEX_REVIEW_FORCE \
    -u CODEX_REVIEW_NO_AUTOFIX -u CODEX_REVIEW_MAX_ITERATIONS \
    -u CODEX_REVIEW_MAX_DIFF_LINES -u CODEX_REVIEW_CACHE_CLEAN \
    -u CODEX_REVIEW_DISABLE_CACHE -u CODEX_REVIEW_TIMEOUT \
    -u CODEX_REVIEW_ON_ERROR -u CODEX_REVIEW_CONTEXT_MODE \
    -u CODEX_REVIEW_CONTEXT_SMALL_MAX_DIFF_LINES \
    -u CODEX_REVIEW_CONTEXT_SMALL_MAX_FILES \
    -u CODEX_REVIEW_LOCK_WAIT_SECONDS -u CODEX_REVIEW_LOCK_STALE_SECONDS \
    -u CODEX_REVIEW_DISABLE_LOCK -u CODEX_REVIEW_IN_PROGRESS \
    -u CODEX_REVIEW_ASSIST -u CODEX_REVIEW_ASSIST_TIMEOUT \
    -u CODEX_REVIEW_ASSIST_MAX_ROUNDS -u CODEX_REVIEW_ASSIST_WITH \
    -u CODEX_REVIEW_ASSIST_PREFER -u CODEX_REVIEW_ASSIST_EFFORT \
    -u CODEX_REVIEW_ASSIST_TAGS -u CODEX_REVIEW_SUMMARY_FILE \
    -u CODEX_REVIEW_DIAGNOSTICS_FILE \
    -u CODEX_REVIEW_FINDINGS_HISTORY_FILE \
    -u CODEX_REVIEW_SUPPRESS_LEGACY_WARNINGS \
    bash -c "$command"
}

configured_command_for_action() {
  case "$1" in
    lint) printf '%s\n' "$LINT_COMMAND" ;;
    typecheck) printf '%s\n' "$TYPECHECK_COMMAND" ;;
    build) printf '%s\n' "$BUILD_COMMAND" ;;
    test) printf '%s\n' "$TEST_COMMAND" ;;
    validate) printf '%s\n' "$VALIDATE_COMMAND" ;;
    *) printf '\n' ;;
  esac
}

run_node_script() {
  local script="$1" package_manager command

  has_package_script "$script" || return 1

  package_manager="${PACKAGE_MANAGER:-auto}"
  if [ "$package_manager" = "auto" ] || [ -z "$package_manager" ]; then
    package_manager="$(detect_node_package_manager ".")"
  fi

  case "$package_manager" in
    pnpm) command="pnpm $script" ;;
    yarn) command="yarn $script" ;;
    bun) command="bun run $script" ;;
    npm | *) command="npm run $script" ;;
  esac

  run_shell_command "$command"
}

find_python_bin() {
  local candidate cwd parent_root parent_python

  # Operator override wins over everything else.
  if [ -n "${PYTEST_PYTHON:-}" ]; then
    if command -v "$PYTEST_PYTHON" >/dev/null 2>&1; then
      command -v "$PYTEST_PYTHON"
      return 0
    fi
    echo "ERROR: PYTEST_PYTHON is set but not executable: $PYTEST_PYTHON" >&2
    return 1
  fi

  for candidate in ".venv/bin/python" "agent/.venv/bin/python"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  # Worktree fallback: when the current checkout is a worktree (.git is a
  # file, not a directory), the venv lives in the parent repo. Resolve to
  # the parent's .venv/bin/python — pinned deps are identical across
  # worktrees, so the parent's interpreter is the right answer.
  cwd="$(pwd)"
  if parent_root="$(find_worktree_parent_root "$cwd")"; then
    parent_python="$parent_root/.venv/bin/python"
    if [ -x "$parent_python" ]; then
      printf '%s\n' "$parent_python"
      return 0
    fi
  fi

  # No silent fallback to system python3: the project's pinned deps live
  # in a venv. Running the test suite against a system interpreter would
  # produce confusing ModuleNotFoundError noise that the operator can't
  # diagnose from the failure alone (#171).
  echo "ERROR: no project virtualenv found." >&2
  echo "       Tried: $cwd/.venv/bin/python (this checkout)" >&2
  if [ -n "${parent_root:-}" ]; then
    echo "       Tried: $parent_root/.venv/bin/python (worktree parent)" >&2
  fi
  echo "       Run \`bash setup.sh\` in this checkout, OR push from the" >&2
  echo "       parent checkout that has the venv set up." >&2
  return 1
}

# Returns the absolute path of the parent repo's worktree root when $1 is
# a worktree checkout (i.e. $1/.git is a regular file containing a
# `gitdir:` pointer). Returns 1 when $1 is a normal checkout or when the
# worktree metadata is malformed.
find_worktree_parent_root() {
  local checkout_root="$1" git_file gitdir gitdir_path search_dir

  git_file="$checkout_root/.git"
  if [ ! -f "$git_file" ]; then
    return 1
  fi
  if [ ! -r "$git_file" ]; then
    echo "       Worktree check failed: cannot read $git_file" >&2
    return 1
  fi

  IFS= read -r gitdir <"$git_file" || {
    echo "       Worktree check failed: cannot read gitdir from $git_file" >&2
    return 1
  }
  case "$gitdir" in
    gitdir:*) gitdir="${gitdir#gitdir:}" ;;
    *) return 1 ;;
  esac
  gitdir="$(trim "$gitdir")"
  if [ -z "$gitdir" ]; then
    echo "       Worktree check failed: empty gitdir in $git_file" >&2
    return 1
  fi

  case "$gitdir" in
    /*) gitdir_path="$gitdir" ;;
    *) gitdir_path="$checkout_root/$gitdir" ;;
  esac
  if [ ! -d "$gitdir_path" ]; then
    echo "       Worktree check failed: gitdir does not exist: $gitdir_path" >&2
    return 1
  fi

  search_dir="$(cd "$(dirname "$gitdir_path")" && pwd)"
  while [ "$search_dir" != "/" ]; do
    if [ "$(basename "$search_dir")" = ".git" ]; then
      dirname "$search_dir"
      return 0
    fi
    search_dir="$(dirname "$search_dir")"
  done

  echo "       Worktree check failed: no parent .git directory above $gitdir_path" >&2
  return 1
}

# Allow tests to source the script just for its helpers without invoking
# the action dispatcher below.
if [ "${TOUCHSTONE_RUN_SOURCE_ONLY:-0}" = "1" ]; then
  return 0
fi

run_node_action() {
  local action="$1"

  case "$action" in
    lint | typecheck | build | test)
      if run_node_script "$action"; then
        return 0
      fi
      ok "no package.json '$action' script; skipped"
      ;;
    build_if_distinct)
      # Bundler builds (webpack/vite/esbuild/turbopack) catch errors typecheck
      # misses. Only fire when both scripts are declared — "build: tsc" (build
      # IS typecheck) shouldn't double-run during validate.
      if has_package_script typecheck && has_package_script build; then
        run_node_script build
      fi
      ;;
    *)
      warn "unknown Node action: $action"
      return 1
      ;;
  esac
}

run_python_action() {
  local action="$1" python_bin

  case "$action" in
    lint)
      if command -v ruff >/dev/null 2>&1; then
        run_shell_command "ruff check ."
      else
        ok "ruff not installed; skipped"
      fi
      ;;
    typecheck)
      if [ "$TYPECHECK_COMMAND_AUTO" != true ]; then
        ok "no Python typecheck_command configured; skipped"
      elif command -v pyright >/dev/null 2>&1; then
        run_shell_command "pyright"
      elif command -v mypy >/dev/null 2>&1; then
        run_shell_command "mypy ."
      else
        ok "pyright/mypy not installed; skipped"
      fi
      ;;
    build)
      ok "no default Python build command; set build_command in .touchstone-config"
      ;;
    test)
      if python_bin="$(find_python_bin)"; then
        local pytest_rc=0
        run_shell_command "$python_bin -m pytest" || pytest_rc=$?
        # pytest exit 5 = no tests collected. Treat like absent linters — skip, don't fail.
        if [ "$pytest_rc" -eq 5 ]; then
          ok "pytest found no tests; skipped"
        elif [ "$pytest_rc" -ne 0 ]; then
          return "$pytest_rc"
        fi
      else
        ok "python not found; skipped"
      fi
      ;;
    build_if_distinct)
      : # no default Python build — nothing useful to add during validate
      ;;
    *)
      warn "unknown Python action: $action"
      return 1
      ;;
  esac
}

run_rust_action() {
  local action="$1"

  if ! command -v cargo >/dev/null 2>&1; then
    ok "cargo not installed; skipped"
    return 0
  fi

  case "$action" in
    lint)
      if cargo fmt --version >/dev/null 2>&1; then
        run_shell_command "cargo fmt -- --check"
      else
        ok "cargo fmt not installed; skipped"
      fi
      if cargo clippy --version >/dev/null 2>&1; then
        run_shell_command "cargo clippy --all-targets --all-features -- -D warnings"
      else
        ok "cargo clippy not installed; skipped"
      fi
      ;;
    typecheck) run_shell_command "cargo check --all-targets --all-features" ;;
    build) run_shell_command "cargo build --all" ;;
    test) run_shell_command "cargo test --all" ;;
    build_if_distinct)
      : # cargo check already runs the full compiler — cargo build would repeat
      ;;
    *)
      warn "unknown Rust action: $action"
      return 1
      ;;
  esac
}

run_swift_action() {
  local action="$1"

  if ! command -v swift >/dev/null 2>&1; then
    ok "swift not installed; skipped"
    return 0
  fi

  case "$action" in
    lint)
      if command -v swift-format >/dev/null 2>&1; then
        run_shell_command "swift-format lint -r ."
      else
        ok "swift-format not installed; skipped"
      fi
      ;;
    typecheck | build) run_shell_command "swift build" ;;
    test) run_shell_command "swift test" ;;
    build_if_distinct)
      : # swift typecheck IS swift build — running it again would repeat
      ;;
    *)
      warn "unknown Swift action: $action"
      return 1
      ;;
  esac
}

run_go_action() {
  local action="$1"

  if ! command -v go >/dev/null 2>&1; then
    ok "go not installed; skipped"
    return 0
  fi

  case "$action" in
    lint) run_shell_command "go vet ./..." ;;
    typecheck | build) run_shell_command "go build ./..." ;;
    test) run_shell_command "go test ./..." ;;
    build_if_distinct)
      : # go typecheck IS go build — running it again would repeat
      ;;
    *)
      warn "unknown Go action: $action"
      return 1
      ;;
  esac
}

run_profile_action() {
  local profile="$1" action="$2"

  case "$profile" in
    node | typescript | ts) run_node_action "$action" ;;
    python) run_python_action "$action" ;;
    rust) run_rust_action "$action" ;;
    swift) run_swift_action "$action" ;;
    go) run_go_action "$action" ;;
    generic | "")
      # build_if_distinct is a validate-time extra — silently no-op for generic
      # so "touchstone run validate" doesn't print a scary "no default command"
      # line on every non-typed project.
      if [ "$action" = "build_if_distinct" ]; then
        return 0
      fi
      ok "generic project has no default '$action' command; set ${action}_command in .touchstone-config"
      ;;
    *)
      warn "unknown project_type '$profile' for action '$action'"
      return 1
      ;;
  esac
}

run_targets_action() {
  local action="$1" entry name path profile
  local -a target_entries=()

  [ -n "$TARGETS" ] || return 1

  IFS=',' read -r -a target_entries <<<"$TARGETS"
  for entry in "${target_entries[@]}"; do
    entry="$(trim "$entry")"
    [ -z "$entry" ] && continue
    name="${entry%%:*}"
    path="${entry#*:}"
    profile="${path#*:}"
    path="${path%%:*}"
    if [ "$path" = "$profile" ]; then
      profile="auto"
    fi
    if [ "$profile" = "auto" ] || [ -z "$profile" ]; then
      profile="$(detect_profile "$path")"
    fi

    if [ ! -d "$path" ]; then
      warn "target '$name' path not found: $path"
      continue
    fi

    info "target $name ($profile) — $action"
    (cd "$path" && run_profile_action "$profile" "$action")
  done
}

run_action() {
  local action="$1" configured profile

  configured="$(configured_command_for_action "$action")"
  if [ -n "$configured" ]; then
    run_shell_command "$configured"
    return 0
  fi

  if run_targets_action "$action"; then
    return 0
  fi

  profile="${PROJECT_TYPE:-auto}"
  if [ "$profile" = "auto" ] || [ -z "$profile" ]; then
    profile="$(detect_profile ".")"
  fi
  if [ "$profile" = "generic" ] && [ "$(detect_profile ".")" != "generic" ]; then
    profile="$(detect_profile ".")"
  fi

  run_profile_action "$profile" "$action"
}

run_validate() {
  local configured

  if should_skip_feature_push_validate; then
    ok "feature-branch pre-push validate skipped; merge gate runs full validation"
    return 0
  fi

  configured="$(configured_command_for_action validate)"
  if [ -n "$configured" ]; then
    run_shell_command "$configured"
    return 0
  fi

  run_action lint
  run_action typecheck
  # Node targets with distinct typecheck + build scripts: run the bundler too.
  # Other profiles no-op because their typecheck already runs the compiler.
  # Distinctness is per-target, so this flows through run_targets_action just
  # like every other action — no special-casing for monorepo vs single-package.
  run_action build_if_distinct
  run_action test
}

print_detection() {
  local profile package_manager monorepo targets

  profile="${PROJECT_TYPE:-auto}"
  [ "$profile" = "auto" ] || [ -n "$profile" ] || profile="auto"
  if [ "$profile" = "auto" ]; then
    profile="$(detect_profile ".")"
  fi
  if [ "$profile" = "generic" ] && [ "$(detect_profile ".")" != "generic" ]; then
    profile="$(detect_profile ".")"
  fi

  package_manager="${PACKAGE_MANAGER:-auto}"
  if [ "$package_manager" = "auto" ] || [ -z "$package_manager" ]; then
    if [ "$profile" = "node" ]; then
      package_manager="$(detect_node_package_manager ".")"
    else
      package_manager=""
    fi
  fi

  monorepo="${MONOREPO:-auto}"
  if [ "$monorepo" = "auto" ] || [ -z "$monorepo" ]; then
    monorepo="$(detect_monorepo ".")"
  fi

  targets="${TARGETS:-}"
  if [ -z "$targets" ]; then
    targets="$(detect_targets ".")"
  fi

  printf 'project_type=%s\n' "$profile"
  [ -n "$package_manager" ] && printf 'package_manager=%s\n' "$package_manager"
  printf 'monorepo=%s\n' "$monorepo"
  if [ -n "$targets" ]; then
    printf 'targets=%s\n' "$targets"
  fi
}

load_config

case "$ACTION" in
  -h | --help) usage ;;
  detect) print_detection ;;
  lint | typecheck | build | test) run_action "$ACTION" ;;
  validate) run_validate ;;
  *)
    echo "ERROR: unknown touchstone-run action '$ACTION'" >&2
    usage >&2
    exit 1
    ;;
esac
