"""Streamlit composition and request handling."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from queue import Empty, Queue

import streamlit as st

from src.workflow import run

from .live import render_stream, render_timeline, snapshot_path
from .views import (
    render_data_model, render_outcome, render_results, render_technical, render_usage,
)


SURFACES = ("Run", "Results", "Data Model", "Technical Details")


def _collect_run():
    """Keep the job and its record independent of a particular UI rerun."""
    job = st.session_state.get("_run_job")
    if not job:
        return st.session_state.get("result")
    while True:
        try:
            job["events"].append(job["queue"].get_nowait())
        except Empty:
            break
    path = snapshot_path(job["events"])
    if path:
        try:
            job["current"] = json.loads(path.read_text())
        except (OSError, ValueError):
            pass
    job["current"]["events"] = job["events"]
    if job["future"].done():
        try:
            st.session_state["result"] = job["future"].result()
            if st.session_state["result"].get("status") == "complete":
                st.session_state["_next_surface"] = "Results"
        except Exception as exc:
            st.session_state["ui_run_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            job["executor"].shutdown(wait=False)
            del st.session_state["_run_job"]
        st.rerun()
    return job["current"]


def render_run(state):
    current = _collect_run()
    busy = "_run_job" in st.session_state
    st.header("Ask a business question")
    st.write("Upload 2–6 related datasets and describe what you want to understand. "
             "ModeLLM builds and validates a data model before calculating your results.")
    # Keep these widgets mounted in the Run tab on every app rerun. Drafts
    # commit when edited, rather than remaining unsubmitted form values.
    if "run_request" not in st.session_state:
        st.session_state["run_request"] = (state or {}).get("request", "")
    with st.container(border=True):
        files = st.file_uploader(
            "Related datasets", type=["csv", "parquet", "json", "jsonl", "ndjson"],
            accept_multiple_files=True, key="run_uploads", disabled=busy,
        )
        request = st.text_area(
            "What would you like to understand?", value=None, key="run_request", disabled=busy,
            height=360, placeholder="Describe the question, the comparisons you need and any business rules to apply.",
        )
        submitted = st.button("Build dashboard", type="primary", disabled=busy)
    show_stream = st.toggle("Show live chronological stream", value=os.environ.get("MODELLM_SHOW_LIVE_STREAM", "1") != "0",
                            key="show_live_stream")
    if show_stream:
        st.subheader("Live chronological stream")
        with st.container(height=420, border=True, autoscroll=True):
            stream = st.empty()
            with stream.container():
                render_stream((current or {}).get("events", []))
    st.subheader("Workflow stages")
    timeline = st.empty()
    with timeline.container():
        render_timeline(current or {"events": [], "status": "running"})
    if submitted:
        if not request.strip():
            st.error("Describe the business question you want to answer.")
        elif not 2 <= len(files) <= 6:
            st.error("Upload between 2 and 6 related datasets.")
        else:
            input_dir = Path("runs") / "uploads"
            input_dir.mkdir(parents=True, exist_ok=True)
            paths = []
            for file in files:
                path = input_dir / Path(file.name).name
                path.write_bytes(file.getvalue())
                paths.append(path)
            # The future survives navigation; only the UI reads session state.
            queue = Queue()
            events = []
            executor = ThreadPoolExecutor(max_workers=1)
            st.session_state.pop("ui_run_error", None)
            st.session_state["_run_job"] = {
                "executor": executor, "queue": queue, "events": events,
                "current": {"events": events, "status": "running", "request": request},
                "future": executor.submit(run, paths, request, Path("runs"), on_event=queue.put),
            }
            st.rerun()
    if st.session_state.get("ui_run_error"):
        st.error("The run could not start. Your request and uploads are retained.")
        with st.expander("Failure diagnostics"):
            st.code(st.session_state["ui_run_error"], language=None)
    if state and not busy:
        st.divider()
        st.subheader("Latest run")
        render_outcome(state)
        if state["status"] == "complete":
            if st.button("Open dashboard", type="primary"):
                st.session_state["_next_surface"] = "Results"
                st.rerun()
        if state["status"] == "failed":
            with st.expander("Reported model usage and cost"):
                render_usage(state)


def render_app():
    st.set_option("client.toolbarMode", "viewer")
    st.set_page_config(page_title="ModeLLM", layout="wide")
    st.html('''
        <style>
          [data-testid="stTableStyledTable"] td {white-space:normal;}
          [data-testid="stTableStyledTable"] [data-testid="stMarkdownContainer"] {
            display:block;width:100%;max-width:none;white-space:normal;
          }
          [data-testid="stAppDeployButton"],
          [data-testid="stMainMenuItem-rerun"],
          [data-testid="stMainMenuItem-autoRerun"],
          [data-testid="stMainMenuItem-clearCache"],
          [data-testid="stMainMenuItem-recordScreencast"] {display:none;}
        </style>
        <a href="https://github.com/EngineSeer40/modellm/" target="_blank"
           rel="noopener noreferrer" aria-label="ModeLLM on GitHub" title="ModeLLM on GitHub"
           style="position:fixed;top:0.85rem;right:3.75rem;z-index:999999;color:inherit;line-height:0;">
          <span aria-hidden="true" style="display:inline-block;width:24px;height:24px;
                background-color:currentColor;mask:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJjdXJyZW50Q29sb3IiIGFyaWEtaGlkZGVuPSJ0cnVlIj4KICAgICAgICAgICAgPHBhdGggZD0iTTEyIC4yOTdhMTIgMTIgMCAwIDAtMy43OTMgMjMuMzg1Yy42LjExMS44Mi0uMjYxLjgyLS41Nzd2LTIuMjM0Yy0zLjMzOC43MjYtNC4wNDMtMS40MTYtNC4wNDMtMS40MTYtLjU0Ni0xLjM4Ny0xLjMzMy0xLjc1Ni0xLjMzMy0xLjc1Ni0xLjA5LS43NDUuMDgzLS43MjkuMDgzLS43MjkgMS4yMDUuMDg1IDEuODM5IDEuMjM3IDEuODM5IDEuMjM3IDEuMDcxIDEuODM1IDIuODA5IDEuMzA1IDMuNDk1Ljk5OC4xMDgtLjc3Ni40MTktMS4zMDUuNzYyLTEuNjA1LTIuNjY1LS4zMDUtNS40NjctMS4zMzQtNS40NjctNS45MzEgMC0xLjMxLjQ2OS0yLjM4MSAxLjIzNi0zLjIyMS0uMTI0LS4zMDMtLjUzNi0xLjUyNC4xMTctMy4xNzYgMCAwIDEuMDA4LS4zMjMgMy4zMDEgMS4yM0ExMS41MiAxMS41MiAwIDAgMSAxMiA2LjA5OGMxLjAxOS4wMDUgMi4wNDUuMTM4IDMuMDAzLjQwNCAyLjI5MS0xLjU1MyAzLjI5Ny0xLjIzIDMuMjk3LTEuMjMuNjU1IDEuNjUyLjI0MyAyLjg3My4xMiAzLjE3Ni43Ny44NCAxLjIzNSAxLjkxMSAxLjIzNSAzLjIyMSAwIDQuNjA5LTIuODA3IDUuNjI0LTUuNDc5IDUuOTIxLjQzMS4zNzIuODE1IDEuMTAyLjgxNSAyLjIyMnYzLjI5M2MwIC4zMTkuMjE2LjY5NC44MjUuNTc2QTEyLjAwMSAxMi4wMDEgMCAwIDAgMTIgLjI5N3oiLz4KICAgICAgICAgIDwvc3ZnPg==) center / contain no-repeat;
                -webkit-mask:url(data:image/svg+xml;base64,PHN2ZyB4bWxucz0iaHR0cDovL3d3dy53My5vcmcvMjAwMC9zdmciIHdpZHRoPSIyNCIgaGVpZ2h0PSIyNCIgdmlld0JveD0iMCAwIDI0IDI0IiBmaWxsPSJjdXJyZW50Q29sb3IiIGFyaWEtaGlkZGVuPSJ0cnVlIj4KICAgICAgICAgICAgPHBhdGggZD0iTTEyIC4yOTdhMTIgMTIgMCAwIDAtMy43OTMgMjMuMzg1Yy42LjExMS44Mi0uMjYxLjgyLS41Nzd2LTIuMjM0Yy0zLjMzOC43MjYtNC4wNDMtMS40MTYtNC4wNDMtMS40MTYtLjU0Ni0xLjM4Ny0xLjMzMy0xLjc1Ni0xLjMzMy0xLjc1Ni0xLjA5LS43NDUuMDgzLS43MjkuMDgzLS43MjkgMS4yMDUuMDg1IDEuODM5IDEuMjM3IDEuODM5IDEuMjM3IDEuMDcxIDEuODM1IDIuODA5IDEuMzA1IDMuNDk1Ljk5OC4xMDgtLjc3Ni40MTktMS4zMDUuNzYyLTEuNjA1LTIuNjY1LS4zMDUtNS40NjctMS4zMzQtNS40NjctNS45MzEgMC0xLjMxLjQ2OS0yLjM4MSAxLjIzNi0zLjIyMS0uMTI0LS4zMDMtLjUzNi0xLjUyNC4xMTctMy4xNzYgMCAwIDEuMDA4LS4zMjMgMy4zMDEgMS4yM0ExMS41MiAxMS41MiAwIDAgMSAxMiA2LjA5OGMxLjAxOS4wMDUgMi4wNDUuMTM4IDMuMDAzLjQwNCAyLjI5MS0xLjU1MyAzLjI5Ny0xLjIzIDMuMjk3LTEuMjMuNjU1IDEuNjUyLjI0MyAyLjg3My4xMiAzLjE3Ni43Ny44NCAxLjIzNSAxLjkxMSAxLjIzNSAzLjIyMSAwIDQuNjA5LTIuODA3IDUuNjI0LTUuNDc5IDUuOTIxLjQzMS4zNzIuODE1IDEuMTAyLjgxNSAyLjIyMnYzLjI5M2MwIC4zMTkuMjE2LjY5NC44MjUuNTc2QTEyLjAwMSAxMi4wMDEgMCAwIDAgMTIgLjI5N3oiLz4KICAgICAgICAgIDwvc3ZnPg==) center / contain no-repeat;"></span>
        </a>
    ''')
    st.title("ModeLLM")
    st.caption("Turn related business datasets into a validated data model and an evidence-backed analytical dashboard.")
    pending = st.session_state.pop("_next_surface", None)
    if pending:
        st.session_state["ui_surface"] = pending
    if "ui_surface" not in st.session_state:
        st.session_state["ui_surface"] = "Run"
    tabs = st.tabs(SURFACES, key="ui_surface", on_change="rerun")
    state = st.session_state.get("result")
    if "_run_job" in st.session_state:
        st.caption("Analysis in progress. Run retains the live record; the other tabs show the latest saved result.")
    with tabs[0]:
        # Poll only the Run fragment while a job is active, without blocking
        # navigation or reconstructing the job on every interaction.
        st.fragment(run_every=0.5 if "_run_job" in st.session_state else None)(render_run)(state)
    with tabs[1]:
        render_results(state)
    with tabs[2]:
        render_data_model(state)
    with tabs[3]:
        render_technical(state)
