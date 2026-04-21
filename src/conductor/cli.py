"""Conductor CLI — ``conductor call [--with <id> | --auto] --task "..."``.

v0.1 ships the ``call`` command in both manual mode (``--with <id>``) and auto
mode (``--auto``, router picks). ``list``, ``smoke``, ``init``, and ``doctor``
land in Phase 5 per autumn-garage/.cortex/plans/conductor-bootstrap.md.
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
from conductor.router import NoConfiguredProvider, RouteDecision, pick


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


def _parse_tags(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [t.strip() for t in raw.split(",") if t.strip()]


def _emit(
    response: CallResponse,
    *,
    as_json: bool,
    decision: Optional[RouteDecision] = None,
) -> None:
    if as_json:
        payload = asdict(response)
        if decision is not None:
            payload["route"] = asdict(decision)
        click.echo(json.dumps(payload, default=str, indent=2))
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
    default=None,
    help="Provider identifier (kimi, claude, codex, gemini, ollama). "
    "Mutually exclusive with --auto.",
)
@click.option(
    "--auto",
    is_flag=True,
    default=False,
    help="Let the router pick based on --tags and configured providers.",
)
@click.option(
    "--tags",
    default=None,
    help="Comma-separated task tags for --auto routing "
    "(e.g. 'long-context,cheap'). Ignored in --with mode.",
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
    help="Emit the full CallResponse as JSON (with routing info when --auto).",
)
def call(
    provider_id: Optional[str],
    auto: bool,
    tags: Optional[str],
    task: Optional[str],
    model: Optional[str],
    as_json: bool,
) -> None:
    """Send a task to a provider and print the response."""
    if auto and provider_id:
        raise click.UsageError("--with and --auto are mutually exclusive.")
    if not auto and not provider_id:
        raise click.UsageError("pass --with <id> or --auto.")

    body = _read_task(task)

    decision: Optional[RouteDecision] = None
    if auto:
        try:
            provider, decision = pick(_parse_tags(tags))
        except NoConfiguredProvider as e:
            click.echo(f"conductor: {e}", err=True)
            sys.exit(2)
    else:
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

    _emit(response, as_json=as_json, decision=decision)


if __name__ == "__main__":
    main()
