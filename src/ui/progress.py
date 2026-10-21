"""User-facing projections of existing workflow events; no execution decisions."""

from datetime import datetime

from src.run import RunEvent, RunState


PHASES = (
    ("Loading datasets", "Reading your uploaded files."),
    ("Understanding data", "Inspecting columns, data quality and candidate relationships."),
    ("Conceptual Model Stage", "Choosing source-supported facts, dimensions and grains."),
    ("Dimensional Model Stage", "Building the model and checking its tables and relationships."),
    ("Analysis Stage", "Calculating and validating the requested analytical results."),
    ("Interpreting results", "Checking findings against evidence from the accepted results."),
    ("Plotting Stage", "Choosing visualisations and rendering the accepted results."),
)
WORKFLOW_STAGES = (
    (2, "Conceptual Model"),
    (3, "Dimensional Model"),
    (4, "Analysis"),
    (6, "Plotting"),
)
STAGE_PHASE = {
    "loading data": 0,
    "profiling data": 1, "inferring relationships": 1,
    "conceptual modelling": 2,
    "generating dbt models": 3, "dbt compile/build/test": 3, "validation and repair": 3,
    "analytical interpretation": 4, "analysis acceptance": 4,
    "analytical finding": 5,
    "visualisation generation": 6,
}
COMPLETION_STAGES = {
    "loading data", "inferring relationships", "conceptual modelling",
    "validation and repair", "analysis acceptance",
}
PROGRESS_KINDS = {"start", "done", "failure", "repair", "model_call", "contract_check"}


def project_progress(events: list[RunEvent], status: str = "running") -> list[dict]:
    phases = [{"label": label, "detail": detail, "status": "pending", "repairs": 0}
              for label, detail in PHASES]
    current = None
    for event in events:
        stage, kind = event.get("stage"), event.get("kind")
        if stage == "dashboard completion":
            if kind == "done":
                for phase in phases:
                    phase["status"] = "complete"
            elif kind == "failure" and current is not None:
                phases[current]["status"] = "failed"
            continue
        if stage not in STAGE_PHASE or kind not in PROGRESS_KINDS:
            continue
        index = STAGE_PHASE[stage]
        if current is None or index > current:
            for previous in phases[:index]:
                if previous["status"] != "pending":
                    previous["status"] = "complete"
        current = index
        phase = phases[index]
        if kind == "repair":
            phase["repairs"] += 1
            phase["status"] = "repairing"
        elif kind == "failure":
            phase["status"] = "checking"
        elif kind == "done" and stage in COMPLETION_STAGES:
            phase["status"] = "complete"
        elif phase["status"] != "complete" or kind == "start":
            phase["status"] = "running"
    if status == "failed" and current is not None:
        phases[current]["status"] = "failed"
    elif status == "complete":
        for phase in phases:
            phase["status"] = "complete"
    return phases


def repair_counts(state: RunState) -> dict[str, int]:
    counts = {}
    for label, key in (("Model design", "conceptual_attempts"), ("dbt", "dbt_attempts"),
                       ("Analysis", "analysis_attempts"), ("Findings", "finding_attempts")):
        counts[label] = sum(item.get("attempt", 1) > 1 for item in state.get(key, []))
    if not state.get("dbt_attempts"):
        counts["dbt"] = state.get("repair_attempts", 0)
    return counts


def failure_message(state: RunState) -> str:
    phases = project_progress(state.get("events", []), "failed")
    failed = next((phase["label"] for phase in phases if phase["status"] == "failed"), None)
    messages = {
        "Loading datasets": "The uploaded datasets could not be loaded.",
        "Understanding data": "The source data could not be inspected.",
        "Conceptual Model Stage": "A valid data model could not be established.",
        "Dimensional Model Stage": "The generated data model could not pass validation.",
        "Analysis Stage": "The requested analytical results could not be validated.",
        "Interpreting results": "The analytical findings could not be verified against their evidence.",
        "Plotting Stage": "The dashboard could not be rendered from the accepted results.",
    }
    return messages.get(failed, "The run could not be completed.")


def duration_label(state: RunState) -> str:
    events = state.get("events", [])
    start = state.get("started_at_utc") or next((e.get("time") for e in events if e.get("time")), None)
    end = next((e.get("time") for e in reversed(events) if e.get("time")), None)
    if not start or not end:
        return "Unavailable"
    try:
        seconds = max(0, int((datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()))
    except (ValueError, TypeError):
        return "Unavailable"
    minutes, seconds = divmod(seconds, 60)
    return f"{minutes}m {seconds}s" if minutes else f"{seconds}s"
