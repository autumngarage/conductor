#!/usr/bin/env python3
"""Warn when PR text looks like a symptom patch without a root-cause note."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

SMELL_PATTERNS = (
    re.compile(r"\braise(?:s|d)?\s+the\s+cap\b", re.IGNORECASE),
    re.compile(r"\bwarn(?:s|ed|ing)?\s+when\b", re.IGNORECASE),
    re.compile(r"\bskip\s+if\b", re.IGNORECASE),
    re.compile(r"\bretry\s+on\b", re.IGNORECASE),
)
DISCLOSURE_PATTERNS = (
    re.compile(r"\bsymptom\s+patch\b", re.IGNORECASE),
    re.compile(r"\broot\s+cause\s+is\b", re.IGNORECASE),
    re.compile(r"\broot-cause\b", re.IGNORECASE),
)


def _event_text(path: Path) -> str:
    payload = json.loads(path.read_text(encoding="utf-8"))
    pr = payload.get("pull_request") or {}
    pieces = [
        str(pr.get("title") or ""),
        str(pr.get("body") or ""),
    ]
    return "\n\n".join(pieces)


def _github_warning(message: str) -> str:
    if os.environ.get("GITHUB_ACTIONS"):
        return f"::warning::{message}"
    return f"warning: {message}"


def text_needs_root_cause_note(text: str) -> bool:
    if not any(pattern.search(text) for pattern in SMELL_PATTERNS):
        return False
    return not any(pattern.search(text) for pattern in DISCLOSURE_PATTERNS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--event-path",
        default=os.environ.get("GITHUB_EVENT_PATH"),
        help="GitHub pull_request event JSON. Defaults to GITHUB_EVENT_PATH.",
    )
    parser.add_argument(
        "--text",
        default=None,
        help="Direct text to inspect; mainly for tests.",
    )
    args = parser.parse_args(argv)

    if args.text is not None:
        text = args.text
    elif args.event_path:
        path = Path(args.event_path)
        if not path.exists():
            print(f"warning: GitHub event path not found: {path}", file=sys.stderr)
            return 0
        text = _event_text(path)
    else:
        return 0

    if text_needs_root_cause_note(text):
        print(
            _github_warning(
                "PR text matches bug-triage smell tests but does not disclose "
                "'symptom patch' or 'root cause is'."
            ),
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
