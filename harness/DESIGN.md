# Analysis and Plotting development harness — approved design

Status: implementation approved by the user and carried out on 2026-10-19, including explicit external-agent operation. See [README.md](README.md) for the executable CLI and walkthrough and [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md) for the source-checked integration boundary. The evidence section below records design-time observations. No billed evaluation is authorized or claimed by this implementation.

The harness will locate losses of analytical intent between the request, Analysis proposal, acceptance, scheduled selections, and figures. It will preserve the evidence at each boundary and separate mechanical checks from analytical assessment. It will not presume that a prompt, validator, agent, or renderer is the cause or the remedy.

Dataset interchangeability is a core requirement. The runner must work with a previously unseen ModeLLM post-dbt bundle without changing harness code, writing a dataset adapter, registering its column names, providing expected answers, or recording tool mocks. The existing agents/tools discover the selected data at runtime. Historical survey and commercial cases are optional regression packs, not the supported universe of inputs. Nothing in the generic runner assumes geological, customer, store, or finance data.

## Evidence available and its limits

Read: `application_flow.md`, the consolidation and fixture inventory under `todo/`, and the three historical documents identified there: the targeted workflow upgrade, Plotting healing/cache specification, and variant-field explanation. Their proposed remedies and reported verification are historical evidence, not requirements for this design or proof of present reliability.

The following checks were performed during design, without model calls:

- All 16 source `result.json` files and databases exist and match their inventory SHA-256 hashes.
- All 16 saved conceptual and Analysis plans parse with the current Pydantic contracts. Their saved model schemas match the actual `main` tables. This is compatibility evidence, not a fresh verification of their business meaning or historical dbt execution.
- Read-only queries confirm the 0622 selector case has 1,573 rows, 1,496 distinct non-null readings and one null group; the 0424 result has 1,036 rows and 10 null Cu values.
- Saved figure JSON confirms the 0724 zero-trace result, its differing three/four-measure lithology figures, and the 0731 elevation figure's 1,459 `histogram2dcontour` traces.
- The two `/tmp` verification artifacts referenced by the healing specification still exist. The cache report records 32,043 cache-read tokens, 5,052 writes and USD 0.022522 estimated cost. These remain historical observations with their original conditions, not measurements of a new variant.

The consolidation's account of an uncommitted application snapshot is now historical: current HEAD is `2ea2af7`, following `919d9c1` and `4a0f9c9`. The tracked application tree is currently clean. Do not use the older dirty-state descriptions as the identity of a new baseline. Historical dirty snapshots still cannot be reconstructed from a commit alone.

Every imported or generated artifact will have a provenance classification:

| Classification | Examples | What it establishes |
| --- | --- | --- |
| Recorded | Source record, database, saved proposal, tool exchange, figure, system prompt and output tool schema | What was preserved from that run |
| Reconstructed | Three-row model summaries, missing accepted profiles, assembled historical data prompt | A reproducible new input derived from preserved material; not the exact historical payload |
| Reported/unverified | An old spec's success claim, missing source diff, unsupported domain interpretation | A claim needing evidence; not a passing criterion |
| Experimental | A harness proposal, accepted table, figure, evaluation or review | What this specific trial produced under its recorded conditions |

Original uploads and complete initial provider requests are unavailable. The runner must not imply byte-for-byte historical replay. Reconstructed summaries and profiles will be computed once when freezing a fixture, saved with their method/version, and reused across variants. Frozen profile statistics may differ from historical ones. Do not silently recalculate them between trials.

The user confirmed there is no additional survey data dictionary and identified the commercial records by their request prefix. A dictionary is not a prerequisite for importing or running any dataset. Missing domain definitions are represented as uncertainty rather than supplied from assumed knowledge. Coordinate values in metres cannot be used as geographic degrees; a conversion still requires CRS evidence. No specific QC inclusion or duplicate policy is prescribed by a historical output.

Searching the provided commercial prompt prefix found 14 saved records sharing that request: five marked complete, two marked running, and seven failed. Those statuses are observations, not quality verdicts. Three concrete comparison sources were inspected:

| Run | Recorded outcome | Available evidence |
| --- | --- | --- |
| `261014_1347_28950d040703` | Complete, four analyses/four charts | Six main models, accepted results and successful final dbt compile/build/test after a compile repair |
| `261015_0004_9eff14907ba8` | Complete, four analyses/four charts | Six main models, accepted results and successful dbt compile/build/test; useful alternate model snapshot |
| `261014_2336_66160cbad89e` | Failed in Plotting, four analyses/no charts | Seven main models, accepted results, successful dbt compile/build/test and a recorded template error |

