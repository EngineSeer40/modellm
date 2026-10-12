# ModelLM discovery spike

This prototype loads 2–5 CSV or Parquet files into DuckDB, profiles them, plans a dimensional model, builds and tests dbt models, and renders validated tables as a small Plotly dashboard. The bundled sales example is deterministic and needs no AWS account. Uploaded datasets use Strands agents on Amazon Bedrock for conceptual modelling, dbt implementation, and analytical view selection.

## Setup

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. From this directory:

```sh
uv sync --extra dev
uv run modellm-demo
uv run pytest
uv run streamlit run app.py
```

To exercise the agent path, configure AWS credentials with Bedrock model access and run `uv run modellm-demo --bedrock`, or select **Strands + Bedrock agents** in Streamlit. Set `MODELLM_BEDROCK_MODEL_ID` to override the default model ID. The demo mode is a stable acceptance case, not a substitute for the agent path on arbitrary uploads.

## Workflow and outputs

Each run writes `runs/<id>/analytics.duckdb`, a generated dbt project, dbt artifacts, Plotly figure JSON, and `result.json`. `raw` tables retain the ingested source rows. dbt materializes fact and dimension tables into `main`. The dashboard reads only validated `main` tables. Generated dbt SQL is compiled, built and tested; agent mode makes at most one structured repair attempt after failure. A failed validation prevents dashboard generation.

The Streamlit Request tab shows application stage events and Strands streaming model/tool events during execution. The Agent Trace tab shows human-readable events, dbt logs, timing and any available Strands usage summary. The event callback is the seam for later OTEL/Langfuse instrumentation; neither is required to run this spike, and UI updates do not depend on OTEL transport.

The bundled model treats completed order items as atomic sales. It retains unmatched customers through an unknown dimension member and excludes items with missing orders or products. These are explicit demo assumptions. The synthetic data includes nulls, duplicate-looking customer names, orphan keys, dates, categories, quantities and prices. The raw tables are not cleaned or filtered.

For arbitrary uploads, agent-generated dbt SQL and analytical specifications are validated structurally, then dbt and DuckDB determine whether they execute. The spike deliberately offers no general semantic guarantee beyond successful dbt tests. Review assumptions and data-quality warnings before interpreting a new dataset.

Implementation follows Strands' [structured-output](https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/) and [streaming](https://strandsagents.com/docs/user-guide/sdk/streaming/async-iterators/) APIs and dbt's [DuckDB profile](https://docs.getdbt.com/docs/local/connect-data-platform/duckdb-setup) and [source](https://docs.getdbt.com/docs/build/sources) conventions.


export AWS_PROFILE=your-profile
export AWS_DEFAULT_REGION=your-region
export MODELLM_BEDROCK_MODEL_ID=your-accessible-model-id
uv run modellm-demo --bedrock