"""Execution spans only; a future worker exporter can send native Strands traces."""

import logging
from contextlib import contextmanager
from collections.abc import Iterator

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue


@contextmanager
def workflow_span(run_id: str) -> Iterator[Span]:
    # With no configured provider these API spans are no-ops; no exporter is created.
    with trace.get_tracer(__name__).start_as_current_span(
        "modellm.workflow", attributes={"modellm.run_id": run_id}
    ) as span:
        yield span


@contextmanager
def stage_span(run_id: str, stage: str, *, attempt: int | None = None,
               result_index: int | None = None) -> Iterator[dict[str, AttributeValue]]:
    attributes: dict[str, AttributeValue] = {"modellm.run_id": run_id, "modellm.stage": stage}
    if attempt is not None:
        attributes["modellm.attempt"] = attempt
    if result_index is not None:
        attributes["modellm.result_index"] = result_index
    with trace.get_tracer(__name__).start_as_current_span(stage, attributes=attributes) as span:
        span.set_attribute("modellm.outcome", "accepted")
        try:
            yield attributes
        except BaseException:
            span.set_attribute("modellm.outcome", "rejected")
            raise


def reject_attempt(message: str) -> None:
    """Mark handled validation failures too; repairs need not raise an exception."""
    span = trace.get_current_span()
    span.set_attribute("modellm.outcome", "rejected")
    span.set_status(Status(StatusCode.ERROR, message))


def flush_telemetry(timeout_millis: int = 1000) -> None:
    """Bound normal worker shutdown without changing the workflow's result."""
    flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    if flush is not None:
        try:
            flush(timeout_millis=timeout_millis)
        except Exception:
            logging.getLogger(__name__).warning("Telemetry flush failed")
