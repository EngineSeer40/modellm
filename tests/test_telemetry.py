import asyncio
import importlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from strands import tool

from src import agent_runtime, telemetry, workflow
from test_context_cost import LocalModel
from test_model_selection import Response


@pytest.fixture(autouse=True)
def reset_langfuse(monkeypatch):
    monkeypatch.setattr(telemetry, "_langfuse_client", None)
    monkeypatch.setattr(telemetry, "_langfuse_public", False)
    monkeypatch.setenv("MODELLM_LANGFUSE_ENABLED", "false")


@pytest.fixture
def spans(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Configure a test-local provider without replacing OTEL's global singleton.
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: provider)
    native = importlib.import_module("strands.telemetry.tracer")
    monkeypatch.setattr(native, "_tracer_instance", None)
    yield exporter
    provider.shutdown()


@pytest.fixture
def langfuse_spans(spans, monkeypatch):
    from langfuse import Langfuse

    exporter = InMemorySpanExporter()
    monkeypatch.setenv("MODELLM_LANGFUSE_ENABLED", "true")
    monkeypatch.setenv("MODELLM_LANGFUSE_PUBLIC", "true")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-offline-native-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-offline-native-test")
    # Use the real SDK/provider/filter but replace export and the one project lookup.
    monkeypatch.setattr("langfuse.Langfuse", lambda **kwargs: Langfuse(**kwargs, span_exporter=exporter))
    telemetry.configure_langfuse()
    client = telemetry._langfuse_client
    monkeypatch.setattr(client.api.projects, "get", lambda: SimpleNamespace(data=[SimpleNamespace(id="demo")]))
    yield exporter
    client.shutdown()


def test_native_sdk_spans_share_workflow_attempt_context_and_call_ids(spans, langfuse_spans, monkeypatch):
    models = []

    @tool
    async def read_value() -> int:
        """Return a recorded value for this local tracing test."""
        return 7

    class FakeBedrockModel(LocalModel):
        def __init__(self, **kwargs):
            super().__init__([("read_value", {}), ("Response", {"value": "ok"})])
            self.config = kwargs
            self.client = SimpleNamespace(meta=SimpleNamespace(region_name="local"))
            models.append(self)

    monkeypatch.setattr("strands.models.BedrockModel", FakeBedrockModel)
    events = []
    async def invoke_attempts():
        for attempt in [1, 2]:
            with telemetry.stage_span("test-run", "analytical interpretation", attempt=attempt) as attributes:
                result = await agent_runtime._invoke("Question", Response, "analytical interpretation",
                    lambda *event: events.append(event), "Return a value", tools=[read_value],
                    trace_attributes=attributes, cost_experiments=frozenset())
                assert result.value == "ok"
                if attempt == 1:
                    telemetry.reject_attempt("Rejected by deterministic acceptance")

    with telemetry.workflow_span("test-run", "Question") as root_span:
        url = telemetry.current_langfuse_trace_url()
        assert url == f"https://cloud.langfuse.com/project/demo/traces/{root_span.get_span_context().trace_id:032x}"
        asyncio.run(invoke_attempts())
    # An unrelated scope must not be exported simply because it is in the process.
    with trace.get_tracer("unrelated.library").start_as_current_span("unrelated"):
        pass
    telemetry.flush_telemetry()

    finished = langfuse_spans.get_finished_spans()
    roots = [span for span in finished if span.name == "modellm.workflow"]
    assert len(roots) == 1
    root = roots[0]
    assert root.attributes["langfuse.trace.public"] is True
    assert json.loads(root.attributes["langfuse.observation.input"]) == "Question"
    assert all(span.name != "unrelated" for span in finished)
    assert len(finished) == len(spans.get_finished_spans()) - 1
    assert all(span.context.trace_id == root.context.trace_id for span in finished)
    attempts = sorted([span for span in finished if span.name == "analytical interpretation"],
                      key=lambda span: span.attributes["modellm.attempt"])
    assert len(attempts) == 2
    assert all(span.parent.span_id == root.context.span_id for span in attempts)
    assert [span.attributes["modellm.outcome"] for span in attempts] == ["rejected", "accepted"]
    assert attempts[0].status.status_code == StatusCode.ERROR
    agents = [span for span in finished if span.attributes.get("gen_ai.operation.name") == "invoke_agent"]
    assert len(agents) == 2
    assert {span.parent.span_id for span in agents} == {span.context.span_id for span in attempts}
    assert all(span.attributes["modellm.run_id"] == "test-run" for span in agents)
    call_ids = {event[3]["call_id"] for event in events if event[1] == "model_call"}
    assert call_ids == {span.attributes["modellm.call_id"] for span in agents}
    # The actual SDK emits children, rather than an application event-to-span copy.
    assert any(span.parent and span.parent.span_id in {agent.context.span_id for agent in agents}
               for span in finished)
    assert any(span.attributes.get("gen_ai.tool.name") == "read_value" for span in finished)
    assert len(models) == 2 and all(len(model.requests) == 2 for model in models)


