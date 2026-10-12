"""Read-only access to validated dbt models for the analytical agent and executor."""

import json
from pathlib import Path

import duckdb
import pandas as pd
import sqlglot
from sqlglot import exp
from strands import tool

MAX_ANALYSIS_ROWS = 5000


def validate_analysis_sql(sql: str, model_names: list[str]) -> str:
    """Accept one SELECT query whose table references are validated main models."""
    statements = sqlglot.parse(sql, read="duckdb")
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise ValueError("Analysis SQL must be one SELECT query")
    expression = statements[0]
    ctes = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    allowed = set(model_names)
    model_seen = False
    for table in expression.find_all(exp.Table):
        if not isinstance(table.this, exp.Identifier) or table.catalog:
            raise ValueError("Analysis SQL may only read validated dbt models")
        name = table.name
        if not table.db and name.lower() in ctes:
            continue
        if table.db not in ("", "main") or name not in allowed:
            raise ValueError(f"Analysis SQL references an unvalidated model: {table.sql()}")
        model_seen = True
    if not model_seen:
        raise ValueError("Analysis SQL must read a validated dbt model")
    for function in expression.find_all(exp.Func):
        name = function.sql_name().lower()
        if name.startswith(("read_", "sqlite_", "postgres_", "http_")) or name in {
            "glob", "query", "query_table", "parquet_scan", "csv_scan",
        }:
            raise ValueError(f"Analysis SQL cannot call {name}")
    return sql.strip().rstrip(";")


def normalized_sql(sql: str) -> str:
    return sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb")


def execute_analysis_sql(
    database: Path,
    sql: str,
    model_names: list[str],
    *,
    max_rows: int = MAX_ANALYSIS_ROWS,
    require_complete: bool = True,
) -> tuple[pd.DataFrame, bool]:
    statement = validate_analysis_sql(sql, model_names)
    with duckdb.connect(
        str(database), read_only=True, config={"enable_external_access": "false"}
    ) as con:
        frame = con.execute(
            f"SELECT * FROM ({statement}) AS analysis_result LIMIT {max_rows + 1}"
        ).df()
    truncated = len(frame) > max_rows
    if truncated and require_complete:
        raise ValueError(f"Analysis result exceeds {max_rows} rows; refine its SQL")
    return frame.head(max_rows), truncated


class DuckDBAnalysisTools:
    def __init__(self, database: Path, model_names: list[str]):
        self.database = database
        self.model_names = model_names
        self.inspected_queries: dict[str, bool] = {}

    @tool
    def list_models(self) -> dict:
        """List validated dbt models available for analysis."""
        return {"models": self.model_names}

    @tool
    def describe_model(self, model: str) -> dict:
        """Return schema and row count for a validated model."""
        if model not in self.model_names:
            raise ValueError(f"Unvalidated model: {model}")
        with duckdb.connect(str(self.database), read_only=True) as con:
            columns = con.execute(f'DESCRIBE SELECT * FROM main."{model}"').fetchall()
            rows = con.execute(f'SELECT count(*) FROM main."{model}"').fetchone()[0]
        return {
            "model": model,
            "rows": rows,
            "columns": [{"name": row[0], "type": row[1]} for row in columns],
        }

    @tool
    def query_data(self, sql: str, limit: int = 500) -> dict:
        """Preview a read-only SELECT over validated main models (up to 500 rows)."""
        limit = min(max(limit, 1), 500)
        frame, truncated = execute_analysis_sql(
            self.database, sql, self.model_names, max_rows=limit, require_complete=False
        )
        self.inspected_queries[normalized_sql(sql)] = not truncated
        return {
            "columns": list(frame.columns),
            "row_count": len(frame),
            "truncated": truncated,
            "rows": json.loads(frame.to_json(orient="records", date_format="iso")),
        }
