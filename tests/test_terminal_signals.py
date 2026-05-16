"""Direct coverage for the provider-stream classifier.

Most of the classifier's behavior is exercised indirectly through the
adapter integration tests. The tool-error category — added to fix #443
where a Gemini tool failure was reported as "rate limit on stderr" —
deserves dedicated coverage so the ranking against rate-limit signals
in the same buffer doesn't silently regress.
"""

from __future__ import annotations

from conductor.providers.terminal_signals import (
    detect_retriable_provider_failure,
)


def test_tool_error_takes_precedence_over_rate_limit_in_same_buffer():
    """Mixed buffer: a Gemini tool failure followed by rate-limit-shaped
    noise (deprecation warnings, downstream messages) used to be tagged
    as `rate-limit`. The actionable signal is the tool error.
    """
    text = (
        "YOLO mode is enabled. All tool calls will be automatically approved. "
        "Ripgrep is not available. Falling back to GrepTool. "
        "Error executing tool replace: Error: Failed to apply edit. "
        "You have hit your limit on retries."
    )
    signal = detect_retriable_provider_failure(text, source="stderr")

    assert signal is not None
    assert signal.category == "tool-error"
    # Detail should anchor to the tool-error message, not the noise prefix.
    assert signal.detail.startswith("Error executing tool replace")


def test_tool_error_label_is_tool_execution_error():
    text = "Error executing tool write_file: permission denied"
    signal = detect_retriable_provider_failure(text, source="stderr")

    assert signal is not None
    assert signal.error_message("gemini").startswith(
        "gemini reported tool execution error on stderr:"
    )


def test_pure_rate_limit_still_classified_as_rate_limit():
    """No tool-error pattern present — classification must stay rate-limit
    so existing rate-limit handling (cascade fallback, retry budget) keeps
    working.
    """
    text = "You have hit your limit. HTTP 429 Too Many Requests"
    signal = detect_retriable_provider_failure(text, source="stderr")

    assert signal is not None
    assert signal.category == "rate-limit"


def test_tool_execution_failed_variant_is_recognized():
    text = "Tool execution failed: replace returned non-zero"
    signal = detect_retriable_provider_failure(text, source="stderr")

    assert signal is not None
    assert signal.category == "tool-error"
