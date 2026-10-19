import json
from datetime import datetime, timezone
from dataclasses import replace
from importlib.metadata import version
from pathlib import Path
from typing import Callable

from .run import RunRecord, RunState
from .telemetry import (
    workflow_span, stage_span, reject_attempt, current_langfuse_trace_url, finish_workflow_trace,
)
from opentelemetry.trace import Status, StatusCode

import plotly.graph_objects as go

from . import agents
from .analysis import (
    AnalysisAcceptanceError, analysis_statement, accept_analyses, load_result_frame, result_metrics, summarize_models,
)
from .visualisation import (
    PlotValidationError, validate_plot_candidate, representation_selections,
)
from .agent_runtime import InvocationLimitError
from .modelling_validation import plan_issues, validate_built_models
from .core.contracts import (
    CONTRACT_VERSION, AcceptedAnalysisResult, AnalysisPlan, AnalysisSpec, AnalyticalPlan, DbtImplementation,
)
from .context_cost import configured_cost_experiments
from .data import load_raw, profile, relationships, schemas
from .dbt import dbt_results, run_dbt, write_project
from .experiments import provenance, record_run
from .config import ReleaseSettings, current_settings, use_settings, check_limit
from .execution import current_budget, use_budget
from .storage import cleanup_run_workspaces
from .workspace import RunWorkspace, validate_submission
from .tools.result_tools import DuckDBResultTools, EvidenceRegistry, render_finding
from .usage import summarize_usage


AcceptedResults = list[tuple[AnalysisSpec, AcceptedAnalysisResult]]


MAX_CONCEPTUAL_REVISIONS = 1
MAX_DBT_REPAIRS = 2
MAX_ANALYSIS_REPAIRS = 1
MAX_FINDING_REPAIRS = 1

def run(paths: list[Path], request: str, root: Path, *, on_event: Callable | None = None,
        workspace: RunWorkspace | None = None, settings: ReleaseSettings | None = None) -> RunState:
    """Run the explicit stages inside one workspace, settings snapshot and budget."""
    settings = settings or current_settings()
    settings = replace(settings, runs_root=str((workspace.root if workspace else root).resolve()))
    settings.validate()
    uploads = [(path.name, path.read_bytes()) for path in paths]
    validate_submission(uploads, request, settings)
    workspace = workspace or RunWorkspace.create(root)
    try:
        if all(path.resolve().parent == workspace.input_dir for path in paths):
            if any(path.is_symlink() for path in paths):
                raise ValueError("Workspace inputs must not be symlinks")
        else:
            paths = workspace.persist(uploads, settings)
        with use_settings(settings), use_budget(settings), workflow_span(workspace.run_id, request) as span:
            result = _run(paths, request, workspace, on_event=on_event)
            if result["status"] == "failed":
                span.set_status(Status(StatusCode.ERROR, result.get("error", "Workflow failed")))
            finish_workflow_trace(span, result)
            return result
    finally:
        workspace.remove_inputs()


