from conductor.brief_preprocessor import inject_auto_close


def test_literal_closes_line_already_present_is_noop():
    brief = "Implement the requested change.\n\nCloses #123"

    assert inject_auto_close(brief) == brief


def test_prose_closes_reference_injects_auto_close_section():
    brief = "Implement the requested change. This closes #123 after merge."

    processed = inject_auto_close(brief)

    assert processed != brief
    assert "## Auto-close" in processed
    assert "Closes #123" in processed


def test_multiple_fix_refs_inject_in_order():
    processed = inject_auto_close("fixes #1, fixes #2")

    assert processed.endswith("Closes #1\nCloses #2")


def test_cross_repo_ref_is_preserved():
    processed = inject_auto_close("Fix org/repo#5 in the delegated change.")

    assert "Closes org/repo#5" in processed


def test_no_ref_is_unchanged():
    brief = "Implement the requested change without issue references."

    assert inject_auto_close(brief) == brief


def test_existing_auto_close_section_is_unchanged():
    brief = "Fixes #123.\n\n## Auto-close\n\nCloses #123"

    assert inject_auto_close(brief) == brief


def test_verbs_match_case_insensitively():
    processed = inject_auto_close("Fix #1. FIXES #2. Resolves #3.")

    assert processed.endswith("Closes #1\nCloses #2\nCloses #3")


def test_standalone_hash_ref_is_not_matched():
    brief = "Look at #123 while making the requested change."

    assert inject_auto_close(brief) == brief


def test_code_block_content_is_not_matched():
    brief = "Inspect this example:\n\n```python\n# 123\n# Fixes #456\n```\n"

    assert inject_auto_close(brief) == brief
