# Analysis and Plotting development harness

Run selected ModeLLM stages from the repository root, without Streamlit, ingestion, Conceptual generation or dbt execution. Use any frozen post-dbt dataset bundle. The runner has no survey/customer/store/finance schema registry and does not require a case pack, expected answer or mock for new data.

Read [DESIGN.md](DESIGN.md) for the boundary and [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md) for integration details. The harness is development tooling, not a claim that the reported analytical defects have been fixed.

## Setup and storage

Use the application's environment (`uv sync --extra dev` if it has not already been installed):

```sh
.venv/bin/python -m harness --help
.venv/bin/python -m pytest harness/tests
```

The CLI prints one JSON result to stdout. `--root <directory>` must precede the subcommand; it defaults to `harness`. Data stays local:

```text
harness/
  fixtures/<id>/             frozen database, manifests, recorded comparison evidence
  scenarios/<id>.json        request + fixture + optional checks, sealed by hash
  outputs/trials/<id>/       prepared source/input snapshots and independent execution copy
    plan.json                immutable inputs, settings, variant and code identity
    code/                    source snapshot actually executed
    input/                   unchanged input snapshot
    modellm.duckdb            experimental database
    result.json              production stage events/state
    boundary.json            proposals, full accepted metadata, schedule and SDK exchanges
    trial.json               execution, checks, usage, raw cache availability
    report.md                human-readable assessment and limitations
    chart_*.json, *.html      generated figures and self-contained Plotly HTML
  outputs/experiments/       policies, iteration lineage, offline logs, gates and decisions
  outputs/comparisons/       parent/baseline comparisons
```

Fixtures, scenarios and experimental outputs are ignored by Git. Source runs are never opened for writes. Import checks for an active WAL/concurrent changes and freezes a copy before using app database helpers. Current model compatibility issues are retained as diagnostics and prevent normal stage dispatch; they are not silently reclassified as valid. Preparing a trial freezes its executable sources and selected inputs. Edits afterward require another prepared trial.

`list` displays registered bundles; `remove --fixture ID` unregisters one while retaining evidence and trial inputs. It never deletes original runs. Active fixtures cannot be unregistered.

## Bring data in and switch questions

A generic bundle is a JSON file with these keys:

- `database`: path relative to the bundle file (or absolute), containing materialized validated `main` tables.
- `plan`: the actual accepted conceptual plan object, matching those tables.
- `request`: original user request.
- `settings`: an explicit ReleaseSettings snapshot.
- `controls`: an explicit list of cost-experiment controls, including `[]` if disabled.
- `model_schemas`: optional recorded schemas, checked against the database if supplied.

Create it from available upstream artifacts; schemas and three-row model summaries are discovered/frozen automatically. For example, after placing a database and accepted plan under your local input folder:

```python
from pathlib import Path
from harness.io import read, write

write("harness/outputs/new-input/bundle.json", {
    "database": "validated.duckdb",
    "plan": read("harness/outputs/new-input/conceptual-plan.json"),
    "request": Path("harness/outputs/new-input/request.txt").read_text(),
    "settings": read("harness/examples/reconstructed-settings.json"),
    "controls": [],
}, exclusive=True)
```

The example settings file is explicitly reconstructed development configuration. It is not a recovered historical setting or a live evaluation authorization.

```sh
.venv/bin/python -m harness import-bundle --bundle harness/outputs/new-input/bundle.json --id dataset-a
.venv/bin/python -m harness verify --fixture dataset-a
.venv/bin/python -m harness list
.venv/bin/python -m harness scenario --fixture dataset-a --id question-b --request-file harness/outputs/question-b.txt --reason 'Intentional alternate user request'
```

Selecting another dataset switches the whole linked bundle. A new question gets its own scenario and comparison group; it does not silently inherit another question's expected outcomes. Original model snapshots may differ even when uploaded inputs were identical. Keep the chosen upstream bundle fixed within a comparison.

For saved runs, import the record/database together:

```sh
.venv/bin/python -m harness import-run --run runs/prod-local/261019_0424_a949816288eb --id survey-nullable --settings harness/examples/reconstructed-settings.json --controls ''
```

Omit `--settings`/`--controls` to use recorded values when present. If either is missing, import requires explicit reconstructed values. Prompts, proposals, tool messages and figures from a saved run are comparison evidence, not predetermined answers or extra context silently supplied to agents. Import copies available saved figures and the exact source record. Missing evidence is labelled; missing full initial prompts, profiles and dirty source diffs cannot be recovered by guessing.

## The three execution modes

