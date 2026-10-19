"""Prepare immutable trials and supervise only the requested development stages."""

import copy
import os
import shutil
import subprocess
import sys
import time
from importlib.metadata import version
from pathlib import Path

from src.config import ReleaseSettings, use_settings
from src.execution import terminate_group, use_budget
from src.run import RunRecord
from src.workspace import RunWorkspace

from .contracts import AuthorizationRecord, CaseEvaluation, TrialPlan, TrialResult, Variant
from .fixtures import clone_trial_database, fixture_dir, load_fixture, load_scenario, verify_fixture
from .io import digest, file_hash, now, read, seal, unseal, write

PACKAGES = ("strands-agents", "pydantic", "duckdb", "pandas", "plotly", "sqlglot")


class ScopeStop(ValueError):
    pass


def code_files(repo):
    repo = Path(repo)
    files = []
    for folder in ("src", "harness", "tests"):
        for path in (repo / folder).rglob("*"):
            rel = path.relative_to(repo)
            if any(p in {"outputs", "fixtures", "scenarios", "plotting", "__pycache__", ".pytest_cache"} for p in rel.parts[1:-1]):
                continue
            if path.is_file() and path.suffix in {".py", ".json", ".yml", ".yaml", ".toml"}:
                files.append(rel)
    files += [Path(name) for name in ("app.py", "pyproject.toml", "uv.lock") if (repo / name).exists()]
    return sorted(set(files))


def snapshot_code(destination, source=None):
    source = Path(source or Path(__file__).resolve().parents[1]).resolve()
    destination = Path(destination)
    hashes = {}
    for rel in code_files(source):
        target = destination / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source / rel, target)
        hashes[rel.as_posix()] = file_hash(target)
    return hashes


def finite_settings(settings):
    values = dict(settings)
    defaults = {"run_seconds": 300, "max_model_calls": 20, "max_cost_usd": 1.0,
                "max_output_tokens": 8192}
    for key, default in defaults.items():
        if values.get(key) is None:
            values[key] = default
    ReleaseSettings(**values).validate()
    return values


