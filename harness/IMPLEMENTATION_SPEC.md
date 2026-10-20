# Source-checked implementation specification

Status: approved implementation reference, 2026-10-19. Read with [DESIGN.md](DESIGN.md) and the executable [README](README.md). The user subsequently approved implementation of this bounded plan, including external-agent operation. Billed evaluation remains separately unauthorized.

Updated 2026-10-20 after fresh review: scenario checks retain result-scoped observations and failure-preserving aggregates; duplicate legacy check IDs are rejected by reporting. Coordinate comparison keys use result/typed-selection fingerprints, independent of trial IDs. Gates require complete observed parent/baseline scenario/repetition coverage and all corresponding comparisons. Named cases freeze a typed `CaseEvaluation` in `TrialPlan` with explicit evidence/acceptance/plotting/analytical scope. Evidence-only cases never fall back to SQL acceptance. The mixed-unit case replays both variants and evaluates their actual figure axes and declared measure mappings using recorded unit evidence. Missing or ambiguous assessments stay unobserved or require review. These harness changes do not alter production stage behavior.

The pre-implementation audit checked `2ea2af7217063b56f62bca2a0f6753263429ce1a`; installed Strands version is 1.59.0. Source observations below describe that baseline and were inspected, not inferred from earlier specifications. Implementation validation uses offline tests/replays and the walkthrough described in README. No billed provider calls were run.

## 1. Code audit and consequences

| Existing code | Observed behavior | Required implementation response |
| --- | --- | --- |
| `src/agents.py:93`, `analyse` | Accepts summaries as an argument, reads settings from `current_settings`, creates real restricted model tools and calls the locally imported `invoke` | Call this function; pass frozen summaries/settings/controls. Patch `src.agents.invoke`, not just `src.agent_runtime.invoke`, to intercept its invocation. |
| `src/workflow.py:376`, `_accept_analysis` | Recomputes summaries through its imported `summarize_models`; permits one repair on `AnalysisAcceptanceError`; the analysis-count check is outside that exception type | Install a worker-scoped `workflow.summarize_models` adapter returning a deep copy of the fixture summaries after verifying the database/model names. Preserve the repair boundary and count-limit behavior. Capture every returned proposal before later checks can fail. |
| `src/analysis.py:45`, `accept_analyses` | Creates all accepted tables in one transaction; calls its module-global `validate_result_semantics` inside the transaction; materializes full results and profiles | Reuse unchanged. Metadata-only acceptance extensions can wrap that validator inside the transaction. Full-result report checks run after acceptance and do not silently become acceptance/repair checks. |
| `src/analysis.py:148`, `summarize_models` | Uses three rows without ordering, converted to strings | Reconstruct once on an import staging copy, persist the exact serialized summary and reuse it. Do not regenerate a preview per trial. |
| `src/workflow.py:551`, `_render_visualisations` | Loads full frames, expands selectors, handles empty results separately, catches only local limit/PlotValidationError, and writes files outside that catch | Reuse for complete scheduled scopes. Do not broaden the catch. Capture full schedule/associations in a separate harness record. |
| `src/workflow.py:613`, successful `plot_attempts` | Contains title/status/plot, without a result or selection ID; `plot_specs` is compressed by skips | Instrument calls and schedule directly. Never infer result identity from `plot_specs` position, title, or the accepted-attempt list position. |
| `src/agents.py:193`, `plot` | Builds result tools; captures full frame in a scoped schema; supplies full-result profile and exact selection; hardcodes four SDK turns | Wrap its invocation arguments for context/schema/turn variants. Retain full-result profiles in the baseline. Any selected-profile context is a declared variant. |
| `src/visualisation.py:183`, `accepted_result_plot_schema` | Builds a fresh subclass with a renderer validator and stable field schema | Extend the supplied scoped class. Replacing it with plain PlottingPlan would discard the correction mechanism and is forbidden in compatible baseline variants. |
| `src/visualisation.py:202`, `validate_plot_candidate` | Calls a global renderer and translates four ordinary exception types into PlotValidationError | An experimental render/validation wrapper must be installed in this module and at the workflow's imported binding when applicable; otherwise SDK and final validation can disagree. |
| `src/agent_runtime.py:44,227`, `_invoke` / `invoke` | Internal provider construction; Anthropic-stage cache gate; explicit prefix checkpoint; optional CacheConfig; no cache-off argument | Add the single optional keyword specified in §2. No new provider/runtime implementation. |
| `src/agent_runtime.py:187` | Uses a coarse exception-text category; some construction failures occur before `model_call` is emitted; missing cache metrics can become zeros in convenience events | Capture structured exceptions and raw usage presence in harness adapters. Preserve production diagnostics as evidence rather than treating them as authoritative classification. |
| `src/execution.py:25`, `RunBudget` | Bounds internal model cycles within one settings context; estimated cost updates after responses; failed usage may become unknown | Retain it per trial. Carry the experiment's remaining allowances into each serialized worker. Do not mistake its per-trial state for an aggregate experiment limit. |
| `src/usage.py:54`, `summarize_usage` | Deduplicates response records against invocation summaries; `model_calls` counts started agent invocations | Reuse for estimated usage/cost, but label its count as agent invocations. Separately record RunBudget calls, SDK cycles and observed provider dispatch/retry counts. |
| `src/database.py:10`, `connection_options` | Creates `spill/` next to the database even for a read-only connection | Run app database helpers only against harness staging/trial copies. |
| `src/experiments.py:39`, `provenance` | Hashes src/app/lock/config, not harness source; dirty flag omits untracked files | Reuse available provenance fields, then add a complete selected code/config snapshot and harness/check hashes. Do not rely on this fingerprint alone. |
| `tests/local_model.py`, `LocalModel` | Scripts one tool submission per model response through the actual SDK, records messages, supplies synthetic usage | Reuse this pattern in a harness-owned transcript adapter supporting multiple invocations and bounded tool exchanges. Do not make the CLI depend on pytest's `local_model` import path. |
| `pyproject.toml` | pytest default collection includes `tests/`; wheel packaging includes only `src` | Run harness from the repo with `python -m harness`; explicitly include `harness/tests` in validation commands. No dependency/packaging/CI change is needed for this first version. |

