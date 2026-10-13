"""Accept analytical results in DuckDB and render their data with Plotly."""

import inspect
import math
import re
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px

from .core.contracts import (
    AcceptedAnalysisResult,
    AnalysisPlan,
    AnalysisSpec,
    AnalyticalPlan,
    PlotSpec,
    ResultFilter,
)
from .data import identifier
from .tools.analysis_tools import POPULATION_CTE, validate_analysis_sql, validate_variant_sql


class AnalysisAcceptanceError(RuntimeError):
    def __init__(self, index: int, title: str, error: Exception):
        self.index = index
        self.title = title
        self.database_error = str(error)
        super().__init__(f"Analysis {index} ({title}) failed: {error}")


def analysis_statement(spec: AnalysisSpec, plan: AnalysisPlan, model_names: list[str]) -> str:
    """Return the exact final SQL to execute, including an optional shared qualification."""
    if spec.qualification_id is None:
        if not spec.sql or spec.variant_sql:
            raise ValueError("Direct analysis needs sql and no variant_sql")
        return validate_analysis_sql(spec.sql, model_names)
    if spec.sql or not spec.variant_sql:
        raise ValueError("Qualified analysis needs variant_sql and no direct sql")
    qualifications = {item.concept_id: item for item in plan.qualifications}
    if len(qualifications) != len(plan.qualifications):
        raise ValueError("Shared qualification IDs must be distinct")
    qualification = qualifications.get(spec.qualification_id)
    if qualification is None:
        raise ValueError(f"Unknown shared qualification: {spec.qualification_id}")
    if spec.concept_id != qualification.concept_id:
        raise ValueError("Analysis concept_id does not match its qualification")
    base = validate_analysis_sql(qualification.sql, model_names)
    variant = validate_variant_sql(spec.variant_sql)
    combined = f"WITH {POPULATION_CTE} AS ({base}) SELECT * FROM ({variant}) AS accepted_variant"
    return validate_analysis_sql(combined, model_names)


def accept_analyses(
    database: Path,
    model_names: list[str],
    analysis: AnalysisPlan,
) -> list[tuple[AnalysisSpec, AcceptedAnalysisResult]]:
    accepted: list[tuple[AnalysisSpec, AcceptedAnalysisResult]] = []

    with duckdb.connect(
        str(database),
        config={"enable_external_access": "false"},
    ) as con:
        con.begin()
        try:
            con.execute("CREATE SCHEMA IF NOT EXISTS analysis")
            for index, spec in enumerate(analysis.analyses):
                try:
                    statement = analysis_statement(spec, analysis, model_names)
                    result_id = f"result_{index:02d}"
                    relation = f'analysis."{result_id}"'
                    con.execute(f"CREATE OR REPLACE TABLE {relation} AS {statement}")
                    row_count = con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
                    if row_count == 0 and spec.empty_result_policy == "repair":
                        raise ValueError("Analysis result is empty")
                    described = con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
                    columns = {row[0]: row[1] for row in described}
                    profile_frame = con.execute(f"SUMMARIZE SELECT * FROM {relation}").fetchdf()
                    profile_rows = (
                        profile_frame.astype(object)
                        .where(pd.notna(profile_frame), None)
                        .to_dict("records")
                    )
                    accepted_result = AcceptedAnalysisResult(
                        result_id=result_id,
                        relation=relation,
                        row_count=row_count,
                        columns=columns,
                        profile=profile_rows,
                    )
                    validate_result_semantics(spec, accepted_result)
                    accepted.append((spec, accepted_result))
                except Exception as exc:
                    raise AnalysisAcceptanceError(index, spec.title, exc) from exc
            con.commit()
        except Exception:
            con.rollback()
            raise

    return accepted


def validate_result_semantics(
    spec: AnalysisSpec,
    result: AcceptedAnalysisResult,
) -> None:
    available = set(result.columns)

    declared = {item.field for item in [*spec.dimensions, *spec.measures]}

    missing = declared - available
    if missing:
        raise ValueError(
            f"Analysis semantics reference missing columns "
            f"for {spec.title}: {sorted(missing)}"
        )
    if spec.variant_field and spec.variant_field not in available:
        raise ValueError(f"{spec.title}: missing variant field {spec.variant_field}")
    numeric = re.compile(r"^(?:U?(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|INT\d*|DECIMAL|DOUBLE|FLOAT|REAL)", re.I)
    for measure in spec.measures:
        components = measure.components
        if components is None:
            continue
        if not math.isfinite(components.scale) or components.scale <= 0:
            raise ValueError(f"{spec.title}: invalid ratio scale for {measure.field}")
        for field in (components.numerator_field, components.denominator_field):
            if field not in available:
                raise ValueError(f"{spec.title}: {measure.field} component {field} is missing")
            if not numeric.search(result.columns[field]):
                raise ValueError(f"{spec.title}: {measure.field} component {field} is not numeric")