def prepare_trial(root, scenario_id, *, mode="pipeline", backend="scripted", variant=None,
                  script=None, result_indexes=None, view_ordinal=None, recorded_plots=None,
                  diagnostic_acceptance=False, repetition=0, authorization=None,
                  experiment_id=None, iteration_id=None, code_source=None, case_evaluation=None):
    root = Path(root).resolve()
    authorization = str(Path(authorization).resolve()) if authorization else None
    scenario, _ = load_scenario(root, scenario_id)
    fixture, fingerprint = load_fixture(root, scenario.fixture_id)
    verify_fixture(root, fixture.fixture_id)
    if fingerprint != scenario.fixture_hash:
        raise ValueError("Scenario does not bind the current fixture")
    if (fixture_dir(root, fixture.fixture_id) / "unregistered.json").exists():
        raise ValueError("Fixture is unregistered")
    if fixture.validation_issues:
        raise ValueError(f"Current upstream compatibility failed: {fixture.validation_issues}")
    if mode in {"plotting", "replay"} and scenario.request != fixture.request:
        raise ValueError("Fixed accepted-result execution requires its original request; create a reviewed derived Plotting fixture first")
    variant = Variant.model_validate(variant or {})
    case_evaluation = CaseEvaluation.model_validate(case_evaluation) if case_evaluation else None
    if case_evaluation:
        if mode != "replay":
            raise ValueError("Named evidence cases require replay mode")
        needs_plots = case_evaluation.kind in {"plotting", "analytical"}
        if needs_plots != bool(recorded_plots):
            raise ValueError("Case evaluation kind must match its explicit plotting replays")
    from src.context_cost import configured_cost_experiments
    controls = sorted(configured_cost_experiments(frozenset(fixture.controls if variant.controls is None else variant.controls)))
    settings = finite_settings({**fixture.settings, **variant.settings})
    indexes = list(result_indexes if result_indexes is not None else range(len(fixture.plotting)))
    if mode in {"plotting", "replay"}:
        if not indexes or any(i not in {p.source_index for p in fixture.plotting} for i in indexes):
            raise ValueError("Selected accepted results are not in this fixture")
        if view_ordinal is not None and len(indexes) != 1:
            raise ValueError("One-view scope requires exactly one result")
    elif view_ordinal is not None or result_indexes:
        raise ValueError("Result/view selection applies only to fixed accepted results")
    if (mode == "replay") != (backend == "recorded"):
        raise ValueError("Replay requires recorded backend; other modes use scripted or live")
    workspace = RunWorkspace.create(root / "outputs" / "trials", owner="development-harness")
    source = fixture_dir(root, fixture.fixture_id)
    shutil.copyfile(source / fixture.database.path, workspace.input_dir / "modellm.duckdb")
    if (source / "historical.json").exists():
        shutil.copyfile(source / "historical.json", workspace.input_dir / "historical.json")
    seal(workspace.input_dir / "fixture.json", fixture)
    hashes = snapshot_code(workspace.run_dir / "code", code_source)
    packages = {p: version(p) for p in PACKAGES}
    input_hash = digest({"fixture": fingerprint, "scenario": scenario, "mode": mode,
                         "indexes": indexes if mode in {"plotting", "replay"} else [],
                         "view": view_ordinal, "checks": scenario.checks,
                         "case_evaluation": case_evaluation,
                         "recorded_plots": recorded_plots or []})
    plan = TrialPlan(trial_id=workspace.run_id, prepared_at=now(), mode=mode, backend=backend,
        fixture=fixture, fixture_hash=fingerprint, scenario=scenario, variant=variant,
        variant_hash=digest(variant), source_root=str(workspace.run_dir / "code"), source_hashes=hashes,
        packages=packages, input_hash=input_hash, settings=settings, controls=controls,
        result_indexes=indexes if mode in {"plotting", "replay"} else [], view_ordinal=view_ordinal,
        script=script or [], recorded_plots=recorded_plots or [], diagnostic_acceptance=diagnostic_acceptance,
        case_evaluation=case_evaluation,
        repetition=repetition, authorization=authorization, experiment_id=experiment_id, iteration_id=iteration_id)
    plan_hash = seal(workspace.run_dir / "plan.json", plan)
    write(workspace.run_dir / "preparation.json", {"finite_limit_defaults": {k: settings[k] for k in settings if settings[k] != fixture.settings.get(k)},
          "packages": packages, "source_hashes": hashes, "plan_hash": plan_hash}, exclusive=True)
    return {"trial_id": plan.trial_id, "plan": str(workspace.run_dir / "plan.json"), "plan_hash": plan_hash}


def load_plan(path):
    data, fingerprint = unseal(path)
    return TrialPlan.model_validate(data), fingerprint


def validate_plan(path):
    plan, fingerprint = load_plan(path)
    directory = Path(path).resolve().parent
    if digest(plan.fixture) != plan.fixture_hash or digest(plan.variant) != plan.variant_hash:
        raise ValueError("Embedded fixture or variant identity mismatch")
    if file_hash(directory / "input" / "modellm.duckdb") != plan.fixture.database.sha256:
        raise ValueError("Input database integrity failure")
    if {p.as_posix() for p in code_files(plan.source_root)} != set(plan.source_hashes):
        raise ValueError("Prepared source file set changed")
    for rel, expected in plan.source_hashes.items():
        if file_hash(Path(plan.source_root) / rel) != expected:
            raise ValueError(f"Prepared source changed: {rel}")
    if {p: version(p) for p in PACKAGES} != plan.packages:
        raise ValueError("Installed package versions changed since preparation")
    return plan, fingerprint


def check_authorization(plan, plan_hash):
    if plan.backend != "live":
        return
    if not plan.authorization:
        raise ScopeStop("Live evaluation needs a recorded explicit user approval reference")
    approval_data, _ = unseal(plan.authorization)
    approval = AuthorizationRecord.model_validate(approval_data)
    if approval.revoked:
        raise ScopeStop("Live authorization revoked")
    if plan.experiment_id:
        root = Path(plan.source_root).parents[3]
        policy_data, policy_hash = unseal(root / "outputs" / "experiments" / plan.experiment_id / "policy.json")
        if approval.subject_hash != policy_hash:
            raise ScopeStop("Live authorization is not for this experiment policy")
    elif approval.subject_hash != plan_hash:
        raise ScopeStop("Live authorization is not for this fixed plan")


