import json
from pathlib import Path

import duckdb
import pytest

from src.config import ReleaseSettings
from harness.fixtures import import_bundle


def make_fixture(tmp_path, name="fresh", fields=("position", "reading")):
    source = tmp_path / f"source_{name}"
    source.mkdir()
    model = f"fact_{name}"
    first, second = fields
    database = source / "data.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute(f'CREATE TABLE "{model}" AS SELECT * FROM (VALUES (1, 10.0),(2, NULL),(3, 30.0)) t("{first}","{second}")')
    outputs = [{"name": f, "description": f"Recorded {f}",
        "definition": {"kind": "copy", "inputs": [{"kind": "source", "dataset": "uploaded", "column": f}]}}
        for f in fields]
    plan = {"business_processes": [{"name": "observations", "description": "Recorded observations"}],
        "dimensions": [], "facts": [{"name": model, "business_process": "observations",
        "fact_type": "transaction", "grain": f"one row per {first}", "modelling_rationale": "Source observations",
        "source_datasets": ["uploaded"], "dimension_links": [], "output_fields": outputs, "grain_key_fields": [first]}],
        "assumptions": [], "unresolved_questions": []}
    settings = ReleaseSettings(run_seconds=60, max_model_calls=10, max_cost_usd=1.0,
        max_charts=12, max_analyses=8, max_output_tokens=2048).snapshot()
    bundle = {"database": "data.duckdb", "plan": plan, "request": f"Show {second} by {first}",
              "settings": settings, "controls": []}
    path = source / "bundle.json"
    path.write_text(json.dumps(bundle))
    root = tmp_path / "harness"
    import_bundle(root, path, name)
    proposal = {"analyses": [{"title": "Recorded values", "question": bundle["request"],
        "query": {"kind": "direct", "sql": f'SELECT * FROM "{model}"'}, "concept_id": "observations",
        "population_definition": "All recorded observations", "observation_rule": None,
        "rationale": "Explore recorded observations", "grain": f"one row per {first}",
        "dimensions": [{"field": first, "semantic_type": "ordinal", "description": "Recorded position"}],
        "measures": [{"field": second, "semantic_type": "numeric", "description": "Recorded value; unit unknown",
                      "additivity": "non_additive"}], "visualisation_goal": "Show the recorded values"}],
        "qualifications": [], "warnings": []}
    good = {"function": "scatter", "columns": {"x": first, "y": first, "color": second}, "filters": [], "options": {}}
    bad = {**good, "columns": {**good["columns"], "size": second}}
    script = [{"stage": "analysis", "responses": [{"tool": "query_data", "input": {"sql": f'SELECT count(*) AS n FROM "{model}"'}},
               {"input": proposal}]}, {"stage": "plotting", "responses": [{"input": bad}, {"input": good}]}]
    return {"root": root, "id": name, "database": database, "proposal": proposal,
            "good": good, "bad": bad, "script": script, "settings": settings}


@pytest.fixture
def fresh(tmp_path):
    return make_fixture(tmp_path)
