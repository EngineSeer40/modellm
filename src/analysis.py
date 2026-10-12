from pathlib import Path

import duckdb
import plotly.express as px

from .contracts import AnalysisPlan, ViewSpec
from .data import identifier


def demo_analysis() -> AnalysisPlan:
    return AnalysisPlan(views=[
        ViewSpec(title="Monthly revenue", table="sales", x="order_month", y="revenue", aggregate="sum", chart="line", finding="The monthly trend shows how recorded completed sales change across the available months."),
        ViewSpec(title="Revenue by customer", table="sales", x="customer_name", y="revenue", aggregate="sum", chart="bar", finding="Customer revenue highlights concentration, including sales assigned to an unknown customer."),
        ViewSpec(title="Revenue by product", table="sales", x="product_name", y="revenue", aggregate="sum", chart="bar", finding="Product revenue shows which recorded items contribute most."),
        ViewSpec(title="Units by category", table="sales", x="category", y="quantity", aggregate="sum", chart="bar", finding="Category units offer a volume comparison alongside revenue."),
    ])


def available_views(model_names: list[str]) -> dict[str, str]:
    views = {name: f'SELECT * FROM main."{identifier(name)}"' for name in model_names}
    if {"fact_order_items", "dim_customer", "dim_product"}.issubset(model_names):
        views["sales"] = """SELECT f.*, c.customer_name, c.segment, c.region,
            p.product_name, p.category, date_trunc('month', f.order_date) AS order_month
            FROM main.fact_order_items f
            JOIN main.dim_customer c ON f.customer_key = c.customer_key
            JOIN main.dim_product p ON f.product_key = p.product_key"""
    return views


def describe_views(database: Path, views: dict[str, str]) -> tuple[dict, dict]:
    schemas, summaries = {}, {}
    with duckdb.connect(str(database), read_only=True) as con:
        for name, sql in views.items():
            schemas[name] = {row[0]: row[1] for row in con.execute(f"DESCRIBE {sql}").fetchall()}
            summaries[name] = {"rows": con.execute(f"SELECT count(*) FROM ({sql})").fetchone()[0], "preview": con.execute(f"SELECT * FROM ({sql}) LIMIT 3").df().astype(str).to_dict("records")}
    return schemas, summaries


def make_charts(database: Path, views: dict[str, str], schemas: dict, analysis: AnalysisPlan) -> list[tuple[ViewSpec, object, object]]:
    charts = []
    with duckdb.connect(str(database), read_only=True) as con:
        for spec in analysis.views[:4]:
            if spec.table not in views or spec.x not in schemas[spec.table] or spec.y not in schemas[spec.table]:
                raise ValueError(f"Invalid view fields: {spec.table}.{spec.x}/{spec.y}")
            x, y = identifier(spec.x), identifier(spec.y)
            aggregate = {"sum": f"sum({y})", "avg": f"avg({y})", "count": f"count({y})", "count_distinct": f"count(distinct {y})"}[spec.aggregate]
            frame = con.execute(f"SELECT {x} AS x, {aggregate} AS y FROM ({views[spec.table]}) GROUP BY 1 ORDER BY 1 LIMIT 500").df()
            chart_fn = {"line": px.line, "bar": px.bar, "scatter": px.scatter, "pie": px.pie}[spec.chart]
            fig = chart_fn(frame, names="x", values="y", title=spec.title) if spec.chart == "pie" else chart_fn(frame, x="x", y="y", title=spec.title)
            fig.update_layout(xaxis_title=spec.x.replace("_", " ").title(), yaxis_title=f"{spec.aggregate} {spec.y}".replace("_", " ").title())
            charts.append((spec, fig, frame))
    return charts


def metrics(database: Path, model_names: list[str], plan=None) -> dict:
    if "fact_order_items" not in model_names:
        result = {}
        if plan is None:
            return result
        with duckdb.connect(str(database), read_only=True) as con:
            for fact in plan.target_model.facts:
                if fact.name not in model_names:
                    continue
                available = {row[0] for row in con.execute(f'DESCRIBE SELECT * FROM main."{identifier(fact.name)}"').fetchall()}
                for measure in fact.measures:
                    if measure.name in available and measure.additivity == "additive":
                        label = f"{fact.name}.{measure.name}"
                        value = con.execute(f'SELECT sum("{identifier(measure.name)}") FROM main."{identifier(fact.name)}"').fetchone()[0]
                        result[label] = float(value or 0)
        return result
    with duckdb.connect(str(database), read_only=True) as con:
        revenue, orders, customers, units = con.execute("SELECT coalesce(sum(revenue),0), count(DISTINCT order_id), count(DISTINCT customer_key), coalesce(sum(quantity),0) FROM main.fact_order_items").fetchone()
    return {"Revenue": float(revenue), "Orders": orders, "Customers": customers, "Units": float(units)}
