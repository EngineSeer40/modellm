import asyncio
import json
import os
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from src.core.prompt import ANALYSIS, CONCEPTUAL_MODEL, DIMENSIONAL_MODEL

from .core.contracts import AnalysisPlan, AnalyticalPlan, DbtImplementation

T = TypeVar("T", bound=BaseModel)
DEFAULT_BEDROCK_MODEL_ID = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def configured_model_id() -> str:
    return os.environ.get("MODELLM_BEDROCK_MODEL_ID") or DEFAULT_BEDROCK_MODEL_ID


async def _invoke(
    prompt: str, schema: type[T], stage: str, emit: Callable, system_prompt: str
) -> T:
    from strands import Agent
    from strands.models import BedrockModel

    model_id = configured_model_id()
    model = BedrockModel(model_id=model_id, temperature=0.1)
    agent = Agent(model=model, system_prompt=system_prompt)
    started = time.monotonic()
    emit(stage, "model_call", f"Calling {model_id}")
    final = None
    for_event_tool = set()
    async for event in agent.stream_async(prompt, structured_output_model=schema):
        if "current_tool_use" in event:
            tool = event["current_tool_use"]
            name = (
                tool.get("name", "structured output")
                if isinstance(tool, dict)
                else str(tool)
            )
            if name not in for_event_tool:
                emit(stage, "tool_call", name)
                for_event_tool.add(name)
        if "result" in event:
            final = event["result"]
    if final is None or final.structured_output is None:
        raise RuntimeError(f"{stage} returned no structured output")
    usage = {}
    try:
        usage = final.metrics.get_summary()
    except (AttributeError, TypeError):
        pass
    emit(
        stage,
        "model_call",
        f"{model_id} completed in {time.monotonic() - started:.1f}s",
        usage,
    )
    return final.structured_output


def invoke(
    prompt: str, schema: type[T], stage: str, emit: Callable, system_prompt: str
) -> T:
    return asyncio.run(_invoke(prompt, schema, stage, emit, system_prompt))


def conceptual(
    request: str,
    profiles: dict,
    relationships: list[dict],
    emit: Callable,
    feedback: list[str] | None = None,
) -> AnalyticalPlan:
    prompt = json.dumps(
        {
            "request": request,
            "profiles": profiles,
            "candidate_relationships": relationships,
            "previous_plan_issues": feedback or [],
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
) -> DbtImplementation:
    prompt = json.dumps(
        {
            "plan": plan.model_dump(),
            "profiles": profiles,
            "relationships": relationships,
            "previous_failure": failure,
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
) -> AnalysisPlan:
    prompt = json.dumps(
        {
            "request": request,
            "analytical_plan": plan.model_dump(),
            "validated_schemas": model_schemas,
            "model_summaries": summaries,
        },
        default=str,
    )
    return invoke(prompt, AnalysisPlan, "analytical interpretation", emit, ANALYSIS)
