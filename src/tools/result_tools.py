"""Inspect one accepted result and retain exactly the evidence the agent saw."""

import copy
import json
import re
from pathlib import Path
from string import Formatter
from typing import Callable
from uuid import uuid4

import duckdb
from strands import tool

from src.core.contracts import (
    AnalysisFinding, AnalysisSpec, EvidenceRequest,
)


def quoted(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


class EvidenceRegistry:
    def __init__(self, acceptance_id: str, relation: str, on_register: Callable | None = None):
        self.acceptance_id = acceptance_id
        self.relation = relation
        self.records: dict[str, dict] = {}
        self.on_register = on_register

    def register(self, request, rows, columns, truncated, boundary_tie):
        evidence_id = "e_" + uuid4().hex[:12]
        record = {
            "id": evidence_id, "acceptance_id": self.acceptance_id,
            "relation": self.relation, "request": request.model_dump(),
            "columns": columns, "truncated": truncated,
            "boundary_tie": boundary_tie,
            "rows": [{"row_id": "r_" + uuid4().hex[:12], "values": row} for row in rows],
        }
        # Persist and return the same JSON representation (dates/decimals included).
        record = json.loads(json.dumps(record, default=str))
        self.records[evidence_id] = record
        if self.on_register:
            self.on_register(copy.deepcopy(record))
        return copy.deepcopy(record)

    def row(self, reference):
        record = self.records.get(reference.evidence_id)
        if not record or record["acceptance_id"] != self.acceptance_id or record["relation"] != self.relation:
            raise ValueError(f"Evidence is not registered for this accepted result: {reference.evidence_id}")
        for row in record["rows"]:
            if row["row_id"] == reference.row_id:
                return row["values"]
        raise ValueError(f"Unknown evidence row: {reference.row_id}")

    def snapshot(self):
        return copy.deepcopy(list(self.records.values()))


class DuckDBResultTools:
    def __init__(self, database: Path, relation: str, columns: set[str] | dict[str, str],
                 spec: AnalysisSpec | None = None, registry: EvidenceRegistry | None = None):
        self.database = database
        self.relation = relation
        self.columns = set(columns)
        self.column_types = columns if isinstance(columns, dict) else {}
        self.spec = spec
        self.registry = registry
        if registry and registry.relation != relation:
            raise ValueError("Evidence registry and result relation differ")

    def _connect(self):
        return duckdb.connect(str(self.database), read_only=True, config={"enable_external_access": "false"})

    def _field(self, field):
        if field not in self.columns:
            raise ValueError(f"Unknown result field: {field}. Available: {sorted(self.columns)}")
        return quoted(field)

    @tool
    def category_counts(self, field: str, limit: int = 20) -> dict:
        """Inspect categories for visual encoding in this accepted result only."""
        column = self._field(field)
        limit = min(max(limit, 1), 100)
        with self._connect() as con:
            rows = con.execute(
                f"SELECT {column}, count(*) AS n FROM {self.relation} GROUP BY {column} ORDER BY n DESC LIMIT ?",
                [limit + 1],
            ).fetchall()
        return {"field": field, "truncated": len(rows) > limit,
                "values": [{"value": value, "count": count} for value, count in rows[:limit]]}

    def inspect(self, request: EvidenceRequest) -> dict:
        if self.registry is None:
            raise ValueError("Evidence inspection needs a registry for the accepted result")
        group_by = request.group_by
        fields = group_by if request.kind == "summary" else request.fields
        if len(fields) != len(set(fields)):
            raise ValueError("Evidence fields must be distinct")
        select = [self._field(field) for field in fields]
        output_fields = set(fields)
        if request.kind == "summary":
            if output_fields & set(request.aggregates):
                raise ValueError("Aggregate aliases cannot replace grouping fields")
            if not self.column_types:
                with self._connect() as con:
                    self.column_types = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {self.relation}").fetchall()}
            additive = {m.field for m in self.spec.measures if m.additivity == "additive"} if self.spec else set()
            for alias, aggregate in request.aggregates.items():
                if not alias:
                    raise ValueError("Evidence aliases cannot be empty")
                if aggregate.function == "count_rows":
                    expression = "count(*)"
                else:
                    field = self._field(aggregate.field)
                    if aggregate.function in {"sum", "avg"}:
                        if not re.match(r"^(U?(TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|INT\d*|DECIMAL|DOUBLE|FLOAT|REAL)", self.column_types[aggregate.field], re.I):
                            raise ValueError(f"{aggregate.function} needs a numeric field: {aggregate.field}")
                    if aggregate.function == "sum" and aggregate.field not in additive:
                        raise ValueError(f"Cannot sum non-additive or undeclared measure {aggregate.field}")
                    expression = (f"count(DISTINCT {field})" if aggregate.function == "count_distinct"
                                  else f"{aggregate.function}({field})")
                select.append(f"{expression} AS {quoted(alias)}")
                output_fields.add(alias)
            discriminator = self.spec.variant_field if self.spec else None
            if discriminator and discriminator not in group_by and not any(
                f.field == discriminator and (
                    f.operator == "is_null" or f.operator in {"eq", "in"} and len(f.values) == 1
                ) for f in request.filters
            ):
                raise ValueError("A summary over alternate representations must select one representation or group by its discriminator")
        clauses, parameters = [], []
        for selection in request.filters:
            field = self._field(selection.field)
            if selection.operator == "eq":
                clauses.append(f"{field} = ?")
                parameters.append(selection.values[0])
            elif selection.operator == "in":
                clauses.append(f"{field} IN ({', '.join('?' for _ in selection.values)})")
                parameters.extend(selection.values)
            else:
                clauses.append(f"{field} IS NULL")
        order = []
        for sort in request.order_by:
            if sort.field not in output_fields:
                raise ValueError(f"Evidence sort must name an output field: {sort.field}; available: {sorted(output_fields)}")
            order.append(f"{quoted(sort.field)} {sort.direction.upper()} NULLS {sort.nulls.upper()}")
        sql = f"SELECT {', '.join(select)} FROM {self.relation}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        if group_by:
            sql += " GROUP BY " + ", ".join(self._field(field) for field in group_by)
        if order:
            sql += " ORDER BY " + ", ".join(order)
        sql += f" LIMIT {request.limit + 1}"
        with self._connect() as con:
            cursor = con.execute(sql, parameters)
            columns = {item[0]: str(item[1]) for item in cursor.description}
            rows = [dict(zip(columns, row)) for row in cursor.fetchall()]
        truncated = len(rows) > request.limit
        boundary_tie = bool(truncated and request.order_by and
                            rows[request.limit - 1][request.order_by[0].field] == rows[request.limit][request.order_by[0].field])
        return self.registry.register(request, rows[:request.limit], columns, truncated, boundary_tie)

    @tool
    def inspect_result(self, request: EvidenceRequest) -> dict:
        """Inspect accepted-result rows or summaries and store evidence for findings.

        Use rows to cite existing values without recalculation. Use summary for
        supported aggregates. Sort with separate field and asc/desc direction;
        a sort field is a projected/group field or aggregate alias. The returned
        evidence ID and row IDs are the only valid references in final findings.
        A truncated result or boundary tie does not establish a unique winner.
        """
        # Strands validates nested inputs, then passes model_dump() dictionaries.
        return self.inspect(EvidenceRequest.model_validate(request))


def render_finding(finding: AnalysisFinding, registry: EvidenceRegistry) -> tuple[str, list[dict]]:
    """Resolve stored evidence; never re-execute a final finding's query."""
    rendered, used, errors = [], set(), []
    for index, statement in enumerate(finding.statements, 1):
        prefix = f"Statement {index}"
        statement_errors = []
        placeholders = set()
        try:
            parts = list(Formatter().parse(statement.template))
        except ValueError as exc:
            errors.append(f"{prefix}: invalid template: {exc}")
            continue
        for _, field, format_spec, conversion in parts:
            if field is not None:
                if not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", field) or format_spec or conversion:
                    statement_errors.append(f"{prefix}: placeholder {field!r} must be a simple name without formatting or conversions")
                placeholders.add(field)
        if placeholders != set(statement.bindings):
            statement_errors.append(
                f"{prefix}: placeholders without bindings {sorted(placeholders - set(statement.bindings))}; "
                f"bindings without placeholders {sorted(set(statement.bindings) - placeholders)}"
            )
        rows = {}
        for alias, ref in statement.selected_rows.items():
            try:
                rows[alias] = registry.row(ref)
            except ValueError as exc:
                statement_errors.append(f"{prefix}, selected alias {alias!r}: {exc}")
        referenced_rows = {binding.row for binding in statement.bindings.values()}
        selected_aliases = set(statement.selected_rows)
        if referenced_rows != selected_aliases:
            statement_errors.append(
                f"{prefix}: unused selected aliases {sorted(selected_aliases - referenced_rows)}; "
                f"undeclared bound aliases {sorted(referenced_rows - selected_aliases)}. "
                "Every selected alias must be used by a binding; remove unused selections or bind their relevant evidence values."
            )
        values = {}
        for placeholder, binding in statement.bindings.items():
            if binding.row not in rows:
                continue  # Missing aliases or evidence are reported above.
            if binding.column not in rows[binding.row]:
                statement_errors.append(f"{prefix}, binding {placeholder!r}: missing evidence column {binding.column!r} on alias {binding.row!r}")
            else:
                values[placeholder] = rows[binding.row][binding.column]
        if statement_errors:
            errors.extend(statement_errors)
            continue
        rendered.append(statement.template.format_map(values))
        used.update(ref.evidence_id for ref in statement.selected_rows.values())
    if errors:
        raise ValueError("Finding evidence validation failed:\n" + "\n".join(errors))
    return "\n\n".join(rendered), [copy.deepcopy(registry.records[key]) for key in registry.records if key in used]
