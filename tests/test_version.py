from __future__ import annotations

import conductor


def test_git_root_only_matches_editable_source_checkout(tmp_path):
    repo = tmp_path / "consumer-repo"
    (repo / ".git").mkdir(parents=True)

    installed_package = (
        repo
        / ".venv"
        / "lib"
        / "python3.11"
        / "site-packages"
        / "conductor"
        / "__init__.py"
    )
    installed_package.parent.mkdir(parents=True)
    installed_package.write_text("", encoding="utf-8")

    source_package = repo / "src" / "conductor" / "__init__.py"
    source_package.parent.mkdir(parents=True)
    source_package.write_text("", encoding="utf-8")

    assert conductor._git_root(installed_package) is None
    assert conductor._git_root(source_package) == repo


def test_git_describe_version_parser_handles_exact_release():
    assert conductor._parse_git_describe_version("v0.8.1-0-g2e6d0ca") == "0.8.1"


def test_git_describe_version_parser_keeps_release_base_for_local_checkout():
    assert (
        conductor._parse_git_describe_version("v0.8.1-2-gabcdef0-dirty")
        == "0.8.1+2.gabcdef0.dirty"
    )
