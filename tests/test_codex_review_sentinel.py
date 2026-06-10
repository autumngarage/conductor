from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import textwrap
from pathlib import Path


def test_codex_review_sentinel_shell_regression() -> None:
    repo = Path(__file__).resolve().parent.parent
    test_script = repo / "tests" / "test_codex_review_sentinel.sh"
    subprocess.run(["bash", str(test_script)], cwd=repo, check=True)


def _make_review_repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    sanitized_env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("GIT_") or k.startswith("PRE_COMMIT_"))
    }
    env = {
        **sanitized_env,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\nfeature\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=repo, env=env, check=True)

    (repo / ".codex-review.toml").write_text(
        textwrap.dedent(
            """
            [codex_review]
            max_iterations = 1
            max_diff_lines = 5000
            cache_clean_reviews = false
            safe_by_default = true
            mode = "review-only"
            on_error = "fail-open"
            unsafe_paths = []

            [review]
            enabled = true
            reviewer = "conductor"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".codex-review.toml"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "config"], cwd=repo, env=env, check=True)
    return repo, env


def test_codex_review_wrapper_accepts_footer_after_sentinel(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Strip inherited GIT_* and PRE_COMMIT_* env vars — when this test runs
    # inside a `git push` context (e.g., the pre-push hook via pre-commit),
    # git exports GIT_DIR / GIT_WORK_TREE pointing at the outer repo, and
    # pre-commit exports PRE_COMMIT_REMOTE_BRANCH naming the branch being
    # pushed. `scripts/codex-review.sh` reads PRE_COMMIT_REMOTE_BRANCH to
    # decide whether the push targets the default branch — without
    # stripping it, the script sees the outer feature-branch name, takes
    # the "not on main, skip" path, and the "ALL CLEAR" assertion fails.
    sanitized_env = {
        k: v
        for k, v in os.environ.items()
        if not (k.startswith("GIT_") or k.startswith("PRE_COMMIT_"))
    }
    env = {
        **sanitized_env,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "base"], cwd=repo, env=env, check=True)
    (repo / "README").write_text("base\nfeature\n", encoding="utf-8")
    subprocess.run(["git", "add", "README"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "feature"], cwd=repo, env=env, check=True)

    (repo / ".codex-review.toml").write_text(
        textwrap.dedent(
            """
            [codex_review]
            max_iterations = 1
            max_diff_lines = 5000
            cache_clean_reviews = false
            safe_by_default = true
            mode = "review-only"
            on_error = "fail-open"
            unsafe_paths = []

            [review]
            enabled = true
            reviewer = "conductor"
            """
        ).lstrip(),
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".codex-review.toml"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "config"], cwd=repo, env=env, check=True)

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor_args = tmp_path / "conductor-args.txt"
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_CONDUCTOR_ARGS:?}"
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n---\\nreview complete\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "FAKE_CONDUCTOR_ARGS": str(conductor_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CLEAR" in result.stdout
    assert "malformed sentinel" not in result.stdout
    conductor_invocations = conductor_args.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("review ") for line in conductor_invocations)
    assert not any(line.startswith("exec ") for line in conductor_invocations)


def test_codex_review_large_diff_uses_large_low_risk_route(tmp_path: Path) -> None:
    repo, env = _make_review_repo(tmp_path)
    large_body = "".join(f"generated line {i}\n" for i in range(450))
    (repo / "large.txt").write_text(large_body, encoding="utf-8")
    subprocess.run(["git", "add", "large.txt"], cwd=repo, env=env, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "large diff"], cwd=repo, env=env, check=True)

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor_args = tmp_path / "conductor-args.txt"
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_CONDUCTOR_ARGS:?}"
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "",
            "CODEX_REVIEW_MAX_STALL_SEC": "",
            "FAKE_CONDUCTOR_ARGS": str(conductor_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Review routing: larger low-risk diff" in result.stdout
    conductor_invocations = conductor_args.read_text(encoding="utf-8").splitlines()
    review_invocations = [
        line for line in conductor_invocations if line.startswith("review ")
    ]
    assert review_invocations, conductor_invocations
    assert all("--timeout" not in line for line in review_invocations)


def test_codex_review_wrapper_prefers_source_checkout_conductor(tmp_path: Path) -> None:
    repo, env = _make_review_repo(tmp_path)
    (repo / "pyproject.toml").write_text('[project]\nname = "conductor"\n', encoding="utf-8")
    (repo / "src" / "conductor").mkdir(parents=True)
    (repo / "src" / "conductor" / "cli.py").write_text("# source checkout\n", encoding="utf-8")

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    uv_args = tmp_path / "uv-args.txt"
    uv = fakes / "uv"
    uv.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_UV_ARGS:?}"
            if [ "$1" = "run" ] && [ "$2" = "conductor" ]; then
              shift 2
            else
              exit 9
            fi
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    uv.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "FAKE_UV_ARGS": str(uv_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    invocations = uv_args.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("run conductor doctor") for line in invocations)
    assert any(line.startswith("run conductor review ") for line in invocations)


def test_codex_review_trusted_config_temp_failure_fails_closed(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    mktemp = fakes / "mktemp"
    mktemp.write_text(
        "#!/usr/bin/env bash\nprintf 'mktemp unavailable\\n' >&2\nexit 1\n",
        encoding="utf-8",
    )
    mktemp.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD",
            "CODEX_REVIEW_PR_NUMBER": "1",
            "CODEX_REVIEW_TEST_PRINT_CONFIG": "1",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "failed to create temporary file for trusted review file" in result.stderr
    assert "HEAD:.codex-review.toml" in result.stderr
    assert "CONFIG_FILE=" not in result.stdout


def test_codex_review_trusted_config_show_failure_fails_closed(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    real_git = shutil.which("git")
    assert real_git is not None
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    git = fakes / "git"
    git.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env bash
            if [ "$1" = "show" ]; then
              printf 'simulated git show failure\\n' >&2
              exit 42
            fi
            exec {shlex.quote(real_git)} "$@"
            """
        ),
        encoding="utf-8",
    )
    git.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD",
            "CODEX_REVIEW_PR_NUMBER": "1",
            "CODEX_REVIEW_TEST_PRINT_CONFIG": "1",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert "simulated git show failure" in result.stderr
    assert "failed to materialize trusted review file" in result.stderr
    assert "HEAD:.codex-review.toml" in result.stderr
    assert "CONFIG_FILE=" not in result.stdout


