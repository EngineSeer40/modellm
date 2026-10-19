"""Scoped assessments for optional evidence cases, with explicit observation limits."""

import plotly.io as pio

from harness.io import digest


def evaluate_case(plan, sidecar, directory):
    case = plan.case_evaluation
    prefix = f"case:{case.case_id}"
    checks = []

    def add(suffix, status, explanation, **kwargs):
        checks.append(dict(check_id=f"{prefix}:{suffix}", dimension="encoding", status=status,
            explanation=explanation, evidence=["plan.json", "boundary.json"], **kwargs))

    if case.kind == "evidence":
        add("assessment", "not_observed", "Evidence only: no executable assessment is defined for this case. " + case.expectation)
        return checks
    if case.kind != "analytical":
        add("assessment", "needs_review", "The replay checks mechanics only. " + case.expectation, method="review")
        return checks

    fixed = next((p for p in plan.fixture.plotting if p.source_index == case.result_index), None)
    if not fixed or not case.units:
        add("assessment", "not_observed", "No accepted result or evidence-backed units are configured")
        return checks
    result_id = fixed.result.result_id
    scheduled = {digest(v["selection"]) for v in sidecar["schedule"] if v["result_id"] == result_id and v["selected"]}
    observations = []
    for call in sidecar["plot_calls"]:
        if call["result_id"] != result_id or call.get("status") != "rendered" or not call.get("figure"):
            continue
        figure = pio.read_json(directory / call["figure"])
        fields = call["plot"]["columns"].get("y", [])
        fields = fields if isinstance(fields, list) else [fields]
        # This case assesses y-axis comparisons. Multiple actual axes require a
        # richer explicit mapping; do not guess which trace carries each field.
        axes = sorted({getattr(trace, "yaxis", None) or "y" for trace in figure.data})
        units = sorted({case.units[f] for f in fields if f in case.units})
        observations.append({"selection": call["selection"], "figure": call["figure"],
            "fields": fields, "units": units, "axes": axes, "traces": len(figure.data),
            "unknown_fields": sorted(set(fields) - case.units.keys())})
    observed_views = {digest(o["selection"]) for o in observations}
    complete = bool(scheduled) and observed_views == scheduled
    incompatible = [o for o in observations if o["traces"] and len(o["axes"]) == 1 and len(o["units"]) > 1]
    uncertain = [o for o in observations if not o["traces"] or len(o["axes"]) != 1 or not o["units"] or o["unknown_fields"]]
    status = "fail" if incompatible else "not_observed" if not complete else "needs_review" if uncertain else "pass"
    add("shared_axis_units", status,
        "Inspected rendered trace axes and mapped y fields using case-declared units from recorded measure descriptions; "
        "different units on one magnitude axis fail. Missing views are unobserved; ambiguous axes/units require review.",
        observed={"views": observations, "complete": complete}, scope=f"result:{result_id}")
    sets = {tuple(sorted(set(o["fields"]) & case.units.keys())) for o in observations}
    status = "fail" if len(sets) > 1 else "not_observed" if not complete or len(observations) < 2 else "needs_review" if uncertain else "pass"
    add("variant_measure_consistency", status,
        "Compared mapped y-measure sets across rendered selector variants. Equal sets do not establish requested coverage or analytical correctness.",
        observed={"measure_sets": sorted(sets), "rendered_views": len(observations), "complete": complete},
        scope=f"result:{result_id}")
    add("assessment", "needs_review", "Unit and mapping checks cover only these explicit obligations. " + case.expectation, method="review")
    return checks
