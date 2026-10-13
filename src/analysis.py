"""Accept analytical results in DuckDB and render their data with Plotly."""

import inspect
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px

from .core.contracts import AcceptedAnalysisResult, AnalysisPlan, AnalysisSpec, PlotSpec
from .data import identifier
from .tools.analysis_tools import validate_analysis_sql


def accept_analyses(
    database: Path,
    model_names: list[str],
    analysis: AnalysisPlan,
) -> list[tuple[AnalysisSpec, AcceptedAnalysisResult]]:
    accepted = []

    with duckdb.connect(
        str(database),
        config={"enable_external_access": "false"},
    ) as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS analysis")

        for index, spec in enumerate(analysis.analyses):
            statement = validate_analysis_sql(spec.sql, model_names)
            result_id = f"result_{index:02d}"
            relation = f'analysis."{result_id}"'

            con.execute(f"CREATE OR REPLACE TABLE {relation} AS {statement}")

            row_count = con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]

            if row_count == 0:
                raise ValueError(f"Analysis result is empty: {spec.title}")

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


def render_plot(frame: pd.DataFrame, plot: PlotSpec, title: str):
    """Use only the executed DataFrame as Plotly's data source."""
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
