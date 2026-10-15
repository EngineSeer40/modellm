"""Validate generated dbt source before Jinja or dbt can execute it."""

from pathlib import Path

import yaml
from jinja2 import Environment, nodes

from .tools.analysis_tools import validate_analysis_sql

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
