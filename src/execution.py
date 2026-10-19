"""Single-process admission and supervised workers, shared by local dev and ECS."""

import atexit
import json
import os
import signal
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .config import ReleaseSettings, check_limit
from .run import RunJob, RunRecord, RunState
from .storage import artifact_bytes, cleanup_run_workspaces
from .usage import estimate_usage_cost, summarize_usage
from .workspace import RunWorkspace, atomic_json, validate_submission


class RunBudget:
    """One budget across stages, repairs and SDK model cycles; no prompt editing."""
    def __init__(self, settings):
        self.settings = settings
        self.started = time.monotonic()
        self.calls = 0
        self.cost = 0.0
        self.usage_unknown = False

    def remaining_seconds(self):
        limit = self.settings.run_seconds
        remaining = limit - (time.monotonic() - self.started) if limit else None
        if remaining is not None and remaining <= 0:
            raise RuntimeError("Workflow deadline exceeded")
        return remaining

    def before_model(self):
        self.remaining_seconds()
        check_limit(self.calls + 1, self.settings.max_model_calls, "Internal model calls")
        if self.settings.max_cost_usd is not None:
            if self.usage_unknown:
                raise RuntimeError("Cannot continue: provider usage or pricing unavailable")
            if self.cost >= self.settings.max_cost_usd:
                raise RuntimeError("Reported model cost budget exhausted")
        self.calls += 1

    def record_response(self, message, model, region):
        usage = message.get("metadata", {}).get("usage", {})
        cost = estimate_usage_cost(model, region, usage)
        if cost is None:
            self.usage_unknown = True
            return
        self.cost += cost

    def hooks(self, model, region):
        from strands.hooks.events import BeforeModelCallEvent, AfterModelCallEvent
        budget = self

        class BudgetHooks:
            def register_hooks(self, registry, **kwargs):
                registry.add_callback(BeforeModelCallEvent, self.before)
                registry.add_callback(AfterModelCallEvent, self.after)

            pending = False

            def before(self, event):
                budget.before_model()
                self.pending = True

            def after(self, event):
                if event.stop_response:
                    budget.record_response(event.stop_response.message, model, region)
                elif event.exception and self.pending:
                    # A transport failure may already have incurred unreported usage.
                    budget.usage_unknown = True
                self.pending = False
        return BudgetHooks()


_budget = ContextVar("run_budget", default=None)


def current_budget():
    return _budget.get()


@contextmanager
def use_budget(settings):
    budget = RunBudget(settings)
    token = _budget.set(budget)
    try:
        yield budget
    finally:
        _budget.reset(token)


def terminate_group(process, grace=2):
    """Wait for termination before releasing admission; dbt shares this group."""
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        pass
    # The group can outlive its leader, including after a successful worker exit.
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    process.wait()