def _run(paths: list[Path], request: str, workspace: RunWorkspace, *, on_event: Callable | None = None) -> RunState:
    # Snapshot once so every stage and repair in an A/B run uses the same controls.
    cost_experiments = configured_cost_experiments()
    settings = current_settings()
    root = workspace.root
    cleanup = cleanup_run_workspaces(root, active={workspace.run_id}, settings=settings)
    run_id, run_dir = workspace.run_id, workspace.run_dir
    state: RunState = {
        "status": "running",
        "release_settings": settings.snapshot(),
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "request": request,
        "run_dir": str(run_dir),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "storage_cleanup": cleanup,
        "events": [],
        "cost_experiments": sorted(cost_experiments),
        "strands_version": version("strands-agents"),
    }
    record = RunRecord(workspace, state, on_event)
    if trace_url := current_langfuse_trace_url():
        state["langfuse_trace_url"] = trace_url
    events = state["events"]

    try:
        names, profiles, links = _prepare_sources(record, paths)
        plan = _build_conceptual_model(record, request, names, profiles, links, cost_experiments)
        model_names = _build_models(record, names, plan, profiles, links, cost_experiments)
        analysis, accepted_results = _accept_analysis(record, request, plan, model_names, cost_experiments)
        # Finding is disabled pending removal; keep its implementation and saved-run fields.
        state["findings"] = []
        state["finding_attempts"] = []
        state["evidence"] = []
        # _generate_findings(record, accepted_results, cost_experiments)
        _render_visualisations(record, accepted_results, cost_experiments)
        state["quality_warnings"] = (
            [
                f"{r['orphan_rows']} orphan rows: {r['from']} → {r['to']}"
                for r in links
                if r["orphan_rows"]
            ]
            + [
                f"{n}: {p['duplicate_rows']} duplicate rows"
                for n, p in profiles.items()
                if p["duplicate_rows"]
            ]
            + analysis.warnings
            + [warning for finding in state["findings"] for warning in finding["caveats"]]
        )
        record.complete("Validated dashboard is ready")
    except Exception as exc:
        record.fail(f"{type(exc).__name__}: {exc}")
    budget = current_budget()
    state["runtime_budget"] = {"internal_model_calls": budget.calls, "reported_estimated_cost_usd": budget.cost,
                               "usage_unknown": budget.usage_unknown}
    state["usage"] = summarize_usage(events)
    record.save()
    _track(record)
    return state


def _save_dbt_attempt(record: RunRecord, number: int, implementation: DbtImplementation) -> None:
    state, emit, run_dir = record.state, record.emit, record.workspace.run_dir
    attempts_dir = run_dir / "attempts"
    attempts_dir.mkdir(exist_ok=True)
    path = attempts_dir / f"dbt_{number:02d}.json"
    path.write_text(implementation.model_dump_json(indent=2))
    state.setdefault("dbt_attempts", []).append(
        {"attempt": number, "artifact": str(path),
         "files": [item.path for item in implementation.files]}
    )
    emit("generating dbt models", "artifact", f"Saved dbt proposal {number}",
         {"path": str(path)})


def _track(record: RunRecord) -> None:
    state, root = record.state, record.workspace.root
    try:
        record_run(root, state)
    except Exception as exc:
        state["experiment_tracker_error"] = f"{type(exc).__name__}: {exc}"
        record.emit("experiment tracking", "failure", state["experiment_tracker_error"], notify=False)


def _prepare_sources(record: RunRecord, paths: list[Path]) -> tuple[list[str], dict, list[dict]]:
    state, emit = record.state, record.emit
    database = record.workspace.database
    settings = current_settings()
    state["experiment"] = provenance(paths)
    emit(
        "loading data",
        "start",
        "Loading original CSV/Parquet/JSON data into DuckDB raw schema",
    )
    _track(record)
    names = load_raw(database, paths)
    state["raw_names"] = names
    emit("loading data", "done", f"Loaded {len(names)} raw datasets")

    emit(
        "profiling data",
        "start",
        "Profiling columns, uniqueness, nulls and duplicate rows",
    )
    profiles = profile(database, names)
    check_limit(len(json.dumps(profiles, default=str)), settings.max_profile_chars, "Serialized source profile")
    state["profiles"] = profiles
    emit("profiling data", "done", "Raw data profile complete")

    emit(
        "inferring relationships",
        "start",
        "Checking shared keys and orphan references",
    )
    links = relationships(database, profiles)
    state["relationships"] = links
    emit(
        "inferring relationships",
        "done",
        f"Found {len(links)} candidate relationships",
    )

    return names, profiles, links


