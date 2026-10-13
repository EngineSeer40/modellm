"""Materialise analytical results, then render from their pandas DataFrames."""

import inspect
from pathlib import Path

import duckdb
import pandas as pd
import plotly.express as px

from .core.contracts import AcceptedAnalysisResult, AnalysisPlan, AnalysisSpec, PlotSpec
from .data import identifier
from .tools.analysis_tools import execute_analysis_sql, validate_analysis_sql


def demo_analysis() -> AnalysisPlan:
    """Fixed analytical questions for the bundled sales acceptance case."""
    return AnalysisPlan(
        analyses=[
            AnalysisSpec(
                title="Monthly revenue",
                question="How are completed sales changing over time?",
                sql="SELECT date_trunc('month', order_date) AS order_month, sum(revenue) AS revenue FROM main.fact_order_items GROUP BY 1 ORDER BY 1",
                finding="Completed sales total $160 in January, $290 in February, and $65 in March.",
                visualisation_intent="Show the monthly revenue trend",
            ),
            AnalysisSpec(
                title="Revenue by customer",
                question="Which customers contribute most to revenue?",
                sql="SELECT c.customer_name, sum(f.revenue) AS revenue FROM main.fact_order_items f JOIN main.dim_customer c ON f.customer_key = c.customer_key GROUP BY 1 ORDER BY revenue DESC",
                finding="Beta contributes $290, Acme $220, and the unknown customer $5.",
                visualisation_intent="Compare customer revenue",
            ),
            AnalysisSpec(
                title="Revenue by product",
                question="Which products drive revenue?",
                sql="SELECT p.product_name, sum(f.revenue) AS revenue FROM main.fact_order_items f JOIN main.dim_product p ON f.product_key = p.product_key GROUP BY 1 ORDER BY revenue DESC",
                finding="Monitor contributes $440 of the $515 completed sales revenue.",
                visualisation_intent="Rank products by revenue",
            ),
            AnalysisSpec(
                title="Units by category",
                question="How does unit volume vary by product category?",
                sql="SELECT p.category, sum(f.quantity) AS units FROM main.fact_order_items f JOIN main.dim_product p ON f.product_key = p.product_key GROUP BY 1 ORDER BY units DESC",
                finding="Each recorded product category contributes three units to completed sales.",
                visualisation_intent="Compare unit volume by category",
            ),
        ]
    )


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

            profile_rows = (
                con.execute(f"SUMMARIZE SELECT * FROM {relation}")
                .fetchdf()
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


# def materialize_analyses(
#     database: Path, model_names: list[str], analysis: AnalysisPlan
# ) -> list[tuple[AnalysisSpec, pd.DataFrame]]:
#     results = []
#     for spec in analysis.analyses:
#         frame, _ = execute_analysis_sql(database, spec.sql, model_names)
#         if frame.empty or len(frame.columns) == 0 or frame.isna().all().all():
#             raise ValueError(f"Analysis result is empty or unusable: {spec.title}")
#         if not frame.columns.is_unique:
#             raise ValueError(f"Analysis result has duplicate column names: {spec.title}")
#         results.append((spec, frame))
#     return results


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


def demo_plot(spec: AnalysisSpec) -> PlotSpec:
    if spec.title == "Monthly revenue":
        return PlotSpec(function="line", columns={"x": "order_month", "y": "revenue"})
    if spec.title == "Units by category":
        return PlotSpec(function="bar", columns={"x": "category", "y": "units"})
    column = "customer_name" if spec.title == "Revenue by customer" else "product_name"
    return PlotSpec(function="bar", columns={"x": column, "y": "revenue"})


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


def demo_metrics(database: Path) -> dict:
    """Existing dashboard cards for the bundled deterministic fixture only."""
    with duckdb.connect(str(database), read_only=True) as con:
        revenue, orders, customers, units = con.execute(
            "SELECT coalesce(sum(revenue),0), count(DISTINCT order_id), count(DISTINCT customer_key), coalesce(sum(quantity),0) FROM main.fact_order_items"
        ).fetchone()
    return {
        "Revenue": float(revenue),
        "Orders": orders,
        "Customers": customers,
        "Units": float(units),
    }


def result_metrics(results: list[tuple[AnalysisSpec, pd.DataFrame]]) -> dict:
    """Use scalar query results as cards, without assuming any business measure."""
    cards = {}
    for spec, frame in results:
        if len(frame) == 1:
            for column in frame.select_dtypes(include="number").columns:
                cards[f"{spec.title}: {column}"] = float(frame.iloc[0][column])
    return cards
