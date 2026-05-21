"""Pytest wrapper for the verify_pr_merged regression tests (issue #489).

The actual assertions live in tests/test-open-pr-verify-merged.sh — bash
shell function is the system under test, so the cleanest contract is a
shell script that extracts the function and drives it with a fake gh.
This wrapper just guarantees the shell test runs in CI.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_open_pr_verify_merged_regression() -> None:
    repo = Path(__file__).resolve().parent.parent
    test_script = repo / "tests" / "test-open-pr-verify-merged.sh"
    subprocess.run(["bash", str(test_script)], cwd=repo, check=True)
