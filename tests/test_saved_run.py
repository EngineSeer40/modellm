import json
import sys
from pathlib import Path

import duckdb
import plotly.graph_objects as go
import pytest
from streamlit.testing.v1 import AppTest

from src.ui.saved_run import load_saved_run


def test_saved_run_uses_copied_artifacts_and_never_starts_execution(tmp_path, monkeypatch):
    trace_url = "https://cloud.langfuse.com/project/demo/traces/existing"
    snapshot = tmp_path / "saved_run"
    snapshot.mkdir()
    go.Figure(go.Bar(x=["customers"], y=[2])).write_json(snapshot / "chart_0.json")
    with duckdb.connect(str(snapshot / "modellm.duckdb")) as con:
        con.execute("CREATE SCHEMA raw; CREATE TABLE raw.customers AS SELECT 1 AS id")
        con.execute("CREATE TABLE dim_customer AS SELECT * FROM raw.customers")
    state = {
        "status": "complete", "run_id": "saved_run", "request": "Count customers",
        "run_dir": str(tmp_path / "original_deleted_directory"), "events": [],
        "raw_names": ["customers"], "model_names": ["dim_customer"],
        "analysis": {"analyses": [{"title": "Customer count", "concept_id": "count"}]},
        "charts": [{"title": "Customer count", "file": "chart_0.json", "result_index": 0}],
        "findings": [{"text": "There are two customers", "evidence": []}],
        "quality_warnings": ["Unresolved source issue"],
    }
    if trace_url:
        state["langfuse_trace_url"] = trace_url
    (snapshot / "result.json").write_text(json.dumps(state))
    assert load_saved_run(snapshot)["run_dir"] == str(snapshot)
    assert load_saved_run(snapshot / "result.json")["run_dir"] == str(snapshot)

    def forbidden():
        pytest.fail("Saved dashboards must not start workers or retention cleanup")

    monkeypatch.setattr("src.ui.app.run_manager", forbidden)
    monkeypatch.setattr("src.telemetry.configure_langfuse", forbidden)
    monkeypatch.setattr(sys, "argv", ["app.py", "--saved-run", str(snapshot)])
    root = Path(__file__).resolve().parents[1]
    app = AppTest.from_file(root / "app.py").run(timeout=20)
    assert not app.exception
    assert not app.tabs[1].warning
    assert app.session_state["ui_surface"] == "Results"
    assert len(app.get("plotly_chart")) == 1
    assert not app.get("file_uploader")
    assert not any(button.label == "Start Modelling" for button in app.button)
    assert not any("expired" in message.value for message in app.info)
    assert [tab.label for tab in app.tabs] == ["Run", "Results", "Data Model", "Technical Details"]
    links = [item.value for item in app.header if "Langfuse Full Trace" in item.value]
    assert links == ([f"[Langfuse Full Trace]({trace_url})"] if trace_url else [])
    app.run(timeout=20)
    assert not app.exception and len(app.get("plotly_chart")) == 1
    # Relocating a run must not change its recorded original result on disk.
    assert json.loads((snapshot / "result.json").read_text()) == state


@pytest.mark.parametrize("state", [{}, {"run_id": "active", "status": "running"}, []])
def test_saved_run_rejects_invalid_or_unfinished_results(tmp_path, state):
    path = tmp_path / "result.json"
    path.write_text(json.dumps(state))
    with pytest.raises(ValueError):
        load_saved_run(path)
