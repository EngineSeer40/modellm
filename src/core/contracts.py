"""Run-scoped declarations. Explanations communicate intent; references are checked."""

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

CONTRACT_VERSION = 3


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationIssue(Contract):
    boundary: str
    code: str
    path: str
    message: str
    expected: Any = None
    observed: Any = None

    def __str__(self):
        return f"{self.path}: {self.message}"


class FieldRef(Contract):
    kind: Literal["source", "model"]
    dataset: str | None = Field(
        default=None,
        description="For source: exact dataset from this run's profile; otherwise null",
    )
    model: str | None = Field(
        default=None,
        description="For model: a model declared in this plan; otherwise null",
    )
    column: str = Field(
        min_length=1, description="Exact column name, including spaces and punctuation"
    )

    @model_validator(mode="after")
    def reference_target(self):
        if self.kind == "source" and (not self.dataset or self.model is not None):
            raise ValueError(
                "A source reference needs dataset and column, with model null"
            )
        if self.kind == "model" and (not self.model or self.dataset is not None):
            raise ValueError(
                "A model reference needs model and column, with dataset null"
            )
        return self


class OutputKeyPair(Contract):
    fact_field: str
    dimension_field: str


class MatchPair(Contract):
    left: FieldRef
    right: FieldRef


class MatchStep(Contract):
    pairs: list[MatchPair] = Field(
        min_length=1,
        description="Pairs jointly define the match; all right endpoints name one destination",
    )
    matching_rule: str | None = None


class SourceMatchPath(Contract):
    source_dataset: str
    steps: list[MatchStep] = Field(min_length=1)


class FactDimensionLink(Contract):
    dimension_model: str
    key_pairs: list[OutputKeyPair] = Field(
        min_length=1,
        description="References to canonical outputs; supports composite keys",
    )
    source_paths: list[SourceMatchPath] = Field(min_length=1)
    unmatched_policy: Literal["null", "unknown_member", "exclude", "other"]
    unmatched_explanation: str | None = None


# ----- CONCEPTUAL -----


class FieldDefinition(Contract):
    kind: Literal["copy", "derive", "constant", "generate"]
    inputs: list[FieldRef] = Field(
        description="copy: one reference; derive: actual inputs; constant/generate: empty list"
    )
    explanation: str | None = Field(
        default=None,
        description="Transformation intent for derive, purpose for constant, generation rule for generate; dbt writes the SQL",
    )
    value: Any = Field(
        default=None,
        description="For constant: actual JSON scalar (string, number, boolean or null); otherwise omit",
    )

    @model_validator(mode="after")
    def definition_shape(self):
        if self.kind == "copy" and len(self.inputs) != 1:
            raise ValueError("copy needs exactly one input reference")
        if self.kind == "derive" and not self.inputs:
            raise ValueError("derive needs its actual field inputs")
        if self.kind in {"constant", "generate"} and self.inputs:
            raise ValueError(
                f"{self.kind} has no field inputs; use derive for input-dependent values"
            )
        if self.kind != "copy" and not (self.explanation and self.explanation.strip()):
            raise ValueError(f"{self.kind} needs an explanation")
        if self.kind == "constant":
            if "value" not in self.model_fields_set:
                raise ValueError(
                    "constant needs an explicit value, including null when intended"
                )
            if self.value is not None and type(self.value) not in (
                str,
                int,
                float,
                bool,
            ):
                raise ValueError("constant value must be a JSON scalar")
        elif self.value is not None:
            raise ValueError("Only constant has a literal value")
        return self


class MeasureSemantics(Contract):
    additivity: Literal["additive", "semi_additive", "non_additive"]


class ModelOutput(Contract):
    name: str = Field(
        min_length=1,
        description="Exact output column dbt must emit; declare it only once",
    )
    description: str = Field(min_length=1, description="Business meaning of the output")
    definition: FieldDefinition
    measure: MeasureSemantics | None = Field(
        default=None,
        description="Measure metadata when this output is a measure; otherwise null",
    )


