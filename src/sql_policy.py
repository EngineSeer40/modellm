"""Validate generated dbt source before Jinja or dbt can execute it."""

from pathlib import Path

import yaml
from jinja2 import Environment, nodes

import sqlglot
from sqlglot import exp
from sqlglot.optimizer.scope import traverse_scope

POPULATION_CTE = "__modellm_population"



def _validate_calendar_generation(expression):
    """dbt-only exception for finite calendar series; keep arbitrary table functions blocked.

    sqlglot represents both DuckDB range() and generate_series() as GenerateSeries.
    Require explicit endpoints and a positive whole-day-or-coarser interval, avoiding
    accidental numeric/subsecond explosions. Source-derived spans retain the existing
    dbt memory, deadline and workspace limits; this is not a static row-count guarantee.
    """
    series = list(expression.find_all(exp.GenerateSeries))
    for generator in series:
        start, end, step = (generator.args.get(key) for key in ("start", "end", "step"))
        value = step.this if isinstance(step, exp.Interval) else None
        unit = step.args.get("unit") if isinstance(step, exp.Interval) else None
        if (start is None or end is None or not isinstance(value, exp.Literal)
                or not str(value.this).isdigit() or int(value.this) <= 0
                or unit is None or unit.name.upper() not in {"DAY", "WEEK", "MONTH", "QUARTER", "YEAR"}
                or any(bound.find(exp.GenerateSeries) for bound in (start, end))
                or any(str(literal.this).lower() in {"infinity", "-infinity", "inf", "-inf"}
                       for bound in (start, end) for literal in bound.find_all(exp.Literal))):
            raise ValueError("Calendar range/generate_series requires finite start/end bounds and a positive whole-day-or-coarser interval")
    for node in expression.walk():
        if isinstance(node, exp.UDTF):
            # Support UNNEST(series) in either the SELECT list or FROM clause only.
            args = [node.this] if isinstance(node, exp.Explode) else node.expressions
            if not isinstance(node, (exp.Explode, exp.Unnest)) or len(args) != 1 or not isinstance(args[0], exp.GenerateSeries):
                raise ValueError(f"SQL table function is not allowed: {node.sql(dialect='duckdb')}")
    return bool(series)



def validate_analysis_sql(sql: str, model_names: list[str], *, source_names=None,
                          allow_calendar_generation=False) -> str:
    """Accept one SELECT query; dbt may explicitly enable bounded calendar generators."""
    statements = sqlglot.parse(sql, read="duckdb")
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise ValueError("Analysis SQL must be one SELECT query")
    expression = statements[0]
    calendar_seen = _validate_calendar_generation(expression) if allow_calendar_generation else False
    if expression.find(exp.Into) or (not allow_calendar_generation and
            any(isinstance(node, exp.UDTF) for node in expression.walk())):
        raise ValueError("SQL must be a read-only SELECT without table functions")
    allowed = set(model_names)
    model_seen = calendar_seen
    # Resolve CTE scope so a nested alias cannot disguise an unrelated real table.
    tables = (source for scope in traverse_scope(expression) for source in scope.sources.values()
              if isinstance(source, exp.Table))
    for table in tables:
        if (allow_calendar_generation and isinstance(table.this, exp.GenerateSeries)
                and not table.catalog and not table.db):
            continue
        if not isinstance(table.this, exp.Identifier) or table.catalog:
            if allow_calendar_generation:
                raise ValueError(f"SQL relation/table function is not allowed: {table.sql(dialect='duckdb')}")
            raise ValueError("Analysis SQL may only read validated dbt models")
        name = table.name
        if table.db == "raw" and source_names is not None and name in source_names:
            model_seen = True
            continue
        if table.db not in ("", "main") or name not in allowed:
            raise ValueError(
                f"Analysis SQL references an unvalidated model: {table.sql()}"
            )
        model_seen = True
    if not model_seen:
        raise ValueError("Analysis SQL must read a validated dbt model")
    for function in expression.find_all(exp.Func):
        name = function.sql_name().lower()
        if name.startswith(("read_", "sqlite_", "postgres_", "http_")) or name in {
            "glob",
            "query",
            "query_table",
            "parquet_scan",
            "csv_scan",
        }:
            raise ValueError(f"Analysis SQL cannot call {name}")
    return sql.strip().rstrip(";")



def validate_variant_sql(sql: str) -> str:
    """Allow a variant to read only the application's shared population CTE."""
    statements = sqlglot.parse(sql, read="duckdb")
    if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.Union)):
        raise ValueError("Analysis variant must be one SELECT query")
    expression = statements[0]
    ctes = {cte.alias_or_name.lower() for cte in expression.find_all(exp.CTE)}
    if POPULATION_CTE in ctes:
        raise ValueError(f"Analysis variant cannot redefine {POPULATION_CTE}")
    population_seen = False
    for table in expression.find_all(exp.Table):
        if table.catalog or table.db:
            raise ValueError("Analysis variant may read only the shared population")
        if table.name.lower() in ctes:
            continue
        if table.name.lower() != POPULATION_CTE:
            raise ValueError(f"Analysis variant references another table: {table.sql()}")
        population_seen = True
    if not population_seen:
        raise ValueError("Analysis variant must read the shared population")
    return sql.strip().rstrip(";")



def normalized_sql(sql: str) -> str:
    return sqlglot.parse_one(sql, read="duckdb").sql(dialect="duckdb")



