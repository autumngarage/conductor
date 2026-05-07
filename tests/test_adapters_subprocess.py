"""Tests for the subprocess-based adapters (claude, codex, gemini).

All three call external CLIs. We stub ``subprocess.run`` and
``shutil.which`` so tests run with no dependencies on the real binaries.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from conductor.cli import main
from conductor.providers.claude import (
    CLAUDE_CALL_FIRST_OUTPUT_TIMEOUT_SEC,
    CLAUDE_EXEC_FIRST_OUTPUT_TIMEOUT_SEC,
    ClaudeProvider,
)
from conductor.providers.codex import CODEX_STARTUP_PROBE_CONFIG, CodexProvider
from conductor.providers.gemini import GEMINI_AUTH_ENV_VARS, GeminiProvider
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderHTTPError,
    ProviderStalledError,
    ProviderStartupStalledError,
)
from conductor.session_log import (
    SESSION_DATA_TOKEN_COUNT,
    SESSION_DATA_USAGE,
    SESSION_EVENT_SUBAGENT_MESSAGE,
    SESSION_EVENT_TOOL_CALL,
    SESSION_EVENT_USAGE,
    SESSION_USAGE_OUTPUT_TOKENS,
    SessionLog,
)


def _strip_gemini_auth_env(monkeypatch) -> None:
    """Clear any GEMINI/GOOGLE auth env vars inherited from the host shell.

    Without this, tests that exercise the OAuth-file path silently
    succeed via the env-var fast path on developer machines.
    """
    for var in GEMINI_AUTH_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["stub"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("GIT_"):
            env.pop(key)
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _read_pid_file(path: Path) -> int:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if path.exists():
            return int(path.read_text(encoding="utf-8"))
        time.sleep(0.02)
    raise AssertionError(f"pid file was not written: {path}")


def _wait_for_pid_gone(pid: int, *, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pid_exists(pid):
            return True
        time.sleep(0.02)
    return not _pid_exists(pid)


def _repo_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test User")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "initial")
    worktree = tmp_path / "repo-linked"
    _git(repo, "worktree", "add", "-b", "feature/linked", str(worktree), "HEAD")
    return repo, worktree


# ---------------------------------------------------------------------------
# Claude
# ---------------------------------------------------------------------------

CLAUDE_JSON = """{
    "result": "hello from claude",
    "usage": {"input_tokens": 10, "output_tokens": 3, "cache_read_input_tokens": 2},
    "duration_ms": 1234,
    "total_cost_usd": 0.002,
    "session_id": "abc"
}"""


def test_claude_configured_when_cli_present_and_authed(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout='{"loggedIn": true}'),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is True and reason is None


def test_claude_configured_false_when_cli_missing(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value=None)
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "not found on PATH" in reason
    assert "CONDUCTOR_CLAUDE_CLI" in reason
    assert "non-interactive agent environment" in reason
    # Reason names the actionable login command + env-var fallback.
    assert "claude auth login" in reason
    assert "ANTHROPIC_API_KEY" in reason


def test_claude_uses_configured_cli_env_for_path_and_auth_probe(mocker, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_CLAUDE_CLI", "/opt/claude/bin/claude")
    mocker.patch(
        "conductor.providers.claude.shutil.which",
        side_effect=lambda cmd: cmd if cmd == "/opt/claude/bin/claude" else None,
    )
    run = mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout='{"loggedIn": true}'),
    )

    ok, reason = ClaudeProvider().configured()

    assert ok is True and reason is None
    assert run.call_args.args[0][0] == "/opt/claude/bin/claude"


def test_claude_configured_false_when_configured_cli_env_missing(
    mocker, monkeypatch
):
    monkeypatch.setenv("CONDUCTOR_CLAUDE_CLI", "/missing/claude")
    mocker.patch("conductor.providers.claude.shutil.which", return_value=None)

    ok, reason = ClaudeProvider().configured()

    assert ok is False
    assert "CONDUCTOR_CLAUDE_CLI" in reason
    assert "/missing/claude" in reason
    assert "does not point to an executable" in reason
    assert "non-interactive agent environment" in reason


def test_claude_configured_false_when_not_authed(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout='{"loggedIn": false}'),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "not authenticated" in reason
    assert "claude auth login" in reason
    assert "ANTHROPIC_API_KEY" in reason


def test_claude_configured_false_when_auth_probe_exits_nonzero(mocker):
    """Older CLI versions without `auth status` exit non-zero — surface,
    don't silently treat as authed."""
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(
            stderr="error: unknown command 'auth'", returncode=2
        ),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "exited 2" in reason
    assert "upgrade" in reason.lower()


def test_claude_configured_true_when_auth_probe_times_out_but_cli_health_passes(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        side_effect=[
            subprocess.TimeoutExpired(cmd="claude", timeout=15),
            _fake_completed(stdout="2.1.121 (Claude Code)"),
        ],
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is True and reason is None


def test_claude_configured_false_when_auth_and_health_probes_time_out(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=15),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "could not verify" in reason
    assert "--version" in reason


def test_claude_configured_false_when_auth_probe_returns_non_json(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout="hello not json"),
    )
    ok, reason = ClaudeProvider().configured()
    assert ok is False
    assert "not JSON" in reason


@pytest.mark.parametrize("provider_cls,module_path,cli_name", [
    (ClaudeProvider, "conductor.providers.claude", "claude"),
    (CodexProvider, "conductor.providers.codex", "codex"),
    (GeminiProvider, "conductor.providers.gemini", "gemini"),
])
def test_cli_health_probe_success(mocker, provider_cls, module_path, cli_name):
    mocker.patch(f"{module_path}.shutil.which", return_value=f"/usr/bin/{cli_name}")
    mocker.patch(
        f"{module_path}.subprocess.run",
        return_value=_fake_completed(stdout=f"{cli_name} 1.2.3"),
    )
    ok, reason = provider_cls().health_probe(timeout_sec=7)
    assert ok is True and reason is None


@pytest.mark.parametrize("provider_cls,module_path,cli_name", [
    (ClaudeProvider, "conductor.providers.claude", "claude"),
    (CodexProvider, "conductor.providers.codex", "codex"),
    (GeminiProvider, "conductor.providers.gemini", "gemini"),
])
def test_cli_health_probe_timeout(mocker, provider_cls, module_path, cli_name):
    mocker.patch(f"{module_path}.shutil.which", return_value=f"/usr/bin/{cli_name}")
    mocker.patch(
        f"{module_path}.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=cli_name, timeout=7),
    )
    ok, reason = provider_cls().health_probe(timeout_sec=7)
    assert ok is False
    assert "timed out" in reason


@pytest.mark.parametrize("provider_cls,module_path,cli_name", [
    (ClaudeProvider, "conductor.providers.claude", "claude"),
    (CodexProvider, "conductor.providers.codex", "codex"),
    (GeminiProvider, "conductor.providers.gemini", "gemini"),
])
def test_cli_health_probe_nonzero_exit(mocker, provider_cls, module_path, cli_name):
    mocker.patch(f"{module_path}.shutil.which", return_value=f"/usr/bin/{cli_name}")
    mocker.patch(
        f"{module_path}.subprocess.run",
        return_value=_fake_completed(stderr="broken", returncode=2),
    )
    ok, reason = provider_cls().health_probe()
    assert ok is False
    assert "exited 2" in reason


@pytest.mark.parametrize("provider_cls,module_path", [
    (ClaudeProvider, "conductor.providers.claude"),
    (CodexProvider, "conductor.providers.codex"),
    (GeminiProvider, "conductor.providers.gemini"),
])
def test_cli_health_probe_missing_binary(mocker, provider_cls, module_path):
    mocker.patch(f"{module_path}.shutil.which", return_value=None)
    ok, reason = provider_cls().health_probe()
    assert ok is False
    assert "not found on PATH" in reason


def test_claude_call_returns_normalized_response(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout=CLAUDE_JSON),
    )

    response = ClaudeProvider().call("hi")

    assert response.text == "hello from claude"
    assert response.provider == "claude"
    assert response.model == "sonnet"
    assert response.usage["input_tokens"] == 10
    assert response.usage["output_tokens"] == 3
    assert response.usage["cached_tokens"] == 2
    assert response.usage["effort"] == "medium"
    assert response.usage["thinking_budget"] == 8_000
    assert response.cost_usd == 0.002
    assert response.duration_ms == 1234
    assert response.session_id == "abc"


def test_claude_review_uses_native_review_slash_command_read_only(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0, CLAUDE_JSON)])

    def factory(args, **kwargs):
        fake.args = args
        return fake

    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=factory,
    )

    response = ClaudeProvider().review(
        "Focus on regressions.",
        base="origin/main",
        effort="high",
        cwd="/tmp/repo",
        timeout_sec=30,
    )

    assert fake.args is not None
    assert fake.args[0:2] == ["claude", "-p"]
    prompt = fake.args[2]
    assert prompt.startswith("/review")
    assert "Review changes against base branch: origin/main" in prompt
    assert "Focus on regressions." in prompt
    assert "--permission-mode" in fake.args
    assert fake.args[fake.args.index("--permission-mode") + 1] == "plan"
    assert response.provider == "claude"
    assert response.text == "hello from claude"


def test_claude_review_repairs_missing_requested_sentinel(mocker, capsys):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(
        stdout_schedule=[
            (
                0,
                json.dumps(
                    {
                        "result": "I found risky behavior but omitted the marker.",
                        "usage": {},
                    }
                ),
            )
        ]
    )
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    response = ClaudeProvider().review("End with CODEX_REVIEW_CLEAN or BLOCKED.")

    assert response.text.endswith("\nCODEX_REVIEW_BLOCKED")
    assert "CODEX_REVIEW_CLEAN" not in response.text
    assert "[conductor] claude review repaired missing Touchstone sentinel" in (
        capsys.readouterr().err
    )


def test_claude_call_passes_resume_session_id_as_resume_flag(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    captured = mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout=CLAUDE_JSON),
    )
    ClaudeProvider().call("hi", resume_session_id="abc-123")
    args = captured.call_args.args[0]
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "abc-123"


