"""Result, model and diagnostic views over a saved workflow result."""

import json
from pathlib import Path

import duckdb
import plotly.io as pio
import streamlit as st

from src.run import RunState
from src.data import preview
from src.storage import DUCKDB_FILENAME
from src.usage import summarize_usage

from .progress import duration_label, failure_message, project_progress, repair_counts
from .model_diagram import model_diagram
from .live import _conceptual
from .presentation import concise_numbers, display_table, finding_bullets, format_chart


def usage_for(state: RunState):
    return state.get("usage") or summarize_usage(state.get("events", []))


def render_progress(phases):
    symbols = {"pending": "○", "running": "◉", "repairing": "↻", "checking": "◉",
               "complete": "✓", "failed": "✕"}
    for phase in phases:
        status = phase["status"]
        detail = "Correcting a validation issue automatically." if status == "repairing" else phase["detail"]
        if status == "complete":
            detail = "Completed"
        elif status == "pending":
            detail = "Waiting"
        elif status == "failed":
            detail = "Could not complete this stage"
        repair_note = f" · {phase['repairs']} repair(s) attempted" if phase["repairs"] else ""
        st.markdown(f"{symbols[status]} **{phase['label']}**")
        st.caption(detail + repair_note)


def render_outcome(state: RunState):
    if state["status"] == "failed":
        st.error(failure_message(state))
        st.caption(f"Run ID: {state['run_id']} · Diagnostics are in Technical Details; usage is shown on Results.")
    elif state["status"] == "complete":
        st.success("Analysis completed. Your dashboard is ready.")
    else:
        st.info("This run has not completed.")


def render_usage(state: RunState):
    usage = usage_for(state)
    total = usage["total"]
    st.subheader("Model usage and cost")
    cols = st.columns(5)
    cols[4].metric("Duration", duration_label(state))
    for col, label, key in zip(cols[:3], ("Input tokens", "Output tokens", "Total tokens"),
                               ("input_tokens", "output_tokens", "total_tokens")):
        value = total[key]
        col.metric(label, f"{value:,}" if value is not None else "Unavailable")
    cost = total["estimated_cost_usd"]
    cols[3].metric("Estimated cost · USD", f"${cost:.2f}" if cost is not None else "Unavailable")
    if total.get("cache_read_tokens") is not None:
        st.caption(f"Input includes cache tokens · Uncached: {total['uncached_input_tokens']:,} · "
                   f"Cache read: {total['cache_read_tokens']:,} · Cache write: {total['cache_write_tokens']:,}")
    if usage["agents"]:
        st.dataframe([
            {"Agent": row["agent"], "Model": row["models"], "Calls": row["model_calls"],
             "Input tokens": row["input_tokens"], "Output tokens": row["output_tokens"],
             "Cache read": row.get("cache_read_tokens"), "Cache write": row.get("cache_write_tokens"),
             "Total tokens": row["total_tokens"], "Estimated USD": row["estimated_cost_usd"],
             "Usage": row["usage_status"]} for row in usage["agents"]
        ], hide_index=True, width="stretch", column_config={"Estimated USD": st.column_config.NumberColumn(format="$%.2f")})
    st.caption(f"Usage: {total['usage_status']}. Reported responses include repairs and tool cycles; "
               "unreported usage is unavailable. Costs are list-price estimates, not invoices. "
               f"{usage['pricing_basis']}.")


def render_results(state: RunState | None):
    st.header("Results")
    if not state:
        st.info("Upload related datasets and describe your question in Run to create a dashboard.")
        return
    render_outcome(state)
    if trace_url := state.get("langfuse_trace_url"):
        st.header(f"[Langfuse Full Trace]({trace_url})")
    render_usage(state)
    if state["status"] != "complete":
        st.info("A completed run is required to display the dashboard. Data Model shows any model built so far.")
        return
    run_dir = Path(state["run_dir"])
    analyses = state.get("analysis", {}).get("analyses", [])
    concepts = list(dict.fromkeys(item.get("concept_id", item["title"]) for item in analyses))
    for concept in concepts:
        related = [(index, item) for index, item in enumerate(analyses)
                   if item.get("concept_id", item["title"]) == concept]
        related = [(index, item) for index, item in related
                   if any(chart["result_index"] == index for chart in state.get("charts", []))
                   or "plot_attempts" not in state]
        if not related:
            continue
        st.divider()
        st.subheader(related[0][1]["title"])
        for index, analysis in related:
            charts = [chart for chart in state.get("charts", []) if chart["result_index"] == index]
            skipped = [attempt for attempt in state.get("plot_attempts", [])
                       if attempt.get("result_index") == index and attempt.get("status") == "skipped"]
            if not charts and not skipped:
                charts = [{"title": analysis["title"], "file": f"chart_{index}.json"}]
            for chart in charts:
                if len(charts) > 1 and chart["title"] != related[0][1]["title"]:
                    st.caption(chart["title"])
                path = run_dir / chart["file"]
                if path.exists():
                    st.plotly_chart(format_chart(pio.read_json(path)), width="stretch", key=f"chart_{state['run_id']}_{chart['file']}")
                else:
                    st.caption("This saved chart is unavailable. The methodology remains below.")
            findings = state.get("findings", [])
            finding = findings[index] if index < len(findings) else {}
            if finding.get("text"):
                st.markdown("**Analysis Observations**")
                st.table([{"Observation": " ".join(item.split())}
                          for item in finding_bullets(finding["text"])], hide_index=True)
            if finding.get("evidence"):
                with st.expander("Supporting evidence"):
                    for evidence in finding["evidence"]:
                        st.caption(f"Evidence {evidence['id']}")
                        st.dataframe(display_table([row.get("values", row) for row in evidence["rows"]]), hide_index=True, width="stretch")
                        if evidence.get("truncated"):
                            st.caption("This evidence preview is truncated; its selection is recorded in Technical Details.")
            with st.expander("Methodology"):
                methodology = finding.get("methodology") or {
                    "question": analysis.get("question"), "population": analysis.get("population_definition"),
                    "observation rule": analysis.get("observation_rule"), "grain": analysis.get("grain"),
                    "rationale": analysis.get("rationale"),
                }
                for label, value in methodology.items():
                    if value:
                        st.markdown(f"**{label.replace('_', ' ').capitalize()}**")
                        st.write(concise_numbers(value))


