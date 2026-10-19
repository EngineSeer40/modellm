"""External-agent iteration protocol: immutable candidates, comparisons and finite gates."""

import difflib
import fcntl
import os
import subprocess
import sys
from fnmatch import fnmatch
from pathlib import Path

from .contracts import ExperimentPolicy, Iteration, TrialResult, Variant
from .fixtures import load_scenario, load_fixture, verify_fixture
from .io import digest, identifier, now, read, safe_id, seal, unseal, write
from .reporting import compare, effective_checks, trial_directory
from .runner import ScopeStop, code_files, execute_trial, load_plan, prepare_trial, snapshot_code


def experiment_dir(root, experiment_id):
    return Path(root).resolve() / "outputs" / "experiments" / safe_id(experiment_id)


def load_policy(root, experiment_id):
    data, fingerprint = unseal(experiment_dir(root, experiment_id) / "policy.json")
    return ExperimentPolicy.model_validate(data), fingerprint


def iteration_dir(root, experiment_id, iteration_id):
    return experiment_dir(root, experiment_id) / "iterations" / safe_id(iteration_id)


def load_iteration(root, experiment_id, iteration_id):
    data, _ = unseal(iteration_dir(root, experiment_id, iteration_id) / "iteration.json")
    return Iteration.model_validate(data)


def _save_iteration(root, iteration, event):
    directory = iteration_dir(root, iteration.experiment_id, iteration.iteration_id)
    seal(directory / "events" / f"{identifier('event')}.json", {"at": now(), "event": event, "iteration": iteration})
    seal(directory / "iteration.json", iteration, exclusive=False)


def create_experiment(root, policy):
    policy = ExperimentPolicy.model_validate(policy)
    directory = experiment_dir(root, policy.experiment_id)
    directory.mkdir(parents=True, exist_ok=False)
    locks = {}
    for scenario_id in policy.scenarios:
        scenario, fingerprint = load_scenario(root, scenario_id)
        verify_fixture(root, scenario.fixture_id)
        locks[scenario_id] = {"scenario": fingerprint, "fixture": scenario.fixture_hash}
    _, policy_hash = (None, seal(directory / "policy.json", policy))
    seal(directory / "inputs.json", locks)
    write(directory / "state.json", {"created_at": now(), "baseline_id": None, "iterations": []}, exclusive=True)
    return {"experiment_id": policy.experiment_id, "policy_hash": policy_hash}


def _inputs_unchanged(root, policy):
    locks, _ = unseal(experiment_dir(root, policy.experiment_id) / "inputs.json")
    for scenario_id, expected in locks.items():
        scenario, fingerprint = load_scenario(root, scenario_id)
        verify_fixture(root, scenario.fixture_id)
        if expected != {"scenario": fingerprint, "fixture": scenario.fixture_hash}:
            raise ScopeStop("Frozen scenario or fixture changed; start a separate experiment")


def _changes(before, after):
    return sorted(p for p in before.keys() | after.keys() if before.get(p) != after.get(p))


def _verify_iteration(iteration):
    from .io import file_hash
    actual = {p.as_posix(): file_hash(Path(iteration.source_root) / p) for p in code_files(iteration.source_root)}
    if actual != iteration.source_hashes:
        raise ScopeStop("Iteration source snapshot changed after creation")


def validate_dispatch(root, policy, plan):
    _inputs_unchanged(root, policy)
    if plan.mode != policy.mode or plan.backend != policy.backend or plan.scenario.scenario_id not in policy.scenarios:
        raise ScopeStop("Prepared trial is outside experiment stage/input/backend scope")
    if plan.result_indexes != (policy.result_indexes or list(range(len(plan.fixture.plotting))) if plan.mode in {"plotting", "replay"} else []):
        raise ScopeStop("Prepared trial is outside experiment result scope")
    if plan.view_ordinal != policy.view_ordinal or plan.repetition >= policy.repetitions:
        raise ScopeStop("Prepared trial is outside experiment view/repetition scope")
    iteration = load_iteration(root, policy.experiment_id, plan.iteration_id)
    _verify_iteration(iteration)
    if plan.trial_id not in iteration.trial_ids or plan.source_hashes != iteration.source_hashes or plan.variant != iteration.variant:
        raise ScopeStop("Prepared trial does not match its iteration snapshot")
    if plan.backend == "live" and {plan.settings["model_id"], plan.settings["plotting_model_id"]} - set(policy.models):
        raise ScopeStop("Prepared models are outside policy")


