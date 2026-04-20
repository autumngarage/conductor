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

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

CONFIG_FILE="${TOUCHSTONE_CONFIG_FILE:-.touchstone-config}"

PROJECT_TYPE=""
PACKAGE_MANAGER=""
MONOREPO=""
TARGETS=""
LINT_COMMAND=""
TYPECHECK_COMMAND=""
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
      project_type|profile) PROJECT_TYPE="$value" ;;
      package_manager) PACKAGE_MANAGER="$value" ;;
      monorepo) MONOREPO="$value" ;;
      targets) TARGETS="$value" ;;
      lint_command) LINT_COMMAND="$value" ;;
      typecheck_command) TYPECHECK_COMMAND="$value" ;;
      build_command) BUILD_COMMAND="$value" ;;
      test_command) TEST_COMMAND="$value" ;;
      validate_command) VALIDATE_COMMAND="$value" ;;
    esac
  done < "$CONFIG_FILE"
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
    npm|*) command="npm run $script" ;;
  esac

  run_shell_command "$command"
}

find_python_bin() {
  local candidate

  for candidate in ".venv/bin/python" "agent/.venv/bin/python"; do
    if [ -x "$candidate" ]; then
      printf '%s\n' "$candidate"
      return 0
    fi
  done

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  return 1
}

run_node_action() {
  local action="$1"

  case "$action" in
    lint|typecheck|build|test)
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
      if command -v pyright >/dev/null 2>&1; then
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
        info "$python_bin -m pytest"
        bash -c "$python_bin -m pytest" || pytest_rc=$?
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
    typecheck|build) run_shell_command "swift build" ;;
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
    typecheck|build) run_shell_command "go build ./..." ;;
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
    node|typescript|ts) run_node_action "$action" ;;
    python) run_python_action "$action" ;;
    rust) run_rust_action "$action" ;;
    swift) run_swift_action "$action" ;;
    go) run_go_action "$action" ;;
    generic|"")
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

  IFS=',' read -r -a target_entries <<< "$TARGETS"
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
  -h|--help) usage ;;
  detect) print_detection ;;
  lint|typecheck|build|test) run_action "$ACTION" ;;
  validate) run_validate ;;
  *)
    echo "ERROR: unknown touchstone-run action '$ACTION'" >&2
    usage >&2
    exit 1
    ;;
esac