def test_claude_call_omits_resume_flag_when_session_id_none(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    captured = mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout=CLAUDE_JSON),
    )
    ClaudeProvider().call("hi")
    args = captured.call_args.args[0]
    assert "--resume" not in args


def test_claude_call_raises_config_error_when_cli_missing(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value=None)
    with pytest.raises(ProviderConfigError):
        ClaudeProvider().call("hi")


def test_claude_call_raises_on_non_zero_exit(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stderr="auth failed", returncode=1),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        ClaudeProvider().call("hi")
    assert "auth failed" in str(exc.value)


def test_claude_call_raises_on_is_error_true(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(
            stdout='{"is_error": true, "result": "permission denied"}'
        ),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        ClaudeProvider().call("hi")
    assert "permission denied" in str(exc.value)


def test_claude_call_raises_on_malformed_json(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        return_value=_fake_completed(stdout="not json"),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        ClaudeProvider().call("hi")
    assert "json" in str(exc.value).lower()


def test_claude_timeout_maps_to_provider_error(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="claude", timeout=1),
    )
    with pytest.raises(ProviderError) as exc:
        ClaudeProvider().call("hi")
    assert "timed out" in str(exc.value)


def test_claude_exec_surfaces_auth_prompt_and_records_notice(mocker, capsys):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(
        stdout_schedule=[(0, CLAUDE_JSON)],
        stderr_schedule=[
            (0, "Please visit https://claude.ai/oauth/authorize to authenticate\n")
        ],
    )
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    response = ClaudeProvider().exec("hi")

    assert response.auth_prompts == [
        {
            "provider": "claude",
            "message": "provider is waiting for OAuth completion",
            "source": "stderr",
            "url": "https://claude.ai/oauth/authorize",
        }
    ]
    err = capsys.readouterr().err
    assert "[conductor] auth required for claude" in err
    assert "https://claude.ai/oauth/authorize" in err


def test_claude_exec_fails_fast_on_quota_stderr(mocker, tmp_path):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(
        stdout_schedule=[],
        stderr_schedule=[
            (
                0,
                '{"api_error_status":429,"result":"You have hit your limit"}\n',
            )
        ],
        hang_after_stdout=True,
    )
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )
    session_log = SessionLog(path=tmp_path / "claude-quota.ndjson")

    with pytest.raises(ProviderHTTPError) as exc:
        ClaudeProvider(exec_first_output_timeout_sec=0.5).exec(
            "hi",
            max_stall_sec=0.5,
            session_log=session_log,
        )

    assert fake.terminated is True
    assert "rate limit HTTP 429" in str(exc.value)
    assert "hit your limit" in str(exc.value)
    events = [
        json.loads(line)
        for line in session_log.log_path.read_text(encoding="utf-8").splitlines()
    ]
    error_event = next(event for event in events if event["event"] == "error")
    assert error_event["data"]["reason"] == "provider_terminal_failure"
    assert error_event["data"]["category"] == "rate-limit"
    assert error_event["data"]["status_code"] == 429


def test_claude_exec_stall_watchdog_kills_silent_provider_and_logs_error(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )
    session_log = SessionLog(path=tmp_path / "claude-stall.ndjson")

    with pytest.raises(ProviderStalledError) as exc:
        ClaudeProvider().exec(
            "hi",
            max_stall_sec=0.05,
            session_log=session_log,
        )

    assert fake.terminated is True
    assert "claude CLI stalled" in str(exc.value)
    events = [
        json.loads(line)
        for line in session_log.log_path.read_text(encoding="utf-8").splitlines()
    ]
    error_event = next(event for event in events if event["event"] == "error")
    assert error_event["data"]["reason"] == "no_provider_response_within_0.05s"
    assert error_event["data"]["last_event"] == "provider_started"


def test_claude_exec_first_output_watchdog_is_separate_from_mid_task_stall(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )
    session_log = SessionLog(path=tmp_path / "claude-first-output-stall.ndjson")

    with pytest.raises(ProviderStalledError) as exc:
        ClaudeProvider(exec_first_output_timeout_sec=0.05).exec(
            "hi",
            timeout_sec=1,
            max_stall_sec=30,
            session_log=session_log,
        )

    assert fake.terminated is True
    assert "claude exec stalled at first_output" in str(exc.value)
    events = [
        json.loads(line)
        for line in session_log.log_path.read_text(encoding="utf-8").splitlines()
    ]
    diagnostic = next(
        event
        for event in events
        if event["event"] == "provider_diagnostic"
        and event["data"]["check"] == "claude_exec_watchdogs"
    )
    assert diagnostic["data"]["first_output_timeout_sec"] == 0.05
    assert diagnostic["data"]["max_stall_sec"] == 30
    error_event = next(event for event in events if event["event"] == "error")
    assert error_event["data"]["reason"] == "no_initial_provider_output_within_0.05s"
    assert error_event["data"]["phase"] == "first_output"
    assert error_event["data"]["last_event"] == "provider_started"


def test_claude_exec_startup_stall_diagnostic_includes_processes_and_locks(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / "auth.lock").write_text("", encoding="utf-8")
    (claude_dir / "session.tmp").write_text("", encoding="utf-8")
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude._run_ps_for_claude_probe",
        return_value=(
            0,
            (
                "123 1 claude -p hi\n"
                "456 1 /usr/local/bin/claude --model sonnet\n"
                "789 1 conductor exec --with claude\n"
            ),
            "",
        ),
    )
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    with pytest.raises(ProviderStartupStalledError) as exc:
        ClaudeProvider(exec_first_output_timeout_sec=0.01).exec(
            "hi",
            timeout_sec=1,
            max_stall_sec=30,
        )

    assert fake.terminated is True
    message = str(exc.value)
    assert "claude exec stalled at first_output" in message
    assert "Detected 2 other live `claude` processes" in message
    assert "PIDs: 123, 456" in message
    assert "auth.lock" in message
    assert "session.tmp" in message
    assert "--retry-on-stall 1" in message


def test_claude_exec_retry_on_stall_respawns_once_then_reports_diagnostic(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude._run_ps_for_claude_probe",
        return_value=(0, "123 1 claude -p hi\n", ""),
    )
    mocker.patch("conductor.providers.claude.time.sleep")
    fakes = [
        _FakePopen(stdout_schedule=[], hang_after_stdout=True),
        _FakePopen(stdout_schedule=[], hang_after_stdout=True),
    ]
    popen = mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fakes.pop(0),
    )

    with pytest.raises(ProviderStartupStalledError) as exc:
        ClaudeProvider(exec_first_output_timeout_sec=0.01).exec(
            "hi",
            timeout_sec=1,
            max_stall_sec=30,
            retry_on_stall=1,
        )

    assert popen.call_count == 2
    assert "Retry attempts were exhausted" in str(exc.value)


def test_claude_exec_retry_on_stall_can_succeed_after_respawn(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude._run_ps_for_claude_probe",
        return_value=(0, "123 1 claude -p hi\n", ""),
    )
    mocker.patch("conductor.providers.claude.time.sleep")
    fakes = [
        _FakePopen(stdout_schedule=[], hang_after_stdout=True),
        _FakePopen(stdout_schedule=[(0, CLAUDE_JSON)]),
    ]
    popen = mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fakes.pop(0),
    )

    response = ClaudeProvider(exec_first_output_timeout_sec=0.01).exec(
        "hi",
        timeout_sec=1,
        max_stall_sec=30,
        retry_on_stall=1,
    )

    assert popen.call_count == 2
    assert response.text == "hello from claude"


@pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX-only")
def test_claude_exec_startup_stall_terminates_child_process_group(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    child_pid_file = tmp_path / "claude-child.pid"
    fake_claude = tmp_path / "fake-claude"
    fake_claude.write_text(
        "\n".join(
            [
                "#!/usr/bin/env python3",
                "import os",
                "import pathlib",
                "import subprocess",
                "import sys",
                "import time",
                "pidfile = pathlib.Path(os.environ['CONDUCTOR_TEST_CHILD_PID'])",
                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                "pidfile.write_text(str(child.pid), encoding='utf-8')",
                "time.sleep(60)",
            ]
        ),
        encoding="utf-8",
    )
    fake_claude.chmod(0o755)
    monkeypatch.setenv("CONDUCTOR_TEST_CHILD_PID", str(child_pid_file))

    child_pid: int | None = None
    try:
        with pytest.raises(ProviderStartupStalledError):
            ClaudeProvider(
                cli_command=str(fake_claude),
                exec_first_output_timeout_sec=1,
            ).exec("hi", timeout_sec=5, max_stall_sec=30)

        child_pid = _read_pid_file(child_pid_file)
        assert _wait_for_pid_gone(child_pid, timeout=2)
    finally:
        if child_pid is None and child_pid_file.exists():
            child_pid = _read_pid_file(child_pid_file)
        if child_pid is not None and _pid_exists(child_pid):
            os.kill(child_pid, signal.SIGKILL)


def test_cli_exec_retry_on_stall_wires_to_claude_provider(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    mocker.patch(
        "conductor.providers.claude._run_ps_for_claude_probe",
        return_value=(0, "123 1 claude -p hi\n", ""),
    )
    mocker.patch("conductor.providers.claude.time.sleep")
    fakes = [
        _FakePopen(stdout_schedule=[], hang_after_stdout=True),
        _FakePopen(stdout_schedule=[(0, CLAUDE_JSON)]),
    ]
    popen = mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fakes.pop(0),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--no-preflight",
            "--start-timeout",
            "0.01",
            "--retry-on-stall",
            "1",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert popen.call_count == 2
    assert "hello from claude" in result.output


def test_claude_exec_retry_on_stall_never_retries_mid_task_stall(
    mocker,
):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0, "{")], hang_after_stdout=True)
    popen = mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    with pytest.raises(ProviderStalledError):
        ClaudeProvider(exec_first_output_timeout_sec=1).exec(
            "hi",
            timeout_sec=1,
            max_stall_sec=0.01,
            retry_on_stall=1,
        )

    assert popen.call_count == 1
    assert fake.terminated is True