## 2. Changes to existing application files

### `src/agent_runtime.py` — the only required application-code edit

Add a keyword-only parameter to both `invoke` and `_invoke`:

```python
cache_policy: Literal["default", "disabled"] = "default"
```

Import `Literal`. Validate the value at runtime, since the annotation alone does not reject arbitrary strings. `invoke` forwards it. `default` retains the current stage/model compatibility gate exactly. `disabled` makes the effective `cache_enabled` false before both checkpoint insertion and provider construction; omit `CacheConfig` and application-inserted prompt checkpoints. The harness's payload transforms must leave checkpoint placement to the runtime; reject an explicit checkpoint in an incoming disabled-policy prompt rather than silently sending it. Do not mutate the caller's prompt list.

Add the requested policy to `model_call` metadata alongside the existing effective `prompt_cache.enabled` and TTL fields. Keep runtime events, model routing, temperature/reasoning branches, budget hooks, plugins, tool choice, SDK correction, provider retries and exception propagation otherwise unchanged. Only the harness invocation wrapper supplies `disabled`; existing application callers keep their current signatures/call sites and behavior. This controls application/SDK explicit caching; it is not a promise to disable provider-internal caching for every possible model.

No ReleaseSettings field, environment toggle, contract version bump, new workflow stage or RunState field is required. No default Analysis/Plotting prompt or validator is changed while implementing the harness. Candidate fixes tested later remain separate experiments.

### `AGENTS.md`

