import json
from collections.abc import Callable, Mapping
from pathlib import Path

import pandas as pd
from opentelemetry.util.types import AttributeValue

from src.core.prompt import ANALYSIS, CONCEPTUAL_MODEL, DIMENSIONAL_MODEL, PLOTTING
from src.core.prompt.analysis import ANALYSIS_PREVIEW_GUIDANCE
from src.core.prompt.conceptual import CONCEPTUAL_REFERENCE_GUIDANCE
from src.tools.analysis_tools import DuckDBAnalysisTools
from src.tools.result_tools import DuckDBResultTools

from .agent_runtime import invoke
from .config import current_settings
from .context_cost import (
    configured_cost_experiments,
    split_stable_and_repair_context,
)
from .core.contracts import (
    AcceptedAnalysisResult,
    AnalysisFinding,
    AnalysisSpec,
    AnalyticalPlan,
    ConceptualPlan,
    DimensionalPlan,
    EarlyValidatedAnalysisFinding,
    PlottingPlan,
    ResultFilter,
    ValidationIssue,
    registered_plot_templates,
)
from .visualisation import (
    accepted_result_plot_schema,
    plot_capabilities,
    plot_descriptions,
)


def conceptual(
    request: str,
    profiles: dict,
    relationships: list[dict],
    emit: Callable,
    feedback: list[ValidationIssue] | None = None,
    previous: ConceptualPlan | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
    trace_attributes: Mapping[str, AttributeValue] | None = None,
) -> ConceptualPlan:
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
    prompt = (
        split_stable_and_repair_context(stable, repair)
        if "conceptual_cache" in experiments
        else json.dumps({**stable, **repair}, default=str)
    )
    system_prompt = CONCEPTUAL_MODEL + (
        CONCEPTUAL_REFERENCE_GUIDANCE if "conceptual_guidance" in experiments else ""
    )
    return invoke(
        prompt,
        ConceptualPlan,
        "conceptual modelling",
        emit,
        system_prompt,
        cost_experiments=experiments,
        trace_attributes=trace_attributes,
    )


def implement(
    plan: ConceptualPlan,
    profiles: dict,
    relationships: list[dict],
    emit: Callable,
    failure: str = "",
    previous: DimensionalPlan | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
    trace_attributes: Mapping[str, AttributeValue] | None = None,
) -> DimensionalPlan:
    prompt = split_stable_and_repair_context(
        {
            "plan": plan.model_dump(),
            "profiles": profiles,
            "relationships": relationships,
        },
        {
            "previous_failure": failure,
            "previous_implementation": previous.model_dump() if previous else None,
        },
    )
    return invoke(
        prompt,
        DimensionalPlan,
        "generating dbt models",
        emit,
        DIMENSIONAL_MODEL,
        cost_experiments=cost_experiments,
        trace_attributes=trace_attributes,
    )


