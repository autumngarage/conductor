#!/usr/bin/env python3
"""Warn when shared routing code grows provider-name special cases.

The provider-capability roadmap allows a known baseline while making new
``provider == "codex"``-style branches visible in CI. The script intentionally
exits zero; it is a review signal, not a merge blocker.
"""

from __future__ import annotations

import argparse
import ast
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PATHS = (
    Path("src/conductor/cli.py"),
    Path("src/conductor/semantic.py"),
    Path("src/conductor/router.py"),
)

# Baseline captured when the guardrail was introduced. These are not endorsed;
# they are the audit tail that should shrink as shared code migrates to
# capability queries.
BASELINE_COUNTS = {
    "src/conductor/cli.py": 18,
    "src/conductor/semantic.py": 1,
    "src/conductor/router.py": 0,
}


@dataclass(frozen=True)
class Finding:
    path: Path
    line: int
    expression: str


class ProviderSpecialCaseVisitor(ast.NodeVisitor):
    def __init__(self, path: Path, source: str) -> None:
        self.path = path
        self.source = source
        self.findings: list[Finding] = []

    def visit_Compare(self, node: ast.Compare) -> None:  # noqa: N802 - ast API
        if _is_provider_name_compare(node):
            segment = ast.get_source_segment(self.source, node) or "provider-name comparison"
            self.findings.append(Finding(self.path, node.lineno, " ".join(segment.split())))
        self.generic_visit(node)


def _is_providerish_name(node: ast.AST) -> bool:
    if isinstance(node, ast.Name):
        return node.id in {"provider", "provider_id", "name", "target_provider", "failed"}
    if isinstance(node, ast.Attribute):
        return node.attr in {"provider", "provider_id", "name"}
    if isinstance(node, ast.Call):
        return _is_providerish_name(node.func)
    return False


def _is_provider_literal(node: ast.AST) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str) and node.value in _known_provider_names()
    if isinstance(node, ast.Set | ast.Tuple | ast.List):
        return any(_is_provider_literal(elt) for elt in node.elts)
    return False


def _known_provider_names() -> set[str]:
    return {
        "claude",
        "codex",
        "deepseek-chat",
        "deepseek-reasoner",
        "gemini",
        "kimi",
        "mistral",
        "ollama",
        "openrouter",
    }


def _is_provider_name_compare(node: ast.Compare) -> bool:
    values = [node.left, *node.comparators]
    if not any(_is_providerish_name(value) for value in values):
        return False
    if not any(_is_provider_literal(value) for value in values):
        return False
    return any(isinstance(op, ast.Eq | ast.NotEq | ast.In | ast.NotIn) for op in node.ops)


def scan_path(path: Path) -> list[Finding]:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    visitor = ProviderSpecialCaseVisitor(path, source)
    visitor.visit(tree)
    return visitor.findings


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


def _github_warning(message: str) -> str:
    if os.environ.get("GITHUB_ACTIONS"):
        return f"::warning::{message}"
    return f"warning: {message}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        default=[str(path) for path in DEFAULT_PATHS],
        help="Python files to scan, relative to the repo root.",
    )
    parser.add_argument(
        "--fail-on-new",
        action="store_true",
        help="Exit 1 when a file exceeds the audited baseline. Intended for tests.",
    )
    args = parser.parse_args(argv)

    exit_code = 0
    for raw_path in args.paths:
        path = Path(raw_path)
        if not path.is_absolute():
            path = ROOT / path
        rel = _display_path(path)
        findings = scan_path(path)
        baseline = BASELINE_COUNTS.get(rel, 0)
        if len(findings) <= baseline:
            continue
        print(
            _github_warning(
                f"{rel} has {len(findings)} provider-name branches; "
                f"baseline is {baseline}. Prefer provider capabilities."
            ),
            file=sys.stderr,
        )
        for finding in findings[baseline:]:
            print(
                f"  {_display_path(finding.path)}:{finding.line}: {finding.expression}",
                file=sys.stderr,
            )
        if args.fail_on_new:
            exit_code = 1
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