| Mode | Behavior |
| --- | --- |
| `analysis` | One Analysis invocation with actual restricted model tools and SDK structured correction. Stops before workflow acceptance/repair and Plotting. `--diagnostic-acceptance` checks the proposal on another copy without sending feedback upstream. |
| `plotting` | Plotting against a fixed accepted relation, AnalysisSpec, profile and original request. Select `--result N` (repeatable), and optionally `--view N` for one exact scheduled selector value. |
| `pipeline` | Production Analysis acceptance/one-repair boundary, followed by production selection and Plotting on the newly accepted tables. Historical tables are not substituted. |

The default backend is `scripted`, which never constructs an AWS client. Scripts drive the real SDK/tool/validator wiring. A script consists of one entry per invocation, with `stage` equal to `analysis` or `plotting`, and `responses` containing model submissions. `$output` resolves to the actual current output tool name. For example:

```json
[
  {
    "stage": "plotting",
    "responses": [
      {"input": {"function": "scatter", "columns": {"x": "position", "y": "reading", "size": "reading"}, "filters": [], "options": {}}},
      {"input": {"function": "scatter", "columns": {"x": "position", "y": "reading"}, "filters": [], "options": {}}}
    ]
  }
]
```

Those illustrative field names must match the selected fixed result. Analysis submissions use the complete current AnalyticalPlan shape. A response may use `{"tool":"query_data","input":{"sql":"..."}}` to invoke the actual read-only DB tool, or a `tools` list to submit multiple calls in one response. New queries use the selected frozen data and do not require recorded responses.

```sh
.venv/bin/python -m harness prepare --fixture dataset-a --mode analysis --backend scripted --script harness/outputs/analysis-script.json --diagnostic-acceptance
.venv/bin/python -m harness run --plan harness/outputs/trials/TRIAL_ID/plan.json --backend scripted
```

Each prepared attempt runs once. A failure/interruption stays recorded; prepare a new trial to repeat it. The parent enforces time/size limits, and model hooks enforce internal-call/reported-cost limits. Provider usage after interruption can be unknown.

To freeze a newly produced analytical result for Plotting, import the generated run directory, which includes its new tables and Analysis plan. Alternatively register an explicit AnalysisSpec against an already preserved accepted relation:

```sh
.venv/bin/python -m harness register-result --fixture dataset-a --spec harness/outputs/analysis-spec.json --relation 'analysis."result_00"' --result-id result_00 --id fixed-result-a
.venv/bin/python -m harness prepare --fixture fixed-result-a --mode plotting --result 0 --view 0 --backend scripted --script harness/outputs/plot-script.json
```

Registration creates another immutable bundle, not an edit to the first. It reconstructs/fixes profiles once and validates declared references. `--request-file` with `--reason` records an intentional new request; its consistency/coverage still needs review. A generic changed-question scenario cannot silently change the original request of a fixed historical accepted result.

Empty analytical results use the production placeholder without a Plotting call. A skipped chart stays associated with its original result/selection. Successful execution or a placeholder does not imply coverage.

## Variants and comparison

`--variant baseline` preserves production context, tools and validators. A variant JSON can supply `variant_id`, hypothesis, stage-specific `system_prompts`, `cache_policy` (`default`/`disabled`), `context_layout` (`production`/`flat`), existing `controls`, `plotting_turns`, or explicit ReleaseSettings overrides.

Custom Python factories live under `harness/` and are referenced as `harness.module:callable`:

- `context_factory(prompt, stage_context)` returns the context blocks/string.
- `schema_factory(base_schema, stage_context)` extends the supplied Analysis/scoped Plotting class.
- `acceptance_validator(spec, result)` adds metadata/profile validation inside the existing transaction.
- `plotting_validator(frame, plot, result, selection, figure)` extends the same validator used in SDK correction and the final guard.

See [variants/examples.py](variants/examples.py). New contract fields need an explicit context consumer; unsupported handoffs fail instead of being silently discarded. Full-table semantic diagnostics run after acceptance and do not pretend to be transactional rejection. A candidate requiring new application capabilities can be tested as a declared source change under an experiment policy.

```sh
.venv/bin/python -m harness compare --runs BASELINE_TRIAL CANDIDATE_TRIAL
.venv/bin/python -m harness compare --runs BASELINE_TRIAL CANDIDATE_TRIAL --allowed-path 'src/core/prompt/*'
```

Comparisons validate input/mode/check/environment invariants, show source/config differences, and list regressions/improvements separately. Changed scripts are labelled wiring changes, not evidence of improved model behavior. Actual dispatch limits show remaining experiment headroom. Budget-truncated or provider-failed work supplies no semantic verdict for unanswered stages.

Coordinate checks use result/typed-selection identities shared across trials; trial-specific view IDs remain in the evidence. Scenario obligations produce one scoped check per applicable result plus an aggregate under the declared check ID. Any failed result fails that aggregate, and an obligation with no observed result is `not_observed`. Duplicate scenario IDs are rejected. Legacy trial files containing duplicate check IDs cannot be compared or reviewed: prepare a fresh trial rather than silently keeping their last observation.