def validate_built_models(
    database: Path, plan: AnalyticalPlan, built_schemas: dict[str, dict[str, str]]
) -> list[str]:
    """Check run-declared output interfaces and optional declared grain keys."""
    issues: list[str] = []
    models = plan.target_model.dimensions + plan.target_model.facts
    for model in models:
        actual = set(built_schemas.get(model.name, {}))
        required = {field.name for field in model.output_fields}
        missing = required - actual
        if missing:
            issues.append(f"{model.name} omits planned outputs: {sorted(missing)}")
        keys = model.grain_key_fields
        if keys and set(keys) <= actual:
            table = f'main."{identifier(model.name)}"'
            fields = ", ".join('"' + field.replace('"', '""') + '"' for field in keys)
            with duckdb.connect(str(database), read_only=True) as con:
                duplicate = con.execute(
                    f"SELECT 1 FROM {table} GROUP BY {fields} HAVING count(*) > 1 LIMIT 1"
                ).fetchone()
            if duplicate:
                issues.append(f"{model.name} duplicates declared grain keys {keys}")
    for fact in plan.target_model.facts:
        fact_columns = set(built_schemas.get(fact.name, {}))
        for link in fact.dimension_links:
            if link.fact_output_field not in fact_columns:
                issues.append(f"{fact.name} omits fact link output {link.fact_output_field}")
            dimension_columns = set(built_schemas.get(link.dimension_model, {}))
            if link.dimension_key_field not in dimension_columns:
                issues.append(
                    f"{link.dimension_model} omits join key {link.dimension_key_field} "
                    f"required by {fact.name}"
                )
            for path in link.source_paths:
                for step in path.steps:
                    for endpoint in (step.left, step.right):
                        if endpoint.kind == "model" and endpoint.column not in built_schemas.get(endpoint.container, {}):
                            issues.append(
                                f"{endpoint.container} omits match output {endpoint.column} "
                                f"required by {fact.name}"
                            )
    return sorted(set(issues))


def summarize_models(database: Path, model_names: list[str]) -> dict:
    summaries = {}
    with duckdb.connect(str(database), read_only=True) as con:
        for name in model_names:
            table = f'main."{identifier(name)}"'
            summaries[name] = {
                "rows": con.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                "preview": con.execute(f"SELECT * FROM {table} LIMIT 3")
                .df()
                .astype(str)
                .to_dict("records"),
            }
    return summaries


PLOTLY_COLUMN_ARGUMENTS = {
    "x",
    "y",
    "z",
    "color",
    "size",
    "symbol",
    "text",
    "facet_row",
    "facet_col",
    "hover_name",
    "hover_data",
    "names",
    "values",
    "path",
    "ids",
    "parents",
    "lat",
    "lon",
    "locations",
    "animation_frame",
    "animation_group",
    "dimensions",
    "custom_data",
    "line_group",
    "line_dash",
    "pattern_shape",
}


def load_result_frame(
    database: Path,
    result: AcceptedAnalysisResult,
) -> pd.DataFrame:
    with duckdb.connect(
        str(database),
        read_only=True,
        config={"enable_external_access": "false"},
    ) as con:
        return con.execute(f"SELECT * FROM {result.relation}").df()


def validate_plot_spec(plot: PlotSpec, result: AcceptedAnalysisResult) -> None:
    available = set(result.columns)
    for argument, value in plot.columns.items():
        names = value if isinstance(value, list) else [value]
        if not names or any(name not in available for name in names):
            raise ValueError(f"Plot references a missing accepted-result column: {argument}={value}")
    for selection in plot.filters:
        if selection.field not in available:
            raise ValueError(f"Plot filter references missing accepted-result column: {selection.field}")
        if selection.operator == "eq" and len(selection.values) != 1:
            raise ValueError("Plot eq filter needs one value")
        if selection.operator == "in" and not selection.values:
            raise ValueError("Plot in filter needs values")
        if selection.operator == "is_null" and selection.values:
            raise ValueError("Plot is_null filter takes no values")


def filter_frame(frame: pd.DataFrame, filters: list[ResultFilter]) -> pd.DataFrame:
    for selection in filters:
        if selection.field not in frame.columns:
            raise ValueError(f"Plot filter references missing result column: {selection.field}")
        series = frame[selection.field]
        if selection.operator == "eq":
            if len(selection.values) != 1:
                raise ValueError("Plot eq filter needs one value")
            frame = frame.loc[series == selection.values[0]]
        elif selection.operator == "in":
            if not selection.values:
                raise ValueError("Plot in filter needs values")
            frame = frame.loc[series.isin(selection.values)]
        else:
            if selection.values:
                raise ValueError("Plot is_null filter takes no values")
            frame = frame.loc[series.isna()]
    return frame


def render_plot(frame: pd.DataFrame, plot: PlotSpec, title: str):
    """Use only the executed DataFrame as Plotly's data source."""
    frame = filter_frame(frame, plot.filters)
    if plot.function == "imshow":
        if plot.columns:
            raise ValueError("imshow uses the full DataFrame without column arguments")
        return px.imshow(frame, title=title)
    function = getattr(px, plot.function, None)
    if not callable(function):
        raise ValueError(f"Unknown Plotly Express function: {plot.function}")
    parameters = inspect.signature(function).parameters
    if "data_frame" not in parameters or "title" not in parameters:
        raise ValueError(f"Unsupported Plotly Express function: {plot.function}")
    for argument, value in plot.columns.items():
        if argument not in PLOTLY_COLUMN_ARGUMENTS or argument not in parameters:
            raise ValueError(f"Unsupported plot column argument: {argument}")
        names = value if isinstance(value, list) else [value]
        if not names or any(name not in frame.columns for name in names):
            raise ValueError(
                f"Plot references a missing result column: {argument}={value}"
            )
    return function(frame, title=title, **plot.columns)


def result_metrics(
    database: Path, results: list[tuple[AnalysisSpec, AcceptedAnalysisResult]]
) -> dict:
    """Use scalar accepted results as cards without assuming a business measure."""
    cards = {}
    for spec, result in results:
        if result.row_count != 1:
            continue
        frame = load_result_frame(database, result)
        for column in frame.select_dtypes(include="number").columns:
            value = frame.iloc[0][column]
            if pd.notna(value):
                cards[f"{spec.title}: {column}"] = float(value)
    return cards
