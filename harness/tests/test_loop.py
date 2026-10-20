from pathlib import Path

import pytest

from harness.contracts import ExperimentPolicy
from harness.io import now, read
from harness.loop import (create_experiment, create_iteration, evaluate_iteration, gate_iteration,
                         load_iteration, experiment_usage, record_decision)
from harness.reporting import record_review, compare
from harness.runner import ScopeStop


def policy(fresh, **changes):
    return ExperimentPolicy(experiment_id="experiment", problem="Exercise an unfamiliar schema and preserve controls",
        scenarios=[fresh["id"]], mode="pipeline", offline_tests=[], max_no_progress=5,
        required_checks=["input_integrity", "execution", "scheduled_views"], protected_checks=["scheduled_views"], **changes)


def test_agentic_lineage_regression_revised_candidate_and_semantic_stop(fresh):
    create_experiment(fresh["root"], policy(fresh))
    baseline = create_iteration(fresh["root"], "experiment", None,
        {"hypothesis": "Record baseline", "boundary": "rendering", "script": fresh["script"]})
    observed = evaluate_iteration(fresh["root"], "experiment", baseline["iteration_id"])
    assert observed["disposition"] == "needs_review"
    bad = create_iteration(fresh["root"], "experiment", baseline["iteration_id"],
        {"hypothesis": "Deliberately reject every encoding", "boundary": "rendering",
         "variant": {"variant_id": "regression", "plotting_validator": "harness.variants.examples:reject_every_plot"},
         "script": [fresh["script"][0], {"stage": "plotting", "responses": [{"input": fresh["good"]}]*4}]})
    failed = evaluate_iteration(fresh["root"], "experiment", bad["iteration_id"])
    assert failed["disposition"] == "revise"
    assert any("scheduled_views" in k for k in failed["blocking_checks"])
    improved = create_iteration(fresh["root"], "experiment", bad["iteration_id"],
        {"hypothesis": "Retain compatible schema validation", "boundary": "rendering",
         "variant": {"variant_id": "revised", "schema_factory": "harness.variants.examples:no_op_schema"},
         "script": fresh["script"]})
    final = evaluate_iteration(fresh["root"], "experiment", improved["iteration_id"])
    assert final["disposition"] == "needs_review"
    assert final["baseline_id"] == baseline["iteration_id"]
    assert final["parent_id"] == bad["iteration_id"]
    assert len(final["comparisons"]) == 2
    assert experiment_usage(fresh["root"], "experiment")["internal_model_calls"] == 14
    record_decision(fresh["root"], "experiment", improved["iteration_id"], "Needs semantic review; no prompt-quality claim", final["comparisons"])
    loaded = load_iteration(fresh["root"], "experiment", bad["iteration_id"])
    assert loaded.trial_ids and (fresh["root"] / "outputs" / "trials" / loaded.trial_ids[0] / "boundary.json").exists()


def test_out_of_scope_variants_and_duplicate_candidates_stop(fresh):
    create_experiment(fresh["root"], policy(fresh))
    baseline = create_iteration(fresh["root"], "experiment", None, {"hypothesis": "Baseline", "boundary": "analysis"})
    with pytest.raises(ScopeStop, match="Duplicate"):
        create_iteration(fresh["root"], "experiment", baseline["iteration_id"], {"hypothesis": "Rename only", "boundary": "analysis"})
    with pytest.raises(ScopeStop, match="outside policy"):
        create_iteration(fresh["root"], "experiment", baseline["iteration_id"], {"hypothesis": "Change model", "boundary": "analysis", "variant": {"settings": {"model_id": "other"}}})


def test_aggregate_model_calls_limit_survives_individual_trial_interface(fresh):
    create_experiment(fresh["root"], policy(fresh, max_model_calls=4))
    baseline = create_iteration(fresh["root"], "experiment", None,
        {"hypothesis": "Baseline", "boundary": "analysis", "script": fresh["script"]})
    value = evaluate_iteration(fresh["root"], "experiment", baseline["iteration_id"])
    assert value["disposition"] == "stopped"
    assert value["usage"]["internal_model_calls"] == 4
    with pytest.raises(ScopeStop, match="allowance exhausted"):
        evaluate_iteration(fresh["root"], "experiment", baseline["iteration_id"])


def test_agent_review_cannot_satisfy_required_human_judgment(fresh):
    create_experiment(fresh["root"], policy(fresh))
    baseline = create_iteration(fresh["root"], "experiment", None,
        {"hypothesis": "Baseline", "boundary": "analysis", "script": fresh["script"]})
    value = evaluate_iteration(fresh["root"], "experiment", baseline["iteration_id"])
    iteration = load_iteration(fresh["root"], "experiment", baseline["iteration_id"])
    record_review(fresh["root"], {"review_id": "agent-note", "author": "Test agent", "author_kind": "agent",
        "trial_id": iteration.trial_ids[0], "check_id": "coverage", "status": "pass", "rationale": "Test-only claim",
        "evidence": ["boundary.json"], "created_at": now()})
    result = gate_iteration(fresh["root"], "experiment", baseline["iteration_id"])
    assert result["disposition"] == "needs_review"
    assert any("coverage" in x for x in result["review_checks"])


def test_candidate_cannot_be_eligible_without_observed_baseline(fresh):
    create_experiment(fresh["root"], policy(fresh, human_checks=[]))
    baseline = create_iteration(fresh["root"], "experiment", None,
        {"hypothesis": "Unevaluated baseline", "boundary": "analysis"})
    child = create_iteration(fresh["root"], "experiment", baseline["iteration_id"],
        {"hypothesis": "Candidate", "boundary": "analysis", "variant": {"cache_policy": "disabled"}, "script": fresh["script"]})
    gate = evaluate_iteration(fresh["root"], "experiment", child["iteration_id"])
    assert gate["disposition"] == "incomparable"
    assert any("required_observations_missing" in i for i in gate["incomparability"])


def test_gate_requires_comparison_for_every_baseline_control(fresh):
    from harness.loop import _save_iteration
    create_experiment(fresh["root"], policy(fresh, human_checks=[], repetitions=2))
    baseline = create_iteration(fresh["root"], "experiment", None,
        {"hypothesis": "Baseline", "boundary": "analysis", "script": fresh["script"]})
    evaluate_iteration(fresh["root"], "experiment", baseline["iteration_id"])
    child = create_iteration(fresh["root"], "experiment", baseline["iteration_id"],
        {"hypothesis": "Candidate", "boundary": "analysis", "variant": {"cache_policy": "disabled"}, "script": fresh["script"]})
    assert evaluate_iteration(fresh["root"], "experiment", child["iteration_id"])["disposition"] == "eligible_for_review"
    iteration = load_iteration(fresh["root"], "experiment", child["iteration_id"])
    iteration.comparisons.pop()
    _save_iteration(fresh["root"], iteration, "test_missing_comparison")
    gate = gate_iteration(fresh["root"], "experiment", child["iteration_id"])
    assert gate["disposition"] == "incomparable"
    assert any("required_comparison_missing" in i for i in gate["incomparability"])
    prior = load_iteration(fresh["root"], "experiment", baseline["iteration_id"])
    prior.trial_ids.pop()
    _save_iteration(fresh["root"], prior, "test_missing_baseline_repetition")
    gate = gate_iteration(fresh["root"], "experiment", child["iteration_id"])
    assert any("required_observations_missing" in i for i in gate["incomparability"])