## Recorded evidence and mocks

[cases/index.json](cases/index.json) indexes 16 survey and three selected commercial records, preserving inventory pointers/call IDs and observed failures/useful partial controls. These optional packs do not define the inputs supported by the harness. A replay uses the corresponding imported source fixture:

```sh
.venv/bin/python -m harness replay --case nullable-marker-size --fixture survey-nullable
.venv/bin/python -m harness extract-exchange --run runs/prod-local/261019_0555_cdf21e357159 --call-id 6e3d0dc00643 --out harness/outputs/exchange.json
```

`extract-exchange` retains messages and produces a `responses` list, which can be wrapped in a scripted invocation. Current validators generate fresh feedback. Historical responses can diverge from a new validation path; a reproduction is not automatically a success.

A scripted invocation may include `tool_mocks`: an ordered list of `tool`, exact `input`, and `response` objects. These are tied to the fixture hash. A new/unmatched query produces an explicit SDK error and failing mock-scope check, never an unrelated answer or provider fallback. Use the actual DB-backed tools for arbitrary queries.

Every named case declares an `evaluation_kind`, frozen in its trial plan and report:

| Kind | Executed assessment |
| --- | --- |
| `evidence` | Retains the description and reports `not_observed`; executes no acceptance or plotting replay. |
| `acceptance` | Re-executes historical Analysis acceptance with an explicit expected mechanical outcome. |
| `plotting` | Validates/renders explicitly indexed PlottingPlan proposals; analytical meaning still needs review. |
| `analytical` | Renders explicit proposals and runs the configured scoped analytical checks. |

`mixed-unit-variant-inconsistency` is an analytical case. It renders both recorded lithology variants and inspects their actual trace axes together with mapped y fields. Units come explicitly from the recorded measure descriptions, rather than guesses from column names. The case reports unlike units on one magnitude axis and differing measure sets across variants. Missing variants remain unobserved; ambiguous axes or unknown units need review. These bounded checks do not establish overall coverage or population correctness. Other evidence-only cases require an explicit executable assessment before they can supply a quality verdict; their names and expectation text are not passing criteria.

Recorded provider failures test classification. Scripted responses test execution/correction wiring. Live seeded correction tests recovery from an injected state. Only unseeded live evaluations can provide observations about a tuned prompt's choices, and small samples do not establish reliability.

## External-agent build → test → inspect → revise loop

The external coding agent proposes/edits candidates. The harness records lineage, executes declared checks, compares to parent and original baseline, and enforces scope/budgets. It does not generate another candidate or promote code automatically.

Start with the complete offline walkthrough:

```sh
.venv/bin/python -m harness.examples.offline_loop --root harness
```

It creates an unfamiliar synthetic dataset and two scenarios. The baseline renders a figure that omits the requested measure. A blanket-rejection candidate fails protected controls. A compatible visible-measure validator then rejects the incomplete proposal through the actual SDK and receives a scripted correction. The final gate stops at `needs_review` for analytical meaning/coverage. Rejected artifacts and parent/baseline comparisons remain available. This is a wiring demonstration, not a measured prompt improvement; no billed calls occur.

For your own experiment, create a policy JSON. The following example is complete and assumes `dataset-a` is already registered:

```json
{
  "experiment_id": "measure-handoff",
  "problem": "Investigate loss of requested measures between accepted results and encodings",
  "scenarios": ["dataset-a"],
  "mode": "pipeline",
  "backend": "scripted",
  "required_checks": ["input_integrity", "execution", "scheduled_views"],
  "protected_checks": ["scheduled_views"],
  "human_checks": ["analytical_meaning", "coverage"],
  "max_iterations": 4,
  "max_trials": 8,
  "max_model_calls": 40,
  "max_cost_usd": 1.0,
  "max_seconds": 1800,
  "max_no_progress": 2
}
```

```sh
.venv/bin/python -m harness experiment create --policy harness/outputs/policy.json
.venv/bin/python -m harness iteration create --experiment measure-handoff --candidate harness/outputs/baseline-candidate.json
.venv/bin/python -m harness iteration evaluate --experiment measure-handoff --iteration ITERATION_ID
.venv/bin/python -m harness iteration gate --experiment measure-handoff --iteration ITERATION_ID --require-eligible
```

A baseline candidate JSON supplies `hypothesis`, target `boundary`, and `script` (or `scripts_by_scenario`). A child also supplies a `variant` and names `--parent ITERATION_ID`. To test application source edits, make the intended edit, then create a child: its complete source snapshot and diff are frozen, subject to the policy's `allowed_paths`. Default paths cover prompts/contracts; explicitly declare other source/factory paths before starting when needed. Gate/evaluator/rubric changes require a new experiment. Input/request changes also form a separate experiment/comparison group.

