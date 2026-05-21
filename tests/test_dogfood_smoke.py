from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOGFOOD = ROOT / "scripts" / "conductor-dogfood-smoke.py"
PREFLIGHT = ROOT / "lib" / "preflight.sh"
MERGE_PR = ROOT / "scripts" / "merge-pr.sh"
VALIDATE_WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"


def test_deterministic_dogfood_smoke_passes() -> None:
    result = subprocess.run(
        [sys.executable, str(DOGFOOD)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "dogfood smoke: route/call/exec passed" in result.stdout


def test_merge_and_ci_paths_run_dogfood_smoke() -> None:
    preflight = PREFLIGHT.read_text(encoding="utf-8")
    merge_pr = MERGE_PR.read_text(encoding="utf-8")
    validate = VALIDATE_WORKFLOW.read_text(encoding="utf-8")

    assert "touchstone_preflight_dogfood_smoke" in preflight
    assert "scripts/conductor-dogfood-smoke.py" in preflight
    assert "scripts/conductor-dogfood-smoke.py" in merge_pr
    assert "Deterministic Conductor dogfood smoke" in validate
    assert "scripts/conductor-dogfood-smoke.py" in validate