class BusinessProcessPlan(Contract):
    name: str
    description: str
    source_datasets: list[str]


class DimensionPlan(Contract):
    name: str = Field(pattern=r"^dim_[a-z][a-z0-9_]*$")
    business_entity: str
    grain: str
    source_datasets: list[str]
    natural_key_fields: list[str] = Field(
        description="Declared identity outputs, or an empty list"
    )
    surrogate_key: str | None = None
    output_fields: list[ModelOutput] = Field(min_length=1)
    grain_key_fields: list[str] = Field(
        description="Outputs whose combined uniqueness is justified; otherwise an empty list"
    )


class FactPlan(Contract):
    name: str = Field(pattern=r"^fact_[a-z][a-z0-9_]*$")
    business_process: str
    fact_type: Literal[
        "transaction", "periodic_snapshot", "accumulating_snapshot", "factless"
    ]
    grain: str
    modelling_rationale: str = Field(
        min_length=1,
        description="Why this is a reusable business process at this grain",
    )
    source_datasets: list[str]
    dimension_links: list[FactDimensionLink] = Field(
        description="Declared links, or an empty list for a fact without dimensions"
    )
    output_fields: list[ModelOutput] = Field(
        min_length=1,
        description="Every output once, with measure metadata on measure fields",
    )
    grain_key_fields: list[str] = Field(
        description="Outputs whose combined uniqueness is justified; otherwise an empty list"
    )


class DimensionalModelPlan(Contract):
    business_processes: list[BusinessProcessPlan]
    dimensions: list[DimensionPlan]
    facts: list[FactPlan]


class AnalyticalPlan(Contract):
    objective: str
    business_questions: list[str]
    assumptions: list[str]
    unresolved_questions: list[str]
    target_model: DimensionalModelPlan


# ----------------------


def field_inputs(field: ModelOutput) -> list[FieldRef]:
    return field.definition.inputs


def ref_container(ref: FieldRef) -> tuple[str, str]:
    return (ref.kind, ref.dataset if ref.kind == "source" else ref.model)