def test_claude_startup_timeout_defaults_split_call_and_exec():
    provider = ClaudeProvider()

    assert CLAUDE_CALL_FIRST_OUTPUT_TIMEOUT_SEC == 60.0
    assert CLAUDE_EXEC_FIRST_OUTPUT_TIMEOUT_SEC == 300.0
    assert provider._call_first_output_timeout_sec == 60.0
    assert provider._exec_first_output_timeout_sec == 300.0
    assert provider._effective_first_output_timeout(None) == 300.0


def test_claude_legacy_first_output_timeout_does_not_override_exec_default(
    mocker,
):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")

    def fake_run_subprocess_with_live_stderr(**kwargs):
        raise ProviderStartupStalledError(
            provider=kwargs["provider_name"],
            timeout_sec=kwargs["first_output_timeout_sec"],
        )

    mocker.patch(
        "conductor.providers.claude.run_subprocess_with_live_stderr",
        side_effect=fake_run_subprocess_with_live_stderr,
    )

    with pytest.raises(ProviderStartupStalledError) as exc:
        ClaudeProvider(first_output_timeout_sec=45).exec(
            "hi",
            timeout_sec=1,
            max_stall_sec=30,
        )

    assert exc.value.error_response["timeout_sec"] == 300.0
    assert "claude exec stalled at first_output (300s)" in exc.value.error_response["message"]


def test_claude_exec_start_timeout_allows_slow_first_byte(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0.06, CLAUDE_JSON)])
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--no-preflight",
            "--start-timeout",
            "0.24",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "hello from claude" in result.output
    assert fake.terminated is False


def test_claude_exec_start_timeout_fails_nonzero_with_error_shape(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0.06, CLAUDE_JSON)])
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--no-preflight",
            "--start-timeout",
            "0.03",
            "--task",
            "hi",
            "--json",
        ],
    )

    assert result.exit_code == 1
    assert fake.terminated is True
    payload = json.loads(result.stdout)
    assert payload["error"] == "provider_startup_stalled"
    assert payload["provider"] == "claude"
    assert payload["timeout_sec"] == 0.03
    assert payload["phase"] == "startup"
    assert "claude exec stalled at first_output (0.03s)" in payload["message"]


def test_claude_exec_zero_bytes_ever_fails_as_startup_stall(mocker):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    with pytest.raises(ProviderStartupStalledError) as exc:
        ClaudeProvider().exec(
            "hi",
            timeout_sec=1,
            start_timeout_sec=0.03,
            max_stall_sec=30,
        )

    assert fake.terminated is True
    assert exc.value.error_response["error"] == "provider_startup_stalled"
    assert exc.value.error_response["provider"] == "claude"
    assert exc.value.error_response["timeout_sec"] == 0.03
    assert exc.value.error_response["phase"] == "startup"


def test_claude_exec_start_timeout_zero_disables_startup_watchdog(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0.06, CLAUDE_JSON)])
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "claude",
            "--no-preflight",
            "--start-timeout",
            "0",
            "--timeout",
            "1",
            "--task",
            "hi",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "hello from claude" in result.output
    assert fake.terminated is False


def test_claude_exec_startup_watchdog_runs_when_mid_task_stall_disabled(
    mocker,
):
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    mocker.patch(
        "conductor.providers.claude.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    with pytest.raises(ProviderStartupStalledError) as exc:
        ClaudeProvider(exec_first_output_timeout_sec=0.01).exec(
            "hi",
            timeout_sec=1,
            max_stall_sec=None,
        )

    assert exc.value.error_response["error"] == "provider_startup_stalled"
    assert fake.terminated is True


def test_claude_exec_sets_pwd_to_configured_cwd_for_project_settings(
    mocker,
    monkeypatch,
    tmp_path,
):
    monkeypatch.setenv("PWD", "/not/the/project")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    repo = tmp_path / "repo"
    settings_dir = repo / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.json").write_text(
        json.dumps({"permissions": {"deny": ["Bash(rm:*)"]}}),
        encoding="utf-8",
    )
    (settings_dir / "settings.local.json").write_text(
        json.dumps({"permissions": {"allow": ["Bash(git status:*)"]}}),
        encoding="utf-8",
    )
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0, CLAUDE_JSON)])
    captured: dict[str, object] = {}

    def factory(args, **kwargs):
        fake.args = args
        captured.update(kwargs)
        return fake

    mocker.patch("conductor.providers.claude.subprocess.Popen", side_effect=factory)
    session_log = SessionLog(path=tmp_path / "claude-settings.ndjson")

    response = ClaudeProvider(exec_first_output_timeout_sec=1).exec(
        "hi",
        sandbox="workspace-write",
        cwd=str(repo),
        session_log=session_log,
    )

    assert response.text == "hello from claude"
    assert "--setting-sources" in fake.args
    assert fake.args[fake.args.index("--setting-sources") + 1] == "user,project,local"
    assert captured["cwd"] == str(repo.resolve())
    assert captured["env"]["PWD"] == str(repo.resolve())
    events = [
        json.loads(line)
        for line in session_log.log_path.read_text(encoding="utf-8").splitlines()
    ]
    settings_event = next(
        event
        for event in events
        if event["event"] == "provider_diagnostic"
        and event["data"]["check"] == "claude_project_settings"
    )
    assert settings_event["data"]["settings_json_exists"] is True
    assert settings_event["data"]["settings_local_exists"] is True
    assert settings_event["data"]["permissions_configured"] is True
    assert settings_event["data"]["permission_sources"] == [
        "settings.json",
        "settings.local.json",
    ]
    assert settings_event["data"]["permission_keys"] == ["allow", "deny"]
    assert settings_event["data"]["permission_mode"] is None


def test_claude_exec_rejects_invalid_project_settings_before_launch(
    mocker,
    tmp_path,
):
    repo = tmp_path / "repo"
    settings_dir = repo / ".claude"
    settings_dir.mkdir(parents=True)
    (settings_dir / "settings.local.json").write_text("{not json", encoding="utf-8")
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    popen = mocker.patch("conductor.providers.claude.subprocess.Popen")

    with pytest.raises(ProviderConfigError) as exc:
        ClaudeProvider().exec("hi", sandbox="workspace-write", cwd=str(repo))

    assert "invalid Claude project settings JSON" in str(exc.value)
    assert popen.call_count == 0


def test_claude_exec_allows_linked_worktree_cwd(
    mocker,
    tmp_path,
):
    _repo, worktree = _repo_with_linked_worktree(tmp_path)
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0, CLAUDE_JSON)])
    captured: dict[str, object] = {}

    def factory(args, **kwargs):
        fake.args = args
        captured.update(kwargs)
        return fake

    mocker.patch("conductor.providers.claude.subprocess.Popen", side_effect=factory)

    response = ClaudeProvider(exec_first_output_timeout_sec=1).exec(
        "hi",
        sandbox="workspace-write",
        cwd=str(worktree),
    )

    assert response.text == "hello from claude"
    assert captured["cwd"] == str(worktree.resolve())


def test_claude_exec_allows_read_only_linked_worktree_cwd(
    mocker,
    tmp_path,
):
    _repo, worktree = _repo_with_linked_worktree(tmp_path)
    mocker.patch("conductor.providers.claude.shutil.which", return_value="/usr/bin/claude")
    fake = _FakePopen(stdout_schedule=[(0, CLAUDE_JSON)])
    captured: dict[str, object] = {}

    def factory(args, **kwargs):
        fake.args = args
        captured.update(kwargs)
        return fake

    mocker.patch("conductor.providers.claude.subprocess.Popen", side_effect=factory)

    response = ClaudeProvider(exec_first_output_timeout_sec=1).exec(
        "hi",
        sandbox="read-only",
        cwd=str(worktree),
    )

    assert response.text == "hello from claude"
    assert captured["cwd"] == str(worktree.resolve())


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------

CODEX_NDJSON = (
    '{"type":"session.created","session_id":"sess-codex-1"}\n'
    '{"type":"item.started","item":{"type":"agent_message"}}\n'
    '{"type":"item.completed","item":{"type":"agent_message","text":"hello from codex"}}\n'
    '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n'
)


class _FakeScheduledPipe:
    def __init__(
        self,
        schedule: list[tuple[float, str]],
        *,
        hang_after_schedule: bool,
        terminated: threading.Event,
        on_eof,
        raise_after_schedule: BaseException | None = None,
        on_schedule_exhausted=None,
    ) -> None:
        self._schedule = schedule
        self._hang_after_schedule = hang_after_schedule
        self._terminated = terminated
        self._on_eof = on_eof
        self._raise_after_schedule = raise_after_schedule
        self._on_schedule_exhausted = on_schedule_exhausted
        self._schedule_exhausted = False
        self._idx = 0

    def readline(self) -> str:
        if self._idx < len(self._schedule):
            delay, line = self._schedule[self._idx]
            self._idx += 1
            if self._terminated.wait(delay):
                return ""
            return line
        if not self._schedule_exhausted:
            self._schedule_exhausted = True
            if self._on_schedule_exhausted is not None:
                self._on_schedule_exhausted()
        if self._raise_after_schedule is not None:
            raise self._raise_after_schedule
        if self._hang_after_schedule:
            self._terminated.wait()
        if self._on_eof is not None:
            self._on_eof()
        return ""

    def read(self, size: int = -1) -> str:
        if size == 0:
            return ""
        return self.readline()