class RunManager:
    def __init__(self, settings: ReleaseSettings):
        self.settings = settings
        self.root = Path(settings.runs_root).resolve()
        self.lock = threading.Lock()
        self.active = None
        self.process = None
        self.closed = threading.Event()
        self.supervisor = None
        self.last_submission = {}
        self.admission_path = self.root.parent / "runtime" / "admission.json"
        self.admission_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._cleanup()
        self.maintenance = threading.Thread(target=self._maintain, daemon=True)
        self.maintenance.start()
        atexit.register(self.shutdown)

    def _cleanup(self):
        return cleanup_run_workspaces(self.root, active={self.active.run_id} if self.active else set(), settings=self.settings)

    def _maintain(self):
        while not self.closed.wait(3600):
            with self.lock:
                self._cleanup()

    def submit(self, uploads: list[tuple[str, bytes]], request: str, owner: str) -> RunJob:
        validate_submission(uploads, request, self.settings)
        with self.lock:
            if self.closed.is_set() or not self.settings.live_enabled:
                raise ValueError("Live runs are currently disabled")
            if self.active:
                raise ValueError("Another analysis is running. Please try again when it finishes.")
            now = time.time()
            cooldown = self.settings.cooldown_seconds
            if cooldown and now - self.last_submission.get(owner, 0) < cooldown:
                raise ValueError("Please wait before submitting another analysis")
            cleanup = self._cleanup()
            check_limit(cleanup["retained_bytes"] + sum(len(content) for _, content in uploads),
                        self.settings.workspace_bytes, "Workspace storage")
            # Check capacity for the remaining application allowance before accepting work.
            if shutil.disk_usage(self.root).free < self.settings.workspace_bytes - cleanup["retained_bytes"]:
                raise ValueError("Insufficient free disk space for the workspace allowance")
            day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            try:
                admission = json.loads(self.admission_path.read_text())
            except FileNotFoundError:
                admission = {}
            count = admission.get("count", 0) if admission.get("day") == day else 0
            check_limit(count + 1, self.settings.daily_runs, "Daily live runs")
            workspace = RunWorkspace.create(self.root, owner)
            try:
                paths = workspace.persist(uploads, self.settings)
                atomic_json(workspace.run_dir / "request.json", {
                    "paths": [str(path) for path in paths], "request": request,
                    "settings": self.settings.snapshot(),
                })
                atomic_json(workspace.result_path, {"status": "running", "run_id": workspace.run_id,
                            "run_dir": str(workspace.run_dir), "request": request, "events": []})
                self.active = workspace
                # Persist admission before dispatch: a crash may consume a slot, never grant free runs.
                atomic_json(self.admission_path, {"day": day, "count": count + 1})
                self.last_submission = {key: value for key, value in self.last_submission.items()
                                        if cooldown and now - value < cooldown}
                self.last_submission[owner] = now
                future: Future[RunState] = Future()
                future.set_running_or_notify_cancel()
                def supervise():
                    try:
                        future.set_result(self._supervise(workspace))
                    except BaseException as exc:
                        future.set_exception(exc)
                self.supervisor = threading.Thread(target=supervise, name="workflow-supervisor", daemon=True)
                self.supervisor.start()
                return {"workspace": workspace, "future": future, "current": {"status": "running", "events": []}}
            except Exception:
                self.active = None
                workspace.remove_inputs()
                raise

    def _supervise(self, workspace: RunWorkspace) -> RunState:
        try:
            return self._execute_worker(workspace)
        finally:
            # Admission must recover even when terminal-record I/O or cleanup fails.
            with self.lock:
                self.active = self.process = None
                self._cleanup()

    def _execute_worker(self, workspace: RunWorkspace) -> RunState:
        reason = None
        process = None
        try:
            env = {key: value for key, value in os.environ.items() if key != "MODELLM_LIVE_ACCESS_CODE"}
            process = subprocess.Popen([sys.executable, "-m", "src.worker", "--run-dir", str(workspace.run_dir)],
                                       env=env, cwd=Path(__file__).resolve().parents[1], start_new_session=True,
                                       stdout=subprocess.DEVNULL, stderr=None)
            with self.lock:
                self.process = process
            deadline = time.monotonic() + self.settings.run_seconds if self.settings.run_seconds else None
            next_storage_check = time.monotonic()
            while process.poll() is None:
                if self.closed.wait(.2):
                    reason = "Service shutting down"
                    break
                if time.monotonic() >= next_storage_check:
                    size = artifact_bytes(self.root)
                    if size > self.settings.workspace_bytes:
                        reason = "Workspace storage limit exceeded"
                        break
                    next_storage_check = time.monotonic() + 5
                if deadline and time.monotonic() >= deadline:
                    reason = "Workflow deadline exceeded"
                    break
            if reason or process.returncode != 0:
                reason = reason or f"Worker exited unexpectedly ({process.returncode})"
        except Exception as exc:
            reason = f"Worker supervision failed: {type(exc).__name__}"
        finally:
            if process:
                terminate_group(process)
            workspace.remove_inputs()
        try:
            state = json.loads(workspace.result_path.read_text())
        except (OSError, ValueError):
            state = {"run_id": workspace.run_id, "run_dir": str(workspace.run_dir), "events": []}
        if reason or state.get("status") not in {"complete", "failed"}:
            record = RunRecord(workspace, state)
            state["failure_category"] = "worker"
            record.fail(reason or "Worker produced no terminal result")
            state["usage"] = summarize_usage(state["events"])
            if state["usage"]["total"]["usage_status"] == "reported":
                state["usage"]["total"]["usage_status"] = "partial"
            record.save()
        return state

    def shutdown(self):
        self.closed.set()
        if self.supervisor and self.supervisor is not threading.current_thread():
            self.supervisor.join()