All three databases exist; their plans parse with current contracts and saved schemas match actual main tables. None has a full recorded release-settings snapshot, so explicit reconstructed settings are necessary. The request supplies review obligations around separate operational and finance facts, purchase-event grain, rate components, a 180-day window, customer populations, and presentation coverage. Its reference to “Level 2” churn does not itself supply a definition. Historical choices of a churn proxy remain choices to review, not a definition the harness assumes.

## Bounded first version

Build a local Python CLI, using the installed Pydantic, DuckDB, pandas, Plotly, Strands and pytest environment. An external coding agent can drive a documented build/test/revise loop through that CLI. The harness owns immutable trials, comparisons and loop limits; the external agent proposes and edits candidates. No additional optimization agent, web service, UI interaction, upstream model generation, Findings stage, or model judge is required. Human review handles semantic questions that the declared checks cannot establish.

Index the 16 survey runs, all 14 survey inventory cases, and the three selected commercial comparison sources above. Materialize database snapshots only for selected fixtures. A new dataset enters through a generic frozen post-dbt bundle: model tables, conceptual plan, settings and request. Discover and verify schemas and compute summaries during import rather than require hand-written schema configuration. A saved ModeLLM run is one convenient source of that bundle; a new prepared bundle is equally supported. Uploading raw source CSVs and running Conceptual/dbt is outside this first version, consistent with the requested initial boundary. Small synthetic post-dbt bundles may be added for specific edge cases and must be labelled synthetic.

The first implementation provides three development modes and two offline diagnostic paths:

| Mode | Fixed input | Executed behavior |
| --- | --- | --- |
| `analysis` | Request, conceptual plan, model tables, schemas, frozen summaries and settings | Call `agents.analyse`; save the proposal and SDK trajectory. Stop before workflow acceptance/repair and Plotting. Optional acceptance diagnostics use another disposable copy and do not send feedback to Analysis. |
| `plotting` | Request, AnalysisSpec, accepted table and metadata, exact selection/effective title | Run Plotting and the existing final render guard against that accepted result. One selected view or all its scheduled views can be requested; the chosen scope is recorded. |
| `pipeline` | Same post-dbt bundle as `analysis` | Reuse workflow Analysis acceptance/repair, then production scheduling and Plotting. Carry the newly accepted results forward; never substitute historical tables. |
| `replay` | Recorded proposal(s), fixed table(s) and current or named validators | Exercise acceptance, selection and rendering without a model. Report current treatment of recorded examples. |
| `scripted` backend | Bounded transcript/model submission sequence | Exercise real SDK tools, validation feedback, correction limits and continuation using local responses. Establish wiring only. |

The `analysis`, `plotting` and `pipeline` modes can use either the scripted backend or an explicitly authorized live backend. Offline commands cannot fall through to Bedrock. A missing scripted response or mock match is an explicit failure.

## Fixture identity and preservation

Use three linked identities rather than loose paths:

1. An **upstream snapshot** binds the particular database's validated model tables to its conceptual plan, model names/schemas, source provenance, frozen summaries and recorded settings. Identical uploads do not make two generated snapshots equivalent.
2. A **scenario** binds that snapshot to a user request and a versioned evaluation rubric. Preserve the original request; a new request creates a child scenario with an explicit reason. It does not silently reuse the previous request's historical answers or rubric.
3. A **Plotting fixture** additionally binds an AnalysisSpec, accepted relation, full accepted metadata, and scheduled selection/title to that scenario. Historical acceptance IDs are provenance; reconstructed or newly accepted artifacts get their own identities with links to the historical record.

Pydantic manifests validate these links and hashes. Required settings missing from an old run are declared assumptions supplied in an explicit profile; do not fill them from the current shell and call them historical settings. Validate actual table schemas, row counts and referenced semantics. Use existing built-model validators to report current compatibility, keeping their scope distinct from historical validation.

Import from source artifacts read-only into `harness/fixtures/`. Verify source hashes before/after import. Every trial works on a new database copy under `harness/outputs/`; even read helpers can create a `spill/` directory, so runtime helpers operate on the trial copy. Analysis and pipeline copies exclude historical accepted tables from the active stage inputs. Historical plans/figures stay in the comparison evidence, outside agent context. Preserve table types and duplicates; do not convert the input to a simplified CSV mock.

