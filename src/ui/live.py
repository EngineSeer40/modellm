"""Chronological diagnostics and stage summaries from emitted run information."""

import json
from pathlib import Path

import streamlit as st

from .progress import STAGE_PHASE, project_progress


def stream_text(events):
    """Readable chronological messages; full payloads remain in Technical Details."""
    entries = []
    for event in events:
        kind = event.get("kind", "")
        if kind in {"contract_schema", "model_message", "failure", "repair", "contract_check"}:
            continue
        message = str(event.get("message", ""))
        if kind in {"model_text", "reasoning"}:
            # Text deltas belong to one continuous thought, not separate JSON records.
            if entries and entries[-1][0] == (event.get("stage"), kind):
                entries[-1][1] += message
                continue
        if message.lstrip().startswith(("{", "[")):
            message = "Structured output recorded in Technical Details."
        if len(message) > 1500 and kind not in {"model_text", "reasoning"}:
            message = message[:1500] + "…"
        if event.get("stage") == "validation and repair" or (kind == "result" and event.get("stage") == "dbt compile/build/test"):
            continue
        time = event.get("time", "").replace("T", " ")
        entries.append([(event.get("stage"), kind), f"[{time}] {event.get('stage', '')}\n{message}"])
    return "\n\n".join(entry[1] for entry in entries)


def render_stream(events):
    if events:
        st.code(stream_text(events), language=None, wrap_lines=True)
    else:
        st.caption("Agent activity will appear here when the run starts.")


def _proposals(events, name):
    proposals = []
    for event in events:
        message = event.get("detail", {}).get("message", {})
        if not isinstance(message, dict):
            continue
        for block in message.get("content", []):
            tool = block.get("toolUse", {})
            if tool.get("name") == name and isinstance(tool.get("input"), dict):
                proposals.append(tool["input"])
    return proposals


def _reasoning(events):
    chunks = {}
    for event in events:
        call = event.get("detail", {}).get("call_id", "")
        if event.get("kind") in {"model_text", "reasoning"}:
            chunks.setdefault(call, []).append(event["message"])
    streamed_calls = set(chunks)
    # Completed messages cover providers that do not emit text deltas.
    for event in events:
        call = event.get("detail", {}).get("call_id", "")
        if call in streamed_calls:
            continue
        message = event.get("detail", {}).get("message", {})
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        texts = [block["text"] for block in message.get("content", []) if block.get("text")]
        if texts:
            chunks.setdefault(call, []).extend(texts)
    return ["".join(parts) for parts in chunks.values() if parts]


def _conceptual(state, events, *, decisions_as_table=False):
    proposals = _proposals(events, "AnalyticalPlan")
    plan = state.get("plan") or (proposals[-1] if proposals else None)
    if not plan:
        st.caption("The agent is establishing fact grains, measures, dimensions and relationships. The proposal will appear when returned.")
        return
    target = plan.get("target_model", plan)
    st.caption("Accepted model" if state.get("plan") else "Latest proposed model · awaiting application validation")
    for title, key in (("Proposed facts", "facts"), ("Proposed dimensions", "dimensions")):
        models = target.get(key, [])
        st.markdown(f"**{title}**")
        if models:
            st.dataframe([{"Model": m["name"], "Grain": m.get("grain"),
                           "Process / entity": m.get("business_process", m.get("business_entity"))}
                          for m in models], hide_index=True, width="stretch")
    reasons = [(m["name"], m.get("modelling_rationale")) for m in target.get("facts", [])]
    if any(reason for _, reason in reasons):
        st.markdown("**Modelling decisions**")
        if decisions_as_table:
            st.table([{"Modelling decision": f"{name}: {reason}"} for name, reason in reasons if reason])
        else:
            for name, reason in reasons:
                if reason:
                    st.markdown(f"**{name}**")
                    st.write(reason)
    links = [{"Fact": f["name"], "Dimension": link["dimension_model"],
              "Fields": "; ".join(f"{p['fact_field']} → {p['dimension_field']}" for p in link.get("key_pairs", []))}
             for f in target.get("facts", []) for link in f.get("dimension_links", [])]
    if links:
        st.markdown("**Proposed relationships**")
        st.dataframe(links, hide_index=True, width="stretch")


def _dbt(state, events):
    proposals = _proposals(events, "DbtImplementation")
    if proposals:
        proposal = proposals[-1]
        files = proposal.get("files", [])
        st.markdown("**Generated models and files**")
        st.dataframe([{"File": f["path"], "Lines": len(f.get("content", "").splitlines())} for f in files],
                     hide_index=True, width="stretch")
        if proposal.get("explanation"):
            st.write(proposal["explanation"])
        for file in files:
            if file["path"].endswith(".sql"):
                with st.expander(file["path"]):
                    st.code(file.get("content", ""), language="sql")


def render_timeline(state):
    events = state.get("events", [])
    phases = project_progress(events, state.get("status", "running"))
    for index, phase in enumerate(phases):
        phase_events = [e for e in events if STAGE_PHASE.get(e.get("stage")) == index]
        label = "Running" if phase["status"] in {"repairing", "checking"} else phase["status"].capitalize()
        title = "Building data models" if index == 3 else phase["label"]
        with st.expander(f"{title} · {label}", expanded=phase["status"] != "pending"):
            st.write("Building the declared facts and dimensions." if index == 3 else phase["detail"])
            if phase["status"] == "pending":
                st.caption("Waiting for earlier stages to finish.")
                continue
            observations = [e["message"] for e in phase_events if e.get("kind") == "done"]
            if index <= 1 and state.get("profiles"):
                st.markdown("**Key observations**")
                st.dataframe([{"Dataset": name, "Rows": p["rows"], "Columns": len(p["columns"]),
                               "Duplicate rows": p.get("duplicate_rows")}
                              for name, p in state["profiles"].items()], hide_index=True, width="stretch")
                if index == 1 and state.get("relationships"):
                    st.dataframe(state["relationships"], hide_index=True, width="stretch")
            elif observations and index != 5:
                st.markdown("**Key observations**")
                for observation in observations:
                    st.write(observation)
            if index == 2:
                _conceptual(state, phase_events)
            elif index == 3:
                _dbt(state, phase_events)
            elif index == 4:
                proposals = _proposals(phase_events, "AnalysisPlan")
                analysis = state.get("analysis") or (proposals[-1] if proposals else {})
                for item in analysis.get("analyses", []):
                    st.markdown(f"**{item['title']}**")
                    st.write(item.get("rationale", ""))
                    st.caption(f"Grain: {item.get('grain', '')}")
            elif index == 5:
                for finding in state.get("findings", []):
                    st.markdown(f"**{finding['title']}**")

            elif index == 6 and state.get("charts"):
                st.dataframe([{"Chart": c["title"], "Concept": c.get("concept_id")}
                              for c in state["charts"]], hide_index=True, width="stretch")
            if index == 5:
                continue
            reasoning = _reasoning(phase_events)
            if reasoning:
                st.markdown("**Agent reasoning and approach**")
                for text in reasoning:
                    st.markdown(text)
            for event in phase_events:
                if event.get("kind") == "reasoning_status":
                    st.caption(event["message"])



def snapshot_path(events):
    """Locate only this invocation's snapshot from its emitted dbt artifact."""
    for event in events:
        if event.get("stage") == "generating dbt models" and event.get("kind") == "artifact":
            path = event.get("detail", {}).get("path")
            if path:
                return Path(path).parent.parent / "result.json"
    return None
