from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def test_merge_pr_worktree_path_shell_regression() -> None:
    repo = Path(__file__).resolve().parent.parent
    test_script = repo / "tests" / "test-merge-pr-worktree-handling.sh"
    subprocess.run(["bash", str(test_script)], cwd=repo, check=True)


def test_open_pr_verify_merged_state_fallback_shell_regression() -> None:
    repo = Path(__file__).resolve().parent.parent
    test_script = repo / "tests" / "test-open-pr-verify-merged.sh"
    subprocess.run(["bash", str(test_script)], cwd=repo, check=True)
