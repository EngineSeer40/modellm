"""Read-only access to validated dbt models for the analytical agent and executor."""

import json
from pathlib import Path

from ..database import connect
import pandas as pd
from strands import tool

from ..sql_policy import validate_analysis_sql, normalized_sql

MAX_ANALYSIS_ROWS = 5000


def execute_analysis_sql(
    database: Path,
    sql: str,
    model_names: list[str],
    *,
    max_rows: int = MAX_ANALYSIS_ROWS,
    require_complete: bool = True,
) -> tuple[pd.DataFrame, bool]:
    statement = validate_analysis_sql(sql, model_names)
    with connect(database, read_only=True) as con:
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
        self.executed_queries: set[str] = set()

    @tool
    def list_models(self) -> dict:
        """List validated dbt models available for analysis."""
        return {"models": self.model_names}

    @tool
    def describe_model(self, model: str) -> dict:
        """Return schema and row count for a validated model."""
        if model not in self.model_names:
            raise ValueError(f"Unvalidated model: {model}")
        with connect(self.database, read_only=True) as con:
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
        self.executed_queries.add(normalized_sql(sql))

        return {
            "columns": list(frame.columns),
            "row_count": len(frame),
            "truncated": truncated,
            "rows": json.loads(frame.to_json(orient="records", date_format="iso")),
        }

    @tool(name="query_data")
    def query_data_small_preview(self, sql: str, limit: int = 20) -> dict:
        """Preview validated models; request a larger limit explicitly (up to 500).

        truncated describes this preview cap, not completeness of SQL containing LIMIT.
        """
        # Separate SDK tool signature preserves the original schema when the experiment is off.
        return self.query_data(sql, limit)
