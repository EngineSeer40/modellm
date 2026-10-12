# ModelLM discovery spike

This prototype loads 2–5 CSV or Parquet files into DuckDB, profiles them, plans a dimensional model, builds and tests dbt models, and renders validated tables as a small Plotly dashboard. The bundled sales example is deterministic and needs no AWS account. Uploaded datasets use Strands agents on Amazon Bedrock for conceptual modelling, dbt implementation, analytical queries, and plot selection.

## Setup

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. From this directory:

```sh
uv sync --extra dev
uv run modellm-demo
uv run pytest
uv run streamlit run app.py
```

The Request tab lets you select **deterministic** or **agent** separately for conceptual modelling, dbt generation, and analytical interpretation. With no uploads, all stages can use the bundled sales data. Deterministic stages use fixed fixtures for that dataset; uploads require all three agent stages. Mixed runs are supported when the chosen fixtures match the preceding stage's model. An incompatible mix stops with an explicit error rather than silently replacing the plan.

For CLI isolation, `uv run modellm-demo` uses all deterministic stages, and `uv run modellm-demo --bedrock` uses all agents. Override one stage with, for example, `uv run modellm-demo --analysis agent` or `uv run modellm-demo --bedrock --dbt deterministic`. The default Bedrock model is Claude Haiku 4.5's AU inference profile; set `MODELLM_BEDROCK_MODEL_ID` to override it. The Request tab shows the selected model ID.

## Workflow and outputs

Each run writes `runs/<YYMMDD_HHMM>_<12-hex-id>/modellm.duckdb`, a generated dbt project, dbt artifacts, Plotly figure JSON, and `result.json`. The UTC timestamp prefix makes run directories sort chronologically; the random suffix keeps runs unique when multiple start in the same minute. The same `run_id` is recorded in `result.json`, and the DuckDB file is scoped to that run directory. `raw` tables retain the ingested source rows. dbt materializes fact and dimension tables into `main`. The dashboard reads only validated `main` tables. Agent mode checks plan structure and requests one revision if a process or dimension is disconnected, a source is unknown, or a measure embeds an aggregate. Generated dbt SQL is compiled, built and tested; agent mode makes at most one structured dbt repair attempt after failure. A failed validation prevents dashboard generation.

At the start of each workflow, old per-run DuckDB databases and WAL files are removed after 14 days. If retained run databases exceed 2 GiB, the oldest completed or failed databases are removed until usage is under budget. Active runs are preserved; abandoned runs still marked `running` are expired after 24 hours. `MODELLM_DUCKDB_RETENTION_DAYS` and `MODELLM_DUCKDB_BUDGET_BYTES` override these defaults. Run summaries, dbt logs and chart artifacts remain available after the database expires; raw and transformed table previews require the database.

The Streamlit Request tab shows application stage events and Strands streaming model/tool events during execution. The Agent Trace tab shows human-readable events, dbt logs, timing and any available Strands usage summary. The event callback is the seam for later OTEL/Langfuse instrumentation; neither is required to run this spike, and UI updates do not depend on OTEL transport.

The bundled model treats completed order items as atomic sales. It retains unmatched customers through an unknown dimension member and excludes items with missing orders or products. These are explicit demo assumptions. The synthetic data includes nulls, duplicate-looking customer names, orphan keys, dates, categories, quantities and prices. The raw tables are not cleaned or filtered.

For arbitrary uploads, the analysis agent uses read-only DuckDB tools to inspect validated dbt models and must inspect each final SQL query. The application checks that final queries reference only validated models, executes them into pandas DataFrames, then asks a plotting agent to choose a Plotly Express visual from each result. The plotting model sees the result schema and a 12-row sample; the renderer uses the full DataFrame. The spike deliberately offers no general semantic guarantee beyond successful dbt tests and inspected analysis queries. Review assumptions and data-quality warnings before interpreting a new dataset.

Implementation follows Strands' [structured-output](https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/) and [streaming](https://strandsagents.com/docs/user-guide/sdk/streaming/async-iterators/) APIs and dbt's [DuckDB profile](https://docs.getdbt.com/docs/local/connect-data-platform/duckdb-setup) and [source](https://docs.getdbt.com/docs/build/sources) conventions.


export AWS_PROFILE=your-profile
export AWS_DEFAULT_REGION=ap-southeast-2
export MODELLM_BEDROCK_MODEL_ID=au.anthropic.claude-haiku-4-5-20251001-v1:0
uv run modellm-demo --bedrock