def _preview(database, schema, name):
    if not database.exists():
        st.info("This run's stored database has expired. Saved model details and dashboard artifacts remain available.")
        return
    try:
        st.dataframe(display_table(preview(database, schema, name)), width="stretch")
    except (OSError, duckdb.Error):
        st.warning("This table is unavailable in the saved database. Its recorded schema and diagnostics are still available.")


def render_data_model(state: RunState | None):
    st.header("Data Model")
    if not state:
        st.info("Run an analysis to inspect its sources and resulting data model.")
        return
    database = Path(state["run_dir"]) / DUCKDB_FILENAME
    plan = state.get("plan", {})
    target = plan.get("target_model", plan)
    dimensions, facts = target.get("dimensions", []), target.get("facts", [])
    cols = st.columns(3)
    cols[0].metric("Source datasets", len(state.get("raw_names", [])))
    cols[1].metric("Dimensions", len(dimensions))
    cols[2].metric("Facts", len(facts))
    st.subheader("Dimensional model")
    if dimensions or facts:
        scope_col, fields_col = st.columns([3, 1])
        scope = scope_col.selectbox("Diagram scope", ["All models", *[f["name"] for f in facts]],
                                    key=f"diagram_scope_{state['run_id']}")
        all_fields = fields_col.toggle("Show all fields", value=False, key=f"diagram_details_{state['run_id']}")
        st.caption("Facts in teal · Dimensions in blue · Connections use declared field pairs, not inferred foreign keys.")
        dark = st.context.theme.type == "dark"
        st.iframe(model_diagram(target, state.get("model_schemas", {}),
                                      fact_name=None if scope == "All models" else scope,
                                      all_fields=all_fields, dark=dark), height=490)
        st.dataframe([{"Model": m["name"], "Role": role, "Grain": m["grain"]}
                      for role, models in (("Fact", facts), ("Dimension", dimensions)) for m in models],
                     hide_index=True, width="stretch")
        with st.expander("Assumptions and unresolved questions"):
            for label, values in (("Assumptions", plan.get("assumptions", [])),
                                  ("Unresolved questions", plan.get("unresolved_questions", []))):
                st.markdown(f"**{label}**")
                for value in values:
                    st.write(value)
                if not values:
                    st.caption("None recorded")
    else:
        st.info("No accepted model plan is available for this run.")
    st.subheader("Source datasets")
    profiles = state.get("profiles", {})
    if profiles:
        st.dataframe([{"Dataset": name, "Rows": p["rows"], "Columns": len(p["columns"]),
                       "Duplicate rows": p.get("duplicate_rows")}
                      for name, p in profiles.items()], hide_index=True, width="stretch")
    raw_names = state.get("raw_names", [])
    if raw_names:
        name = st.selectbox("Source preview", raw_names, key=f"source_{state['run_id']}")
        _preview(database, "raw", name)
        columns = profiles.get(name, {}).get("columns", [])
        if columns:
            with st.expander("Column profile"):
                st.dataframe([{"Column": c["name"], "Type": c["type"], "Nulls": c["nulls"],
                               "Distinct values": c["distinct"], "Unique": c["unique"]}
                              for c in columns], hide_index=True, width="stretch")
    st.subheader("Built models")
    names = state.get("model_names", [])
    validation = any(e.get("stage") == "validation and repair" and e.get("kind") == "done"
                     for e in state.get("events", []))
    if validation:
        st.success("The built models passed database validation.")
    elif names:
        st.warning("Tables are available, but model validation did not complete.")
    else:
        st.info("No built models are available for this run.")
    if names:
        name = st.selectbox("Model preview", names, key=f"model_{state['run_id']}")
        _preview(database, "main", name)
        fields = state.get("model_schemas", {}).get(name, {})
        if fields:
            with st.expander("Model schema"):
                st.dataframe([{"Column": name, "Type": dtype} for name, dtype in fields.items()],
                             hide_index=True, width="stretch")


