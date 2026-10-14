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

For the current trial, Plotting is hardcoded to `au.anthropic.claude-haiku-4-5-20251001-v1:0`, including when `MODELLM_BEDROCK_MODEL_ID` selects Sonnet or another model. All other stages use the configured model. The Request caption shows both selections, and per-agent usage displays the actual model used.

JSON uploads can contain an array of records, one record object, or newline-delimited records (`.jsonl`/`.ndjson`). Nested fields remain nested DuckDB columns. A single object wrapping a record array (for example, `{"data": [{...}]}`) is rejected with an explanation because it would otherwise appear as one source row; upload the inner array or JSON Lines instead.

For example, in a shell with an AWS profile configured:

```sh
export AWS_PROFILE=your-profile
export AWS_DEFAULT_REGION=ap-southeast-2
export MODELLM_BEDROCK_MODEL_ID=au.anthropic.claude-haiku-4-5-20251001-v1:0
uv run streamlit run app.py
```

Bedrock calls use a 3600-second read timeout. To try Nova 2 Lite from Sydney, set `MODELLM_BEDROCK_MODEL_ID=global.amazon.nova-2-lite-v1:0`; this uses global routing. Nova 2 Lite calls enable extended reasoning at low effort. Nova currently redacts its reasoning text; the Request and Agent Trace tabs show this status, any provider-visible reasoning, and streamed agent output. Streamed output is saved in `result.json`, including partial output before a failure.

To try Mistral Large 3 through Bedrock in Sydney:

```sh
AWS_DEFAULT_REGION=ap-southeast-2 \
MODELLM_BEDROCK_MODEL_ID=mistral.mistral-large-3-675b-instruct \
uv run streamlit run app.py
```

This uses the existing AWS credentials and bills through AWS. Mistral Large 3 supports Converse, streaming and structured outputs, with a 256K context window and 32K maximum output according to [AWS model documentation](https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-mistral-ai-mistral-large-3.html). New calls record the actual AWS region. Sydney standard list-price estimates use $0.515 input and $1.545 output per million tokens; unknown regions and cache pricing remain unavailable. No separate Mistral API key is needed.

## Workflow and outputs

Each run writes `runs/<YYMMDD_HHMM>_<12-hex-id>/modellm.duckdb`, a generated dbt project, dbt artifacts, Plotly figure JSON, and `result.json`. The UTC timestamp prefix makes run directories sort chronologically; the random suffix keeps runs unique. `raw` tables retain the ingested source rows; dbt materializes models into `main`. Contract version 4 declares processes, dimensions and facts directly on the plan, with each output defined once and optional additivity on that output. Matching uses declared field values and equality pairs; normalization belongs in output definitions. Source references and planned dependencies are checked against this run. Repairs receive the rejected proposal and actual errors: one conceptual revision, two dbt repairs, one analysis correction and one finding correction. Valid empty results are retained. Plot filters must match the supplied presentation selection. A failed boundary prevents dashboard completion.

Findings cite evidence registered from an accepted DuckDB result, using stable evidence/row IDs. They do not rerun a final list of evidence queries. The dashboard displays supporting evidence and methodology separately, and groups related chart representations by analytical concept. Run traces preserve conceptual proposals, dbt proposal files, accepted SQL, evidence, model call IDs, internal model cycles, and transport failures. Historical plans remain trace artifacts; they are not silently migrated into the new contract.

At the start of each workflow, old per-run DuckDB databases and WAL files are removed after 14 days. If retained run databases exceed 2 GiB, the oldest completed or failed databases are removed until usage is under budget. Active runs are preserved; abandoned runs still marked `running` are expired after 24 hours. `MODELLM_DUCKDB_RETENTION_DAYS` and `MODELLM_DUCKDB_BUDGET_BYTES` override these defaults. Run summaries, dbt logs and chart artifacts remain available after the database expires; raw and transformed table previews require the database.

The Streamlit Request tab shows application stage events and Strands streaming model/tool events during execution. The Agent Trace tab shows human-readable events, dbt logs, timing and any available Strands usage summary. The event callback is the seam for later OTEL/Langfuse instrumentation; neither is required to run this spike, and UI updates do not depend on OTEL transport.

`runs/experiments/ledger.csv` records one row per run, with links, scenario/request grouping, outcome, duration, retries, tokens and estimated USD cost. `agent_usage.csv` breaks usage down by agent; `overview.md` provides a summary. New runs update the ledger at start and completion, including failures. Refresh saved records with `.venv/bin/python -m src.experiments runs`. Labels live in `scenarios.csv`; ledger notes survive refresh. Request and Dashboard tabs both display total and per-agent usage, including failed runs. Costs use verified standard provider list rates and are estimates of reported usage; unknown rates or caching details produce unavailable estimates. See `runs/experiments/README.md` for conventions and pricing sources.

The analysis agent uses read-only DuckDB tools to inspect validated dbt models. Application code validates its final SQL, materializes each analytical result in DuckDB, checks declared result fields, and profiles the accepted result. The plotting agent receives result semantics and statistics and can inspect only the accepted result through scoped tools. Plotly rendering loads that result into a pandas DataFrame. The spike does not establish business correctness merely because dbt tests passed; review assumptions and data-quality warnings when interpreting a dashboard.

Analysis, dbt, Finding and Plotting calls enable Strands automatic prompt caching for Anthropic Claude models, using the default five-minute lifetime for prompts, tools and conversation prefixes. Model call events record the configuration; `cache_usage` events report reads and writes. Usage panels and ledgers distinguish uncached input, cache reads and cache writes; input totals include all three. Known Claude prices include five-minute cache writes at 1.25 times the input rate and reads at 0.1 times the input rate. Unsupported cache prices remain unavailable. Cache hits and savings must be measured from provider responses.

Implementation follows Strands' [structured-output](https://strandsagents.com/docs/user-guide/sdk/agents/structured-output/) and [streaming](https://strandsagents.com/docs/user-guide/sdk/streaming/async-iterators/) APIs and dbt's [DuckDB profile](https://docs.getdbt.com/docs/local/connect-data-platform/duckdb-setup) and [source](https://docs.getdbt.com/docs/build/sources) conventions.
