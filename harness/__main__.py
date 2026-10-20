"""JSON CLI for humans and external coding agents. Live dispatch is never implicit."""

import argparse
import json
import sys
from pathlib import Path

from .contracts import AuthorizationRecord, ExperimentPolicy, ReviewRecord
from .io import identifier, now, read, seal, unseal, write


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", default="harness", help="Local artifact/catalog root (default: harness)")
    commands = p.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import-run")
    imp.add_argument("--run", required=True)
    imp.add_argument("--id", required=True)
    imp.add_argument("--settings")
    imp.add_argument("--controls", help="Explicit comma-separated controls; empty string disables cost experiments")
    bundle = commands.add_parser("import-bundle")
    bundle.add_argument("--bundle", required=True)
    bundle.add_argument("--id", required=True)
    fixed = commands.add_parser("register-result")
    fixed.add_argument("--fixture", required=True)
    fixed.add_argument("--spec", required=True)
    fixed.add_argument("--relation", required=True)
    fixed.add_argument("--result-id", required=True)
    fixed.add_argument("--id", required=True)
    fixed.add_argument("--request-file")
    fixed.add_argument("--reason")
    for name in ("verify", "remove"):
        c = commands.add_parser(name)
        c.add_argument("--fixture", required=True)
    commands.add_parser("list")
    scenario = commands.add_parser("scenario")
    scenario.add_argument("--fixture", required=True)
    scenario.add_argument("--id", required=True)
    scenario.add_argument("--request-file", required=True)
    scenario.add_argument("--reason", required=True)
    scenario.add_argument("--checks")
    prepare = commands.add_parser("prepare")
    choice = prepare.add_mutually_exclusive_group(required=True)
    choice.add_argument("--fixture", dest="scenario_id")
    choice.add_argument("--scenario", dest="scenario_id")
    prepare.add_argument("--mode", choices=["analysis", "plotting", "pipeline", "replay"], required=True)
    prepare.add_argument("--backend", choices=["scripted", "live", "recorded"], default="scripted")
    prepare.add_argument("--variant", default="baseline")
    prepare.add_argument("--script")
    prepare.add_argument("--plots")
    prepare.add_argument("--result", type=int, action="append", dest="result_indexes")
    prepare.add_argument("--view", type=int, dest="view_ordinal")
    prepare.add_argument("--diagnostic-acceptance", action="store_true")
    prepare.add_argument("--authorization")
    for name in ("run", "_worker"):
        c = commands.add_parser(name)
        c.add_argument("--plan", required=True)
        if name == "run":
            c.add_argument("--backend", choices=["scripted", "live", "recorded"], required=True)
    comp = commands.add_parser("compare")
    comp.add_argument("--runs", nargs=2, required=True)
    comp.add_argument("--allowed-path", action="append", default=[])
    review = commands.add_parser("review")
    review.add_argument("--review-file", required=True)
    auth = commands.add_parser("authorize")
    auth.add_argument("--subject", required=True, help="Sealed plan.json or policy.json actually approved by user")
    auth.add_argument("--approval-file", required=True, help="File containing actual explicit user approval text")
    auth.add_argument("--reference", required=True, help="Reference to the user's approval")
    auth.add_argument("--out", required=True)
    exchange = commands.add_parser("extract-exchange")
    exchange.add_argument("--run", required=True)
    exchange.add_argument("--call-id", required=True)
    exchange.add_argument("--out", required=True)
    replay = commands.add_parser("replay")
    replay.add_argument("--case", required=True)
    replay.add_argument("--fixture", required=True)
    replay.add_argument("--variant", default="baseline")
    experiment = commands.add_parser("experiment")
    exp_sub = experiment.add_subparsers(dest="action", required=True)
    create = exp_sub.add_parser("create")
    create.add_argument("--policy", required=True)
    iteration = commands.add_parser("iteration")
    it_sub = iteration.add_subparsers(dest="action", required=True)
    for name in ("create", "prepare", "evaluate", "gate", "decision"):
        c = it_sub.add_parser(name)
        c.add_argument("--experiment", required=True)
        if name == "create":
            c.add_argument("--parent")
            c.add_argument("--candidate", required=True)
        else:
            c.add_argument("--iteration", required=True)
        if name in {"prepare", "evaluate"}:
            c.add_argument("--authorization")
        if name == "gate":
            c.add_argument("--require-eligible", action="store_true")
        if name == "decision":
            c.add_argument("--text", required=True)
            c.add_argument("--evidence", action="append", required=True)
    return p


def _variant(value):
    if value == "baseline":
        return {}
    return read(value)


