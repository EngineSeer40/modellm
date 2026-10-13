import re
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class BusinessProcessPlan(BaseModel):
    name: str
    description: str
    source_datasets: list[str]


class SourceFieldRef(BaseModel):
    dataset: str = Field(min_length=1)
    column: str = Field(min_length=1)


class PlannedOutputField(BaseModel):
    name: str = Field(min_length=1)
    description: str
    inputs: list[SourceFieldRef] = Field(default_factory=list)
    derivation: str | None = None

    @model_validator(mode="after")
    def has_lineage(self):
        if not self.inputs and not self.derivation:
            raise ValueError("Planned output needs source inputs or a derivation")
        return self


class JoinEndpoint(BaseModel):
    kind: Literal["source", "model"]
    container: str = Field(min_length=1)
    column: str = Field(min_length=1)


class JoinStep(BaseModel):
    left: JoinEndpoint
    right: JoinEndpoint
    rule: str | None = None


class SourceMatchPath(BaseModel):
    source_dataset: str
    steps: list[JoinStep] = Field(min_length=1)


class FactDimensionLink(BaseModel):
    fact_output_field: str
    dimension_model: str
    dimension_key_field: str
    source_paths: list[SourceMatchPath] = Field(min_length=1)
    unmatched_policy: Literal["null", "unknown_member", "exclude", "other"]
    unmatched_explanation: str | None = None


class DimensionPlan(BaseModel):
    name: str = Field(pattern=r"^dim_[a-z][a-z0-9_]*$")
    business_entity: str
    grain: str
    source_datasets: list[str]
    natural_key_fields: list[str] = Field(default_factory=list)
    surrogate_key: str | None = None
    output_fields: list[PlannedOutputField] = Field(min_length=1)
    grain_key_fields: list[str] = Field(default_factory=list)


class FactMeasure(BaseModel):
    name: str
    source_expression: str
    additivity: Literal["additive", "semi_additive", "non_additive"]
    inputs: list[SourceFieldRef] = Field(default_factory=list)
    derivation: str | None = None

    @model_validator(mode="after")
    def has_lineage(self):
        if not self.inputs and not self.derivation:
            raise ValueError("Fact measure needs source inputs or a derivation")
        return self


class FactPlan(BaseModel):
    name: str = Field(pattern=r"^fact_[a-z][a-z0-9_]*$")
    business_process: str
    fact_type: Literal[
        "transaction", "periodic_snapshot", "accumulating_snapshot", "factless"
    ]
    grain: str
    source_datasets: list[str]
    dimension_links: list[FactDimensionLink] = Field(default_factory=list)
    measures: list[FactMeasure]
    output_fields: list[PlannedOutputField] = Field(min_length=1)
    grain_key_fields: list[str] = Field(default_factory=list)


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