def _unresolved_issues(issues, *, key):
    with st.expander("Unresolved issues", key=key):
        for issue in issues:
            st.write(issue)
        if not issues:
            st.caption("None recorded")


def render_technical(state: RunState | None):
    st.header("Technical Details")
    if not state:
        st.info("Run details and diagnostics appear here after execution.")
        return
    st.caption(f"Run ID: {state['run_id']} · Status: {state['status']} · Duration: {duration_label(state)}")
    if state.get("error"):
        with st.expander("Failure diagnostics"):
            st.code(state["error"], language=None)
    source_issues = [
        f"{r['orphan_rows']} orphan rows: {r['from']} → {r['to']}"
        for r in state.get("relationships", []) if r.get("orphan_rows")
    ] + [
        f"{name}: {profile['duplicate_rows']} duplicate rows"
        for name, profile in state.get("profiles", {}).items() if profile.get("duplicate_rows")
    ]
    analysis_issues = state.get("analysis", {}).get("warnings", [])
    finding_issues = [issue for finding in state.get("findings", []) for issue in finding.get("caveats", [])]
    st.subheader("Source profiling")
    _unresolved_issues(source_issues, key=f"source_issues_{state['run_id']}")
    st.subheader("Conceptual modelling")
    _conceptual(state, [e for e in state.get("events", []) if e.get("stage") == "conceptual modelling"],
                decisions_as_table=True)
    plan = state.get("plan", {})
    with st.expander("Unresolved issues", key=f"conceptual_issues_{state['run_id']}"):
        for title, key in (("Assumptions", "assumptions"), ("Unresolved questions", "unresolved_questions")):
            st.markdown(f"**{title}**")
            st.table([{title: " ".join(str(item).split())} for item in plan.get(key, [])]
                     or [{title: "None recorded"}], hide_index=True)
    st.subheader("Analysis")
    _unresolved_issues(analysis_issues, key=f"analysis_issues_{state['run_id']}")
    if finding_issues:
        st.subheader("Finding interpretation (saved run)")
        _unresolved_issues(finding_issues, key=f"finding_issues_{state['run_id']}")
    st.subheader("Plotting")
    _unresolved_issues([
        f"Chart skipped: {attempt['title']}. {attempt['error']}"
        for attempt in state.get("plot_attempts", []) if attempt.get("status") == "skipped"
    ], key=f"plotting_issues_{state['run_id']}")
    known_issues = set(source_issues + analysis_issues + finding_issues)
    unassigned = [issue for issue in state.get("quality_warnings", []) if issue not in known_issues]
    if unassigned:
        st.subheader("Other run diagnostics")
        st.caption("These recorded issues have no identifiable source stage.")
        _unresolved_issues(unassigned, key=f"other_issues_{state['run_id']}")
    st.subheader("Validation and repairs")
    repairs = repair_counts(state)
    st.dataframe([{"Stage": stage, "Repairs attempted": count} for stage, count in repairs.items()],
                 hide_index=True, width="stretch")
    st.subheader("Execution timeline")
    st.dataframe([{"Time": e.get("time"), "Stage": e.get("stage"), "Event": e.get("kind"),
                   "Message": e.get("message")} for e in state.get("events", [])],
                 hide_index=True, width="stretch", height=420)
    passed = {item["command"]: item for item in state.get("dbt_runs", []) if item.get("ok")}
    for command in ("compile", "build", "test"):
        item = passed.get(command)
        if not item:
            continue
        with st.expander(f"dbt {command} · passed"):
            st.code(item["output"], language=None)
    with st.expander("Recorded agent messages and contracts"):
        records = [e for e in state.get("events", []) if e.get("kind") in {
            "model_message", "contract_schema", "reasoning", "reasoning_status", "model_text",
        }]
        if records:
            index = st.selectbox("Recorded event", range(len(records)),
                                 format_func=lambda i: f"{i + 1} · {records[i]['stage']} · {records[i]['kind']}",
                                 key=f"agent_record_{state['run_id']}")
            event = records[index]
            st.text(event["message"])
            if event.get("detail"):
                st.json(event["detail"])
        else:
            st.caption("No agent messages recorded")
    st.download_button("Download run diagnostics · JSON", json.dumps(state, indent=2, default=str),
                       file_name=f"{state['run_id']}_diagnostics.json", mime="application/json")
