import asyncio
import json
import time
import uuid
from collections.abc import Callable
from importlib.metadata import version
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from src.core.prompt import ANALYSIS, ANALYSIS_FINDING, CONCEPTUAL_MODEL, DIMENSIONAL_MODEL, PLOTTING
from src.core.prompt.analysis import ANALYSIS_PREVIEW_GUIDANCE
from src.core.prompt.conceptual import CONCEPTUAL_REFERENCE_GUIDANCE
from src.tools.analysis_tools import DuckDBAnalysisTools
from src.tools.result_tools import DuckDBResultTools

from .core.contracts import (
    AcceptedAnalysisResult,
    AnalysisFinding,
    AnalysisPlan,
    AnalysisSpec,
    AnalyticalPlan,
    DbtImplementation,
    EarlyValidatedAnalysisFinding,
    PlotSpec,
    ResultFilter,
    ValidationIssue,
    registered_plot_templates,
)
from .analysis import plot_capabilities
from .config import current_settings
from .execution import current_budget
from .context_cost import (
    COST_EXPERIMENT_STAGES, configured_cost_experiments,
    split_stable_and_repair_context, tool_result_offload_plugins,
)

T = TypeVar("T", bound=BaseModel)


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
    context_directory: Path | None = None,
) -> T:
    from botocore.config import Config
    from strands import Agent
    from strands.models import BedrockModel, CacheConfig
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    experiments = configured_cost_experiments(cost_experiments)
    model_id = configured_model_id(stage)
    sonnet_55 = "anthropic.claude-sonnet-5-5" in model_id
    sonnet_5 = model_id.split("anthropic.")[-1] in {"claude-sonnet-5", "claude-sonnet-5-5"}
    nova_reasoning = "amazon.nova-2-lite" in model_id
    cache_enabled = (stage in {
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
    agent = Agent(model=model, system_prompt=system_prompt, tools=tools or [], callback_handler=None,
                  **({"hooks": [budget.hooks(model_id, settings.region)]} if budget else {}),
                  **({"plugins": plugins} if plugins else {}))
    call_context = {
        "cost_experiments": sorted(name for name in experiments if COST_EXPERIMENT_STAGES[name] == stage),
        "strands_version": version("strands-agents"),
        "context_directory": str(offload_directory) if plugins else None,
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
    context_directory: Path | None = None,
) -> T:
    return asyncio.run(_invoke(prompt, schema, stage, emit, system_prompt, tools,
                              cost_experiments=cost_experiments, context_directory=context_directory))


def conceptual(
    request: str,
    profiles: dict,
    relationships: list[dict],
    emit: Callable,
    feedback: list[ValidationIssue] | None = None,
    previous: AnalyticalPlan | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
) -> AnalyticalPlan:
    experiments = configured_cost_experiments(cost_experiments)
    stable = {
        "request": request,
        "profiles": profiles,
        "candidate_relationships": relationships,
    }
    repair = {
        "previous_plan_issues": [issue.model_dump() for issue in (feedback or [])],
        "previous_plan": previous.model_dump() if previous else None,
    }
    prompt = (split_stable_and_repair_context(stable, repair) if "conceptual_cache" in experiments
              else json.dumps({**stable, **repair}, default=str))
    system_prompt = CONCEPTUAL_MODEL + (CONCEPTUAL_REFERENCE_GUIDANCE if "conceptual_guidance" in experiments else "")
    return invoke(
        prompt, AnalyticalPlan, "conceptual modelling", emit, system_prompt, cost_experiments=experiments,
    )


def implement(
    plan: AnalyticalPlan,
    profiles: dict,
    relationships: list[dict],
    emit: Callable,
    failure: str = "",
    previous: DbtImplementation | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
) -> DbtImplementation:
    prompt = split_stable_and_repair_context(
        {
            "plan": plan.model_dump(), "profiles": profiles, "relationships": relationships,
        },
        {
            "previous_failure": failure,
            "previous_implementation": previous.model_dump() if previous else None,
        },
    )
    return invoke(
        prompt, DbtImplementation, "generating dbt models", emit, DIMENSIONAL_MODEL,
        cost_experiments=cost_experiments,
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
    *,
    cost_experiments: frozenset[str] | None = None,
) -> AnalysisPlan:
    experiments = configured_cost_experiments(cost_experiments)
    db = DuckDBAnalysisTools(database, list(model_schemas))
    small_previews = "small_analysis_previews" in experiments

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
        ANALYSIS + (ANALYSIS_PREVIEW_GUIDANCE if small_previews else ""),
        tools=[
            db.list_models,
            db.describe_model,
            db.query_data_small_preview if small_previews else db.query_data,
        ],
        cost_experiments=experiments, context_directory=database.parent / "context",
    )

    return result


def interpret(
    spec: AnalysisSpec,
    result: AcceptedAnalysisResult,
    emit: Callable,
    db: DuckDBResultTools,
    feedback: str = "",
    previous: AnalysisFinding | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
) -> AnalysisFinding:
    experiments = configured_cost_experiments(cost_experiments)
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
        EarlyValidatedAnalysisFinding if "early_finding_validation" in experiments else AnalysisFinding,
        "analytical finding",
        emit,
        ANALYSIS_FINDING,
        tools=[db.inspect_result],
        cost_experiments=experiments, context_directory=db.database.parent / "context",
    )


def plot(
    spec: AnalysisSpec,
    result: AcceptedAnalysisResult,
    finding: dict,
    emit: Callable,
    database: Path,
    selection: list[ResultFilter] | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
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
            "renderer_templates": registered_plot_templates(),
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
        cost_experiments=cost_experiments,
    )