For comparisons, require identical scenario/input hashes, rubric version, execution mode, selected views and settings except for declared variant fields. Record table/frame ordering where it can affect lines or previews. Record full code/lock provenance, package versions, and relevant source snapshots/diffs, not just Git SHA. Unknown or accidental differences invalidate a controlled comparison and appear in its report. Intentional input or request changes form a separate comparison group.

Provide an importer for a supplied post-dbt bundle as well as saved-run import, plus list/select/remove commands for local fixture registrations. Removal never deletes original saved runs; outputs retain their input lock and provenance. Keep a fixture in use by an active trial protected from removal. Reject independently selected request/plan/schema/database paths whose manifest links disagree. A Plotting-only request change must be registered as a derived fixture and reviewed for consistency with the fixed accepted analysis.

Selecting another dataset switches the entire linked bundle. Selecting another question creates a scenario bound to that bundle. Users do not edit implementation constants or carry forward unrelated saved results. Creating or selecting a fixture never invokes a model.

## Reuse and limited integration

Call existing components rather than maintaining a second implementation of their behavior:

- `agents.analyse` / `agents.plot` for normal context assembly and tools.
- `workflow._accept_analysis` and `_render_visualisations` for pipeline acceptance/repair, scheduling, empty-result behavior and local recovery. Keep a compatibility check because these are private interfaces.
- `accept_analyses`, `analysis_statement`, `representation_selections`, `accepted_result_plot_schema` and `validate_plot_candidate` for offline diagnostics and targeted replay.
- `RunWorkspace`, `RunRecord`, `use_settings`, `use_budget` and usage aggregation for local records, settings and budget accounting.

Do not call `workflow.run` or `_run`: they perform upstream work, cleanup and experiment-ledger operations beyond the selected stages. A harness worker owns stage initialization/finalization, local parent deadline, usage capture even on failure, and its own output directories. It does not run application retention against the saved evidence.

One worker subprocess per trial allows scoped Python adapters at the existing invocation boundary without changing global behavior in the app or concurrent trials. A wrapper around `agents.invoke` records and, for a named variant, transforms invocation arguments before delegating to the production runtime. A wrapper at the Plotting call records result/selection/title associations for every attempt, including successful calls whose production attempt records currently lack those keys. A scoped `workflow.summarize_models` adapter supplies the frozen summaries in pipeline mode: the production helper otherwise recomputes them. The exact bindings, schemas, worker steps and tests are specified in [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md).

One small application seam is proposed: an optional cache-policy override on `agent_runtime.invoke` and `_invoke`, defaulting to current behavior, permitting explicit cache disable for these stages. Current cost-experiment switches do not disable Analysis/Plotting caching. The override must govern both cache configuration and explicit checkpoints, and emit the effective policy. All experiment logic stays in `harness/`. Update `application_flow.md` for this signature/control, and document the harness in `AGENTS.md`. No other application behavior change is part of approval of this harness.

## Controlled variants

A Pydantic variant manifest names its baseline, hypothesis, intended changes and immutable inputs. Save resolved values and before/after diffs, plus source hashes for custom Python factories/validators. Unsupported combinations fail preparation rather than silently dropping fields or controls.

| Surface | First-version control |
| --- | --- |
| Prompts | Stage-specific system prompt file and field-description variants; preserve baseline text verbatim |
| Pydantic contract | Named factory producing a compatible AnalysisPlan or scoped PlotSpec subclass; schema descriptions, fields and validators are recorded |
| Validation | Named acceptance/render wrappers; distinguish report-only checks from checks that reject and return feedback |
| Context | Explicit payload transforms, catalogue form, additional declared context, stable/dynamic block layout; record complete resolved blocks and tool schemas |
| Context management | Existing small-preview/offload controls on/off; existing SDK behavior remains in use |
| Cache | Production automatic policy or explicit disabled policy; shared-prefix versus flattened layout; checkpoint positions recorded |
| Runtime | Explicit model IDs, region, output/call/time/cost limits and local Plotting turns; defaults resolved and frozen |

New contract fields do not automatically reach downstream consumers. A variant adding units or presentation obligations must specify how acceptance and Plotting context consume them; otherwise preparation reports an unsupported handoff. Retain the production scoped rendering validator when extending PlotSpec. Contract-shape changes needing broad application changes are reported as a separate proposal, rather than hidden inside a prompt experiment.

