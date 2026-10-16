"""Open an operator-selected run snapshot without starting a workflow or cleanup."""

import json
from pathlib import Path


def load_saved_run(path):
    path = Path(path).expanduser().resolve()
    if path.is_dir():
        path = path / "result.json"
    state = json.loads(path.read_text())
    if not isinstance(state, dict) or not state.get("run_id"):
        raise ValueError("The saved result must contain a run ID")
    if state.get("status") not in {"complete", "failed"}:
        raise ValueError("Only finished runs can be opened as saved dashboards")
    # Charts and the database belong to this copy, even if the original was removed.
    state["run_dir"] = str(path.parent)
    return state
