import json
from concurrent.futures import ThreadPoolExecutor

from src.run import RunRecord, RunState
from src.workspace import RunWorkspace


def test_run_record_persists_before_callbacks_and_preserves_terminal_events(tmp_path):
    workspace = RunWorkspace.create(tmp_path)
    state: RunState = {"run_id": workspace.run_id, "run_dir": str(workspace.run_dir),
                       "status": "running", "events": []}
    observed = []

    def callback(event):
        saved = json.loads(workspace.result_path.read_text())
        assert saved["events"][-1] == event
        # A callback can re-enter persistence without the emitting lock being held.
        with ThreadPoolExecutor() as pool:
            pool.submit(record.save).result(timeout=2)
        observed.append(event)

    record = RunRecord(workspace, state, callback)
    record.emit("loading data", "start", "Loading", {})
    record.emit("experiment tracking", "failure", "Ledger unavailable", notify=False)
    record.complete("Ready")
    assert [event["kind"] for event in observed] == ["start", "done"]
    assert "detail" not in state["events"][0]
    assert state["status"] == "complete"
    record.fail("Worker stopped")
    assert state["status"] == "failed" and state["error"] == "Worker stopped"
    assert observed[-1]["stage"] == "dashboard completion"
    assert json.loads(workspace.result_path.read_text()) == state


def test_concurrent_evidence_events_are_all_persisted(tmp_path):
    workspace = RunWorkspace.create(tmp_path)
    state: RunState = {"run_id": workspace.run_id, "run_dir": str(workspace.run_dir),
                       "status": "running", "events": []}
    record = RunRecord(workspace, state)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda index: record.emit("analytical finding", "evidence", str(index)), range(40)))
    saved = json.loads(workspace.result_path.read_text())
    assert len(saved["events"]) == 40
    assert {event["message"] for event in saved["events"]} == {str(index) for index in range(40)}