Contract/schema changes can invalidate a cache prefix. Record that as a condition, not a mysterious model cost regression. Baseline parity tests establish that the harness sends the same context, tools, schemas and limits as production for the same explicit inputs. Historical prompts/schemas can be compared or used in a labelled compatible variant, but selecting them does not recreate an unknown historical runtime.

## Evaluation and evidence trail

Keep execution outcome and evaluation outcome separate. A trial may finish with failed or unreviewed semantic checks; a provider failure can leave earlier Analysis checks valid and Plotting unassessed. No general success score is inferred from application `complete`, number of charts, nonempty traces, or matching an old response.

Each check has an ID/version, dimension, scope, status (`pass`, `fail`, `needs_review`, `not_applicable`, `not_observed`), evidence references, and an explanation of its limits. Reports display all dimensions separately:

| Dimension | Checks and review evidence |
| --- | --- |
| Definitions, populations, calculations, units, grain | Compare declared definitions with executed SQL, admitted rows and grouped results; key uniqueness, exclusions, numeric component identities and small independently computed references where justified. Unresolved domain policies require review. |
| Analysis and presentation coverage | A request-to-proposal-to-result-to-view-to-encoding coverage matrix. Record missing obligations and primary/supporting/hover-only treatment. The explicit three-chart request has stricter obligations than exploration. |
| Shape and selectors | Exact schema, annotated fields, actual row grain evidence, selector values including null, row counts per selection, expanded view count, population preservation, and cross-variant measure consistency. Numeric selectors are not rejected solely for being numeric. |
| Encoding, rendering, recovery | Existing validators plus figure/trace inspection, coordinate semantics, units on shared axes, actual aggregation, points represented where measurable, empty or skipped output, and rejected-to-corrected sequence. Save interactive HTML for human inspection using installed Plotly. |
| Provider/infrastructure | Structured provider exception code where available, timeouts, throttling, global budget, local turn limit, storage and unexpected application failures distinguished; unknown attribution remains unknown. Preserve original error. |
| Usage, cost, cache | Actual response/call usage, internal cycles, tool/correction counts, latency, cache reads/writes and uncached tokens; estimated cost with model/region/pricing basis and reported/partial/unavailable status. Scripted token counts cannot become measured model cost. |

The core checks derive their field/table references from the selected bundle and actual outputs. Run them without an optional regression pack, custom rubric, or mock. Unknown datasets can therefore run and produce useful diagnostics immediately, while semantic checks lacking sufficient evidence remain `needs_review`. Domain-specific assertions live only in optional case packs and cannot become universal validators by accident.

Use bounded case-specific checks where independent evidence supports them, not a generic SQL/prose truth engine. For a novel exploratory proposal, a reviewer can accept a defensible alternative and record its reasoning. Do not require fixed SQL, titles, analysis count, row counts from a different population, or a historical chart choice. Preserve row counts as historical observations; enforce them only when a case's frozen query/population makes them an appropriate assertion.

Reference calculations are independently written and reviewed, with tolerances, null and duplicate policy specified. Some result-equivalent queries coincide accidentally on one dataset; optional synthetic edge fixtures can expose denominator, duplicate, null and unit errors. They are separate frozen scenarios, not silent modifications inside a comparison.

Record where evidence first diverges: request versus proposal, proposal versus SQL, SQL versus accepted metadata, scheduled selection versus intended view, and PlotSpec versus figure. This helps investigate ownership without automatically declaring a root cause. Human judgments are stored as versioned review records, separate from execution logs; changes retain their history.

Seed an optional survey regression pack from all 14 inventory cases: excessive reading selector; useful analyte selector; absent field; nullable size; recorded recovery; questionable tier splitting; projected coordinates; empty traces; mixed units across variants; elevation contours; working campaign/sample charts; useful label shape in a provider-failed run; provider failures; methodology contradictions. Add an optional commercial pack for the user-identified request, retaining alternative analyses and the template-failure case. Finding arithmetic remains historical context because Finding is disabled. Passing the positive selector case does not approve its old QC policy; passing the nullable-size recovery does not approve its dashboard. Importing a new dataset requires neither pack.

The elevation case must report the current capability gap when the requested measured surface is unavailable. An honest limitation can pass a disclosure check while requested presentation coverage remains unmet. The harness does not add interpolation, coordinate conversion, pivots, or new renderer functions as a presumed fix.

## Recorded mocks and correction tests