def plan_issues(
    plan: AnalyticalPlan, profiles: dict, relationships: list[dict] | None = None
) -> list[ValidationIssue]:
    """Resolve declarations against this run; do not interpret transformation prose."""
    issues: list[ValidationIssue] = []
    target = plan.target_model
    models = target.dimensions + target.facts
    sources = {
        name: {col["name"] for col in p["columns"]} for name, p in profiles.items()
    }
    outputs = {m.name: {f.name for f in m.output_fields} for m in models}
    dimensions = {m.name: m for m in target.dimensions}
    processes = {p.name for p in target.business_processes}
    field_graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    model_graph: dict[str, set[str]] = {m.name: set() for m in models}

    def issue(code, path, message, expected=None, observed=None):
        issues.append(
            ValidationIssue(
                boundary="conceptual",
                code=code,
                path=path,
                message=message,
                expected=expected,
                observed=observed,
            )
        )

    def reference(ref, path, declared):
        if ref.kind == "source":
            if ref.dataset not in sources:
                issue(
                    "unknown_source",
                    path,
                    f"Unknown source {ref.dataset}",
                    sorted(sources),
                    ref.dataset,
                )
            elif ref.column not in sources[ref.dataset]:
                issue(
                    "missing_source_field",
                    path,
                    f"Missing source field {ref.dataset}.{ref.column}",
                    sorted(sources[ref.dataset]),
                    ref.column,
                )
            if ref.dataset not in declared:
                issue(
                    "undeclared_source",
                    path,
                    f"Source {ref.dataset} is outside the declared source scope",
                    declared,
                    ref.dataset,
                )
        elif ref.model not in outputs or ref.column not in outputs[ref.model]:
            issue(
                "missing_planned_field",
                path,
                f"Missing planned output {ref.model}.{ref.column}",
                sorted(outputs.get(ref.model, [])),
                ref.column,
            )

    def output(model, name, path):
        if name not in outputs.get(model, set()):
            issue(
                "missing_planned_field",
                path,
                f"Missing planned output {model}.{name}",
                sorted(outputs.get(model, [])),
                name,
            )

    if len(outputs) != len(models):
        issue("duplicate_model", "target_model", "Model names must be distinct")
    if len(processes) != len(target.business_processes):
        issue(
            "duplicate_process", "business_processes", "Process names must be distinct"
        )
    if not target.facts:
        issue("missing_fact", "facts", "The plan has no fact table")
    for process in target.business_processes:
        if not any(f.business_process == process.name for f in target.facts):
            issue("unimplemented_process", process.name, "Business process has no fact")
        for name in process.source_datasets:
            if name not in sources:
                issue(
                    "unknown_source",
                    process.name,
                    f"Unknown source {name}",
                    sorted(sources),
                    name,
                )
    for model in models:
        if len(outputs[model.name]) != len(model.output_fields):
            issue("duplicate_output", model.name, "Each output must be declared once")
        for name in model.source_datasets:
            if name not in sources:
                issue(
                    "unknown_source",
                    model.name,
                    f"Unknown source {name}",
                    sorted(sources),
                    name,
                )
        for field in model.output_fields:
            node = (model.name, field.name)
            field_graph[node] = set()
            for index, ref in enumerate(field_inputs(field)):
                reference(
                    ref,
                    f"{model.name}.output_fields.{field.name}.definition.inputs[{index}]",
                    model.source_datasets,
                )
                if ref.kind == "model":
                    field_graph[node].add((ref.model, ref.column))
                    if ref.model != model.name:
                        model_graph[model.name].add(ref.model)
        for key in model.grain_key_fields:
            output(model.name, key, f"{model.name}.grain_key_fields")
    for dimension in target.dimensions:
        for key in dimension.natural_key_fields + (
            [dimension.surrogate_key] if dimension.surrogate_key else []
        ):
            output(dimension.name, key, f"{dimension.name}.identity")
    for fact in target.facts:
        if fact.business_process not in processes:
            issue(
                "unknown_process",
                fact.name,
                f"Unknown process {fact.business_process}",
                sorted(processes),
                fact.business_process,
            )
        for index, link in enumerate(fact.dimension_links):
            path = f"{fact.name}.dimension_links[{index}]"
            dimension = dimensions.get(link.dimension_model)
            if dimension is None:
                issue(
                    "unknown_dimension",
                    path,
                    f"Unknown dimension {link.dimension_model}",
                    sorted(dimensions),
                    link.dimension_model,
                )
            model_graph[fact.name].add(link.dimension_model)
            for pair in link.key_pairs:
                output(fact.name, pair.fact_field, path)
                output(link.dimension_model, pair.dimension_field, path)
            if link.unmatched_policy == "other" and not link.unmatched_explanation:
                issue(
                    "missing_policy_explanation",
                    path,
                    "Other unmatched policy needs an explanation",
                )
            for pi, match_path in enumerate(link.source_paths):
                pp = f"{path}.source_paths[{pi}]"
                if match_path.source_dataset not in fact.source_datasets:
                    issue(
                        "undeclared_source",
                        pp,
                        "Path must start at a declared fact source",
                        fact.source_datasets,
                        match_path.source_dataset,
                    )
                seen = {("source", match_path.source_dataset)}
                destinations = set()
                for si, step in enumerate(match_path.steps):
                    sp = f"{pp}.steps[{si}]"
                    destinations = {ref_container(pair.right) for pair in step.pairs}
                    if len(destinations) != 1:
                        issue(
                            "multiple_destinations",
                            sp,
                            "A match step must have one destination container",
                        )
                    for pair in step.pairs:
                        if ref_container(pair.left) not in seen:
                            issue(
                                "disconnected_path",
                                sp,
                                "Left endpoint must be in the source or an already reached container",
                            )
                        for ref in (pair.left, pair.right):
                            reference(
                                ref,
                                sp,
                                fact.source_datasets
                                + (dimension.source_datasets if dimension else []),
                            )
                            if ref.kind == "model" and ref.model != fact.name:
                                model_graph[fact.name].add(ref.model)
                    seen.update(destinations)
                if destinations != {("model", link.dimension_model)}:
                    issue(
                        "wrong_path_destination",
                        pp,
                        f"Path must end at {link.dimension_model}",
                    )

    def cycles(graph, code):
        visiting, visited = set(), set()

        def visit(node, trail):
            if node in visiting:
                issue(code, "target_model", f"Dependency cycle: {trail + [node]}")
                return
            if node in visited or node not in graph:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency, trail + [node])
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node, [])

    cycles(field_graph, "field_dependency_cycle")
    cycles(model_graph, "model_dependency_cycle")
    return issues


