from __future__ import annotations

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MERGE_PR = ROOT / "scripts" / "merge-pr.sh"


def test_merge_pr_rejects_fail_open_marker_bypass_flag() -> None:
    result = subprocess.run(
        ["bash", str(MERGE_PR), "1", "--allow-fail-open-marker"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Unknown option: --allow-fail-open-marker" in result.stderr


def test_merge_pr_usage_does_not_advertise_fail_open_marker_bypass() -> None:
    result = subprocess.run(
        ["bash", str(MERGE_PR)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "--allow-fail-open-marker" not in result.stderr
