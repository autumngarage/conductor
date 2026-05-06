from __future__ import annotations

import json
from typing import TYPE_CHECKING

import httpx
from click.testing import CliRunner

from conductor.cli import _network_target_for_provider, main
from conductor.network_profile import (
    NETWORK_PROFILE_FALLBACK_TARGET,
    NETWORK_PROFILE_TTL_SEC,
    NetworkProfile,
    apply_scaling,
    get_network_profile,
    scaling_multiplier,
)
from conductor.providers import (
    CallResponse,
    CodexProvider,
    ShellProvider,
    ShellProviderSpec,
    get_provider,
)

if TYPE_CHECKING:
    from pathlib import Path


def _cache_file(tmp_path: Path) -> Path:
    return tmp_path / "conductor" / "network_profile"


class _FakeClient:
    calls: list[str] = []
    failures: dict[str, int] = {}

    def __init__(self, *, follow_redirects: bool) -> None:
        self.follow_redirects = follow_redirects

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        return None

    def head(self, target: str, *, timeout: float) -> httpx.Response:
        self.calls.append(target)
        remaining_failures = self.failures.get(target, 0)
        if remaining_failures:
            self.failures[target] = remaining_failures - 1
            raise httpx.ConnectError("connect failed")
        return httpx.Response(204)


EXPECTED_ENDPOINT_URLS = {
    "claude": "https://api.anthropic.com",
    "codex": "https://api.openai.com",
    "deepseek-chat": "https://openrouter.ai/api/v1/models",
    "deepseek-reasoner": "https://openrouter.ai/api/v1/models",
    "gemini": "https://generativelanguage.googleapis.com",
    "kimi": "https://openrouter.ai/api/v1/models",
    "openrouter": "https://openrouter.ai/api/v1/models",
}


def _install_fake_probe(monkeypatch, perf_values: list[float]) -> None:
    monkeypatch.setattr("conductor.network_profile.httpx.Client", _FakeClient)
    monkeypatch.setattr(
        "conductor.network_profile.time.perf_counter",
        lambda: perf_values.pop(0),
    )
    monkeypatch.setattr("conductor.network_profile.time.monotonic", lambda: 10.0)


def test_provider_endpoint_urls_are_adapter_owned() -> None:
    for provider_id, expected in EXPECTED_ENDPOINT_URLS.items():
        assert get_provider(provider_id).endpoint_url() == expected

    assert get_provider("ollama").endpoint_url() is None
    assert (
        ShellProvider(ShellProviderSpec(name="local-shell", shell="printf ok"))
        .endpoint_url()
        is None
    )


def test_network_target_for_provider_delegates_to_provider_endpoint() -> None:
    for provider_id in EXPECTED_ENDPOINT_URLS:
        provider = get_provider(provider_id)
        assert _network_target_for_provider(provider_id) == provider.endpoint_url()

    assert _network_target_for_provider(None) == NETWORK_PROFILE_FALLBACK_TARGET


def test_network_target_for_ollama_keeps_local_base_url(monkeypatch) -> None:
    monkeypatch.delenv("OLLAMA_BASE_URL", raising=False)
    assert _network_target_for_provider("ollama") == "http://localhost:11434"

    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama.internal:11434")
    assert _network_target_for_provider("ollama") == "http://ollama.internal:11434"


def test_cache_hit_reuses_fresh_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = _cache_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "rtt_ms": 123,
                "target": "https://api.example.test",
                "timestamp": 1_000,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "conductor.network_profile.httpx.Client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("probe should not run")),
    )

    profile = get_network_profile("https://api.example.test", now=1_100)

    assert profile == NetworkProfile(
        rtt_ms=123,
        target="https://api.example.test",
        timestamp=1_000,
    )


def test_cache_miss_probes_and_writes_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _FakeClient.calls = []
    _FakeClient.failures = {}
    _install_fake_probe(monkeypatch, [0.0, 0.050, 1.0, 1.120, 2.0, 2.250])

    profile = get_network_profile("https://api.example.test", now=2_000)

    assert profile.rtt_ms == 120
    assert profile.target == "https://api.example.test"
    cached = json.loads(_cache_file(tmp_path).read_text(encoding="utf-8"))
    assert cached["rtt_ms"] == 120
    assert cached["target"] == "https://api.example.test"


def test_ttl_expiry_reprobes(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = _cache_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "rtt_ms": 90,
                "target": "https://api.example.test",
                "timestamp": 1_000,
            }
        ),
        encoding="utf-8",
    )
    _FakeClient.calls = []
    _FakeClient.failures = {}
    _install_fake_probe(monkeypatch, [0.0, 0.300, 1.0, 1.330, 2.0, 2.360])

    profile = get_network_profile(
        "https://api.example.test",
        now=1_000 + NETWORK_PROFILE_TTL_SEC + 1,
    )

    assert profile.rtt_ms == 330
    assert _FakeClient.calls == ["https://api.example.test"] * 3