class DbtFile(Contract):
    path: str = Field(
        description="Flat planned dim_*.sql/fact_*.sql filename or schema.yml",
        pattern=r"^(?:dim_[a-z][a-z0-9_]*\.sql|fact_[a-z][a-z0-9_]*\.sql|schema\.yml)$",
    )
    content: str


class DbtImplementation(Contract):
    files: list[DbtFile]
    explanation: str


class AnalysisDimension(Contract):
    field: str
    semantic_type: Literal[
        "temporal", "categorical", "ordinal", "geographic", "identifier", "other"
    ]
    description: str


class RatioComponents(Contract):
    numerator_field: str
    denominator_field: str
    scale: float = 1.0


class AnalysisMeasure(Contract):
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
    additivity: Literal["additive", "semi_additive", "non_additive"]
    components: RatioComponents | None = None


class SharedQualification(Contract):
    concept_id: str = Field(
        min_length=1,
        description="Run-local qualification ID referenced by qualified queries",
    )
    sql: str
    grain: str
    population_definition: str


class QueryDefinition(Contract):
    kind: Literal["direct", "qualified"]
    sql: str = Field(
        min_length=1,
        description="direct: SELECT over validated main models; qualified: SELECT over __modellm_population only",
    )
    qualification_id: str | None = Field(
        default=None,
        description="Required for qualified: ID of a SharedQualification in this proposal; direct: null",
    )

    @model_validator(mode="after")
    def qualification_choice(self):
        if self.kind == "qualified" and not self.qualification_id:
            raise ValueError("qualified needs a qualification_id")
        if self.kind == "direct" and self.qualification_id is not None:
            raise ValueError("direct has no qualification_id")
        return self


class AnalysisSpec(Contract):
    title: str
    question: str
    query: QueryDefinition
    concept_id: str = Field(
        min_length=1, description="Groups related views of one analytical concept"
    )
    variant_id: str | None = None
    population_definition: str = Field(min_length=1)
    observation_rule: str | None = None
    empty_result_policy: Literal["accept", "repair"] = "repair"
    variant_field: str | None = Field(
        default=None,
        description="Actual result discriminator when alternate analytical grains share one result",
    )
    rationale: str = Field(description="Why these transformations answer the question")
    grain: str = Field(description="What one result row represents")
    dimensions: list[AnalysisDimension]
    measures: list[AnalysisMeasure]
    visualisation_goal: str = Field(
        description="What the result must communicate, without chart mechanics"
    )


class AnalysisPlan(Contract):
    analyses: list[AnalysisSpec] = Field(min_length=1)
    qualifications: list[SharedQualification] = Field(
        description="Shared populations used by qualified queries; otherwise an empty list"
    )
    warnings: list[str] = Field(
        description="Analytical assumptions/limitations, or an empty list"
    )


class ResultFilter(Contract):
    field: str = Field(description="Exact accepted-result field")
    operator: Literal["eq", "in", "is_null"]
    values: list[Any] = Field(
        description="JSON scalar values: eq takes one, in takes one or more, is_null takes an empty list"
    )

    @model_validator(mode="after")
    def cardinality(self):
        if any(type(value) not in (str, int, float, bool) for value in self.values):
            raise ValueError(
                "Filter values must be non-null JSON scalars; use is_null for null"
            )
        if (
            self.operator == "eq"
            and len(self.values) != 1
            or self.operator == "in"
            and not self.values
            or self.operator == "is_null"
            and self.values
        ):
            raise ValueError(
                "Filter values must match the operator: eq=one, in=nonempty, is_null=none"
            )
        return self


