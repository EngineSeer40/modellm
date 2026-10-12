import argparse
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import agents
from .analysis import available_views, demo_analysis, describe_views, make_charts, metrics
from .contracts import AnalysisPlan, AnalyticalPlan, DbtImplementation
from .data import load_raw, profile, relationships, schemas
from .dbt import dbt_results, demo_implementation, run_dbt, write_project
from .demo import REQUEST, demo_plan, write_demo


def run(paths: list[Path], request: str, root: Path, *, demo: bool = False, on_event: Callable | None = None) -> dict:
    run_dir = root.resolve() / uuid.uuid4().hex[:12]
    run_dir.mkdir(parents=True, exist_ok=False)
    events = []
    state = {"status": "running", "request": request, "run_dir": str(run_dir), "events": events}

    def save():
        (run_dir / "result.json").write_text(json.dumps(state, indent=2, default=str))

    def emit(stage: str, kind: str, message: str, detail=None):
        event = {"time": datetime.now(timezone.utc).isoformat(), "stage": stage, "kind": kind, "message": message}
        if detail:
            event["detail"] = detail
        events.append(event)
        save()
        if on_event:
            on_event(event)

    database = run_dir / "analytics.duckdb"
    project = run_dir / "dbt"
    try:
        emit("loading data", "start", "Loading original CSV/Parquet data into DuckDB raw schema")
        names = load_raw(database, paths)
        state["raw_names"] = names
        emit("loading data", "done", f"Loaded {len(names)} raw datasets")

        emit("profiling data", "start", "Profiling columns, uniqueness, nulls and duplicate rows")
        profiles = profile(database, names)
        state["profiles"] = profiles
        emit("profiling data", "done", "Raw data profile complete")

        emit("inferring relationships", "start", "Checking shared keys and orphan references")
        links = relationships(database, profiles)
        state["relationships"] = links
        emit("inferring relationships", "done", f"Found {len(links)} candidate relationships")

        emit("conceptual modelling", "start", "Interpreting business request and source evidence")
        plan = demo_plan() if demo else agents.conceptual(request, profiles, links, emit)
        state["plan"] = plan.model_dump()
        emit("conceptual modelling", "done", plan.objective)

        emit("generating dbt models", "start", "Creating dbt sources, dimensions, facts and tests")
        implementation = demo_implementation() if demo else agents.implement(plan, profiles, links, emit)
        expected_models = {item.name for item in plan.target_model.dimensions + plan.target_model.facts}
        produced_models = {f.path[:-4] for f in implementation.files if f.path.endswith(".sql")}
        if not expected_models.issubset(produced_models):
            raise ValueError(f"dbt implementation omitted planned models: {sorted(expected_models - produced_models)}")
        write_project(project, database, names, implementation)
        state["dbt_explanation"] = implementation.explanation
        emit("generating dbt models", "done", f"Wrote {len(implementation.files)} model files")

        repair_count = 0
        while True:
            emit("dbt compile/build/test", "start", "Compiling dbt project")
            compile_result = run_dbt(project, "compile")
            state.setdefault("dbt_runs", []).append(compile_result)
            emit("dbt compile/build/test", "result", f"compile: {'passed' if compile_result['ok'] else 'failed'}")
            if compile_result["ok"]:
                build_result = run_dbt(project, "build")
                state["dbt_runs"].append(build_result)
                emit("dbt compile/build/test", "result", f"build: {'passed' if build_result['ok'] else 'failed'}")
                if build_result["ok"]:
                    test_result = run_dbt(project, "test")
                    state["dbt_runs"].append(test_result)
                    emit("dbt compile/build/test", "result", f"test: {'passed' if test_result['ok'] else 'failed'}")
                else:
                    test_result = build_result
            else:
                test_result = compile_result
            state["dbt_results"] = dbt_results(project)
            if test_result["ok"]:
                break
            emit("validation and repair", "failure", test_result["output"][-1500:])
            if demo or repair_count >= 1:
                raise RuntimeError("dbt validation failed; inspect dbt_runs in result.json")
            repair_count += 1
            emit("validation and repair", "repair", "Requesting one structured dbt repair")
            implementation = agents.implement(plan, profiles, links, emit, failure=test_result["output"][-5000:])
            produced_models = {f.path[:-4] for f in implementation.files if f.path.endswith(".sql")}
            if not expected_models.issubset(produced_models):
                raise ValueError(f"Repair omitted planned models: {sorted(expected_models - produced_models)}")
            write_project(project, database, names, implementation)
        state["repair_attempts"] = repair_count

        model_names = [f.path[:-4] for f in implementation.files if f.path.endswith(".sql")]
        state["model_names"] = model_names
        state["model_schemas"] = schemas(database, model_names)
        emit("validation and repair", "done", "Validated dbt tables are available in DuckDB")

        views = available_views(model_names)
        view_schemas, summaries = describe_views(database, views)
        emit("analytical interpretation", "start", "Selecting metrics and visuals from validated tables")
        analysis = demo_analysis() if demo else agents.analyse(request, plan, view_schemas, summaries, emit)
        state["analysis"] = analysis.model_dump()
        emit("analytical interpretation", "done", f"Selected {len(analysis.views)} analytical views")

        emit("metric generation", "start", "Calculating metrics from validated facts")
        state["metrics"] = metrics(database, model_names, plan)
        emit("metric generation", "done", f"Calculated {len(state['metrics'])} metrics")

        emit("visualisation generation", "start", "Rendering Plotly figures from validated tables")
        charts = make_charts(database, views, view_schemas, analysis)
        for index, (_, fig, _) in enumerate(charts):
            fig.write_json(run_dir / f"chart_{index}.json")
        emit("visualisation generation", "done", f"Rendered {len(charts)} figures")
        state["quality_warnings"] = [f"{r['orphan_rows']} orphan rows: {r['from']} → {r['to']}" for r in links if r["orphan_rows"]] + [f"{n}: {p['duplicate_rows']} duplicate rows" for n, p in profiles.items() if p["duplicate_rows"]] + analysis.warnings
        state["status"] = "complete"
        emit("dashboard completion", "done", "Validated dashboard is ready")
    except Exception as exc:
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        emit("dashboard completion", "failure", state["error"])
    save()
    return state


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("runs"))
    parser.add_argument("--bedrock", action="store_true", help="Use Strands/Bedrock agents for the bundled data")
    args = parser.parse_args()
    data_dir = args.output / "demo_input"
    paths = write_demo(data_dir)
    result = run(paths, REQUEST, args.output, demo=not args.bedrock, on_event=lambda e: print(f"[{e['stage']}] {e['message']}", flush=True))
    print(result["run_dir"])
    raise SystemExit(0 if result["status"] == "complete" else 1)


if __name__ == "__main__":
    main()
