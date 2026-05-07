from pathlib import Path

from conductor.grounding import format_grounding_warning, ground_citations


def test_ground_citations_handles_supported_patterns_hits_and_misses(tmp_path: Path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "foo.py").write_text("def foo_bar():\n    return 1\n", encoding="utf-8")
    (src / "q.py").write_text("def qux():\n    return 2\n", encoding="utf-8")

    report = ground_citations(
        "\n".join(
            [
                "`foo_bar` in src/foo.py:1",
                "`missing_symbol` in src/foo.py",
                "src/foo.py:2",
                "src/missing.py:13",
                "qux() in src/q.py",
                "absent() in src/q.py",
            ]
        ),
        tmp_path,
    )

    warning = format_grounding_warning(report)

    assert warning is not None
    assert "[conductor] grounding misses: 3" in warning
    assert "`missing_symbol` in src/foo.py — symbol not found in src/foo.py" in warning
    assert "src/missing.py:13 — file does not exist" in warning
    assert "`absent()` in src/q.py — symbol not found in src/q.py" in warning


def test_ground_citations_empty_input_has_no_warning(tmp_path: Path) -> None:
    report = ground_citations("", tmp_path)

    assert report.misses == []
    assert report.errors == []
    assert format_grounding_warning(report) is None
