"""Tests for the credentials resolver — mocked subprocess, no real Keychain."""

from __future__ import annotations

import subprocess

import pytest

from conductor import credentials


def _fake_completed(stdout: str = "", stderr: str = "", returncode: int = 0):
    return subprocess.CompletedProcess(
        args=["stub"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_get_prefers_env_var(monkeypatch, mocker):
    monkeypatch.setenv("CONDUCTOR_TEST_KEY", "from-env")
    # Even if Keychain would return something, env var wins.
    mocker.patch.object(credentials, "_keychain_find", return_value="from-keychain")
    assert credentials.get("CONDUCTOR_TEST_KEY") == "from-env"


def test_get_falls_back_to_keychain(monkeypatch, mocker):
    monkeypatch.delenv("CONDUCTOR_TEST_KEY", raising=False)
    mocker.patch.object(credentials.sys, "platform", "darwin")
    mocker.patch.object(credentials, "_keychain_find", return_value="from-keychain")
    assert credentials.get("CONDUCTOR_TEST_KEY") == "from-keychain"


def test_get_returns_none_when_unset(monkeypatch, mocker):
    monkeypatch.delenv("CONDUCTOR_TEST_KEY", raising=False)
    mocker.patch.object(credentials, "_keychain_find", return_value=None)
    assert credentials.get("CONDUCTOR_TEST_KEY") is None


def test_set_in_keychain_calls_security(mocker):
    mocker.patch.object(credentials.sys, "platform", "darwin")
    mocker.patch("conductor.credentials.shutil.which", return_value="/usr/bin/security")
    run_mock = mocker.patch(
        "conductor.credentials.subprocess.run",
        return_value=_fake_completed(returncode=0),
    )

    credentials.set_in_keychain("SOME_KEY", "secret-value")

    args = run_mock.call_args.args[0]
    assert args[0] == "security"
    assert args[1] == "add-generic-password"
    assert "-U" in args  # update-if-exists
    assert "-w" in args and args[args.index("-w") + 1] == "secret-value"


def test_set_in_keychain_raises_on_non_zero(mocker):
    mocker.patch.object(credentials.sys, "platform", "darwin")
    mocker.patch("conductor.credentials.shutil.which", return_value="/usr/bin/security")
    mocker.patch(
        "conductor.credentials.subprocess.run",
        return_value=_fake_completed(stderr="denied", returncode=1),
    )
    with pytest.raises(RuntimeError) as exc:
        credentials.set_in_keychain("K", "v")
    assert "denied" in str(exc.value)


def test_set_in_keychain_raises_on_non_darwin(mocker):
    mocker.patch.object(credentials.sys, "platform", "linux")
    with pytest.raises(RuntimeError) as exc:
        credentials.set_in_keychain("K", "v")
    assert "macOS-only" in str(exc.value)


def test_keychain_find_parses_stdout(mocker):
    mocker.patch("conductor.credentials.shutil.which", return_value="/usr/bin/security")
    mocker.patch(
        "conductor.credentials.subprocess.run",
        return_value=_fake_completed(stdout="value-from-keychain\n"),
    )
    assert credentials._keychain_find("K") == "value-from-keychain"


def test_keychain_find_returns_none_on_non_zero(mocker):
    mocker.patch("conductor.credentials.shutil.which", return_value="/usr/bin/security")
    mocker.patch(
        "conductor.credentials.subprocess.run",
        return_value=_fake_completed(returncode=44),
    )
    assert credentials._keychain_find("missing") is None


def test_keychain_find_returns_none_when_security_absent(mocker):
    mocker.patch("conductor.credentials.shutil.which", return_value=None)
    assert credentials._keychain_find("K") is None


def test_keychain_has_shortcut(mocker):
    mocker.patch.object(credentials, "_keychain_find", return_value="x")
    assert credentials.keychain_has("K") is True
    mocker.patch.object(credentials, "_keychain_find", return_value=None)
    assert credentials.keychain_has("K") is False


# --------------------------------------------------------------------------- #
# key_command — credentials.toml load / save / resolve.
# --------------------------------------------------------------------------- #


@pytest.fixture
def credfile(tmp_path, monkeypatch):
    """Isolate the credentials TOML to a per-test tmp path."""
    path = tmp_path / "credentials.toml"
    monkeypatch.setenv(credentials.CREDENTIALS_FILE_ENV, str(path))
    credentials.clear_key_command_cache()
    yield path
    credentials.clear_key_command_cache()


def test_load_key_commands_empty_when_file_missing(credfile):
    assert credentials.load_key_commands() == {}


def test_save_and_load_key_command_roundtrips(credfile):
    credentials.save_key_command("FOO_KEY", "echo hello")
    assert credentials.load_key_commands() == {"FOO_KEY": "echo hello"}


def test_save_key_command_sets_file_mode_0600(credfile):
    credentials.save_key_command("FOO_KEY", "echo hello")
    import stat

    mode = stat.S_IMODE(credfile.stat().st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"


def test_save_key_command_merges_multiple_entries(credfile):
    credentials.save_key_command("A_KEY", "echo a")
    credentials.save_key_command("B_KEY", "echo b")
    loaded = credentials.load_key_commands()
    assert loaded == {"A_KEY": "echo a", "B_KEY": "echo b"}


def test_save_key_command_escapes_quotes_and_backslashes(credfile):
    cmd = 'echo "hello\\world"'
    credentials.save_key_command("K", cmd)
    assert credentials.load_key_commands()["K"] == cmd


def test_delete_key_command_removes_entry(credfile):
    credentials.save_key_command("A_KEY", "echo a")
    credentials.save_key_command("B_KEY", "echo b")
    assert credentials.delete_key_command("A_KEY") is True
    assert credentials.load_key_commands() == {"B_KEY": "echo b"}


def test_delete_key_command_removes_file_when_last_entry_removed(credfile):
    credentials.save_key_command("A_KEY", "echo a")
    assert credentials.delete_key_command("A_KEY") is True
    assert not credfile.exists()


def test_delete_key_command_returns_false_when_missing(credfile):
    assert credentials.delete_key_command("NOPE") is False


def test_load_key_commands_warns_and_returns_empty_on_invalid_toml(
    credfile, capsys
):
    credfile.write_text("this is = not [valid toml\n")
    assert credentials.load_key_commands() == {}
    err = capsys.readouterr().err
    assert "not valid TOML" in err


def test_load_key_commands_ignores_non_string_entries(credfile):
    credfile.write_text(
        '[key_commands]\nGOOD = "echo good"\nBAD = 42\nEMPTY = ""\n'
    )
    assert credentials.load_key_commands() == {"GOOD": "echo good"}


def test_get_resolves_via_key_command(credfile, monkeypatch, mocker):
    monkeypatch.delenv("MY_KEY", raising=False)
    mocker.patch.object(credentials, "_keychain_find", return_value=None)
    credentials.save_key_command("MY_KEY", "echo from-op")
    assert credentials.get("MY_KEY") == "from-op"


def test_resolve_with_source_reports_env_first(credfile, monkeypatch):
    monkeypatch.setenv("MY_KEY", "from-env")
    credentials.save_key_command("MY_KEY", "echo from-op")
    value, source = credentials.resolve_with_source("MY_KEY")
    assert value == "from-env"
    assert source == "env"


def test_resolve_with_source_reports_key_command_when_no_env(
    credfile, monkeypatch, mocker
):
    monkeypatch.delenv("MY_KEY", raising=False)
    mocker.patch.object(credentials, "_keychain_find", return_value="from-keychain")
    credentials.save_key_command("MY_KEY", "echo from-op")
    value, source = credentials.resolve_with_source("MY_KEY")
    assert value == "from-op"
    assert source == "key_command"


def test_resolve_with_source_reports_keychain_when_no_env_or_command(
    credfile, monkeypatch, mocker
):
    monkeypatch.delenv("MY_KEY", raising=False)
    mocker.patch.object(credentials.sys, "platform", "darwin")
    mocker.patch.object(credentials, "_keychain_find", return_value="from-keychain")
    value, source = credentials.resolve_with_source("MY_KEY")
    assert value == "from-keychain"
    assert source == "keychain"


def test_key_command_failure_does_not_fall_through_to_keychain(
    credfile, monkeypatch, mocker, capsys
):
    """If the user explicitly configured key_command, a failed resolution
    must not silently fall back to a possibly-stale keychain value — the
    operator needs to see their configuration is broken."""
    monkeypatch.delenv("MY_KEY", raising=False)
    mocker.patch.object(credentials, "_keychain_find", return_value="STALE-KEY")
    credentials.save_key_command("MY_KEY", "false")  # exits 1
    value, source = credentials.resolve_with_source("MY_KEY")
    assert value is None
    assert source is None
    err = capsys.readouterr().err
    assert "MY_KEY" in err and "exited" in err


def test_key_command_unknown_binary_warns(credfile, capsys):
    credentials.save_key_command(
        "MY_KEY", "this-command-definitely-does-not-exist-xyz123 read foo"
    )
    assert credentials.get("MY_KEY") is None
    err = capsys.readouterr().err
    assert "not found on PATH" in err


def test_key_command_empty_output_returns_none(credfile, capsys):
    credentials.save_key_command("MY_KEY", "true")  # exits 0, empty stdout
    assert credentials.get("MY_KEY") is None
    err = capsys.readouterr().err
    assert "empty output" in err


def test_key_command_result_is_cached_per_process(credfile, mocker):
    credentials.save_key_command("MY_KEY", "echo cached-value")
    spy = mocker.spy(credentials.subprocess, "run")
    assert credentials.get("MY_KEY") == "cached-value"
    assert credentials.get("MY_KEY") == "cached-value"
    # Only one subprocess invocation despite two get() calls.
    assert spy.call_count == 1


def test_clear_key_command_cache_forces_re_resolve(credfile, mocker):
    credentials.save_key_command("MY_KEY", "echo first")
    assert credentials.get("MY_KEY") == "first"
    credentials.clear_key_command_cache()
    # Rewrite to a new value; without cache clear, we'd get the old one.
    credentials.save_key_command("MY_KEY", "echo second")
    assert credentials.get("MY_KEY") == "second"


def test_save_key_command_rejects_empty_inputs(credfile):
    with pytest.raises(ValueError):
        credentials.save_key_command("", "echo x")
    with pytest.raises(ValueError):
        credentials.save_key_command("KEY", "   ")


# --------------------------------------------------------------------------- #
# set_key_commands — atomic multi-credential update (regression coverage).
# --------------------------------------------------------------------------- #


def test_set_key_commands_atomic_write_preserves_unrelated_entries(credfile):
    """Updating credential A must not touch the user's existing credential B."""
    credentials.save_key_command("PRE_EXISTING", "echo old-value")
    credentials.set_key_commands({"NEW_KEY": "echo new-value"})
    loaded = credentials.load_key_commands()
    assert loaded == {
        "PRE_EXISTING": "echo old-value",
        "NEW_KEY": "echo new-value",
    }


def test_set_key_commands_overwrites_same_key(credfile):
    """A second wizard run for the same provider should update, not append."""
    credentials.save_key_command("MY_KEY", "echo first-version")
    credentials.set_key_commands({"MY_KEY": "echo second-version"})
    assert credentials.load_key_commands() == {"MY_KEY": "echo second-version"}


def test_set_key_commands_writes_multiple_in_one_pass(credfile):
    """Two credentials supplied together should land together (kimi case)."""
    credentials.set_key_commands(
        {
            "CLOUDFLARE_API_TOKEN": "op read op://Personal/CF/token",
            "CLOUDFLARE_ACCOUNT_ID": "op read op://Personal/CF/account",
        }
    )
    loaded = credentials.load_key_commands()
    assert loaded == {
        "CLOUDFLARE_API_TOKEN": "op read op://Personal/CF/token",
        "CLOUDFLARE_ACCOUNT_ID": "op read op://Personal/CF/account",
    }


def test_set_key_commands_rejects_empty_dict(credfile):
    with pytest.raises(ValueError):
        credentials.set_key_commands({})


def test_set_key_commands_rejects_invalid_value(credfile):
    with pytest.raises(ValueError):
        credentials.set_key_commands({"K": ""})
    with pytest.raises(ValueError):
        credentials.set_key_commands({"K": "   "})


def test_set_key_commands_clears_cache_for_updated_keys_only(credfile, mocker):
    """Cache invalidation is per-key — updating A shouldn't drop the cache
    entry for B (an unrelated credential's resolved value)."""
    credentials.save_key_command("KEY_A", "echo from-a")
    credentials.save_key_command("KEY_B", "echo from-b")
    # Prime cache for both.
    assert credentials.get("KEY_A") == "from-a"
    assert credentials.get("KEY_B") == "from-b"
    # Update only KEY_A.
    credentials.set_key_commands({"KEY_A": "echo from-a-v2"})
    spy = mocker.spy(credentials.subprocess, "run")
    # KEY_B still served from cache (no new subprocess).
    assert credentials.get("KEY_B") == "from-b"
    assert spy.call_count == 0
    # KEY_A re-resolves.
    assert credentials.get("KEY_A") == "from-a-v2"
    assert spy.call_count == 1