Add a concise development-harness section linking `harness/README.md`, DESIGN and this spec. Explain the three stage modes, generic post-dbt bundle selection, immutable artifacts, external-agent loop, separate semantic verdicts, and explicit approval for live evaluations. State that saved runs must not be edited. Preserve mandatory startup reading and the application boundary instructions.

### `application_flow.md`

At implementation time, update both runtime signatures and caching/control description for `cache_policy`. Add a development execution-mode row pointing to the harness's own documentation, explaining reuse of private stage helpers without ingestion/dbt/UI or ordinary run cleanup. Record where harness sidecars, stage adapters and loop policies live; do not enumerate every harness helper in the production signature index. Keep the production flow and disabled Findings behavior accurate.

Existing `src/agents.py`, `workflow.py`, `analysis.py`, `visualisation.py`, `config.py`, `execution.py`, tools and usage need no harness-specific edits. The scoped adapters below account for their current limitations. Tests added for the new runtime keyword live under `harness/tests`; existing tests remain regression checks. Documentation and implementation changes outside these declared files require a stated reason and scope revision.

## 3. Harness modules and data contracts

Create `harness/__init__.py`, `__main__.py`, `contracts.py`, `fixtures.py`, `runner.py`, `adapters.py`, `replay.py`, `evaluation.py`, `reporting.py`, `loop.py`, README, local tests and `.gitignore`. Store optional cases and named variants under their own directories. Ignore `fixtures/`, `outputs/`, Python caches and local spill/context data. Do not copy AWS credentials or unrelated untracked files into provenance snapshots.

Use Pydantic with `extra="forbid"`, explicit format versions and validated links for the following harness-owned records. Use the application's actual models as nested types where compatible. No duplicated AnalyticalPlan or PlottingPlan schema definitions:

| Record | Required information |
| --- | --- |
| `ArtifactRef` | Relative path, content hash, media/format version, recorded/reconstructed/reported/experimental classification, source pointer and derivation where applicable |
| `UpstreamFixture` | Immutable ID/hash, database ref, actual declared model names/schemas, ConceptualPlan, frozen model summaries, original request, explicit settings/control provenance, original run/input identities and validation report |
| `Scenario` | ID/hash, upstream fixture hash, request ref/hash, parent/reason for a changed request, optional versioned checks/rubric; an empty optional case pack is valid |
| `PlottingFixture` | Scenario hash, AnalysisSpec, full AcceptedAnalysisResult, table/data ref, historical versus new acceptance identity, frozen selected scope/filters/title and profiles' provenance |
| `Variant` | ID/hash, baseline, hypothesis, source/config refs and intended diff; stage prompt/context/schema factories, metadata acceptance validator, plotting validator and runtime overrides |
| `TrialPlan` | Mode/backend, scenario/Plotting fixture/input lock, variant hash, actual code snapshot, settings/controls, selected views, check/rubric versions, repetition, finite resource limits and optional live authorization ref |
| `TrialResult` | Execution state, typed failure/stage/scope, artifacts, invocation/view associations, usage and its availability, check results and input/code integrity outcome |
| `CheckResult` | ID/version/dimension, result/view/obligation scope, status, evidence refs, expected/observed where meaningful, explanation and assessment method/source; preserve the DESIGN status vocabulary |
| `ExperimentPolicy` | Problem refs, baseline identity, fixed scenarios/scopes/controls, fixed check/gate versions, permitted candidate edit surfaces/models/backends, finite iteration/trial/call/cost/time limits, repetitions, retry allowance, no-progress rule and required review categories |
| `Iteration` | Parent/baseline links, hypothesis and boundary, source/config snapshot/diff, offline check artifacts, trial/comparison/review IDs, decision and cumulative usage |
| `ReviewRecord` | Author kind, subject/check IDs, decision/rationale/evidence, timestamp, superseded review ID; agent assessment cannot impersonate required human review |
| `AuthorizationRecord` | Exact approved fixed plan or policy hash, user approval reference/text, backend/scope/models/limits, validity/revocation if supplied; generated plans alone confer no authorization |

