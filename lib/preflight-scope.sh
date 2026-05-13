#!/usr/bin/env bash
#
# Shared helpers for scope-aware deterministic preflight checks.

compute_changed_paths_against() {
  local base_ref="$1"

  if [ -z "$base_ref" ]; then
    echo "ERROR: compute_changed_paths_against requires a base ref." >&2
    return 2
  fi
  if ! git rev-parse --verify --quiet "$base_ref^{commit}" >/dev/null 2>&1; then
    echo "ERROR: preflight diff base '$base_ref' does not resolve to a commit." >&2
    return 2
  fi

  git diff --name-only "$base_ref"...HEAD
}