def create_iteration(root, experiment_id, parent_id, candidate):
    policy, _ = load_policy(root, experiment_id)
    _inputs_unchanged(root, policy)
    directory = experiment_dir(root, experiment_id)
    state = read(directory / "state.json")
    if len(state["iterations"]) >= policy.max_iterations:
        raise ScopeStop("Maximum iteration count reached")
    allowed = {"hypothesis", "boundary", "variant", "script", "scripts_by_scenario", "recorded_plots", "source_root", "retry_of"}
    if set(candidate) - allowed:
        raise ValueError(f"Unknown candidate fields: {sorted(set(candidate)-allowed)}")
    if not candidate.get("hypothesis") or not candidate.get("boundary"):
        raise ValueError("Candidate needs a hypothesis and target boundary")
    baseline_id = state["baseline_id"]
    if baseline_id and not parent_id:
        raise ValueError("A child iteration must name its parent")
    if parent_id and parent_id not in state["iterations"]:
        raise ValueError("Parent is not in this experiment")
    ident = identifier("iteration")
    target = iteration_dir(root, experiment_id, ident)
    target.mkdir(parents=True)
    hashes = snapshot_code(target / "code", candidate.get("source_root"))
    variant = Variant.model_validate(candidate.get("variant", {}))
    changed = []
    if baseline_id:
        baseline = load_iteration(root, experiment_id, baseline_id)
        changed = _changes(baseline.source_hashes, hashes)
        if any(not any(fnmatch(p, pattern) for pattern in policy.allowed_paths) for p in changed):
            raise ScopeStop(f"Candidate edits outside policy: {changed}")
        # Harness/gate semantics cannot be modified under a frozen experiment.
        if any(p.startswith("harness/") and not p.startswith("harness/variants/") for p in changed):
            raise ScopeStop("Changing harness/evaluator code requires a new experiment")
        bv, cv = baseline.variant.model_dump(), variant.model_dump()
        keys = {k for k in bv if bv[k] != cv[k]} - {"variant_id", "hypothesis", "baseline"}
        if keys - set(policy.allowed_variant_keys):
            raise ScopeStop(f"Candidate variant changes outside policy: {sorted(keys)}")
        candidate_identity = digest([hashes, {k: v for k, v in cv.items() if k not in {"variant_id", "hypothesis", "baseline"}}])
        for previous_id in state["iterations"]:
            previous = load_iteration(root, experiment_id, previous_id)
            old = previous.variant.model_dump()
            previous_identity = digest([previous.source_hashes, {k: v for k, v in old.items() if k not in {"variant_id", "hypothesis", "baseline"}}])
            if candidate_identity == previous_identity and not candidate.get("retry_of"):
                raise ScopeStop("Duplicate candidate without new evidence; declare a bounded provider retry when applicable")
        patches = []
        for path in changed:
            before = Path(baseline.source_root) / path
            after = target / "code" / path
            patches.extend(difflib.unified_diff(before.read_text().splitlines(True) if before.exists() else [],
                after.read_text().splitlines(True) if after.exists() else [], fromfile=f"baseline/{path}", tofile=f"candidate/{path}"))
        (target / "candidate.diff").write_text("".join(patches))
    if candidate.get("retry_of"):
        prior = TrialResult.model_validate(read(trial_directory(root, candidate["retry_of"]) / "trial.json"))
        prior_plan, _ = load_plan(trial_directory(root, candidate["retry_of"]) / "plan.json")
        if prior_plan.experiment_id != experiment_id or not prior.failure or prior.failure["category"] not in {"provider", "infrastructure"}:
            raise ScopeStop("retry_of must reference a provider/infrastructure failure in this experiment")
    iteration = Iteration(iteration_id=ident, experiment_id=experiment_id, parent_id=parent_id,
        baseline_id=baseline_id or ident, hypothesis=candidate["hypothesis"], boundary=candidate["boundary"],
        created_at=now(), source_hashes=hashes, source_root=str(target / "code"), variant=variant, changed_paths=changed)
    seal(target / "candidate.json", candidate)
    _save_iteration(root, iteration, "created")
    if not baseline_id:
        state["baseline_id"] = ident
    state["iterations"].append(ident)
    write(directory / "state.json", state)
    return iteration.model_dump()


