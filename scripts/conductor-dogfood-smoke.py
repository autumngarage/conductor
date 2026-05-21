#!/usr/bin/env python3
"""Deterministic Conductor dogfood smoke for merge gates.

This intentionally avoids live provider credentials. It registers a temporary
custom shell provider and exercises Conductor's real CLI entrypoint for route,
call, and exec JSON paths.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path
from typing import Any

PROVIDER_NAME = "dogfood-local"
ROUTER_TAG = "dogfood-smoke"
BUILTIN_PROVIDER_EXCLUDES = (
    "claude",
    "codex",
    "deepseek-chat",
    "deepseek-reasoner",
    "gemini",
    "kimi",
    "ollama",
    "openrouter",
)
LIVE_PROVIDER_ENV = (
    "ANTHROPIC_API_KEY",
    "CLAUDE_API_KEY",
    "CLOUDFLARE_ACCOUNT_ID",
    "CLOUDFLARE_API_TOKEN",
    "DEEPSEEK_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "KIMI_API_KEY",
    "MOONSHOT_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
)


class DogfoodError(RuntimeError):
    pass


def main() -> int:
    repo_root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix="conductor-dogfood-") as raw_tmp:
        tmp = Path(raw_tmp)
        shim = tmp / "dogfood_provider.py"
        providers_file = tmp / "providers.toml"
        home_router_defaults_file = tmp / "home-router.toml"
        repo_router_defaults_file = tmp / "repo-router.toml"
        cache_dir = tmp / "xdg-cache"
        conductor_home = tmp / "conductor-home"
        log_file = tmp / "exec-session.ndjson"

        _write_provider_shim(shim)
        _write_providers_file(providers_file, shim)
        env = _dogfood_env(
            repo_root=repo_root,
            providers_file=providers_file,
            home_router_defaults_file=home_router_defaults_file,
            repo_router_defaults_file=repo_router_defaults_file,
            cache_dir=cache_dir,
            conductor_home=conductor_home,
        )

        route = _run_json(
            repo_root,
            env,
            "route",
            "--tags",
            ROUTER_TAG,
            "--prefer",
            "balanced",
            "--exclude",
            ",".join(BUILTIN_PROVIDER_EXCLUDES),
            "--json",
        )
        _assert(route.get("provider") == PROVIDER_NAME, "route did not pick dogfood provider")

        call = _run_json(
            repo_root,
            env,
            "call",
            "--with",
            PROVIDER_NAME,
            "--task",
            "DOGFOOD_CALL",
            "--json",
        )
        _assert(call.get("provider") == PROVIDER_NAME, "call used the wrong provider")
        _assert("DOGFOOD_CALL" in str(call.get("text")), "call did not return provider output")

        exec_response = _run_json(
            repo_root,
            env,
            "exec",
            "--with",
            PROVIDER_NAME,
            "--no-preflight",
            "--allow-short-brief",
            "--task",
            "DOGFOOD_EXEC",
            "--log-file",
            str(log_file),
            "--json",
        )
        _assert(exec_response.get("provider") == PROVIDER_NAME, "exec used the wrong provider")
        _assert(
            "DOGFOOD_EXEC" in str(exec_response.get("text")),
            "exec did not return provider output",
        )
        raw = exec_response.get("raw")
        _assert(isinstance(raw, dict), "exec response raw payload is missing")
        health = raw.get("conductor_run_health")
        _assert(isinstance(health, dict), "exec run health was not attached")
        _assert(health.get("status") == "healthy", "exec run health was not healthy")
        _assert(_session_log_has_event(log_file, "run_health"), "exec log missed run_health event")

    print("dogfood smoke: route/call/exec passed")
    return 0


def _write_provider_shim(path: Path) -> None:
    path.write_text(
        textwrap.dedent(
            """\
            from __future__ import annotations

            import sys

            prompt = sys.stdin.read()
            if not prompt and len(sys.argv) > 1:
                prompt = " ".join(sys.argv[1:])
            first_line = prompt.strip().splitlines()[0] if prompt.strip() else ""
            print("DOGFOOD_OK " + first_line[:120])
            """
        ),
        encoding="utf-8",
    )


def _write_providers_file(path: Path, shim: Path) -> None:
    shell = f"{shlex.quote(sys.executable)} {shlex.quote(str(shim))}"
    path.write_text(
        textwrap.dedent(
            f"""\
            [[providers]]
            name = "{PROVIDER_NAME}"
            shell = "{_toml_string(shell)}"
            accepts = "stdin"
            tags = ["{ROUTER_TAG}", "cheap", "fast", "research"]
            tier = "local"
            typical_p50_ms = 1
            """
        ),
        encoding="utf-8",
    )


def _dogfood_env(
    *,
    repo_root: Path,
    providers_file: Path,
    home_router_defaults_file: Path,
    repo_router_defaults_file: Path,
    cache_dir: Path,
    conductor_home: Path,
) -> dict[str, str]:
    env = os.environ.copy()
    for name in LIVE_PROVIDER_ENV:
        env.pop(name, None)
    env.update(
        {
            "CONDUCTOR_AUTO_REFRESH_VIA_PR": "never",
            "CONDUCTOR_HOME": str(conductor_home),
            "CONDUCTOR_NO_AUTO_REFRESH": "1",
            "CONDUCTOR_PROVIDERS_FILE": str(providers_file),
            "CONDUCTOR_REPO_ROUTER_DEFAULTS_FILE": str(repo_router_defaults_file),
            "CONDUCTOR_ROUTER_DEFAULTS_FILE": str(home_router_defaults_file),
            "XDG_CACHE_HOME": str(cache_dir),
        }
    )
    src_path = str(repo_root / "src")
    existing_pythonpath = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        src_path if not existing_pythonpath else os.pathsep.join((src_path, existing_pythonpath))
    )
    return env


def _run_json(repo_root: Path, env: dict[str, str], *args: str) -> dict[str, Any]:
    result = subprocess.run(
        [sys.executable, "-m", "conductor.cli", *args],
        cwd=repo_root,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if result.returncode != 0:
        raise DogfoodError(
            f"`conductor {' '.join(args)}` exited {result.returncode}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise DogfoodError(
            f"`conductor {' '.join(args)}` did not emit JSON stdout:\n{result.stdout}"
        ) from exc
    if not isinstance(payload, dict):
        raise DogfoodError(f"`conductor {' '.join(args)}` emitted non-object JSON")
    return payload


def _session_log_has_event(path: Path, event_name: str) -> bool:
    if not path.exists():
        return False
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("event") == event_name:
            return True
    return False


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise DogfoodError(message)


def _toml_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


if __name__ == "__main__":
    raise SystemExit(main())