def plan_issues(
    plan: AnalyticalPlan, profiles: dict, relationships: list[dict] | None = None
) -> list[str]:
    """Check this run's source and output references before dbt generation."""
    target = plan.target_model
    issues: list[str] = []
    source_columns = {
        dataset: {column["name"] for column in profile["columns"]}
        for dataset, profile in profiles.items()
    }
    process_names = {process.name for process in target.business_processes}
    fact_processes = {fact.business_process for fact in target.facts}
    models = target.dimensions + target.facts
    model_names = [model.name for model in models]
    if len(model_names) != len(set(model_names)):
        issues.append("Planned model names must be distinct")
    outputs = {
        model.name: {field.name for field in model.output_fields} for model in models
    }
    dimensions = {dimension.name: dimension for dimension in target.dimensions}

    def check_source(ref: SourceFieldRef, location: str, declared: list[str]) -> None:
        if ref.dataset not in source_columns:
            issues.append(f"{location} references unknown source {ref.dataset}")
        elif ref.column not in source_columns[ref.dataset]:
            issues.append(
                f"{location} references missing source field "
                f"{ref.dataset}.{ref.column}; available: {sorted(source_columns[ref.dataset])}"
            )
        if ref.dataset not in declared:
            issues.append(f"{location} uses undeclared source {ref.dataset}")

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
            if measure.name not in outputs[fact.name]:
                issues.append(f"Fact {fact.name} omits measure output {measure.name}")
            for index, ref in enumerate(measure.inputs):
                check_source(ref, f"{fact.name}.measures.{measure.name}.inputs[{index}]", fact.source_datasets)
        for link_index, link in enumerate(fact.dimension_links):
            location = f"{fact.name}.dimension_links[{link_index}]"
            if link.fact_output_field not in outputs[fact.name]:
                issues.append(f"{location} omits fact output {link.fact_output_field}")
            dimension = dimensions.get(link.dimension_model)
            if dimension is None:
                issues.append(f"{location} names unknown dimension {link.dimension_model}")
            elif link.dimension_key_field not in outputs[dimension.name]:
                issues.append(
                    f"{location} names missing dimension output "
                    f"{dimension.name}.{link.dimension_key_field}"
                )
            for path_index, path in enumerate(link.source_paths):
                path_location = f"{location}.source_paths[{path_index}]"
                if path.source_dataset not in fact.source_datasets:
                    issues.append(f"{path_location} starts at undeclared fact source {path.source_dataset}")
                seen_containers = {("source", path.source_dataset)}
                for step_index, step in enumerate(path.steps):
                    step_location = f"{path_location}.steps[{step_index}]"
                    if (step.left.kind, step.left.container) not in seen_containers:
                        issues.append(f"{step_location} is disconnected from the source path")
                    for endpoint in (step.left, step.right):
                        if endpoint.kind == "source":
                            check_source(
                                SourceFieldRef(dataset=endpoint.container, column=endpoint.column),
                                step_location,
                                fact.source_datasets + (dimension.source_datasets if dimension else []),
                            )
                        elif endpoint.container not in outputs:
                            issues.append(f"{step_location} names unknown model {endpoint.container}")
                        elif endpoint.column not in outputs[endpoint.container]:
                            issues.append(
                                f"{step_location} names missing planned output "
                                f"{endpoint.container}.{endpoint.column}"
                            )
                    seen_containers.add((step.right.kind, step.right.container))
                if path.steps[-1].right.kind != "model" or path.steps[-1].right.container != link.dimension_model:
                    issues.append(f"{path_location} must end at {link.dimension_model}")
            if link.unmatched_policy == "other" and not link.unmatched_explanation:
                issues.append(f"{location} needs an unmatched explanation")
        for field in fact.grain_key_fields:
            if field not in outputs[fact.name]:
                issues.append(f"Fact {fact.name} grain key {field} is not a planned output")
    linked_dimensions = {
        link.dimension_model for fact in target.facts for link in fact.dimension_links
    }
    for dimension in target.dimensions:
        if dimension.name not in linked_dimensions:
            issues.append(f"Dimension {dimension.name} is not linked to a fact")
        for field in [*dimension.natural_key_fields, *dimension.grain_key_fields]:
            if field not in outputs[dimension.name]:
                issues.append(f"Dimension {dimension.name} key {field} is not a planned output")
        if dimension.surrogate_key and dimension.surrogate_key not in outputs[dimension.name]:
            issues.append(
                f"Dimension {dimension.name} surrogate key "
                f"{dimension.surrogate_key} is not a planned output"
            )
    for model in models:
        field_names = [field.name for field in model.output_fields]
        if len(field_names) != len(set(field_names)):
            issues.append(f"Model {model.name} declares duplicate output fields")
        for field_index, field in enumerate(model.output_fields):
            for input_index, ref in enumerate(field.inputs):
                check_source(
                    ref,
                    f"{model.name}.output_fields[{field_index}].inputs[{input_index}]",
                    model.source_datasets,
                )
        unknown = set(model.source_datasets) - set(source_columns)
        if unknown:
            issues.append(
                f"{model.name} names unknown source datasets: {', '.join(sorted(unknown))}"
            )
    for process in target.business_processes:
        unknown = set(process.source_datasets) - set(source_columns)
        if unknown:
            issues.append(
                f"{process.name} names unknown source datasets: {', '.join(sorted(unknown))}"
            )
    return issues


