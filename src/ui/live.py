"""Chronological diagnostics and stage summaries from emitted run information."""

import json
from pathlib import Path

import streamlit as st
import sqlglot
from sqlglot import exp

from src.run import RunProgress, RunState

from .progress import STAGE_PHASE, project_progress


def _plan_summary(stage, payload):
    """Summarise current and saved contracts without dumping generated payloads."""
    if stage == "conceptual modelling":
        target = payload.get("target_model", payload)
        if not isinstance(target, dict):
            return "ConceptualPlan proposal received; model structure requires correction"
        facts, dimensions = target.get("facts", []), target.get("dimensions", [])
        if not all(isinstance(items, list) and all(isinstance(item, dict) for item in items)
                   for items in (facts, dimensions)):
            return "ConceptualPlan proposal received; fact/dimension structure requires correction"
        names = [item.get("name", "") for item in facts + dimensions]
        return f"ConceptualPlan proposed: {len(facts)} facts, {len(dimensions)} dimensions · " + ", ".join(names)
    if stage == "generating dbt models":
        files = payload.get("files", [])
        if not isinstance(files, list) or not all(isinstance(item, dict) for item in files):
            return "DimensionalPlan proposal received; file structure requires correction"
        return f"DimensionalPlan proposed: {len(files)} files · " + ", ".join(item.get("path", "") for item in files)
    if stage == "analytical interpretation":
        analyses = payload.get("analyses", [])
        if not isinstance(analyses, list) or not all(isinstance(item, dict) for item in analyses):
            return "AnalyticalPlan proposal received; analysis structure requires correction"
        return f"AnalyticalPlan proposed: {len(analyses)} analyses · " + "; ".join(item.get("title", "") for item in analyses)
    if stage == "visualisation generation":
        columns = payload.get("columns", {})
        channels = (", ".join(f"{key}={value}" for key, value in columns.items())
                    if isinstance(columns, dict) else "column mappings awaiting validation")
        return f"Plotting Plan proposed: {payload.get('function', '')} · {channels}"
    return None


def _short(value, limit=240):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit] + "…"


def _query_description(sql):
    """Summarise query activity without displaying SQL expressions."""
    try:
        query = sqlglot.parse_one(sql, read="duckdb")
        if query is None:
            return "Inspecting analytical data"
        tables = list(dict.fromkeys(table.name for table in query.find_all(exp.Table)))
        parts = ["Inspecting " + _short(", ".join(tables))] if tables else ["Inspecting analytical data"]
        for expression, description in ((exp.Join, "combining related data"),
                                        (exp.Where, "selecting relevant rows"),
                                        (exp.AggFunc, "calculating summary values"),
                                        (exp.Group, "comparing groups"),
                                        (exp.Order, "ordering results")):
            if query.find(expression) is not None:
                parts.append(description)
        return " · ".join(parts)
    except (sqlglot.errors.ParseError, ValueError, TypeError):
        return "Inspecting analytical data"


def _tool_description(name, payload):
    if name == "query_data":
        return _query_description(payload.get("sql", ""))
    if name == "describe_model":
        return f"Inspecting schema and row count of {payload.get('model', '')}"
    if name == "list_models":
        return "Listing validated models available to Analysis"
    if name == "category_counts":
        return f"Checking category frequencies for {payload.get('field', '')} (up to {payload.get('limit', 20)} values)"
    return None


def _tool_result(result):
    """Read recorded tool content; never execute or re-query saved results."""
    for block in result.get("content", []):
        if isinstance(block.get("json"), dict):
            return block["json"]
        if isinstance(block.get("text"), str):
            try:
                value = json.loads(block["text"])
                if isinstance(value, dict):
                    return value
            except (ValueError, TypeError):
                pass
    return None


def _result_description(result):
    data = _tool_result(result)
    if result.get("status") == "error" or (data and data.get("error")):
        return "Inspection could not complete; details are in Technical Details"
    if not data:
        return "Inspection completed; result details are in Technical Details"
    if "row_count" in data:
        description = f"Returned {data['row_count']} preview rows"
        if data.get("truncated"):
            description += " (preview limit reached; more rows available)"
        description += "; fields: " + _short(", ".join(map(str, data.get("columns", []))))
        rows = data.get("rows", [])
        if len(rows) == 1 and isinstance(rows[0], dict):
            description += "; values: " + _short(", ".join(f"{key}={value}" for key, value in rows[0].items()))
        return description
    if "model" in data:
        fields = ", ".join(f"{field['name']} ({field['type']})" for field in data.get("columns", []))
        return f"{data['rows']:,} rows; fields: " + _short(fields)
    if "models" in data:
        return "Available models: " + ", ".join(map(str, data["models"]))
    if "values" in data:
        values = data["values"]
        description = "; ".join(f"{item.get('value')}: {item.get('count')} rows" for item in values[:5])
        return "Category counts: " + _short(description) + (" (more categories available)" if data.get("truncated") else "")
    return "Inspection completed; result details are in Technical Details"


