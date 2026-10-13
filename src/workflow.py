import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import agents
from .analysis import (
    accept_analyses,
    load_result_frame,
    render_plot,
    result_metrics,
    summarize_models,
)
from .core.contracts import plan_issues
from .data import load_raw, profile, relationships, schemas
from .dbt import dbt_results, run_dbt, write_project
from .storage import DUCKDB_FILENAME, cleanup_run_databases

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
        issues = plan_issues(plan, names)
        if issues:
            emit(
                "conceptual modelling",
                "repair",
                f"Revising disconnected or invalid plan: {'; '.join(issues)}",
            )
            plan = agents.conceptual(request, profiles, links, emit, feedback=issues)
            issues = plan_issues(plan, names)
        if issues:
            raise ValueError(f"AnalyticalPlan validation failed: {'; '.join(issues)}")
        state["plan"] = plan.model_dump()
        emit("conceptual modelling", "done", plan.objective)

        emit(
            "generating dbt models",
            "start",
            "Creating dbt sources, dimensions, facts and tests",
        )
        implementation = agents.implement(plan, profiles, links, emit)
        expected_models = {
            item.name for item in plan.target_model.dimensions + plan.target_model.facts
        }
        produced_models = {
            f.path[:-4] for f in implementation.files if f.path.endswith(".sql")
        }
        if not expected_models.issubset(produced_models):
            raise ValueError(
                f"dbt implementation omitted planned models: {sorted(expected_models - produced_models)}"
            )
        write_project(project, database, names, implementation)
        state["dbt_explanation"] = implementation.explanation
        emit(
            "generating dbt models",
            "done",
            f"Wrote {len(implementation.files)} model files",
        )

        repair_count = 0
        while True:
            emit("dbt compile/build/test", "start", "Compiling dbt project")
            compile_result = run_dbt(project, "compile")
            state.setdefault("dbt_runs", []).append(compile_result)
            emit(
                "dbt compile/build/test",
                "result",
                f"compile: {'passed' if compile_result['ok'] else 'failed'}",
            )
            if compile_result["ok"]:
                build_result = run_dbt(project, "build")
                state["dbt_runs"].append(build_result)
                emit(
                    "dbt compile/build/test",
                    "result",
                    f"build: {'passed' if build_result['ok'] else 'failed'}",
                )
                if build_result["ok"]:
                    test_result = run_dbt(project, "test")
                    state["dbt_runs"].append(test_result)
                    emit(
                        "dbt compile/build/test",
                        "result",
                        f"test: {'passed' if test_result['ok'] else 'failed'}",
                    )
                else:
                    test_result = build_result
            else:
                test_result = compile_result
            state["dbt_results"] = dbt_results(project)
            if test_result["ok"]:
                break
            emit("validation and repair", "failure", test_result["output"][-1500:])
            if repair_count >= 1:
                raise RuntimeError(
                    "dbt validation failed; inspect dbt_runs in result.json"
                )
            repair_count += 1
            emit(
                "validation and repair",
                "repair",
                "Requesting one structured dbt repair",
            )
            implementation = agents.implement(
                plan, profiles, links, emit, failure=test_result["output"][-5000:]
            )
            produced_models = {
                f.path[:-4] for f in implementation.files if f.path.endswith(".sql")
            }
            if not expected_models.issubset(produced_models):
                raise ValueError(
                    f"Repair omitted planned models: {sorted(expected_models - produced_models)}"
                )
            write_project(project, database, names, implementation)
        state["repair_attempts"] = repair_count

        model_names = [
            f.path[:-4] for f in implementation.files if f.path.endswith(".sql")
        ]
        state["model_names"] = model_names
        state["model_schemas"] = schemas(database, model_names)
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
        analysis = agents.analyse(
            request, plan, state["model_schemas"], summaries, emit, database
        )
        state["analysis"] = analysis.model_dump()
        emit(
            "analytical interpretation",
            "done",
            f"Selected {len(analysis.analyses)} analytical queries",
        )

        emit(
            "analysis acceptance",
            "start",
            "Validating and registering analytical results in DuckDB",
        )

        accepted_results = accept_analyses(
            database,
            model_names,
            analysis,
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
            "done",
            f"Accepted {len(accepted_results)} analytical results",
        )
        state["metrics"] = result_metrics(database, accepted_results)

        emit(
            "visualisation generation",
            "start",
            "Rendering Plotly figures from accepted analytical results",
        )
        state["plot_specs"] = []
        for index, (spec, accepted_result) in enumerate(accepted_results):
            plot = agents.plot(spec, accepted_result, emit, database)
            frame = load_result_frame(database, accepted_result)
            fig = render_plot(frame, plot, spec.title)
            state["plot_specs"].append(plot.model_dump())
            fig.write_json(run_dir / f"chart_{index}.json")
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
        )
        state["status"] = "complete"
        emit("dashboard completion", "done", "Validated dashboard is ready")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        emit("dashboard completion", "failure", state["error"])
    save()
    return state