def _build_conceptual_model(record: RunRecord, request: str, names: list[str],
                            profiles: dict, links: list[dict], cost_experiments: frozenset[str]) -> AnalyticalPlan:
    state, emit = record.state, record.emit
    emit(
        "conceptual modelling",
        "start",
        "Interpreting business request and source evidence",
    )
    plan = None
    issues = []
    state["conceptual_attempts"] = []
    for revision in range(MAX_CONCEPTUAL_REVISIONS + 1):
        with stage_span(record.workspace.run_id, "conceptual modelling", attempt=revision + 1) as trace_attributes:
            plan = agents.conceptual(request, profiles, links, emit, feedback=issues, previous=plan,
                                     cost_experiments=cost_experiments, trace_attributes=trace_attributes)
            attempt = {"attempt": revision + 1, "status": "proposed", "proposal": plan.model_dump()}
            state["conceptual_attempts"].append(attempt)
            record.save()
            issues = plan_issues(plan, profiles, links)
            attempt.update(status="rejected" if issues else "accepted", issues=[issue.model_dump() for issue in issues])
            if issues:
                reject_attempt("; ".join(map(str, issues)))
            if not issues:
                break
            emit("conceptual modelling", "failure", f"Plan attempt {revision + 1} failed reference validation",
                 {"attempt": revision + 1, "issues": attempt["issues"]})
            if revision >= MAX_CONCEPTUAL_REVISIONS:
                break
            emit(
                "conceptual modelling",
                "repair",
                f"Revising plan after {len(issues)} source/plan contract failures",
                {"issues": attempt["issues"], "revision": revision + 1},
            )
    if issues:
        raise ValueError(f"AnalyticalPlan validation failed: {'; '.join(map(str, issues))}")
    emit(
        "conceptual modelling",
        "contract_check",
        "Source references and planned output links resolve in this run",
        {"models": [m.name for m in plan.dimensions + plan.facts],
         "sources": names},
    )
    state["plan"] = plan.model_dump()
    emit("conceptual modelling", "done", "Model plan accepted")

    return plan


def _build_models(record: RunRecord, names: list[str], plan: AnalyticalPlan,
                  profiles: dict, links: list[dict], cost_experiments: frozenset[str]) -> list[str]:
    state, emit = record.state, record.emit
    database = record.workspace.database
    settings = current_settings()
    project = record.workspace.dbt_dir
    emit(
        "generating dbt models",
        "start",
        "Creating dbt sources, dimensions, facts and tests",
    )
    expected_models = {
        item.name for item in plan.dimensions + plan.facts
    }
    repair_count = 0
    built_schemas = None
    validation_failure = ""
    implementation = None
    while True:
        with stage_span(record.workspace.run_id, "generating dbt models", attempt=repair_count + 1) as trace_attributes:
            implementation = agents.implement(
                plan, profiles, links, emit, failure=validation_failure[-5000:], previous=implementation,
                cost_experiments=cost_experiments,
                trace_attributes=trace_attributes,
            )
            _save_dbt_attempt(record, repair_count + 1, implementation)
            produced_models = {
                f.path[:-4] for f in implementation.files if f.path.endswith(".sql")
            }
            validation_failure = ""
            if expected_models != produced_models:
                validation_failure = (
                    "dbt model set differs from plan: "
                    f"missing={sorted(expected_models - produced_models)}, "
                    f"extra={sorted(produced_models - expected_models)}"
                )
            else:
                try:
                    write_project(project, database, names, implementation)
                    state["dbt_explanation"] = implementation.explanation
                    emit("generating dbt models", "done", f"Wrote {len(implementation.files)} model files")
                except Exception as exc:
                    validation_failure = f"dbt project write failed: {type(exc).__name__}: {exc}"
            if not validation_failure:
                emit("dbt compile/build/test", "start", "Compiling dbt project")
                compile_result = run_dbt(project, "compile")
                compile_result["attempt"] = repair_count + 1
                state.setdefault("dbt_runs", []).append(compile_result)
                emit(
                    "dbt compile/build/test", "result",
                    f"compile: {'passed' if compile_result['ok'] else 'failed'}",
                )
                if compile_result["ok"]:
                    build_result = run_dbt(project, "build")
                    build_result["attempt"] = repair_count + 1
                    state["dbt_runs"].append(build_result)
                    emit(
                        "dbt compile/build/test", "result",
                        f"build: {'passed' if build_result['ok'] else 'failed'}",
                    )
                    if build_result["ok"]:
                        test_result = run_dbt(project, "test")
                        test_result["attempt"] = repair_count + 1
                        state["dbt_runs"].append(test_result)
                        emit(
                            "dbt compile/build/test", "result",
                            f"test: {'passed' if test_result['ok'] else 'failed'}",
                        )
                    else:
                        test_result = build_result
                else:
                    test_result = compile_result
                state["dbt_results"] = dbt_results(project)
                if test_result["ok"]:
                    try:
                        built_schemas = schemas(database, sorted(expected_models))
                        issues = validate_built_models(database, plan, built_schemas)
                        validation_failure = "; ".join(issues)
                    except Exception as exc:
                        validation_failure = f"Built-schema inspection failed: {type(exc).__name__}: {exc}"
                    if not validation_failure:
                        emit(
                            "dbt compile/build/test", "contract_check",
                            "Built model outputs match this run's plan",
                            {"model_columns": built_schemas},
                        )
                        state["dbt_attempts"][-1]["status"] = "accepted"
                        break
                else:
                    validation_failure = test_result["output"][-5000:]
                    failures = [item for item in state["dbt_results"] if item["status"] not in {"pass", "success"}]
                    if failures:
                        validation_failure += "\nFailing dbt nodes: " + json.dumps(failures)
                    try:
                        current_schemas = schemas(database, sorted(expected_models))
                        model_issues = validate_built_models(database, plan, current_schemas)
                        if model_issues:
                            validation_failure += "\nObserved model contract failures: " + "; ".join(model_issues)
                    except Exception as exc:
                        validation_failure += f"\nModel inspection unavailable: {type(exc).__name__}: {exc}"
            state["dbt_attempts"][-1].update(status="rejected", error=validation_failure[-5000:])
            reject_attempt(validation_failure[-1500:])
            emit("validation and repair", "failure", validation_failure[-1500:])
            if repair_count >= MAX_DBT_REPAIRS:
                raise RuntimeError(
                    f"dbt validation failed after {repair_count} repairs: {validation_failure[-1000:]}"
                )
            repair_count += 1
            emit(
                "validation and repair",
                "repair",
                f"Requesting dbt repair {repair_count}/{MAX_DBT_REPAIRS}",
                {"failure": validation_failure[-5000:]},
            )
    state["repair_attempts"] = repair_count

    model_names = sorted(expected_models)
    state["model_names"] = model_names
    state["model_schemas"] = built_schemas
    emit(
        "validation and repair",
        "done",
        "Validated dbt tables are available in DuckDB",
    )

    return model_names


