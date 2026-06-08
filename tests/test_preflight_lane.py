from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREFLIGHT = ROOT / "lib" / "preflight.sh"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _run_lane(tmp_path: Path, changed_paths: list[str], *, python_repo: bool = True) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    if python_repo:
        (repo / "pyproject.toml").write_text("[project]\nname = 'fixture'\n", encoding="utf-8")
    scripts_dir = repo / "scripts"
    scripts_dir.mkdir()
    (scripts_dir / "touchstone-run.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")

    script = textwrap.dedent(
        f"""
        set -euo pipefail
        source {shlex.quote(str(PREFLIGHT))}
        touchstone_preflight_changed_files() {{
          printf '%s\\n' "$CHANGED_PATHS"
        }}
        cd {shlex.quote(str(repo))}
        affected="$(touchstone_preflight_default_affected_command "$PWD" || true)"
        touchstone_preflight_validation_lane "$PWD" "$affected" ""
        """
    )
    env = {**os.environ, "CHANGED_PATHS": "\n".join(changed_paths)}
    for key in (
        "TOUCHSTONE_PREFLIGHT_VALIDATE_LANE",
        "TOUCHSTONE_PREFLIGHT_VALIDATE_AFFECTED_COMMAND",
        "TOUCHSTONE_PREFLIGHT_VALIDATE_SMOKE_COMMAND",
        "TOUCHSTONE_PREFLIGHT_VALIDATE_FULL_COMMAND",
    ):
        env.pop(key, None)

    result = subprocess.run(
        ["bash", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_python_issue_claim_workflow_diff_uses_affected_lane(tmp_path: Path) -> None:
    lane = _run_lane(
        tmp_path,
        [
            ".github/workflows/issue-claim-check.yml",
            "src/conductor/router.py",
            "tests/test_router.py",
        ],
    )

    assert lane == (
        "affected\tall changed paths are target-scoped and "
        "validate_affected_command is configured"
    )


def test_validation_workflow_still_forces_full_lane(tmp_path: Path) -> None:
    lane = _run_lane(tmp_path, [".github/workflows/validate.yml"])

    assert lane == "full\tCI validation workflow changed: .github/workflows/validate.yml"


def test_pre_commit_config_uses_affected_lane(tmp_path: Path) -> None:
    lane = _run_lane(tmp_path, [".pre-commit-config.yaml", "tests/test_touchstone_run.py"])

    assert lane == (
        "affected\tall changed paths are target-scoped and "
        "validate_affected_command is configured"
    )


def test_dependency_manifest_still_forces_full_lane(tmp_path: Path) -> None:
    lane = _run_lane(tmp_path, ["pyproject.toml", "src/conductor/router.py"])

    assert lane == "full\tdependency manifest changed: pyproject.toml"


@pytest.mark.parametrize(
    ("path", "reason"),
    [
        ("uv.lock", "Python dependency or build config changed: uv.lock"),
        (".touchstone-config", "CI or repository tooling config changed: .touchstone-config"),
        (
            ".github/workflows/release.yml",
            "release workflow changed: .github/workflows/release.yml",
        ),
        ("Dockerfile", "deployment container config changed: Dockerfile"),
    ],
)
def test_global_boundaries_still_force_full_lane(
    tmp_path: Path,
    path: str,
    reason: str,
) -> None:
    lane = _run_lane(tmp_path, [path, "src/conductor/router.py"])

    assert lane == f"full\t{reason}"


def test_unclassified_workflow_still_forces_full_lane(tmp_path: Path) -> None:
    lane = _run_lane(tmp_path, [".github/workflows/custom-deploy.yml"])

    assert lane == "full\tunclassified CI workflow changed: .github/workflows/custom-deploy.yml"


def test_non_python_source_path_still_forces_full_lane(tmp_path: Path) -> None:
    lane = _run_lane(tmp_path, ["src/conductor/frontend.ts"])

    assert lane == (
        "full\tunknown or unconfigured validation scope; full validation is the safe fallback"
    )


def test_non_python_repo_has_no_default_affected_lane(tmp_path: Path) -> None:
    lane = _run_lane(tmp_path, ["src/app.py"], python_repo=False)

    assert lane == (
        "full\tunknown or unconfigured validation scope; full validation is the safe fallback"
    )
