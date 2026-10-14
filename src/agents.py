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


def configured_model_id() -> str:
    return os.environ.get("MODELLM_BEDROCK_MODEL_ID") or DEFAULT_BEDROCK_MODEL_ID


async def _invoke(
    prompt: str,
    schema: type[T],
    stage: str,
    emit: Callable,
    system_prompt: str,
    tools: list | None = None,
) -> T:
    from strands import Agent
    from strands.models import BedrockModel
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool

    model_id = configured_model_id()
    model = BedrockModel(model_id=model_id, temperature=0.1)
    agent = Agent(model=model, system_prompt=system_prompt, tools=tools or [])
    started = time.monotonic()
    call_id = uuid.uuid4().hex[:12]
    emit(stage, "model_call", f"Calling {model_id}", {"call_id": call_id, "status": "started"})
    emit(stage, "contract_schema", "Recorded the output contract delivered to the model",
         {"call_id": call_id, "tool_spec": StructuredOutputTool(schema).tool_spec,
          "system_prompt": system_prompt})
    final = None
    seen_tools = set()
    try:
        async for event in agent.stream_async(prompt, structured_output_model=schema):
            if "current_tool_use" in event:
                current = event["current_tool_use"]
                if isinstance(current, dict):
                    tool_id = current.get("toolUseId", current.get("name"))
                    if tool_id not in seen_tools:
                        seen_tools.add(tool_id)
                        emit(stage, "tool_call", current.get("name", "structured output"),
                             {"call_id": call_id, "tool_id": tool_id})
            if event.get("message"):
                emit(stage, "model_message", "Agent message recorded",
                     {"call_id": call_id, "message": event["message"]})
            if event.get("start_event_loop"):
                emit(stage, "model_cycle", "Internal model cycle", {"call_id": call_id})
            if "result" in event:
                final = event["result"]
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        category = "transport" if any(word in error.lower() for word in ("timeout", "timed out", "connection", "throttl")) else "invocation"
        emit(stage, "model_failure", error,
             {"call_id": call_id, "category": category, "elapsed_seconds": time.monotonic() - started,
              "status": "failed", "tool_calls": len(seen_tools)})
        raise
    if final is None or final.structured_output is None:
        raise RuntimeError(f"{stage} returned no structured output")
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
        {"call_id": call_id, "status": "completed", **usage},
    )
    return final.structured_output


def invoke(
    prompt: str,
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
    prompt = json.dumps(
        {
            "plan": plan.model_dump(),
            "profiles": profiles,
            "relationships": relationships,
            "previous_failure": failure,
            "previous_implementation": previous.model_dump() if previous else None,
        },
        default=str,
    )
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
            "variant_id": spec.variant_id,
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
            "variant_id": spec.variant_id,
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
