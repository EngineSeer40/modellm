import copy
from pathlib import Path

from harness import cases
from harness.fixtures import import_run
from harness.io import read
from harness.runner import load_plan, prepare_trial, run_worker


def test_mixed_unit_case_renders_variants_and_assesses_actual_figures(fresh, monkeypatch):
    proposal = copy.deepcopy(fresh["proposal"])
    spec = proposal["analyses"][0]
    spec["query"]["sql"] = "SELECT position, reading AS avg_cu_ppm, reading/10 AS avg_fe_pct, CASE position WHEN 1 THEN 'A' ELSE 'B' END AS band FROM fact_fresh"
    spec["variant_field"] = "band"
    spec["measures"] = [{**spec["measures"][0], "field": field} for field in ("avg_cu_ppm", "avg_fe_pct")]
    plots = [{"function": "line", "columns": {"x": "position", "y": fields},
        "filters": [{"field": "band", "operator": "eq", "values": [band]}], "options": {}}
        for band, fields in [("A", ["avg_cu_ppm"]), ("B", ["avg_cu_ppm", "avg_fe_pct"])]]
    original = prepare_trial(fresh["root"], fresh["id"], script=[
        {"stage": "analysis", "responses": [{"input": proposal}]},
        *[{"stage": "plotting", "responses": [{"input": plot}]} for plot in plots]])
    assert run_worker(original["plan"]).execution == "finished"
    source = Path(original["plan"]).parent
    import_run(fresh["root"], source, "variants")
    index = read(Path(cases.__file__).with_name("index.json"))
    case = copy.deepcopy(next(c for c in index["cases"] if c["id"] == "mixed-unit-variant-inconsistency"))
    assert case["evaluation_kind"] == "analytical" and len(case["replay_plots"]) == 2
    case.update(runs=[source.name], result_indexes=[0], unit_result_index=0,
        units={"avg_cu_ppm": "ppm", "avg_fe_pct": "weight_percent"},
        replay_plots=[{"result_index": 0, "pointer": f"/plot_attempts/{i}/plot", "mechanical_expectation": "rendered"} for i in range(2)])
    def case_read(path):
        return {"cases": [case]} if Path(path).name == "index.json" else read(path)
    monkeypatch.setattr(cases, "read", case_read)
    prepared = cases.prepare_case(fresh["root"], case["id"], "variants")
    result = run_worker(prepared["plan"])
    assert result.execution == "finished", result.failure
    checks = {c.check_id: c for c in result.checks}
    prefix = "case:mixed-unit-variant-inconsistency:"
    assert checks[prefix + "shared_axis_units"].status == "fail"
    assert checks[prefix + "variant_measure_consistency"].status == "fail"
    boundary = read(Path(prepared["plan"]).parent / "boundary.json")
    assert [o["kind"] for o in boundary["replay"]] == ["plot", "plot"]
    assert all((Path(prepared["plan"]).parent / c["figure"]).is_file() for c in boundary["plot_calls"])

    # A complete same-unit/same-measure control passes the scoped checks.
    plan, _ = load_plan(prepared["plan"])
    corrected = copy.deepcopy(plan.recorded_plots)
    corrected[1]["plot"]["columns"]["y"] = ["avg_cu_ppm"]
    control = prepare_trial(fresh["root"], "variants", mode="replay", backend="recorded",
        recorded_plots=corrected, case_evaluation=plan.case_evaluation.model_dump())
    result = run_worker(control["plan"])
    checks = {c.check_id: c for c in result.checks}
    assert checks[prefix + "shared_axis_units"].status == "pass"
    assert checks[prefix + "variant_measure_consistency"].status == "pass"
    assert checks[prefix + "assessment"].status == "needs_review"

    # One rendered view cannot establish a complete two-variant control.
    incomplete = prepare_trial(fresh["root"], "variants", mode="replay", backend="recorded",
        recorded_plots=corrected[:1], case_evaluation=plan.case_evaluation.model_dump())
    checks = {c.check_id: c for c in run_worker(incomplete["plan"]).checks}
    assert checks[prefix + "shared_axis_units"].status == "not_observed"
    assert checks[prefix + "variant_measure_consistency"].status == "not_observed"


def test_evidence_case_does_not_silently_execute_acceptance(fresh, monkeypatch):
    original = prepare_trial(fresh["root"], fresh["id"], script=fresh["script"])
    assert run_worker(original["plan"]).execution == "finished"
    source = Path(original["plan"]).parent
    import_run(fresh["root"], source, "evidence")
    case = {"id": "provider-failure-controls", "runs": [source.name], "evaluation_kind": "evidence",
        "expectation": "Assess provider failure classification", "result_indexes": [0]}
    monkeypatch.setattr(cases, "read", lambda path: {"cases": [case]} if Path(path).name == "index.json" else read(path))
    prepared = cases.prepare_case(fresh["root"], case["id"], "evidence")
    result = run_worker(prepared["plan"])
    boundary = read(Path(prepared["plan"]).parent / "boundary.json")
    assert boundary["replay"][0]["kind"] == "evidence"
    assert result.internal_model_calls == 0
    assert not (Path(prepared["plan"]).parent / "replay-acceptance.duckdb").exists()
    assert next(c for c in result.checks if c.check_id == "case:provider-failure-controls:assessment").status == "not_observed"
