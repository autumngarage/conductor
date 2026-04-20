#!/usr/bin/env bash
#
# setup.sh — one-command project setup.
#
# Run this after cloning the repo:
#   bash setup.sh
#
# Installs all dev tools, syncs touchstone files, sets up hooks, and installs
# project dependencies. Idempotent — safe to re-run anytime.
#
set -euo pipefail

# Colors.
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
BOLD='\033[1m'
RESET='\033[0m'

info()  { printf "${BOLD}==> %s${RESET}\n" "$*"; }
ok()    { printf "  ${GREEN}✓${RESET} %s\n" "$*"; }
warn()  { printf "  ${YELLOW}!${RESET} %s\n" "$*"; }
fail()  { printf "  ${RED}✗${RESET} %s\n" "$*"; }

DEPS_ONLY=false
GIT_WORKFLOW="git"
GITBUTLER_MCP="false"
SKIP_DEVTOOLS=false
if [ "${TOUCHSTONE_SKIP_DEVTOOLS:-}" = "1" ] || [ "${TOUCHSTONE_SKIP_DEVTOOLS:-}" = "true" ]; then
  SKIP_DEVTOOLS=true
fi

while [ "$#" -gt 0 ]; do
  case "$1" in
    --deps-only) DEPS_ONLY=true; shift ;;
    --skip-devtools) SKIP_DEVTOOLS=true; shift ;;
    -h|--help)
      echo "Usage: bash setup.sh [--deps-only] [--skip-devtools]"
      echo ""
      echo "  --deps-only       Reinstall project deps (also refreshes per-profile dev tools)"
      echo "  --skip-devtools   Skip per-profile dev-tool install (for CI or offline environments)"
      echo "                    Also honored via TOUCHSTONE_SKIP_DEVTOOLS=1"
      exit 0
      ;;
    *) fail "Unknown argument: $1"; exit 1 ;;
  esac
done

trim_config_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  value="${value%,}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  printf '%s' "$value"
}

truthy() {
  case "$(printf '%s' "${1:-false}" | tr '[:upper:]' '[:lower:]')" in
    true|1|yes|on) return 0 ;;
    *) return 1 ;;
  esac
}

prompt_yes_no() {
  local prompt="$1"
  local default="${2:-false}"
  local suffix answer

  if [ "$default" = "true" ]; then
    suffix="[Y/n]"
  else
    suffix="[y/N]"
  fi

  read -r -p "   $prompt $suffix: " answer
  answer="$(trim_config_value "$answer")"
  if [ -z "$answer" ]; then
    answer="$default"
  fi

  case "$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')" in
    y|yes|true|1|on) return 0 ;;
    *) return 1 ;;
  esac
}

load_touchstone_config() {
  local line key value

  [ -f ".touchstone-config" ] || return 0

  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim_config_value "$line")"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac

    key="$(trim_config_value "${line%%=*}")"
    value="$(trim_config_value "${line#*=}")"

    case "$key" in
      git_workflow) GIT_WORKFLOW="$value" ;;
      gitbutler_mcp) GITBUTLER_MCP="$value" ;;
    esac
  done < ".touchstone-config"
}

PROJECT_NAME="$(basename "$(pwd)")"
echo ""
printf "${BOLD}Setting up ${PROJECT_NAME}${RESET}\n"
echo ""

if [ "$DEPS_ONLY" = false ]; then
load_touchstone_config

# --------------------------------------------------------------------------
# 1. Homebrew (required foundation)
# --------------------------------------------------------------------------
info "Checking Homebrew"
if command -v brew >/dev/null 2>&1; then
  ok "brew installed"
else
  fail "Homebrew is required. Install from https://brew.sh"
  exit 1
fi

# --------------------------------------------------------------------------
# 2. Touchstone CLI
# --------------------------------------------------------------------------
info "Checking touchstone"
if command -v touchstone >/dev/null 2>&1; then
  TOUCHSTONE_VERSION_SUMMARY="$(touchstone version 2>&1 | awk 'NF && !seen { sub(/^touchstone /, ""); print; seen = 1 }')"
  ok "touchstone ${TOUCHSTONE_VERSION_SUMMARY:-installed}"
