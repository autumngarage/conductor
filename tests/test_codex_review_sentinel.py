from __future__ import annotations

import os
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