class DbtFile(BaseModel):
    path: str = Field(
        description="Flat filename under models/: a planned dim_*.sql or fact_*.sql model, or schema.yml.",
        pattern=r"^(?:dim_[a-z][a-z0-9_]*\.sql|fact_[a-z][a-z0-9_]*\.sql|schema\.yml)$",
    )
    content: str


class DbtImplementation(BaseModel):
    files: list[DbtFile]
    explanation: str


class AnalysisDimension(BaseModel):
    field: str
    semantic_type: Literal[
        "temporal",
        "categorical",
        "ordinal",
        "geographic",
        "identifier",
        "other",
    ]
    description: str


class RatioComponents(BaseModel):
    numerator_field: str
    denominator_field: str
    scale: float = 1.0


class AnalysisMeasure(BaseModel):
    field: str
    semantic_type: Literal[
        "count",
        "currency",
        "percentage",
        "ratio",
        "quantity",
        "duration",
        "numeric",
        "other",
    ]
    description: str
    additivity: Literal[
        "additive",
        "semi_additive",
        "non_additive",
    ]
    components: RatioComponents | None = None


class SharedQualification(BaseModel):
    concept_id: str
    sql: str
    grain: str
    population_definition: str


class AnalysisSpec(BaseModel):
    title: str = Field(description="Title of this analysis")
    question: str = Field(description="User question that is being answered")

    sql: str | None = Field(default=None, description="Direct final SELECT over validated dbt models")
    qualification_id: str | None = None
    variant_sql: str | None = None
    concept_id: str = Field(min_length=1)
    variant_id: str | None = None
    population_definition: str = Field(min_length=1)
    observation_rule: str | None = None
    empty_result_policy: Literal["accept", "repair"] = "repair"
    variant_field: str | None = None

    rationale: str = Field(
        description="Why this transformation / grain answers the user question."
    )

    grain: str = Field(
        description="Human-readable declaration of what one result row represents"
    )

    dimensions: list[AnalysisDimension]
    measures: list[AnalysisMeasure]

    visualisation_goal: str = Field(
        description="What the result should communicate, without naming chart mechanics."
    )


class AnalysisPlan(BaseModel):
    analyses: list[AnalysisSpec] = Field(min_length=1, max_length=4)
    qualifications: list[SharedQualification] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class ResultFilter(BaseModel):
    field: str
    operator: Literal["eq", "in", "is_null"]
    values: list[str | int | float | bool] = Field(default_factory=list)


class EvidenceAggregate(BaseModel):
    function: Literal["count_rows", "count_distinct", "sum", "avg", "min", "max"]
    field: str | None = None


class FindingEvidenceRequest(BaseModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    group_by: list[str] = Field(default_factory=list)
    aggregates: dict[str, EvidenceAggregate] = Field(min_length=1)
    filters: list[ResultFilter] = Field(default_factory=list)
    order_by: str | None = None
    limit: int = Field(default=20, ge=1, le=100)


class FindingBinding(BaseModel):
    evidence_id: str
    row_index: int = Field(default=0, ge=0)
    column: str


class FindingStatement(BaseModel):
    template: str = Field(min_length=1)
    bindings: dict[str, FindingBinding] = Field(default_factory=dict)


class AnalysisFinding(BaseModel):
    evidence_requests: list[FindingEvidenceRequest] = Field(default_factory=list)
    statements: list[FindingStatement] = Field(min_length=1)
    warnings: list[str] = Field(default_factory=list)


class ResultColumnProfile(BaseModel):
    column_name: str
    column_type: str
    min: str | None = None
    max: str | None = None
    approx_unique: int | None = None
    avg: str | None = None
    std: str | None = None
    q25: str | None = None
    q50: str | None = None
    q75: str | None = None
    count: int
    null_percentage: str | float | None = None


class AcceptedAnalysisResult(BaseModel):
    result_id: str
    relation: str
    row_count: int
    columns: dict[str, str]
    profile: list[ResultColumnProfile]


class PlotSpec(BaseModel):
    function: str = Field(
        description="Plotly Express chart function, such as line, histogram, box, or treemap"
    )
    columns: dict[str, str | list[str]] = Field(
        default_factory=dict,
        description="Plotly Express column arguments, using only DataFrame column names",
    )
    filters: list[ResultFilter] = Field(default_factory=list)