class _FakePopen:
    _next_pid = 500000

    def __init__(
        self,
        *,
        stdout_schedule: list[tuple[float, str]],
        stderr_schedule: list[tuple[float, str]] | None = None,
        hang_after_stdout: bool = False,
        exit_after_stdout_schedule: bool = False,
        returncode: int = 0,
        stdout_reader_error: BaseException | None = None,
        stderr_reader_error: BaseException | None = None,
    ) -> None:
        self.args: list[str] | None = None
        self.cwd: str | Path | None = None
        self.env: dict[str, str] | None = None
        self.pid = _FakePopen._next_pid
        _FakePopen._next_pid += 1
        self.returncode: int | None = None
        self.terminated = False
        self.killed = False
        self._configured_returncode = returncode
        self._terminated_event = threading.Event()
        self._finished_event = threading.Event()
        self.stdout = _FakeScheduledPipe(
            stdout_schedule,
            hang_after_schedule=hang_after_stdout,
            terminated=self._terminated_event,
            on_eof=self._finish_success,
            raise_after_schedule=stdout_reader_error,
            on_schedule_exhausted=(
                self._finish_success if exit_after_stdout_schedule else None
            ),
        )
        self.stderr = _FakeScheduledPipe(
            stderr_schedule or [],
            hang_after_schedule=False,
            terminated=self._terminated_event,
            on_eof=None,
            raise_after_schedule=stderr_reader_error,
        )
        # codex now reads the prompt from stdin (`codex exec -`); mock a
        # writable pipe so the provider's write+close path doesn't blow up.
        self.stdin = io.StringIO()

    def _finish_success(self) -> None:
        if self.returncode is None:
            self.returncode = self._configured_returncode
            self._finished_event.set()

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        if not self._finished_event.wait(timeout):
            raise subprocess.TimeoutExpired(cmd=self.args or "codex", timeout=timeout)
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15
        self._terminated_event.set()
        self._finished_event.set()

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._terminated_event.set()
        self._finished_event.set()


def _patch_codex_popen(mocker, fake: _FakePopen):
    def factory(args, **kwargs):
        fake.args = args
        fake.cwd = kwargs.get("cwd")
        fake.env = kwargs.get("env")
        return fake

    return mocker.patch("conductor.providers.codex.subprocess.Popen", side_effect=factory)


def _patch_codex_popen_with_output_backstop(
    mocker, fake: _FakePopen, *, backstop_text: str
):
    def factory(args, **kwargs):
        fake.args = args
        output_path = Path(args[args.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(backstop_text, encoding="utf-8")
        return fake

    return mocker.patch("conductor.providers.codex.subprocess.Popen", side_effect=factory)


def test_codex_configured_when_cli_present_and_authed(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout="Logged in using ChatGPT"),
    )
    ok, reason = CodexProvider().configured()
    assert ok is True and reason is None


def test_codex_configured_false_when_cli_missing(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value=None)
    ok, reason = CodexProvider().configured()
    assert ok is False
    assert "not found on PATH" in reason
    assert "codex login" in reason
    assert "OPENAI_API_KEY" in reason


def test_codex_configured_false_when_not_authed(mocker):
    """`codex login status` exits non-zero when the user isn't authed."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stderr="not logged in", returncode=1),
    )
    ok, reason = CodexProvider().configured()
    assert ok is False
    assert "not authenticated" in reason
    assert "codex login" in reason
    assert "OPENAI_API_KEY" in reason
    assert "--with-api-key" in reason


def test_codex_configured_false_when_auth_probe_times_out(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="codex", timeout=5),
    )
    ok, reason = CodexProvider().configured()
    assert ok is False
    assert "could not verify" in reason


def test_codex_configured_false_when_exec_startup_probe_times_out(mocker):
    """Regression for #125: PATH + auth are not enough readiness.

    `conductor list` renders provider.configured(); codex must only report
    ready when the real `codex exec` startup path also completes inside a
    bounded timeout.
    """
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")

    def run(args, **kwargs):
        if args == ["codex", "login", "status"]:
            return _fake_completed(stdout="Logged in using ChatGPT")
        if args[:3] == ["codex", "exec", "-"]:
            raise subprocess.TimeoutExpired(
                cmd=args,
                timeout=kwargs["timeout"],
                stderr=(
                    "codex_models_manager: failed to refresh available models: "
                    "timeout waiting for child process to exit"
                ),
            )
        raise AssertionError(f"unexpected command: {args!r}")

    captured = mocker.patch("conductor.providers.codex.subprocess.run", side_effect=run)

    ok, reason = CodexProvider(startup_probe_timeout_sec=8).configured()

    assert ok is False
    assert "`codex exec` startup probe timed out after 8s" in reason
    assert "codex_models_manager" in reason
    startup_call = captured.call_args_list[1]
    assert startup_call.args[0][:3] == ["codex", "exec", "-"]
    assert startup_call.kwargs["timeout"] == 8
    assert startup_call.kwargs["input"] == "Reply with OK."


def test_codex_startup_probe_avoids_minimal_reasoning_tool_conflicts(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")

    def run(args, **kwargs):
        if args == ["codex", "login", "status"]:
            return _fake_completed(stdout="Logged in using ChatGPT")
        if args[:3] == ["codex", "exec", "-"]:
            config_values = [
                args[idx + 1] for idx, value in enumerate(args) if value == "-c"
            ]
            if "model_reasoning_effort=minimal" in config_values:
                web_search_disabled = (
                    "web_search='disabled'" in config_values
                    or 'web_search="disabled"' in config_values
                )
                tools_disabled = (
                    web_search_disabled
                    and "features.image_generation=false" in config_values
                )
                if not tools_disabled:
                    return _fake_completed(
                        stdout=json.dumps(
                            {
                                "type": "error",
                                "message": json.dumps(
                                    {
                                        "type": "error",
                                        "error": {
                                            "type": "invalid_request_error",
                                            "message": (
                                                "The following tools cannot be used with "
                                                "reasoning.effort 'minimal': "
                                                "image_gen, web_search."
                                            ),
                                            "param": "tools",
                                        },
                                        "status": 400,
                                    },
                                ),
                            },
                        ),
                        returncode=1,
                    )
            return _fake_completed(stdout='{"type":"turn.completed"}\n')
        raise AssertionError(f"unexpected command: {args!r}")

    captured = mocker.patch("conductor.providers.codex.subprocess.run", side_effect=run)

    ok, reason = CodexProvider().configured()

    assert ok is True and reason is None
    startup_args = captured.call_args_list[1].args[0]
    config_values = [
        startup_args[idx + 1]
        for idx, value in enumerate(startup_args)
        if value == "-c"
    ]
    assert config_values == list(CODEX_STARTUP_PROBE_CONFIG)
    assert "model_reasoning_effort=low" in config_values
    assert "model_reasoning_effort=minimal" not in config_values
    assert "features.web_search=false" not in config_values
    assert "features.image_gen=false" not in config_values


def test_codex_startup_probe_reports_api_error_instead_of_first_event(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-123"}',
            '{"type":"turn.started"}',
            json.dumps(
                {
                    "type": "error",
                    "message": json.dumps(
                        {
                            "type": "error",
                            "error": {
                                "type": "invalid_request_error",
                                "message": (
                                    "The following tools cannot be used with "
                                    "reasoning.effort 'minimal': image_gen, web_search."
                                ),
                                "param": "tools",
                            },
                            "status": 400,
                        }
                    ),
                }
            ),
            '{"type":"turn.failed"}',
        ]
    )

    def run(args, **kwargs):
        if args == ["codex", "login", "status"]:
            return _fake_completed(stdout="Logged in using ChatGPT")
        if args[:3] == ["codex", "exec", "-"]:
            return _fake_completed(stdout=stdout, returncode=1)
        raise AssertionError(f"unexpected command: {args!r}")

    mocker.patch("conductor.providers.codex.subprocess.run", side_effect=run)

    ok, reason = CodexProvider().configured()

    assert ok is False
    assert "`codex exec` startup probe exited 1" in reason
    assert "invalid_request_error" in reason
    assert "The following tools cannot be used" in reason
    assert "param=tools" in reason
    assert "thread.started" not in reason


def test_codex_startup_probe_reports_turn_failed_error_dict(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    stdout = json.dumps(
        {
            "type": "turn.failed",
            "error": {
                "type": "invalid_request_error",
                "message": "The request was rejected.",
                "param": "tools",
            },
        }
    )

    def run(args, **kwargs):
        if args == ["codex", "login", "status"]:
            return _fake_completed(stdout="Logged in using ChatGPT")
        if args[:3] == ["codex", "exec", "-"]:
            return _fake_completed(stdout=stdout, returncode=1)
        raise AssertionError(f"unexpected command: {args!r}")

    mocker.patch("conductor.providers.codex.subprocess.run", side_effect=run)

    ok, reason = CodexProvider().configured()

    assert ok is False
    assert "invalid_request_error: The request was rejected.: param=tools" in reason


def test_codex_call_parses_ndjson_and_usage(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )

    response = CodexProvider().call("hi")

    assert response.text == "hello from codex"
    assert response.provider == "codex"
    assert response.usage["input_tokens"] == 5
    assert response.usage["output_tokens"] == 2
    assert response.session_id == "sess-codex-1"


def test_codex_review_uses_native_review_command(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout="LGTM\n"),
    )

    response = CodexProvider().review(
        "Use AGENTS.md rubric.",
        effort="medium",
        base="origin/main",
        title="PR review",
    )

    args = captured.call_args.args[0]
    assert args[0:2] == ["codex", "review"]
    assert "-c" in args
    assert args[args.index("-c") + 1] == "model_reasoning_effort=medium"
    assert "--base" not in args
    assert "--title" not in args
    assert args[-1] == "-"
    prompt = captured.call_args.kwargs["input"]
    assert "Review changes against base branch/ref: origin/main" in prompt
    assert "Review title: PR review" in prompt
    assert "Use AGENTS.md rubric." in prompt
    assert response.provider == "codex"
    assert response.model == "codex-review"
    assert response.text == "LGTM"


def test_codex_review_repairs_missing_requested_sentinel(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout="The changes need operator review.\n"),
    )

    response = CodexProvider().review(
        "Return a final standalone CODEX_REVIEW_CLEAN or CODEX_REVIEW_BLOCKED line.",
    )

    assert response.text == (
        "The changes need operator review.\nCODEX_REVIEW_BLOCKED"
    )
    assert "[conductor] codex review repaired missing Touchstone sentinel" in (
        capsys.readouterr().err
    )


def test_codex_review_stall_watchdog_fails_fast(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured_timeout: float | None = None

    def fake_run(args, **kwargs):
        nonlocal captured_timeout
        captured_timeout = kwargs["timeout"]
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    mocker.patch("conductor.providers.codex.subprocess.run", side_effect=fake_run)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().review("Review this.", timeout_sec=60, max_stall_sec=0.05)

    assert captured_timeout == 0.05
    assert "codex review stalled after 0.05s" in str(exc.value)


def test_codex_call_reads_output_backstop_when_ndjson_loses_agent_message(
    mocker, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")

    def fake_run(args, **kwargs):
        output_path = Path(args[args.index("-o") + 1])
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text("hello from output backstop\n", encoding="utf-8")
        return _fake_completed(
            stdout=(
                '{"type":"session.created","session_id":"sess-codex-backstop"}\n'
                '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n'
            )
        )

    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        side_effect=fake_run,
    )

    response = CodexProvider().call("hi")

    args = captured.call_args.args[0]
    output_path = Path(args[args.index("-o") + 1])
    assert response.text == "hello from output backstop"
    assert output_path.exists()
    assert output_path.name.startswith("codex-exec-")
    assert response.raw["output_path"] == str(output_path)


def test_codex_call_translates_effort_to_reasoning_effort_config(mocker):
    """Codex CLI 0.125.0 dropped --effort in favor of `-c model_reasoning_effort=`.
    Conductor must emit the new form. Regression test for the silent breakage
    where conductor's call passed `--effort minimal` to a 0.125.0 codex CLI,
    which exited 2 with `error: unexpected argument '--effort' found`."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )
    CodexProvider().call("hi", effort="minimal")
    args = captured.call_args.args[0]
    # Old (broken) form must not appear.
    assert "--effort" not in args, (
        "codex CLI >= 0.125.0 removed --effort; conductor must use -c instead. "
        f"args={args!r}"
    )
    # New form: -c model_reasoning_effort=minimal must be present together.
    assert "-c" in args, f"missing -c flag, args={args!r}"
    c_idx = args.index("-c")
    assert args[c_idx + 1] == "model_reasoning_effort=minimal", (
        f"expected `model_reasoning_effort=minimal`, got {args[c_idx + 1]!r}"
    )


def test_codex_call_with_resume_uses_resume_subcommand(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )
    CodexProvider().call("follow-up", resume_session_id="sess-codex-1")
    args = captured.call_args.args[0]
    # `codex exec resume <id> -` is the documented shape (stdin-as-prompt
    # via PR openai/codex#15917; argv-as-prompt is no longer used).
    assert args[1] == "exec" and args[2] == "resume"
    assert args[3] == "sess-codex-1"
    assert args[4] == "-"
    # The actual prompt arrives via stdin (the `input=` kwarg).
    assert captured.call_args.kwargs.get("input") == "follow-up"
    # When resuming, --ephemeral does not apply (resume implies persistence).
    assert "--ephemeral" not in args


def test_codex_call_forwards_attachments_as_image_flags(mocker, tmp_path):
    """codex exec accepts `-i, --image <FILE>...` (repeatable). Conductor must
    forward each attachment as a separate `-i <path>` pair so the provider
    sees the file. Regression coverage for the cross-provider --attach plumbing."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )

    img1 = tmp_path / "screen.png"
    img1.write_bytes(b"\x89PNG\r\n\x1a\n")
    img2 = tmp_path / "diagram.png"
    img2.write_bytes(b"\x89PNG\r\n\x1a\n")

    CodexProvider().call("look at these", attachments=(img1, img2))

    args = captured.call_args.args[0]
    image_pairs = [
        (args[i], args[i + 1])
        for i in range(len(args) - 1)
        if args[i] == "-i"
    ]
    assert image_pairs == [
        ("-i", str(img1)),
        ("-i", str(img2)),
    ]


def test_codex_call_omits_image_flags_when_no_attachments(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )

    CodexProvider().call("hi")

    args = captured.call_args.args[0]
    assert "-i" not in args


def test_codex_call_raises_when_no_agent_message(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(
            stdout='{"type":"turn.completed","usage":{}}\n'
        ),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        CodexProvider().call("hi")
    assert "agent_message" in str(exc.value)


def test_codex_call_raises_on_non_zero_exit(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stderr="login required", returncode=1),
    )
    with pytest.raises(ProviderHTTPError) as exc:
        CodexProvider().call("hi")
    assert "login required" in str(exc.value)


def test_codex_exec_default_timeout_is_unbounded(mocker):
    """Regression: `conductor exec --with codex` (no --timeout) used to default
    to 300s, which silently killed long agent sessions and lost the session_id.
    The default is now no-timeout; exec uses the streaming Popen path and
    must complete without imposing a wall-clock cap when the caller omits it."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in CODEX_NDJSON.splitlines(keepends=True)]
    )
    captured = _patch_codex_popen(mocker, fake)
    CodexProvider().exec("hi")
    assert captured.called
    assert fake.terminated is False