Use canonical JSON for manifest identity and preserve original text bytes as artifact refs. Store types explicitly for non-JSON scalars rather than allowing `default=str` to erase distinctions such as null versus string or date versus text. Keep trial-unique IDs/timestamps separate from input-comparison fingerprints.

### `fixtures.py`

Provide `import_run`, `import_bundle`, `verify_fixture`, `create_scenario`, `create_plotting_fixture` and `clone_trial_database`, plus catalog/list/unregister helpers. Import-run extracts using actual record keys and inventory pointers; import-bundle discovers schemas from supplied tables and checks them against the supplied plan. No dataset-specific module is selected based on table/column names.

Copy the source database into a staging area before calling application helpers. Verify source hashes, no active/uncheckpointed WAL and no concurrent source change; require a stable exported snapshot when necessary. On the staging copy, reuse `schemas`, `validate_built_models`, `summarize_models` and application contracts. A failed current compatibility check is evidence: retain the import diagnostic, distinguish historical validation from current compatibility, and do not silently call that snapshot currently valid.

Freeze schemas, summaries, settings and once-reconstructed accepted profiles with hashes. `AcceptedAnalysisResult.profile` requires a one-time SUMMARIZE reconstruction when only saved `analysis_results` is available; label it reconstructed and keep the old acceptance ID as provenance rather than asserting identical historical metadata. Never substitute a fresh execution of historical SQL for a preserved accepted table without declaring it a different artifact.

For Analysis/pipeline, trial copies expose only the selected upstream main tables to tools; remove saved accepted results from the active execution copy. Keep raw/source evidence outside the active tool scope. Plotting copies preserve the exact selected accepted table and metadata. All helpers use the clone, not original source paths. Catalog removal unregisters local fixtures; referenced output/input-lock snapshots remain intact and source runs are never deleted.

### `runner.py` and `adapters.py`

Suggested interfaces are `prepare_trial(...) -> TrialPlan`, `execute_trial(plan) -> TrialResult`, `run_worker(plan_path)`, and `stage_adapters(plan, record)` as a context manager. The prepared plan includes a snapshot of src/harness executable code, required local resources, relevant tests, pyproject/lock and variant config. Run the worker from that snapshot using the installed interpreter; record installed versions and recheck they match the plan. Exclude fixture/output trees, credentials and unrelated files. Code changes made after preparation cannot change an already prepared trial. Evaluator/gate code versions stay fixed across a comparison.

Parent process: validate plan, input/code hashes and scope, reserve a unique attempt, derive remaining experiment allowances, create a fresh RunWorkspace under harness outputs, launch one worker in a process group, enforce deadline/size, and finalize interrupted/terminated attempts with partial artifacts and usage uncertainty. Use existing `terminate_group` for termination. No application retention or ordinary experiment ledger is invoked. First version dispatch is serial per experiment; no distributed scheduler, queue or concurrent budget reconciliation is required.

Worker initialization: build a minimal RunState containing run ID/dir, running status, request, events, plan/model names/schemas and settings/controls. Use `RunRecord`, `use_settings` and `use_budget` explicitly; initialize the disabled Finding compatibility lists empty. On every exit save usage, internal call count and harness status in `finally`. Completion of selected stages is an execution outcome; the harness report owns the quality verdict. Do not reuse the full `_run` dashboard-completion message as a semantic verdict.

Execution routes:

- **Analysis:** call `agents.analyse` once with frozen summaries. Its SDK may correct structured submissions. Save the returned plan before subsequent diagnostics. Optional `accept_analyses` diagnostics run on a second copy without feeding errors back or replacing the proposal.
- **Pipeline:** under scoped adapters call `_accept_analysis`, save full returned AcceptedAnalysisResult objects immediately, then `_render_visualisations`. Return objects and their tables remain paired. The normal workflow one-repair behavior remains unchanged.
- **Plotting, full result/all selected results:** load the fixture pair(s) and call `_render_visualisations`, retaining a map from helper-local result indexes to fixture IDs. Capture empty-result scheduling without inventing a model call.
- **Plotting, one view:** compute the complete schedule first; select an exact entry by stable view identity. Restrict `workflow.representation_selections` to that entry for the target relation, retaining its original ordinal/selection/title in sidecars. Delegate the rest of rendering to the existing helper. No arbitrary new population filters or manual label substitutions.
- **Replay:** call existing acceptance/selection/candidate validators directly, with a trial copy wherever writes are possible. Save expected historical observations alongside current findings.