Preserve inventory JSON pointers, call IDs and toolUse IDs with extracted exchanges. A tool-response mock is keyed by fixture identity, tool name and exact canonical arguments. No fuzzy SQL matching, default historical response, or fallback for an unseen query. New live Analysis queries execute the real restricted tools against the frozen models.

A small transcript adapter uses the installed Strands Model interface and the existing local-model test pattern. Script the recorded rejected proposal and following submission through the real SDK and current validator; record freshly produced feedback alongside the historical feedback. If current validation follows a different path, report the divergence. It need not consume a stale correction script successfully.

Separate labels for deterministic proposal replay, scripted SDK correction, live unseeded behavior, and live behavior seeded with a recorded mistake. The latter tests recovery from that injected state, not the probability of avoiding it. A replayed provider failure tests classification; it does not measure provider reliability.

## Runs, comparisons and cache measurement

Persist an input lock, resolved variant/diff, code provenance, exact assembled system/data context and tool/output schemas, model settings, all runtime events/messages, proposals/rejections, executed SQL, accepted metadata including full profiles, schedule, selections, figures, check results, reviews and usage. Correlate every view with scenario/result/concept/selection/call/attempt IDs, including skipped and failed calls. Preserve partial artifacts and diagnostic outputs on failure.

For a fixed live comparison, resolve the entire matrix first: fixtures, stages/views, variants, repetitions, models and ceilings. For an adaptive loop, approval instead covers a bounded experiment policy: fixed fixture/scenario/control IDs, stage/view scope, allowed candidate changes, models, repetitions, maximum iterations/trials, aggregate call/cost/time ceilings, and provider-failure retry allowance. Each new candidate is resolved to an immutable trial plan and checked against that approved policy before dispatch. Start with a smoke trial; continue only inside the authorized envelope. An explicit retry consumes the same limits and remains a separate recorded attempt. Report every repetition and provider failure, conditional semantic outcomes and sample size; a small sample does not establish reliability.

Cache checks record request ordering/timestamps and prefix/system/tool/schema hashes, checkpoint placement, provider usage and TTL details where supplied. Distinguish within-invocation correction reuse from reuse between separate views. A first request is only “first observed,” not guaranteed cold; the provider cache is shared outside this experiment. A warm attempt with zero reported reads is a measured miss; absent cache metrics are unknown. Arrange any cold/warm experiment explicitly without changing the semantic payload merely to obtain a different prefix.

Reuse application usage aggregation to avoid counting response usage and call summaries twice. Cost ceilings apply to reported estimates before subsequent calls, so they are not hard billing caps. Freeze the pricing basis and report unknown/partial usage rather than zero. Verify current provider/model pricing before an authorized live batch when cost comparison is material.

## Agentic operation: build, test, inspect and revise

Agentic operation is a required first-version workflow, not an implied consequence of having a CLI. `harness/README.md` will document it with a complete offline walkthrough. The harness does not select a fix by itself or launch another agent. The external coding agent follows this protocol:

1. Create an experiment policy with the problem/evidence references, baseline source identity, immutable scenario set, optional regression packs, frozen check/rubric versions, allowed edit surfaces, and finite limits. New datasets require no case pack or expected output. Offline execution is the default.
2. Establish the baseline under that exact policy. Keep historical output evidence separate from this newly measured baseline.
3. Create an iteration naming its parent and the original baseline. State the hypothesis, the boundary it targets and what evidence would support or refute it. Record the full candidate source/config snapshot and diff before running. Candidate code changes may target prompts, contracts, context or other application components when the experiment permits those paths; the harness itself introduces no such solution changes.
4. Run applicable offline tests/replays first. Prepare immutable stage trial plans; use model calls only when covered by explicit live authorization. Capture every selected regression control, not just the failing example.
5. Compare to both parent and original baseline. Read the dimension-level findings, coverage gaps, regressions, unknowns and boundary evidence. Report a hypothesis as supported, refuted or unresolved within this experiment; do not call a scripted correction a prompt-quality improvement.
6. Record the decision and evidence before creating another child iteration. A candidate eligible for review is an experimental artifact. Applying it to production or changing the application default is a separate action.

Iteration lineage includes `experiment_id`, `iteration_id`, `parent_iteration_id`, `baseline_iteration_id`, hypothesis, intended boundary, candidate diff/source identity, input lock/check versions, offline results, trial IDs, comparison IDs, review IDs, cumulative usage and decision. Prior artifacts are retained after a candidate is rejected. Changing an input, rubric, mandatory gate or baseline creates a new experiment/fork and cannot make a failed iteration pass retroactively.