class EvidenceAggregate(Contract):
    function: Literal["count_rows", "count_distinct", "sum", "avg", "min", "max"]
    field: str | None = Field(
        default=None,
        description="No field for count_rows; an exact accepted-result field for every other operation",
    )

    @model_validator(mode="after")
    def field_required(self):
        if (self.function == "count_rows") != (self.field is None):
            raise ValueError("Only count_rows takes no field")
        return self


class EvidenceSort(Contract):
    field: str = Field(
        description="Exact projected/group field or aggregate alias; no SQL suffix"
    )
    direction: Literal["asc", "desc"]
    nulls: Literal["first", "last"] = "last"


class EvidenceRequest(Contract):
    kind: Literal["rows", "summary"]
    fields: list[str] = Field(
        default_factory=list,
        description="rows: exact result columns to project; summary: empty",
    )
    group_by: list[str] = Field(default_factory=list)
    aggregates: dict[str, EvidenceAggregate] = Field(
        default_factory=dict,
        description="summary: output alias to aggregate operation; rows: empty",
    )
    filters: list[ResultFilter] = Field(default_factory=list)
    order_by: list[EvidenceSort] = Field(default_factory=list)
    limit: int = Field(default=20, ge=1, le=100)

    @model_validator(mode="after")
    def request_shape(self):
        if self.kind == "rows" and (
            not self.fields or self.group_by or self.aggregates
        ):
            raise ValueError("rows needs fields and no group_by/aggregates")
        if self.kind == "summary" and (self.fields or not self.aggregates):
            raise ValueError("summary needs aggregates and no projected fields")
        return self


class EvidenceRowRef(Contract):
    evidence_id: str = Field(
        description="ID returned by inspect_result for this accepted result"
    )
    row_id: str = Field(
        description="Stable row ID returned with that evidence; never an invented position"
    )


class FindingCell(Contract):
    row: str = Field(description="Alias in this statement's selected_rows")
    column: str = Field(description="Exact column of the stored evidence row")


class FindingStatement(Contract):
    template: str = Field(
        min_length=1,
        description="Outcome text with simple {placeholders}. Bind observed values/dates/labels to evidence; constants from the supplied analytical definition may appear in prose.",
    )
    selected_rows: dict[str, EvidenceRowRef]
    bindings: dict[str, FindingCell]


class AnalysisFinding(Contract):
    statements: list[FindingStatement] = Field(min_length=1)
    caveats: list[str] = Field(
        description="Explanatory claims, not verified numerical outcomes; empty list if none"
    )


class ResultColumnProfile(Contract):
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


class AcceptedAnalysisResult(Contract):
    result_id: str
    acceptance_id: str = Field(default_factory=lambda: uuid4().hex)
    relation: str
    row_count: int
    columns: dict[str, str]
    profile: list[ResultColumnProfile]


class PlotSpec(Contract):
    function: str = Field(description="Supported Plotly Express chart function")
    columns: dict[str, Any] = Field(
        description="Plotly argument to exact accepted field name, or a list of exact names for multi-field arguments"
    )
    filters: list[ResultFilter] = Field(
        description="Accepted-result selections; empty list if none"
    )
    options: dict[str, Any] = Field(
        description="Supported presentation options from renderer_capabilities, such as orientation=h; never field references or data arrays. Empty object if none."
    )

    @model_validator(mode="after")
    def column_mappings(self):
        for value in self.columns.values():
            if isinstance(value, str):
                continue
            if (
                not isinstance(value, list)
                or not value
                or any(not isinstance(name, str) for name in value)
            ):
                raise ValueError(
                    "A plot column mapping must be one field name or a nonempty list of field names"
                )
        if any(
            not isinstance(value, (str, int, float, bool))
            for value in self.options.values()
        ):
            raise ValueError(
                "Plot presentation options must be scalar strings, numbers or booleans"
            )
        return self
