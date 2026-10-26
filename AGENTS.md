# Mandatory startup reading

**Your first action in every new session MUST be to open and read [application_flow.md](application_flow.md) in full, before your first response or any other investigation.** This applies to questions, overviews, and acknowledgements as well as implementation tasks.

Read sequentially in manageable sections. If tool output is truncated, continue reading until the entire file has been received. Knowing the file exists, reading this summary, searching headings, or reading excerpts does not satisfy this requirement. A request for a concise answer does not reduce the required reading. If the file cannot be read, report that explicitly before proceeding.

After the required startup reading, do only what the user asks for now. Do not treat a request to acknowledge future work as permission to investigate or act. Wait for the user's brief and go-ahead.

# ModeLLM

ModeLLM is a Streamlit application that turns related uploaded datasets and a natural-language question into a dimensional data model and visual dashboard. It uses Strands agents through Amazon Bedrock, dbt for model construction and testing, DuckDB for data storage and analysis, and Plotly for visualisation.

Each run follows this path:

`Uploaded datasets → raw DuckDB tables → source profiling → conceptual model → dbt build and validation → accepted analytical results → Plotly charts → dashboard`

## Components

| Component | Location | Responsibility and dependencies |
| --- | --- | --- |
| Interface | `app.py`, `src/ui/` | Collects uploads and questions, submits runs through `RunManager`, and displays persisted run records and chart artifacts. |
| Execution | `src/execution.py`, `src/worker.py` | Starts and supervises the workflow process. Uses `src/config.py` for settings and budgets, and `src/workspace.py` for run paths and inputs. |
| Workflow | `src/workflow.py` | Owns stage order, acceptance, bounded repairs, and failure propagation. Coordinates the components below. |
| Agents | `src/agents.py`, `src/agent_runtime.py`, `src/core/prompt/` | Assembles stage context, instructions, tools, and output schemas. The runtime handles Strands/Bedrock calls, streaming, caching, and invocation limits. |
| Sources and models | `src/data.py`, `src/dbt.py`, `src/modelling_validation.py` | Loads and profiles sources, writes generated dbt projects, and checks built models against the accepted conceptual plan. Uses DuckDB through `src/database.py` and SQL restrictions in `src/sql_policy.py`. |
| Analysis | `src/analysis.py`, `src/tools/analysis_tools.py` | Gives Analysis read-only access to validated models. Executes final proposals transactionally and registers accepted result tables and profiles. |
| Plotting | `src/visualisation.py`, `src/tools/result_tools.py` | Selects representations from accepted results, validates `PlotSpec`, and renders pandas DataFrames through Plotly Express. Plotting tools inspect the accepted result. |
| Run records | `src/run.py`, `src/workspace.py`, `src/storage.py` | Defines run state, persists events and results, owns artifact paths, and manages retention. The UI and saved-run loader consume these artifacts. |

## Shared definitions

The main definitions that changes must stay consistent with are:

- `src/core/contracts.py`: Pydantic schemas shared by agent outputs and application acceptance checks.
- `src/core/prompt/`: stage instructions; `src/agents.py` determines the actual context and tools delivered alongside them.
- `src/workflow.py`: executed stage sequence and repair boundaries.
- `src/run.py`: persisted run structure consumed by the interface and saved-run replay.
- [application_flow.md](application_flow.md): authoritative living map of Pydantic contracts, prompt expectations, Python signatures, validation, data flow, and consumers.

## Authoritative living application map

Use [application_flow.md](application_flow.md) as the authoritative reference for application structure, contracts, prompt expectations, and execution flow.

**This is a living map and MUST be maintained.** Update it in the same change whenever substantial application changes affect its documented contracts, prompts, function signatures, dependencies, data flow, validation, or persistence. If the map and source disagree, explicitly identify and reconcile the discrepancy; do not silently leave the map inaccurate.

## Analysis and plotting boundary

Analysis owns business calculations, populations, and result grain. Plotting chooses visual encodings from accepted results. SQL execution, schema validation, and successful rendering establish mechanical validity; they do not establish analytical correctness.

The findings stage remains in the code but is currently disabled in `src/workflow.py`.

## Analysis and Plotting development harness

Use [harness/README.md](harness/README.md) for selected-stage development without the UI. The harness supports Analysis alone, Plotting against a fixed accepted result, and Analysis through acceptance and Plotting. Import/select complete post-dbt bundles and explicit request scenarios; new datasets require no domain adapter, expected answer or recorded mock.

An external coding agent can use the documented build → test → inspect → revise protocol. Preserve input/code identities, iteration lineage, baseline/control comparisons and unresolved semantic judgments. Execution, valid schemas and rendered figures do not establish analytical correctness. Recorded tool mocks answer only their exact recorded requests; scripted responses establish wiring, not tuned model quality.

Keep frozen fixtures and experimental outputs under `harness/`, separate from saved source artifacts. Never edit original saved runs. Implementing or using offline tooling does not authorize billed model calls: obtain explicit approval for a fixed evaluation plan or bounded adaptive experiment policy before live dispatch. Honor its scope and aggregate limits.

The approved design and source-level integration reference are [harness/DESIGN.md](harness/DESIGN.md) and [harness/IMPLEMENTATION_SPEC.md](harness/IMPLEMENTATION_SPEC.md). Harness tests are under `harness/tests/` and require explicit collection; see the README's verification commands.