def test_codex_exec_fails_fast_on_quota_stderr(mocker, tmp_path):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[],
        stderr_schedule=[
            (
                0,
                'ERROR: {"type":"error","status":429,'
                '"error":{"type":"rate_limit_exceeded",'
                '"message":"Too many requests"}}\n',
            )
        ],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    session_log = SessionLog(path=tmp_path / "codex-quota.ndjson")

    with pytest.raises(ProviderHTTPError) as exc:
        CodexProvider().exec(
            "hi",
            max_stall_sec=0.5,
            liveness_interval_sec=0,
            session_log=session_log,
        )

    assert fake.terminated is True
    assert "rate limit HTTP 429" in str(exc.value)
    assert "Too many requests" in str(exc.value)
    events = [
        json.loads(line)
        for line in session_log.log_path.read_text(encoding="utf-8").splitlines()
    ]
    error_event = next(event for event in events if event["event"] == "error")
    assert error_event["data"]["reason"] == "provider_terminal_failure"
    assert error_event["data"]["category"] == "rate-limit"
    assert error_event["data"]["status_code"] == 429


@pytest.mark.parametrize("conductor_sandbox", ["read-only", "workspace-write", "none", "strict"])
def test_codex_exec_ignores_conductor_sandbox_modes(mocker, conductor_sandbox):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in CODEX_NDJSON.splitlines(keepends=True)]
    )
    _patch_codex_popen(mocker, fake)

    CodexProvider().exec("hi", sandbox=conductor_sandbox)

    assert fake.args is not None
    assert "--sandbox" in fake.args
    assert fake.args[fake.args.index("--sandbox") + 1] == "danger-full-access"


def test_codex_exec_worktree_cwd_allows_git_add_and_commit(mocker, tmp_path: Path):
    _repo, worktree = _repo_with_linked_worktree(tmp_path)
    git_pointer = worktree / ".git"
    assert git_pointer.is_file()
    gitdir = Path(git_pointer.read_text(encoding="utf-8").split(":", 1)[1].strip())
    assert gitdir.is_dir()
    assert "worktrees" in gitdir.parts

    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in CODEX_NDJSON.splitlines(keepends=True)]
    )
    real_popen = subprocess.Popen

    def factory(args, **kwargs):
        if args[0] == "git":
            return real_popen(args, **kwargs)
        fake.args = args
        fake.cwd = kwargs.get("cwd")
        fake.env = kwargs.get("env")
        assert fake.cwd == str(worktree)

        changed = worktree / "codex-change.txt"
        changed.write_text("changed by codex\n", encoding="utf-8")
        _git(worktree, "add", "codex-change.txt")
        _git(worktree, "commit", "-m", "codex worktree change")
        return fake

    mocker.patch("conductor.providers.codex.subprocess.Popen", side_effect=factory)

    CodexProvider().exec("hi", sandbox="workspace-write", cwd=str(worktree))

    assert fake.args is not None
    assert fake.args[fake.args.index("--sandbox") + 1] == "danger-full-access"
    assert fake.cwd == str(worktree)
    head_subject = _git(worktree, "log", "-1", "--pretty=%s").stdout.strip()
    assert head_subject == "codex worktree change"


def test_codex_exec_inherits_auth_and_home_env(monkeypatch, mocker):
    monkeypatch.setenv("GH_TOKEN", "gh-token")
    monkeypatch.setenv("GITHUB_TOKEN", "github-token")
    monkeypatch.setenv("HOME", "/tmp/conductor-home")
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in CODEX_NDJSON.splitlines(keepends=True)]
    )
    _patch_codex_popen(mocker, fake)

    CodexProvider().exec("hi")

    assert fake.env is not None
    assert fake.env["GH_TOKEN"] == "gh-token"
    assert fake.env["GITHUB_TOKEN"] == "github-token"
    assert fake.env["HOME"] == "/tmp/conductor-home"


def test_codex_exec_explicit_timeout_is_honored(mocker):
    """Caller-supplied --timeout still works — sentinel pattern must not
    swallow an explicitly-passed integer."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, '{"type":"session.created","session_id":"sess-slow"}\n')],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05, max_stall_sec=None)
    assert "timed out" in str(exc.value)
    assert fake.terminated is True


def test_codex_call_keeps_constructor_default_timeout(mocker):
    """Single-turn call() retains the constructor-default HTTP-style timeout
    (180s) — the no-timeout change is scoped to exec(), where long agent
    sessions are normal."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    captured = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )
    CodexProvider().call("hi")
    # Default constructor timeout is 180s — see CODEX_REQUEST_TIMEOUT_SEC.
    assert captured.call_args.kwargs["timeout"] == 180.0


