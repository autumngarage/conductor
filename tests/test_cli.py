"""CLI smoke tests — provider lookup, stdin fallback, error paths."""

from __future__ import annotations

import httpx
import respx
from click.testing import CliRunner

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor import agent_wiring as aw
from conductor.cli import main
from conductor.providers.kimi import KIMI_DEFAULT_MODEL
from conductor.providers.openrouter import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_DEFAULT_MODEL,
)

_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"


def _stub_kimi_catalog(monkeypatch):
    monkeypatch.setattr(
        openrouter_catalog,
        "load_catalog",
        lambda: [
            openrouter_catalog.ModelEntry(
                id=KIMI_DEFAULT_MODEL,
                name=KIMI_DEFAULT_MODEL,
                created=1_700_000_000,
                context_length=256_000,
                pricing_prompt=0.001,
                pricing_completion=0.002,
                pricing_thinking=None,
                supports_thinking=False,
                supports_tools=False,
                supports_vision=False,
            )
        ],
    )


def test_call_unknown_provider_shows_usage_error():
    result = CliRunner().invoke(main, ["call", "--with", "noprovider", "--task", "hi"])
    assert result.exit_code != 0
    assert "unknown provider" in result.output.lower() or "noprovider" in result.output


def test_cli_warns_once_for_stale_agent_wiring(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.chdir(repo)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    aw.wire_agents_md(version="0.1.0")

    first = CliRunner().invoke(main, ["list"])
    second = CliRunner().invoke(main, ["list"])

    assert first.exit_code == 0, first.output
    assert "agent instructions are out of date" in first.stderr
    assert "conductor init --yes" in first.stderr
    assert second.exit_code == 0, second.output
    assert "agent instructions are out of date" not in second.stderr


def test_call_missing_task_and_no_stdin_errors():
    # CliRunner attaches an empty pipe as stdin (isatty=False), so we hit the
    # empty-brief branch rather than the no-brief-no-stdin branch. Both signal
    # the same user error: nothing to send.
    result = CliRunner().invoke(main, ["call", "--with", "kimi"])
    assert result.exit_code != 0
    assert "brief" in result.output.lower() and "empty" in result.output.lower()


def test_call_task_file_reads_file(monkeypatch, tmp_path):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    _stub_kimi_catalog(monkeypatch)
    brief = tmp_path / "brief.md"
    brief.write_text("brief from file\n", encoding="utf-8")

    with respx.mock() as router:
        route = router.post(_OPENROUTER_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello back"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task-file", str(brief)]
        )

    assert result.exit_code == 0, result.output
    assert "hello back" in result.output
    assert route.calls.last.request.content.decode("utf-8").count("brief from file") == 1


def test_call_brief_file_reads_file(monkeypatch, tmp_path):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    _stub_kimi_catalog(monkeypatch)
    brief = tmp_path / "brief.md"
    brief.write_text("delegation brief from file\n", encoding="utf-8")

    with respx.mock() as router:
        route = router.post(_OPENROUTER_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello back"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--brief-file", str(brief)]
        )

    assert result.exit_code == 0, result.output
    assert "hello back" in result.output
    assert (
        route.calls.last.request.content.decode("utf-8").count(
            "delegation brief from file"
        )
        == 1
    )


def test_call_kimi_happy_path(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    _stub_kimi_catalog(monkeypatch)
    with respx.mock() as router:
        router.post(_OPENROUTER_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello back"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(main, ["call", "--with", "kimi", "--task", "hi"])

    assert result.exit_code == 0, result.output
    assert "hello back" in result.output


def test_call_openrouter_happy_path(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    with respx.mock(base_url="https://openrouter.ai/api/v1") as router:
        router.post("/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": OPENROUTER_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello from openrouter"}}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 2},
                },
            )
        )
        result = CliRunner().invoke(
            main,
            [
                "call",
                "--with",
                "openrouter",
                "--model",
                OPENROUTER_DEFAULT_MODEL,
                "--task",
                "hi",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "hello from openrouter" in result.output


def test_call_kimi_missing_openrouter_key_exits_2(monkeypatch, mocker):
    monkeypatch.delenv(OPENROUTER_API_KEY_ENV, raising=False)
    mocker.patch("conductor.providers.openrouter.credentials.get", return_value=None)
    result = CliRunner().invoke(main, ["call", "--with", "kimi", "--task", "hi"])
    assert result.exit_code == 2
    assert OPENROUTER_API_KEY_ENV in result.output


def test_call_json_output(monkeypatch):
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    _stub_kimi_catalog(monkeypatch)
    # The caller banner is silenced under --json (matches existing route-log
    # silencing), so stdout stays clean for json.loads().
    with respx.mock() as router:
        router.post(_OPENROUTER_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "ok"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi", "--json"]
        )

    assert result.exit_code == 0, result.stderr
    import json

    payload = json.loads(result.stdout)
    assert payload["text"] == "ok"
    assert payload["provider"] == "kimi"


def test_call_emits_caller_banner_when_claude_detected(monkeypatch):
    """When CLAUDECODE is set, `conductor call` announces itself on stderr."""
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("NO_COLOR", "1")  # plain ASCII for stable assertion
    _stub_kimi_catalog(monkeypatch)

    with respx.mock() as router:
        router.post(_OPENROUTER_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi"]
        )

    assert result.exit_code == 0, result.stderr
    # Banner on stderr — keeps stdout clean for subprocess parsing.
    assert "Claude Code is using Conductor → kimi" in result.stderr
    # Response on stdout, untouched.
    assert result.stdout.strip() == "hello"


def test_call_silent_route_suppresses_caller_banner(monkeypatch):
    """--silent-route silences the caller banner (and the route log)."""
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
    monkeypatch.setenv("CLAUDECODE", "1")
    _stub_kimi_catalog(monkeypatch)

    with respx.mock() as router:
        router.post(_OPENROUTER_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "hello"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi", "--silent-route"]
        )

    assert result.exit_code == 0, result.stderr
    assert "Conductor" not in result.stderr


def test_router_defaults_set_list_unset(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    runner = CliRunner()
    set_result = runner.invoke(main, ["router", "defaults", "set", "code-review", "codex"])
    assert set_result.exit_code == 0, set_result.output

    list_result = runner.invoke(main, ["router", "defaults", "list"])
    assert list_result.exit_code == 0, list_result.output
    assert "code-review = codex (home)" in list_result.output

    unset_result = runner.invoke(main, ["router", "defaults", "unset", "code-review"])
    assert unset_result.exit_code == 0, unset_result.output

    empty_result = runner.invoke(main, ["router", "defaults", "list"])
    assert empty_result.exit_code == 0, empty_result.output
    assert "(no router defaults)" in empty_result.output


def test_router_defaults_list_marks_repo_override(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    config_dir = home / ".config" / "conductor"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "router.toml").write_text(
        '[tag_defaults]\ncode-review = "claude"\nlong-context = "gemini"\n',
        encoding="utf-8",
    )
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    (repo_dir / ".conductor").mkdir()
    (repo_dir / ".conductor" / "router.toml").write_text(
        '[tag_defaults]\ncode-review = "codex"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repo_dir)

    result = CliRunner().invoke(main, ["router", "defaults", "list"], catch_exceptions=False)

    assert result.exit_code == 0, result.output
    assert "code-review = codex (repo override)" in result.output
    assert "long-context = gemini (home)" in result.output
