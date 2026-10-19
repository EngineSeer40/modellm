"""Validate plans against source evidence and the resulting dbt model interfaces."""

from pathlib import Path

from .core.contracts import ConceptualPlan, FieldRef, ModelOutput, ValidationIssue
from .database import connect
from .data import identifier


def field_inputs(field: ModelOutput) -> list[FieldRef]:
    return field.definition.inputs


def ref_container(ref: FieldRef) -> tuple[str, str]:
    return (ref.kind, ref.dataset if ref.kind == "source" else ref.model)


def plan_issues(
    plan: ConceptualPlan, profiles: dict, relationships: list[dict] | None = None
) -> list[ValidationIssue]:
    """Resolve declarations against this run; do not interpret transformation prose."""
    issues: list[ValidationIssue] = []
    target = plan
    models = target.dimensions + target.facts
    sources = {
        name: {col["name"] for col in p["columns"]} for name, p in profiles.items()
    }
    outputs = {m.name: {f.name for f in m.output_fields} for m in models}
    dimensions = {m.name: m for m in target.dimensions}
    processes = {p.name for p in target.business_processes}
    field_graph: dict[tuple[str, str], set[tuple[str, str]]] = {}
    model_graph: dict[str, set[str]] = {m.name: set() for m in models}

    def issue(code, path, message, expected=None, observed=None):
        issues.append(
            ValidationIssue(
                boundary="conceptual",
                code=code,
                path=path,
                message=message,
                expected=expected,
                observed=observed,
            )
        )

    def reference(ref, path, declared):
        if ref.kind == "source":
            if ref.dataset not in sources:
                issue(
                    "unknown_source",
                    path,
                    f"Unknown source {ref.dataset}",
                    sorted(sources),
                    ref.dataset,
                )
            elif ref.column not in sources[ref.dataset]:
                issue(
                    "missing_source_field",
                    path,
                    f"Missing source field {ref.dataset}.{ref.column}",
                    sorted(sources[ref.dataset]),
                    ref.column,
                )
            if ref.dataset not in declared:
                issue(
                    "undeclared_source",
                    path,
                    f"Source {ref.dataset} is outside the declared source scope",
                    declared,
                    ref.dataset,
                )
        elif ref.model not in outputs or ref.column not in outputs[ref.model]:
            issue(
                "missing_planned_field",
                path,
                f"Missing planned output {ref.model}.{ref.column}",
                sorted(outputs.get(ref.model, [])),
                ref.column,
            )

    def output(model, name, path):
        if name not in outputs.get(model, set()):
            issue(
                "missing_planned_field",
                path,
                f"Missing planned output {model}.{name}",
                sorted(outputs.get(model, [])),
                name,
            )

    if len(outputs) != len(models):
        issue("duplicate_model", "models", "Model names must be distinct")
    if len(processes) != len(target.business_processes):
        issue(
            "duplicate_process", "business_processes", "Process names must be distinct"
        )
    if not target.facts:
        issue("missing_fact", "facts", "The plan has no fact table")
    for process in target.business_processes:
        if not any(f.business_process == process.name for f in target.facts):
            issue("unimplemented_process", process.name, "Business process has no fact")
    for model in models:
        if len(outputs[model.name]) != len(model.output_fields):
            issue("duplicate_output", model.name, "Each output must be declared once")
        for name in model.source_datasets:
            if name not in sources:
                issue(
                    "unknown_source",
                    model.name,
                    f"Unknown source {name}",
                    sorted(sources),
                    name,
                )
        for field in model.output_fields:
            node = (model.name, field.name)
            field_graph[node] = set()
            for index, ref in enumerate(field_inputs(field)):
                reference(
                    ref,
                    f"{model.name}.output_fields.{field.name}.definition.inputs[{index}]",
                    model.source_datasets,
                )
                if ref.kind == "model":
                    field_graph[node].add((ref.model, ref.column))
                    if ref.model != model.name:
                        model_graph[model.name].add(ref.model)
        for key in model.grain_key_fields:
            output(model.name, key, f"{model.name}.grain_key_fields")
    for dimension in target.dimensions:
        for key in dimension.natural_key_fields + (
            [dimension.surrogate_key] if dimension.surrogate_key else []
        ):
            output(dimension.name, key, f"{dimension.name}.identity")
    for fact in target.facts:
        fact_fields = {field.name: field for field in fact.output_fields}

        def local_sources(column, trail=()):
            """Sources for a local pre-join value; external models need resolution."""
            if column in trail or column not in fact_fields:
                return None
            found = set()
            for ref in field_inputs(fact_fields[column]):
                if ref.kind == "source":
                    found.add(ref.dataset)
                elif ref.model == fact.name:
                    upstream = local_sources(ref.column, (*trail, column))
                    if upstream is None:
                        return None
                    found.update(upstream)
                else:
                    return None
            return found

        if fact.business_process not in processes:
            issue(
                "unknown_process",
                fact.name,
                f"Unknown process {fact.business_process}",
                sorted(processes),
                fact.business_process,
            )
        for index, link in enumerate(fact.dimension_links):
            path = f"{fact.name}.dimension_links[{index}]"
            dimension = dimensions.get(link.dimension_model)
            if dimension is None:
                issue(
                    "unknown_dimension",
                    path,
                    f"Unknown dimension {link.dimension_model}",
                    sorted(dimensions),
                    link.dimension_model,
                )
            model_graph[fact.name].add(link.dimension_model)
            for pair in link.key_pairs:
                output(fact.name, pair.fact_field, path)
                output(link.dimension_model, pair.dimension_field, path)
            for pi, match_path in enumerate(link.source_paths):
                pp = f"{path}.source_paths[{pi}]"
                if match_path.source_dataset not in fact.source_datasets:
                    issue(
                        "undeclared_source",
                        pp,
                        "Path must start at a declared fact source",
                        fact.source_datasets,
                        match_path.source_dataset,
                    )
                seen = {("source", match_path.source_dataset)}
                destinations = set()
                for si, step in enumerate(match_path.steps):
                    sp = f"{pp}.steps[{si}]"
                    destinations = {ref_container(pair.right) for pair in step.pairs}
                    if len(destinations) != 1:
                        issue(
                            "multiple_destinations",
                            sp,
                            "A match step must have one destination container",
                        )
                    for pair in step.pairs:
                        local_input = (
                            pair.left.kind == "model" and pair.left.model == fact.name
                            and local_sources(pair.left.column) == {match_path.source_dataset}
                        )
                        if ref_container(pair.left) not in seen and not local_input:
                            issue(
                                "disconnected_path",
                                sp,
                                "Left endpoint must be in the source, an already reached container, or a fact-local output derived from this source before dimension resolution",
                            )
                        for ref in (pair.left, pair.right):
                            reference(
                                ref,
                                sp,
                                fact.source_datasets
                                + (dimension.source_datasets if dimension else []),
                            )
                            if ref.kind == "model" and ref.model != fact.name:
                                model_graph[fact.name].add(ref.model)
                    seen.update(destinations)
                if destinations != {("model", link.dimension_model)}:
                    issue(
                        "wrong_path_destination",
                        pp,
                        f"Path must end at {link.dimension_model}",
                    )

    def cycles(graph, code):
        visiting, visited = set(), set()

        def visit(node, trail):
            if node in visiting:
                issue(code, "models", f"Dependency cycle: {trail + [node]}")
                return
            if node in visited or node not in graph:
                return
            visiting.add(node)
            for dependency in graph[node]:
                visit(dependency, trail + [node])
            visiting.remove(node)
            visited.add(node)

        for node in graph:
            visit(node, [])

    cycles(field_graph, "field_dependency_cycle")
    cycles(model_graph, "model_dependency_cycle")
    return issues


