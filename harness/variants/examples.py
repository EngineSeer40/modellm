"""Small dataset-independent candidate examples for the offline walkthrough."""

from pydantic import model_validator


def no_op_schema(base, context):
    """Compatible schema extension; deliberately makes no quality-improvement claim."""
    class Candidate(base):
        pass
    Candidate.__name__ = base.__name__
    return Candidate


def require_visible_measure(base, context):
    """Example enforceable PlottingPlan obligation using current stage metadata."""
    if context["stage"] != "plotting":
        return base
    measures = {m.field for m in context["spec"].measures}

    class Candidate(base):
        @model_validator(mode="after")
        def visible_measure(self):
            channels = {"x", "y", "z", "color", "size", "values", "dimensions", "r", "a", "b", "c"}
            fields = {field for key, value in self.columns.items() if key in channels
                      for field in (value if isinstance(value, list) else [value])}
            if measures and not fields.intersection(measures):
                raise ValueError("No declared measure is visibly encoded")
            return self
    Candidate.__name__ = base.__name__
    return Candidate


def reject_every_plot(frame, plot, result, selection, figure):
    """Deliberately regressing candidate used to demonstrate gates, never a solution."""
    raise ValueError("Demonstration candidate rejects every plot")


def reject_metadata(spec, result):
    """Deliberate transactional rejection for the isolation test."""
    raise ValueError("Demonstration metadata rejection")
