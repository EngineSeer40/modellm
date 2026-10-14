import asyncio
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from src.core.prompt import ANALYSIS, ANALYSIS_FINDING, CONCEPTUAL_MODEL, DIMENSIONAL_MODEL, PLOTTING
from src.tools.analysis_tools import DuckDBAnalysisTools
from src.tools.result_tools import DuckDBResultTools

from .core.contracts import (
    AcceptedAnalysisResult,
    AnalysisFinding,
    AnalysisPlan,
    AnalysisSpec,
    AnalyticalPlan,
    DbtImplementation,
    PlotSpec,
    ResultFilter,
    ValidationIssue,
)
from .analysis import plot_capabilities

T = TypeVar("T", bound=BaseModel)
DEFAULT_BEDROCK_MODEL_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"
PLOTTING_TRIAL_MODEL_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def configured_model_id(stage: str | None = None) -> str:
    if stage == "visualisation generation":
        return PLOTTING_TRIAL_MODEL_ID
    return os.environ.get("MODELLM_BEDROCK_MODEL_ID") or DEFAULT_BEDROCK_MODEL_ID


async def _invoke(
    prompt: str | list[dict],
    schema: type[T],
    stage: str,
    emit: Callable,
    system_prompt: str,
    tools: list | None = None,
) -> T:
    from botocore.config import Config
    from strands import Agent
    from strands.models import BedrockModel, CacheConfig
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    model_id = configured_model_id(stage)
    nova_reasoning = "amazon.nova-2-lite" in model_id
    cache_enabled = stage in {
        "analytical interpretation", "generating dbt models",
        "analytical finding", "visualisation generation",
    } and (
        "anthropic" in model_id.lower() or "claude" in model_id.lower()
    )
    if cache_enabled and isinstance(prompt, list):
        # dbt keeps stable modelling evidence before changing repair feedback.
        prompt = [prompt[0], {"cachePoint": {"type": "default"}}, *prompt[1:]]
    model = BedrockModel(
        model_id=model_id,
        temperature=0.1,
        boto_client_config=Config(read_timeout=3600),
        **({"cache_config": CacheConfig(strategy="auto", tools_ttl=True)} if cache_enabled else {}),
        **({"additional_request_fields": {
            "reasoningConfig": {"type": "enabled", "maxReasoningEffort": "low"},
        }} if nova_reasoning else {}),
    )
    agent = Agent(model=model, system_prompt=system_prompt, tools=tools or [])
    started = time.monotonic()
    call_id = uuid.uuid4().hex[:12]
    emit(stage, "model_call", f"Calling {model_id}", {
        "call_id": call_id, "model_id": model_id, "status": "started",
        "region": model.client.meta.region_name,
        "read_timeout_seconds": 3600,
        "reasoning_effort": "low" if nova_reasoning else None,
        "prompt_cache": {"enabled": cache_enabled, "ttl": "5m" if cache_enabled else None},
    })
    if nova_reasoning:
        emit(stage, "reasoning_status", "Extended reasoning enabled (low); Nova redacts its reasoning text",
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
        async for event in agent.stream_async(prompt, structured_output_model=schema):
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
        {"call_id": call_id, "model_id": model_id, "status": "completed", **usage},
    )
    return final.structured_output


def invoke(
    prompt: str | list[dict],
    schema: type[T],
    stage: str,
    emit: Callable,
    system_prompt: str,
    tools: list | None = None,
) -> T:
    return asyncio.run(_invoke(prompt, schema, stage, emit, system_prompt, tools))


def conceptual(
    request: str,
    profiles: dict,
    relationships: list[dict],
    emit: Callable,
    feedback: list[ValidationIssue] | None = None,
    previous: AnalyticalPlan | None = None,
) -> AnalyticalPlan:
    prompt = json.dumps(
        {
            "request": request,
            "profiles": profiles,
            "candidate_relationships": relationships,
            "previous_plan_issues": [issue.model_dump() for issue in (feedback or [])],
            "previous_plan": previous.model_dump() if previous else None,
        },
        default=str,
    )
    return invoke(
        prompt, AnalyticalPlan, "conceptual modelling", emit, CONCEPTUAL_MODEL
    )