Install adaptations with `contextlib.ExitStack` and scoped `unittest.mock.patch.object`; restore them on every exit. They exist only in the isolated worker:

| Binding | Adapter behavior |
| --- | --- |
| `src.agents.invoke` | Capture arguments, apply named prompt/context/schema/limit changes, allocate an invocation ID, wrap the emitter to associate the runtime's generated call_id, call the saved original runtime invoke with `cache_policy` |
| `src.workflow.summarize_models` | Verify active clone/model list and return the fixture's frozen summary; record provenance without modifying it |
| `src.workflow.agents.analyse` / `.plot` | Capture the actual returned Analysis proposal before acceptance and Plotting spec/result/selection/title/frame identity; expose stage context to the invocation wrapper without adding it to model context by default |
| `src.analysis.validate_result_semantics` | Optional metadata acceptance extension calls the original first, then the candidate callable `(spec, result) -> None`; it executes in the existing acceptance transaction and ordinary failures enter the existing repair path |
| `src.visualisation.validate_plot_candidate` and `src.workflow.validate_plot_candidate` | Optional compatible wrapper calls the original checks/renderer and applies the same candidate validation at SDK submission and final guard; preserve ordinary local error conversion and unexpected-error propagation |
| `src.workflow.representation_selections` | Schedule capture and explicit one-view restriction only; delegate to the original scheduler and keep exact filters, null handling and original ordinals |
| `strands.models.BedrockModel` | Scripted local subclass or pass-through recording subclass for the live backend; retain compatibility with `automatic_tool_choice_model`, which subclasses this binding for Sonnet 5.5 |

Use a stage-specific factory `make_schema(base_schema, stage_context) -> type[BaseModel]`. For Analysis it must produce a compatible AnalyticalPlan subclass; for Plotting it extends the supplied accepted-result class. Persist the SDK's actual `StructuredOutputTool(candidate_schema).tool_spec`, not only `model_json_schema()`. Do not mutate shared Pydantic field metadata or global prompts in place. New fields require explicit acceptance/context consumers and no lossy conversion back to a base schema. Reject unsupported handoffs at preparation, or label an explicitly requested schema-only experiment as such and prevent end-to-end claims.

An acceptance extension here can inspect the existing metadata/profile while the transaction is open. It must not open another differently configured connection to that database, execute arbitrary SQL outside the application policy, invoke a model, or perform post-commit rejection masquerading as atomic acceptance. Full-table semantic checks belong to evaluation in this first version. A solution needing a new transactional full-table validator is a candidate application-code change, which can be tested under a declared source-edit experiment.

For rendering extensions patch both aliases above because the SDK closure and workflow final guard resolve different bindings. Return the same validated figure; never derive business measures, filter data or fill nulls as a hidden repair. The base checks and frame/selection identities remain protected.

Record a `view_id` from trial/result/typed-selection identity and a separate selection fingerprint for comparison. Populate a schedule record before calls, then dispatched/rejected/corrected/rendered/skipped/failed outcomes. Resolve call IDs from emitter metadata, not timestamps or title matching. Save independent figure artifact links after the production helper writes them. Treat file writes and broader budget/provider failures as separate failures, preserving earlier charts. Precomputed schedule size is a diagnostic; it must not introduce a different baseline chart-limit rule.

### Provider capture and offline isolation