def test_codex_review_wrapper_passes_configured_max_stall(tmp_path: Path) -> None:
    repo, env = _make_review_repo(tmp_path)
    config = repo / ".codex-review.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "max_diff_lines = 5000\n",
            "max_diff_lines = 5000\nmax_stall_sec = 300\n",
        ),
        encoding="utf-8",
    )

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor_args = tmp_path / "conductor-args.txt"
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_CONDUCTOR_ARGS:?}"
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "FAKE_CONDUCTOR_ARGS": str(conductor_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    review_invocations = [
        line
        for line in conductor_args.read_text(encoding="utf-8").splitlines()
        if line.startswith("review ")
    ]
    assert review_invocations
    assert any("--max-stall-seconds 300" in line for line in review_invocations)


def test_codex_review_wrapper_fail_opens_malformed_sentinel_by_default(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'I found a possible blocker but forgot the sentinel\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "output did not match the expected sentinel contract" in result.stdout
    assert "not blocking push (on_error=fail-open)" in result.stdout
    assert "[fail-open:FAIL_OPEN_PARSE_ERROR]" in result.stderr


def test_codex_review_wrapper_blocks_reviewer_error_when_fail_closed(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'provider chain exhausted after rate limits\\n' >&2
                exit 1
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
            env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
                "CODEX_REVIEW_ON_ERROR": "fail-closed",
                "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "review failed with exit 1" in result.stdout
    assert "blocking push (on_error=fail-closed)" in result.stderr


def test_codex_review_wrapper_rejects_blocked_without_actionable_findings(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor_args = tmp_path / "conductor-args.txt"
    summary_file = tmp_path / "review-summary.json"
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_CONDUCTOR_ARGS:?}"
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review)
                cat >/dev/null
                printf 'No blocking issues found, but I am not comfortable approving.\\n'
                printf 'CODEX_REVIEW_BLOCKED\\n'
                ;;
              exec)
                cat >/dev/null
                printf 'fix phase should not run\\n' >> README
                printf 'CODEX_REVIEW_FIXED\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "fix",
            "CODEX_REVIEW_ON_ERROR": "fail-closed",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "CODEX_REVIEW_SUMMARY_FILE": str(summary_file),
            "TOUCHSTONE_CONDUCTOR_FALLBACK_RETRY": "false",
            "FAKE_CONDUCTOR_ARGS": str(conductor_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "emitted BLOCKED without actionable findings" in result.stdout
    assert "not entering auto-fix" in result.stdout
    assert "blocking push (on_error=fail-closed)" in result.stderr

    conductor_invocations = conductor_args.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("review ") for line in conductor_invocations)
    assert not any(line.startswith("exec ") for line in conductor_invocations)

    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "blocked-no-findings"
    assert summary["findings"] == 0
    assert summary["review_status"] == "review_not_completed"

    head_subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_subject == "config"
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""


def test_codex_review_wrapper_retries_blocked_without_actionable_findings(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor_args = tmp_path / "conductor-args.txt"
    summary_file = tmp_path / "review-summary.json"
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_CONDUCTOR_ARGS:?}"
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review)
                cat >/dev/null
                case " $* " in
                  *" --exclude "*codex*)
                    printf '[conductor] review tried providers: gemini (success)\\n' >&2
                    printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n'
                    ;;
                  *)
                    printf '[conductor] review tried providers: codex (success)\\n' >&2
                    printf 'No blocking issues found, but I am not comfortable approving.\\n'
                    printf 'CODEX_REVIEW_BLOCKED\\n'
                    ;;
                esac
                ;;
              exec)
                cat >/dev/null
                printf 'fix phase should not run\\n' >> README
                printf 'CODEX_REVIEW_FIXED\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "fix",
            "CODEX_REVIEW_ON_ERROR": "fail-closed",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "CODEX_REVIEW_SUMMARY_FILE": str(summary_file),
            "FAKE_CONDUCTOR_ARGS": str(conductor_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "Review infrastructure/noncompliance failure: blocked without actionable findings"
        in result.stdout
    )
    assert "Retrying once with auto-routing" in result.stdout
    assert "ALL CLEAR" in result.stdout

    conductor_invocations = conductor_args.read_text(encoding="utf-8").splitlines()
    review_invocations = [
        line for line in conductor_invocations if line.startswith("review ")
    ]
    assert len(review_invocations) == 2
    assert "--exclude" in review_invocations[1]
    assert "codex" in review_invocations[1]
    assert not any(line.startswith("exec ") for line in conductor_invocations)

    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "clean"
    assert summary["findings"] == 0
    assert summary["fallback_attempted"] is True
    assert summary["fallback_primary_provider"] == "codex"
    assert summary["fallback_retry_provider"] == "gemini"
    assert summary["fallback_reason"] == "blocked without actionable findings"

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""


