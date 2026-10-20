"""Execution spans and optional worker-owned Langfuse export of native Strands traces."""

import json
import logging
import os
from contextlib import contextmanager
from collections.abc import Iterator
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode
from opentelemetry.util.types import AttributeValue

from .config import boolean
from .run import RunState

if TYPE_CHECKING:
    from langfuse import Langfuse
    from opentelemetry.sdk.trace import ReadableSpan

_langfuse_client: "Langfuse | None" = None
_langfuse_public = False
logger = logging.getLogger(__name__)


def _export_span(span: "ReadableSpan") -> bool:
    from langfuse.span_filter import is_default_export_span

    # Retain workflow/attempt parents as well as native model/tool children.
    return is_default_export_span(span) or (
        span.instrumentation_scope is not None
        and span.instrumentation_scope.name == __name__
    )


def configure_langfuse() -> None:
    """Initialise once per worker; credentials never enter run settings or artifacts."""
    global _langfuse_client, _langfuse_public
    if _langfuse_client is not None:
        return
    try:
        if not boolean("MODELLM_LANGFUSE_ENABLED", False):
            return
        public = boolean("MODELLM_LANGFUSE_PUBLIC", True)
        if not all(os.environ.get(name, "").strip() for name in (
            "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL",
        )):
            logger.warning("Langfuse recording disabled: project keys and base URL are required")
            return
        from langfuse import Langfuse

        # The SDK configures/reuses the global provider shared with Strands.
        _langfuse_client = Langfuse(timeout=2, should_export_span=_export_span)
        _langfuse_public = public
    except Exception:
        logger.warning("Langfuse setup failed; continuing without trace links")


def current_langfuse_trace_url() -> str | None:
    """Link the existing workflow trace, publishing it by default for UI sharing."""
    span = trace.get_current_span()
    context = span.get_span_context()
    if _langfuse_client is None or not span.is_recording() or not context.is_valid:
        return None
    try:
        if _langfuse_public:
            _langfuse_client.set_current_trace_as_public()
        url = _langfuse_client.get_trace_url(trace_id=f"{context.trace_id:032x}")
        if not url:
            logger.warning("Langfuse trace URL unavailable")
        return url
    except Exception:
        logger.warning("Langfuse trace URL unavailable; continuing workflow")
        return None


@contextmanager
def workflow_span(run_id: str, request: str = "") -> Iterator[Span]:
    # With no configured provider these API spans are no-ops; no exporter is created.
    with trace.get_tracer(__name__).start_as_current_span(
        "modellm.workflow", attributes={"modellm.run_id": run_id}
    ) as span:
        if _langfuse_client is not None:
            span.set_attribute("langfuse.trace.name", run_id)
            span.set_attribute("langfuse.trace.metadata.run_id", json.dumps(run_id))
            span.set_attribute("langfuse.observation.input", json.dumps(request))
            for name in ("MODELLM_APP_VERSION", "MODELLM_GIT_SHA"):
                if value := os.environ.get(name):
                    span.set_attribute(f"langfuse.trace.metadata.{name.lower()}", json.dumps(value))
        yield span


def finish_workflow_trace(span: Span, state: RunState) -> None:
    """Summarise the outcome without exporting the full result or duplicating usage."""
    if _langfuse_client is None or not span.is_recording():
        return
    try:
        output: dict[str, str | int] = {"status": state["status"], "chart_count": len(state.get("charts", []))}
        if state.get("error"):
            output["error"] = state["error"]
        span.set_attribute("langfuse.observation.output", json.dumps(output))
        span.set_attribute("langfuse.trace.metadata.cost_experiments",
                           json.dumps(state.get("cost_experiments", [])))
        cost = state.get("usage", {}).get("total", {}).get("estimated_cost_usd")
        if cost is not None:
            span.set_attribute("langfuse.trace.metadata.reported_estimated_cost_usd", json.dumps(cost))
    except Exception:
        logger.warning("Langfuse workflow summary unavailable")


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
    """Best-effort normal-exit flush; SDK batching may ignore the requested timeout."""
    flush = getattr(trace.get_tracer_provider(), "force_flush", None)
    if flush is not None:
        try:
            if flush(timeout_millis=timeout_millis) is False:
                logger.warning("Telemetry flush did not complete")
        except Exception:
            logger.warning("Telemetry flush failed")