def experiment_usage(root, experiment_id):
    directory = experiment_dir(root, experiment_id)
    state = read(directory / "state.json")
    usage = {"trials": 0, "internal_model_calls": 0, "estimated_cost_usd": 0.0, "usage_unknown": False,
             "provider_failures": 0, "provider_retries": 0}
    for ident in state["iterations"]:
        iteration = load_iteration(root, experiment_id, ident)
        candidate, _ = unseal(iteration_dir(root, experiment_id, ident) / "candidate.json")
        usage["provider_retries"] += bool(candidate.get("retry_of"))
        for trial_id in iteration.trial_ids:
            trial_dir = trial_directory(root, trial_id)
            dispatched = (trial_dir / "dispatch.json").exists() or (trial_dir / "trial.json").exists()
            usage["trials"] += dispatched
            if not (trial_dir / "trial.json").exists():
                usage["usage_unknown"] |= dispatched
                continue
            result = TrialResult.model_validate(read(trial_dir / "trial.json"))
            usage["internal_model_calls"] += result.internal_model_calls
            if not result.synthetic_usage:
                total = result.usage.get("total", {})
                cost = total.get("estimated_cost_usd")
                if cost is not None:
                    usage["estimated_cost_usd"] += cost
                plan, _ = load_plan(trial_dir / "plan.json")
                if plan.backend == "live":
                    usage["usage_unknown"] |= result.usage_unknown or cost is None or total.get("usage_status") != "reported"
            usage["provider_failures"] += bool(result.failure and result.failure["category"] in {"provider", "infrastructure"})
    return usage


def _remaining(root, policy):
    state = read(experiment_dir(root, policy.experiment_id) / "state.json")
    from datetime import datetime, timezone
    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(state["created_at"])).total_seconds()
    usage = experiment_usage(root, policy.experiment_id)
    if usage["trials"] >= policy.max_trials:
        raise ScopeStop("Experiment trial allowance exhausted")
    if usage["provider_retries"] > policy.provider_retries:
        raise ScopeStop("Provider retry allowance exhausted")
    if policy.backend == "live" and usage["usage_unknown"]:
        raise ScopeStop("Prior provider usage/pricing unknown; further billed calls stopped")
    values = {"max_model_calls": policy.max_model_calls - usage["internal_model_calls"],
        "max_cost_usd": policy.max_cost_usd - usage["estimated_cost_usd"], "run_seconds": int(policy.max_seconds - elapsed)}
    if any(v <= 0 for v in values.values()):
        raise ScopeStop("Experiment call/cost/time allowance exhausted")
    return values


def prepare_iteration(root, experiment_id, iteration_id, authorization=None):
    policy, _ = load_policy(root, experiment_id)
    _inputs_unchanged(root, policy)
    iteration = load_iteration(root, experiment_id, iteration_id)
    _verify_iteration(iteration)
    if iteration.trial_ids:
        return iteration.trial_ids
    _remaining(root, policy)
    candidate, _ = unseal(iteration_dir(root, experiment_id, iteration_id) / "candidate.json")
    for scenario_id in policy.scenarios:
        for repetition in range(policy.repetitions):
            prepared = prepare_trial(root, scenario_id, mode=policy.mode, backend=policy.backend,
                variant=iteration.variant, script=candidate.get("scripts_by_scenario", {}).get(scenario_id, candidate.get("script", [])),
                recorded_plots=candidate.get("recorded_plots", []), result_indexes=policy.result_indexes or None,
                view_ordinal=policy.view_ordinal, repetition=repetition, authorization=authorization,
                experiment_id=experiment_id, iteration_id=iteration_id, code_source=iteration.source_root)
            plan, _ = load_plan(prepared["plan"])
            if policy.backend == "live" and (not policy.models or {plan.settings["model_id"], plan.settings["plotting_model_id"]} - set(policy.models)):
                raise ScopeStop("Resolved models are outside the live experiment policy")
            iteration.trial_ids.append(prepared["trial_id"])
            _save_iteration(root, iteration, "trial_prepared")
    return iteration.trial_ids