The gate command returns structured findings and one deterministic disposition:

| Disposition | Required interpretation and next step |
| --- | --- |
| `eligible_for_review` | Required checks passed under the policy, controls did not regress and evidence is sufficient for those checks. The agent can present the candidate; this does not prove general correctness or promote code. |
| `revise` | A required check failed or a protected behavior regressed. The agent can propose a child candidate within the remaining scope/limits. |
| `needs_review` | A material definition, coverage judgment or other required assessment lacks sufficient evidence. Stop dependent acceptance or live work and request a focused judgment; independent offline investigation may continue. |
| `incomparable` | Inputs/checks/code differ outside declared changes, or a required baseline/control is missing. Repair the comparison or start a separately identified experiment. |
| `stopped` | Iteration/trial/call/cost/time limits, repeated non-improvement, scope limits or provider retry allowance were exhausted. Preserve the final reason and partial results. |

The policy names required checks, protected controls and material review requirements explicitly. The generic runner can execute unfamiliar data without those domain judgments; its report must leave them unassessed. Missing metrics, skipped required views and provider-failed work cannot satisfy a required gate. Quality regressions are assessed separately from cost and latency; lower cost cannot offset wrong calculations or missing requested content. An optional semantic check stays optional only if that choice was declared before the comparison.

Stop when any finite limit is reached, the same candidate is proposed again without new evidence, the configured number of consecutive iterations yields no progress against the declared target, or a candidate needs capabilities/evidence outside the authorized scope. Provider failures do not trigger prompt revision automatically. Explicit provider retries consume trial, time and usage allowances. If provider usage becomes unknown, stop further billed calls until the cost uncertainty is resolved or the user explicitly revises the authorization. Process interruption leaves an uncertain partial attempt; resuming never silently reruns it.

Live authorization is a recorded reference to the user's explicit approval of an experiment policy or fixed plan. A flag or agent-written manifest cannot establish that approval. A bounded adaptive policy allows new in-scope candidates without requesting approval for each iteration; changes to fixed inputs, models, permitted edits or ceilings need an updated authorization. Implementation approval does not authorize billed evaluations.

The first-version demonstration must include a baseline defect, one candidate that fails or regresses a control, a revised candidate and a retained comparison/decision trail. Use offline recorded/scripted outputs to establish loop mechanics, then show the `needs_review` stop for a semantic question the available evidence cannot settle. This demonstration is not a claim of live analytical improvement. Also demonstrate dataset switching without a domain-specific adapter or mocks for arbitrary queries.

## Proposed files and commands

All new harness code, local tests, case/variant configuration, imported assets and generated output live under `harness/`. Besides the small runtime seam, the only intended changes outside it are `AGENTS.md` and `application_flow.md`.

```text
harness/
  DESIGN.md                 this approved/proposed boundary
  IMPLEMENTATION_SPEC.md    source-checked changes, exact adapters and acceptance tests
  README.md                 installation, imports, offline/live use, interpretation
  __main__.py               CLI commands and exit behavior
  contracts.py              fixture, variant, trial and check manifests
  fixtures.py               import, verify, freeze and clone
  runner.py                 selected-stage worker and production adapters
  adapters.py               process-local context/schema/validation/runtime instrumentation
  loop.py                   iteration lineage, gates, authorization scope and aggregate limits
  replay.py                 recorded proposals, bounded mocks and scripted Model
  evaluation.py             case checks, coverage and review records
  reporting.py              JSON/Markdown comparisons and local Plotly HTML
  cases/                    versioned observations, rubrics and evidence pointers
  variants/                 baseline and explicit experiment configurations
  tests/                    offline isolation, parity and diagnostic tests
  fixtures/                 local frozen snapshots, ignored by Git
  outputs/                  append-only trial/comparison artifacts, ignored by Git
  .gitignore                local data/output rules
```

CLI shape (see README for complete arguments and examples):