def analyse(
    request: str,
    plan: ConceptualPlan,
    model_schemas: dict,
    summaries: dict,
    emit: Callable,
    database: Path,
    feedback: str = "",
    previous: AnalyticalPlan | None = None,
    *,
    cost_experiments: frozenset[str] | None = None,
    trace_attributes: Mapping[str, AttributeValue] | None = None,
) -> AnalyticalPlan:
    experiments = configured_cost_experiments(cost_experiments)
    db = DuckDBAnalysisTools(database, list(model_schemas))
    small_previews = "small_analysis_previews" in experiments
    settings = current_settings()

    # Select existing fields; do not classify prose or reinterpret model meaning.
    # Built-model dependencies can explain a derived field even after dbt build.
    semantic_plan = plan.model_dump(exclude={
        "dimensions": {"__all__": {"source_datasets"}},
        "facts": {"__all__": {
            "source_datasets": True,
            "dimension_links": {"__all__": {"source_paths"}},
        }},
    })
    for model in semantic_plan["dimensions"] + semantic_plan["facts"]:
        for field in model["output_fields"]:
            field["definition"]["inputs"] = [
                ref for ref in field["definition"]["inputs"] if ref["kind"] == "model"
            ]

    prompt = split_stable_and_repair_context(
        {
            "request": request,
            "conceptual_plan": semantic_plan,
            "validated_schemas": model_schemas,
            "model_summaries": summaries,
            "available_plots": plot_descriptions(),
            "limits": {
                "max_analyses": settings.max_analyses,
                "max_charts": settings.max_charts,
            },
        },
        {
            "previous_analysis": previous.model_dump() if previous else None,
            "previous_acceptance_error": feedback,
        },
    )
    result = invoke(
        prompt,
        AnalyticalPlan,
        "analytical interpretation",
        emit,
        ANALYSIS + (ANALYSIS_PREVIEW_GUIDANCE if small_previews else ""),
        tools=[
            db.list_models,
            db.describe_model,
            db.query_data_small_preview if small_previews else db.query_data,
        ],
        cost_experiments=experiments,
        context_directory=database.parent / "context",
        trace_attributes=trace_attributes,
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
    executed_sql: str,
    cost_experiments: frozenset[str] | None = None,
    trace_attributes: Mapping[str, AttributeValue] | None = None,
) -> AnalysisFinding:
    experiments = configured_cost_experiments(cost_experiments)
    prompt = split_stable_and_repair_context(
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
            "visualisation_goal": spec.visualisation_goal,
            "executed_sql": executed_sql,
            "result": result.model_dump(),
        },
        {
            "previous_finding_error": feedback,
            "previous_finding": previous.model_dump() if previous else None,
            "registered_evidence": db.registry.snapshot(),
        },
    )
    return invoke(
        prompt,
        EarlyValidatedAnalysisFinding
        if "early_finding_validation" in experiments
        else AnalysisFinding,
        "analytical finding",
        emit,
        ANALYSIS,
        tools=[db.inspect_result],
        cost_experiments=experiments,
        context_directory=db.database.parent / "context",
        trace_attributes=trace_attributes,
    )


def plot(
    spec: AnalysisSpec,
    result: AcceptedAnalysisResult,
    emit: Callable,
    database: Path,
    selection: list[ResultFilter] | None = None,
    *,
    original_request: str,
    frame: pd.DataFrame,
    title: str | None = None,
    cost_experiments: frozenset[str] | None = None,
    trace_attributes: Mapping[str, AttributeValue] | None = None,
) -> PlottingPlan:

    db = DuckDBResultTools(
        database=database,
        relation=result.relation,
        columns=set(result.columns),
    )

    prompt = split_stable_and_repair_context(
        {
            "available_plots": plot_descriptions(),
            "available_plot_arguments": plot_capabilities(),
            "renderer_templates": registered_plot_templates(),
        },
        {
            "title": title or spec.title,
            "question": spec.question,
            "original_request": original_request,
            "presentation_selection": [item.model_dump() for item in (selection or [])],
            "visualisation_goal": spec.visualisation_goal,
            "analytical_result": {
                "population_definition": spec.population_definition,
                "observation_rule": spec.observation_rule,
                "rationale": spec.rationale,
                "grain": spec.grain,
                "variant_field": spec.variant_field,
                "dimensions": [item.model_dump() for item in spec.dimensions],
                "measures": [item.model_dump() for item in spec.measures],
                "row_count": result.row_count,
                "columns": result.columns,
                "profile": [item.model_dump() for item in result.profile],
                "profile_scope": "Full accepted result; rendering applies presentation_selection",
            },
        },
    )

    return invoke(
        prompt,
        accepted_result_plot_schema(
            result, selection, frame=frame, title=title or spec.title
        ),
        "visualisation generation",
        emit,
        PLOTTING,
        tools=[
            db.category_counts,
        ],
        cost_experiments=cost_experiments,
        trace_attributes=trace_attributes,
        limits={"turns": 7},
    )
