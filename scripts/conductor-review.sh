#!/usr/bin/env bash
#
# Preferred Conductor review entry point.
#
# The implementation still accepts the legacy codex-review protocol names
# (`CODEX_REVIEW_*`, CODEX_REVIEW_CLEAN/FIXED/BLOCKED) for compatibility.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec "$SCRIPT_DIR/codex-review.sh" "$@"
