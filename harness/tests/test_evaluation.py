import copy
from pathlib import Path

import pytest

from harness.contracts import CheckResult, TrialResult
from harness.fixtures import create_scenario
from harness.io import now, read, write
from harness.reporting import compare, effective_checks
from harness.runner import load_plan, prepare_trial, run_worker


def test_result_checks_preserve_failure_before_later_pass(fresh, monkeypatch):
    create_scenario(fresh["root"], fresh["id"], "two-results", "Show readings", "Check each result",
        [{"id": "three_rows", "kind": "row_count", "expected": 3, "explanation": "Retain the population"}])
    proposal = copy.deepcopy(fresh["proposal"])
    proposal["analyses"].append(copy.deepcopy(proposal["analyses"][0]))
    proposal["analyses"][0]["query"]["sql"] += " WHERE position=1"
    prepared = prepare_trial(fresh["root"], "two-results", script=[
        {"stage": "analysis", "responses": [{"input": proposal}]},
        *[{"stage": "plotting", "responses": [{"input": fresh["good"]}]} for _ in range(2)]])
    result = run_worker(prepared["plan"])
    assert result.execution == "finished", result.failure
    checks = effective_checks(fresh["root"], prepared["trial_id"])
    assert checks["three_rows:result:result_00"]["status"] == "fail"
    assert checks["three_rows:result:result_01"]["status"] == "pass"
    assert checks["three_rows"]["status"] == "fail"
    from harness import evaluation
    load_frame = evaluation.load_result_frame
    def unreadable_first(database, accepted):
        if accepted.result_id == "result_00":
            raise OSError("Synthetic unreadable result")
        return load_frame(database, accepted)
    monkeypatch.setattr(evaluation, "load_result_frame", unreadable_first)
    directory = Path(prepared["plan"]).parent
    plan, _ = load_plan(prepared["plan"])
    observed = evaluation.evaluate(plan, result, read(directory / "boundary.json"), directory / "modellm.duckdb", True)
    assert next(c for c in observed if c.check_id == "three_rows").status == "not_observed"


def test_identical_geographic_trials_have_no_false_changes(fresh):
    script = [{"stage": "analysis", "responses": [{"input": fresh["proposal"]}]},
        {"stage": "plotting", "responses": [{"input": {"function": "scatter_map",
        "columns": {"lat": "position", "lon": "reading"}, "filters": [], "options": {}}}]}]
    ids = []
    for _ in range(2):
        prepared = prepare_trial(fresh["root"], fresh["id"], script=script)
        result = run_worker(prepared["plan"])
        assert result.execution == "finished", result.failure
        ids.append(prepared["trial_id"])
    comparison = compare(fresh["root"], *ids)
    assert comparison["disposition"] == "compared"
    assert not comparison["regressions"] and not comparison["improvements"]
    assert sum(k.startswith("coordinate:") for k in comparison["baseline_checks"]) == 2


def test_legacy_duplicate_check_ids_fail_explicitly(tmp_path):
    check = CheckResult(check_id="same", dimension="shape", status="fail", explanation="First observation")
    result = TrialResult(trial_id="legacy", execution="finished", ended_at=now(),
        checks=[check, check.model_copy(update={"status": "pass"})])
    write(tmp_path / "outputs/trials/legacy/trial.json", result)
    with pytest.raises(ValueError, match="Duplicate check ID"):
        effective_checks(tmp_path, "legacy")


def test_duplicate_scenario_obligation_ids_are_rejected(fresh):
    check = {"id": "same", "kind": "row_count", "expected": 3}
    with pytest.raises(ValueError, match="IDs must be unique"):
        create_scenario(fresh["root"], fresh["id"], "duplicate", "Show readings", "Review", [check, check])


def test_obligation_without_matching_result_is_unobserved(fresh):
    create_scenario(fresh["root"], fresh["id"], "missing-result", "Show readings", "Review missing scope",
        [{"id": "missing", "kind": "row_count", "result_id": "result_99", "expected": 3, "explanation": "Required result"}])
    prepared = prepare_trial(fresh["root"], "missing-result", script=fresh["script"])
    result = run_worker(prepared["plan"])
    assert next(c for c in result.checks if c.check_id == "missing").status == "not_observed"