def test_handled_workflow_failure_marks_root_without_changing_result(spans, tmp_path, monkeypatch):
    paths = [tmp_path / "accounts.csv", tmp_path / "events.csv"]
    for path in paths:
        path.write_text("id\n1\n")

    def reject_sources(*args):
        raise ValueError("Source inspection failed")

    monkeypatch.setattr(workflow, "_prepare_sources", reject_sources)
    client = Mock()
    client.get_trace_url.return_value = "https://cloud.langfuse.com/project/demo/traces/existing"
    monkeypatch.setattr(telemetry, "_langfuse_client", client)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-not-for-artifacts")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-not-for-artifacts")
    state = workflow.run(paths, "Question", tmp_path / "runs")
    assert state["status"] == "failed" and state["error"] == "ValueError: Source inspection failed"
    root, = spans.get_finished_spans()
    assert root.status.status_code == StatusCode.ERROR
    assert root.attributes["modellm.run_id"] == state["run_id"]
    assert json.loads(root.attributes["langfuse.observation.output"]) == {
        "status": "failed", "chart_count": 0, "error": state["error"],
    }
    assert json.loads(root.attributes["langfuse.trace.metadata.cost_experiments"]) == state["cost_experiments"]
    client.get_trace_url.assert_called_once_with(trace_id=f"{root.context.trace_id:032x}")
    client.set_current_trace_as_public.assert_not_called()
    saved = (Path(state["run_dir"]) / "result.json").read_text()
    assert json.loads(saved)["langfuse_trace_url"] == state["langfuse_trace_url"]
    assert "sk-lf-not-for-artifacts" not in saved and "pk-lf-not-for-artifacts" not in saved
    assert "langfuse_trace_id" not in state


def test_unconfigured_tracing_is_noop_and_flush_requests_timeout(monkeypatch):
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.NoOpTracerProvider())
    with telemetry.workflow_span("local") as span:
        assert not span.is_recording()
        with telemetry.stage_span("local", "analysis") as attributes:
            assert attributes == {"modellm.run_id": "local", "modellm.stage": "analysis"}
    telemetry.flush_telemetry()

    timeouts = []

    def fail_flush(*, timeout_millis):
        timeouts.append(timeout_millis)
        raise OSError("Exporter unavailable")

    monkeypatch.setattr(trace, "get_tracer_provider", lambda: SimpleNamespace(force_flush=fail_flush))
    telemetry.flush_telemetry()
    assert timeouts == [1000]


def test_disabled_recording_never_initializes_client_even_with_keys(monkeypatch):
    constructor = Mock(side_effect=AssertionError("Disabled recording must not initialise"))
    monkeypatch.setattr("langfuse.Langfuse", constructor)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-unused")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-unused")
    telemetry.configure_langfuse()
    assert telemetry.current_langfuse_trace_url() is None
    constructor.assert_not_called()


@pytest.mark.parametrize("missing", ["LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL", None])
def test_setup_is_optional_and_warns_without_credentials(missing, monkeypatch, caplog):
    constructor = Mock(side_effect=OSError("sk-lf-private-error-value"))
    monkeypatch.setattr("langfuse.Langfuse", constructor)
    monkeypatch.setenv("MODELLM_LANGFUSE_ENABLED", "true")
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.setenv(name, "configured")
    if missing:
        monkeypatch.delenv(missing)
    telemetry.configure_langfuse()
    assert telemetry._langfuse_client is None
    assert "Langfuse" in caplog.text and "sk-lf-private-error-value" not in caplog.text
    assert constructor.call_count == (0 if missing else 1)


def test_setup_is_once_per_worker_with_short_timeout(monkeypatch):
    constructor = Mock()
    monkeypatch.setattr("langfuse.Langfuse", constructor)
    monkeypatch.setenv("MODELLM_LANGFUSE_ENABLED", "true")
    for name in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_BASE_URL"):
        monkeypatch.setenv(name, "configured")
    telemetry.configure_langfuse()
    telemetry.configure_langfuse()
    constructor.assert_called_once_with(timeout=2, should_export_span=telemetry._export_span)


@pytest.mark.parametrize("failure", ["lookup", "publication"])
def test_trace_link_failure_does_not_fail_workflow(spans, tmp_path, monkeypatch, caplog, failure):
    client = Mock()
    if failure == "publication":
        client.set_current_trace_as_public.side_effect = OSError("private-auth-value")
    else:
        client.get_trace_url.side_effect = OSError("private-auth-value")
    monkeypatch.setattr(telemetry, "_langfuse_client", client)
    monkeypatch.setattr(telemetry, "_langfuse_public", failure == "publication")
    paths = [tmp_path / name for name in ("accounts.csv", "events.csv")]
    for path in paths:
        path.write_text("id\n1\n")

    def reject_sources(*args):
        raise ValueError("Expected validation failure")

    monkeypatch.setattr(workflow, "_prepare_sources", reject_sources)
    state = workflow.run(paths, "Question", tmp_path / "runs")
    assert state["error"] == "ValueError: Expected validation failure"
    assert "langfuse_trace_url" not in state
    assert "private-auth-value" not in caplog.text


def test_non_recording_span_cannot_be_linked_or_published(monkeypatch):
    client = Mock()
    monkeypatch.setattr(telemetry, "_langfuse_client", client)
    monkeypatch.setattr(telemetry, "_langfuse_public", True)
    monkeypatch.setattr(trace, "get_tracer_provider", lambda: trace.NoOpTracerProvider())
    with telemetry.workflow_span("disabled"):
        assert telemetry.current_langfuse_trace_url() is None
    client.set_current_trace_as_public.assert_not_called()
    client.get_trace_url.assert_not_called()