def test_codex_review_wrapper_requires_conductor_review_command(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor_args = tmp_path / "conductor-args.txt"
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_CONDUCTOR_ARGS:?}"
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review)
                exit 2
                ;;
              exec)
                cat >/dev/null
                printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "FAKE_CONDUCTOR_ARGS": str(conductor_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    conductor_invocations = conductor_args.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("review ") for line in conductor_invocations)
    assert not any(line.startswith("exec ") for line in conductor_invocations)
    assert "reviewer exit 2" in result.stdout


def test_codex_review_wrapper_emits_fix_classification_for_fixed_batch(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor_args = tmp_path / "conductor-args.txt"
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            printf '%s\\n' "$*" >> "${FAKE_CONDUCTOR_ARGS:?}"
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review)
                cat >/dev/null
                if grep -q 'fixed by fake conductor' README; then
                  printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n'
                else
                  printf -- '- README:1 - missing fake fix [fixable]\\n'
                  printf 'CODEX_REVIEW_BLOCKED\\n'
                fi
                ;;
              exec)
                cat >/dev/null
                printf 'fixed by fake conductor\\n' >> README
                printf 'Applied README:1.\\n'
                printf 'FIX_CLASSIFICATION: substantive; findings-addressed: 1\\n'
                printf 'CODEX_REVIEW_FIXED\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "fix",
            "CODEX_REVIEW_MAX_ITERATIONS": "2",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "FAKE_CONDUCTOR_ARGS": str(conductor_args),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (
        "[conductor] exec fix-classification: substantive (findings-addressed: 1)"
        in result.stdout
    )
    assert "ALL CLEAR" in result.stdout
    conductor_invocations = conductor_args.read_text(encoding="utf-8").splitlines()
    assert any(line.startswith("exec ") for line in conductor_invocations)

    head_subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert head_subject == "fix: address Conductor review findings (auto, fix, iter 1)"


