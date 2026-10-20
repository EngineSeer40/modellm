import copy
from pathlib import Path

import pytest

from harness.fixtures import import_run, load_fixture, load_scenario, unregister, verify_fixture
from harness.io import file_hash, read, unseal
from harness.runner import execute_trial, load_plan, prepare_trial, run_worker


def test_saved_result_can_be_frozen_then_plotted_without_analysis(fresh):
    original = prepare_trial(fresh["root"], fresh["id"], script=fresh["script"])
    assert run_worker(original["plan"]).execution == "finished"
    source = Path(original["plan"]).parent
    before = file_hash(source / "modellm.duckdb")
    import_run(fresh["root"], source, "fixed-result")
    fixture, _ = load_fixture(fresh["root"], "fixed-result")
    frozen_profile = fixture.plotting[0].result.model_dump()
    trial = prepare_trial(fresh["root"], "fixed-result", mode="plotting", result_indexes=[0],
                          script=fresh["script"][1:])
    result = run_worker(trial["plan"])
    assert result.execution == "finished", result.failure
    assert result.internal_model_calls == 2
    boundary = read(Path(trial["plan"]).parent / "boundary.json")
    assert not boundary["proposals"]
    assert boundary["accepted"][0]["result"] == frozen_profile
    assert file_hash(source / "modellm.duckdb") == before
    assert fixture.source["record_sha256"] == file_hash(source / "result.json")


def test_exact_variant_scope_and_null_selection(fresh):
    proposal = copy.deepcopy(fresh["proposal"])
    spec = proposal["analyses"][0]
    spec["query"]["sql"] = "SELECT *, CASE position WHEN 1 THEN 'A' WHEN 3 THEN 'B' ELSE NULL END AS band FROM fact_fresh"
    spec["variant_field"] = "band"
    plots = []
    for value in ["A", "B", None]:
        plot = copy.deepcopy(fresh["good"])
        plot["columns"].pop("color")
        plot["filters"] = [{"field": "band", "operator": "is_null" if value is None else "eq", "values": [] if value is None else [value]}]
        plots.append({"stage": "plotting", "responses": [{"input": plot}]})
    source = prepare_trial(fresh["root"], fresh["id"], script=[{"stage": "analysis", "responses": [{"input": proposal}]}, *plots])
    assert run_worker(source["plan"]).execution == "finished"
    import_run(fresh["root"], Path(source["plan"]).parent, "variants")
    trial = prepare_trial(fresh["root"], "variants", mode="plotting", result_indexes=[0], view_ordinal=1, script=[plots[1]])
    result = run_worker(trial["plan"])
    assert result.execution == "finished", result.failure
    boundary = read(Path(trial["plan"]).parent / "boundary.json")
    assert len(boundary["schedule"]) == 3
    assert [s["ordinal"] for s in boundary["schedule"] if s["selected"]] == [1]
    assert boundary["plot_calls"][0]["selection"] == plots[1]["responses"][0]["input"]["filters"]
    assert boundary["schedule"][2]["selection"][0]["operator"] == "is_null"


def test_empty_result_placeholder_uses_no_plotting_model(fresh):
    proposal = copy.deepcopy(fresh["proposal"])
    proposal["analyses"][0]["query"]["sql"] += " WHERE FALSE"
    trial = prepare_trial(fresh["root"], fresh["id"], script=[{"stage": "analysis", "responses": [{"input": proposal}]}])
    result = run_worker(trial["plan"])
    assert result.execution == "finished", result.failure
    assert result.internal_model_calls == 1
    boundary = read(Path(trial["plan"]).parent / "boundary.json")
    assert boundary["schedule"][0]["empty_result"] and not boundary["plot_calls"]
    assert boundary["schedule"][0]["figure"] == "chart_0.json"


def test_unregister_preserves_originals_and_trial_input_locks(fresh):
    trial = prepare_trial(fresh["root"], fresh["id"], mode="analysis")
    unregister(fresh["root"], fresh["id"])
    assert fresh["database"].exists()
    assert (Path(trial["plan"]).parent / "input" / "modellm.duckdb").exists()
    with pytest.raises(ValueError, match="unregistered"):
        prepare_trial(fresh["root"], fresh["id"], mode="analysis")