def _accept_analysis(record: RunRecord, request: str, plan: AnalyticalPlan,
                     model_names: list[str], cost_experiments: frozenset[str]) -> tuple[AnalysisPlan, AcceptedResults]:
    state, emit = record.state, record.emit
    database = record.workspace.database
    settings = current_settings()
    summaries = summarize_models(database, model_names)
    emit(
        "analytical interpretation",
        "start",
        "Deriving analytical results from validated models",
    )
    analysis = None
    accepted_results = None
    feedback = ""
    state["analysis_attempts"] = []
    for attempt in range(MAX_ANALYSIS_REPAIRS + 1):
        with stage_span(record.workspace.run_id, "analytical interpretation", attempt=attempt + 1) as trace_attributes:
            proposed = agents.analyse(
                request,
                plan,
                state["model_schemas"],
                summaries,
                emit,
                database,
                feedback=feedback,
                previous=analysis,
                cost_experiments=cost_experiments,
                trace_attributes=trace_attributes,
            )
            analysis = proposed
            emit(
                "analysis acceptance",
                "start",
                f"Validating and registering analysis attempt {attempt + 1}",
                {"titles": [spec.title for spec in proposed.analyses]},
            )
            try:
                check_limit(len(proposed.analyses), settings.max_analyses, "Analysis count")
                accepted_results = accept_analyses(database, model_names, proposed)
                state["analysis_attempts"].append(
                    {"attempt": attempt + 1, "status": "accepted",
                     "proposal": proposed.model_dump()}
                )
                break
            except AnalysisAcceptanceError as exc:
                feedback = str(exc)
                reject_attempt(feedback)
                state["analysis_attempts"].append(
                    {"attempt": attempt + 1, "status": "rejected", "index": exc.index,
                     "title": exc.title, "error": exc.database_error,
                     "proposal": proposed.model_dump()}
                )
                emit(
                    "analysis acceptance",
                    "failure",
                    f"Attempt {attempt + 1}: {feedback}",
                    {"index": exc.index, "title": exc.title, "error": exc.database_error},
                )
                if attempt >= MAX_ANALYSIS_REPAIRS:
                    raise
                emit(
                    "analytical interpretation",
                    "repair",
                    f"Requesting analysis correction {attempt + 1}/{MAX_ANALYSIS_REPAIRS}",
                    {"error": feedback},
                )
    assert analysis is not None and accepted_results is not None
    state["analysis"] = analysis.model_dump()
    emit(
        "analytical interpretation",
        "done",
        f"Selected {len(analysis.analyses)} accepted analytical queries",
    )

    state["analysis_results"] = []

    for spec, accepted_result in accepted_results:
        state["analysis_results"].append(
            {
                "title": spec.title,
                "result_id": accepted_result.result_id,
                "relation": accepted_result.relation,
                "rows": accepted_result.row_count,
                "columns": accepted_result.columns,
                "acceptance_id": accepted_result.acceptance_id,
                "executed_sql": analysis_statement(spec, analysis, model_names),
                "concept_id": spec.concept_id,
            }
        )
        emit(
            "analysis acceptance",
            "contract_check",
            f"Accepted {spec.title} as {accepted_result.relation}",
            {"row_count": accepted_result.row_count,
             "columns": accepted_result.columns,
             "concept_id": spec.concept_id,
             "qualification_id": getattr(spec.query, "qualification_id", None)},
        )

    emit(
        "analysis acceptance",
        "done",
        f"Accepted {len(accepted_results)} analytical results",
    )
    state["metrics"] = result_metrics(database, accepted_results)

    return analysis, accepted_results