def execute_trial(plan_path, *, remaining=None, _experiment_locked=False):
    plan, _ = validate_plan(plan_path)
    if plan.experiment_id:
        import fcntl
        from contextlib import nullcontext
        from .loop import validate_dispatch, _remaining, experiment_dir, load_policy
        policy, _ = load_policy(Path(plan.source_root).parents[3], plan.experiment_id)
        root = Path(plan.source_root).parents[3]
        lock_path = experiment_dir(root, plan.experiment_id) / "dispatch.lock"
        with (nullcontext() if _experiment_locked else lock_path.open("a")) as lock:
            if lock is not None:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ScopeStop("Another worker is evaluating this experiment") from exc
            validate_dispatch(root, policy, plan)
            allowance = _remaining(root, policy)
            if remaining:
                allowance = {k: min(v, remaining.get(k, v)) for k, v in allowance.items()}
            return _execute_trial(plan_path, remaining=allowance)
    return _execute_trial(plan_path, remaining=remaining)


def _execute_trial(plan_path, *, remaining=None):
    plan_path = Path(plan_path).resolve()
    plan, plan_hash = validate_plan(plan_path)
    check_authorization(plan, plan_hash)
    directory = plan_path.parent
    if (directory / "trial.json").exists() or (directory / "running.json").exists():
        raise ScopeStop("This attempt already ran or was interrupted; prepare a new linked trial")
    settings = dict(plan.settings)
    for key, value in (remaining or {}).items():
        if key not in {"max_model_calls", "max_cost_usd", "run_seconds"} or value <= 0:
            raise ScopeStop("Aggregate resource allowance exhausted or invalid")
        settings[key] = min(settings[key], value)
    write(directory / "dispatch.json", {"plan_hash": plan_hash, "settings": settings}, exclusive=True)
    write(directory / "running.json", {"fixture_id": plan.fixture.fixture_id, "started": now()}, exclusive=True)
    environment = {**os.environ, "MODELLM_LANGFUSE_ENABLED": "false", "PYTHONPATH": ""}
    if plan.backend != "live":
        environment["AWS_EC2_METADATA_DISABLED"] = "true"
    started = time.monotonic()
    with (directory / "worker.log").open("x") as log:
        process = subprocess.Popen([sys.executable, "-m", "harness", "_worker", "--plan", str(plan_path)],
            cwd=plan.source_root, env=environment, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                size = sum(p.stat().st_size for p in directory.rglob("*") if p.is_file())
                if elapsed > settings["run_seconds"] or size > settings["workspace_bytes"]:
                    raise ScopeStop("Trial deadline or workspace-size limit exceeded")
                time.sleep(0.1)
        except BaseException as exc:
            terminate_group(process)
            if not (directory / "trial.json").exists():
                write(directory / "trial.json", TrialResult(trial_id=plan.trial_id, execution="interrupted",
                    failure={"category": "supervision", "message": str(exc)}, usage_unknown=True,
                    ended_at=now()))
            raise
        finally:
            (directory / "running.json").unlink(missing_ok=True)
    if not (directory / "trial.json").exists():
        write(directory / "trial.json", TrialResult(trial_id=plan.trial_id, execution="interrupted",
            failure={"category": "worker", "message": f"Worker exited {process.returncode}; see worker.log"},
            usage_unknown=plan.backend == "live", ended_at=now()))
    return TrialResult.model_validate(read(directory / "trial.json"))


def model_fingerprints(database, names):
    from src.database import connect
    output = {}
    with connect(database, read_only=True) as con:
        for name in names:
            table = '"' + name.replace('"', '""') + '"'
            columns = [row[0] for row in con.execute(f"DESCRIBE main.{table}").fetchall()]
            row = ",".join('"' + c.replace('"', '""') + '"' for c in columns)
            output[name] = con.execute(f"SELECT count(*), bit_xor(hash(ROW({row}))), sum(hash(ROW({row}))::HUGEINT) FROM main.{table}").fetchone()
    return output


def run_worker(plan_path):
    from src import agents, workflow
    from src.analysis import accept_analyses
    from src.core.contracts import AnalyticalPlan, PlottingPlan, ResultFilter
    from src.usage import summarize_usage
    from src.analysis import load_result_frame
    from src.visualisation import validate_plot_candidate
    from .adapters import stage_adapters, classify_failure
    from .evaluation import evaluate
    from .reporting import render_report

    plan_path = Path(plan_path).resolve()
    plan, plan_hash = validate_plan(plan_path)
    check_authorization(plan, plan_hash)
    directory = plan_path.parent
    if (directory / "trial.json").exists():
        raise ScopeStop("This worker attempt already finished; prepare another trial")
    settings = dict(plan.settings)
    dispatch_path = directory / "dispatch.json"
    if plan.backend == "live" and (not dispatch_path.exists() or not (directory / "running.json").exists()):
        raise ScopeStop("Live workers require a supervised dispatch; use harness run")
    if dispatch_path.exists():
        dispatch = read(dispatch_path)
        if dispatch["plan_hash"] != plan_hash:
            raise ValueError("Dispatch is for a different plan")
        for key, value in dispatch["settings"].items():
            if value != settings.get(key):
                if key not in {"max_model_calls", "max_cost_usd", "run_seconds"} or value <= 0 or value > settings[key]:
                    raise ValueError("Dispatch expanded or changed prepared settings")
                settings[key] = value
    settings = ReleaseSettings(**settings)
    workspace = RunWorkspace.open(directory)
    clone_trial_database(directory / "input" / "modellm.duckdb", workspace.database, plan.fixture, plan.mode)
    state = {"run_id": plan.trial_id, "run_dir": str(directory), "request": plan.scenario.request,
        "status": "running", "events": [], "plan": plan.fixture.plan.model_dump(),
        "model_names": plan.fixture.model_names, "model_schemas": plan.fixture.model_schemas,
        "release_settings": settings.snapshot(), "cost_experiments": plan.controls,
        "findings": [], "finding_attempts": [], "evidence": []}
    record = RunRecord(workspace, state)
    sidecar = {k: [] for k in ("captures", "invocations", "proposals", "schedule", "plot_calls", "accepted", "replay")}
    accepted = []
    stage, failure, budget = "initialization", None, None
    before = None
    integrity = False
    with use_settings(settings), use_budget(settings) as budget:
        try:
            before = model_fingerprints(workspace.database, plan.fixture.model_names)
            with stage_adapters(plan, record, sidecar):
                stage = plan.mode
                if plan.mode == "analysis":
                    proposed = agents.analyse(plan.scenario.request, plan.fixture.plan,
                        plan.fixture.model_schemas, copy.deepcopy(plan.fixture.summaries), record.emit,
                        workspace.database, cost_experiments=frozenset(plan.controls))
                    state["analysis"] = proposed.model_dump()
                    if plan.diagnostic_acceptance:
                        diagnostic_db = directory / "diagnostic.duckdb"
                        shutil.copyfile(workspace.database, diagnostic_db)
                        try:
                            values = accept_analyses(diagnostic_db, plan.fixture.model_names, proposed, max_charts=settings.max_charts)
                            sidecar["diagnostic_acceptance"] = {"accepted": [r.model_dump() for _, r in values]}
                        except Exception as exc:
                            sidecar["diagnostic_acceptance"] = {"failure": classify_failure(exc)}
                elif plan.mode == "pipeline":
                    _, accepted = workflow._accept_analysis(record, plan.scenario.request, plan.fixture.plan,
                        plan.fixture.model_names, frozenset(plan.controls))
                else:
                    accepted = [(p.spec, p.result) for p in plan.fixture.plotting if p.source_index in plan.result_indexes]
                sidecar["accepted"] = [{"spec": s.model_dump(), "result": r.model_dump()} for s, r in accepted]
                write(directory / "boundary.json", sidecar)
                if plan.mode in {"pipeline", "plotting"}:
                    for spec, result in accepted:
                        if result.row_count:
                            workflow.representation_selections(workspace.database, spec, result)
                        else:
                            if plan.view_ordinal not in (None, 0):
                                raise ValueError("Empty result has only one placeholder view")
                            fp = digest([result.result_id, []])
                            sidecar["schedule"].append({"view_id": digest([plan.trial_id, fp]),
                                "selection_fingerprint": fp, "result_id": result.result_id,
                                "acceptance_id": result.acceptance_id, "relation": result.relation,
                                "ordinal": 0, "selection": [], "title": spec.title, "selected": True, "empty_result": True})
                    workflow._render_visualisations(record, accepted, frozenset(plan.controls))
                if plan.mode == "replay":
                    if plan.case_evaluation and plan.case_evaluation.kind == "evidence":
                        sidecar["replay"].append({"kind": "evidence", "outcome": "not_observed",
                            "case_id": plan.case_evaluation.case_id,
                            "limitation": "Evidence description only; no analytical or provider evaluation was executed"})
                    elif plan.recorded_plots:
                        by_index = {p.source_index: p for p in plan.fixture.plotting}
                        for item in plan.recorded_plots:
                            fixed = by_index[item["result_index"]]
                            if item["result_index"] not in plan.result_indexes:
                                raise ValueError("Recorded plot does not belong to selected result scope")
                            selected = [ResultFilter.model_validate(s) for s in item["plot"].get("filters", [])]
                            actual_schedule = workflow.representation_selections(workspace.database, fixed.spec, fixed.result)
                            if [s.model_dump() for s in selected] not in [[s.model_dump() for s in filters] for _, filters in actual_schedule]:
                                raise ValueError("Recorded filter is not a scheduled selection")
                            observation = {"result_index": item["result_index"], "expected": item.get("expected"), "kind": "plot"}
                            call = {"result_id": fixed.result.result_id, "view_id": f"recorded_{len(sidecar['replay'])}",
                                "selection": [s.model_dump() for s in selected], "plot": item["plot"], "status": "recorded_candidate"}
                            sidecar["plot_calls"].append(call)
                            try:
                                figure = workflow.validate_plot_candidate(load_result_frame(workspace.database, fixed.result),
                                    PlottingPlan.model_validate(item["plot"]), fixed.result, selected, fixed.spec.title)
                                filename = f"replay_{len(sidecar['replay'])}.json"
                                figure.write_json(directory / filename)
                                observation.update(outcome="rendered", traces=len(figure.data), file=filename)
                                call.update(status="rendered", figure=filename)
                            except (ValueError, TypeError, KeyError, NotImplementedError) as exc:
                                observation.update(outcome="rejected", failure=classify_failure(exc))
                            sidecar["replay"].append(observation)
                    else:
                        historical = read(directory / "input" / "historical.json")
                        observation = {"kind": "acceptance", "expected": plan.case_evaluation.expected_acceptance if plan.case_evaluation else None}
                        try:
                            acceptance_db = directory / "replay-acceptance.duckdb"
                            shutil.copyfile(workspace.database, acceptance_db)
                            replay_accepted = accept_analyses(acceptance_db, plan.fixture.model_names,
                                AnalyticalPlan.model_validate(historical["analysis"]), max_charts=settings.max_charts)
                            observation.update(outcome="accepted", rows=[r.row_count for _, r in replay_accepted])
                        except ValueError as exc:
                            observation.update(outcome="rejected", failure=classify_failure(exc))
                        except Exception as exc:
                            from src.analysis import AnalysisAcceptanceError
                            if not isinstance(exc, AnalysisAcceptanceError):
                                raise
                            observation.update(outcome="rejected", failure=classify_failure(exc))
                        sidecar["replay"].append(observation)
            state["status"] = "complete"
        except Exception as exc:
            failure = classify_failure(exc)
            state.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        finally:
            try:
                integrity = (before == model_fingerprints(workspace.database, plan.fixture.model_names)
                    and file_hash(directory / "input" / "modellm.duckdb") == plan.fixture.database.sha256)
            except Exception:
                integrity = False
            for local_index, (_, result) in enumerate(accepted):
                views = [v for v in sidecar["schedule"] if v["acceptance_id"] == result.acceptance_id and v["selected"]]
                for local_ordinal, view in enumerate(views):
                    name = f"chart_{local_index}.json" if not result.row_count else f"chart_{local_index}_{local_ordinal}.json"
                    view["figure"] = name if (directory / name).exists() else None
                    view["outcome"] = "rendered" if view["figure"] else "unrendered"
            sidecar["coverage"] = []
            entries = sidecar["accepted"] or [{"spec": s, "result": None} for proposal in sidecar["proposals"][-1:] for s in proposal["analyses"]]
            for entry in entries:
                spec = entry["spec"]
                result_id = entry["result"]["result_id"] if entry["result"] else None
                calls = [c for c in sidecar["plot_calls"] if c["result_id"] == result_id]
                encodings = []
                for call in calls:
                    columns = call.get("plot", {}).get("columns", {})
                    visible = {f for k, v in columns.items() if k not in {"hover_data", "hover_name", "custom_data"}
                               for f in (v if isinstance(v, list) else [v])}
                    encodings.append({"view_id": call["view_id"], "channels": columns,
                        "declared_measures_not_visibly_mapped": [m["field"] for m in spec["measures"] if m["field"] not in visible],
                        "invocation_id": call.get("invocation_id")})
                sidecar["coverage"].append({"original_request": plan.scenario.request,
                    "question": spec["question"], "title": spec["title"], "population": spec["population_definition"],
                    "grain": spec["grain"], "measures": spec["measures"], "visualisation_goal": spec["visualisation_goal"],
                    "result_id": result_id, "views": [v["view_id"] for v in sidecar["schedule"] if v["result_id"] == result_id],
                    "encodings": encodings, "assessment": "Inventory for review; missing visible mappings are not automatically analytical errors"})
            state["usage"] = summarize_usage(state["events"])
            state["runtime_budget"] = {"internal_model_calls": budget.calls, "usage_unknown": budget.usage_unknown,
                                       "reported_estimated_cost_usd": budget.cost}
            record.save()
            write(directory / "boundary.json", sidecar)
    result = TrialResult(trial_id=plan.trial_id, execution="failed" if failure else "finished",
        stage=stage, failure=failure, usage=state["usage"], internal_model_calls=budget.calls,
        sdk_cycles=sum(e.get("kind") == "model_cycle" for e in state["events"]),
        provider_dispatches=sum(c["kind"] == "provider_dispatch" for c in sidecar["captures"]),
        provider_attempts=sum(c["kind"] == "provider_attempt" for c in sidecar["captures"]),
        usage_unknown=budget.usage_unknown if plan.backend == "live" else False,
        synthetic_usage=plan.backend == "scripted", ended_at=now(),
        artifacts={"state": "result.json", "boundaries": "boundary.json", "plan": "plan.json"})
    if result.synthetic_usage:
        result.usage = {"synthetic": True, "measured_cost_usd": None, "measured_cache": "not_observed",
                        "sdk_test_usage": state["usage"]}
    raw_usage = [c["payload"]["usage"] for c in sidecar["captures"] if c["kind"] == "provider_metadata" and "usage" in c["payload"]]
    result.cache = {"status": "reported" if raw_usage and all("cacheReadInputTokens" in u and "cacheWriteInputTokens" in u for u in raw_usage) else "not_observed",
        "read_tokens": sum(u["cacheReadInputTokens"] for u in raw_usage) if raw_usage and all("cacheReadInputTokens" in u for u in raw_usage) else None,
        "write_tokens": sum(u["cacheWriteInputTokens"] for u in raw_usage) if raw_usage and all("cacheWriteInputTokens" in u for u in raw_usage) else None,
        "raw_responses": raw_usage,
        "limitation": "First observed is not guaranteed cold. Missing raw keys are unknown. Synthetic responses do not measure caching."}
    result.checks = evaluate(plan, result, sidecar, workspace.database, integrity)
    write(directory / "trial.json", result)
    render_report(directory, plan, result, sidecar)
    return result
