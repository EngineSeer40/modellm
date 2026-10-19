"""Harness manifests bind inputs, changes and observations without duplicating app contracts."""

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.core.contracts import ConceptualPlan, AnalysisSpec, AcceptedAnalysisResult
from src.config import ReleaseSettings


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    format_version: Literal[1] = 1


class ArtifactRef(Manifest):
    path: str
    sha256: str
    classification: Literal["recorded", "reconstructed", "reported", "experimental"]
    source: str | None = None
    derivation: str | None = None


class PlottingFixture(Manifest):
    spec: AnalysisSpec
    result: AcceptedAnalysisResult
    source_index: int
    historical_acceptance_id: str | None = None
    profile_provenance: str = "reconstructed: SUMMARIZE preserved accepted relation once at import"


class UpstreamFixture(Manifest):
    fixture_id: str
    database: ArtifactRef
    plan: ConceptualPlan
    model_names: list[str]
    model_schemas: dict[str, dict[str, str]]
    summaries: dict[str, Any]
    request: str
    settings: dict[str, Any]
    controls: list[str]
    settings_provenance: str
    source: dict[str, Any]
    plotting: list[PlottingFixture] = Field(default_factory=list)
    validation_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def coherent(self):
        expected = {m.name for m in [*self.plan.facts, *self.plan.dimensions]}
        if not expected or set(self.model_names) != expected or set(self.model_schemas) != expected:
            raise ValueError("Model names, schemas and conceptual plan must describe the same nonempty model set")
        if set(self.summaries) != expected:
            raise ValueError("Summaries must describe exactly the selected models")
        ReleaseSettings(**self.settings).validate()
        if not self.request.strip():
            raise ValueError("A request is required")
        return self