else
  warn "Installing touchstone..."
  brew tap autumngarage/touchstone 2>/dev/null || true
  brew install touchstone
  ok "touchstone installed"
fi

# --------------------------------------------------------------------------
# 3. Dev tools (brew)
# --------------------------------------------------------------------------
info "Checking dev tools"

brew_install_if_missing() {
  local cmd="$1"
  local formula="${2:-$1}"
  if command -v "$cmd" >/dev/null 2>&1; then
    ok "$cmd installed"
  else
    warn "Installing $formula..."
    brew install "$formula" 2>/dev/null
    ok "$cmd installed"
  fi
}

brew_install_if_missing "git"        "git"
brew_install_if_missing "gh"         "gh"
brew_install_if_missing "pre-commit" "pre-commit"
brew_install_if_missing "gitleaks"   "gitleaks"
brew_install_if_missing "shellcheck" "shellcheck"
brew_install_if_missing "shfmt"      "shfmt"

# --------------------------------------------------------------------------
# 4. AI reviewer CLI (optional)
# --------------------------------------------------------------------------
info "Checking AI reviewer"

AI_REVIEW_ENABLED=true
AI_REVIEWERS=()
AI_REVIEWERS_CHECKED=""
AI_LOCAL_REVIEW_COMMAND=""
AI_REVIEW_ROUTING_ENABLED=false
AI_REVIEW_ROUTING_SMALL_MAX=400

trim_review_value() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  value="${value%,}"
  value="${value#\"}"; value="${value%\"}"
  value="${value#\'}"; value="${value%\'}"
  printf '%s' "$value"
}

add_reviewers_from_csv() {
  local csv="$1" item
  local -a items=()
  csv="$(trim_review_value "$csv")"
  csv="${csv#\[}"
  csv="${csv%\]}"
  IFS=',' read -r -a items <<< "$csv"
  for item in "${items[@]}"; do
    item="$(trim_review_value "$item")"
    [ -n "$item" ] && AI_REVIEWERS+=("$item")
  done
}

load_ai_review_config() {
  local section="" line key value

  [ -f ".codex-review.toml" ] || {
    AI_REVIEWERS=("codex")
    return 0
  }

  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%%#*}"
    line="$(trim_review_value "$line")"
    [ -z "$line" ] && continue

    case "$line" in
      \[*\])
        section="${line#\[}"
        section="${section%\]}"
        continue
        ;;
    esac

    case "$line" in *=*) ;; *) continue ;; esac
    key="$(trim_review_value "${line%%=*}")"
    value="$(trim_review_value "${line#*=}")"

    if [ "$section" = "review" ]; then
      case "$key" in
        enabled) AI_REVIEW_ENABLED="$value" ;;
        reviewers) add_reviewers_from_csv "${line#*=}" ;;
      esac
    elif [ "$section" = "review.routing" ]; then
      case "$key" in
        enabled) AI_REVIEW_ROUTING_ENABLED="$value" ;;
        small_max_diff_lines|small_diff_lines) AI_REVIEW_ROUTING_SMALL_MAX="$value" ;;
        small_reviewers|large_reviewers) add_reviewers_from_csv "${line#*=}" ;;
      esac
    elif [ "$section" = "review.local" ]; then
      case "$key" in
        command) AI_LOCAL_REVIEW_COMMAND="$value" ;;
      esac
    fi
  done < ".codex-review.toml"

  if [ "${#AI_REVIEWERS[@]}" -eq 0 ] && [ "$AI_REVIEW_ENABLED" != "false" ]; then
    AI_REVIEWERS=("codex")
  fi
}