def test_codex_timeout_recovers_session_id_from_partial_ndjson(mocker):
    """When codex emits a session.created event but then runs past the wall
    clock, the user must be able to --resume from the partial session_id.
    Pre-fix, the TimeoutExpired path threw away the captured stdout entirely,
    leaving the user with 22 minutes of churn and no recovery handle."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = (
        '{"type":"session.created","session_id":"sess-partial-7"}\n'
        '{"type":"item.started","item":{"type":"agent_message"}}\n'
    )
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in partial.splitlines(keepends=True)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05)
    msg = str(exc.value)
    assert "timed out" in msg
    assert "sess-partial-7" in msg, (
        "Timeout error must surface the partial session_id so --resume "
        f"is actionable. Got: {msg!r}"
    )
    assert "--resume sess-partial-7" in msg


def test_codex_timeout_without_partial_session_id_still_raises_clean(mocker):
    """If codex died before emitting session.created, the timeout error
    should still raise cleanly without crashing on the missing ID."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)
    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05)
    msg = str(exc.value)
    assert "timed out" in msg
    assert "session_id" not in msg  # Nothing to surface


def test_codex_call_timeout_handles_bytes_stdout_from_subprocess(mocker):
    """subprocess.TimeoutExpired.output can be bytes when text=False is used
    elsewhere in the call path. Recovery must decode defensively."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial_bytes = (
        b'{"type":"session.created","session_id":"sess-bytes-9"}\n'
    )
    mocker.patch(
        "conductor.providers.codex.subprocess.run",
        side_effect=subprocess.TimeoutExpired(
            cmd="codex", timeout=5, output=partial_bytes, stderr=b""
        ),
    )
    with pytest.raises(ProviderError) as exc:
        CodexProvider().call("hi")
    assert "sess-bytes-9" in str(exc.value)


def test_codex_exec_stall_watchdog_kills_silent_provider(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-stalled-1"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "stalled" in msg
    assert "sess-stalled-1" in msg
    assert "--resume sess-stalled-1" in msg
    assert fake.terminated is True


def test_codex_exec_streams_output_resets_stall_clock(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-streaming-1"}\n',
        '{"type":"item.started","item":{"type":"agent_message"}}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n',
    ]
    fake = _FakePopen(stdout_schedule=[(0.1, line) for line in lines])
    _patch_codex_popen(mocker, fake)

    response = CodexProvider().exec("hi", max_stall_sec=2, liveness_interval_sec=0)

    assert response.text == "done"
    assert response.session_id == "sess-streaming-1"
    assert fake.terminated is False


def test_codex_exec_returns_after_process_exit_when_stdout_reader_lingers(
    mocker, capsys
):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-exited-reader"}\n',
        '{"type":"item.started","item":{"type":"agent_message"}}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":2}}\n',
    ]
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in lines],
        hang_after_stdout=True,
        exit_after_stdout_schedule=True,
    )
    _patch_codex_popen(mocker, fake)

    started = time.monotonic()
    response = CodexProvider().exec(
        "hi",
        timeout_sec=0.4,
        max_stall_sec=None,
        liveness_interval_sec=0.01,
    )
    elapsed = time.monotonic() - started

    assert response.text == "done"
    assert response.session_id == "sess-exited-reader"
    assert fake.terminated is False
    assert elapsed < 0.8
    assert "[conductor] no output from codex for " not in capsys.readouterr().err


def test_codex_exec_no_stall_watchdog_by_default(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-no-watchdog"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.05)

    assert "timed out" in str(exc.value)
    assert not isinstance(exc.value, ProviderStalledError)
    assert fake.terminated is True


def test_codex_exec_emits_liveness_signal_to_stderr(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-liveness"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec("hi", max_stall_sec=0.25, liveness_interval_sec=0.1)

    assert "[conductor] no output from codex for " in capsys.readouterr().err


def test_codex_exec_stall_watchdog_ignores_wrapper_heartbeats(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.25, liveness_interval_sec=0.1)

    err = capsys.readouterr().err
    msg = str(exc.value)
    assert "[conductor] no output from codex for " in err
    assert "codex CLI stalled" in msg
    assert "timed out" not in msg
    assert fake.terminated is True


def test_codex_exec_stall_watchdog_kills_silent_provider_at_deadline(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    started = time.monotonic()
    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)
    elapsed = time.monotonic() - started

    assert "codex CLI stalled" in str(exc.value)
    assert fake.terminated is True
    assert elapsed < 0.5


def test_codex_exec_reader_death_is_stall_failure(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[],
        stdout_reader_error=RuntimeError("reader crashed"),
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=5, liveness_interval_sec=0)

    assert "stream reader failed" in str(exc.value)
    assert "stdout reader failed" in capsys.readouterr().err
    assert fake.terminated is True


def test_codex_exec_stall_watchdog_kills_after_repeated_heartbeats(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec("hi", max_stall_sec=0.2, liveness_interval_sec=0.03)

    err = capsys.readouterr().err
    assert err.count("[conductor] no output from codex for ") >= 3
    assert fake.terminated is True


def test_codex_exec_wall_timeout_survives_blocked_heartbeat_stderr(
    mocker, monkeypatch
):
    class BlockingStderr:
        def __init__(self) -> None:
            self.write_started = threading.Event()

        def write(self, _text: str) -> int:
            self.write_started.set()
            threading.Event().wait()
            return 0

        def flush(self) -> None:
            return None

    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)
    blocking_stderr = BlockingStderr()
    monkeypatch.setattr("conductor.providers.codex.sys.stderr", blocking_stderr)

    started = time.monotonic()
    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec(
            "hi",
            timeout_sec=0.12,
            max_stall_sec=None,
            liveness_interval_sec=0.01,
        )
    elapsed = time.monotonic() - started

    assert blocking_stderr.write_started.is_set()
    assert "timed out" in str(exc.value)
    assert fake.terminated is True
    assert elapsed < 0.7


def test_codex_exec_heartbeat_reports_tool_calls_from_session_log(
    mocker, capsys, tmp_path, monkeypatch
):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    lines = [
        '{"type":"session.created","session_id":"sess-heartbeat-tools"}\n',
        (
            '{"type":"item.completed","item":{"type":"function_call","name":"Read",'
            '"arguments":{"path":"README.md"}}}\n'
        ),
    ]
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in lines],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    session_log = SessionLog(path=tmp_path / "heartbeat-tools.ndjson")

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec(
            "hi",
            max_stall_sec=0.25,
            liveness_interval_sec=0.1,
            session_log=session_log,
        )

    err = capsys.readouterr().err
    assert "1 tool call" in err


def test_codex_exec_heartbeat_reports_subagent_tokens_from_session_log(
    mocker, capsys, tmp_path, monkeypatch
):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    lines = [
        '{"type":"session.created","session_id":"sess-heartbeat-tokens"}\n',
        (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"working","token_count":1200}}\n'
        ),
    ]
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in lines],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    session_log = SessionLog(path=tmp_path / "heartbeat-tokens.ndjson")

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec(
            "hi",
            max_stall_sec=0.25,
            liveness_interval_sec=0.1,
            session_log=session_log,
        )

    err = capsys.readouterr().err
    assert "1.2k tokens received since last heartbeat" in err


def test_codex_heartbeat_reader_matches_canonical_session_log_shape(tmp_path):
    session_log = SessionLog(path=tmp_path / "heartbeat-canonical.ndjson")
    session_log.emit(
        SESSION_EVENT_TOOL_CALL,
        {"provider": "codex", "item_type": "function_call", "name": "Read"},
    )
    session_log.emit(
        SESSION_EVENT_SUBAGENT_MESSAGE,
        {"provider": "codex", "text": "working"},
    )
    session_log.emit(
        SESSION_EVENT_USAGE,
        {
            "provider": "codex",
            SESSION_DATA_USAGE: {SESSION_USAGE_OUTPUT_TOKENS: 1200},
        },
    )

    template, offset = CodexProvider()._read_session_log_progress(
        session_log=session_log,
        offset=0,
    )

    assert offset == session_log.log_path.stat().st_size
    assert template is not None
    assert "1 tool call" in template
    assert "1 subagent message" in template
    assert "1.2k tokens received since last heartbeat" in template


def test_codex_stream_writer_emits_usage_shape_heartbeat_reader_counts(tmp_path):
    session_log = SessionLog(path=tmp_path / "heartbeat-stream-usage.ndjson")
    provider = CodexProvider()

    provider._emit_stream_event(
        (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"working"}}\n'
        ),
        session_log=session_log,
    )
    provider._emit_stream_event(
        '{"type":"turn.completed","usage":{"input_tokens":5,"output_tokens":1200}}\n',
        session_log=session_log,
    )

    events = [
        json.loads(line)
        for line in session_log.log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["event"] == SESSION_EVENT_SUBAGENT_MESSAGE
    assert events[0]["data"][SESSION_DATA_TOKEN_COUNT] is None
    assert events[1]["event"] == SESSION_EVENT_USAGE
    assert events[1]["data"][SESSION_DATA_USAGE][SESSION_USAGE_OUTPUT_TOKENS] == 1200

    template, _offset = provider._read_session_log_progress(
        session_log=session_log,
        offset=0,
    )

    assert template is not None
    assert "1 subagent message" in template
    assert "1.2k tokens received since last heartbeat" in template
    assert "0 tool calls, 0 tokens" not in template


def test_codex_exec_heartbeat_marks_zero_progress_as_possibly_stalled(
    mocker, capsys, tmp_path, monkeypatch
):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    partial = '{"type":"session.created","session_id":"sess-heartbeat-stalled"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)
    session_log = SessionLog(path=tmp_path / "heartbeat-stalled.ndjson")

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec(
            "hi",
            max_stall_sec=0.25,
            liveness_interval_sec=0.1,
            session_log=session_log,
        )

    err = capsys.readouterr().err
    assert "0 tool calls, 0 tokens — possibly stalled" in err


def test_codex_exec_heartbeat_falls_back_when_session_log_missing(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-heartbeat-fallback"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError):
        CodexProvider().exec("hi", max_stall_sec=0.25, liveness_interval_sec=0.1)

    err = capsys.readouterr().err
    assert "[conductor] no output from codex for " in err
    assert "tool call" not in err
    assert "tokens received" not in err
    assert "possibly stalled" not in err


def test_codex_call_unaffected_by_streaming_changes(mocker):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    popen_mock = mocker.patch("conductor.providers.codex.subprocess.Popen")
    run_mock = mocker.patch(
        "conductor.providers.codex.subprocess.run",
        return_value=_fake_completed(stdout=CODEX_NDJSON),
    )

    response = CodexProvider().call("hi")

    assert response.text == "hello from codex"
    assert run_mock.called
    assert not popen_mock.called


def test_codex_exec_emits_session_id_to_stderr_when_received(mocker, capsys):
    """Once codex emits the `session.created` NDJSON event, conductor must
    surface the session_id on stderr immediately so a wrapping agent can
    correlate logs and (if needed) `--resume` mid-flight. Without this the
    session_id only became visible at completion or on error."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-early-1"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ]
    fake = _FakePopen(stdout_schedule=[(0, line) for line in lines])
    _patch_codex_popen(mocker, fake)

    CodexProvider().exec("hi", liveness_interval_sec=0)

    captured = capsys.readouterr()
    assert "[conductor] codex session_id=sess-early-1" in captured.err, (
        f"Expected session_id stderr line. Got: {captured.err!r}"
    )


