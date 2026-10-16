"""Streamlit composition and request handling."""

import base64
import hmac
import json
import os
import secrets
import time
from pathlib import Path

import streamlit as st

from samples.sample_questions import SAMPLE_1, SAMPLE_1_FILES

from src.run import RunState
from src.config import ReleaseSettings
from src.execution import RunManager

from .live import render_stream, render_timeline
from .saved_run import load_saved_run
from .views import (
    render_data_model,
    render_outcome,
    render_results,
    render_technical,
    render_usage,
)

SURFACES = ("Run", "Results", "Data Model", "Technical Details")


@st.cache_resource
def run_manager():
    return RunManager(ReleaseSettings.from_env())


def _live_access(settings):
    if not settings.live_enabled:
        st.info("Live runs are currently disabled.")
        return False
    if not settings.prod:
        return True
    if st.session_state.get("_preview_access"):
        return True
    expected = os.environ.get("MODELLM_LIVE_ACCESS_CODE", "")
    if len(expected) < 8:
        st.info("Live access is not configured.")
        return False
    with st.form("preview_access"):
        code = st.text_input("Access code", type="password")
        submitted = st.form_submit_button("Unlock live runs")
    if submitted and time.time() >= st.session_state.get("_access_retry_at", 0):
        if hmac.compare_digest(code.encode(), expected.encode()):
            st.session_state["_preview_access"] = True
            st.rerun()
        else:
            st.session_state["_access_retry_at"] = time.time() + 5
            st.error("Access code not accepted. Wait a moment before trying again.")
    return False