check_ai_reviewer() {
  local reviewer="$1"

  case " $AI_REVIEWERS_CHECKED " in
    *" $reviewer "*) return 0 ;;
  esac
  AI_REVIEWERS_CHECKED="$AI_REVIEWERS_CHECKED $reviewer"

  case "$reviewer" in
    codex)
      if command -v codex >/dev/null 2>&1; then
        if codex login status >/dev/null 2>&1; then
          ok "codex installed and authenticated"
        else
          warn "codex installed but not logged in. Run: codex login"
        fi
      elif command -v npm >/dev/null 2>&1; then
        warn "Installing Codex CLI..."
        npm install -g @openai/codex 2>/dev/null && ok "codex installed — run: codex login" || warn "codex install failed. Manual install: npm install -g @openai/codex"
      else
        warn "codex not installed. Install Node.js/npm first, then: npm install -g @openai/codex && codex login"
      fi
      ;;
    claude)
      if command -v claude >/dev/null 2>&1; then
        if claude auth status >/dev/null 2>&1; then
          ok "claude installed and authenticated"
        else
          warn "claude installed but auth check failed. Authenticate Claude before relying on review."
        fi
      else
        warn "claude reviewer selected, but Claude CLI is not installed."
      fi
      ;;
    gemini)
      if command -v gemini >/dev/null 2>&1; then
        if [ -n "${GEMINI_API_KEY:-}" ] || { command -v gcloud >/dev/null 2>&1 && gcloud auth print-access-token >/dev/null 2>&1; }; then
          ok "gemini installed and authenticated"
        else
          warn "gemini installed but auth is not configured. Set GEMINI_API_KEY or authenticate gcloud."
        fi
      else
        warn "gemini reviewer selected, but Gemini CLI is not installed."
      fi
      ;;
    local)
      if [ -n "$AI_LOCAL_REVIEW_COMMAND" ]; then
        ok "local reviewer configured: $AI_LOCAL_REVIEW_COMMAND"
      else
        warn "local reviewer selected, but [review.local].command is empty in .codex-review.toml"
      fi
      ;;
    *)
      warn "unknown AI reviewer '$reviewer' in .codex-review.toml"
      ;;
  esac
}

load_ai_review_config
if [ "$AI_REVIEW_ENABLED" = "false" ]; then
  ok "AI review disabled in .codex-review.toml"
else
  if truthy "$AI_REVIEW_ROUTING_ENABLED"; then
    ok "review routing enabled — local/small-diff routes can use <= ${AI_REVIEW_ROUTING_SMALL_MAX} diff lines"
  fi
  for reviewer in "${AI_REVIEWERS[@]}"; do
    check_ai_reviewer "$reviewer"
  done
fi

# --------------------------------------------------------------------------
# 5. Git workflow helpers (optional)
# --------------------------------------------------------------------------
info "Checking Git workflow"

install_gitbutler_cli() {
  local installer install_status

  if ! command -v curl >/dev/null 2>&1; then
    warn "curl is required for the official GitButler installer."
    return 1
  fi

  installer="$(mktemp -t gitbutler-install.XXXXXX)"
  if curl -fsSL https://gitbutler.com/install.sh -o "$installer"; then
    sh "$installer"
    install_status=$?
    rm -f "$installer"
    return "$install_status"
  else
    rm -f "$installer"
    return 1
  fi
}

configure_gitbutler_mcp() {
  if ! truthy "$GITBUTLER_MCP"; then
    return 0
  fi

  if ! command -v claude >/dev/null 2>&1; then
    warn "GitButler MCP requested, but Claude Code is not installed. Later: claude mcp add gitbutler but mcp"
    return 0
  fi

  if claude mcp list 2>/dev/null | grep -q '^gitbutler:'; then
    ok "GitButler MCP already configured for Claude Code"
    return 0
  fi

  warn "GitButler MCP lets AI agents ask GitButler to record branches/savepoints."
  if [ -t 0 ] && prompt_yes_no "Add GitButler MCP to Claude Code now?" "false"; then
    claude mcp add gitbutler but mcp >/dev/null 2>&1 && ok "GitButler MCP added" || warn "GitButler MCP setup failed. Later: claude mcp add gitbutler but mcp"
  else
    warn "Later: claude mcp add gitbutler but mcp"
  fi
}

