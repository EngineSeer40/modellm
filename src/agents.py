import asyncio
import json
import os
import time
from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from .contracts import AnalysisPlan, AnalyticalPlan, DbtImplementation

T = TypeVar("T", bound=BaseModel)


async def _invoke(prompt: str, schema: type[T], stage: str, emit: Callable, system_prompt: str) -> T:
    from strands import Agent
    from strands.models import BedrockModel

    model_id = os.environ.get("MODELLM_BEDROCK_MODEL_ID", "global.anthropic.claude-sonnet-4-6")
    model = BedrockModel(model_id=model_id, temperature=0.1)
    agent = Agent(model=model, system_prompt=system_prompt)
    started = time.monotonic()
    final = None
    for_event_tool = set()
    async for event in agent.stream_async(prompt, structured_output_model=schema):
        if "current_tool_use" in event:
            tool = event["current_tool_use"]
            name = tool.get("name", "structured output") if isinstance(tool, dict) else str(tool)
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
    emit(stage, "model_call", f"{model_id}; {time.monotonic() - started:.1f}s", usage)
    return final.structured_output


def invoke(prompt: str, schema: type[T], stage: str, emit: Callable, system_prompt: str) -> T:
    return asyncio.run(_invoke(prompt, schema, stage, emit, system_prompt))


CONCEPTUAL_RULES = """You are a Kimball dimensional modeller. Return the requested structured AnalyticalPlan. Identify business processes, declare the lowest supported atomic fact grain, then choose measures valid at that grain and descriptive dimensions. Keep distinct grains in separate facts; reuse conformed dimensions. Infer keys/relationships from evidence and record assumptions or unresolved semantics. Do not invent business meaning. Source expressions are short field references or simple arithmetic, not SQL transformation programs. Use simple snake_case model names."""

DBT_RULES = """Implement the supplied AnalyticalPlan as a small dbt-duckdb project. Return model .sql files and schema.yml only, relative to models/. Use {{ source('raw', 'dataset') }} and {{ ref('model') }}. Preserve declared atomic grains. Include not_null, unique and relationship tests where justified; handle known orphan keys deliberately. Do not use macros, packages, Python models, external files or arbitrary commands. SQL must be DuckDB compatible. Do not redefine sources.yml; the application writes it."""

ANALYSIS_RULES = """You are a BI analyst. Return up to four useful views that answer the original request from validated dimensional tables only. Each view must choose an existing table and existing x/y columns; aggregation is applied to y. Use count_distinct only for identifiers. Findings must be cautious and grounded in the provided preview/statistics. Do not request raw tables or invent values. The application executes all queries deterministically."""


def conceptual(request: str, profiles: dict, relationships: list[dict], emit: Callable) -> AnalyticalPlan:
    prompt = json.dumps({"request": request, "profiles": profiles, "candidate_relationships": relationships}, default=str)
    return invoke(prompt, AnalyticalPlan, "conceptual modelling", emit, CONCEPTUAL_RULES)


def implement(plan: AnalyticalPlan, profiles: dict, relationships: list[dict], emit: Callable, failure: str = "") -> DbtImplementation:
    prompt = json.dumps({"plan": plan.model_dump(), "profiles": profiles, "relationships": relationships, "previous_failure": failure}, default=str)
    return invoke(prompt, DbtImplementation, "generating dbt models", emit, DBT_RULES)


def analyse(request: str, plan: AnalyticalPlan, model_schemas: dict, summaries: dict, emit: Callable) -> AnalysisPlan:
    prompt = json.dumps({"request": request, "analytical_plan": plan.model_dump(), "validated_schemas": model_schemas, "model_summaries": summaries}, default=str)
    return invoke(prompt, AnalysisPlan, "analytical interpretation", emit, ANALYSIS_RULES)