def test_scaling_tiers():
    assert scaling_multiplier(NetworkProfile(99, "target", 0)) == 1
    assert scaling_multiplier(NetworkProfile(100, "target", 0)) == 2
    assert scaling_multiplier(NetworkProfile(250, "target", 0)) == 3
    assert apply_scaling(600, NetworkProfile(310, "target", 0)) == 1800


def test_fallback_target_when_provider_unreachable(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    _FakeClient.calls = []
    _FakeClient.failures = {"https://api.example.test": 3}
    _install_fake_probe(
        monkeypatch,
        [0.0, 1.0, 2.0, 3.0, 3.040, 4.0, 4.070, 5.0, 5.100],
    )

    profile = get_network_profile("https://api.example.test", now=3_000)

    assert profile.target == NETWORK_PROFILE_FALLBACK_TARGET
    assert profile.rtt_ms == 70
    assert _FakeClient.calls == [
        "https://api.example.test",
        "https://api.example.test",
        "https://api.example.test",
        NETWORK_PROFILE_FALLBACK_TARGET,
        NETWORK_PROFILE_FALLBACK_TARGET,
        NETWORK_PROFILE_FALLBACK_TARGET,
    ]


def test_corrupt_cache_is_deleted_and_rebuilt(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    path = _cache_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text("{not json", encoding="utf-8")
    warnings: list[str] = []
    _FakeClient.calls = []
    _FakeClient.failures = {}
    _install_fake_probe(monkeypatch, [0.0, 0.080, 1.0, 1.090, 2.0, 2.100])

    profile = get_network_profile(
        "https://api.example.test",
        now=4_000,
        warn=warnings.append,
    )

    assert profile.rtt_ms == 90
    assert "deleted corrupt cache" in warnings[0]
    assert json.loads(path.read_text(encoding="utf-8"))["rtt_ms"] == 90


def test_exec_default_scaling_line_and_values(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch.object(CodexProvider, "configured", return_value=(True, None))
    mocker.patch.object(CodexProvider, "health_probe", return_value=(True, None))
    exec_mock = mocker.patch.object(
        CodexProvider,
        "exec",
        return_value=CallResponse(
            text="ok",
            provider="codex",
            model="gpt",
            duration_ms=1,
            usage={},
            raw={},
        ),
    )
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(310, "https://api.openai.com", 1_000),
    )

    result = CliRunner().invoke(
        main,
        ["exec", "--with", "codex", "--task", "Reply OK."],
    )

    assert result.exit_code == 0, result.output
    assert "310ms RTT to api.openai.com" in result.stderr
    assert "timeouts scaled 3×" in result.stderr
    # exec --timeout default is None (unbounded); scaling None stays None.
    assert exec_mock.call_args.kwargs["timeout_sec"] is None
    # max_stall_sec default 360 → 3× = 1080.
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 1080


def test_call_default_scaling_line_and_values(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch.object(CodexProvider, "configured", return_value=(True, None))
    call_mock = mocker.patch.object(
        CodexProvider,
        "call",
        return_value=CallResponse(
            text="ok",
            provider="codex",
            model="gpt",
            duration_ms=1,
            usage={},
            raw={},
        ),
    )
    mocker.patch(
        "conductor.cli.get_network_profile",
        return_value=NetworkProfile(180, "https://api.openai.com", 1_000),
    )

    result = CliRunner().invoke(
        main,
        ["call", "--with", "codex", "--task", "Reply OK."],
    )

    assert result.exit_code == 0, result.output
    assert "180ms RTT to api.openai.com" in result.stderr
    assert "timeouts scaled 2×" in result.stderr
    # call --timeout default is None (unbounded); scaling None stays None.
    assert call_mock.call_args.kwargs["timeout_sec"] is None
    # max_stall_sec default 360 → 2× = 720.
    assert call_mock.call_args.kwargs["max_stall_sec"] == 720


def test_user_timeout_and_stall_overrides_win(monkeypatch, mocker, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    mocker.patch.object(CodexProvider, "configured", return_value=(True, None))
    mocker.patch.object(CodexProvider, "health_probe", return_value=(True, None))
    profile_mock = mocker.patch("conductor.cli.get_network_profile")
    exec_mock = mocker.patch.object(
        CodexProvider,
        "exec",
        return_value=CallResponse(
            text="ok",
            provider="codex",
            model="gpt",
            duration_ms=1,
            usage={},
            raw={},
        ),
    )

    result = CliRunner().invoke(
        main,
        [
            "exec",
            "--with",
            "codex",
            "--timeout",
            "600",
            "--max-stall-seconds",
            "5",
            "--task",
            "Reply OK.",
        ],
    )

    assert result.exit_code == 0, result.output
    profile_mock.assert_not_called()
    assert exec_mock.call_args.kwargs["timeout_sec"] == 600
    assert exec_mock.call_args.kwargs["max_stall_sec"] == 5