if [ "$GIT_WORKFLOW" = "gitbutler" ]; then
  ok "GitButler selected — useful for stacked branches, parallel work, undo history, and AI-agent savepoints"
  if command -v but >/dev/null 2>&1; then
    ok "but installed"
    if [ "$(git config --get touchstone.gitbutlerSetup 2>/dev/null || true)" = "configured" ]; then
      ok "GitButler setup already recorded for this clone"
    elif [ -t 0 ]; then
      warn "Run 'but setup' once to let GitButler manage this repo. Undo later with: but teardown"
      if prompt_yes_no "Run 'but setup' now?" "false"; then
        if but setup; then
          git config touchstone.gitbutlerSetup configured
          ok "GitButler repo setup complete"
        else
          warn "GitButler setup failed. Later: but setup"
        fi
      else
        warn "Later: but setup"
      fi
    else
      warn "GitButler selected. Run once when ready: but setup"
    fi
    configure_gitbutler_mcp
  else
    warn "GitButler selected, but 'but' CLI is not installed."
    warn "GitButler CLI install: curl -fsSL https://gitbutler.com/install.sh | sh"
    if [ -t 0 ] && prompt_yes_no "Run the official GitButler CLI installer now?" "false"; then
      install_gitbutler_cli && ok "GitButler installer finished" || warn "GitButler install failed"
    fi
  fi
else
  ok "plain Git workflow selected"
fi

# --------------------------------------------------------------------------
# 6. Sync touchstone files to latest
# --------------------------------------------------------------------------
info "Syncing touchstone files"
# Skip update if this IS the Touchstone repo (it's the source, not a downstream project).
if [ -f "bin/touchstone" ] && [ -f "lib/auto-update.sh" ]; then
  ok "this is the Touchstone repo — skipping self-update"
elif [ -f ".touchstone-version" ]; then
  touchstone update --check 2>&1 | grep -E "Already|Needs sync|Run: touchstone update" | head -5 | while read -r line; do
    ok "$line"
  done
  ok "touchstone sync status checked"
else
  warn "No .touchstone-version found — this project hasn't been bootstrapped."
  warn "Run: touchstone new $(pwd)"
fi

# --------------------------------------------------------------------------
# 7. Pre-commit hooks
# --------------------------------------------------------------------------
info "Setting up git hooks"
if [ -f ".pre-commit-config.yaml" ]; then
  # Clear core.hooksPath if set — it conflicts with pre-commit.
  git config --unset-all core.hooksPath 2>/dev/null || true
  # Install hook shims (environments install lazily on first run).
  pre-commit install 2>&1 | tail -1 | while read -r line; do ok "$line"; done
  pre-commit install --hook-type pre-push 2>&1 | tail -1 | while read -r line; do ok "$line"; done
  ok "pre-commit hooks installed (pre-commit, pre-push)"
else
  warn "No .pre-commit-config.yaml found — skipping hooks"
fi

# --------------------------------------------------------------------------
# 8. gh CLI auth check
# --------------------------------------------------------------------------
info "Checking GitHub auth"
if gh auth status 2>&1 | grep -q "Logged in"; then
  ok "gh authenticated"
else
  warn "gh not authenticated. Run: gh auth login"
fi

fi

# --------------------------------------------------------------------------
# 9. Project dependencies
# --------------------------------------------------------------------------
info "Installing project dependencies"

select_python_for_venv() {
  local python_dir="${1:-.}"
  local pyenv_python

  if [ -n "${PYTHON:-}" ]; then
    if command -v "$PYTHON" >/dev/null 2>&1; then
      command -v "$PYTHON"
      return 0
    fi
    fail "PYTHON is set but not executable: $PYTHON"
    return 1
  fi

  if command -v pyenv >/dev/null 2>&1; then
    if [ -f "$python_dir/.python-version" ] || [ -f ".python-version" ]; then
      pyenv_python="$(cd "$python_dir" && pyenv which python 2>/dev/null || true)"
      if [ -n "$pyenv_python" ] && [ -x "$pyenv_python" ]; then
        printf '%s\n' "$pyenv_python"
        return 0
      fi
    fi
  fi

  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi

  fail "Python is required to create a virtualenv"
  return 1
}

install_python_requirements() {
  local label="$1"
  local requirements_file="$2"
  local venv_dir="$3"
  local python_bin python_dir python_version
  python_dir="$(dirname "$requirements_file")"

  if [ ! -x "$venv_dir/bin/python" ]; then
    python_bin="$(select_python_for_venv "$python_dir")"
    python_version="$("$python_bin" --version 2>&1)"
    if [ ! -f "$python_dir/.python-version" ] && [ ! -f ".python-version" ]; then
      warn "No .python-version found — creating $venv_dir with $python_version"
    else
      ok "Using $python_version for $venv_dir"
    fi
    "$python_bin" -m venv "$venv_dir"
    ok "$venv_dir created"
  fi

  "$venv_dir/bin/python" -m pip install -r "$requirements_file" 2>&1 | tail -1 | while read -r line; do
    ok "$label dependencies installed: $line"
  done
}

