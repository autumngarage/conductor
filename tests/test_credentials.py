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
