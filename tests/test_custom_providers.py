"""Tests for the custom-provider layer — ShellProvider + TOML persistence + CLI."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from conductor.cli import main
from conductor.custom_providers import (
    CustomProviderError,
    add_spec,
    load_specs,
    remove_spec,
    save_specs,
)
from conductor.providers import get_provider, known_providers
from conductor.providers.interface import (
    ProviderConfigError,
    ProviderHTTPError,
    UnsupportedCapability,
)
from conductor.providers.shell import ShellProvider, ShellProviderSpec


@pytest.fixture(autouse=True)
def isolated_providers_file(tmp_path, monkeypatch):
    """Every test gets a fresh custom-providers file in its own tmp dir."""
    providers_file = tmp_path / "providers.toml"
    monkeypatch.setenv("CONDUCTOR_PROVIDERS_FILE", str(providers_file))
    yield providers_file


# ---------------------------------------------------------------------------
# ShellProvider — unit tests on the provider class itself.
# ---------------------------------------------------------------------------


def test_shell_provider_configured_true_when_binary_on_path():
    spec = ShellProviderSpec(name="demo", shell="/bin/cat")
    ok, reason = ShellProvider(spec).configured()
    assert ok is True and reason is None


def test_shell_provider_configured_false_when_binary_missing():
    spec = ShellProviderSpec(name="demo", shell="/absolutely-not-installed-xyz")
    ok, reason = ShellProvider(spec).configured()
    assert ok is False
    assert "absolutely-not-installed-xyz" in reason
    assert "PATH" in reason


def test_shell_provider_call_stdin_round_trips():
    # /bin/cat on stdin echoes its input — perfect stub.
    spec = ShellProviderSpec(name="echo", shell="/bin/cat", accepts="stdin")
    resp = ShellProvider(spec).call("hello from conductor")
    assert resp.text == "hello from conductor"
    assert resp.provider == "echo"
    assert resp.session_id is None  # shell providers are stateless


def test_shell_provider_call_argv_appends_task():
    # `/bin/echo X` prints X — confirms argv-style prompt delivery.
    spec = ShellProviderSpec(name="argv-echo", shell="/bin/echo", accepts="argv")
    resp = ShellProvider(spec).call("argv task")
    assert resp.text.strip() == "argv task"


def test_shell_provider_call_raises_on_non_zero_exit():
    # `/usr/bin/false` always exits non-zero — should raise ProviderHTTPError.
    spec = ShellProviderSpec(name="failing", shell="/usr/bin/false")
    with pytest.raises(ProviderHTTPError) as exc:
        ShellProvider(spec).call("anything")
    assert "failing" in str(exc.value)


def test_shell_provider_call_raises_config_error_when_binary_missing():
    spec = ShellProviderSpec(name="ghost", shell="/nonexistent-binary-qqq")
    with pytest.raises(ProviderConfigError):
        ShellProvider(spec).call("ping")


def test_shell_provider_exec_with_tools_raises_unsupported():
    spec = ShellProviderSpec(name="demo", shell="/bin/cat")
    with pytest.raises(UnsupportedCapability) as exc:
        ShellProvider(spec).exec("task", tools=frozenset({"Read"}))
    assert "tool-use" in str(exc.value)


def test_shell_provider_call_with_resume_raises_unsupported():
    spec = ShellProviderSpec(name="demo", shell="/bin/cat")
    with pytest.raises(UnsupportedCapability) as exc:
        ShellProvider(spec).call("task", resume_session_id="any-id")
    assert "stateless" in str(exc.value)


# ---------------------------------------------------------------------------
# custom_providers.py — TOML persistence round-trip.
# ---------------------------------------------------------------------------


def test_load_specs_returns_empty_when_file_absent(isolated_providers_file):
    assert load_specs() == []


def test_add_save_load_round_trip():
    spec = ShellProviderSpec(
        name="my-local",
        shell="lm-studio-cli",
        accepts="stdin",
        tags=("code-review", "offline"),
        quality_tier="local",
    )
    add_spec(spec)
    loaded = load_specs()
    assert len(loaded) == 1
    assert loaded[0].name == "my-local"
    assert loaded[0].shell == "lm-studio-cli"
    assert loaded[0].tags == ("code-review", "offline")


def test_add_spec_rejects_duplicate_name():
    spec = ShellProviderSpec(name="dup", shell="/bin/cat")
    add_spec(spec)
    with pytest.raises(CustomProviderError) as exc:
        add_spec(spec)
    assert "already exists" in str(exc.value)


def test_add_spec_rejects_builtin_name_shadowing(isolated_providers_file):
    # Simulate someone writing the file by hand with a built-in name, then loading.
    isolated_providers_file.write_text(
        '[[providers]]\nname = "claude"\nshell = "/bin/cat"\n'
    )
    with pytest.raises(CustomProviderError) as exc:
        load_specs()
    assert "built-in" in str(exc.value)


def test_remove_spec_removes_and_reports_what_happened():
    add_spec(ShellProviderSpec(name="a", shell="/bin/cat"))
    add_spec(ShellProviderSpec(name="b", shell="/bin/echo"))
    _, removed = remove_spec("a")
    assert removed is True
    names = [s.name for s in load_specs()]
    assert names == ["b"]

    _, removed_again = remove_spec("a")
    assert removed_again is False  # idempotent


def test_malformed_toml_raises_clear_error(isolated_providers_file):
    isolated_providers_file.write_text("not [ valid toml =")
    with pytest.raises(CustomProviderError) as exc:
        load_specs()
    assert "not valid TOML" in str(exc.value)


def test_missing_required_field_raises(isolated_providers_file):
    isolated_providers_file.write_text('[[providers]]\nname = "no-shell"\n')
    with pytest.raises(CustomProviderError) as exc:
        load_specs()
    assert "shell" in str(exc.value)


def test_invalid_accepts_value_raises(isolated_providers_file):
    isolated_providers_file.write_text(
        '[[providers]]\nname = "x"\nshell = "/bin/cat"\naccepts = "pipe"\n'
    )
    with pytest.raises(CustomProviderError) as exc:
        load_specs()
    assert "accepts" in str(exc.value)
    assert "'stdin'" in str(exc.value) or "stdin" in str(exc.value)


def test_invalid_tier_raises(isolated_providers_file):
    isolated_providers_file.write_text(
        '[[providers]]\nname = "x"\nshell = "/bin/cat"\ntier = "excellent"\n'
    )
    with pytest.raises(CustomProviderError) as exc:
        load_specs()
    assert "tier" in str(exc.value)


def test_save_specs_is_atomic(tmp_path, monkeypatch):
    # Partial save interrupted mid-write should not corrupt an existing file.
    providers_file = tmp_path / "providers.toml"
    monkeypatch.setenv("CONDUCTOR_PROVIDERS_FILE", str(providers_file))
    save_specs([ShellProviderSpec(name="original", shell="/bin/cat")])
    original_content = providers_file.read_text()

    # After a successful save, the tmp sibling should be gone.
    assert not providers_file.with_suffix(".toml.tmp").exists()
    # And the persisted content is exactly what we wrote.
    loaded = load_specs()
    assert [s.name for s in loaded] == ["original"]
    assert original_content == providers_file.read_text()


# ---------------------------------------------------------------------------
# Registry integration — get_provider / known_providers see custom entries.
# ---------------------------------------------------------------------------


def test_known_providers_includes_custom_after_add():
    before = set(known_providers())
    assert "my-custom" not in before
    add_spec(ShellProviderSpec(name="my-custom", shell="/bin/cat"))
    after = set(known_providers())
    assert "my-custom" in after


def test_get_provider_returns_shell_provider_for_custom_name():
    add_spec(ShellProviderSpec(name="my-custom", shell="/bin/cat"))
    provider = get_provider("my-custom")
    assert isinstance(provider, ShellProvider)
    assert provider.name == "my-custom"


def test_get_provider_unknown_name_still_raises_keyerror():
    with pytest.raises(KeyError):
        get_provider("nonexistent-provider-zzz")


def test_corrupt_custom_file_does_not_brick_builtins(isolated_providers_file):
    isolated_providers_file.write_text("garbage {{ not toml")
    # Should not raise — built-ins still resolvable.
    assert "claude" in known_providers()
    provider = get_provider("claude")
    assert provider.name == "claude"


# ---------------------------------------------------------------------------
# CLI surface — providers add / remove / list.
# ---------------------------------------------------------------------------


def test_cli_providers_add_writes_file():
    result = CliRunner().invoke(
        main,
        [
            "providers",
            "add",
            "--name", "cli-demo",
            "--shell", "/bin/cat",
            "--tags", "offline,demo",
            "--tier", "local",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "registered custom provider `cli-demo`" in result.output
    specs = load_specs()
    assert [s.name for s in specs] == ["cli-demo"]


def test_cli_providers_add_rejects_builtin_name():
    result = CliRunner().invoke(
        main,
        ["providers", "add", "--name", "claude", "--shell", "/bin/cat"],
    )
    assert result.exit_code != 0
    assert "built-in" in result.output.lower()


def test_cli_providers_add_duplicate_errors():
    CliRunner().invoke(
        main, ["providers", "add", "--name", "dup", "--shell", "/bin/cat"]
    )
    result = CliRunner().invoke(
        main, ["providers", "add", "--name", "dup", "--shell", "/bin/cat"]
    )
    assert result.exit_code != 0
    assert "already exists" in result.output


def test_cli_providers_remove():
    CliRunner().invoke(
        main, ["providers", "add", "--name", "doomed", "--shell", "/bin/cat"]
    )
    result = CliRunner().invoke(main, ["providers", "remove", "doomed"])
    assert result.exit_code == 0
    assert "removed" in result.output
    assert load_specs() == []


def test_cli_providers_remove_unknown_errors():
    result = CliRunner().invoke(main, ["providers", "remove", "nobody"])
    assert result.exit_code != 0
    assert "no custom provider" in result.output


def test_cli_providers_list_empty():
    result = CliRunner().invoke(main, ["providers", "list"])
    assert result.exit_code == 0
    assert "no custom providers" in result.output


def test_cli_providers_list_json():
    add_spec(
        ShellProviderSpec(
            name="j1",
            shell="/bin/cat",
            tags=("t1", "t2"),
            quality_tier="local",
        )
    )
    result = CliRunner().invoke(main, ["providers", "list", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert len(payload) == 1
    assert payload[0]["name"] == "j1"
    assert payload[0]["tags"] == ["t1", "t2"]


def test_cli_list_command_includes_custom_provider(tmp_path, monkeypatch):
    # `conductor list` (not `providers list`) shows built-ins + custom together.
    add_spec(ShellProviderSpec(name="merged", shell="/bin/cat"))
    result = CliRunner().invoke(main, ["list"])
    assert result.exit_code == 0
    assert "merged" in result.output