install_uv_if_missing() {
  if command -v uv >/dev/null 2>&1; then
    return 0
  fi

  if command -v brew >/dev/null 2>&1; then
    warn "Installing uv..."
    brew install uv 2>/dev/null
    ok "uv installed"
    return 0
  fi

  warn "uv is required for uv.lock/pyproject.toml projects. Install uv first."
  return 1
}

install_uv_project() {
  local label="$1"
  local project_dir="$2"

  if install_uv_if_missing; then
    (cd "$project_dir" && uv sync) 2>&1 | tail -1 | while read -r line; do
      ok "$label dependencies synced: $line"
    done
  fi
}

CONFIG_PROJECT_TYPE=""
CONFIG_PACKAGE_MANAGER=""
CONFIG_TARGETS=""

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

load_touchstone_config() {
  local line key value

  [ -f ".touchstone-config" ] || return 0

  while IFS= read -r line || [ -n "$line" ]; do
    line="$(trim "$line")"
    [ -z "$line" ] && continue
    case "$line" in \#*) continue ;; esac
    case "$line" in *=*) ;; *) continue ;; esac

    key="$(trim "${line%%=*}")"
    value="$(trim "${line#*=}")"
    case "$key" in
      project_type|profile) CONFIG_PROJECT_TYPE="$value" ;;
      package_manager) CONFIG_PACKAGE_MANAGER="$value" ;;
      targets) CONFIG_TARGETS="$value" ;;
    esac
  done < ".touchstone-config"
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

install_node_dependencies() {
  local label="$1"
  local project_dir="$2"
  local package_manager="$3"

  [ -f "$project_dir/package.json" ] || [ -f "$project_dir/pnpm-workspace.yaml" ] || return 1

  if [ "$package_manager" = "auto" ] || [ -z "$package_manager" ]; then
    package_manager="$(detect_node_package_manager "$project_dir")"
  fi

  if { [ "$package_manager" = "pnpm" ] || [ "$package_manager" = "yarn" ]; } && command -v corepack >/dev/null 2>&1; then
    corepack enable 2>/dev/null || true
  fi

  case "$package_manager" in
    pnpm)
      if command -v pnpm >/dev/null 2>&1; then
        (cd "$project_dir" && pnpm install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses pnpm, but pnpm is not installed. Run: corepack enable or brew install pnpm"
      fi
      ;;
    yarn)
      if command -v yarn >/dev/null 2>&1; then
        (cd "$project_dir" && yarn install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses yarn, but yarn is not installed. Run: corepack enable"
      fi
      ;;
    bun)
      if command -v bun >/dev/null 2>&1; then
        (cd "$project_dir" && bun install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses bun, but bun is not installed."
      fi
      ;;
    npm|*)
      if command -v npm >/dev/null 2>&1; then
        (cd "$project_dir" && npm install) 2>&1 | tail -1 | while read -r line; do ok "$label dependencies installed: $line"; done
      else
        warn "$label uses npm, but npm is not installed. Install Node.js/npm first."
      fi
      ;;
  esac

  return 0
}

install_python_dependencies() {
  local label="$1"
  local project_dir="$2"

  if [ -f "$project_dir/uv.lock" ]; then
    install_uv_project "$label" "$project_dir"
  elif [ -f "$project_dir/pyproject.toml" ] && [ ! -f "$project_dir/requirements.txt" ]; then
    install_uv_project "$label" "$project_dir"
  elif [ -f "$project_dir/requirements.txt" ]; then
    if [ "$project_dir" = "." ]; then
      install_python_requirements "$label" "$project_dir/requirements.txt" ".venv"
    else
      install_python_requirements "$label" "$project_dir/requirements.txt" "$project_dir/.venv"
    fi
  else
    return 1
  fi

  return 0
}

