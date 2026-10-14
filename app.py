from pathlib import Path

import plotly.io as pio
import streamlit as st

from src.agents import configured_model_id
from src.data import preview
from src.storage import DUCKDB_FILENAME
from src.workflow import run

st.set_page_config(page_title="ModelLM", layout="wide")
st.title("ModelLM · modelling and BI spike")
st.caption("Raw CSV/Parquet/JSON → DuckDB → analytical plan → dbt validation → Plotly dashboard")

request_tab, data_tab, trace_tab, dashboard_tab = st.tabs(["Request", "Data", "Agent Trace", "Dashboard"])
state = st.session_state.get("result")

with request_tab:
    request = st.text_area("Business request", height=100)
    files = st.file_uploader("Upload 2–6 related CSV, Parquet or JSON datasets", type=["csv", "parquet", "json", "jsonl", "ndjson"], accept_multiple_files=True)
    st.caption(f"Agent model: `{configured_model_id()}`")
    if st.button("Run workflow", type="primary"):
        if not request.strip():
            st.error("Enter a business request.")
        elif not 2 <= len(files) <= 6:
            st.error("Upload 2 to 6 datasets.")
        else:
            input_dir = Path("runs") / "uploads"
            input_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for file in files:
                path = input_dir / Path(file.name).name
                path.write_bytes(file.getvalue())
                paths.append(path)
            status = st.status("Workflow running", expanded=True)
            progress = st.empty()
            steps = []

            def update(event):
                if event["kind"] in {"start", "done", "failure", "repair", "result", "model_call", "tool_call", "contract_check"}:
                    message = f"{event['stage']}: {event['message']}"
                    if event["kind"] == "model_call" and event.get("detail"):
                        message += f" · usage: {event['detail']}"
                    steps.append(message)
                    progress.markdown("  \n".join(steps[-12:]))

            state = run(paths, request, Path("runs"), on_event=update)
            st.session_state["result"] = state
            status.update(label=f"Workflow {state['status']}", state="complete" if state["status"] == "complete" else "error")
            if state["status"] == "failed":
                st.error(state.get("error", "Workflow failed"))

if state:
    run_dir = Path(state["run_dir"])
    database = run_dir / DUCKDB_FILENAME
    with data_tab:
        raw, transformed, model = st.tabs(["Raw Data", "Transformed Data", "Inferred Data Model"])
        with raw:
            name = st.selectbox("Raw dataset", state.get("raw_names", []), key="raw_select")
            if name:
                if database.exists():
                    st.dataframe(preview(database, "raw", name), use_container_width=True)
                    st.json(state.get("profiles", {}).get(name, {}))
                else:
                    st.info("This run's DuckDB file has expired. Its profile and trace are still available.")
        with transformed:
            names = state.get("model_names", [])
            name = st.selectbox("Transformed dataset", names, key="model_select")
            if name:
                if database.exists():
                    st.dataframe(preview(database, "main", name), use_container_width=True)
                else:
                    st.info("This run's DuckDB file has expired. Its trace and dashboard artifacts are still available.")
                st.success("dbt compile, build and test passed" if state["status"] == "complete" else "Build incomplete")
                st.dataframe([r for r in state.get("dbt_results", []) if name in r["name"]], use_container_width=True)
        with model:
            plan = state.get("plan")
            if plan:
                target = plan["target_model"]
                lines = ["digraph G { rankdir=LR; node [shape=record];"]
                for dimension in target["dimensions"]:
                    label = f"{dimension['name']}|grain: {dimension['grain']}|natural: {', '.join(dimension['natural_key_fields'])}|surrogate: {dimension['surrogate_key']}"
                    lines.append(f'"{dimension["name"]}" [label="{{{label}}}"];')
                for fact in target["facts"]:
                    keys = [pair["fact_field"] for link in fact["dimension_links"] for pair in link.get("key_pairs", [{"fact_field": link.get("fact_output_field", "")}])]
                    label = f"{fact['name']}|grain: {fact['grain']}|keys: {', '.join(keys)}"
                    lines.append(f'"{fact["name"]}" [label="{{{label}}}"];')
                    for dimension in target["dimensions"]:
                        if any(link["dimension_model"] == dimension["name"] for link in fact["dimension_links"]):
                            lines.append(f'"{fact["name"]}" -> "{dimension["name"]}";')
                lines.append("}")
                st.graphviz_chart("\n".join(lines), use_container_width=True)
                st.write(plan["objective"])
                st.markdown("**Observed:** raw column profiles and key match counts above. **Inferred:** candidate relationships and model structure below. **Assumed:** business semantics stated in the plan.")
                st.json(state.get("relationships", []))
                st.write("Assumptions:", plan["assumptions"])
    with trace_tab:
        plan = state.get("plan", {})
        st.subheader("Interpretation")
        st.write(plan.get("objective", "Pending"))
        st.write("Assumptions:", plan.get("assumptions", []))
        st.write("Unresolved:", plan.get("unresolved_questions", []))
        st.write("Proposed model:", plan.get("target_model", {}))
        st.subheader("Execution timeline")
        st.dataframe(state.get("events", []), use_container_width=True)
        st.write("Repair attempts:", state.get("repair_attempts", 0))
        st.write("Final status:", state["status"])
        for item in state.get("dbt_runs", []):
            with st.expander(f"dbt {item['command']} — {'passed' if item['ok'] else 'failed'}"):
                st.code(item["output"])
        st.dataframe(state.get("dbt_results", []), use_container_width=True)
    with dashboard_tab:
        if state["status"] != "complete":
            st.info("Complete dbt validation to display the dashboard.")
        else:
            metrics = state.get("metrics", {})
            if metrics:
                cols = st.columns(len(metrics))
                for col, (label, value) in zip(cols, metrics.items()):
                    col.metric(label, f"{value:,.2f}" if isinstance(value, float) else f"{value:,}")
            analyses = state["analysis"]["analyses"]
            concepts = list(dict.fromkeys(item["concept_id"] for item in analyses))
            for concept in concepts:
                related = [(index, item) for index, item in enumerate(analyses) if item["concept_id"] == concept]
                st.subheader(related[0][1]["title"])
                for index, analysis in related:
                    charts = [chart for chart in state.get("charts", []) if chart["result_index"] == index]
                    if not charts:  # Historical dashboard artifacts remain readable.
                        charts = [{"title": analysis["title"], "file": f"chart_{index}.json"}]
                    for chart in charts:
                        st.caption(chart["title"])
                        st.plotly_chart(pio.read_json(run_dir / chart["file"]), use_container_width=True)
                    finding = state["findings"][index]
                    st.write(finding["text"])
                    with st.expander("Supporting evidence"):
                        for evidence in finding.get("evidence", []):
                            st.caption(evidence["id"])
                            st.dataframe([row.get("values", row) for row in evidence["rows"]], use_container_width=True)
                            if evidence.get("truncated"):
                                st.caption("Evidence preview is truncated; see its selection and ordering in the trace.")
                    with st.expander("Methodology and assumptions"):
                        st.write(finding.get("methodology", {"grain": analysis["grain"], "rationale": analysis["rationale"]}))
                        for caveat in finding.get("caveats", []):
                            st.write(caveat)
            st.subheader("Validated data preview")
            names = state.get("model_names", [])
            if names and database.exists():
                st.dataframe(preview(database, "main", next((n for n in names if n.startswith("fact_")), names[0])), use_container_width=True)
            for warning in state.get("quality_warnings", []):
                st.warning(warning)
            st.write("Unresolved assumptions:", state["plan"]["unresolved_questions"])