def test_codex_exec_emits_session_id_only_once(mocker, capsys):
    """If codex emits multiple session.created events (shouldn't happen,
    but the parser must tolerate it), conductor surfaces the first one
    and stays quiet on duplicates — operator stderr stays clean."""
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-first"}\n',
        '{"type":"session.created","session_id":"sess-second"}\n',
        '{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ]
    fake = _FakePopen(stdout_schedule=[(0, line) for line in lines])
    _patch_codex_popen(mocker, fake)

    CodexProvider().exec("hi", liveness_interval_sec=0)

    err = capsys.readouterr().err
    assert err.count("[conductor] codex session_id=") == 1
    assert "sess-first" in err
    assert "sess-second" not in err


def test_codex_exec_surfaces_auth_prompt_and_records_notice(mocker, capsys):
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in CODEX_NDJSON.splitlines(keepends=True)],
        stderr_schedule=[
            (
                0,
                "Please visit https://chatgpt.com/oauth/device to authenticate\n",
            )
        ],
    )
    _patch_codex_popen(mocker, fake)

    response = CodexProvider().exec("hi", liveness_interval_sec=0)

    assert response.auth_prompts == [
        {
            "provider": "codex",
            "message": "provider is waiting for OAuth completion",
            "source": "stderr",
            "url": "https://chatgpt.com/oauth/device",
        }
    ]
    err = capsys.readouterr().err
    assert "[conductor] auth required for codex" in err
    assert "https://chatgpt.com/oauth/device" in err


def test_codex_exec_reads_output_backstop_when_stream_loses_agent_message(
    mocker, monkeypatch, tmp_path
):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    lines = [
        '{"type":"session.created","session_id":"sess-stream-backstop"}\n',
        '{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}\n',
    ]
    fake = _FakePopen(stdout_schedule=[(0, line) for line in lines])
    _patch_codex_popen_with_output_backstop(
        mocker,
        fake,
        backstop_text="hello from streaming backstop\n",
    )

    response = CodexProvider().exec("hi", liveness_interval_sec=0)

    assert fake.args is not None
    output_path = Path(fake.args[fake.args.index("-o") + 1])
    assert response.text == "hello from streaming backstop"
    assert output_path.exists()
    assert output_path.name.startswith("codex-exec-")
    assert response.raw["output_path"] == str(output_path)


def test_codex_exec_writes_forensic_envelope_on_stall(mocker, tmp_path, monkeypatch):
    """Envelope captures everything needed to attribute a stall:
    command, cwd, conductor version, partial stdout, partial stderr.
    Independent of whether codex emitted any NDJSON."""
    import json as _json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-forensic-1"}\n'
    fake = _FakePopen(
        stdout_schedule=[(0, partial)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "forensic envelope:" in msg
    log_dir = tmp_path / "conductor"
    log_files = list(log_dir.glob("codex-*.json"))
    assert len(log_files) == 1, f"expected one envelope file, got {log_files}"
    envelope = _json.loads(log_files[0].read_text())
    assert envelope["kind"] == "stall"
    # The CLI argv keeps the literal `codex` (not the resolved PATH).
    # The envelope's separate `codex_path` field is what shutil.which()
    # resolved at the time of failure.
    assert envelope["command"][0] == "codex"
    assert envelope["codex_path"] == "/usr/bin/codex"
    assert "exec" in envelope["command"]
    assert envelope["captured_stdout"] == partial
    assert envelope["conductor_version"]  # whatever it is, it's set
    assert str(log_files[0]) in msg


def test_codex_exec_writes_forensic_envelope_on_streaming_timeout(
    mocker, tmp_path, monkeypatch
):
    """Same envelope behavior on the wall-clock timeout path."""
    import json as _json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = (
        '{"type":"session.created","session_id":"sess-forensic-2"}\n'
        '{"type":"item.started","item":{"type":"agent_message"}}\n'
    )
    fake = _FakePopen(
        stdout_schedule=[(0, line) for line in partial.splitlines(keepends=True)],
        hang_after_stdout=True,
    )
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderError) as exc:
        CodexProvider().exec("hi", timeout_sec=0.1, liveness_interval_sec=0)

    msg = str(exc.value)
    assert "forensic envelope:" in msg
    log_files = list((tmp_path / "conductor").glob("codex-*.json"))
    assert len(log_files) == 1
    envelope = _json.loads(log_files[0].read_text())
    assert envelope["kind"] == "timeout"
    assert envelope["captured_stdout"] == partial


def test_codex_exec_writes_envelope_when_codex_emits_zero_bytes(
    mocker, tmp_path, monkeypatch
):
    """The high-leverage case from .cortex/journal/2026-04-26-codex-exec-
    wedge-trace.md: codex wedges *before* session.created fires and
    produces no output at all. Pre-fix, this left the wrapping agent
    with nothing — no session_id, no NDJSON file, nothing to attribute
    the failure to. The envelope must still be written, capturing
    (command, cwd, conductor_version) so an operator can pin the run."""
    import json as _json

    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec(
            "test prompt body",
            max_stall_sec=0.05,
            liveness_interval_sec=0,
        )

    msg = str(exc.value)
    assert "forensic envelope:" in msg
    log_files = list((tmp_path / "conductor").glob("codex-*.json"))
    assert len(log_files) == 1, (
        "Envelope MUST be written even when codex emitted zero bytes — "
        "this is the wedge-before-session.created class of failure that "
        "had no diagnostics pre-fix."
    )
    envelope = _json.loads(log_files[0].read_text())
    assert envelope["kind"] == "stall"
    assert envelope["captured_stdout"] == ""
    # The prompt body now arrives via stdin (`codex exec -`), not argv, so
    # the envelope surfaces it as a separate `prompt` field. An operator
    # can still correlate the wedge with the request — just not by reading
    # `command`. argv contains only the flags and effort overrides.
    assert envelope["prompt"] == "test prompt body"
    assert "-" in envelope["command"]  # stdin sentinel
    assert "test prompt body" not in envelope["command"]


def test_codex_exec_forensic_envelope_disk_failure_does_not_mask_real_error(
    mocker, tmp_path, monkeypatch
):
    """If the cache write itself fails (read-only fs, ENOSPC, etc.), the
    original ProviderStalledError must still propagate cleanly — losing
    the forensic envelope is acceptable, masking the original error is not."""
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch("conductor.providers.codex.shutil.which", return_value="/usr/bin/codex")
    partial = '{"type":"session.created","session_id":"sess-disk-fail"}\n'
    fake = _FakePopen(stdout_schedule=[(0, partial)], hang_after_stdout=True)
    _patch_codex_popen(mocker, fake)

    # Force write_text to raise — simulates a read-only fs / quota failure.
    mocker.patch(
        "conductor.providers.codex.Path.write_text",
        side_effect=OSError("disk full"),
    )

    with pytest.raises(ProviderStalledError) as exc:
        CodexProvider().exec("hi", max_stall_sec=0.05, liveness_interval_sec=0)
    msg = str(exc.value)
    # Original error info still present:
    assert "stalled" in msg
    assert "sess-disk-fail" in msg
    # No fabricated envelope path mentioned:
    assert "forensic envelope:" not in msg


# ---------------------------------------------------------------------------
# Gemini
# ---------------------------------------------------------------------------

GEMINI_JSON = """{
    "response": "hello from gemini",
    "session_id": "xyz",
    "stats": {
        "models": {
            "gemini-2.5-pro": {"tokens": {"input": 12, "candidates": 4}}
        }
    }
}"""

GEMINI_SAVED_JSON = """{
    "response": "My analysis is complete and has been saved. I'm ready for your next instruction.",
    "session_id": "saved-xyz",
    "stats": {
        "tools": {
            "byName": {
                "write_file": {"count": 1, "success": 1}
            }
        }
    }
}"""


def test_gemini_configured_when_cli_present_and_env_authed(mocker, monkeypatch):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    ok, reason = GeminiProvider().configured()
    assert ok is True and reason is None


def test_gemini_configured_when_oauth_creds_have_tokens(
    mocker, monkeypatch, tmp_path
):
    """Filesystem-only auth path — env vars unset, OAuth file present."""
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    creds = tmp_path / "oauth_creds.json"
    creds.write_text(
        '{"access_token": "ya29.fake", "refresh_token": "1//fake", '
        '"expiry_date": 0}'
    )
    # Expiry deliberately stale — we trust the CLI to refresh, so an
    # expired access_token alongside a refresh_token is still authed.
    ok, reason = GeminiProvider(oauth_creds_path=creds).configured()
    assert ok is True and reason is None


def test_gemini_configured_false_when_cli_missing(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value=None)
    ok, reason = GeminiProvider().configured()
    assert ok is False
    assert "not found on PATH" in reason
    assert "GEMINI_API_KEY" in reason


def test_gemini_configured_false_when_no_env_and_no_oauth_file(
    mocker, monkeypatch, tmp_path
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    missing = tmp_path / "does_not_exist.json"
    ok, reason = GeminiProvider(oauth_creds_path=missing).configured()
    assert ok is False
    assert "not authenticated" in reason
    assert "GEMINI_API_KEY" in reason
    assert "GOOGLE_API_KEY" in reason


def test_gemini_configured_false_when_oauth_file_corrupt(
    mocker, monkeypatch, tmp_path
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    creds = tmp_path / "oauth_creds.json"
    creds.write_text("not valid json {{")
    ok, reason = GeminiProvider(oauth_creds_path=creds).configured()
    assert ok is False
    assert "could not parse" in reason


def test_gemini_configured_false_when_oauth_file_has_no_tokens(
    mocker, monkeypatch, tmp_path
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    creds = tmp_path / "oauth_creds.json"
    creds.write_text('{"unrelated_field": "value"}')
    ok, reason = GeminiProvider(oauth_creds_path=creds).configured()
    assert ok is False
    assert "no usable tokens" in reason


def test_gemini_configured_when_only_google_application_credentials_set(
    mocker, monkeypatch, tmp_path
):
    """Vertex AI ADC path — GOOGLE_APPLICATION_CREDENTIALS is sufficient."""
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/tmp/sa.json")
    missing = tmp_path / "does_not_exist.json"  # unreachable; env wins
    ok, reason = GeminiProvider(oauth_creds_path=missing).configured()
    assert ok is True and reason is None


def test_gemini_review_configured_requires_code_review_extension(mocker, monkeypatch):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout="[]"),
    )

    ok, reason = GeminiProvider().review_configured()

    assert ok is False
    assert "Code Review extension" in reason


def test_gemini_call_parses_json_and_usage(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_JSON),
    )

    response = GeminiProvider().call("hi")

    assert response.text == "hello from gemini"
    assert response.provider == "gemini"
    assert response.usage["input_tokens"] == 12
    assert response.usage["output_tokens"] == 4
    assert response.session_id == "xyz"


def test_gemini_call_appends_inline_response_contract(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    run_mock = mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_JSON),
    )

    GeminiProvider().call("hi")

    args = run_mock.call_args.args[0]
    prompt = args[args.index("-p") + 1]
    assert "Conductor call output contract" in prompt
    assert "Return the complete answer directly" in prompt
    assert "Do not save the answer to disk" in prompt
    assert "write_file" in prompt


