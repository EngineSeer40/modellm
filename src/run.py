"""Shared run records and synchronous persistence; execution stays in workflow.py."""

from concurrent.futures import Future
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Literal, NotRequired, TypedDict

from .workspace import RunWorkspace, atomic_json

RunStatus = Literal["running", "complete", "failed"]
EventCallback = Callable[..., None]


class RunEvent(TypedDict):
    time: str
    stage: str
    kind: str
    message: str
    detail: NotRequired[dict[str, Any]]


class ChartArtifact(TypedDict):
    result_index: int
    title: str
    file: str
    concept_id: NotRequired[str]


class RunProgress(TypedDict):
    status: RunStatus
    events: list[RunEvent]


class RunState(RunProgress):
    run_id: str
    run_dir: str
    langfuse_trace_url: NotRequired[str]
    request: NotRequired[str]
    started_at_utc: NotRequired[str]
    contract_version: NotRequired[int]
    release_settings: NotRequired[dict[str, Any]]
    storage_cleanup: NotRequired[dict[str, Any]]
    cost_experiments: NotRequired[list[str]]
    strands_version: NotRequired[str]
    experiment: NotRequired[dict[str, Any]]
    experiment_tracker_error: NotRequired[str]
    raw_names: NotRequired[list[str]]
    profiles: NotRequired[dict[str, Any]]
    relationships: NotRequired[list[dict[str, Any]]]
    conceptual_attempts: NotRequired[list[dict[str, Any]]]
    plan: NotRequired[dict[str, Any]]
    dbt_attempts: NotRequired[list[dict[str, Any]]]
    dbt_explanation: NotRequired[str]
    dbt_runs: NotRequired[list[dict[str, Any]]]
    dbt_results: NotRequired[list[dict[str, Any]]]
    repair_attempts: NotRequired[int]
    model_names: NotRequired[list[str]]
    model_schemas: NotRequired[dict[str, dict[str, str]]]
    analysis_attempts: NotRequired[list[dict[str, Any]]]
    analysis: NotRequired[dict[str, Any]]
    analysis_results: NotRequired[list[dict[str, Any]]]
    metrics: NotRequired[dict[str, Any]]
    findings: NotRequired[list[dict[str, Any]]]
    finding_attempts: NotRequired[list[dict[str, Any]]]
    evidence: NotRequired[list[dict[str, Any]]]
    plot_specs: NotRequired[list[dict[str, Any] | None]]
    plot_attempts: NotRequired[list[dict[str, Any]]]
    charts: NotRequired[list[ChartArtifact]]
    quality_warnings: NotRequired[list[str]]
    runtime_budget: NotRequired[dict[str, Any]]
    usage: NotRequired[dict[str, Any]]
    error: NotRequired[str]
    failure_category: NotRequired[str]


class RunJob(TypedDict):
    workspace: RunWorkspace
    future: Future[RunState]
    current: RunState | RunProgress


class RunRecord:
    def __init__(self, workspace: RunWorkspace, state: RunState,
                 on_event: EventCallback | None = None):
        self.workspace = workspace
        self.state = state
        self.on_event = on_event
        self.lock = RLock()

    def save(self) -> None:
        with self.lock:
            atomic_json(self.workspace.result_path, self.state)

    def emit(self, stage: str, kind: str, message: str,
             detail: dict[str, Any] | None = None, *, notify: bool = True) -> None:
        event: RunEvent = {"time": datetime.now(timezone.utc).isoformat(),
                           "stage": stage, "kind": kind, "message": message}
        if detail:
            event["detail"] = detail
        # Evidence tools can emit concurrently. Persist each appended event before
        # notifying the caller, without holding the lock during external callbacks.
        with self.lock:
            self.state["events"].append(event)
            self.save()
        if notify and self.on_event:
            self.on_event(event)

    def complete(self, message: str) -> None:
        self.state["status"] = "complete"
        self.emit("dashboard completion", "done", message)

    def fail(self, message: str) -> None:
        self.state["status"] = "failed"
        self.state["error"] = message
        self.emit("dashboard completion", "failure", message)
