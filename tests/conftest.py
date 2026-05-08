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
