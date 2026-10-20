"""Run a synthetic baseline → regressing candidate → revised candidate → review stop."""

import argparse
import json
from pathlib import Path

import duckdb

from src.config import ReleaseSettings
from harness.fixtures import create_scenario, import_bundle
from harness.io import identifier, write
from harness.loop import create_experiment, create_iteration, evaluate_iteration, record_decision


def demonstrate(root):
    root = Path(root).resolve()
    name = identifier("demo")
    folder = root / "outputs" / "demonstrations" / name
    folder.mkdir(parents=True)
    with duckdb.connect(str(folder / "source.duckdb")) as con:
        con.execute("CREATE TABLE fact_signal AS SELECT * FROM (VALUES (1,10.0),(2,NULL),(3,30.0)) t(offset_value,strength)")
    fields = [{"name": f, "description": f"Synthetic {f}; physical unit unspecified",
               "definition": {"kind": "copy", "inputs": [{"kind": "source", "dataset": "observations", "column": f}]}}
              for f in ("offset_value", "strength")]
    plan = {"business_processes": [{"name": "observations", "description": "Synthetic observations"}],
        "dimensions": [], "facts": [{"name": "fact_signal", "business_process": "observations", "fact_type": "transaction",
            "grain": "one row per offset", "modelling_rationale": "Synthetic observations", "source_datasets": ["observations"],
            "dimension_links": [], "output_fields": fields, "grain_key_fields": ["offset_value"]}],
        "assumptions": [], "unresolved_questions": ["Physical measurement units are not supplied"]}
    settings = ReleaseSettings(run_seconds=60, max_model_calls=10, max_cost_usd=1.0, max_output_tokens=2048,
                               max_analyses=8, max_charts=12).snapshot()
    write(folder / "bundle.json", {"database": "source.duckdb", "plan": plan, "request": "Show recorded strength by offset",
                                  "settings": settings, "controls": []}, exclusive=True)
    import_bundle(root, folder / "bundle.json", name)
    checks = [{"kind": "visible_measure", "id": "visible_measure", "expected": True,
               "explanation": "Synthetic request explicitly asks to see the returned strength measure"}]
    problem, control = name + "-problem", name + "-control"
    for scenario in (problem, control):
        create_scenario(root, name, scenario, "Show recorded strength by offset", "Synthetic loop/control demonstration", checks)
    proposal = {"analyses": [{"title": "Synthetic observations", "question": "How does strength vary with offset?",
        "query": {"kind": "direct", "sql": "SELECT * FROM fact_signal"}, "concept_id": "signal",
        "population_definition": "All synthetic observations", "observation_rule": None,
        "rationale": "Inspect the synthetic measurements", "grain": "one row per offset",
        "dimensions": [{"field": "offset_value", "semantic_type": "ordinal", "description": "Synthetic position"}],
        "measures": [{"field": "strength", "semantic_type": "numeric", "description": "Synthetic measurement; unit unknown", "additivity": "non_additive"}],
        "visualisation_goal": "Show the strength measure by offset"}], "qualifications": [], "warnings": ["Units unspecified"]}
    wrong = {"function": "scatter", "columns": {"x": "offset_value", "y": "offset_value"}, "filters": [], "options": {}}
    good = {**wrong, "columns": {**wrong["columns"], "color": "strength"}}
    def script(problem_case=False, reject_all=False):
        outputs = [wrong, good] if problem_case else [good]
        if reject_all:
            outputs = [good]*4
        return [{"stage": "analysis", "responses": [{"input": proposal}]},
                {"stage": "plotting", "responses": [{"input": p} for p in outputs]}]
    create_experiment(root, {"experiment_id": name, "problem": "Synthetic visible-measure obligation with protected control",
        "scenarios": [problem, control], "mode": "pipeline", "backend": "scripted",
        "required_checks": ["input_integrity", "execution", "scheduled_views", "visible_measure"],
        "protected_checks": ["scheduled_views", "visible_measure"], "human_checks": ["analytical_meaning", "coverage"],
        "max_iterations": 3, "max_trials": 8, "max_model_calls": 40, "max_cost_usd": 1.0, "max_seconds": 600,
        "max_no_progress": 3, "offline_tests": ["harness/tests/test_runtime.py"]})
    baseline = create_iteration(root, name, None, {"hypothesis": "Record a completed figure that omits the requested measure",
        "boundary": "Analysis result → encoding", "scripts_by_scenario": {problem: script(True), control: script()}})
    baseline_gate = evaluate_iteration(root, name, baseline["iteration_id"])
    regressed = create_iteration(root, name, baseline["iteration_id"], {"hypothesis": "A blanket plot rejection should fail protected controls",
        "boundary": "encoding validation", "variant": {"variant_id": "blanket-rejection", "plotting_validator": "harness.variants.examples:reject_every_plot"},
        "scripts_by_scenario": {problem: script(reject_all=True), control: script(reject_all=True)}})
    regressed_gate = evaluate_iteration(root, name, regressed["iteration_id"])
    revised = create_iteration(root, name, regressed["iteration_id"], {"hypothesis": "A compatible visible-measure validator returns useful local feedback",
        "boundary": "encoding validation → correction", "variant": {"variant_id": "visible-measure", "schema_factory": "harness.variants.examples:require_visible_measure"},
        "scripts_by_scenario": {problem: script(True), control: script()}})
    revised_gate = evaluate_iteration(root, name, revised["iteration_id"])
    record_decision(root, name, revised["iteration_id"], "Mechanical target improved in a scripted test; analytical correctness needs human review",
                    revised_gate["comparisons"])
    result = {"experiment_id": name, "classification": "synthetic scripted wiring demonstration; no model-quality/cost/cache measurement",
        "baseline": baseline_gate, "regressing_candidate": regressed_gate, "revised_candidate": revised_gate}
    if baseline_gate["disposition"] != "revise" or regressed_gate["disposition"] != "revise" or revised_gate["disposition"] != "needs_review":
        raise AssertionError(f"Unexpected demonstration gates: {[g['disposition'] for g in (baseline_gate,regressed_gate,revised_gate)]}")
    write(folder / "demo.json", result, exclusive=True)
    return {"report": str(folder / "demo.json"), "experiment_id": name,
            "gates": [g["disposition"] for g in (baseline_gate, regressed_gate, revised_gate)]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="harness")
    print(json.dumps(demonstrate(parser.parse_args().root)))
