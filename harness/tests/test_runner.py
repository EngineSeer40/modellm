import copy
import json
from pathlib import Path

import pytest

from harness.fixtures import create_scenario, load_fixture, verify_fixture
from harness.io import digest, file_hash, read
from harness.runner import ScopeStop, execute_trial, load_plan, prepare_trial, run_worker, validate_plan


def test_pipeline_real_sdk_tools_acceptance_correction_and_input_integrity(fresh):
    source_hash = file_hash(fresh["database"])
    prepared = prepare_trial(fresh["root"], fresh["id"], script=fresh["script"])
    result = execute_trial(prepared["plan"])
    assert result.execution == "finished", result.failure
    assert result.internal_model_calls == 4
    assert result.synthetic_usage and result.usage["measured_cost_usd"] is None
    folder = Path(prepared["plan"]).parent
    boundary = read(folder / "boundary.json")
    assert len(boundary["proposals"]) == 1
    assert boundary["accepted"][0]["result"]["row_count"] == 3
    assert boundary["accepted"][0]["result"]["profile"]
    assert boundary["schedule"][0]["outcome"] == "rendered"
    assert boundary["plot_calls"][0]["view_id"] == boundary["schedule"][0]["view_id"]
    assert (folder / "chart_0_0.html").exists()
    requests = [c for c in boundary["captures"] if c["kind"] == "sdk_request"]
    assert "Invalid element(s)" in json.dumps(requests[-1])
    assert file_hash(fresh["database"]) == source_hash
    assert next(c for c in result.checks if c.check_id == "input_integrity").status == "pass"
    assert next(c for c in result.checks if c.check_id == "coverage").status == "needs_review"
    with pytest.raises(ScopeStop):
        execute_trial(prepared["plan"])


def test_analysis_stops_before_acceptance_and_uses_frozen_summary(fresh):
    prepared = prepare_trial(fresh["root"], fresh["id"], mode="analysis", script=fresh["script"][:1])
    result = run_worker(prepared["plan"])
    assert result.execution == "finished"
    folder = Path(prepared["plan"]).parent
    boundary = read(folder / "boundary.json")
    assert not boundary["accepted"] and not boundary["schedule"]
    plan, _ = load_plan(prepared["plan"])
    payload = json.loads(boundary["invocations"][0]["prompt"][0]["text"])
    assert digest(payload["model_summaries"]) == digest(plan.fixture.summaries)
    assert boundary["invocations"][0]["limits"] is None


def test_new_dataset_names_no_case_pack_or_mock(tmp_path):
    from conftest import make_fixture
    item = make_fixture(tmp_path, "unseen", ("slot", "amplitude"))
    prepared = prepare_trial(item["root"], item["id"], script=item["script"])
    result = run_worker(prepared["plan"])
    assert result.execution == "finished", result.failure
    assert next(c for c in result.checks if c.check_id == "input_integrity").status == "pass"


def test_script_exhaustion_is_local_and_never_creates_provider(fresh):
    prepared = prepare_trial(fresh["root"], fresh["id"], mode="analysis", script=[])
    result = run_worker(prepared["plan"])
    assert result.execution == "failed"
    assert result.failure["category"] == "unmatched_mock"
    assert result.provider_dispatches == 0


def test_tampered_inputs_and_prepared_code_are_rejected(fresh):
    prepared = prepare_trial(fresh["root"], fresh["id"], mode="analysis")
    folder = Path(prepared["plan"]).parent
    code = folder / "code" / "src" / "agents.py"
    code.write_text(code.read_text() + "\n# changed after prepare\n")
    with pytest.raises(ValueError, match="Prepared source changed"):
        validate_plan(prepared["plan"])


def test_derived_request_creates_explicit_scenario(fresh):
    create_scenario(fresh["root"], fresh["id"], "new-question", "Summarize the observations", "Intentional request change")
    prepared = prepare_trial(fresh["root"], "new-question", mode="analysis")
    plan, _ = load_plan(prepared["plan"])
    assert plan.scenario.request != plan.fixture.request
    assert plan.scenario.parent_id == fresh["id"]


def test_live_needs_actual_approval_record_before_worker(fresh):
    prepared = prepare_trial(fresh["root"], fresh["id"], mode="analysis", backend="live")
    with pytest.raises(ScopeStop, match="explicit user approval"):
        execute_trial(prepared["plan"])
    assert not (Path(prepared["plan"]).parent / "running.json").exists()
