import json
import uuid
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path
from threading import Lock
from typing import Callable

import plotly.graph_objects as go

from . import agents
from .analysis import (
    AnalysisAcceptanceError,
    analysis_statement,
    accept_analyses,
    load_result_frame,
    render_plot,
    result_metrics,
    summarize_models,
    validate_built_models,
    validate_plot_spec,
    representation_selections,
)
from .core.contracts import CONTRACT_VERSION, plan_issues
from .context_cost import configured_cost_experiments
from .data import load_raw, profile, relationships, schemas
from .dbt import dbt_results, run_dbt, write_project
from .experiments import provenance, record_run
from .storage import DUCKDB_FILENAME, cleanup_run_databases
from .tools.result_tools import DuckDBResultTools, EvidenceRegistry, render_finding
from .usage import summarize_usage


MAX_CONCEPTUAL_REVISIONS = 1
MAX_DBT_REPAIRS = 2
MAX_ANALYSIS_REPAIRS = 1
MAX_FINDING_REPAIRS = 1

def run(
    paths: list[Path],
    request: str,
    root: Path,
    *,
    on_event: Callable | None = None,
) -> dict:
    # Snapshot once so every stage and repair in an A/B run uses the same controls.
    cost_experiments = configured_cost_experiments()
    root = root.resolve()
    cleanup = cleanup_run_databases(root)
    run_id = f"{datetime.now(timezone.utc):%y%m%d_%H%M}_{uuid.uuid4().hex[:12]}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    events = []
    state = {
        "status": "running",
        "contract_version": CONTRACT_VERSION,
        "run_id": run_id,
        "request": request,
        "run_dir": str(run_dir),
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "storage_cleanup": cleanup,
        "events": events,
        "cost_experiments": sorted(cost_experiments),
        "strands_version": version("strands-agents"),
    }

    save_lock = Lock()

    def save():
        # Concurrent evidence tools share this run's temporary file.
        with save_lock:
            temporary = run_dir / "result.json.tmp"
            temporary.write_text(json.dumps(state, indent=2, default=str))
            temporary.replace(run_dir / "result.json")

    def emit(stage: str, kind: str, message: str, detail=None):
        event = {
            "time": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "kind": kind,
            "message": message,
        }
        if detail:
            event["detail"] = detail
        events.append(event)
        save()
        if on_event:
            on_event(event)

    database = run_dir / DUCKDB_FILENAME
    project = run_dir / "dbt"

    def save_dbt_attempt(number, implementation):
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

    def track():
        try:
            record_run(root, state)
        except Exception as exc:
            state["experiment_tracker_error"] = f"{type(exc).__name__}: {exc}"
            events.append({"time": datetime.now(timezone.utc).isoformat(),
                           "stage": "experiment tracking", "kind": "failure",
                           "message": state["experiment_tracker_error"]})
            save()

    try:
        state["experiment"] = provenance(paths)
        emit(
            "loading data",
            "start",
            "Loading original CSV/Parquet/JSON data into DuckDB raw schema",
        )
        track()
        names = load_raw(database, paths)
        state["raw_names"] = names
        emit("loading data", "done", f"Loaded {len(names)} raw datasets")

        emit(
            "profiling data",
            "start",
            "Profiling columns, uniqueness, nulls and duplicate rows",
        )
        profiles = profile(database, names)
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

        emit(
            "conceptual modelling",
            "start",
            "Interpreting business request and source evidence",
        )
        plan = None
        issues = []
        state["conceptual_attempts"] = []
        for revision in range(MAX_CONCEPTUAL_REVISIONS + 1):
            plan = agents.conceptual(request, profiles, links, emit, feedback=issues, previous=plan,
                                     cost_experiments=cost_experiments)
            attempt = {"attempt": revision + 1, "status": "proposed", "proposal": plan.model_dump()}
            state["conceptual_attempts"].append(attempt)
            save()
            issues = plan_issues(plan, profiles, links)
            attempt.update(status="rejected" if issues else "accepted", issues=[issue.model_dump() for issue in issues])
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
            implementation = agents.implement(
                plan, profiles, links, emit, failure=validation_failure[-5000:], previous=implementation,
                cost_experiments=cost_experiments,
            )
            save_dbt_attempt(repair_count + 1, implementation)
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
            )
            analysis = proposed
            emit(
                "analysis acceptance",
                "start",
                f"Validating and registering analysis attempt {attempt + 1}",
                {"titles": [spec.title for spec in proposed.analyses]},
            )
            try:
                accepted_results = accept_analyses(database, model_names, proposed)
                state["analysis_attempts"].append(
                    {"attempt": attempt + 1, "status": "accepted",
                     "proposal": proposed.model_dump()}
                )
                break
            except AnalysisAcceptanceError as exc:
                feedback = str(exc)
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

        state["findings"] = []
        state["finding_attempts"] = []
        state["evidence"] = []
        for spec, accepted_result in accepted_results:
            def register_evidence(record):
                state["evidence"].append(record)
                emit("analytical finding", "evidence", "Stored accepted-result evidence",
                     {"evidence_id": record["id"], "acceptance_id": record["acceptance_id"],
                      "rows": len(record["rows"]), "truncated": record["truncated"]})

            registry = EvidenceRegistry(accepted_result.acceptance_id, accepted_result.relation, register_evidence)
            result_tools = DuckDBResultTools(database, accepted_result.relation, accepted_result.columns, spec, registry)
            finding_error = ""
            finding = None
            for attempt in range(MAX_FINDING_REPAIRS + 1):
                finding = agents.interpret(
                    spec, accepted_result, emit, result_tools, feedback=finding_error, previous=finding,
                    cost_experiments=cost_experiments,
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
            for variant_index, (label, selection) in enumerate(representation_selections(database, spec, accepted_result)):
                title = spec.title if label is None else f"{spec.title} — {label}"
                plot = agents.plot(spec, accepted_result, state["findings"][index], emit, database,
                                   selection=selection, cost_experiments=cost_experiments)
                try:
                    validate_plot_spec(plot, accepted_result, selection)
                    frame = load_result_frame(database, accepted_result)
                    fig = render_plot(frame, plot, title)
                    filename = f"chart_{index}_{variant_index}.json"
                    fig.write_json(run_dir / filename)
                    state["charts"].append({"result_index": index, "concept_id": spec.concept_id,
                                            "title": title, "file": filename})
                except Exception as exc:
                    state["plot_attempts"].append(
                        {"title": title, "status": "rejected", "plot": plot.model_dump(),
                         "presentation_selection": [f.model_dump() for f in selection],
                         "error": f"{type(exc).__name__}: {exc}"}
                    )
                    emit("visualisation generation", "failure", f"Plot failed for {title}: {type(exc).__name__}: {exc}",
                         {"relation": accepted_result.relation,
                          "presentation_selection": [f.model_dump() for f in selection],
                          "plot": plot.model_dump()})
                    raise
                state["plot_attempts"].append(
                    {"title": title, "status": "accepted", "plot": plot.model_dump()}
                )
                state["plot_specs"].append(plot.model_dump())
                emit("visualisation generation", "contract_check", f"Rendered {title} from {accepted_result.relation}",
                     {"function": plot.function, "columns": plot.columns, "options": plot.options,
                      "filters": [f.model_dump() for f in plot.filters]})
        emit("visualisation generation", "done", f"Rendered {len(state['charts'])} figures")
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
        state["status"] = "complete"
        emit("dashboard completion", "done", "Validated dashboard is ready")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        emit("dashboard completion", "failure", state["error"])
    state["usage"] = summarize_usage(events)
    save()
    track()
    return state