def validate_built_models(
    database: Path, plan: ConceptualPlan, built_schemas: dict[str, dict[str, str]]
) -> list[str]:
    """Check run-declared output interfaces and optional declared grain keys."""
    issues: list[str] = []
    models = plan.dimensions + plan.facts
    for model in models:
        actual = set(built_schemas.get(model.name, {}))
        required = {field.name for field in model.output_fields}
        missing = required - actual
        if missing:
            issues.append(f"{model.name} omits planned outputs: {sorted(missing)}")
        keys = model.grain_key_fields
        if keys and set(keys) <= actual:
            table = f'main."{identifier(model.name)}"'
            fields = ", ".join('"' + field.replace('"', '""') + '"' for field in keys)
            with connect(database, read_only=True) as con:
                duplicates = con.execute(
                    f"SELECT {fields}, count(*) FROM {table} GROUP BY {fields} HAVING count(*) > 1 LIMIT 5"
                ).fetchall()
            if duplicates:
                samples = []
                with connect(database, read_only=True) as con:
                    for row in duplicates:
                        sample = dict(zip([*keys, "observed_row_count"], row))
                        predicate = " AND ".join('"' + key.replace('"', '""') + '" IS NOT DISTINCT FROM ?' for key in keys)
                        cursor = con.execute(f"SELECT * FROM {table} WHERE {predicate} LIMIT 3", list(row[:-1]))
                        columns = [item[0] for item in cursor.description]
                        sample["model_rows"] = [dict(zip(columns, values)) for values in cursor.fetchall()]
                        samples.append(sample)
                issues.append(f"{model.name} duplicates declared grain keys {keys}; observed examples: {samples}")
    for fact in plan.facts:
        fact_columns = set(built_schemas.get(fact.name, {}))
        for link in fact.dimension_links:
            dimension_columns = set(built_schemas.get(link.dimension_model, {}))
            for pair in link.key_pairs:
                if pair.fact_field not in fact_columns:
                    issues.append(f"{fact.name} omits fact link output {pair.fact_field}")
                if pair.dimension_field not in dimension_columns:
                    issues.append(f"{link.dimension_model} omits join key {pair.dimension_field} required by {fact.name}")
            for path in link.source_paths:
                for step in path.steps:
                    for pair in step.pairs:
                        for endpoint in (pair.left, pair.right):
                            if endpoint.kind == "model" and endpoint.column not in built_schemas.get(endpoint.model, {}):
                                issues.append(f"{endpoint.model} omits match output {endpoint.column} required by {fact.name}")
    return sorted(set(issues))
