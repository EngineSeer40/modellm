from typing import Literal

from pydantic import BaseModel, Field


class BusinessProcessPlan(BaseModel):
    name: str
    description: str
    source_datasets: list[str]


class DimensionPlan(BaseModel):
    name: str
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
    name: str
    business_process: str
    fact_type: Literal["transaction", "periodic_snapshot", "accumulating_snapshot", "factless"]
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
