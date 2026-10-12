import re
from typing import Literal

from pydantic import BaseModel, Field


class BusinessProcessPlan(BaseModel):
    name: str
    description: str
    source_datasets: list[str]


class DimensionPlan(BaseModel):
    name: str = Field(pattern=r"^dim_[a-z][a-z0-9_]*$")
    business_entity: str
    grain: str
    source_datasets: list[str]
    natural_key: str
    surrogate_key: str
    attributes: dict[str, str]


class FactMeasure(BaseModel):
    name: str
    source_expression: str
    additivity: Literal["additive", "semi_additive", "non_additive"]


class FactPlan(BaseModel):
    name: str = Field(pattern=r"^fact_[a-z][a-z0-9_]*$")
    business_process: str
    fact_type: Literal[
        "transaction", "periodic_snapshot", "accumulating_snapshot", "factless"
    ]
    grain: str
    source_datasets: list[str]
    dimension_keys: dict[str, str]
    measures: list[FactMeasure]


class DimensionalModelPlan(BaseModel):
    business_processes: list[BusinessProcessPlan]
    dimensions: list[DimensionPlan]
    facts: list[FactPlan]


class AnalyticalPlan(BaseModel):
    objective: str
    business_questions: list[str]
    assumptions: list[str]
    unresolved_questions: list[str]
    target_model: DimensionalModelPlan


def plan_issues(plan: AnalyticalPlan, source_names: list[str]) -> list[str]:
    """Check structural links before asking dbt to implement a plan."""
    target = plan.target_model
    issues = []
    process_names = {process.name for process in target.business_processes}
    fact_processes = {fact.business_process for fact in target.facts}
    dimension_keys = {key for fact in target.facts for key in fact.dimension_keys}
    if not target.facts:
        issues.append("The plan has no fact table")
    for process in target.business_processes:
        if process.name not in fact_processes:
            issues.append(f"Business process {process.name} has no fact table")
    for fact in target.facts:
        if fact.business_process not in process_names:
            issues.append(
                f"Fact {fact.name} refers to undefined business process {fact.business_process}"
            )
        for measure in fact.measures:
            if re.search(
                r"\b(sum|count|avg|min|max)\s*\(",
                measure.source_expression,
                re.IGNORECASE,
            ):
                issues.append(
                    f"Fact {fact.name} measure {measure.name} embeds an aggregate expression"
                )
    for dimension in target.dimensions:
        if dimension.surrogate_key not in dimension_keys:
            issues.append(f"Dimension {dimension.name} is not linked to a fact")
    known_sources = set(source_names)
    for item in target.business_processes + target.dimensions + target.facts:
        unknown = set(item.source_datasets) - known_sources
        if unknown:
            issues.append(
                f"{item.name} names unknown source datasets: {', '.join(sorted(unknown))}"
            )
    return issues


class DbtFile(BaseModel):
    path: str = Field(description="Relative path under models/; .sql or .yml only")
    content: str


class DbtImplementation(BaseModel):
    files: list[DbtFile]
    explanation: str


class ViewSpec(BaseModel):
    title: str
    table: str
    x: str
    y: str
    aggregate: Literal["sum", "avg", "count", "count_distinct"]
    chart: Literal["line", "bar", "scatter", "pie"]
    finding: str


class AnalysisPlan(BaseModel):
    views: list[ViewSpec] = Field(max_length=4)
    warnings: list[str] = Field(default_factory=list)