def _generate_findings(record: RunRecord, accepted_results: AcceptedResults,
                       cost_experiments: frozenset[str]) -> None:
    state, emit = record.state, record.emit
    database = record.workspace.database
    state["findings"] = []
    state["finding_attempts"] = []
    state["evidence"] = []
    for result_index, (spec, accepted_result) in enumerate(accepted_results):
        def register_evidence(evidence):
            state["evidence"].append(evidence)
            emit("analytical finding", "evidence", "Stored accepted-result evidence",
                 {"evidence_id": evidence["id"], "acceptance_id": evidence["acceptance_id"],
                  "rows": len(evidence["rows"]), "truncated": evidence["truncated"]})

        registry = EvidenceRegistry(accepted_result.acceptance_id, accepted_result.relation, register_evidence)
        result_tools = DuckDBResultTools(database, accepted_result.relation, accepted_result.columns, spec, registry)
        finding_error = ""
        finding = None
        for attempt in range(MAX_FINDING_REPAIRS + 1):
            with stage_span(record.workspace.run_id, "analytical finding", attempt=attempt + 1, result_index=result_index) as trace_attributes:
                finding = agents.interpret(
                    spec, accepted_result, emit, result_tools, feedback=finding_error, previous=finding,
                    executed_sql=state["analysis_results"][result_index]["executed_sql"],
                    cost_experiments=cost_experiments,
                    trace_attributes=trace_attributes,
                )
                try:
                    finding_text, evidence = render_finding(finding, registry)
                    state["findings"].append(
                        {"title": spec.title, "text": finding_text,
                         "evidence": evidence, "caveats": finding.caveats,
                         "methodology": {"question": spec.question, "population": spec.population_definition,
                                         "observation_rule": spec.observation_rule, "grain": spec.grain,
                                         "rationale": spec.rationale}}
                    )
                    state["finding_attempts"].append(
                        {"title": spec.title, "attempt": attempt + 1,
                         "status": "accepted", "proposal": finding.model_dump()}
                    )
                    emit(
                        "analytical finding",
                        "contract_check",
                        f"Finding for {spec.title} uses accepted result evidence",
                        {"relation": accepted_result.relation,
                         "evidence_ids": [item["id"] for item in evidence]},
                    )
                    break
                except Exception as exc:
                    finding_error = f"{type(exc).__name__}: {exc}"
                    reject_attempt(finding_error)
                    state["finding_attempts"].append(
                        {"title": spec.title, "attempt": attempt + 1,
                         "status": "rejected", "error": finding_error,
                         "proposal": finding.model_dump()}
                    )
                    emit(
                        "analytical finding",
                        "failure",
                        f"Finding attempt {attempt + 1} failed for {spec.title}: {finding_error}",
                    )
                    if attempt >= MAX_FINDING_REPAIRS:
                        raise


