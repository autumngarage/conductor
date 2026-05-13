#!/usr/bin/env bash
#
# Run pytest with the project's virtualenv Python when available.
#
# Usage:
#   bash scripts/run-pytest-in-venv.sh tests/
#
set -euo pipefail

# tests/test-find-python-bin.sh sources this helper for in-process testing.
# In source-only mode, REPO_ROOT is fixed by the test fixture rather than
# resolved via git.
if [ "${RUN_PYTEST_IN_VENV_SOURCE_ONLY:-0}" = "1" ]; then
  REPO_ROOT="${RUN_PYTEST_IN_VENV_TEST_REPO_ROOT:-$(pwd)}"
else
  REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  cd "$REPO_ROOT"
fi

trim() {
  local value="$1"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  printf '%s' "$value"
}

find_python() {
  local candidate cwd parent_root parent_python fallback_python

  if [ -n "${PYTEST_PYTHON:-}" ]; then
    if command -v "$PYTEST_PYTHON" >/dev/null 2>&1; then
      candidate="$(command -v "$PYTEST_PYTHON")"
      if is_python_312 "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
      fi
      echo "ERROR: PYTEST_PYTHON must point to Python 3.12: $PYTEST_PYTHON" >&2
      return 1
    fi

    echo "ERROR: PYTEST_PYTHON is set but not executable: $PYTEST_PYTHON" >&2
    return 1
  fi

  for candidate in ".venv/bin/python" "agent/.venv/bin/python"; do
    if [ -x "$candidate" ]; then
      if is_python_312 "$candidate"; then
        printf '%s\n' "$candidate"
        return 0
      fi
      echo "ERROR: project virtualenv is not Python 3.12: $candidate" >&2
      echo "       Rebuild it with: python3.12 -m venv --clear .venv && .venv/bin/python -m pip install -e . -r requirements.txt -r requirements-test.txt" >&2
      return 1
    fi
  done

  cwd="$(pwd)"
  if parent_root="$(find_worktree_parent_root "$cwd")"; then
    parent_python="$parent_root/.venv/bin/python"
    if [ -x "$parent_python" ]; then
      if is_python_312 "$parent_python"; then
        printf '%s\n' "$parent_python"
        return 0
      fi
      echo "ERROR: parent worktree virtualenv is not Python 3.12: $parent_python" >&2
      return 1
    fi
  fi

  if fallback_python="$(find_python_with_pytest)"; then
    printf '%s\n' "$fallback_python"
    return 0
  fi

  echo "ERROR: no project virtualenv found." >&2
  echo "       Tried: $cwd/.venv/bin/python (this checkout)" >&2
  if [ -n "${parent_root:-}" ]; then
    echo "       Tried: $parent_root/.venv/bin/python (worktree parent)" >&2
  fi
  echo "       Tried: Python 3.12 on PATH with pytest installed" >&2
  echo "       Run \`bash setup.sh\` in this checkout, push from the" >&2
  echo "       parent checkout that has the venv set up, OR install test" >&2
  echo "       dependencies into the active Python environment." >&2
  return 1
}

is_python_312() {
  local python_bin="$1"
  "$python_bin" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 12) else 1)' >/dev/null 2>&1
}

find_python_with_pytest() {
  local candidate candidate_path

  for candidate in python3 python; do
    if ! candidate_path="$(command -v "$candidate" 2>/dev/null)"; then
      continue
    fi
    if ! is_python_312 "$candidate_path"; then
      continue
    fi
    if "$candidate_path" -c 'import pytest' >/dev/null 2>&1; then
      printf '%s\n' "$candidate_path"
      return 0
    fi
  done

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

if [ "${RUN_PYTEST_IN_VENV_SOURCE_ONLY:-0}" = "1" ]; then
  return 0
fi

PYTHON_BIN="$(find_python)"
exec "$PYTHON_BIN" -m pytest "$@"