The loop runs the declared offline tests before stage trials. Default tests cover acceptance and Plotting; `offline_tests: []` explicitly substitutes compilation only and must not be described as passing application tests. Each iteration can be resumed to inspect existing results, but uncertain/dispatched attempts are never resent automatically. An explicit bounded provider retry references `retry_of` and consumes the policy's retry/trial/time/usage allowances.

Gate dispositions:

| Disposition | Action |
| --- | --- |
| `revise` | Required failure/regression; propose an in-scope child while limits remain |
| `needs_review` | Required semantic evidence/human judgment is missing; stop dependent acceptance/live work, continue independent offline investigation |
| `incomparable` | Repair accidental input/check/code differences or create a separate experiment |
| `stopped` | Limits, duplicate candidate, unknown live usage, retry allowance or no-progress rule prevent continuation |
| `eligible_for_review` | Required checks and controls passed within scope; present evidence/limitations, without automatic production promotion |

Required/protected check IDs and human judgments are explicit policy choices. Different valid exploratory answers are allowed. Avoid replacing a request-specific semantic obligation with a cheaper mechanical proxy. No cost/latency saving offsets a correctness/coverage regression. Lower-level passing checks leave general semantics unreviewed.

Child eligibility requires observed baseline and parent trials for every policy scenario/repetition, plus a comparison for every matching pair. Missing baseline/control observations or comparisons yield `incomparable`. Resuming evaluation rebuilds comparisons using current review records while retaining older comparison artifacts.

Record reviews with `review --review-file FILE`; a ReviewRecord names review ID, author/kind, trial/check IDs, status, rationale, evidence, creation time and optional superseded review. Agent assessments cannot satisfy required human review. Reviews annotate executed mechanical facts rather than reversing them. Record iteration decisions with `iteration decision --text TEXT --evidence REF`.

## Live approval and measured usage

No billed evaluation is authorized by implementing the harness or creating a policy. Obtain explicit user approval for a fixed plan or a bounded adaptive policy. The approval must cover fixed inputs/scopes/controls, allowed edits/models, repetitions, maximum iterations/trials/internal calls/cost/time and provider retry allowance. In-scope adaptive candidates then need no individual reapproval; expanding that envelope does.

For an adaptive policy set `backend: "live"` and explicitly list permitted model IDs in `models`. After actual approval, record it with `authorize --subject POLICY_JSON --approval-file APPROVAL_TEXT --reference USER_APPROVAL_REFERENCE --out AUTHORIZATION_JSON`; then pass that file to `iteration evaluate --authorization AUTHORIZATION_JSON`. The manifest records actual user approval; neither the flag nor an agent-written statement grants it.

For a fixed trial, prepare with `--backend live --authorization /absolute/path/to/future-approval.json`, present the plan/hash, obtain approval, then record authorization against that plan and use `run --backend live`. Experiment trial dispatch remains scope/budget-checked even when invoked through the individual `run` command. Internal `_worker` execution is not a live-approval bypass.

Usage reports distinguish agent invocations, internal model calls, SDK cycles, provider dispatches/retries, input/output tokens and estimated cost/pricing availability. Costs are reported estimates, not hard billing caps; a response/retry can overshoot the remaining estimate. Unknown live usage prevents further billed dispatch. Offline token figures are synthetic and excluded from measured cost/cache results.

Cache configuration does not establish reuse. `trial.json.cache` and raw provider captures preserve read/write metric presence, values and TTL details where returned; absent metrics remain unknown. Prefix hashes and SDK/provider request bodies expose checkpoint placement and structured-tool changes. Distinguish reuse inside correction from reuse between invocations. A first observed call is not guaranteed cold. Interactive HTML supports local review; map tiles and actual browser appearance may still require separate inspection.

## Verification and limits

```sh
.venv/bin/python -m pytest harness/tests
.venv/bin/python -m pytest tests/test_analysis_boundary.py tests/test_analysis_results.py tests/test_execution_contracts.py tests/test_plot_context.py tests/test_plot_healing.py tests/test_context_cost.py tests/test_model_selection.py tests/test_sonnet55.py tests/test_release.py
```

The only required production behavior extension is the default-preserving runtime cache-policy keyword. The harness uses worker-local adapters around private stage interfaces and includes compatibility/parity tests; future application changes may require those adapters to be updated.

Pydantic descriptions do not enforce prose meaning. Ratio-reference validity does not prove a correct calculation or population. Point/trace counts and successful Plotly construction do not establish analytical or visual correctness. Generic reports deliberately retain semantic/coverage review gaps; optional case checks require independent evidence. Finding remains disabled. Renderer capability gaps, such as a requested interpolated surface that the renderer cannot construct, remain visible shortcomings and are not repaired by relabelling the figure.