install_rust_dependencies() {
  local label="$1"
  local project_dir="$2"

  [ -f "$project_dir/Cargo.toml" ] || return 1
  if command -v cargo >/dev/null 2>&1; then
    (cd "$project_dir" && cargo fetch) 2>&1 | tail -1 | while read -r line; do ok "$label crates fetched: $line"; done
  else
    warn "$label is Rust, but cargo is not installed."
  fi
  return 0
}

install_swift_dependencies() {
  local label="$1"
  local project_dir="$2"

  [ -f "$project_dir/Package.swift" ] || return 1
  if command -v swift >/dev/null 2>&1; then
    (cd "$project_dir" && swift package resolve) 2>&1 | tail -1 | while read -r line; do ok "$label packages resolved: $line"; done
  else
    warn "$label is Swift, but swift is not installed."
  fi
  return 0
}

install_go_dependencies() {
  local label="$1"
  local project_dir="$2"

  [ -f "$project_dir/go.mod" ] || return 1
  if command -v go >/dev/null 2>&1; then
    (cd "$project_dir" && go mod download) 2>&1 && ok "$label modules downloaded"
  else
    warn "$label is Go, but go is not installed."
  fi
  return 0
}

install_profile_dependencies() {
  local label="$1"
  local project_dir="$2"
  local profile="$3"

  if [ "$profile" = "auto" ] || [ -z "$profile" ]; then
    profile="$(detect_profile "$project_dir")"
  fi

  case "$profile" in
    node|typescript|ts) install_node_dependencies "$label" "$project_dir" "$CONFIG_PACKAGE_MANAGER" ;;
    python) install_python_dependencies "$label" "$project_dir" ;;
    rust) install_rust_dependencies "$label" "$project_dir" ;;
    swift) install_swift_dependencies "$label" "$project_dir" ;;
    go) install_go_dependencies "$label" "$project_dir" ;;
    generic|"") return 1 ;;
    *) warn "Unknown project_type '$profile' in .touchstone-config"; return 1 ;;
  esac
}

install_configured_targets() {
  local entry name path profile
  local -a target_entries=()

  [ -n "$CONFIG_TARGETS" ] || return 1

  IFS=',' read -r -a target_entries <<< "$CONFIG_TARGETS"
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
    if [ ! -d "$path" ]; then
      warn "target '$name' path not found: $path"
      continue
    fi
    install_profile_dependencies "$name" "$path" "$profile" || true
  done
}

load_touchstone_config

DEPS_FOUND=false
ROOT_PROFILE="${CONFIG_PROJECT_TYPE:-auto}"
if [ "$ROOT_PROFILE" = "generic" ] && [ "$(detect_profile ".")" != "generic" ]; then
  ROOT_PROFILE="$(detect_profile ".")"
fi

# --------------------------------------------------------------------------
# 8b. Per-profile dev tools
# --------------------------------------------------------------------------
# Language-specific linters/formatters that sentinel + touchstone-run expect.
# Each block is idempotent (check-before-install) and guarded on the relevant
# package manager being available — missing package managers print a hint and
# skip rather than failing setup. Runs in both default and --deps-only modes
# so tools stay fresh; skipped entirely by --skip-devtools or
# TOUCHSTONE_SKIP_DEVTOOLS=1 for CI / offline environments.
install_swift_devtools() {
  if ! command -v brew >/dev/null 2>&1; then
    warn "Homebrew not available — skipping swiftlint/swift-format install."
    warn "Manual install (once brew is present): brew install swiftlint swift-format"
    return 0
  fi
  if command -v swiftlint >/dev/null 2>&1; then
    ok "swiftlint installed"
  else
    warn "Installing swiftlint..."
    brew install swiftlint 2>/dev/null && ok "swiftlint installed" || warn "swiftlint install failed"
  fi
  # Note: Apple's swift-format (matches touchstone-run.sh and `touchstone doctor`),
  # NOT Nick Lockwood's swiftformat (different tool, different binary name).
  if command -v swift-format >/dev/null 2>&1; then
    ok "swift-format installed"
  else
    warn "Installing swift-format..."
    brew install swift-format 2>/dev/null && ok "swift-format installed" || warn "swift-format install failed"
  fi
}

