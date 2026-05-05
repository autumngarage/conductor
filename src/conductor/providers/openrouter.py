"""OpenRouter provider — OpenAI-compatible HTTP adapter.

Supports single-turn chat plus Conductor's local tool-call loop for
``exec`` requests. OpenRouter chooses or hosts the model; Conductor still
executes local filesystem/shell tools and feeds results back to the model.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from conductor import credentials
from conductor.openrouter_model_stacks import openrouter_coding_stack
from conductor.providers.interface import (
    CallResponse,
    ProviderConfigError,
    ProviderError,
    ProviderExecutionError,
    ProviderHTTPError,
    UnsupportedCapability,
    resolve_effort_tokens,
)
from conductor.tools import ToolExecutionError, ToolExecutor, build_tool_specs

if TYPE_CHECKING:
    from conductor.session_log import SessionLog

from . import openrouter_catalog

OPENROUTER_API_KEY_ENV = "OPENROUTER_API_KEY"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "openrouter/auto"
OPENROUTER_REQUEST_TIMEOUT_SEC = 120.0
OPENROUTER_HEALTH_PROBE_TIMEOUT_SEC = 10.0
OPENROUTER_MAX_TOOL_ITERATIONS = 10
OPENROUTER_HTTP_REFERER = "https://github.com/autumngarage/conductor"
OPENROUTER_X_TITLE = "conductor"

_OPENROUTER_REASONING_EFFORTS = {
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "max": "xhigh",
}


class OpenRouterProvider:
    name = "openrouter"
    default_model = OPENROUTER_DEFAULT_MODEL
    tags = [
        "long-context",
        "thinking",
        "code-review",
        "tool-use",
        "vision",
        "cheap",
        "strong-reasoning",
    ]
    fix_command = "conductor init --only openrouter"

    quality_tier = "frontier"
    supported_tools: frozenset[str] = frozenset(
        {"Read", "Grep", "Glob", "Edit", "Write", "Bash"}
    )
    enforces_exec_tool_permissions = True
    supports_effort = True
    supports_image_attachments = False
    effort_to_thinking = {
        "minimal": 0,
        "low": 2_000,
        "medium": 8_000,
        "high": 24_000,
        "max": 64_000,
    }
    cost_per_1k_in = 0.005
    cost_per_1k_out = 0.015
    cost_per_1k_thinking = 0.005
    typical_p50_ms = 2500
    max_context_tokens = 200_000

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str = OPENROUTER_BASE_URL,
        timeout_sec: float = OPENROUTER_REQUEST_TIMEOUT_SEC,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout_sec = timeout_sec

    def _resolve_key(self) -> str:
        key = self._api_key or credentials.get(OPENROUTER_API_KEY_ENV)
        if not key:
            raise ProviderConfigError(
                f"{OPENROUTER_API_KEY_ENV} is not set. "
                "Create an API key at https://openrouter.ai/keys and set it via "
                f"`conductor init` or `export {OPENROUTER_API_KEY_ENV}=...`."
            )
        return key

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._resolve_key()}",
            "Content-Type": "application/json",
            "HTTP-Referer": OPENROUTER_HTTP_REFERER,
            "X-Title": OPENROUTER_X_TITLE,
        }

    def _reasoning_payload(self, effort: str | int) -> dict[str, str] | None:
        return _reasoning_payload(effort)

    def _preset_model(self) -> str | None:
        return None

    def _smoke_model(self) -> str:
        return self.default_model

    def configured(self) -> tuple[bool, str | None]:
        if self._api_key or credentials.get(OPENROUTER_API_KEY_ENV):
            return True, None
        return False, (
            f"missing credential: {OPENROUTER_API_KEY_ENV}. "
            "Set via `conductor init` or export as an env var."
        )

    def smoke(self) -> tuple[bool, str | None]:
        try:
            response = self._post_chat(
                {
                    "model": self._smoke_model(),
                    "messages": [{"role": "user", "content": "ping"}],
                    "max_tokens": 1,
                }
            )
        except ProviderConfigError as e:
            return False, str(e)
        except ProviderHTTPError as e:
            return False, str(e)
        if "choices" not in response:
            return False, f"unexpected response shape: {sorted(response)[:5]}"
        return True, None

    def health_probe(
        self, *, timeout_sec: float = OPENROUTER_HEALTH_PROBE_TIMEOUT_SEC
    ) -> tuple[bool, str | None]:
        try:
            headers = self._headers()
        except ProviderConfigError as e:
            return False, str(e)

        url = f"{self._base_url}/models"
        try:
            with httpx.Client(timeout=timeout_sec) as client:
                resp = client.get(url, headers=headers)
        except httpx.TimeoutException:
            return False, f"`GET {url}` timed out after {timeout_sec:.0f}s"
        except httpx.HTTPError as e:
            return False, f"network error calling OpenRouter: {e}"

        if resp.status_code != 200:
            return False, f"OpenRouter returned HTTP {resp.status_code}: {resp.text[:200]}"
        return True, None

    def _post_chat(self, payload: dict) -> dict:
        try:
            with httpx.Client(timeout=self._timeout_sec) as client:
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    headers=self._headers(),
                    json=payload,
                )
        except httpx.HTTPError as e:
            raise ProviderHTTPError(f"network error calling OpenRouter: {e}") from e

        if resp.status_code != 200:
            raise ProviderHTTPError(
                _format_openrouter_http_error(resp.status_code, resp.text, payload)
            )
        try:
            return resp.json()
        except ValueError as e:
            raise ProviderHTTPError(f"OpenRouter response was not JSON: {e}") from e

    def _completion_target_payload(
        self,
        *,
        model: str | None,
        models: tuple[str, ...] | list[str] | None,
        effort: str | int,
        task_tags: list[str] | tuple[str, ...] | None,
        prefer: str,
        exclude: set[str] | frozenset[str] | None,
        log_selection: bool,
    ) -> tuple[dict, str]:
        if model is not None and models:
            raise UnsupportedCapability(
                "openrouter received both `model` and `models`; pass one explicit "
                "model or an ordered fallback list, not both."
            )

        selected_model = model
        ordered_models = tuple(models or ())
        preset_model = (
            self._preset_model()
            if selected_model is None and not ordered_models
            else None
        )
        payload: dict = {}
        if ordered_models:
            payload["models"] = list(ordered_models)
            selected_model = ordered_models[0]
            reasoning = self._reasoning_payload(effort)
            if reasoning is not None:
                payload["reasoning"] = reasoning
        elif preset_model is not None:
            selected_model = preset_model
            payload["model"] = selected_model
            reasoning = self._reasoning_payload(effort)
            if reasoning is not None:
                payload["reasoning"] = reasoning
        elif selected_model is None:
            selector_payload = select_model_for_task(
                task_tags=task_tags,
                prefer=prefer,
                effort=effort,
                exclude=exclude,
            )
            payload.update(selector_payload)
            if "models" in payload:
                selected_model = str(payload["models"][0])
            else:
                selected_model = str(payload["model"])
            if payload.get("reasoning") is None:
                payload.pop("reasoning", None)
            if log_selection:
                _log_selector_choice(
                    task_tags=task_tags,
                    prefer=prefer,
                    payload=selector_payload,
                )
        else:
            payload["model"] = selected_model
            reasoning = self._reasoning_payload(effort)
            if reasoning is not None:
                payload["reasoning"] = reasoning

        return payload, selected_model

    def call(
        self,
        task: str,
        model: str | None = None,
        *,
        models: tuple[str, ...] | list[str] | None = None,
        effort: str | int = "medium",
        task_tags: list[str] | tuple[str, ...] | None = None,
        prefer: str = "balanced",
        exclude: set[str] | frozenset[str] | None = None,
        log_selection: bool = True,
        max_tokens: int | None = None,
        resume_session_id: str | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "openrouter has no session model — each OpenRouter API call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )

        # Fail on missing credentials before any selector/catalog work so an
        # unconfigured provider doesn't mask the real setup error behind a
        # catalog refresh failure.
        self._resolve_key()
        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        target_payload, selected_model = self._completion_target_payload(
            model=model,
            models=models,
            effort=effort,
            task_tags=task_tags,
            prefer=prefer,
            exclude=exclude,
            log_selection=log_selection,
        )
        payload: dict = {
            **target_payload,
            "messages": [{"role": "user", "content": task}],
        }
        if max_tokens is not None:
            payload["max_tokens"] = max(1, max_tokens)

        start = time.monotonic()
        body = self._post_chat(payload)
        duration_ms = int((time.monotonic() - start) * 1000)

        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise ProviderHTTPError(
                f"OpenRouter response missing choices[0].message.content: {body!r:.500}"
            ) from e

        usage = body.get("usage") or {}
        cost_usd = _usage_cost_usd(usage)
        return CallResponse(
            text=text,
            provider=self.name,
            model=body.get("model", selected_model),
            duration_ms=duration_ms,
            usage={
                "input_tokens": usage.get("prompt_tokens"),
                "output_tokens": usage.get("completion_tokens"),
                "cached_tokens": (usage.get("prompt_tokens_details") or {}).get(
                    "cached_tokens"
                ),
                "thinking_tokens": (usage.get("completion_tokens_details") or {}).get(
                    "reasoning_tokens"
                ),
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
            },
            cost_usd=cost_usd,
            raw=body,
        )

    def exec(
        self,
        task: str,
        model: str | None = None,
        *,
        models: tuple[str, ...] | list[str] | None = None,
        effort: str | int = "medium",
        task_tags: list[str] | tuple[str, ...] | None = None,
        prefer: str = "balanced",
        exclude: set[str] | frozenset[str] | None = None,
        log_selection: bool = True,
        tools: frozenset[str] = frozenset(),
        sandbox: str = "none",
        cwd: str | None = None,
        timeout_sec: int | None = None,
        max_stall_sec: int | None = None,
        resume_session_id: str | None = None,
        session_log: SessionLog | None = None,
    ) -> CallResponse:
        if resume_session_id:
            raise UnsupportedCapability(
                "openrouter has no session model — each OpenRouter API call is "
                "stateless. To replay context, prepend the prior turns to `task`."
            )
        if not tools:
            return self.call(
                task,
                model=model,
                models=models,
                effort=effort,
                task_tags=task_tags,
                prefer=prefer,
                exclude=exclude,
                log_selection=log_selection,
            )

        unknown = tools - self.supported_tools
        if unknown:
            raise UnsupportedCapability(
                f"{self.name} does not support tools {sorted(unknown)} "
                f"(supported: {sorted(self.supported_tools)})."
            )

        thinking_budget = resolve_effort_tokens(effort, self.effort_to_thinking)
        effective_task_tags = tuple(task_tags or ())
        if "tool-use" not in effective_task_tags:
            effective_task_tags = (*effective_task_tags, "tool-use")
        target_payload, selected_model = self._completion_target_payload(
            model=model,
            models=models,
            effort=effort,
            task_tags=effective_task_tags,
            prefer=prefer,
            exclude=exclude,
            log_selection=log_selection,
        )

        workdir = Path(cwd) if cwd else Path.cwd()
        executor = ToolExecutor(cwd=workdir)
        tool_specs = build_tool_specs(tools)

        messages: list[dict] = [{"role": "user", "content": task}]
        iteration = 0
        totals = {
            "input_tokens": 0,
            "output_tokens": 0,
            "cached_tokens": 0,
            "thinking_tokens": 0,
        }
        cost_usd_total = 0.0
        cost_usd_seen = False
        iterations_log: list[dict] = []
        final_text = ""
        final_body: dict = {}
        hit_cap = False
        tool_call_count = 0
        write_success_count = 0
        tool_errors: list[dict[str, object]] = []
        bash_failures: list[dict[str, object]] = []
        validation_failures: list[dict[str, object]] = []
        repo_changing_task = _is_repo_changing_tool_task(effective_task_tags, tools)
        git_status_before = _git_clean_status(workdir) if repo_changing_task else None

        start = time.monotonic()
        while iteration < OPENROUTER_MAX_TOOL_ITERATIONS:
            iteration += 1
            payload: dict = {
                **target_payload,
                "messages": messages,
                "tools": tool_specs,
                "tool_choice": "auto",
            }
            body = self._post_chat(payload)
            final_body = body
            message = _first_message(body)

            usage = body.get("usage") or {}
            prompt_tokens = _usage_int(usage, "prompt_tokens")
            completion_tokens = _usage_int(usage, "completion_tokens")
            cached_tokens = _usage_int(
                usage.get("prompt_tokens_details") or {},
                "cached_tokens",
            )
            thinking_tokens = _usage_int(
                usage.get("completion_tokens_details") or {},
                "reasoning_tokens",
            )
            usage_cost_usd = _usage_cost_usd(usage)
            if usage_cost_usd is not None:
                cost_usd_total += usage_cost_usd
                cost_usd_seen = True
            totals["input_tokens"] += prompt_tokens
            totals["output_tokens"] += completion_tokens
            totals["cached_tokens"] += cached_tokens
            totals["thinking_tokens"] += thinking_tokens
            iterations_log.append(
                {
                    "iteration": iteration,
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "cached_tokens": cached_tokens,
                    "thinking_tokens": thinking_tokens,
                    "cost_usd": usage_cost_usd,
                }
            )

            actual_model = body.get("model")
            if (
                isinstance(actual_model, str)
                and actual_model
                and actual_model != OPENROUTER_DEFAULT_MODEL
            ):
                selected_model = actual_model
                target_payload = {"model": actual_model}
                reasoning = self._reasoning_payload(effort)
                if reasoning is not None:
                    target_payload["reasoning"] = reasoning

            tool_calls = message.get("tool_calls") or []
            final_text = _message_content_text(message)
            if not tool_calls:
                break

            messages.append(
                {
                    "role": "assistant",
                    "content": final_text,
                    "tool_calls": tool_calls,
                }
            )

            for idx, call in enumerate(tool_calls):
                name, args, result = _parse_tool_call(call)
                tool_call_count += 1
                tool_error: str | None = None
                executed_tool = False
                if result is None:
                    try:
                        result = executor.run(name, args or {})
                        executed_tool = True
                    except ToolExecutionError as e:
                        tool_error = str(e)
                        result = f"error: {e}"
                elif isinstance(result, str) and result.startswith("error:"):
                    tool_error = result.removeprefix("error:").strip()

                if tool_error is not None:
                    tool_errors.append(
                        {
                            "iteration": iteration,
                            "name": name,
                            "error": tool_error,
                        }
                    )
                elif executed_tool and name in {"Edit", "Write"}:
                    write_success_count += 1

                if executed_tool and name == "Bash" and isinstance(result, str):
                    bash_failure = _bash_failure(
                        args or {},
                        result,
                        iteration=iteration,
                    )
                    if bash_failure is not None:
                        bash_failures.append(bash_failure)
                        if _is_validation_command(str(bash_failure["command"])):
                            validation_failures.append(bash_failure)

                if session_log is not None:
                    session_log.emit(
                        "tool_call",
                        {
                            "provider": self.name,
                            "iteration": iteration,
                            "name": name,
                            "args": args,
                            "result_preview": (
                                result[:200] if isinstance(result, str) else result
                            ),
                        },
                    )

                tool_msg = {
                    "role": "tool",
                    "tool_call_id": call.get("id") or f"call_{iteration}_{idx}",
                    "name": name,
                    "content": result,
                }
                messages.append(tool_msg)
        else:
            hit_cap = True

        if hit_cap:
            final_text = (final_text or "(no content)") + (
                f"\n\n[conductor: OpenRouter tool-use loop hit max iterations "
                f"({OPENROUTER_MAX_TOOL_ITERATIONS}); model kept requesting tools. "
                "Re-run with a narrower task or a larger budget.]"
            )

        git_status_after = _git_clean_status(workdir) if repo_changing_task else None
        execution_status = _execution_status(
            repo_changing_task=repo_changing_task,
            tool_call_count=tool_call_count,
            write_success_count=write_success_count,
            tool_errors=tool_errors,
            bash_failures=bash_failures,
            validation_failures=validation_failures,
            hit_cap=hit_cap,
            git_status_before=git_status_before,
            git_status_after=git_status_after,
        )
        duration_ms = int((time.monotonic() - start) * 1000)
        execution_status["duration_ms"] = duration_ms
        failure_message = _execution_failure_message(execution_status)
        if failure_message is not None:
            raise ProviderExecutionError(
                f"OpenRouter code execution failed: {failure_message}",
                provider=self.name,
                status=execution_status,
            )

        return CallResponse(
            text=final_text,
            provider=self.name,
            model=final_body.get("model", selected_model) if final_body else selected_model,
            duration_ms=duration_ms,
            usage={
                "input_tokens": totals["input_tokens"],
                "output_tokens": totals["output_tokens"],
                "cached_tokens": totals["cached_tokens"],
                "thinking_tokens": totals["thinking_tokens"],
                "effort": effort if isinstance(effort, str) else None,
                "thinking_budget": thinking_budget,
                "tool_iterations": iteration,
                "tool_names": sorted(tools),
                "hit_iteration_cap": hit_cap,
                "tool_call_count": tool_call_count,
                "write_success_count": write_success_count,
                "tool_error_count": len(tool_errors),
                "bash_failure_count": len(bash_failures),
                "execution_status": execution_status,
                "iterations": iterations_log,
            },
            cost_usd=cost_usd_total if cost_usd_seen else None,
            raw=final_body,
        )


_CODE_TASK_TAGS = frozenset({"code", "coding"})
_WRITE_TOOLS = frozenset({"Edit", "Write"})


def _is_repo_changing_tool_task(
    task_tags: tuple[str, ...],
    tools: frozenset[str],
) -> bool:
    return bool(_CODE_TASK_TAGS.intersection(task_tags)) and bool(
        _WRITE_TOOLS.intersection(tools)
    )


def _git_clean_status(cwd: Path) -> dict[str, object]:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "status", "--porcelain", "--untracked-files=all"],
            capture_output=True,
            env=_scrub_git_env(),
            text=True,
            timeout=5,
            check=False,
        )
    except FileNotFoundError as err:
        return {"available": False, "clean": None, "error": f"git unavailable: {err}"}
    except subprocess.TimeoutExpired as err:
        return {
            "available": False,
            "clean": None,
            "error": f"git status timed out after {err.timeout}s",
        }

    if result.returncode != 0:
        error = (result.stderr or result.stdout or "").strip()
        return {
            "available": False,
            "clean": None,
            "error": error or f"git status exited {result.returncode}",
        }

    return {
        "available": True,
        "clean": not result.stdout.strip(),
        "porcelain": result.stdout,
    }


def _scrub_git_env() -> dict[str, str]:
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "GIT_DIR",
            "GIT_WORK_TREE",
            "GIT_INDEX_FILE",
            "GIT_COMMON_DIR",
            "GIT_OBJECT_DIRECTORY",
            "GIT_NAMESPACE",
        }
    }


def _bash_failure(
    args: dict,
    result: str,
    *,
    iteration: int,
) -> dict[str, object] | None:
    command = args.get("command")
    if not isinstance(command, str):
        command = ""

    first_line = result.splitlines()[0] if result else ""
    if first_line.startswith("exit="):
        raw_exit = first_line.removeprefix("exit=").strip()
        try:
            exit_code = int(raw_exit)
        except ValueError:
            exit_code = 0
        if exit_code == 0:
            return None
        return {
            "iteration": iteration,
            "command": command,
            "exit_code": exit_code,
            "result_preview": result[:500],
        }

    if result.startswith("TIMEOUT after"):
        return {
            "iteration": iteration,
            "command": command,
            "timeout": True,
            "result_preview": result[:500],
        }

    return None


def _is_validation_command(command: str) -> bool:
    normalized = command.strip().lower()
    if not normalized:
        return False
    validation_signals = (
        "pytest",
        "tox",
        "unittest",
        "npm test",
        "pnpm test",
        "yarn test",
        "bun test",
        "cargo test",
        "go test",
        "gradle test",
        "mvn test",
        "make test",
        "ruff",
        "mypy",
        "pyright",
        "tsc",
        "eslint",
        "prettier",
        "git diff --check",
    )
    return any(signal in normalized for signal in validation_signals)


def _execution_status(
    *,
    repo_changing_task: bool,
    tool_call_count: int,
    write_success_count: int,
    tool_errors: list[dict[str, object]],
    bash_failures: list[dict[str, object]],
    validation_failures: list[dict[str, object]],
    hit_cap: bool,
    git_status_before: dict[str, object] | None,
    git_status_after: dict[str, object] | None,
) -> dict[str, object]:
    state = "completed"
    after_clean = (
        git_status_after.get("clean") if git_status_after is not None else None
    )
    if hit_cap:
        state = "iteration-cap"
    elif repo_changing_task and validation_failures:
        state = "validation-failed"
    elif repo_changing_task and tool_errors and write_success_count == 0:
        state = "tool-error"
    elif repo_changing_task and (
        write_success_count == 0 or after_clean is True
    ):
        state = "no-op"

    return {
        "state": state,
        "repo_changing": repo_changing_task,
        "tool_calls": tool_call_count,
        "successful_write_tools": write_success_count,
        "tool_errors": tool_errors,
        "bash_failures": bash_failures,
        "validation_failures": validation_failures,
        "hit_iteration_cap": hit_cap,
        "git_status_before": git_status_before,
        "git_status_after": git_status_after,
    }


def _execution_failure_message(status: dict[str, object]) -> str | None:
    state = status.get("state")
    if state == "iteration-cap":
        return "tool-use loop hit max iterations before completing the code task"
    if state == "validation-failed":
        return "validation command failed after edits"
    if state == "tool-error":
        return "tool schema/execution errors occurred before any edit/write succeeded"
    if state == "no-op":
        return "repo-changing code task produced no net workspace changes"
    return None


def select_model_for_task(
    task_tags: list[str] | tuple[str, ...] | None,
    prefer: str,
    effort: str | int,
    exclude: set[str] | frozenset[str] | None = None,
) -> dict[str, object]:
    """Select an OpenRouter completion target.

    ``best`` and ``balanced`` intentionally leave ``openrouter/auto``
    unrestricted. The auto-router has a curated eligible pool that is narrower
    than ``/models``; restricting it with concrete catalog slugs can produce
    "No models match your request and model restrictions" even when every slug
    appears in the public catalog. ``cheapest`` and ``fastest`` still select a
    direct model from the catalog because those modes require local cost
    ordering before the request is sent.
    """
    if prefer not in {"best", "balanced", "cheapest", "fastest"}:
        raise ProviderError(
            f"OpenRouter selector got unsupported prefer={prefer!r}. "
            "Use best, balanced, cheapest, or fastest."
        )

    task_tag_set = set(task_tags or [])
    exclude_set = set(exclude or ())

    if prefer in {"best", "balanced"}:
        if "tool-use" in task_tag_set:
            coding_stack = tuple(
                model
                for model in openrouter_coding_stack(effort)
                if model not in exclude_set
            )
            if not coding_stack:
                raise ProviderError(
                    "OpenRouter coding stack was fully excluded. "
                    f"excluded models: {sorted(exclude_set)}"
                )
            return {
                "models": list(coding_stack),
                "reasoning": _reasoning_payload(effort),
            }
        return {
            "model": OPENROUTER_DEFAULT_MODEL,
            "reasoning": _reasoning_payload(effort),
        }

    catalog = openrouter_catalog.load_catalog(
        force_refresh=True,
        allow_stale_on_error=False,
    )
    catalog_ids = {entry.id for entry in catalog}
    candidates = []
    dropped = []
    for entry in catalog:
        if entry.id in exclude_set:
            continue
        if {"strong-reasoning", "thinking"} & task_tag_set and not entry.supports_thinking:
            continue
        if "tool-use" in task_tag_set and not entry.supports_tools:
            continue
        if "vision" in task_tag_set and not entry.supports_vision:
            continue
        if "long-context" in task_tag_set and entry.context_length < 100_000:
            continue
        if not _is_sendable_openrouter_model_id(entry.id, catalog_ids):
            dropped.append(entry.id)
            continue
        candidates.append(entry)

    if not candidates:
        raise ProviderError(
            _empty_selector_message(
                task_tags=task_tag_set,
                exclude=exclude_set,
                catalog=catalog,
                dropped=dropped,
            )
        )

    ranked = sorted(candidates, key=_catalog_cost_sort_key)
    return {"model": ranked[0].id, "reasoning": None}


def _catalog_cost_sort_key(entry: openrouter_catalog.ModelEntry) -> tuple[float, int, str]:
    return (entry.total_price_per_1k, -entry.created, entry.id)


def _is_sendable_openrouter_model_id(model_id: str, catalog_ids: set[str]) -> bool:
    """Return whether ``model_id`` is valid for direct OpenRouter requests.

    OpenRouter exposes ``~provider/family-latest`` pages as moving aliases. Those
    aliases are useful policy labels, but direct requests and request-level model
    restrictions need concrete catalog IDs. A request-time catalog refresh also
    drops slugs that no longer exist.
    """
    return model_id in catalog_ids and not model_id.startswith("~")


def _empty_selector_message(
    *,
    task_tags: set[str],
    exclude: set[str],
    catalog: list[openrouter_catalog.ModelEntry],
    dropped: list[str],
) -> str:
    catalog_ids = {model.id for model in catalog}
    sendable_examples = [
        entry.id
        for entry in catalog
        if _is_sendable_openrouter_model_id(entry.id, catalog_ids)
    ][:8]
    parts = [
        "OpenRouter selector found no sendable models after catalog validation.",
        f"tags filtered to empty: {sorted(task_tags)}",
        f"excluded models: {sorted(exclude)}",
        "configured provider: openrouter",
        f"catalog models available: {len(catalog)}",
    ]
    if sendable_examples:
        parts.append(f"available model examples: {sendable_examples}")
    if dropped:
        parts.append(f"dropped invalid aliases/stale slugs: {sorted(dropped)[:8]}")
    parts.append(
        "Broaden the request by removing tags such as --tags thinking,long-context, "
        "run `conductor models refresh`, or choose a concrete `--model` from "
        "`conductor models list`."
    )
    return " ".join(parts)


def _reasoning_payload(effort: str | int) -> dict[str, str] | None:
    if not isinstance(effort, str):
        return None
    mapped = _OPENROUTER_REASONING_EFFORTS.get(effort)
    if mapped is None:
        return None
    return {"effort": mapped}


def _log_selector_choice(
    *,
    task_tags: list[str] | tuple[str, ...] | None,
    prefer: str,
    payload: dict[str, object],
) -> None:
    tags_text = ",".join(task_tags or []) or "none"
    if "models" in payload:
        target = f"models={payload['models']}"
    elif payload["model"] == OPENROUTER_DEFAULT_MODEL:
        plugins = payload.get("plugins") or []
        shortlist = []
        if plugins and isinstance(plugins, list):
            first = plugins[0]
            if isinstance(first, dict):
                shortlist = list(first.get("allowed_models") or [])
        target = f"auto shortlist={shortlist}" if shortlist else "auto unrestricted"
    else:
        target = f"model={payload['model']}"
    sys.stderr.write(
        f"[conductor] openrouter selector: tags={tags_text} prefer={prefer} -> {target}\n"
    )
    sys.stderr.flush()


def _format_openrouter_http_error(status_code: int, response_text: str, payload: dict) -> str:
    attempted = _openrouter_attempted_models(payload)
    parts = [
        f"OpenRouter provider failed locally after upstream HTTP {status_code}.",
        f"request model: {payload.get('model') or payload.get('models')}",
    ]
    if attempted:
        parts.append(f"request restrictions/models tried: {attempted}")
    if _looks_like_model_restriction_error(response_text):
        parts.append(
            "OpenRouter rejected the request's model restrictions. "
            "For openrouter/auto, do not derive plugins[].allowed_models from "
            "`GET /models`: the auto-router uses a separate curated pool and "
            "catalog slugs can be unsendable there."
        )
        parts.append(
            "Copy-paste workaround: rerun without request-level auto-router "
            "restrictions, or choose a concrete model with "
            "`conductor call --with openrouter --model <model-id> ...`."
        )
    parts.append(f"upstream response: {response_text[:500]}")
    return " ".join(parts)


def _openrouter_attempted_models(payload: dict) -> list[str]:
    attempted: list[str] = []
    model = payload.get("model")
    if isinstance(model, str):
        attempted.append(model)
    models = payload.get("models")
    if isinstance(models, list):
        attempted.extend(str(item) for item in models)
    plugins = payload.get("plugins")
    if isinstance(plugins, list):
        for plugin in plugins:
            if not isinstance(plugin, dict):
                continue
            allowed = plugin.get("allowed_models")
            if isinstance(allowed, list):
                attempted.extend(str(item) for item in allowed)
    return attempted


def _looks_like_model_restriction_error(response_text: str) -> bool:
    lowered = response_text.lower()
    return "no models match your request and model restrictions" in lowered


def _first_message(body: dict) -> dict:
    try:
        message = body["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as e:
        raise ProviderHTTPError(
            f"OpenRouter response missing choices[0].message: {body!r:.500}"
        ) from e
    if not isinstance(message, dict):
        raise ProviderHTTPError(
            f"OpenRouter response choices[0].message was not an object: {message!r:.500}"
        )
    return message


def _message_content_text(message: dict) -> str:
    content = message.get("content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content)


def _usage_int(usage: dict, key: str) -> int:
    value = usage.get(key)
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _usage_cost_usd(usage: dict) -> float | None:
    value = usage.get("cost")
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_tool_call(call: dict) -> tuple[str, dict | None, str | None]:
    fn = call.get("function") or {}
    name = fn.get("name") or ""
    raw_args = fn.get("arguments")
    if isinstance(raw_args, dict):
        return name, raw_args, None
    if isinstance(raw_args, str):
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            return (
                name,
                None,
                f"error: tool `{name}` arguments were not valid JSON: {e}",
            )
        if not isinstance(args, dict):
            return (
                name,
                None,
                f"error: tool `{name}` arguments must decode to a JSON object.",
            )
        return name, args, None
    return name, {}, None