def _validation_summary(event, retrying):
    detail = event.get("detail", {})
    issues = detail.get("issues", [])
    attempt = detail.get("attempt", detail.get("revision", ""))
    lines = [f"Plan attempt {attempt} needs revision" if retrying else f"Plan attempt {attempt} rejected"]
    for issue in issues:
        if not isinstance(issue, dict):
            lines.append("  • " + str(issue))
            continue
        text = f"{issue.get('path', '')}: {issue.get('message', issue.get('code', ''))}"
        if issue.get("observed") is not None:
            text += f"; supplied: {_short(issue['observed'])}"
        if issue.get("expected") is not None:
            text += f"; expected: {_short(issue['expected'])}"
        lines.append("  • " + text)
    if retrying:
        lines.append("Revising the plan with these validation issues.")
    return "\n".join(lines)


def stream_text(events):
    """Project the full investigation and corrections from recorded evidence."""
    entries = []
    pending = {}
    contracts = {"ConceptualPlan", "DimensionalPlan", "AnalyticalPlan", "PlottingPlan",
                 "AcceptedResultPlottingPlan", "DbtImplementation", "AnalysisPlan",
                 "PlotSpec", "AcceptedResultPlotSpec"}

    def append(event, message):
        time = event.get("time", "").replace("T", " ")
        entries.append(f"[{time}] {event.get('stage', '')} · {message}")
        return len(entries) - 1

    for index, event in enumerate(events):
        kind, stage = event.get("kind", ""), event.get("stage", "")
        detail = event.get("detail", {})
        if stage == "visualisation generation" and kind in {"failure", "model_failure"}:
            continue
        if kind == "model_message":
            message = detail.get("message", {})
            if not isinstance(message, dict):
                continue
            for block in message.get("content", []):
                tool = block.get("toolUse", {})
                payload = tool.get("input")
                key = (detail.get("call_id", ""), tool.get("toolUseId"))
                if isinstance(payload, dict):
                    if tool.get("name") in contracts:
                        summary = _plan_summary(stage, payload)
                        if summary:
                            append(event, summary)
                    else:
                        summary = _tool_description(tool.get("name"), payload)
                        if summary:
                            position = append(event, summary + "\n  Running inspection…")
                            pending[key] = (position, summary, event)
                result = block.get("toolResult")
                if isinstance(result, dict):
                    key = (detail.get("call_id", ""), result.get("toolUseId"))
                    if key in pending:
                        position, summary, original = pending.pop(key)
                        time = original.get("time", "").replace("T", " ")
                        entries[position] = f"[{time}] {original.get('stage', '')} · {summary}\n  {_result_description(result)}"
        elif stage == "conceptual modelling" and detail.get("issues") and kind in {"failure", "repair"}:
            # The workflow emits rejection and repair separately for the same issues.
            next_event = events[index + 1] if index + 1 < len(events) else {}
            if kind == "failure" and next_event.get("stage") == stage and next_event.get("kind") == "repair" and next_event.get("detail", {}).get("issues") == detail["issues"]:
                continue
            append(event, _validation_summary(event, retrying=kind == "repair"))
        elif kind in {"start", "done", "repair", "failure", "model_failure", "contract_check"}:
            if kind == "model_failure":
                message = "Agent call could not complete; details are in Technical Details"
            elif kind == "failure":
                message = "Stage checks could not complete; details are in Technical Details"
            elif kind == "repair":
                message = "Requesting an automatic correction"
            else:
                message = str(event.get("message", ""))
            if message and not message.lstrip().startswith(("{", "[")):
                append(event, message)
    return "\n\n".join(entries)


def render_stream(events):
    if text := stream_text(events):
        st.code(text, language=None, wrap_lines=True)
    else:
        st.caption("Agent activity will appear here when the run starts.")


def _proposals(events, name):
    legacy_names = {"ConceptualPlan": "AnalyticalPlan", "DimensionalPlan": "DbtImplementation",
                    "AnalyticalPlan": "AnalysisPlan"}
    names = {name, legacy_names.get(name)}
    proposals = []
    for event in events:
        message = event.get("detail", {}).get("message", {})
        if not isinstance(message, dict):
            continue
        for block in message.get("content", []):
            tool = block.get("toolUse", {})
            if tool.get("name") in names and isinstance(tool.get("input"), dict):
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
    proposals = _proposals(events, "ConceptualPlan")
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
            st.table([{"Modelling decision": " ".join(f"{name}: {reason}".split())}
                      for name, reason in reasons if reason], hide_index=True)
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
    proposals = _proposals(events, "DimensionalPlan")
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
                with st.expander(file["path"], key=f"dbt_file_{state.get('run_id', 'draft')}_{file['path']}"):
                    st.code(file.get("content", ""), language="sql")


def render_timeline(state: RunState | RunProgress):
    events = state.get("events", [])
    phases = project_progress(events, state.get("status", "running"))
    for index, phase in enumerate(phases):
        phase_events = [e for e in events if STAGE_PHASE.get(e.get("stage")) == index]
        if index == STAGE_PHASE["analytical finding"] and not phase_events:
            continue
        label = "Running" if phase["status"] in {"repairing", "checking"} else phase["status"].capitalize()
        title = "Building data models" if index == 3 else phase["label"]
        # Mount a new, open expander once this phase first has activity. Keep its
        # key and initial expanded value unchanged afterward so polling and
        # completion preserve the user's browser-side open/closed choice.
        started = bool(phase_events)
        with st.expander(f"{title} · {label}", expanded=started,
                         key=f"stage_{state.get('run_id', 'draft')}_{index}_{'started' if started else 'pending'}"):
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
                proposals = _proposals(phase_events, "AnalyticalPlan")
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
