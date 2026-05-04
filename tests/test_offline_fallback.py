"""Tests for the offline-mode fallback UX.

Covers three surfaces:
  1. ``_is_retryable`` classification of network-level errors as the new
     ``"network"`` category.
  2. The ``offline_mode`` sticky-flag module: set/clear/expiry/TTL override
     + graceful-degradation when the cache dir is read-only.
  3. CLI end-to-end behavior — ``--auto`` with a network-down kimi:
       a) prompts on TTY, switches to ollama on "y"
       b) re-raises on "n"
       c) non-TTY re-raises with a hint
       d) sticky flag reorders ollama to the front without prompting
       e) ``--offline`` forces local (and sets the sticky flag)
       f) ``--no-offline`` clears the sticky flag
  4. Explicit-mode (--with kimi) network error shows the hint pointing
     at --offline / --with ollama.
"""

from __future__ import annotations

import httpx
import pytest
import respx
from click.testing import CliRunner

import conductor.providers.openrouter_catalog as openrouter_catalog
from conductor import offline_mode
from conductor.cli import _is_retryable, main
from conductor.providers.interface import ProviderHTTPError
from conductor.providers.kimi import KIMI_DEFAULT_MODEL
from conductor.providers.ollama import (
    OLLAMA_BASE_URL_ENV,
    OLLAMA_DEFAULT_BASE_URL,
    OLLAMA_DEFAULT_MODEL,
)
from conductor.providers.openrouter import OPENROUTER_API_KEY_ENV

_OPENROUTER_CHAT_URL = "https://openrouter.ai/api/v1/chat/completions"
_OLLAMA_CHAT_URL = f"{OLLAMA_DEFAULT_BASE_URL}/api/chat"
_OLLAMA_TAGS_URL = f"{OLLAMA_DEFAULT_BASE_URL}/api/tags"