class Scenario(Manifest):
    scenario_id: str
    fixture_id: str
    fixture_hash: str
    request: str
    parent_id: str | None = None
    reason: str | None = None
    checks: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def changed_request(self):
        if not self.request.strip() or (self.parent_id and not self.reason):
            raise ValueError("Request required; a derived scenario needs a reason")
        ids = [check["id"] for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("Scenario check IDs must be unique")
        return self


class Variant(Manifest):
    variant_id: str = "baseline"
    hypothesis: str = "Observe current behavior"
    baseline: str = "baseline"
    system_prompts: dict[str, str] = Field(default_factory=dict)
    context_factory: str | None = None
    schema_factory: str | None = None
    acceptance_validator: str | None = None
    plotting_validator: str | None = None
    cache_policy: Literal["default", "disabled"] = "default"
    context_layout: Literal["production", "flat"] = "production"
    plotting_turns: int = Field(default=4, ge=1)
    controls: list[str] | None = None
    settings: dict[str, Any] = Field(default_factory=dict)


class CaseEvaluation(Manifest):
    case_id: str
    kind: Literal["evidence", "acceptance", "plotting", "analytical"]
    expectation: str
    expected_acceptance: Literal["accepted", "rejected"] | None = None
    units: dict[str, str] = Field(default_factory=dict)
    result_index: int | None = None


class TrialPlan(Manifest):
    trial_id: str
    prepared_at: str
    mode: Literal["analysis", "plotting", "pipeline", "replay"]
    backend: Literal["scripted", "live", "recorded"]
    fixture: UpstreamFixture
    fixture_hash: str
    scenario: Scenario
    variant: Variant
    variant_hash: str
    source_root: str
    source_hashes: dict[str, str]
    packages: dict[str, str]
    input_hash: str
    settings: dict[str, Any]
    controls: list[str]
    result_indexes: list[int] = Field(default_factory=list)
    view_ordinal: int | None = Field(default=None, ge=0)
    script: list[dict[str, Any]] = Field(default_factory=list)
    recorded_plots: list[dict[str, Any]] = Field(default_factory=list)
    case_evaluation: CaseEvaluation | None = None
    diagnostic_acceptance: bool = False
    repetition: int = Field(default=0, ge=0)
    experiment_id: str | None = None
    iteration_id: str | None = None
    authorization: str | None = None


class CheckResult(Manifest):
    check_id: str
    version: int = 1
    dimension: str
    scope: str = "trial"
    status: Literal["pass", "fail", "needs_review", "not_applicable", "not_observed"]
    evidence: list[str] = Field(default_factory=list)
    explanation: str
    observed: Any = None
    expected: Any = None
    method: str = "code"


class TrialResult(Manifest):
    trial_id: str
    execution: Literal["finished", "failed", "interrupted"]
    stage: str | None = None
    failure: dict[str, Any] | None = None
    checks: list[CheckResult] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    cache: dict[str, Any] = Field(default_factory=dict)
    internal_model_calls: int = 0
    sdk_cycles: int = 0
    provider_dispatches: int = 0
    provider_attempts: int = 0
    usage_unknown: bool = False
    synthetic_usage: bool = False
    ended_at: str
    artifacts: dict[str, str] = Field(default_factory=dict)


class ExperimentPolicy(Manifest):
    experiment_id: str
    problem: str
    scenarios: list[str] = Field(min_length=1)
    mode: Literal["analysis", "plotting", "pipeline", "replay"]
    backend: Literal["scripted", "live", "recorded"] = "scripted"
    result_indexes: list[int] = Field(default_factory=list)
    view_ordinal: int | None = Field(default=None, ge=0)
    models: list[str] = Field(default_factory=list)
    allowed_paths: list[str] = Field(default_factory=lambda: ["src/core/prompt/*", "src/core/contracts.py"])
    allowed_variant_keys: list[str] = Field(default_factory=lambda: ["system_prompts", "schema_factory", "context_factory", "cache_policy", "context_layout", "acceptance_validator", "plotting_validator", "plotting_turns", "controls"])
    required_checks: list[str] = Field(default_factory=lambda: ["input_integrity", "execution"])
    protected_checks: list[str] = Field(default_factory=list)
    human_checks: list[str] = Field(default_factory=lambda: ["analytical_meaning", "coverage"])
    max_iterations: int = Field(default=5, ge=1)
    max_trials: int = Field(default=20, ge=1)
    max_model_calls: int = Field(default=40, ge=1)
    max_cost_usd: float = Field(default=1.0, gt=0)
    max_seconds: int = Field(default=1800, ge=1)
    repetitions: int = Field(default=1, ge=1)
    provider_retries: int = Field(default=0, ge=0)
    max_no_progress: int = Field(default=2, ge=1)
    offline_tests: list[str] = Field(default_factory=lambda: ["tests/test_analysis_boundary.py", "tests/test_execution_contracts.py", "tests/test_plot_context.py", "tests/test_plot_healing.py"])


class Iteration(Manifest):
    iteration_id: str
    experiment_id: str
    parent_id: str | None
    baseline_id: str
    hypothesis: str
    boundary: str
    created_at: str
    source_hashes: dict[str, str]
    source_root: str
    variant: Variant
    changed_paths: list[str]
    offline_results: list[dict[str, Any]] = Field(default_factory=list)
    trial_ids: list[str] = Field(default_factory=list)
    comparisons: list[str] = Field(default_factory=list)
    decisions: list[dict[str, Any]] = Field(default_factory=list)


class ReviewRecord(Manifest):
    review_id: str
    author: str
    author_kind: Literal["human", "agent"]
    trial_id: str
    check_id: str
    status: Literal["pass", "fail", "needs_review"]
    rationale: str
    evidence: list[str] = Field(min_length=1)
    created_at: str
    supersedes: str | None = None


class AuthorizationRecord(Manifest):
    subject_hash: str
    user_approval: str = Field(min_length=1)
    approval_reference: str = Field(min_length=1)
    created_at: str
    revoked: bool = False