def implement(
    plan: AnalyticalPlan,
    profiles: dict,
    relationships: list[dict],
    emit: Callable,
    failure: str = "",
    previous: DbtImplementation | None = None,
) -> DbtImplementation:
    prompt = [
        {"text": json.dumps({
            "plan": plan.model_dump(), "profiles": profiles, "relationships": relationships,
        }, default=str)},
        {"text": json.dumps({
            "previous_failure": failure,
            "previous_implementation": previous.model_dump() if previous else None,
        }, default=str)},
    ]
    return invoke(
        prompt, DbtImplementation, "generating dbt models", emit, DIMENSIONAL_MODEL
    )


def analyse(
    request: str,
    plan: AnalyticalPlan,
    model_schemas: dict,
    summaries: dict,
    emit: Callable,
    database: Path,
    feedback: str = "",
    previous: AnalysisPlan | None = None,
) -> AnalysisPlan:

    db = DuckDBAnalysisTools(database, list(model_schemas))

    prompt = json.dumps(
        {
            "request": request,
            "analytical_plan": plan.model_dump(),
            "validated_schemas": model_schemas,
            "model_summaries": summaries,
            "previous_analysis": previous.model_dump() if previous else None,
            "previous_acceptance_error": feedback,
        },
        default=str,
    )
    result = invoke(
        prompt,
        AnalysisPlan,
        "analytical interpretation",
        emit,
        ANALYSIS,
        tools=[
            db.list_models,
            db.describe_model,
            db.query_data,
        ],
    )

    return result


def interpret(
    spec: AnalysisSpec,
    result: AcceptedAnalysisResult,
    emit: Callable,
    db: DuckDBResultTools,
    feedback: str = "",
    previous: AnalysisFinding | None = None,
) -> AnalysisFinding:
    prompt = json.dumps(
        {
            "title": spec.title,
            "question": spec.question,
            "concept_id": spec.concept_id,
            "rationale": spec.rationale,
            "population_definition": spec.population_definition,
            "observation_rule": spec.observation_rule,
            "grain": spec.grain,
            "dimensions": [item.model_dump() for item in spec.dimensions],
            "measures": [item.model_dump() for item in spec.measures],
            "variant_field": spec.variant_field,
            "result": result.model_dump(),
            "previous_finding_error": feedback,
            "previous_finding": previous.model_dump() if previous else None,
            "registered_evidence": db.registry.snapshot(),
        },
        default=str,
    )
    return invoke(
        prompt,
        AnalysisFinding,
        "analytical finding",
        emit,
        ANALYSIS_FINDING,
        tools=[db.inspect_result],
    )


def plot(
    spec: AnalysisSpec,
    result: AcceptedAnalysisResult,
    finding: dict,
    emit: Callable,
    database: Path,
    selection: list[ResultFilter] | None = None,
) -> PlotSpec:

    db = DuckDBResultTools(
        database=database,
        relation=result.relation,
        columns=set(result.columns),
    )

    prompt = json.dumps(
        {
            "title": spec.title,
            "question": spec.question,
            "finding": finding,
            "concept_id": spec.concept_id,
            "variant_field": spec.variant_field,
            "presentation_selection": [item.model_dump() for item in (selection or [])],
            "visualisation_goal": spec.visualisation_goal,
            "renderer_capabilities": plot_capabilities(),
            "analytical_result": {
                "grain": spec.grain,
                "dimensions": [item.model_dump() for item in spec.dimensions],
                "measures": [item.model_dump() for item in spec.measures],
                "row_count": result.row_count,
                "columns": result.columns,
                "profile": [item.model_dump() for item in result.profile],
            },
        },
        default=str,
    )

    return invoke(
        prompt,
        PlotSpec,
        "visualisation generation",
        emit,
        PLOTTING,
        tools=[
            db.category_counts,
        ],
    )
