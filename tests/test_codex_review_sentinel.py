from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path


def test_codex_review_sentinel_shell_regression() -> None:
    repo = Path(__file__).resolve().parent.parent
    test_script = repo / "tests" / "test_codex_review_sentinel.sh"
    subprocess.run(["bash", str(test_script)], cwd=repo, check=True)


def test_codex_review_wrapper_accepts_footer_after_sentinel(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {
        **os.environ,
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
    conductor = fakes / "conductor"
    conductor.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env bash
            case "$1" in
              doctor)
                printf '{"configured": true}\\n'
                ;;
              exec)
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
            "NO_COLOR": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "ALL CLEAR" in result.stdout
    assert "malformed sentinel" not in result.stdout
