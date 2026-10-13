"""Read-only inspection tools for one accepted analytical result."""

from pathlib import Path

import duckdb
from strands import tool


class DuckDBResultTools:
    """
    Narrow inspection tools for one accepted analytical result.

    General result statistics are supplied through AcceptedAnalysisResult.profile.
    These tools exist only for information that is not economical or useful to
    precompute for every result.
    """

    def __init__(
        self,
        database: Path,
        relation: str,
        columns: set[str],
    ):
        self.database = database
        self.relation = relation
        self.columns = columns

    def _connect(self):
        return duckdb.connect(
            str(self.database),
            read_only=True,
            config={"enable_external_access": "false"},
        )

    def _field(self, field: str) -> str:
        if field not in self.columns:
            raise ValueError(
                f"Unknown result field: {field}. "
                f"Available fields: {sorted(self.columns)}"
            )

        return f'"{field.replace(chr(34), chr(34) * 2)}"'

    @tool
    def category_counts(
        self,
        field: str,
        limit: int = 20,
    ) -> dict:
        """
        Return the most common values and counts for one result field.

        Use this when the actual category values matter for visual encoding,
        such as legends, colours, labels, facets or controls.

        General statistics such as min/max, quartiles, null percentage and
        approximate cardinality are already provided in the result profile.
        """
        quoted = self._field(field)
        limit = min(max(limit, 1), 100)

        with self._connect() as con:
            rows = con.execute(
                f"""
                SELECT
                    {quoted} AS value,
                    count(*) AS count
                FROM {self.relation}
                GROUP BY {quoted}
                ORDER BY count(*) DESC
                LIMIT ?
                """,
                [limit],
            ).fetchall()

            distinct_count = con.execute(
                f"""
                SELECT count(DISTINCT {quoted})
                FROM {self.relation}
                """
            ).fetchone()[0]

        return {
            "field": field,
            "distinct_count": distinct_count,
            "returned_values": len(rows),
            "truncated": distinct_count > len(rows),
            "values": [
                {
                    "value": value,
                    "count": count,
                }
                for value, count in rows
            ],
        }