install_go_devtools() {
  if ! command -v go >/dev/null 2>&1; then
    warn "go not installed — skipping golint install."
    warn "Manual install (once go is present): go install golang.org/x/lint/golint@latest"
    return 0
  fi
  if command -v golint >/dev/null 2>&1; then
    ok "golint installed"
  else
    warn "Installing golint..."
    go install golang.org/x/lint/golint@latest 2>/dev/null && ok "golint installed" || warn "golint install failed"
  fi
}

install_rust_devtools() {
  if ! command -v cargo >/dev/null 2>&1; then
    warn "cargo not installed — skipping clippy/rustfmt setup."
    warn "Manual install (once rustup is present): rustup component add clippy rustfmt"
    return 0
  fi
  if ! command -v rustup >/dev/null 2>&1; then
    warn "rustup not installed — clippy/rustfmt component install unavailable. Verify your toolchain provides them."
    return 0
  fi
  if cargo clippy --version >/dev/null 2>&1; then
    ok "clippy installed"
  else
    warn "Installing clippy..."
    rustup component add clippy 2>/dev/null && ok "clippy installed" || warn "clippy install failed"
  fi
  if cargo fmt --version >/dev/null 2>&1; then
    ok "rustfmt installed"
  else
    warn "Installing rustfmt..."
    rustup component add rustfmt 2>/dev/null && ok "rustfmt installed" || warn "rustfmt install failed"
  fi
}

install_profile_devtools() {
  local profile="$1"
  local label="${2:-}"
  case "$profile" in
    swift)
      info "Checking Swift dev tools${label:+ ($label)}"
      install_swift_devtools
      ;;
    go)
      info "Checking Go dev tools${label:+ ($label)}"
      install_go_devtools
      ;;
    rust)
      info "Checking Rust dev tools${label:+ ($label)}"
      install_rust_devtools
      ;;
    python|node|typescript|ts|generic|"")
      : # python/node own their ecosystems via requirements.txt / package.json; generic is a no-op.
      ;;
    *)
      : # Unknown profile — let dependency install surface the warning.
      ;;
  esac
}

install_configured_target_devtools() {
  local entry name path profile
  local -a target_entries=()

  [ -n "$CONFIG_TARGETS" ] || return 0

  IFS=',' read -r -a target_entries <<< "$CONFIG_TARGETS"
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
    [ -d "$path" ] || continue
    if [ "$profile" = "auto" ] || [ -z "$profile" ]; then
      profile="$(detect_profile "$path")"
    fi
    install_profile_devtools "$profile" "$name"
  done
}

if [ "$SKIP_DEVTOOLS" = true ]; then
  info "Skipping per-profile dev tools (--skip-devtools / TOUCHSTONE_SKIP_DEVTOOLS=1)"
else
  # Resolve 'auto' / empty to a concrete profile the same way
  # install_profile_dependencies does — otherwise a repo with
  # project_type=auto or no .touchstone-config installs deps but silently
  # skips the verifier tools.
  ROOT_PROFILE_RESOLVED="$ROOT_PROFILE"
  if [ "$ROOT_PROFILE_RESOLVED" = "auto" ] || [ -z "$ROOT_PROFILE_RESOLVED" ]; then
    ROOT_PROFILE_RESOLVED="$(detect_profile ".")"
  fi
  install_profile_devtools "$ROOT_PROFILE_RESOLVED"
  # Monorepo targets declared in .touchstone-config also need their dev tools —
  # touchstone-run.sh / doctor validate them per-target, so missing tools would
  # re-create the same silent-skip failure mode this block is meant to close.
  install_configured_target_devtools
fi

if install_profile_dependencies "Project" "." "$ROOT_PROFILE"; then
  DEPS_FOUND=true
fi

# Backward-compatible nested Python agent support.
if install_python_dependencies "agent Python" "agent"; then
  DEPS_FOUND=true
fi

if [ "$DEPS_FOUND" = false ] && install_configured_targets; then
  DEPS_FOUND=true
fi

if [ "$DEPS_FOUND" = false ]; then
  ok "No recognized dependency file — skipping"
fi

# --------------------------------------------------------------------------
# 9. Summary
# --------------------------------------------------------------------------
echo ""
info "Setup complete"
echo ""
printf "  Run ${BOLD}touchstone doctor${RESET} to verify everything.\n"
printf "  Run ${BOLD}touchstone status${RESET} to see project health.\n"
echo ""
