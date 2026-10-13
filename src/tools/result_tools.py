"""Read-only inspection tools for one accepted analytical result."""

from pathlib import Path
from string import Formatter
import re

import duckdb
from strands import tool

from src.core.contracts import (
    AnalysisFinding,
    AnalysisSpec,
    EvidenceAggregate,
    FindingEvidenceRequest,
    ResultFilter,
)


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
        spec: AnalysisSpec | None = None,
    ):
        self.database = database
        self.relation = relation
        self.columns = columns
        self.spec = spec

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

    def aggregate(self, request: FindingEvidenceRequest) -> list[dict]:
        """Execute a structured aggregate against only this accepted relation."""
        if not request.aggregates:
            raise ValueError("Evidence request needs an aggregate")
        if len(request.group_by) > 4:
            raise ValueError("Evidence grouping supports at most four fields")
        group_fields = [self._field(field) for field in request.group_by]
        if set(request.aggregates) & set(request.group_by):
            raise ValueError("Evidence aliases cannot replace group fields")
        select = [f"{quoted} AS {quoted}" for quoted in group_fields]
        measure_additivity = {
            measure.field: measure.additivity for measure in self.spec.measures
        } if self.spec else {}
        for alias, aggregate in request.aggregates.items():
            quoted_alias = f'"{alias.replace(chr(34), chr(34) * 2)}"'
            if aggregate.function == "count_rows":
                if aggregate.field is not None:
                    raise ValueError("count_rows cannot name a field")
                expression = "count(*)"
            else:
                if aggregate.field is None:
                    raise ValueError(f"{aggregate.function} needs a field")
                field = self._field(aggregate.field)
                if aggregate.function == "sum":
                    if measure_additivity.get(aggregate.field) != "additive":
                        raise ValueError(f"Cannot sum non-additive or undeclared measure {aggregate.field}")
                    if self.spec and self.spec.variant_field and not any(
                        item.field == self.spec.variant_field and item.operator in {"eq", "in"}
                        for item in request.filters
                    ):
                        raise ValueError("Summing mixed-grain result requires a variant filter")
                if aggregate.function == "count_distinct":
                    expression = f"count(DISTINCT {field})"
                else:
                    expression = f"{aggregate.function}({field})"
            select.append(f"{expression} AS {quoted_alias}")
        clauses = []
        parameters: list = []
        for selection in request.filters:
            field = self._field(selection.field)
            if selection.operator == "eq":
                if len(selection.values) != 1:
                    raise ValueError("eq filter needs one value")
                clauses.append(f"{field} = ?")
                parameters.append(selection.values[0])
            elif selection.operator == "in":
                if not selection.values:
                    raise ValueError("in filter needs values")
                clauses.append(f"{field} IN ({', '.join('?' for _ in selection.values)})")
                parameters.extend(selection.values)
            else:
                if selection.values:
                    raise ValueError("is_null filter takes no values")
                clauses.append(f"{field} IS NULL")
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        group = " GROUP BY " + ", ".join(group_fields) if group_fields else ""
        order = ""
        if request.order_by:
            if request.order_by not in request.aggregates:
                raise ValueError("Evidence order_by must name an aggregate alias")
            order = f' ORDER BY "{request.order_by.replace(chr(34), chr(34) * 2)}" DESC'
        query = (
            f"SELECT {', '.join(select)} FROM {self.relation}"
            f"{where}{group}{order} LIMIT {request.limit}"
        )
        with self._connect() as con:
            cursor = con.execute(query, parameters)
            names = [item[0] for item in cursor.description]
            return [dict(zip(names, row)) for row in cursor.fetchall()]

    @tool
    def grouped_summary(
        self,
        group_by: list[str],
        aggregates: dict[str, dict],
        filters: list[dict] | None = None,
        order_by: str | None = None,
        limit: int = 20,
    ) -> dict:
        """Summarize only this accepted result with checked fields and filters."""
        request = FindingEvidenceRequest(
            id="inspection",
            group_by=group_by,
            aggregates={key: EvidenceAggregate.model_validate(value) for key, value in aggregates.items()},
            filters=[ResultFilter.model_validate(value) for value in (filters or [])],
            order_by=order_by,
            limit=limit,
        )
        return {"rows": self.aggregate(request)}


def render_finding(finding: AnalysisFinding, tools: DuckDBResultTools) -> tuple[str, list[dict]]:
    """Bind displayed numerical outcomes to recomputed accepted-result evidence."""
    evidence = {}
    for request in finding.evidence_requests:
        if request.id in evidence:
            raise ValueError(f"Duplicate finding evidence ID: {request.id}")
        evidence[request.id] = tools.aggregate(request)
    rendered = []
    for statement in finding.statements:
        placeholders = set()
        for literal, field, _, _ in Formatter().parse(statement.template):
            if re.search(r"\d", literal):
                raise ValueError("Finding outcome text contains an unbound number")
            if field is not None:
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", field):
                    raise ValueError("Finding placeholder must be a simple name")
                placeholders.add(field)
        if placeholders != set(statement.bindings):
            raise ValueError("Finding placeholders and evidence bindings differ")
        values = {}
        for placeholder, binding in statement.bindings.items():
            rows = evidence.get(binding.evidence_id)
            if rows is None or binding.row_index >= len(rows):
                raise ValueError(f"Finding evidence row is unavailable: {binding.evidence_id}")
            if binding.column not in rows[binding.row_index]:
                raise ValueError(f"Finding evidence field is unavailable: {binding.column}")
            values[placeholder] = rows[binding.row_index][binding.column]
        rendered.append(statement.template.format_map(values))
    detail = [{"id": key, "rows": rows} for key, rows in evidence.items()]
    return "\n\n".join(rendered), detail