_JINJA = Environment()


def _relation(call, sources, models):
    if not isinstance(call, nodes.Call) or not isinstance(call.node, nodes.Name):
        raise ValueError("Only literal source() and ref() relation calls are allowed")
    if call.kwargs or call.dyn_args or call.dyn_kwargs or any(not isinstance(arg, nodes.Const) or type(arg.value) is not str for arg in call.args):
        raise ValueError("Relation arguments must be literal strings")
    args = [arg.value for arg in call.args]
    if call.node.name == "source" and len(args) == 2 and args[0] == "raw" and args[1] in sources:
        return f'raw."{args[1]}"'
    if call.node.name == "ref" and len(args) == 1 and args[0] in models:
        return f'main."{args[0]}"'
    raise ValueError("Relation must reference this run's raw source or planned model")


def render_relations_only(content, sources, models):
    parts = []
    for output in _JINJA.parse(content).body:
        if not isinstance(output, nodes.Output):
            raise ValueError("Jinja statements, macros and configuration are not allowed")
        for node in output.nodes:
            parts.append(node.data if isinstance(node, nodes.TemplateData) else _relation(node, sources, models))
    return "".join(parts)


def _literal(value, label):
    if type(value) is not str or any(not isinstance(child, nodes.Output) or
        any(not isinstance(node, nodes.TemplateData) for node in child.nodes)
        for child in _JINJA.parse(value).body):
        raise ValueError(f"{label} must be literal text, without Jinja")


def _mapping(value, allowed, label):
    if not isinstance(value, dict) or set(value) - set(allowed):
        raise ValueError(f"Unsupported {label} keys; allowed: {', '.join(allowed)}")


def _tests(tests, sources, models):
    if not isinstance(tests, list):
        raise ValueError("Tests must be a list")
    for test in tests:
        if isinstance(test, str) and test in {"unique", "not_null"}:
            continue
        if not isinstance(test, dict) or len(test) != 1:
            raise ValueError("Only built-in unique, not_null and relationships tests are allowed")
        name, args = next(iter(test.items()))
        if name not in {"unique", "not_null", "relationships"}:
            raise ValueError(f"Unsupported dbt test: {name}")
        args = args or {}
        if isinstance(args, dict) and set(args) == {"arguments"}:
            args = args["arguments"]
        _mapping(args, {"to", "field"} if name == "relationships" else set(), "test argument")
        if name == "relationships":
            if set(args) != {"to", "field"}:
                raise ValueError("relationships requires to and field")
            _literal(args["field"], "relationships.field")
            target = args["to"]
            if not isinstance(target, str):
                raise ValueError("relationships.to must be a literal ref call")
            parsed = _JINJA.parse(target if "{{" in target else "{{ " + target + " }}")
            if len(parsed.body) != 1 or not isinstance(parsed.body[0], nodes.Output) or len(parsed.body[0].nodes) != 1:
                raise ValueError("relationships.to must contain one ref call")
            call = parsed.body[0].nodes[0]
            if not isinstance(call, nodes.Call) or not isinstance(call.node, nodes.Name) or call.node.name != "ref":
                raise ValueError("relationships.to must reference a planned model")
            _relation(call, sources, models)


def validate_schema(document, sources, models):
    _mapping(document, {"version", "models"}, "schema")
    if document.get("version") != 2 or not isinstance(document.get("models"), list):
        raise ValueError("schema.yml requires version: 2 and a models list")
    seen = set()
    for model in document["models"]:
        _mapping(model, {"name", "description", "columns", "tests", "data_tests"}, "model")
        if model.get("name") not in models or model["name"] in seen:
            raise ValueError("Schema model must be a distinct planned model")
        seen.add(model["name"])
        if "description" in model:
            _literal(model["description"], "Model description")
        for key in ("tests", "data_tests"):
            _tests(model.get(key, []), sources, models)
        if not isinstance(model.get("columns", []), list):
            raise ValueError("Columns must be a list")
        for column in model.get("columns", []):
            _mapping(column, {"name", "description", "tests", "data_tests"}, "column")
            _literal(column.get("name"), "Column name")
            if "description" in column:
                _literal(column["description"], "Column description")
            for key in ("tests", "data_tests"):
                _tests(column.get(key, []), sources, models)


def validate_dbt_files(implementation, source_names):
    files = implementation.files
    models = {Path(file.path).stem for file in files if file.path.endswith(".sql")}
    if not models or not any(file.path.endswith(".yml") for file in files):
        raise ValueError("dbt implementation must include SQL models and schema.yml")
    seen = set()
    for file in files:
        path = Path(file.path)
        if (path.name != file.path or "\\" in file.path or path.suffix not in {".sql", ".yml"}
                or path.name == "sources.yml" or path.name in seen):
            raise ValueError(f"Invalid or duplicate dbt path: {file.path}")
        seen.add(path.name)
        try:
            if path.suffix == ".sql":
                from .data import identifier
                identifier(path.stem)
                rendered = render_relations_only(file.content, set(source_names), models)
                # dbt builds generated calendars; analytical queries still read validated models only.
                validate_analysis_sql(rendered, sorted(models), source_names=source_names,
                                      allow_calendar_generation=True)
            else:
                # All YAML strings are data except the narrowly validated relationships ref.
                validate_schema(yaml.safe_load(file.content), set(source_names), models)
        except ValueError as exc:
            raise ValueError(f"{file.path}: {exc}") from exc