def dispatch(args):
    from . import fixtures, runner
    root = Path(args.root).resolve()
    command = args.command
    if command == "import-run":
        return fixtures.import_run(root, args.run, args.id, read(args.settings) if args.settings else None,
            [x.strip() for x in args.controls.split(",") if x.strip()] if args.controls is not None else None)
    if command == "import-bundle":
        return fixtures.import_bundle(root, args.bundle, args.id)
    if command == "register-result":
        return fixtures.create_plotting_fixture(root, args.fixture, read(args.spec), args.relation,
            args.result_id, args.id, Path(args.request_file).read_text() if args.request_file else None, args.reason)
    if command == "verify":
        return fixtures.verify_fixture(root, args.fixture)
    if command == "list":
        return fixtures.catalog(root)
    if command == "remove":
        return fixtures.unregister(root, args.fixture)
    if command == "scenario":
        return fixtures.create_scenario(root, args.fixture, args.id, Path(args.request_file).read_text(), args.reason,
                                        read(args.checks) if args.checks else None)
    if command == "prepare":
        return runner.prepare_trial(root, args.scenario_id, mode=args.mode, backend=args.backend,
            variant=_variant(args.variant), script=read(args.script) if args.script else None,
            recorded_plots=read(args.plots) if args.plots else None, result_indexes=args.result_indexes,
            view_ordinal=args.view_ordinal, diagnostic_acceptance=args.diagnostic_acceptance,
            authorization=str(Path(args.authorization).resolve()) if args.authorization else None)
    if command in {"run", "_worker"}:
        if command == "_worker":
            return runner.run_worker(args.plan).model_dump()
        plan, _ = runner.load_plan(args.plan)
        if plan.backend != args.backend:
            raise ValueError("Explicit backend does not match the immutable prepared plan")
        return runner.execute_trial(args.plan).model_dump()
    if command == "compare":
        from .reporting import compare
        return compare(root, *args.runs, allowed_paths=args.allowed_path)
    if command == "review":
        from .reporting import record_review
        return record_review(root, read(args.review_file))
    if command == "authorize":
        _, fingerprint = unseal(args.subject)
        record = AuthorizationRecord(subject_hash=fingerprint, user_approval=Path(args.approval_file).read_text(),
            approval_reference=args.reference, created_at=now())
        seal(args.out, record)
        return {"authorization": str(Path(args.out).resolve()), "subject_hash": fingerprint,
                "notice": "This record is valid only if its text/reference represent actual explicit user approval"}
    if command == "extract-exchange":
        from .replay import extract_exchange, script_from_exchange
        source = Path(args.run)
        if source.is_dir():
            source /= "result.json"
        record = read(source)
        messages = extract_exchange(record, args.call_id)
        value = {"source": str(source), "call_id": args.call_id, "messages": messages,
                 "responses": script_from_exchange(messages, {"AnalyticalPlan", "AcceptedResultPlottingPlan", "PlottingPlan", "AnalysisPlan", "AcceptedResultPlotSpec", "PlotSpec"}),
                 "limits": "Submissions test current execution wiring; tool responses match recorded queries only"}
        write(args.out, value, exclusive=True)
        return {"exchange": args.out}
    if command == "replay":
        from .cases import prepare_case
        prepared = prepare_case(root, args.case, args.fixture, _variant(args.variant))
        return runner.execute_trial(prepared["plan"]).model_dump()
    if command == "experiment":
        from .loop import create_experiment
        return create_experiment(root, read(args.policy))
    if command == "iteration":
        from . import loop
        if args.action == "create":
            return loop.create_iteration(root, args.experiment, args.parent, read(args.candidate))
        if args.action == "prepare":
            return {"trial_ids": loop.prepare_iteration(root, args.experiment, args.iteration, args.authorization)}
        if args.action == "evaluate":
            return loop.evaluate_iteration(root, args.experiment, args.iteration, args.authorization)
        if args.action == "gate":
            return loop.gate_iteration(root, args.experiment, args.iteration)
        if args.action == "decision":
            return loop.record_decision(root, args.experiment, args.iteration, args.text, args.evidence)
    raise ValueError("Unknown command")


def main(argv=None):
    args = parser().parse_args(argv)
    from .io import encode
    from .runner import ScopeStop
    try:
        result = dispatch(args)
        if isinstance(result, dict) and result.get("execution") in {"failed", "interrupted"}:
            code = 3
        elif args.command == "iteration" and args.action == "gate" and args.require_eligible and result["disposition"] != "eligible_for_review":
            code = 1
        else:
            code = 0
        print(json.dumps({"format_version": 1, "result": encode(result)}))
        return code
    except Exception as exc:
        code = 4 if isinstance(exc, ScopeStop) else 2
        print(json.dumps({"format_version": 1, "error": {"type": type(exc).__name__, "message": str(exc)}}))
        return code


if __name__ == "__main__":
    sys.exit(main())
