"""Portable reports and invariant-first comparisons for humans and coding agents."""

from pathlib import Path
from uuid import uuid4

from .contracts import ReviewRecord, TrialResult
from .io import digest, now, read, seal, unseal, write
from .runner import load_plan


def render_report(directory, plan, trial, sidecar):
    import plotly.io as pio
    directory = Path(directory)
    for path in directory.glob("chart_*.json"):
        pio.read_json(path).write_html(path.with_suffix(".html"), include_plotlyjs=True, auto_open=False)
    for path in directory.glob("replay_*.json"):
        pio.read_json(path).write_html(path.with_suffix(".html"), include_plotlyjs=True, auto_open=False)
    lines = [f"# Trial {trial.trial_id}", "", f"Execution: {trial.execution}. Backend: {plan.backend}.",
        "Execution success is separate from analytical correctness.", "",
        "| Check | Dimension | Status | Evidence / limitation |", "|---|---|---|---|"]
    if plan.case_evaluation:
        lines[4:4] = [f"Named case: {plan.case_evaluation.case_id}; evaluation kind: {plan.case_evaluation.kind}.", ""]
    for check in trial.checks:
        lines.append(f"| {check.check_id} | {check.dimension} | {check.status} | {check.explanation.replace('|', '/')} |")
    lines += ["", f"Internal model calls: {trial.internal_model_calls}; agent invocations: {len(sidecar['invocations'])}; "
              f"SDK cycles: {trial.sdk_cycles}; provider dispatches/attempts: {trial.provider_dispatches}/{trial.provider_attempts}.",
              "", "See plan.json, result.json and boundary.json for fixed inputs, SQL/proposals, selections, corrections and figures.",
              "Semantic review must follow the original request through each boundary; unresolved checks remain unreviewed."]
    (directory / "report.md").write_text("\n".join(lines) + "\n")


def trial_directory(root, trial_id):
    from .io import safe_id
    return Path(root).resolve() / "outputs" / "trials" / safe_id(trial_id)


def effective_checks(root, trial_id, *, require_human=None):
    directory = trial_directory(root, trial_id)
    result = TrialResult.model_validate(read(directory / "trial.json"))
    checks = {}
    for check in result.checks:
        if check.check_id in checks:
            raise ValueError(f"Duplicate check ID in trial {trial_id}: {check.check_id}; rerun with scoped checks")
        checks[check.check_id] = check.model_dump()
    reviews = [ReviewRecord.model_validate(unseal(path)[0]) for path in (directory / "reviews").glob("*.json")]
    for review in sorted(reviews, key=lambda r: (r.created_at, r.review_id)):
        if require_human and review.check_id in require_human and review.author_kind != "human":
            continue
        if review.check_id in checks:
            checks[review.check_id] = {**checks[review.check_id], "status": review.status,
                "review": review.model_dump()}
    return checks


def compare(root, baseline_id, candidate_id, *, allowed_paths=None, allowed_variant_keys=None):
    from fnmatch import fnmatch
    base_dir, cand_dir = trial_directory(root, baseline_id), trial_directory(root, candidate_id)
    base, _ = load_plan(base_dir / "plan.json")
    candidate, _ = load_plan(cand_dir / "plan.json")
    mismatches = []
    for key in ("input_hash", "mode", "backend", "packages", "repetition"):
        if getattr(base, key) != getattr(candidate, key):
            mismatches.append(key)
    bvariant, cvariant = base.variant.model_dump(), candidate.variant.model_dump()
    variant_changes = {k: {"before": bvariant[k], "after": cvariant[k]} for k in bvariant if bvariant[k] != cvariant[k]}
    source_changes = sorted(p for p in base.source_hashes.keys() | candidate.source_hashes.keys()
                            if base.source_hashes.get(p) != candidate.source_hashes.get(p))
    # Even standalone comparisons require an explicit declaration of code changes.
    if any(not any(fnmatch(p, pattern) for pattern in allowed_paths or []) for p in source_changes):
        mismatches.append("undeclared_source_changes")
    metadata_keys = {"variant_id", "hypothesis", "baseline"}
    if allowed_variant_keys is not None and set(variant_changes) - metadata_keys - set(allowed_variant_keys):
        mismatches.append("undeclared_variant_changes")
    settings_changes = {k for k in base.settings if base.settings[k] != candidate.settings[k]}
    declared_settings = set(base.variant.settings) | set(candidate.variant.settings)
    if settings_changes - declared_settings:
        mismatches.append("undeclared_settings_changes")
    bchecks, cchecks = effective_checks(root, baseline_id), effective_checks(root, candidate_id)
    regressions = sorted(k for k in bchecks if bchecks[k]["status"] == "pass" and cchecks.get(k, {}).get("status") != "pass")
    improvements = sorted(k for k in cchecks if cchecks[k]["status"] == "pass" and bchecks.get(k, {}).get("status") != "pass")
    directory = Path(root) / "outputs" / "comparisons"
    comparison_id = f"comparison_{uuid4().hex[:12]}"
    value = {"format_version": 1, "comparison_id": comparison_id, "baseline": baseline_id, "candidate": candidate_id,
        "disposition": "incomparable" if mismatches else "compared", "mismatches": mismatches,
        "regressions": regressions, "improvements": improvements, "source_changes": source_changes,
        "variant_changes": variant_changes, "baseline_checks": bchecks, "candidate_checks": cchecks}
    value["script_changed"] = base.script != candidate.script
    value["behavior_measurement"] = base.backend == "live"
    value["script_limit"] = "Changed scripted responses test wiring, not model behavior or tuned prompt quality" if value["script_changed"] else None
    value["dispatch_settings"] = {"baseline": read(base_dir / "dispatch.json")["settings"] if (base_dir / "dispatch.json").exists() else base.settings,
        "candidate": read(cand_dir / "dispatch.json")["settings"] if (cand_dir / "dispatch.json").exists() else candidate.settings}
    seal(directory / f"{comparison_id}.json", value)
    return value


def record_review(root, review):
    review = ReviewRecord.model_validate(review)
    directory = trial_directory(root, review.trial_id)
    checks = effective_checks(root, review.trial_id)
    if review.check_id not in checks:
        raise ValueError("Review references an unknown check")
    if review.check_id in {"input_integrity", "execution"} and review.status != checks[review.check_id]["status"]:
        raise ValueError("Reviews cannot override mechanical integrity/execution facts")
    if checks[review.check_id]["method"] == "code" and checks[review.check_id]["status"] in {"pass", "fail"} and review.status != checks[review.check_id]["status"]:
        raise ValueError("Reviews annotate mechanical facts; they cannot reverse an executed check")
    if review.supersedes and not (directory / "reviews" / f"{review.supersedes}.json").exists():
        raise ValueError("Superseded review does not exist")
    seal(directory / "reviews" / f"{review.review_id}.json", review)
    return review.model_dump()