def observed_trials(root, policy, iteration):
    """Require one observed trial per fixed scenario/repetition, without intersections."""
    expected = {(s, r) for s in policy.scenarios for r in range(policy.repetitions)}
    found, issues = {}, []
    if not iteration.offline_results:
        issues.append(f"{iteration.iteration_id}:offline_checks_missing")
    for trial_id in iteration.trial_ids:
        folder = trial_directory(root, trial_id)
        if not (folder / "trial.json").exists():
            issues.append(f"{trial_id}:trial_not_observed")
            continue
        plan, _ = load_plan(folder / "plan.json")
        result = TrialResult.model_validate(read(folder / "trial.json"))
        key = (plan.scenario.scenario_id, plan.repetition)
        if (key not in expected or key in found or plan.iteration_id != iteration.iteration_id
                or plan.experiment_id != policy.experiment_id or not result.checks):
            issues.append(f"{trial_id}:invalid_or_duplicate_observation")
            continue
        found[key] = trial_id
    if set(found) != expected:
        issues.append(f"{iteration.iteration_id}:required_observations_missing")
    return found, issues


def evaluate_iteration(root, experiment_id, iteration_id, authorization=None):
    policy, _ = load_policy(root, experiment_id)
    directory = experiment_dir(root, experiment_id)
    with (directory / "dispatch.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ScopeStop("Another worker is evaluating this experiment") from exc
        _inputs_unchanged(root, policy)
        iteration = load_iteration(root, experiment_id, iteration_id)
        _verify_iteration(iteration)
        if policy.backend == "live" and iteration.parent_id:
            parent_gate = gate_iteration(root, experiment_id, iteration.parent_id)
            if parent_gate["disposition"] in {"needs_review", "stopped", "incomparable"}:
                raise ScopeStop("Parent requires review or scope resolution before dependent live work")
        _remaining(root, policy)
        if not iteration.offline_results:
            tests = policy.offline_tests
            for path in tests:
                if not (path.startswith("tests/") or path.startswith("harness/tests/")) or ".." in Path(path).parts:
                    raise ValueError("Offline test paths must be inside tests/ or harness/tests/")
            command = [sys.executable, "-m", "pytest", *tests] if tests else [sys.executable, "-m", "compileall", "-q", "src", "harness"]
            log = iteration_dir(root, experiment_id, iteration_id) / "offline.log"
            with log.open("w") as stream:
                completed = subprocess.run(command, cwd=iteration.source_root,
                    env={**os.environ, "MODELLM_LANGFUSE_ENABLED": "false", "AWS_EC2_METADATA_DISABLED": "true"},
                    stdout=stream, stderr=subprocess.STDOUT, timeout=max(1, _remaining(root, policy)["run_seconds"]))
            iteration.offline_results.append({"command": command, "exit_code": completed.returncode,
                "log": str(log), "source_hash": digest(iteration.source_hashes), "at": now()})
            _save_iteration(root, iteration, "offline_checks")
        if any(r["exit_code"] for r in iteration.offline_results):
            return gate_iteration(root, experiment_id, iteration_id)
        trial_ids = prepare_iteration(root, experiment_id, iteration_id, authorization)
        for trial_id in trial_ids:
            path = trial_directory(root, trial_id)
            if (path / "trial.json").exists():
                continue
            if (path / "running.json").exists():
                raise ScopeStop("Interrupted/running attempt requires inspection, never automatic resend")
            result = execute_trial(path / "plan.json", remaining=_remaining(root, policy), _experiment_locked=True)
            if result.failure and result.failure["category"] in {"provider", "infrastructure"}:
                break
        iteration = load_iteration(root, experiment_id, iteration_id)
        # Rebuild comparisons on resume so new reviews cannot leave stale gates.
        iteration.comparisons = []
        for prior_id in {iteration.baseline_id, iteration.parent_id} - {None, iteration_id}:
            prior = load_iteration(root, experiment_id, prior_id)
            before, _ = observed_trials(root, policy, prior)
            after, _ = observed_trials(root, policy, iteration)
            for key in before.keys() & after.keys():
                report = compare(root, before[key], after[key], allowed_paths=policy.allowed_paths,
                                 allowed_variant_keys=policy.allowed_variant_keys)
                iteration.comparisons.append(report["comparison_id"])
        _save_iteration(root, iteration, "evaluated")
        return gate_iteration(root, experiment_id, iteration_id)


def gate_iteration(root, experiment_id, iteration_id):
    policy, _ = load_policy(root, experiment_id)
    iteration = load_iteration(root, experiment_id, iteration_id)
    failure, review, incomparable = [], [], []
    if not iteration.offline_results:
        review.append("offline_checks:not_observed")
    try:
        _inputs_unchanged(root, policy)
    except (ValueError, OSError) as exc:
        incomparable.append(str(exc))
    if any(r["exit_code"] for r in iteration.offline_results):
        failure.append("offline_checks")
    expected_trials = len(policy.scenarios) * policy.repetitions
    if len(iteration.trial_ids) != expected_trials:
        incomparable.append("required_trials_missing")
    for trial_id in iteration.trial_ids:
        path = trial_directory(root, trial_id)
        if not (path / "trial.json").exists():
            review.append(f"{trial_id}:not_observed")
            continue
        checks = effective_checks(root, trial_id, require_human=policy.human_checks)
        for key in set(policy.required_checks + policy.protected_checks + policy.human_checks):
            status = checks.get(key, {}).get("status", "not_observed")
            if status == "fail":
                failure.append(f"{trial_id}:{key}")
            elif status not in {"pass", "not_applicable"}:
                review.append(f"{trial_id}:{key}")
            elif key in policy.human_checks and checks.get(key, {}).get("review", {}).get("author_kind") != "human":
                review.append(f"{trial_id}:{key}:human_review_required")
    for comparison_id in iteration.comparisons:
        data, _ = unseal(Path(root) / "outputs" / "comparisons" / f"{comparison_id}.json")
        if data["disposition"] == "incomparable":
            incomparable.extend(data["mismatches"])
        failure.extend(f"regression:{k}" for k in data["regressions"] if k in policy.protected_checks or k in policy.required_checks)
    after, observation_issues = observed_trials(root, policy, iteration)
    incomparable.extend(observation_issues)
    actual_pairs = set()
    for comparison_id in iteration.comparisons:
        data, _ = unseal(Path(root) / "outputs" / "comparisons" / f"{comparison_id}.json")
        actual_pairs.add((data["baseline"], data["candidate"]))
    for prior_id in {iteration.baseline_id, iteration.parent_id} - {None, iteration_id}:
        prior = load_iteration(root, experiment_id, prior_id)
        before, issues = observed_trials(root, policy, prior)
        incomparable.extend(issues)
        for key in before:
            if key not in after or (before[key], after[key]) not in actual_pairs:
                incomparable.append(f"{prior_id}:{key[0]}:{key[1]}:required_comparison_missing")
    stop = None
    try:
        _remaining(root, policy)
    except ScopeStop as exc:
        stop = str(exc)
    state = read(experiment_dir(root, experiment_id) / "state.json")
    consecutive = 0
    current = iteration
    while current.parent_id:
        reports = [unseal(Path(root) / "outputs" / "comparisons" / f"{c}.json")[0] for c in current.comparisons]
        if not reports or any(any(k in policy.required_checks for k in r["improvements"]) for r in reports):
            break
        consecutive += 1
        current = load_iteration(root, experiment_id, current.parent_id)
    if consecutive >= policy.max_no_progress:
        stop = "Consecutive evaluated candidates made no progress against required checks"
    disposition = ("incomparable" if incomparable else "stopped" if stop else "revise" if failure else
                   "needs_review" if review else "eligible_for_review")
    value = {"format_version": 1, "experiment_id": experiment_id, "iteration_id": iteration_id,
        "disposition": disposition, "reason": stop, "blocking_checks": failure,
        "review_checks": review, "incomparability": incomparable,
        "usage": experiment_usage(root, experiment_id), "baseline_id": iteration.baseline_id,
        "parent_id": iteration.parent_id, "comparisons": iteration.comparisons,
        "allowed_next_actions": {"revise": ["create in-scope child candidate"], "needs_review": ["record evidence-based review", "independent offline investigation"],
            "incomparable": ["repair comparison or start separate experiment"], "stopped": ["inspect retained artifacts; obtain revised scope if needed"],
            "eligible_for_review": ["present candidate and limitations; no automatic production promotion"]}[disposition]}
    write(iteration_dir(root, experiment_id, iteration_id) / "gate.json", value)
    return value


def record_decision(root, experiment_id, iteration_id, decision, evidence):
    if not evidence:
        raise ValueError("Decision needs evidence references")
    iteration = load_iteration(root, experiment_id, iteration_id)
    iteration.decisions.append({"decision": decision, "evidence": evidence, "at": now()})
    _save_iteration(root, iteration, "decision")
    return iteration.decisions[-1]
