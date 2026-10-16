# ModeLLM

ModeLLM turns related datasets and a business question into a dimensional data model and an analytical dashboard. It uses Amazon Bedrock agents to plan the model, generate and validate dbt models in DuckDB, analyse the resulting data, and select Plotly visualisations. Streamlit provides the interface.

## Get running locally

Use Linux, macOS or WSL, Python 3.12+, and [uv](https://docs.astral.sh/uv/). Run these commands from the repository root.

### 1. Install the dependencies

```sh
uv sync --frozen --extra dev
```

### 2. Configure AWS and the application

Use your normal AWS credential setup, such as an existing named profile. The application uses the AWS SDK credential chain; it does not require a separate Anthropic API key. Your identity must have access to both configured Bedrock models, including permission for streaming inference. See the [AWS inference prerequisites](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-prereq.html).

Create a local `.env` file with these settings:

```dotenv
AWS_REGION=ap-southeast-2
MODELLM_PROD=false
MODELLM_LIVE_ENABLED=true
MODELLM_BEDROCK_MODEL_ID=au.anthropic.claude-sonnet-4-6
MODELLM_PLOTTING_MODEL_ID=au.anthropic.claude-haiku-4-5-20251001-v1:0
# If you use a named AWS profile, uncomment and replace this placeholder:
# AWS_PROFILE=your-profile-name
```

Main stages default to **Sonnet 4.6**; plotting defaults to **Haiku 4.5**. Both can be overridden independently. Region and inference-profile availability must match for both models. Technical Details records the model actually used by each agent.

Local development bypasses the access-code gate. Production admission limits are off unless explicitly configured; DuckDB resource and workspace limits still apply. Live model runs incur AWS charges.

### 3. Start the app

```sh
uv run --env-file .env --frozen streamlit run app.py --server.address=127.0.0.1
```

Open **http://localhost:8501**. The command explicitly [loads `.env` through uv](https://docs.astral.sh/uv/configuration/files/); running Streamlit alone does not load it automatically. Restart after changing environment settings.

To see UI code changes without building a container, add the watcher options:

```sh
uv run --env-file .env --frozen streamlit run app.py \
  --server.address=127.0.0.1 \
  --server.fileWatcherType=auto --server.runOnSave=true
```

### 4. Run an analysis

In **Run**, upload **2–6 related datasets** and describe what you want to understand. Alternatively, select **Insert Demo Data** to load the bundled `SAMPLE_1` files and question from [samples/](samples/). You can edit the question and remove files before submission. Inserting demo data does not invoke a model; **Start Modelling** does.

Supported formats are CSV, Parquet, JSON, JSON Lines and NDJSON. JSON can contain an array of records, one record object, or newline-delimited records. Nested fields remain nested DuckDB columns. A wrapper such as `{"data": [...]}` is rejected; upload the inner array instead.

Completed runs open **Results**, with charts, findings, supporting evidence and usage. **Data Model** shows the model diagram and table previews. **Technical Details** contains stage diagnostics, dbt results, repairs and recorded model output. Inputs remain available when switching tabs within the same browser session; sessions do not survive a server restart.

## Reopen a saved dashboard without model calls

Preserve the **whole finished run directory**, including its database, `result.json` and chart files. With the default local runs folder, replace `YOUR_RUN_ID` below with the directory name you want to keep:

```sh
mkdir -p saved_runs
cp -R runs/YOUR_RUN_ID saved_runs/YOUR_RUN_ID
```

If you configured another `MODELLM_RUNS_ROOT`, copy from that folder instead. Make the copy before normal retention removes the run.

Open the saved copy with:

```sh
uv run --frozen streamlit run app.py \
  --server.address=127.0.0.1 --server.port=8502 \
  --server.fileWatcherType=auto --server.runOnSave=true \
  -- --saved-run saved_runs/YOUR_RUN_ID
```

Visit **http://localhost:8502**. All four tabs remain available, using the copied charts, findings, database and diagnostics. The Run tab shows execution history and cannot start another workflow. No AWS credentials are needed. You can also pass the path to the saved `result.json`.

This mode opens an operator-selected local file, without the live-run access gate. Keep it bound to localhost and use trusted run folders: anyone who can reach that app process can view the selected dashboard. `saved_runs/` is ignored by Git and sits outside default run cleanup; keep it outside any custom runs root too.

## Test production settings locally

In your private `.env`, change or add:

```dotenv
MODELLM_PROD=true
MODELLM_LIVE_ENABLED=true
MODELLM_RUNS_ROOT=./runs/prod-local
MODELLM_LIVE_ACCESS_CODE=REPLACE_WITH_YOUR_PRIVATE_ACCESS_CODE
STREAMLIT_SERVER_MAX_UPLOAD_SIZE=20
```

Replace the access-code placeholder with your own code of at least **8 characters**, then use the same local startup command. Production mode enables admission limits, deadlines and reported-cost limits. For example, defaults include 20 MiB per file, 60 MiB total uploads, 10 runs per day, a 30-minute run deadline and a $3 reported-cost limit per run. Cost limits use reported estimates and allow the current model response to finish; they are not an AWS billing cap.

With `MODELLM_PROD=true`, live runs default to disabled unless `MODELLM_LIVE_ENABLED=true` is supplied. Setting live enabled to `false` disables new runs in either mode. For local development, restore `MODELLM_PROD=false`; any explicit numeric overrides still apply. The full settings and defaults are in [src/config.py](src/config.py).

## Deploy to ECS

The container and CloudFormation templates deploy one Streamlit process on one ECS Fargate task behind an HTTPS application load balancer. Follow [infra/README.md](infra/README.md) for AWS prerequisites, IAM roles, GitHub configuration, container checks and release instructions. [infra/parameters.json.example](infra/parameters.json.example) contains placeholders for deployment parameters.

The existing **Deploy production** workflow runs after a PR from `dev` into `master` is merged, or manually from `master`. Its GitHub environments are `release-build` and `production`.

ECS sets `MODELLM_PROD=true` in [infra/service.yaml](infra/service.yaml). To enable live runs, set `"LiveEnabled": "true"` inside the `CFN_PARAMETERS` variable in GitHub's **production** environment and run the deployment workflow. The access code comes from Secrets Manager, supplied through the existing `PreviewSecretArn` parameter; put no secret value in `CFN_PARAMETERS` or the image.

ECS task storage, browser sessions and daily counters are ephemeral. Task replacement can interrupt runs and removes local artifacts. Preserve anything you need before deploying.

## Outputs, retention and data handling

Runs are stored under `runs/` locally, or the directory selected by `MODELLM_RUNS_ROOT` (`/data/runs` by default in production). Each run has a UTC timestamp and random ID, and contains:

- `modellm.duckdb`: ingested `raw` tables, dbt models in `main`, and accepted analysis results.
- `dbt/`: generated SQL, model definitions, validation artifacts and logs.
- `chart_*.json`: saved Plotly charts.
- `result.json`: status, request, findings, evidence, attempts, model events and usage.

Copied upload files are deleted when execution finishes or fails. Source rows remain in DuckDB until the workspace is removed. Current cleanup removes **whole application-owned run folders**, normally after 24 hours or earlier when the 2 GiB workspace budget requires it. `MODELLM_RETENTION_SECONDS` and `MODELLM_WORKSPACE_BYTES` configure these limits. Active runs are protected; legacy folders without ownership markers count toward usage but are not automatically deleted. Retention is applied during application activity, not by a separate scheduled service.

Treat run folders, saved dashboards and diagnostic downloads as private data. Model prompts and tool results can include source values and business questions sent to the configured Bedrock service. Deleting the original uploads does not remove these values from the database or diagnostics.

Keep AWS credentials in your normal AWS credential setup or, for ECS, the task role. Keep the live access code in a private local environment or Secrets Manager. `.env`, `runs/` and `saved_runs/` are ignored by Git; the container build allowlist excludes local credentials and run artifacts. Review files before committing or sharing diagnostics.

## Token costs and caching

All six token-cost controls are **enabled by default**, independently of production mode. An explicit empty `MODELLM_COST_EXPERIMENTS=` disables them for A/B testing; a comma-separated list enables only the named controls. Unknown names are rejected before execution.

| Control | Effect |
|---|---|
| `conceptual_guidance` | Adds source-scope and matching-path guidance. |
| `conceptual_cache` | Enables Conceptual caching and separates stable evidence from repair feedback. |
| `early_finding_validation` | Validates finding placeholder/binding structure inside the SDK correction loop. |
| `small_analysis_previews` | Defaults analysis previews to 20 rows; explicit limits up to 500 remain supported. |
| `offload_analysis` | Uses the Strands context offloader for large analysis query outputs. |
| `offload_finding` | Uses the same SDK offloader for large finding inspection outputs. |

Analysis, dbt, finding and plotting use automatic prompt caching for compatible Claude models; Conceptual caching is controlled above. Offloading retains full tool outputs in the run's `context/` directory and permits retrieval. These controls preserve full accepted results and evidence. Their configuration is captured per run for comparisons.

Results and Technical Details display reported tokens, cache usage and estimated USD cost. Prices are maintained in [src/usage.py](src/usage.py); unknown rates are unavailable, and a configured cost guard rejects unpriced models. Adding a model ID does not establish Bedrock availability or account permission.

The local experiment ledger is stored under the configured runs root in `experiments/`, with run-level and per-agent CSVs. To refresh it for the default local folder:

```sh
uv run --frozen python -m src.experiments runs
```

## Development and validation

```sh
uv run --frozen --extra dev pytest -q
uv lock --check
```

Ordinary tests do not invoke Bedrock. Container smoke checks and CloudFormation lint commands are documented in [infra/README.md](infra/README.md).

The main code boundaries are:

| File | Responsibility |
|---|---|
| [src/config.py](src/config.py) | Immutable settings and existing environment-variable defaults |
| [src/workspace.py](src/workspace.py) | Owned workspace paths and uploaded inputs |
| [src/run.py](src/run.py) | Shared record types, event emission and atomic result persistence |
| [src/execution.py](src/execution.py) | Admission, worker supervision and model-call budgets |
| [src/workflow.py](src/workflow.py) | Explicit workflow stages and their individual repair loops |
| [src/agents.py](src/agents.py) / [src/agent_runtime.py](src/agent_runtime.py) | Domain prompts and tools / Strands execution, caching and streaming |
| [src/core/contracts.py](src/core/contracts.py) | Agent output schemas and validation used by SDK correction loops |
| [src/modelling_validation.py](src/modelling_validation.py) / [src/sql_policy.py](src/sql_policy.py) | Evidence-dependent model checks / shared SQL and generated dbt policy |
| [src/analysis.py](src/analysis.py) / [src/visualisation.py](src/visualisation.py) | Accepted results / supported visual encodings and rendering |
| [src/ui/](src/ui/) | Streamlit composition and presentation, including saved-run replay |

Run state remains a dictionary on disk. `RunRecord` owns saving and events without changing existing JSON keys, status values or artifacts. Stateless policy checks remain functions, and repair loops stay with their stages.

[src/telemetry.py](src/telemetry.py) adds workflow and stage/attempt spans around native Strands tracing, with run and model-call correlation. Without a configured OpenTelemetry provider, tracing is a no-op. No cloud exporter or Langfuse credentials are configured by the application. A future exporter should initialise once in the worker process and use its bounded flush on normal exit; forced termination can lose final spans. Saved-dashboard mode starts no workflow traces. Cloud export and prompt/tool-content capture require a separate configuration step.

Analysis executes against validated dbt models; findings cite registered evidence from accepted results. Plotting reads accepted results and does not choose a new business aggregation. Successful execution and passing dbt tests do not establish business correctness: review the assumptions, methodology and data-quality warnings for each run.