The live recording subclass delegates streaming unchanged. Record SDK stream inputs, constructor settings, raw response usage/stop/failure evidence and provider-bound request parameters after SDK cache/structured-tool assembly. The installed Bedrock adapter formats a request then invokes `client.converse_stream` or `client.converse`; it can resend after certain validation responses. Thus `format_request` capture alone is insufficient to count actual dispatches.

Use client event callbacks for `before-parameter-build.bedrock-runtime.ConverseStream` and `...Converse` to persist the actual parameter dictionaries; use `before-send` callbacks only for attempt counts/timestamps, without recording signed headers or credentials. Keep records append-only and correlated with the owning invocation/provider attempt under a lock, as the installed adapter dispatches from a worker thread. Preserve raw cache-key presence: no metric means unknown, not a zero-read miss. Document request capture levels separately: application assembled input, SDK model input, and provider dispatch parameters. SDK/client retries are recorded separately from agent turns and workflow repairs.

For scripted runs patch the provider class before constructing any real client. The subclass accepts the constructor kwargs used by production, provides the minimal `client.meta.region_name` required by runtime event recording, and runs the real Strands Agent/structured-output mechanism using local responses. Fake usage remains labelled synthetic and excluded from measured cost/cache reports. Guard SDK/provider creation and network access in offline tests so an exhausted script or factory error cannot fall back to a real provider. Do not configure Langfuse export in offline mode. No live approval is required for these local checks.

### `replay.py`, `evaluation.py`, `reporting.py`

Replay extracts exact tool exchanges by call ID/toolUse ID/JSON pointer. Match tool mocks by fixture hash, tool name, canonical arguments and sequence where required. An unseen query is an unmatched-mock failure; the normal DB-backed tools answer novel queries on selected data. Historical tool/output class names may need an explicit recorded-to-current mapping; never silently rewrite a recorded invalid field or selector to make a replay pass.

Evaluation has two layers: generic metadata/schedule/render/usage integrity checks that require no dataset registry, and optional case-specific reference checks/reviews with declared evidence and scope. Store request coverage obligations with explicit provenance and links through proposal/result/view/encoding. Generic extraction of fields or SQL relations is evidence, not a proof of an arbitrary population/grain description. Unknown analytical correctness remains needs_review. Do not automatically assign a new arbitrary output to a regression case solely by title or result index; declare mappings in the check implementation or obtain review.

Reporting writes versioned JSON, concise Markdown, standalone local Plotly HTML, input/config/source diffs, and dimension-specific comparisons. Comparisons first validate invariants; compare both parent and original baseline. Plotting-only comparisons require identical accepted inputs, while pipeline comparisons permit different generated results bound to the same upstream input. Display recorded evidence versus fresh experiments distinctly. Persist human/agent review records separately without rewriting execution logs.

## 4. External-agent loop implementation

`loop.py` supplies protocol/state/gates, not model-driven candidate generation. Suggested functions:

```text
create_experiment(policy) -> experiment_id
create_iteration(experiment_id, parent_id, candidate) -> iteration_id
prepare_iteration(iteration_id) -> list[TrialPlan]
evaluate_iteration(iteration_id) -> comparison_ids
gate_iteration(iteration_id) -> GateResult
record_decision(iteration_id, decision, evidence_refs) -> decision_id
```

The external agent edits candidate sources/configuration and invokes these operations. `evaluate_iteration` runs only already prepared/authorized trials and declared offline checks; it never invents another candidate, increases a budget, enables a live backend, retries provider failures or rewrites the rubric. Trial execution does not import an optimizer or judge.

Create an immutable policy and baseline. Each candidate must name a parent, original baseline, hypothesis/target boundary, immutable input/check hashes, changed paths/config keys and their complete source snapshots. Candidate source changes under an experiment may include ordinary application fixes when allowed; dependencies or harness/evaluator/rubric changes are comparison-environment changes requiring a new experiment unless a separate, explicitly justified protocol covers them. No automatic git reset/checkout or application promotion. Existing user edits and historical artifacts stay intact.

