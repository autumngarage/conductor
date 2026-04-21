"""Conductor CLI — `conductor call --with <provider> --task "..."`.

v0.1 ships only the `call` command in manual mode (`--with <id>`). Auto mode,
`list`, `smoke`, `init`, and `doctor` land in subsequent phases per
autumn-garage/.cortex/plans/conductor-bootstrap.md.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from typing import Optional

import click

from conductor import __version__
from conductor.providers import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    get_provider,
)


def _read_task(task: Optional[str]) -> str:
    """Use --task if provided; otherwise read all of stdin.

    Empty task is an error — silently sending an empty prompt wastes tokens
    and produces a confusing response.
    """
    if task is not None:
        body = task
    elif not sys.stdin.isatty():
        body = sys.stdin.read()
    else:
        raise click.UsageError(
            "no task provided. Pass --task '...' or pipe content on stdin."
        )
    body = body.strip()
    if not body:
        raise click.UsageError("task is empty after stripping whitespace.")
    return body


def _emit(response: CallResponse, *, as_json: bool) -> None:
    if as_json:
        click.echo(json.dumps(asdict(response), default=str, indent=2))
    else:
        click.echo(response.text)


@click.group()
@click.version_option(__version__, prog_name="conductor")
def main() -> None:
    """Pick an LLM, give it a job."""


@main.command()
@click.option(
    "--with",
    "provider_id",
    required=True,
    help="Provider identifier (kimi, claude, codex, gemini, ollama).",
)
@click.option(
    "--task",
    default=None,
    help="The task / prompt. If omitted, read from stdin.",
)
@click.option(
    "--model",
    default=None,
    help="Override the provider's default model.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the full CallResponse as JSON instead of plain text.",
)
def call(
    provider_id: str,
    task: Optional[str],
    model: Optional[str],
    as_json: bool,
) -> None:
    """Send a task to a specific provider and print the response."""
    body = _read_task(task)
    try:
        provider = get_provider(provider_id)
    except KeyError as e:
        raise click.UsageError(str(e)) from e

    try:
        response = provider.call(body, model=model)
    except ProviderConfigError as e:
        click.echo(f"conductor: {e}", err=True)
        sys.exit(2)
    except ProviderError as e:
        click.echo(f"conductor: {e}", err=True)
        sys.exit(1)

    _emit(response, as_json=as_json)


if __name__ == "__main__":
    main()
