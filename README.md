# ModelLM discovery spike

This prototype loads 2–6 uploaded CSV, Parquet, JSON, JSON Lines or NDJSON files into DuckDB, profiles them, uses Strands agents on Amazon Bedrock to plan and build a dimensional model with dbt, and renders accepted analytical results as a Plotly dashboard. All modelling, analysis and plotting decisions use agents.

## Setup

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. From this directory:

```sh
uv sync --extra dev
uv run pytest
uv run streamlit run app.py
```

Configure AWS credentials and a region that can invoke the selected Bedrock model. The default is the AU Claude Haiku 4.5 inference profile; set `MODELLM_BEDROCK_MODEL_ID` to override it. The Request tab shows the selected model ID. Enter a business request and upload 2–6 related datasets before running the workflow.

JSON uploads can contain an array of records, one record object, or newline-delimited records (`.jsonl`/`.ndjson`). Nested fields remain nested DuckDB columns. A single object wrapping a record array (for example, `{"data": [{...}]}`) is rejected with an explanation because it would otherwise appear as one source row; upload the inner array or JSON Lines instead.

For example, in a shell with an AWS profile configured:

```sh
export AWS_PROFILE=your-profile
export AWS_DEFAULT_REGION=ap-southeast-2
export MODELLM_BEDROCK_MODEL_ID=au.anthropic.claude-haiku-4-5-20251001-v1:0
uv run streamlit run app.py
```

## Workflow and outputs

Each run writes `runs/<YYMMDD_HHMM>_<12-hex-id>/modellm.duckdb`, a generated dbt project, dbt artifacts, Plotly figure JSON, and `result.json`. The UTC timestamp prefix makes run directories sort chronologically; the random suffix keeps runs unique. `raw` tables retain the ingested source rows; dbt materializes models into `main`. Contract version 3 declares each model output once, with its definition and optional measure metadata. Source references and planned dependencies are checked against this run. Repairs receive the rejected proposal and actual errors: one conceptual revision, two dbt repairs, one analysis correction and one finding correction. A failed boundary prevents dashboard completion.

Findings cite evidence registered from an accepted DuckDB result, using stable evidence/row IDs. They do not rerun a final list of evidence queries. The dashboard displays supporting evidence and methodology separately, and groups related chart representations by analytical concept. Run traces preserve conceptual proposals, dbt proposal files, accepted SQL, evidence, model call IDs, internal model cycles, and transport failures. Historical plans remain trace artifacts; they are not silently migrated into the new contract.

At the start of each workflow, old per-run DuckDB databases and WAL files are removed after 14 days. If retained run databases exceed 2 GiB, the oldest completed or failed databases are removed until usage is under budget. Active runs are preserved; abandoned runs still marked `running` are expired after 24 hours. `MODELLM_DUCKDB_RETENTION_DAYS` and `MODELLM_DUCKDB_BUDGET_BYTES` override these defaults. Run summaries, dbt logs and chart artifacts remain available after the database expires; raw and transformed table previews require the database.

The Streamlit Request tab shows application stage events and Strands streaming model/tool events during execution. The Agent Trace tab shows human-readable events, dbt logs, timing and any available Strands usage summary. The event callback is the seam for later OTEL/Langfuse instrumentation; neither is required to run this spike, and UI updates do not depend on OTEL transport.

The analysis agent uses read-only DuckDB tools to inspect validated dbt models. Application code validates its final SQL, materializes each analytical result in DuckDB, checks declared result fields, and profiles the accepted result. The plotting agent receives result semantics and statistics and can inspect only the accepted result through scoped tools. Plotly rendering loads that result into a pandas DataFrame. The spike does not establish business correctness merely because dbt tests passed; review assumptions and data-quality warnings when interpreting a dashboard.

Implementation follows Strands' [structured-output](https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/) and [streaming](https://strandsagents.com/docs/user-guide/sdk/streaming/async-iterators/) APIs and dbt's [DuckDB profile](https://docs.getdbt.com/docs/local/connect-data-platform/duckdb-setup) and [source](https://docs.getdbt.com/docs/build/sources) conventions.
