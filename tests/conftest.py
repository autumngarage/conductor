"""Test-suite-wide fixtures and environment scrubbing.

When pre-commit's pre-push hook runs the test suite, it inherits the bare
repo's git env (notably ``GIT_DIR``). Tests that create disposable repos in
``tmp_path`` and shell out to ``git`` then attach to the *bare repo's* index
instead of their own — re-init warnings, contaminated indexes, and "no
.pre-commit-config.yaml" failures from the bare repo's hooks chain all
follow.

We strip the offending vars once at collection so every test sees a clean
environment regardless of how pytest was launched. The vars listed here
are the full set git inspects to locate the active repo (see ``man
git-environment``).
"""

from __future__ import annotations

import os
import time

import pytest

for _var in (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY",
    "GIT_NAMESPACE",
):
    os.environ.pop(_var, None)


@pytest.fixture(autouse=True)
def _isolate_xdg_cache_home(monkeypatch, tmp_path):
    """Repo-wide isolation of conductor's user cache so tests can't pollute it.

    Conductor's cache directory resolves via ``conductor.offline_mode._cache_dir``
    which reads ``$XDG_CACHE_HOME`` and falls back to ``~/.cache``. Any test
    that exercises a code path which writes there (delegation ledger entries,
    stall envelopes, sessions, offline marker, OpenRouter catalog) will land
    real artifacts in the developer's ``~/.cache/conductor/`` unless the
    test explicitly redirects.

    PR #452 fixed this for ``test_adapters_subprocess.py`` specifically (stall
    envelope writes). This is the broader version: every test in this
    repository runs against a tmp ``XDG_CACHE_HOME`` so the user's real
    cache is read-only as far as the test suite is concerned. A full
    ``uv run pytest`` should now leave ``~/.cache/conductor/`` byte-identical
    to its pre-run state.

    Tests that need a specific cache layout (e.g. seeding offline marker,
    pre-populated catalogs) still work — they read ``XDG_CACHE_HOME`` from
    the env and write under tmp_path, isolated from siblings *and* from
    the developer's real cache.
    """
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))


@pytest.fixture(autouse=True)
def _fast_cli_network_profile(monkeypatch):
    """Keep CLI tests off the real network unless they patch this explicitly."""
    from conductor import cli
    from conductor.network_profile import NETWORK_PROFILE_FALLBACK_TARGET, NetworkProfile

    monkeypatch.setenv("CONDUCTOR_NO_AUTO_REFRESH", "1")

    def _profile(target: str | None, *, warn=None):
        return NetworkProfile(
            rtt_ms=50,
            target=target or NETWORK_PROFILE_FALLBACK_TARGET,
            timestamp=time.time(),
        )

    monkeypatch.setattr(cli, "get_network_profile", _profile)