@pytest.fixture(autouse=True)
def _isolate_offline_cache(tmp_path, monkeypatch):
    """Every test gets a fresh, writable cache dir.

    Honors offline_mode's $XDG_CACHE_HOME lookup so reads and writes in
    the test suite never touch the real ~/.cache/conductor.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    monkeypatch.delenv(OLLAMA_BASE_URL_ENV, raising=False)
    yield


@pytest.fixture
def _kimi_configured(monkeypatch):
    """Populate the env so KimiProvider.configured() returns True."""
    monkeypatch.setenv(OPENROUTER_API_KEY_ENV, "or-test-key")
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


def _stub_other_providers_unconfigured(mocker, *, include_ollama: bool = False):
    """Only kimi (+ optionally ollama) compete in auto-mode routing.

    Default stubs claude, codex, gemini so the ranking is deterministic
    and ollama stays "reachable" for the prompt/fallback to work.
    Pass ``include_ollama=True`` to also stub ollama — useful for tests
    that need kimi to win routing unconditionally (e.g. --no-offline).
    """
    from conductor.providers import ClaudeProvider, CodexProvider, GeminiProvider

    classes: list[type] = [ClaudeProvider, CodexProvider, GeminiProvider]
    if include_ollama:
        from conductor.providers import OllamaProvider

        classes.append(OllamaProvider)
    for cls in classes:
        mocker.patch.object(
            cls, "configured", lambda self: (False, "stubbed off for test")
        )


def _force_tty(mocker):
    """Convince the prompt path that we're interactive.

    CliRunner wires in-memory streams, so ``sys.stdin.isatty()`` is False
    by default. The offline prompt gates on ``_stderr_is_tty``; mocking
    it is the narrowest seam.
    """
    mocker.patch("conductor.cli._stderr_is_tty", return_value=True)


def _ollama_ok_response(text: str) -> dict:
    return {
        "model": OLLAMA_DEFAULT_MODEL,
        "message": {"role": "assistant", "content": text},
        "done": True,
        "total_duration": 1_000_000,
        "prompt_eval_count": 5,
        "eval_count": 10,
    }


def _ollama_tags_response() -> dict:
    return {"models": [{"name": OLLAMA_DEFAULT_MODEL}]}


# --------------------------------------------------------------------------- #
# 1. _is_retryable classification
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "msg",
    [
        "network error calling OpenRouter: ConnectError: Connection refused",
        "httpx.ConnectError: [Errno 61] Connection refused",
        "[Errno 8] nodename nor servname provided, or not known",
        "[Errno 51] Network is unreachable",
        "[Errno 50] Network is down",  # macOS airplane-mode wording
        "Name or service not known",   # Linux glibc resolver
        "Temporary failure in name resolution",
        "Could not resolve host",
        "No route to host",
        "getaddrinfo failed",
    ],
)
def test_is_retryable_classifies_network_errors(msg):
    retryable, category = _is_retryable(ProviderHTTPError(msg))
    assert retryable is True
    assert category == "network", f"expected 'network' for {msg!r}, got {category!r}"


def test_is_retryable_still_handles_known_categories():
    # Defense in depth — make sure the network additions didn't steal
    # cases that belong to other categories.
    assert _is_retryable(ProviderHTTPError("HTTP 503 Service Unavailable")) == (
        True,
        "5xx",
    )
    assert _is_retryable(ProviderHTTPError("request timed out after 30s")) == (
        True,
        "timeout",
    )
    assert _is_retryable(ProviderHTTPError("rate limit: 429")) == (
        True,
        "rate-limit",
    )
    assert _is_retryable(ProviderHTTPError("quota exceeded: out of tokens")) == (
        True,
        "rate-limit",
    )
    assert _is_retryable(ProviderHTTPError("provider API is down")) == (
        True,
        "5xx",
    )
    assert _is_retryable(ValueError("some other failure")) == (False, "other")


# --------------------------------------------------------------------------- #
# 2. offline_mode sticky flag
# --------------------------------------------------------------------------- #


def test_offline_flag_is_inactive_by_default():
    assert offline_mode.is_active() is False
    assert offline_mode.seconds_remaining() == 0
    assert offline_mode.expiry_timestamp() is None


def test_offline_flag_set_and_clear():
    assert offline_mode.set_active(ttl_sec=60) is True
    assert offline_mode.is_active() is True
    assert 0 < offline_mode.seconds_remaining() <= 60
    offline_mode.clear()
    assert offline_mode.is_active() is False


def test_offline_flag_expires():
    # Negative TTL means "already expired" — is_active is the invariant
    # we care about, not whether the file exists.
    offline_mode.set_active(ttl_sec=-1)
    assert offline_mode.is_active() is False


def test_offline_flag_ttl_override(monkeypatch):
    monkeypatch.setenv(offline_mode.CONDUCTOR_OFFLINE_TTL_ENV, "30")
    offline_mode.set_active()
    assert 0 < offline_mode.seconds_remaining() <= 30


def test_offline_flag_degrades_on_unwritable_cache(monkeypatch, tmp_path):
    # Point XDG_CACHE_HOME at a path we can't create a subdirectory in.
    # On Unix, chmod 0400 the parent so mkdir fails.
    parent = tmp_path / "locked"
    parent.mkdir()
    parent.chmod(0o500)  # read-execute only; can't create new children
    monkeypatch.setenv("XDG_CACHE_HOME", str(parent))

    try:
        # set_active should return False rather than raising.
        assert offline_mode.set_active() is False
        assert offline_mode.is_active() is False
    finally:
        parent.chmod(0o700)  # restore so tmp_path cleanup works


def test_offline_flag_survives_corrupt_file(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    # Write garbage where a unix timestamp should be.
    cache_dir = tmp_path / "conductor"
    cache_dir.mkdir()
    (cache_dir / "offline_until").write_text("not-a-number\n")

    assert offline_mode.is_active() is False
    assert offline_mode.expiry_timestamp() is None
    # A subsequent set() should still succeed (atomic overwrite).
    assert offline_mode.set_active(ttl_sec=60) is True
    assert offline_mode.is_active() is True


# --------------------------------------------------------------------------- #
# 3. CLI end-to-end: --auto with a network-down kimi
# --------------------------------------------------------------------------- #


def _invoke_auto_with_kimi_down(cli_input: str | None = None):
    """Run `conductor call --auto` with kimi failing on network + ollama up.

    ``assert_all_called=False`` because not every test actually reaches
    ollama (the decline / non-TTY branches never hit ``/api/chat``); we
    care about the CLI outcome, not respx bookkeeping.
    """
    with respx.mock(assert_all_called=False) as router:
        router.post(_OPENROUTER_CHAT_URL).mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        router.get(_OLLAMA_TAGS_URL).mock(
            return_value=httpx.Response(200, json=_ollama_tags_response())
        )
        router.post(_OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(200, json=_ollama_ok_response("via ollama"))
        )
        result = CliRunner().invoke(
            main,
            ["call", "--auto", "--task", "hi"],
            input=cli_input,
        )
    return result


def test_auto_network_failure_prompts_and_switches_on_yes(
    mocker, _kimi_configured
):
    _stub_other_providers_unconfigured(mocker)
    _force_tty(mocker)

    result = _invoke_auto_with_kimi_down(cli_input="y\n")

    assert result.exit_code == 0, result.output
    assert "via ollama" in result.output
    # The prompt itself went to stderr; CliRunner merges by default, so
    # we check for both the prompt copy and the ollama response.
    assert "offline" in result.output.lower()
    # Sticky flag set for subsequent calls.
    assert offline_mode.is_active() is True


def test_auto_network_failure_user_declines_reraises(mocker, _kimi_configured):
    _stub_other_providers_unconfigured(mocker)
    _force_tty(mocker)

    result = _invoke_auto_with_kimi_down(cli_input="n\n")

    assert result.exit_code != 0, result.output
    # The kimi error message should still surface to the user.
    assert "connection refused" in result.output.lower() or "kimi" in result.output.lower()
    # No sticky flag: user declined the switch.
    assert offline_mode.is_active() is False


def test_auto_network_failure_non_tty_reraises_with_hint(
    mocker, _kimi_configured
):
    _stub_other_providers_unconfigured(mocker)
    # No _force_tty — the default is False under CliRunner, which is what
    # we want to exercise. Explicit to document intent.
    mocker.patch("conductor.cli._stderr_is_tty", return_value=False)

    result = _invoke_auto_with_kimi_down(cli_input=None)

    assert result.exit_code != 0
    # The hint should point at the offline-mode levers.
    assert "--with ollama" in result.output or "--offline" in result.output
    assert offline_mode.is_active() is False


def test_sticky_flag_reorders_ollama_first(mocker, _kimi_configured):
    """Once the flag is set, subsequent --auto calls skip straight to ollama.

    We deliberately don't register a kimi route — respx raises on unmatched
    requests, so if the sticky flag failed to reorder, the test fails loudly
    with a "no route" error rather than a misleading silent pass.
    """
    _stub_other_providers_unconfigured(mocker)
    offline_mode.set_active(ttl_sec=600)

    with respx.mock() as router:
        router.get(_OLLAMA_TAGS_URL).mock(
            return_value=httpx.Response(200, json=_ollama_tags_response())
        )
        router.post(_OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(
                200, json=_ollama_ok_response("sticky ollama")
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--auto", "--task", "hi"]
        )

    assert result.exit_code == 0, result.output
    assert "sticky ollama" in result.output
    assert "offline mode active" in result.output.lower()


# --------------------------------------------------------------------------- #
# 4. --offline / --no-offline CLI flags
# --------------------------------------------------------------------------- #


def test_offline_flag_forces_ollama_without_auto(mocker):
    """`conductor call --offline` routes to ollama without needing --auto.

    The `--offline` path rewrites to `--with ollama` (no router), so
    ollama.configured() is never called and only `/api/chat` gets hit.
    """
    _stub_other_providers_unconfigured(mocker)

    with respx.mock() as router:
        router.post(_OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(
                200, json=_ollama_ok_response("forced ollama")
            )
        )
        result = CliRunner().invoke(
            main, ["call", "--offline", "--task", "hi"]
        )

    assert result.exit_code == 0, result.output
    assert "forced ollama" in result.output
    # --offline sets the sticky flag so follow-up calls stay local.
    assert offline_mode.is_active() is True


def test_offline_flag_conflicts_with_non_ollama_with(mocker):
    _stub_other_providers_unconfigured(mocker)

    result = CliRunner().invoke(
        main, ["call", "--offline", "--with", "kimi", "--task", "hi"]
    )

    assert result.exit_code != 0
    assert "contradicts" in result.output.lower() or "contradict" in result.output.lower()


def test_offline_flag_accepts_with_ollama(mocker):
    """`--offline --with ollama` is redundant but allowed."""
    _stub_other_providers_unconfigured(mocker)

    with respx.mock() as router:
        router.post(_OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(
                200, json=_ollama_ok_response("both redundant")
            )
        )
        result = CliRunner().invoke(
            main,
            ["call", "--offline", "--with", "ollama", "--task", "hi"],
        )

    assert result.exit_code == 0, result.output
    assert "both redundant" in result.output


def test_offline_flag_forces_ollama_even_with_auto(mocker, _kimi_configured):
    """`--offline --auto` must route to ollama, not silently fall through.

    This is the codex-review regression: previously ``--offline --auto`` let
    the auto router pick a remote provider (kimi wins on tag overlap), then
    relied on ``_invoke_with_fallback`` to reorder ollama in front. If ollama
    wasn't in the ranking at all (e.g. not configured), the CLI would quietly
    route to the remote despite the explicit force-local request. Fix: when
    ``--offline`` is passed, rewrite the invocation to ``--with ollama``
    unconditionally.
    """
    _stub_other_providers_unconfigured(mocker)

    with respx.mock() as router:
        # `--offline` becomes `--with ollama` (no router, no --auto), so
        # only /api/chat is hit.
        router.post(_OLLAMA_CHAT_URL).mock(
            return_value=httpx.Response(
                200, json=_ollama_ok_response("via local despite auto")
            )
        )
        result = CliRunner().invoke(
            main,
            [
                "call",
                "--offline",
                "--auto",
                "--tags",
                "long-context",
                "--task",
                "hi",
            ],
        )

    assert result.exit_code == 0, result.output
    assert "via local despite auto" in result.output
    assert offline_mode.is_active() is True


def test_sticky_flag_raises_when_ollama_missing_from_ranking(
    mocker, _kimi_configured
):
    """Sticky flag promise: local routing. Without ollama, fail loudly.

    Codex-review regression: if the sticky flag was active but ollama
    wasn't in ``decision.ranked`` (e.g. because ollama is excluded or
    unconfigured), ``_invoke_with_fallback`` would skip the reorder
    and silently try remote providers — which, offline, all fail with
    network errors, producing a misleading "kimi is unreachable" when
    the real issue is "local fallback isn't available."
    """
    offline_mode.set_active(ttl_sec=600)
    # Stub ollama off so it drops out of decision.ranked.
    _stub_other_providers_unconfigured(mocker, include_ollama=True)

    result = CliRunner().invoke(
        main, ["call", "--auto", "--task", "hi"]
    )

    assert result.exit_code != 0
    output = result.output.lower()
    assert "offline mode" in output
    assert "ollama" in output
    # The error should name actionable fixes — not silently cascade.
    assert "--no-offline" in result.output or "ollama serve" in result.output


def test_no_offline_flag_clears_sticky_flag(mocker, _kimi_configured):
    offline_mode.set_active(ttl_sec=600)
    assert offline_mode.is_active() is True
    # Also stub ollama off: the semantic scenario is "I'm back online and
    # want the normal remote-first ranking." Leaving ollama reachable
    # would tempt the router to compete it with kimi on tag-overlap.
    _stub_other_providers_unconfigured(mocker, include_ollama=True)

    with respx.mock() as router:
        router.post(_OPENROUTER_CHAT_URL).mock(
            return_value=httpx.Response(
                200,
                json={
                    "model": KIMI_DEFAULT_MODEL,
                    "choices": [{"message": {"content": "via kimi again"}}],
                    "usage": {},
                },
            )
        )
        result = CliRunner().invoke(
            main,
            ["call", "--no-offline", "--auto", "--task", "hi"],
        )

    assert result.exit_code == 0, result.output
    assert "via kimi again" in result.output
    assert offline_mode.is_active() is False


# --------------------------------------------------------------------------- #
# 5. Explicit-mode hint on network failure
# --------------------------------------------------------------------------- #


def test_explicit_with_kimi_network_error_shows_hint(mocker, _kimi_configured):
    _stub_other_providers_unconfigured(mocker)

    with respx.mock() as router:
        router.post(_OPENROUTER_CHAT_URL).mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "kimi", "--task", "hi"]
        )

    assert result.exit_code != 0
    # Both the original error and the hint should be visible.
    output = result.output.lower()
    assert "kimi" in output and "unreachable" in output
    assert "--offline" in result.output or "--with ollama" in result.output


def test_explicit_with_ollama_network_error_does_not_hint(mocker):
    """No point suggesting `--with ollama` if that's already what failed."""
    _stub_other_providers_unconfigured(mocker)

    with respx.mock() as router:
        router.post(_OLLAMA_CHAT_URL).mock(
            side_effect=httpx.ConnectError("Connection refused")
        )
        result = CliRunner().invoke(
            main, ["call", "--with", "ollama", "--task", "hi"]
        )

    assert result.exit_code != 0
    # The hint specifically names an alternative; it should NOT appear
    # when the alternative is the thing that failed.
    assert "--with ollama" not in result.output