def _collect_run():
    """Poll only this session's known snapshot; supervision never depends on reruns."""
    job = st.session_state.get("_run_job")
    if not job:
        return st.session_state.get("result")
    try:
        job["current"] = json.loads(job["workspace"].result_path.read_text())
    except (OSError, ValueError):
        pass
    if job["future"].done():
        try:
            st.session_state["result"] = job["future"].result()
            if st.session_state["result"].get("status") == "complete":
                st.session_state["_next_surface"] = "Results"
        except Exception as exc:
            st.session_state["ui_run_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            del st.session_state["_run_job"]
        st.rerun()
    return job["current"]


def _insert_demo_data():
    """Fill the draft only; demo inputs use the normal validated submission path."""
    samples = Path(__file__).resolve().parents[2] / "samples"
    uploads = {name: (samples / name).read_bytes() for name in SAMPLE_1_FILES}
    st.session_state["_pending_demo_uploads"] = uploads
    st.session_state["run_request"] = SAMPLE_1.strip()
    # The native uploader is read-only in session state. A new key clears old uploads.
    st.session_state["_upload_version"] = st.session_state.get("_upload_version", 0) + 1


def _populate_demo_uploads(uploader_key):
    """Select trusted bundled files through the native uploader's browser input.

    Streamlit forbids setting file-uploader state from Python. This small browser
    bridge uses File/DataTransfer and the normal upload event, so Streamlit owns
    the file chips, removal, uploaded bytes and validation. No private SDK APIs.
    """
    uploads = st.session_state.pop("_pending_demo_uploads", None)
    if uploads is None:
        return
    payload = json.dumps([
        {"name": name, "data": base64.b64encode(data).decode("ascii")}
        for name, data in uploads.items()
    ]).replace("<", "\\u003c")
    # Only our explicit bundled sample files are embedded in executable HTML.
    st.html(f'''<script>
      (async () => {{
        const selector = '.st-key-{uploader_key} input[type="file"]';
        let input;
        for (let attempt = 0; attempt < 100; attempt++) {{
          input = document.querySelector(selector);
          if (input) break;
          await new Promise(resolve => setTimeout(resolve, 50));
        }}
        if (!input || input.dataset.demoInserted) return;
        input.dataset.demoInserted = 'true';
        const transfer = new DataTransfer();
        for (const file of {payload}) {{
          const bytes = Uint8Array.from(atob(file.data), c => c.charCodeAt(0));
          transfer.items.add(new File([bytes], file.name,
            {{type: file.name.endsWith('.json') ? 'application/json' : 'text/csv'}}));
        }}
        input.files = transfer.files;
        input.dispatchEvent(new Event('change', {{bubbles: true}}));
      }})();
    </script>''', unsafe_allow_javascript=True)


def render_run(state: RunState | None, *, saved=False):
    if saved:
        st.header("Business question")
        st.write(state.get("request", ""))
        render_outcome(state)
        st.subheader("Workflow stages")
        render_timeline(state)
        with st.expander("Chronological stream"):
            render_stream(state.get("events", []))
        return
    current = _collect_run()
    busy = "_run_job" in st.session_state
    manager = run_manager()
    st.header("Add data and describe what you are looking for")
    # st.caption(
    #     "Uploads are deleted after a run. Other private artifacts expire within 24 hours and may disappear on a service restart."
    # )
    if not _live_access(manager.settings):
        return
    st.text(
        "Upload 2–6 related datasets and describe what you would like ModeLLM to analyse, then press Start Modelling.\n"
        "When finished, you can view the transformed data and visualisation plots with the tabs above.\n"
        "Along the way, observe the decisions the LLM is making below."
    )
    # Keep these widgets mounted in the Run tab on every app rerun. Drafts
    # commit when edited, rather than remaining unsubmitted form values.
    if "run_request" not in st.session_state:
        st.session_state["run_request"] = (state or {}).get("request", "")
    with st.container(border=True):
        uploader_key = f"run_uploads_{st.session_state.get('_upload_version', 0)}"
        with st.container(gap="xxsmall", key="dataset_picker"):
            st.markdown("Related datasets")
            with st.container(horizontal=True, gap=None, vertical_alignment="bottom"):
                st.button("Insert Demo Data", key="insert_demo_data",
                          on_click=_insert_demo_data, disabled=busy)
                files = st.file_uploader(
                    "Related datasets",
                    type=["csv", "parquet", "json", "jsonl", "ndjson"],
                    accept_multiple_files=True, key=uploader_key, disabled=busy,
                    label_visibility="collapsed",
                )
        _populate_demo_uploads(uploader_key)
        uploads = [(file.name, file.getvalue()) for file in files]
        request = st.text_area(
            "What would you like to understand?",
            value=None,
            key="run_request",
            disabled=busy,
            height=360,
            placeholder="Describe the question, the comparisons you need and any business rules to apply.",
        )
        submitted = st.button("Start Modelling", type="primary", disabled=busy)
    show_stream = st.toggle(
        "Show live chronological stream",
        value=os.environ.get("MODELLM_SHOW_LIVE_STREAM", "1") != "0",
        key="show_live_stream",
    )
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
        elif not 2 <= len(uploads) <= 6:
            st.error("Upload between 2 and 6 related datasets.")
        else:
            try:
                owner = st.session_state.setdefault(
                    "_run_owner", secrets.token_urlsafe(32)
                )
                job = manager.submit(uploads, request, owner)
                st.session_state.pop("ui_run_error", None)
                st.session_state["_run_job"] = job
            except (ValueError, OSError) as exc:
                st.error(str(exc))
                return
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


def render_app(*, saved_run=None):
    st.set_option("client.toolbarMode", "viewer")
    st.set_page_config(page_title="ModeLLM", layout="wide")
    if saved_run is not None:
        if st.session_state.get("_saved_run_source") != saved_run:
            try:
                st.session_state["result"] = load_saved_run(saved_run)
            except (OSError, ValueError) as exc:
                st.error(f"Could not open saved run: {exc}")
                return
            st.session_state["_saved_run_source"] = saved_run
            st.session_state["_next_surface"] = "Results"
    st.html("""
        <style>
          /* Streamlit 1.65 widget styles set their own fonts; override the text elements. */
          /* App title (st.title). */
          [data-testid="stHeading"] h1 {
            color: #ffffff !important;
            font-size: 3rem !important;
          }
          /* Section headings and the tagline (st.header), sized independently. */
          [data-testid="stHeading"] h2 {
            font-size: 1.5rem !important;
          }
          [data-testid="stCaptionContainer"],
          [data-testid="stCaptionContainer"] p {
            font-size: 1rem !important;
            color: #dedede !important;
          }
          [data-testid="stTextArea"] textarea {
            font-size: 1rem !important;
          }
          [data-testid="stTextArea"] textarea::placeholder {
            font-size: inherit !important;
          }
          [data-testid="stTabs"] [data-testid="stTab"],
          [data-testid="stTabs"] [data-testid="stTab"] p {
            font-size: 1.15rem !important;
          }
          [data-testid="stFileUploader"] [data-testid="stWidgetLabel"] p,
          [data-testid="stTextArea"] [data-testid="stWidgetLabel"] p {
            font-size: 1rem !important; /* 18px */
            color: #dedede !important;
          }
          /* Join the sample action to the native upload field as one control. */
          .st-key-dataset_picker .st-key-insert_demo_data {
            min-height: 4.25rem;
            padding: 0.75rem 0.5rem;
            display: flex;
            align-items: center;
            box-sizing: border-box;
            background: light-dark(#f0f2f6, #262730);
            border-right: 1px solid light-dark(#d6d8dc, #41444c);
            border-radius: 0.5rem 0 0 0.5rem;
          }
          .st-key-dataset_picker .st-key-insert_demo_data button,
          .st-key-dataset_picker [data-testid="stFileUploaderDropzone"] > span > button {
            width: 8.25rem;
            min-height: 2.5rem;
          }
          .st-key-dataset_picker [data-testid="stFileUploaderDropzone"] {
            align-items: center;
            border-top-left-radius: 0;
            border-bottom-left-radius: 0;
          }
          @media (max-width: 640px) {
            .st-key-dataset_picker .st-key-insert_demo_data {
              min-width: 100%;
              border-radius: 0.5rem 0.5rem 0 0;
              border-right: 0;
              border-bottom: 1px solid light-dark(#d6d8dc, #41444c);
            }
            .st-key-dataset_picker [data-testid="stFileUploaderDropzone"] {
              border-radius: 0 0 0.5rem 0.5rem;
            }
          }

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
    """)
    st.title("ModeL:primary[LM]")
    st.header("Language driven data modeller & visualisation agent")
    if saved_run is not None:
        st.caption(f"Saved run: {st.session_state['result']['run_id']} · Model execution disabled")
    pending = st.session_state.pop("_next_surface", None)
    if pending:
        st.session_state["ui_surface"] = pending
    if "ui_surface" not in st.session_state:
        st.session_state["ui_surface"] = "Run"
    tabs = st.tabs(SURFACES, key="ui_surface", on_change="rerun")
    state = st.session_state.get("result")
    if "_run_job" in st.session_state:
        st.text(
            "Analysis in progress. Run retains the live record; the other tabs show the latest saved result."
        )
    with tabs[0]:
        # Poll only the Run fragment while a job is active, without blocking
        # navigation or reconstructing the job on every interaction.
        st.fragment(run_every=0.5 if "_run_job" in st.session_state else None)(
            render_run
        )(state, saved=saved_run is not None)
    with tabs[1]:
        render_results(state)
    with tabs[2]:
        render_data_model(state)
    with tabs[3]:
        render_technical(state)
