"""Configured Strands invocation, preserving SDK conversations, caching and budgets."""

import asyncio
import time
import uuid
from collections.abc import Callable, Mapping
from importlib.metadata import version
from pathlib import Path
from typing import Literal, TypeVar

from opentelemetry.util.types import AttributeValue

from pydantic import BaseModel

from .config import current_settings
from .execution import current_budget
from .context_cost import COST_EXPERIMENT_STAGES, configured_cost_experiments, tool_result_offload_plugins

T = TypeVar("T", bound=BaseModel)


class InvocationLimitError(RuntimeError):
    """The SDK exhausted a local invocation limit, not the shared run budget."""


def configured_model_id(stage: str | None = None) -> str:
    settings = current_settings()
    return settings.plotting_model_id if stage == "visualisation generation" else settings.model_id


def automatic_tool_choice_model(model_class):
    """Adapt the public SDK stream API for models rejecting forced tool choice.

    Preserve SDK structured-output retries/validators and all message/cache blocks.
    Only the transport's tool choice changes, including the SDK's end-turn fallback.
    """
    class AutomaticToolChoiceModel(model_class):
        async def stream(self, *args, tool_choice=None, **kwargs):
            async for event in super().stream(*args, tool_choice={"auto": {}}, **kwargs):
                yield event
    return AutomaticToolChoiceModel


