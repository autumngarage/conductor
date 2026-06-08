from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "touchstone-run.sh"
PRE_COMMIT_CONFIG = Path(__file__).resolve().parents[1] / ".pre-commit-config.yaml"

pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="bash not installed")


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".cortex").mkdir()
    (repo / ".touchstone-config").write_text("project_type=generic\n", encoding="utf-8")
    return repo


def _path_with_fake_bin(fake_bin: Path) -> str:
    system_dirs = ["/bin", "/usr/bin"]
    return os.pathsep.join([str(fake_bin), *[path for path in system_dirs if Path(path).is_dir()]])


def _run_validate(repo: Path, fake_bin: Path) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": _path_with_fake_bin(fake_bin),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "validate"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _run_validate_affected(
    repo: Path,
    fake_bin: Path,
    changed_paths_file: Path,
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PATH": _path_with_fake_bin(fake_bin),
        "TOUCHSTONE_PREFLIGHT_CHANGED_PATHS_FILE": str(changed_paths_file),
    }
    return subprocess.run(
        ["bash", str(SCRIPT), "validate-affected"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_validate_does_not_require_optional_cortex_cli(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()

    result = _run_validate(repo, fake_bin)

    assert result.returncode == 0
    assert "cortex" not in result.stderr
    assert "generic project has no default 'lint' command" in result.stdout


def test_validate_does_not_refresh_cortex_index_when_cli_exists(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_file = tmp_path / "cortex.log"
    cortex = fake_bin / "cortex"
    cortex.write_text(
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> {log_file}\n",
        encoding="utf-8",
    )
    cortex.chmod(0o755)

    result = _run_validate(repo, fake_bin)

    assert result.returncode == 0
    assert result.stderr == ""
    assert not log_file.exists()


def test_validate_ignores_uv_cortex_fallback(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    uv = fake_bin / "uv"
    uv.write_text("#!/usr/bin/env bash\nexit 127\n", encoding="utf-8")
    uv.chmod(0o755)

    result = _run_validate(repo, fake_bin)

    assert result.returncode == 0
    assert "cortex" not in result.stderr
    assert "uv run" not in result.stderr


def test_conductor_refresh_hook_checks_optional_cli_before_invoking() -> None:
    text = PRE_COMMIT_CONFIG.read_text(encoding="utf-8")
    normalized = " ".join(text.split())

    assert "id: conductor-refresh" in text
    assert "command -v conductor" in text
    assert "command -v uv" in text
    assert "conductor-refresh: skipping because neither conductor nor uv is installed" in normalized


def test_validate_affected_runs_focused_python_targets(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    (repo / ".touchstone-config").write_text("project_type=python\n", encoding="utf-8")
    (repo / "src" / "conductor").mkdir(parents=True)
    (repo / "src" / "conductor" / "router.py").write_text("", encoding="utf-8")
    tests_dir = repo / "tests"
    tests_dir.mkdir()
    for name in ("test_router.py", "test_cli_v02.py", "test_review_cascade.py"):
        (tests_dir / name).write_text("", encoding="utf-8")
    changed_paths_file = tmp_path / "changed-paths.txt"
    changed_paths_file.write_text(
        "src/conductor/router.py\n.github/workflows/issue-claim-check.yml\n",
        encoding="utf-8",
    )
    log_file = tmp_path / "commands.log"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    ruff = fake_bin / "ruff"
    ruff.write_text(
        f"#!/usr/bin/env bash\nprintf 'ruff:%s\\n' \"$*\" >> {log_file}\n",
        encoding="utf-8",
    )
    ruff.chmod(0o755)
    venv_bin = repo / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    python = venv_bin / "python"
    python.write_text(
        f"#!/usr/bin/env bash\nprintf 'python:%s\\n' \"$*\" >> {log_file}\n",
        encoding="utf-8",
    )
    python.chmod(0o755)

    result = _run_validate_affected(repo, fake_bin, changed_paths_file)

    assert result.returncode == 0, result.stderr
    log = log_file.read_text(encoding="utf-8")
    assert "ruff:check src/conductor/router.py" in log
    assert (
        "python:-m pytest tests/test_router.py tests/test_cli_v02.py "
        "tests/test_review_cascade.py"
    ) in log
    assert "affected validate: no shell test targets" in result.stdout
