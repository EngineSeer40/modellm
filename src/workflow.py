import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import plotly.graph_objects as go

from . import agents
from .analysis import (
    AnalysisAcceptanceError,
    accept_analyses,
    load_result_frame,
    render_plot,
    result_metrics,
    summarize_models,
    validate_built_models,
    validate_plot_spec,
)
from .core.contracts import plan_issues
from .data import load_raw, profile, relationships, schemas
from .dbt import dbt_results, run_dbt, write_project
from .storage import DUCKDB_FILENAME, cleanup_run_databases
from .tools.result_tools import DuckDBResultTools, render_finding


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
    root = root.resolve()
    cleanup = cleanup_run_databases(root)
    run_id = f"{datetime.now(timezone.utc):%y%m%d_%H%M}_{uuid.uuid4().hex[:12]}"
    run_dir = root / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    events = []
    state = {
        "status": "running",
        "run_id": run_id,
        "request": request,
        "run_dir": str(run_dir),
        "storage_cleanup": cleanup,
        "events": events,
    }

    def save():
        (run_dir / "result.json").write_text(json.dumps(state, indent=2, default=str))

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

    try:
        emit(
            "loading data",
            "start",
            "Loading original CSV/Parquet/JSON data into DuckDB raw schema",
        )
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
        plan = agents.conceptual(request, profiles, links, emit)
        issues = plan_issues(plan, profiles, links)
        for revision in range(MAX_CONCEPTUAL_REVISIONS):
            if not issues:
                break
            emit(
                "conceptual modelling",
                "repair",
                f"Revising plan after {len(issues)} source/plan contract failures",
                {"issues": issues, "revision": revision + 1},
            )
            plan = agents.conceptual(request, profiles, links, emit, feedback=issues)
            issues = plan_issues(plan, profiles, links)
        if issues:
            raise ValueError(f"AnalyticalPlan validation failed: {'; '.join(issues)}")
        emit(
            "conceptual modelling",
            "contract_check",
            "Source references and planned output links resolve in this run",
            {"models": [m.name for m in plan.target_model.dimensions + plan.target_model.facts],
             "sources": names},
        )
        state["plan"] = plan.model_dump()
        emit("conceptual modelling", "done", plan.objective)

        emit(
            "generating dbt models",
            "start",
            "Creating dbt sources, dimensions, facts and tests",
        )
        expected_models = {
            item.name for item in plan.target_model.dimensions + plan.target_model.facts
        }
        repair_count = 0
        built_schemas = None
        validation_failure = ""
        while True:
            implementation = agents.implement(
                plan, profiles, links, emit, failure=validation_failure[-5000:]
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
                }
            )
            emit(
                "analysis acceptance",
                "contract_check",
                f"Accepted {spec.title} as {accepted_result.relation}",
                {"row_count": accepted_result.row_count,
                 "columns": accepted_result.columns,
                 "concept_id": spec.concept_id,
                 "qualification_id": spec.qualification_id},
            )

        emit(
            "analysis acceptance",
            "done",
            f"Accepted {len(accepted_results)} analytical results",
        )
        state["metrics"] = result_metrics(database, accepted_results)

        state["findings"] = []
        state["finding_attempts"] = []
        for spec, accepted_result in accepted_results:
            finding_error = ""
            for attempt in range(MAX_FINDING_REPAIRS + 1):
                finding = agents.interpret(
                    spec, accepted_result, emit, database, feedback=finding_error
                )
                result_tools = DuckDBResultTools(
                    database=database,
                    relation=accepted_result.relation,
                    columns=set(accepted_result.columns),
                    spec=spec,
                )
                try:
                    finding_text, evidence = render_finding(finding, result_tools)
                    state["findings"].append(
                        {"title": spec.title, "text": finding_text,
                         "evidence": evidence, "warnings": finding.warnings}
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
        for index, (spec, accepted_result) in enumerate(accepted_results):
            if accepted_result.row_count == 0:
                figure = go.Figure()
                figure.update_layout(title=spec.title)
                figure.add_annotation(text="No rows matched the analysis criteria", showarrow=False)
                figure.write_json(run_dir / f"chart_{index}.json")
                state["plot_specs"].append(None)
                state["plot_attempts"].append(
                    {"title": spec.title, "status": "empty_result", "relation": accepted_result.relation}
                )
                emit(
                    "visualisation generation", "contract_check",
                    f"Rendered an empty-result figure for {spec.title}",
                    {"relation": accepted_result.relation, "row_count": 0},
                )
                continue
            plot = agents.plot(spec, accepted_result, state["findings"][index]["text"], emit, database)
            try:
                validate_plot_spec(plot, accepted_result)
                frame = load_result_frame(database, accepted_result)
                fig = render_plot(frame, plot, spec.title)
                fig.write_json(run_dir / f"chart_{index}.json")
            except Exception as exc:
                state["plot_attempts"].append(
                    {"title": spec.title, "status": "rejected",
                     "plot": plot.model_dump(), "error": f"{type(exc).__name__}: {exc}"}
                )
                emit("visualisation generation", "failure",
                     f"Plot failed for {spec.title}: {type(exc).__name__}: {exc}")
                raise
            state["plot_attempts"].append(
                {"title": spec.title, "status": "accepted", "plot": plot.model_dump()}
            )
            emit(
                "visualisation generation",
                "contract_check",
                f"Plot fields for {spec.title} resolve only in {accepted_result.relation}",
                {"columns": plot.columns,
                 "filters": [item.model_dump() for item in plot.filters]},
            )
            state["plot_specs"].append(plot.model_dump())
        emit("visualisation generation", "done", f"Rendered {len(accepted_results)} figures")
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
            + [warning for finding in state["findings"] for warning in finding["warnings"]]
        )
        state["status"] = "complete"
        emit("dashboard completion", "done", "Validated dashboard is ready")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        emit("dashboard completion", "failure", state["error"])
    save()
    return state