def test_gemini_call_rejects_saved_write_file_placeholder(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_SAVED_JSON),
    )

    with pytest.raises(ProviderHTTPError) as exc:
        GeminiProvider().call("answer inline")

    assert "file-writing tool" in str(exc.value)
    assert "inline output" in str(exc.value)


def test_gemini_exec_allows_saved_write_file_response(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    fake = _FakePopen(stdout_schedule=[(0, GEMINI_SAVED_JSON)])

    def factory(args, **kwargs):
        fake.args = args
        return fake

    mocker.patch("conductor.providers.gemini.subprocess.Popen", side_effect=factory)

    response = GeminiProvider().exec("write a file", sandbox="workspace-write")

    assert "has been saved" in response.text


def test_gemini_review_uses_code_review_extension_command(mocker, monkeypatch):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout='[{"name":"code-review"}]'),
    )
    fake = _FakePopen(stdout_schedule=[(0, GEMINI_JSON)])

    def factory(args, **kwargs):
        fake.args = args
        return fake

    mocker.patch(
        "conductor.providers.gemini.subprocess.Popen",
        side_effect=factory,
    )

    response = GeminiProvider().review("Use the reviewer guide.", base="origin/main")

    assert fake.args is not None
    assert fake.args[0:2] == ["gemini", "-p"]
    prompt = fake.args[2]
    assert prompt.startswith("/code-review")
    assert "Review changes against base branch: origin/main" in prompt
    assert "Use the reviewer guide." in prompt
    assert "--approval-mode" in fake.args
    assert fake.args[fake.args.index("--approval-mode") + 1] == "plan"
    assert response.provider == "gemini"
    assert response.text == "hello from gemini"


def test_gemini_review_repairs_missing_requested_sentinel(
    mocker,
    monkeypatch,
    capsys,
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout='[{"name":"code-review"}]'),
    )
    fake = _FakePopen(
        stdout_schedule=[
            (0, json.dumps({"response": "Plain review without the marker."}))
        ]
    )
    mocker.patch(
        "conductor.providers.gemini.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    response = GeminiProvider().review("End with CODEX_REVIEW_CLEAN or BLOCKED.")

    assert response.text == "Plain review without the marker.\nCODEX_REVIEW_BLOCKED"
    assert "[conductor] gemini review repaired missing Touchstone sentinel" in (
        capsys.readouterr().err
    )


def test_gemini_review_extracts_inner_json_response_and_preserves_sentinel(
    mocker,
    monkeypatch,
    capsys,
):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    _strip_gemini_auth_env(monkeypatch)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout='[{"name":"code-review"}]'),
    )
    outer = {
        "response": json.dumps(
            {
                "response": (
                    "The submitted code follows the requested contract.\n"
                    "CODEX_REVIEW_CLEAN"
                )
            }
        ),
        "stats": {},
    }
    fake = _FakePopen(stdout_schedule=[(0, json.dumps(outer))])
    mocker.patch(
        "conductor.providers.gemini.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    response = GeminiProvider().review(
        "End with CODEX_REVIEW_CLEAN or CODEX_REVIEW_BLOCKED."
    )

    assert response.text == (
        "The submitted code follows the requested contract.\nCODEX_REVIEW_CLEAN"
    )
    assert response.text.lstrip()[0] != "{"
    assert "[conductor] gemini review repaired JSON response envelope" in (
        capsys.readouterr().err
    )


def test_gemini_call_with_resume_passes_resume_flag(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    captured = mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_JSON),
    )
    GeminiProvider().call("follow-up", resume_session_id="latest")
    args = captured.call_args.args[0]
    assert "--resume" in args
    assert args[args.index("--resume") + 1] == "latest"


def test_gemini_call_falls_back_to_plain_text(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout="plain text reply"),
    )

    response = GeminiProvider().call("hi")

    assert response.text == "plain text reply"
    assert response.usage["input_tokens"] is None


def test_gemini_call_passes_model_flag(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    run_mock = mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=GEMINI_JSON),
    )
    GeminiProvider().call("hi", model="gemini-2.5-flash")
    args = run_mock.call_args.args[0]
    assert "-m" in args and args[args.index("-m") + 1] == "gemini-2.5-flash"


def test_gemini_call_raises_on_empty_stdout(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    mocker.patch(
        "conductor.providers.gemini.subprocess.run",
        return_value=_fake_completed(stdout=""),
    )
    with pytest.raises(ProviderHTTPError):
        GeminiProvider().call("hi")


def test_gemini_exec_surfaces_auth_prompt_and_records_notice(mocker, capsys):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    fake = _FakePopen(
        stdout_schedule=[(0, GEMINI_JSON)],
        stderr_schedule=[
            (
                0,
                "Please visit https://accounts.google.com/o/oauth2/auth to authenticate\n",
            )
        ],
    )
    mocker.patch(
        "conductor.providers.gemini.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    response = GeminiProvider().exec("hi")

    assert response.auth_prompts == [
        {
            "provider": "gemini",
            "message": "provider is waiting for OAuth completion",
            "source": "stderr",
            "url": "https://accounts.google.com/o/oauth2/auth",
        }
    ]
    err = capsys.readouterr().err
    assert "[conductor] auth required for gemini" in err
    assert "https://accounts.google.com/o/oauth2/auth" in err


def test_gemini_exec_stall_watchdog_kills_silent_provider(mocker):
    mocker.patch("conductor.providers.gemini.shutil.which", return_value="/usr/bin/gemini")
    fake = _FakePopen(stdout_schedule=[], hang_after_stdout=True)
    mocker.patch(
        "conductor.providers.gemini.subprocess.Popen",
        side_effect=lambda args, **kwargs: fake,
    )

    with pytest.raises(ProviderStalledError) as exc:
        GeminiProvider().exec("hi", max_stall_sec=0.05)

    assert fake.terminated is True
    assert "gemini CLI stalled" in str(exc.value)


# ---------------------------------------------------------------------------
# Live subprocess smoke
# ---------------------------------------------------------------------------

LIVE_SMOKE_DISABLED = not os.environ.get("RUN_LIVE_SMOKE")
LIVE_SMOKE_REASON = "set RUN_LIVE_SMOKE=1 to run live subprocess smoke tests"
LIVE_ONE_TOKEN_PROMPT = "Reply with exactly: OK"


def _assert_live_response_shape(response: CallResponse, *, provider: str) -> None:
    assert isinstance(response, CallResponse)
    assert response.provider == provider
    assert response.model
    assert response.text.strip()
    assert isinstance(response.duration_ms, int)
    assert response.duration_ms >= 0
    assert isinstance(response.usage, dict)


@pytest.mark.skipif(LIVE_SMOKE_DISABLED, reason=LIVE_SMOKE_REASON)
@pytest.mark.skipif(shutil.which("claude") is None, reason="claude CLI not installed")
def test_live_claude_call_returns_call_response_shape():
    response = ClaudeProvider().call(LIVE_ONE_TOKEN_PROMPT, effort="minimal")
    _assert_live_response_shape(response, provider="claude")


@pytest.mark.skipif(LIVE_SMOKE_DISABLED, reason=LIVE_SMOKE_REASON)
@pytest.mark.skipif(shutil.which("codex") is None, reason="codex CLI not installed")
def test_live_codex_call_returns_call_response_shape():
    response = CodexProvider().call(LIVE_ONE_TOKEN_PROMPT, effort="minimal")
    _assert_live_response_shape(response, provider="codex")


@pytest.mark.skipif(LIVE_SMOKE_DISABLED, reason=LIVE_SMOKE_REASON)
@pytest.mark.skipif(shutil.which("gemini") is None, reason="gemini CLI not installed")
def test_live_gemini_call_returns_call_response_shape():
    response = GeminiProvider().call(LIVE_ONE_TOKEN_PROMPT, effort="minimal")
    _assert_live_response_shape(response, provider="gemini")
