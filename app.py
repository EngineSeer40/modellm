from pathlib import Path

import plotly.io as pio
import streamlit as st

from src.data import preview
from src.demo import REQUEST, write_demo
from src.workflow import run

st.set_page_config(page_title="ModelLM", layout="wide")
st.title("ModelLM · modelling and BI spike")
st.caption("Raw CSV/Parquet → DuckDB → analytical plan → dbt validation → Plotly dashboard")

request_tab, data_tab, trace_tab, dashboard_tab = st.tabs(["Request", "Data", "Agent Trace", "Dashboard"])
state = st.session_state.get("result")

with request_tab:
    request = st.text_area("Business request", value=REQUEST, height=100)
    files = st.file_uploader("Upload 2–5 related CSV or Parquet datasets", type=["csv", "parquet"], accept_multiple_files=True)
    mode = st.radio("Run mode", ["Bundled deterministic demo", "Strands + Bedrock agents"], horizontal=True)
    st.caption("The bundled demo runs without AWS. Agent mode requires Bedrock credentials and model access.")
    if st.button("Run workflow", type="primary"):
        demo = mode == "Bundled deterministic demo"
        if files and not 2 <= len(files) <= 5:
            st.error("Upload 2 to 5 datasets.")
        elif not files and not demo:
            st.error("Upload 2 to 5 datasets for agent mode, or select the bundled demo.")
        else:
            input_dir = Path("runs") / "uploads"
            input_dir.mkdir(parents=True, exist_ok=True)
            if files:
                paths = []
                for file in files:
                    path = input_dir / Path(file.name).name
                    path.write_bytes(file.getvalue())
                    paths.append(path)
                demo = False
            else:
                paths = write_demo(input_dir / "demo")
            status = st.status("Workflow running", expanded=True)
            progress = st.empty()
            steps = []

            def update(event):
                if event["kind"] in {"start", "done", "failure", "repair", "result"}:
                    steps.append(f"{event['stage']}: {event['message']}")
                    progress.markdown("  \n".join(steps[-12:]))

            state = run(paths, request, Path("runs"), demo=demo, on_event=update)
            st.session_state["result"] = state
            status.update(label=f"Workflow {state['status']}", state="complete" if state["status"] == "complete" else "error")
            if state["status"] == "failed":
                st.error(state.get("error", "Workflow failed"))

if state:
    run_dir = Path(state["run_dir"])
    database = run_dir / "analytics.duckdb"
    with data_tab:
        raw, transformed, model = st.tabs(["Raw Data", "Transformed Data", "Inferred Data Model"])
        with raw:
            name = st.selectbox("Raw dataset", state.get("raw_names", []), key="raw_select")
            if name:
                st.dataframe(preview(database, "raw", name), use_container_width=True)
                st.json(state.get("profiles", {}).get(name, {}))
        with transformed:
            names = state.get("model_names", [])
            name = st.selectbox("Transformed dataset", names, key="model_select")
            if name:
                st.dataframe(preview(database, "main", name), use_container_width=True)
                st.success("dbt compile, build and test passed" if state["status"] == "complete" else "Build incomplete")
                st.dataframe([r for r in state.get("dbt_results", []) if name in r["name"]], use_container_width=True)
        with model:
            plan = state.get("plan")
            if plan:
                target = plan["target_model"]
                lines = ["digraph G { rankdir=LR; node [shape=record];"]
                for dimension in target["dimensions"]:
                    label = f"{dimension['name']}|grain: {dimension['grain']}|natural: {dimension['natural_key']}|surrogate: {dimension['surrogate_key']}"
                    lines.append(f'"{dimension["name"]}" [label="{{{label}}}"];')
                for fact in target["facts"]:
                    label = f"{fact['name']}|grain: {fact['grain']}|keys: {', '.join(fact['dimension_keys'])}"
                    lines.append(f'"{fact["name"]}" [label="{{{label}}}"];')
                    for dimension in target["dimensions"]:
                        if dimension["surrogate_key"] in fact["dimension_keys"]:
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
            views = state["analysis"]["views"]
            for index, view in enumerate(views):
                st.plotly_chart(pio.read_json(run_dir / f"chart_{index}.json"), use_container_width=True)
                st.caption(view["finding"])
            st.subheader("Validated data preview")
            names = state.get("model_names", [])
            if names:
                st.dataframe(preview(database, "main", next((n for n in names if n.startswith("fact_")), names[0])), use_container_width=True)
            for warning in state.get("quality_warnings", []):
                st.warning(warning)
            st.write("Unresolved assumptions:", state["plan"]["unresolved_questions"])