def _render_visualisations(record: RunRecord, accepted_results: AcceptedResults,
                           cost_experiments: frozenset[str]) -> None:
    state, emit = record.state, record.emit
    database = record.workspace.database
    settings = current_settings()
    run_dir = record.workspace.run_dir
    emit(
        "visualisation generation",
        "start",
        "Rendering Plotly figures from accepted analytical results",
    )
    state["plot_specs"] = []
    state["plot_attempts"] = []
    state["charts"] = []
    for index, (spec, accepted_result) in enumerate(accepted_results):
        if accepted_result.row_count == 0:
            check_limit(len(state["charts"]) + 1, settings.max_charts, "Chart count")
            figure = go.Figure()
            figure.update_layout(title=spec.title)
            figure.add_annotation(text="No rows matched the analysis criteria", showarrow=False)
            figure.write_json(run_dir / f"chart_{index}.json")
            state["plot_specs"].append(None)
            state["charts"].append({"result_index": index, "concept_id": spec.concept_id,
                                    "title": spec.title, "file": f"chart_{index}.json"})
            state["plot_attempts"].append(
                {"title": spec.title, "status": "empty_result", "relation": accepted_result.relation}
            )
            emit(
                "visualisation generation", "contract_check",
                f"Rendered an empty-result figure for {spec.title}",
                {"relation": accepted_result.relation, "row_count": 0},
            )
            continue
        frame = load_result_frame(database, accepted_result)
        for variant_index, (label, selection) in enumerate(representation_selections(database, spec, accepted_result)):
            with stage_span(record.workspace.run_id, "visualisation generation", result_index=index) as trace_attributes:
                title = spec.title if label is None else f"{spec.title} — {label}"
                check_limit(len(state["charts"]) + 1, settings.max_charts, "Chart count")
                plot = None
                try:
                    plot = agents.plot(spec, accepted_result, emit, database,
                                       selection=selection, original_request=state["request"],
                                       frame=frame, title=title,
                                       cost_experiments=cost_experiments, trace_attributes=trace_attributes)
                    fig = validate_plot_candidate(frame, plot, accepted_result, selection, title)
                except (InvocationLimitError, PlotValidationError) as exc:
                    state["plot_attempts"].append(
                        {"title": title, "status": "skipped", "result_index": index,
                         "concept_id": spec.concept_id, "relation": accepted_result.relation,
                         "plot": plot.model_dump() if plot is not None else None,
                         "presentation_selection": [f.model_dump() for f in selection],
                         "error": f"{type(exc).__name__}: {exc}"}
                    )
                    emit("visualisation generation", "failure", f"Chart skipped for {title}: {type(exc).__name__}: {exc}",
                         {"relation": accepted_result.relation,
                          "presentation_selection": [f.model_dump() for f in selection],
                          "plot": plot.model_dump() if plot is not None else None})
                    continue
                filename = f"chart_{index}_{variant_index}.json"
                fig.write_json(run_dir / filename)
                state["charts"].append({"result_index": index, "concept_id": spec.concept_id,
                                        "title": title, "file": filename})
                state["plot_attempts"].append(
                    {"title": title, "status": "accepted", "plot": plot.model_dump()}
                )
                state["plot_specs"].append(plot.model_dump())
                emit("visualisation generation", "contract_check", f"Rendered {title} from {accepted_result.relation}",
                     {"function": plot.function, "columns": plot.columns, "options": plot.options,
                      "filters": [f.model_dump() for f in plot.filters]})
    emit("visualisation generation", "done", f"Rendered {len(state['charts'])} figures")