```sh
.venv/bin/python -m harness import-run --run runs/prod-local/261019_0731_918ac1e26607 --id survey-three-charts
.venv/bin/python -m harness import-bundle --bundle <new-post-dbt-bundle> --id <new-dataset>
.venv/bin/python -m harness list
.venv/bin/python -m harness verify --fixture survey-three-charts
.venv/bin/python -m harness scenario --fixture <new-dataset> --request-file <question.txt> --id <new-question>
.venv/bin/python -m harness replay --case elevation-contour-misrepresentation --variant baseline
.venv/bin/python -m harness prepare --fixture survey-three-charts --mode plotting --result 1 --variant baseline
.venv/bin/python -m harness run --plan harness/outputs/<prepared-plan>/plan.json --backend scripted
.venv/bin/python -m harness compare --runs <trial-a> <trial-b>
.venv/bin/python -m harness review --run <trial-id> --review-file <review.json>
.venv/bin/python -m harness experiment create --policy <policy.json>
.venv/bin/python -m harness iteration create --experiment <experiment-id> --parent <iteration-id> --candidate <candidate.json>
.venv/bin/python -m harness iteration evaluate --iteration <iteration-id>
.venv/bin/python -m harness iteration gate --iteration <iteration-id>
```

Live execution uses a separately explicit `--backend live` command tied to a prepared plan and the approved plan/policy hash. No billed call will be made during implementation without the user's explicit authorization. Preparation, inspection, replay, reporting and tests remain model-free. CLI schemas, exit codes, proposed Python interfaces and exact worker patches are detailed in [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md).

## Delivery and acceptance of the harness

After design approval, implement fixture contracts/import/verification, the stage runner and controlled variants, then replay/evaluation/reporting, the external-agent loop protocol and the usage guide. Import representative fixtures and index the rest. Add the AGENTS entry and living-map update in that implementation change. The implementation checklist and verification commands are in [IMPLEMENTATION_SPEC.md](IMPLEMENTATION_SPEC.md).

Verify offline that source hashes remain unchanged; fixture mixing is rejected; profiles/summaries remain fixed; baseline context and controls match production; newly accepted pipeline tables reach Plotting; exact selections and IDs survive local skips; unmatched mocks fail; invalid nullable-size proposals get real SDK feedback; global/provider/storage failures remain distinct; and usage is not double counted. Demonstrate importing and switching an additional bundle with different model/column names without editing harness code or providing a mock/rubric; the offline scripted run establishes onboarding/wiring, not model analytical quality. Demonstrate both failures and partial positive controls from the survey and commercial packs, with remaining semantic checks visibly unreviewed rather than auto-passed. Run the applicable existing acceptance, plotting, runtime/cache and usage tests after the small integration change.

Completion means a documented, usable diagnostic harness with those offline checks and the agentic walkthrough/gates above. It does not mean the observed analytical or rendering defects have been resolved. Subsequent tuning or source fixes may be tested through an approved experiment; promotion of a candidate, new renderer capabilities and billed evaluation need their applicable concrete scope or authorization.

## Primary-source practices informing this design

Strands separates evaluation cases/tasks from outcome and trajectory evaluators, with deterministic checks available without model judges. This supports preserving both stage outputs and tool/correction history. The first version uses the already installed runtime and pytest; the separate Evals SDK is not installed and is unnecessary for this bounded collection. Its task/evaluator adapter can be added later without changing fixture identities. [Evaluation model](https://strandsagents.com/docs/user-guide/evals-sdk/how-evaluation-works/), [deterministic evaluators](https://strandsagents.com/docs/user-guide/evals-sdk/evaluators/deterministic_evaluators/).

Pydantic supports model validators for enforceable relationships. The current application already uses a scoped validator around a fixed accepted result; compatible contract variants extend that mechanism rather than assuming descriptive prose is enforced. [Pydantic validators](https://docs.pydantic.dev/latest/concepts/validators/).

Bedrock documents cache-read/write usage as evidence of actual caching. The design records those values separately from configured caching and estimated costs. [Bedrock prompt caching](https://docs.aws.amazon.com/bedrock/latest/userguide/prompt-caching.html).

Plotly documents `density_contour` as bin aggregation over observations. Therefore successful rendering of this function is insufficient evidence of a requested measured elevation surface. [Plotly density_contour](https://plotly.com/python-api-reference/generated/plotly.express.density_contour.html).

Text-to-SQL research motivates checking result semantics across distinguishing inputs rather than demanding identical SQL or assuming a matching result on one database establishes correctness. The harness uses small deliberate edge fixtures where justified, without adopting a benchmark's gold-query assumptions for open exploration. [Semantic Evaluation for Text-to-SQL with Distilled Test Suites](https://aclanthology.org/2020.emnlp-main.29/).
