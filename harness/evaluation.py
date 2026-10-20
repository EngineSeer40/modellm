"""Bounded checks and explicit review gaps; no generic SQL/prose truth claim."""

import math
from pathlib import Path

import numpy as np
import pandas as pd

from src.analysis import load_result_frame
from src.config import ReleaseSettings, use_settings
from src.core.contracts import AcceptedAnalysisResult, AnalysisSpec

from .contracts import CheckResult
from .io import digest


def evaluate(plan, trial, sidecar, database, integrity):
    checks = []

    def add(check_id, dimension, status, explanation, **kwargs):
        if any(c.check_id == check_id for c in checks):
            raise ValueError(f"Duplicate evaluation check ID: {check_id}")
        checks.append(CheckResult(check_id=check_id, dimension=dimension, status=status,
                                  explanation=explanation, **kwargs))

    add("input_integrity", "integrity", "pass" if integrity else "fail",
        "Frozen input hash and upstream main-table checksums remained invariant" if integrity else "Input or upstream model contents changed",
        evidence=["plan.json", "boundary.json"])
    add("execution", "execution", "pass" if trial.execution == "finished" else "fail",
        "Selected stages finished; this is not an analytical verdict" if trial.execution == "finished" else str(trial.failure),
        evidence=["result.json"])
    for check_id, dimension, text in (
        ("analytical_meaning", "semantics", "Population, calculations, units and row-grain prose require evidence-based review"),
        ("coverage", "coverage", "Review request → proposal → result → scheduled view → visible encoding; alternatives may be valid"),
        ("encoding_semantics", "encoding", "A rendered trace is insufficient to establish that the visual operation answers the request"),
    ):
        add(check_id, dimension, "needs_review", text, evidence=["boundary.json", "result.json"], method="review")
    for i, observation in enumerate(sidecar.get("replay", [])):
        expected = observation.get("expected")
        passed = expected and expected == observation["outcome"]
        add(f"replay:{i}", "mechanics", "not_observed" if observation["outcome"] == "not_observed" else "pass" if passed else "fail" if expected else "needs_review",
            "Current validator outcome compared with an explicit mechanical expectation; not proof of analytical correctness",
            observed=observation, expected=expected, evidence=["boundary.json"])
    mock_events = [c for c in sidecar["captures"] if c["kind"] in {"mock_match", "mock_mismatch"}]
    if mock_events:
        add("tool_mock_scope", "integrity", "fail" if any(c["kind"] == "mock_mismatch" for c in mock_events) else "pass",
            "Recorded tool responses answer only their exact fixture/tool/arguments; mismatches return SDK errors, never unrelated data",
            evidence=["boundary.json"])
    selected = [v for v in sidecar["schedule"] if v["selected"]]
    mismatched = [v["view_id"] for v in sidecar["schedule"] if "sql_selected_rows" in v and v["sql_selected_rows"] != v["frame_selected_rows"]]
    if any("sql_selected_rows" in v for v in sidecar["schedule"]):
        add("selector_population", "shape", "fail" if mismatched else "pass",
            "Compared independently grouped SQL population counts with actual DataFrame selections",
            observed={"mismatched_views": mismatched}, evidence=["boundary.json"])
    if plan.mode in {"plotting", "pipeline"}:
        missing = [v["view_id"] for v in selected if not v.get("figure")]
        add("scheduled_views", "shape", "fail" if missing else "pass",
            "All selected scheduled views have figures" if not missing else "Required scheduled views were skipped or unobserved",
            observed={"scheduled": len(selected), "missing": missing}, evidence=["boundary.json"])
    scenario_observations = {check["id"]: [] for check in plan.scenario.checks}
    with use_settings(ReleaseSettings(**plan.settings)):
        for entry in sidecar["accepted"]:
            spec = AnalysisSpec.model_validate(entry["spec"])
            result = AcceptedAnalysisResult.model_validate(entry["result"])
            try:
                frame = load_result_frame(database, result)
            except Exception as exc:
                for check in plan.scenario.checks:
                    if check.get("result_id") not in (None, result.result_id):
                        continue
                    scoped_id = f"{check['id']}:result:{result.result_id}"
                    add(scoped_id, "shape", "not_observed", f"Could not inspect accepted result: {type(exc).__name__}: {exc}",
                        scope=f"result:{result.result_id}", evidence=["boundary.json"])
                    scenario_observations[check["id"]].append({"check_id": scoped_id, "status": "not_observed"})
                continue
            for measure in spec.measures:
                c = measure.components
                if not c:
                    continue
                fields = list(dict.fromkeys([measure.field, c.numerator_field, c.denominator_field]))
                if not set(fields) <= set(frame):
                    add(f"ratio:{result.result_id}:{measure.field}", "semantics", "fail", "Ratio component fields are missing")
                    continue
                numeric = frame[fields].apply(pd.to_numeric, errors="coerce")
                malformed = bool((frame[fields].notna() & numeric.isna()).any().any())
                valid = numeric.notna().all(axis=1) & np.isfinite(numeric).all(axis=1) & numeric[c.denominator_field].ne(0)
                observed = numeric.loc[valid, measure.field].astype(float)
                expected = (numeric.loc[valid, c.numerator_field].astype(float) * c.scale / numeric.loc[valid, c.denominator_field].astype(float))
                passes = np.isclose(observed, expected, rtol=1e-7, atol=1e-9).all()
                status = "fail" if malformed or not passes else "pass" if valid.any() else "not_observed"
                add(f"ratio:{result.result_id}:{measure.field}", "semantics", status,
                    "Checked declared ratio identity on finite non-null nonzero-denominator rows; does not establish population or denominator meaning",
                    observed={"checked_rows": int(valid.sum()), "excluded_rows": int((~valid).sum())}, evidence=["boundary.json"])
            for call in sidecar["plot_calls"]:
                if call["result_id"] != result.result_id or "plot" not in call:
                    continue
                for channel, bound in (("lat", 90), ("lon", 180)):
                    field = call["plot"]["columns"].get(channel)
                    if field and field in frame:
                        from src.visualisation import filter_frame
                        from src.core.contracts import ResultFilter
                        selected_frame = filter_frame(frame, [ResultFilter.model_validate(s) for s in call.get("selection", [])])
                        values = pd.to_numeric(selected_frame[field].dropna(), errors="coerce")
                        valid = bool(values.notna().all() and values.between(-bound, bound).all())
                        # Trial/event identities are unsuitable comparison keys.
                        identity = digest([result.result_id, call.get("selection", [])])
                        add(f"coordinate:{identity}:{channel}", "encoding", "pass" if valid else "fail",
                            "Geographic channel must use valid degree bounds; passing bounds does not establish CRS correctness",
                            observed={"field": field, "min": values.min() if len(values) else None,
                                      "max": values.max() if len(values) else None},
                            scope=f"result:{result.result_id}:selection:{identity}", evidence=["boundary.json"])
            for check in plan.scenario.checks:
                if check.get("result_id") not in (None, result.result_id):
                    continue
                kind = check["kind"]
                if kind == "row_count":
                    observed = len(frame)
                elif kind == "distinct":
                    observed = int(frame[check["field"]].nunique(dropna=False))
                elif kind == "null_count":
                    observed = int(frame[check["field"]].isna().sum())
                elif kind == "visible_measure":
                    measures = {m.field for m in spec.measures}
                    calls = [c for c in sidecar["plot_calls"] if c["result_id"] == result.result_id and "plot" in c]
                    channels = {"x", "y", "z", "color", "size", "values", "dimensions", "r", "a", "b", "c"}
                    observed = bool(calls) and all(measures.intersection(
                        field for key, value in c["plot"]["columns"].items() if key in channels
                        for field in (value if isinstance(value, list) else [value])) for c in calls)
                else:
                    raise ValueError(f"Unsupported declared check kind: {kind}")
                status = "pass" if observed == check["expected"] else "fail"
                scoped_id = f"{check['id']}:result:{result.result_id}"
                add(scoped_id, "shape", status, check["explanation"], scope=f"result:{result.result_id}",
                    observed=observed, expected=check["expected"], evidence=["boundary.json"])
                scenario_observations[check["id"]].append({"check_id": scoped_id, "status": status})
    for check in plan.scenario.checks:
        observations = scenario_observations[check["id"]]
        status = ("fail" if any(o["status"] == "fail" for o in observations) else
                  "pass" if observations and all(o["status"] == "pass" for o in observations) else "not_observed")
        add(check["id"], "shape", status, check["explanation"], observed=observations,
            expected=check["expected"], evidence=["boundary.json"])
    if plan.case_evaluation:
        from .cases.evaluation import evaluate_case
        for check in evaluate_case(plan, sidecar, database.parent):
            add(**check)
    add("model_usage", "usage", "not_observed" if trial.synthetic_usage or plan.backend == "recorded" else
        "needs_review" if trial.usage_unknown else "pass",
        "Scripted/recorded trials do not measure model quality, cost or cache behavior" if trial.synthetic_usage or plan.backend == "recorded" else
        "Provider-reported usage; inspect raw cache-key presence separately from cache configuration", evidence=["trial.json", "boundary.json"])
    return checks