Use append-only iteration events and atomic manifests under `outputs/experiments/<id>/`, with per-trial records/databases in `outputs/trials/`. Execution states are `prepared`, `running`, `finished`, `interrupted`; evaluation dispositions are the five in DESIGN. A crash after dispatch leaves an interrupted attempt whose usage may be unknown. Resume verifies completed artifacts and never resends an uncertain call automatically; an explicit retry gets a new attempt linked to the original.

Before every trial, validate code/input/check hashes, allowed changes, required offline results and live authorization scope. Dispatch serially. Count all attempts, regressions, provider failures and retries against maximum trials/time; use RunBudget internal calls for model-call allowances, not `usage.total.model_calls`. Compute remaining call/cost/time allowances from prior trials and pass their minimum with the trial limits through explicit ReleaseSettings. If the remaining allowance is non-positive, stop before constructing the worker/provider. The existing model hooks enforce remaining per-trial calls/cost/deadline. A separate finite overall deadline includes prepare/evaluate work. Unknown provider usage or pricing prevents further billed dispatch under a finite cost policy. Budget estimates remain subject to the existing possible one-response overshoot and provider retry uncertainty; report that limitation.

An adaptive approval references the policy hash and explicitly allows in-scope new candidates. Each resolved trial gets its own immutable hash without requiring new user approval within that policy. Authorization is an operator-recorded reference to actual user approval, not an agent's assertion that approval would be useful. No production approval service, signing infrastructure or extra UI flow is required. Expanding fixed fixtures/requests, models, allowed edits or ceilings falls outside that approval.

Gate order and evidence:

1. Verify invariants and required baseline/control availability; otherwise `incomparable`.
2. Report exhausted limits, unauthorized scope or repeated/no-progress candidates as `stopped` with a specific reason; do not dispatch dependent work.
3. Required failures/regressions produce `revise` while limits remain. A hypothesis may be refuted without establishing the cause of the original problem.
4. Required semantic or human judgments, unknown measurements material to the target, and unobserved required provider-failed work produce `needs_review`. The report still lists all other failures and unknowns; independent offline investigation can continue.
5. Only sufficient passing required checks and nonregressing protected controls produce `eligible_for_review`. This is scoped evidence for presenting a candidate, not automatic proof or deployment.

A no-progress rule compares declared target check statuses/metrics with parent/best prior iteration under fixed tolerances. Source/config hash repetition without new evidence is a duplicate candidate. For targets needing human assessment, progress remains undecided until that assessment; an agent cannot manufacture a numeric progress score. Preserve all facts even when a single disposition has precedence. Changes to check obligations, tolerances or human-review requirements cannot retroactively rescue a candidate.

CLI stdout is one versioned JSON result with IDs, disposition, reason codes, artifact refs, blocking checks and allowed next actions; diagnostics go to stderr. Do not suggest out-of-policy live commands. Exit codes: `0` command executed and recorded successfully (quality disposition may still be revise/needs_review); `2` invalid input/preflight mismatch; `3` a stage/provider/storage execution failure; `4` scope/authorization/resource stop before or during execution. `iteration gate --require-eligible` additionally returns `1` for any noneligible quality disposition, allowing shell/agent gates without conflating quality with command execution. Persist JSON/artifacts for nonzero outcomes.

## 5. Required guide and acceptance checks

README must explain setup from repository root, bundle import/switching/new questions, all stage modes, fixed invariants, known reconstruction limits, variant/factory authoring, mocked versus DB-backed tools, live approval and resource limits, interpreting unknowns, and a complete external-agent loop. Include JSON examples for a new unseen bundle, an offline experiment policy, a candidate, a gate result and a bounded live authorization envelope. Examples are clearly illustrative and never initiate model calls.

The walkthrough establishes a baseline defect, records a failed/regressing candidate, records a revised candidate, compares to original baseline and controls, and stops for a material semantic review. An offline schema/encoding correction may qualify under a mechanical target while whole analytical quality remains unassessed. Demonstrate retained rejected artifacts, finite stop conditions and resume behavior. No scripted example is described as proof of model prompt quality.