def test_codex_review_wrapper_blocks_below_minimum_conductor_version(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    config = repo / ".codex-review.toml"
    config.write_text(
        config.read_text(encoding="utf-8")
        + "\n[review.conductor]\nminimum_version = \"99.0.0\"\n",
        encoding="utf-8",
    )
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$1" in
              --version)
                printf 'conductor, version 0.10.29\\n'
                ;;
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'LGTM\\nCODEX_REVIEW_CLEAN\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "review-only",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "requires conductor >= 99.0.0" in result.stderr
    assert "installed: 0.10.29" in result.stderr
    assert "brew update && brew upgrade autumngarage/conductor/conductor" in result.stderr
    assert "CODEX_REVIEW_CLEAN" not in result.stdout


def test_codex_review_wrapper_blocks_ambiguous_fixed_without_changes(
    tmp_path: Path,
) -> None:
    repo, env = _make_review_repo(tmp_path)
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    summary_file = tmp_path / "review-summary.json"
    review_log = tmp_path / "review-log.tsv"
    findings_history = tmp_path / "findings-history.jsonl"

    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              review|exec)
                cat >/dev/null
                printf 'Potential concern: freshness canary no longer covers the new source.\\n'
                printf 'CODEX_REVIEW_FIXED\\n'
                ;;
              *)
                exit 1
                ;;
            esac
            """
        ),
        encoding="utf-8",
    )
    conductor.chmod(0o755)

    script = Path(__file__).resolve().parent.parent / "scripts" / "codex-review.sh"
    result = subprocess.run(
        ["bash", str(script)],
        cwd=repo,
        env={
            **env,
            "PATH": f"{fakes}:{os.environ.get('PATH', '')}",
            "CODEX_REVIEW_BASE": "HEAD~1",
            "CODEX_REVIEW_MODE": "fix",
            "CODEX_REVIEW_DISABLE_CACHE": "1",
            "CODEX_REVIEW_TIMEOUT": "5",
            "CODEX_REVIEW_SUMMARY_FILE": str(summary_file),
            "CODEX_REVIEW_FINDINGS_HISTORY_FILE": str(findings_history),
            "TOUCHSTONE_REVIEW_LOG": str(review_log),
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stdout + result.stderr
    assert "emitted FIXED but no working-tree changes detected" in result.stdout
    assert "Treating as ambiguous" in result.stdout
    assert "Potential concern: freshness canary" in result.stdout
    assert "exit reason:    ambiguous-fixed-no-changes" in result.stdout

    summary = json.loads(summary_file.read_text(encoding="utf-8"))
    assert summary["exit_reason"] == "ambiguous-fixed-no-changes"
    assert summary["findings"] == 1

    history = json.loads(findings_history.read_text(encoding="utf-8").splitlines()[-1])
    assert history["result"] == "CODEX_REVIEW_BLOCKED"
    assert history["findings_count"] == 1
    assert "Potential concern: freshness canary" in history["findings"]
    assert "CODEX_REVIEW_FIXED" not in history["findings"]

    log_lines = review_log.read_text(encoding="utf-8").splitlines()
    assert log_lines
    assert "\tran\tambiguous-fixed-no-changes:" in log_lines[-1]
