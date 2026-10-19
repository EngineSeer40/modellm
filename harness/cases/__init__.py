"""Optional recorded evidence packs; no dataset registry is needed by the runner."""

from pathlib import Path

from harness.fixtures import fixture_dir, load_fixture
from harness.contracts import CaseEvaluation
from harness.io import read
from harness.replay import pointer
from harness.runner import prepare_trial


def prepare_case(root, case_id, fixture_id, variant=None):
    index = read(Path(__file__).with_name("index.json"))
    case = next((c for c in index["cases"] if c["id"] == case_id), None)
    if not case:
        raise ValueError(f"Unknown optional evidence case: {case_id}")
    fixture, _ = load_fixture(root, fixture_id)
    source_run = Path(fixture.source.get("record_path", "")).parent.name
    if source_run not in case["runs"]:
        raise ValueError("Case does not refer to this fixture's recorded run")
    record = read(fixture_dir(root, fixture_id) / "historical.json")
    plots = []
    for entry in case.get("replay_plots", []):
        plots.append({"result_index": entry["result_index"], "plot": pointer(record, entry["pointer"]),
                      "expected": entry.get("mechanical_expectation")})
    indexes = case.get("result_indexes") or [p.source_index for p in fixture.plotting]
    evaluation = CaseEvaluation(case_id=case_id, kind=case["evaluation_kind"],
        expectation=case["expectation"], expected_acceptance=case.get("expected_acceptance"),
        units=case.get("units", {}), result_index=case.get("unit_result_index"))
    return prepare_trial(root, fixture_id, mode="replay", backend="recorded", variant=variant,
                         result_indexes=indexes, recorded_plots=plots, case_evaluation=evaluation)