Add focused tests under `harness/tests`:

| Test area | Required proof |
| --- | --- |
| Fixtures and interchangeability | Import survey, commercial and a synthetic bundle with previously unused model/column names without adapters or required mocks/rubrics; swapping bundles updates all linked inputs; mismatched plan/table/schema and invalid selections fail; originals/hashes remain unchanged |
| Frozen context and baseline parity | Analysis and pipeline use identical frozen summaries; baseline prompts/tools/output schemas/settings/turns match production; repair feedback changes only its declared block; Plotting baseline gains no SQL/frame samples |
| Acceptance and pipeline wiring | Full profiles persist; pipeline Plotting reads newly accepted tables rather than history; expected acceptance error gets the existing one repair; analysis-count/provider/global failures keep current behavior; metadata validator rejection rolls back the entire transaction |
| Plotting identity/recovery | Real SDK sees nullable-size rejection then correction; missing field/filter tests hold; scoped and final validator variants agree; skips/empty/one-view schedules retain stable IDs and exact populations; provider/storage/global errors remain distinct |
| Cache/runtime seam | Default provider kwargs/checkpoint/schema/events unchanged; disabled removes auto config/checkpoints; caller prompt unchanged; invalid policy/explicit disabled checkpoint rejected; effective policy recorded for both stages |
| Provider capture/isolation | Client dispatch params include SDK-added tools/cache placement; internal resend/retry count is distinct from turns; absent raw cache metrics stay unknown; scripted constructor/script errors never create clients or network calls; synthetic usage never counts as measured cost |
| Comparison and evaluator limits | Accidental input/rubric/code drift is incomparable; exploratory alternatives do not require old SQL/titles; unreviewed semantics cannot pass; ratio references do not alone establish correct ratio calculation; controls keep partial-positive scope |
| Agentic protocol | Baseline → regressing child → revised child retains both parent/baseline comparisons; valid new candidates fit an adaptive policy without reapproval; out-of-scope candidates stop; gate/harness/rubric edits cannot weaken a frozen experiment; actual approval reference required for live dispatch |
| Aggregate limits and interruption | Sum internal calls across trials, include rejected/provider attempts, stop unknown cost and exhausted allowances, stop duplicate/no-progress candidates, preserve interrupted attempts, prevent silent resends, produce stable JSON/exit codes and retained partial reports |

Use installed Strands locally, existing validators and generated synthetic tables where relevant. Reuse existing test assertions/patterns rather than duplicate their entire suites. Required regression commands after implementation:

```sh
.venv/bin/python -m pytest harness/tests
.venv/bin/python -m pytest tests/test_analysis_boundary.py tests/test_analysis_results.py tests/test_execution_contracts.py tests/test_plot_context.py tests/test_plot_healing.py tests/test_context_cost.py tests/test_model_selection.py tests/test_sonnet55.py tests/test_release.py
```

The second command covers the runtime seam's model routing, budgets, cache/offload behavior and affected acceptance/render boundaries. These commands have not been executed for this specification-only revision. No new dependency, model upgrade, UI flow or automatic live test is required.

## 6. Delivery boundary

Implement in this order: (1) manifests, generic importer and source preservation; (2) selected-stage worker, baseline parity and cache seam; (3) optional case packs, replay/checks/reports; (4) loop records/gates/budgets and walkthrough; (5) regression verification and AGENTS/living-map updates. Source-checked details above resolve implementation seams without prescribing a dataset-specific analytical answer.

Deliver a working offline demonstration and machine-readable reports, with unresolved semantic findings visible. Pause before billed evaluations unless their concrete plan or adaptive policy has explicit approval. Testing solutions later can use the documented external-agent protocol; implementing this harness does not itself approve speculative production fixes or claim to resolve the reported analytical defects.
