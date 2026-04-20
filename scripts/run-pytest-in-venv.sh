#!/usr/bin/env bash
#
# Run pytest with the project's virtualenv Python instead of system python3.
#
# Usage:
#   bash scripts/run-pytest-in-venv.sh tests/
#
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
cd "$REPO_ROOT"

find_python() {
  local candidate

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

  echo "ERROR: no project virtualenv found for pytest." >&2
  echo "       Expected .venv/bin/python or agent/.venv/bin/python." >&2
  echo "       Run: bash setup.sh" >&2
  return 1
}

PYTHON_BIN="$(find_python)"
exec "$PYTHON_BIN" -m pytest "$@"
