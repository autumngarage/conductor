from __future__ import annotations

from pathlib import Path

import pytest

from conductor.internal_config import (
    InternalConfigError,
    internal_telemetry_enabled,
)


def test_internal_telemetry_env_overrides_configs(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    internal = repo / ".conductor" / "internal.toml"
    internal.parent.mkdir()
    internal.write_text(
        "[telemetry]\ncapture_route_decisions = false\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONDUCTOR_INTERNAL_TELEMETRY", "1")

    assert internal_telemetry_enabled(cwd=repo) is True


def test_internal_telemetry_repo_config(monkeypatch, tmp_path):
    monkeypatch.delenv("CONDUCTOR_INTERNAL_TELEMETRY", raising=False)
    monkeypatch.setenv("CONDUCTOR_INTERNAL_CONFIG", str(tmp_path / "missing-home.toml"))
    repo = tmp_path / "repo"
    repo.mkdir()
    internal = repo / ".conductor" / "internal.toml"
    internal.parent.mkdir()
    internal.write_text(
        "[telemetry]\ncapture_route_decisions = true\n",
        encoding="utf-8",
    )

    assert internal_telemetry_enabled(cwd=repo) is True


def test_internal_telemetry_invalid_bool_raises(monkeypatch):
    monkeypatch.setenv("CONDUCTOR_INTERNAL_TELEMETRY", "maybe")

    with pytest.raises(InternalConfigError):
        internal_telemetry_enabled(cwd=Path.cwd())


def test_internal_telemetry_found_from_subdirectory(monkeypatch, tmp_path):
    monkeypatch.delenv("CONDUCTOR_INTERNAL_TELEMETRY", raising=False)
    monkeypatch.setenv("CONDUCTOR_INTERNAL_CONFIG", str(tmp_path / "missing-home.toml"))
    repo = tmp_path / "repo"
    (repo / ".conductor").mkdir(parents=True)
    (repo / ".conductor" / "internal.toml").write_text(
        "[telemetry]\ncapture_route_decisions = true\n",
        encoding="utf-8",
    )
    subdir = repo / "src" / "deep" / "nested"
    subdir.mkdir(parents=True)

    assert internal_telemetry_enabled(cwd=subdir) is True