async def _invoke(
    prompt: str | list[dict],
    schema: type[T],
    stage: str,
    emit: Callable,
    system_prompt: str,
    tools: list | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
    trace_attributes: Mapping[str, AttributeValue] | None = None,
    context_directory: Path | None = None,
    limits: dict[str, int] | None = None,
    cache_policy: Literal["default", "disabled"] = "default",
) -> T:
    if cache_policy not in {"default", "disabled"}:
        raise ValueError(f"Unknown cache policy: {cache_policy}")
    def explicit_checkpoint(value):
        if isinstance(value, dict):
            return "cachePoint" in value or any(explicit_checkpoint(item) for item in value.values())
        return isinstance(value, list) and any(explicit_checkpoint(item) for item in value)

    if cache_policy == "disabled" and explicit_checkpoint(prompt):
        raise ValueError("Disabled cache policy cannot receive explicit cache checkpoints")
    from botocore.config import Config
    from strands import Agent
    from strands.models import BedrockModel, CacheConfig
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    experiments = configured_cost_experiments(cost_experiments)
    model_id = configured_model_id(stage)
    sonnet_55 = "anthropic.claude-sonnet-5-5" in model_id
    sonnet_5 = model_id.split("anthropic.")[-1] in {"claude-sonnet-5", "claude-sonnet-5-5"}
    nova_reasoning = "amazon.nova-2-lite" in model_id
    cache_enabled = cache_policy != "disabled" and (stage in {
        "analytical interpretation", "generating dbt models",
        "analytical finding", "visualisation generation",
    } or stage == "conceptual modelling" and "conceptual_cache" in experiments) and (
        "anthropic" in model_id.lower() or "claude" in model_id.lower()
    )
    if cache_enabled and isinstance(prompt, list):
        # Keep stable modelling evidence before changing repair feedback.
        prompt = [prompt[0], {"cachePoint": {"type": "default"}}, *prompt[1:]]
    settings = current_settings()
    budget = current_budget()
    remaining = budget.remaining_seconds() if budget else None
    read_seconds = min(settings.read_seconds, max(1, int(remaining))) if remaining else settings.read_seconds
    model_class = automatic_tool_choice_model(BedrockModel) if sonnet_55 else BedrockModel
    model = model_class(
        model_id=model_id,
        # Sonnet 5 and 5.5 reject non-default sampling; retain default adaptive thinking.
        **({} if sonnet_5 else {"temperature": 0.1}),
        boto_client_config=Config(read_timeout=read_seconds, connect_timeout=10,
                                  retries={"total_max_attempts": 1} if settings.prod else None),
        region_name=settings.region,
        use_native_token_count=False,
        **({"max_tokens": settings.max_output_tokens} if settings.max_output_tokens else {}),
        **({"cache_config": CacheConfig(strategy="auto", tools_ttl=True)} if cache_enabled else {}),
        **({"additional_request_fields": {
            "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"},
        }} if nova_reasoning else {}),
    )
    started = time.monotonic()
    call_id = uuid.uuid4().hex[:12]
    offload_directory = context_directory / call_id if context_directory else None
    plugins = tool_result_offload_plugins(stage, experiments, offload_directory)
    agent = Agent(name=stage, trace_attributes={**(trace_attributes or {}),
                  "modellm.call_id": call_id, "modellm.stage": stage}, model=model, system_prompt=system_prompt, tools=tools or [], callback_handler=None,
                  **({"hooks": [budget.hooks(model_id, settings.region,
                                            allow_unknown_usage=stage == "visualisation generation")]} if budget else {}),
                  **({"plugins": plugins} if plugins else {}))
    call_context = {
        "cost_experiments": sorted(name for name in experiments if COST_EXPERIMENT_STAGES[name] == stage),
        "strands_version": version("strands-agents"),
        "context_directory": str(offload_directory) if plugins else None,
        "limits": limits,
        "cache_policy": cache_policy,
    }
    emit(stage, "model_call", f"Calling {model_id}", {
        "call_id": call_id, "model_id": model_id, "status": "started",
        "region": model.client.meta.region_name,
        "read_timeout_seconds": read_seconds,
        "reasoning_effort": "low" if nova_reasoning else "high (provider default)" if sonnet_5 else None,
        "prompt_cache": {"enabled": cache_enabled, "ttl": "5m" if cache_enabled else None},
        **call_context,
    })
    if nova_reasoning:
        emit(stage, "reasoning_status", "Extended reasoning enabled (low); Nova redacts its reasoning text",
             {"call_id": call_id})
    if sonnet_5:
        emit(stage, "reasoning_status", "Sonnet 5/5.5 uses provider-default adaptive thinking; thinking tokens count toward output and cost",
             {"call_id": call_id})
    emit(stage, "contract_schema", "Recorded the output contract delivered to the model",
         {"call_id": call_id, "tool_spec": StructuredOutputTool(schema).tool_spec,
          "system_prompt": system_prompt})
    final = None
    seen_tools = set()
    streamed = {"reasoning": "", "model_text": ""}
    last_flush = time.monotonic()
    reasoning_seen = False

    def flush_stream():
        nonlocal last_flush
        for kind, content in streamed.items():
            if content:
                emit(stage, kind, content, {"call_id": call_id})
                streamed[kind] = ""
        last_flush = time.monotonic()

    try:
        async for event in agent.stream_async(prompt, structured_output_model=schema,
                                            **({"limits": limits} if limits is not None else {})):
            if event.get("reasoningText"):
                streamed["reasoning"] += event["reasoningText"]
                reasoning_seen = True
            elif event.get("reasoningRedactedContent") is not None:
                if not reasoning_seen:
                    emit(stage, "reasoning_status", "Provider returned redacted reasoning",
                         {"call_id": call_id})
                reasoning_seen = True
            if event.get("data"):
                streamed["model_text"] += event["data"]
            if time.monotonic() - last_flush >= 0.5:
                flush_stream()
            if "current_tool_use" in event:
                current = event["current_tool_use"]
                if isinstance(current, dict):
                    tool_id = current.get("toolUseId", current.get("name"))
                    if tool_id not in seen_tools:
                        seen_tools.add(tool_id)
                        emit(stage, "tool_call", current.get("name", "structured output"),
                             {"call_id": call_id, "tool_id": tool_id})
            if event.get("message"):
                flush_stream()
                # Some providers supply reasoning only in the completed message.
                for block in event["message"].get("content", []):
                    reasoning = block.get("reasoningContent")
                    if reasoning and not reasoning_seen:
                        content = reasoning.get("reasoningText", {}).get("text")
                        emit(stage, "reasoning" if content else "reasoning_status",
                             content or "Provider returned redacted reasoning", {"call_id": call_id})
                reasoning_seen = False
                emit(stage, "model_message", "Agent message recorded",
                     {"call_id": call_id, "message": event["message"]})
                usage = event["message"].get("metadata", {}).get("usage", {})
                if cache_enabled and usage:
                    reads = usage.get("cacheReadInputTokens", 0)
                    writes = usage.get("cacheWriteInputTokens", 0)
                    emit(stage, "cache_usage", f"Prompt cache: {reads:,} tokens read, {writes:,} written", {
                        "call_id": call_id, "cache_read_tokens": reads, "cache_write_tokens": writes,
                        "uncached_input_tokens": usage.get("inputTokens", 0),
                    })
            if event.get("start_event_loop"):
                emit(stage, "model_cycle", "Internal model cycle", {"call_id": call_id})
            if "result" in event:
                final = event["result"]
    except Exception as exc:
        flush_stream()
        error = f"{type(exc).__name__}: {exc}"
        category = "transport" if any(word in error.lower() for word in ("timeout", "timed out", "connection", "throttl")) else "invocation"
        emit(stage, "model_failure", error,
             {"call_id": call_id, "model_id": model_id, "category": category, "elapsed_seconds": time.monotonic() - started,
              "status": "failed", "tool_calls": len(seen_tools)})
        raise
    flush_stream()
    if final is not None and str(getattr(final, "stop_reason", "")).startswith("limit_"):
        error = f"{stage} exhausted its local invocation limit: {final.stop_reason} ({limits})"
        emit(stage, "model_failure", error, {
            "call_id": call_id, "model_id": model_id, "category": "invocation_limit",
            "status": "failed", "limits": limits,
            "elapsed_seconds": time.monotonic() - started, "tool_calls": len(seen_tools),
        })
        raise InvocationLimitError(error)
    if final is None or final.structured_output is None:
        error = f"{stage} returned no structured output"
        emit(stage, "model_failure", error, {
            "call_id": call_id, "model_id": model_id,
            "category": "structured_output", "status": "failed",
            "elapsed_seconds": time.monotonic() - started, "tool_calls": len(seen_tools),
        })
        raise RuntimeError(error)
    usage = {}
    try:
        summary = final.metrics.get_summary()
        usage = {key: summary[key] for key in ("total_cycles", "total_duration", "accumulated_usage", "accumulated_metrics") if key in summary}
    except (AttributeError, TypeError):
        pass
    emit(
        stage,
        "model_call",
        f"{model_id} completed in {time.monotonic() - started:.1f}s",
        {"call_id": call_id, "model_id": model_id, "status": "completed", **call_context, **usage},
    )
    return final.structured_output


def invoke(
    prompt: str | list[dict],
    schema: type[T],
    stage: str,
    emit: Callable,
    system_prompt: str,
    tools: list | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
    trace_attributes: Mapping[str, AttributeValue] | None = None,
    context_directory: Path | None = None,
    limits: dict[str, int] | None = None,
    cache_policy: Literal["default", "disabled"] = "default",
) -> T:
    return asyncio.run(_invoke(prompt, schema, stage, emit, system_prompt, tools,
                              cost_experiments=cost_experiments, context_directory=context_directory,
                              trace_attributes=trace_attributes, limits=limits, cache_policy=cache_policy))
