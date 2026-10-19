import copy
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.adapters import classify_failure
from harness.contracts import AuthorizationRecord
from harness.io import now, read, seal, write
from harness.replay import ScriptedModel
from harness.runner import load_plan, prepare_trial, run_worker


def test_unmatched_recorded_query_produces_explicit_failed_check(fresh):
    script = copy.deepcopy(fresh["script"][:1])
    script[0]["tool_mocks"] = [{"tool": "query_data", "input": {"sql": "SELECT 99"}, "response": {"rows": [{"n": 99}]}}]
    trial = prepare_trial(fresh["root"], fresh["id"], mode="analysis", script=script)
    result = run_worker(trial["plan"])
    assert result.provider_dispatches == 0
    assert next(c for c in result.checks if c.check_id == "tool_mock_scope").status == "fail"
    boundary = read(Path(trial["plan"]).parent / "boundary.json")
    assert any(c["kind"] == "mock_mismatch" for c in boundary["captures"])


def test_dispatch_capture_with_fake_transport_and_unknown_cache_keys(fresh, monkeypatch):
    import strands.models

    class Events:
        def __init__(self):
            self.callbacks = {}
        def register(self, key, callback):
            self.callbacks[key] = callback

    class FakeTransport(ScriptedModel):
        def __init__(self, **kwargs):
            super().__init__([{"input": fresh["proposal"]}], "AnalyticalPlan", kwargs["region_name"], lambda *a: None)
            self.client.meta.events = Events()

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            handlers = self.client.meta.events.callbacks
            handlers["before-parameter-build.bedrock-runtime.ConverseStream"](
                params={"modelId": "fake-local-test", "messages": messages, "toolConfig": {"tools": tool_specs}})
            for _ in range(2):
                handlers["before-send.bedrock-runtime.ConverseStream"]()
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event

    monkeypatch.setattr(strands.models, "BedrockModel", FakeTransport)
    monkeypatch.setattr("botocore.session.Session.create_client", lambda *a, **kw: pytest.fail("No actual AWS client allowed in transport test"))
    approval = fresh["root"] / "synthetic-test-approval.json"
    trial = prepare_trial(fresh["root"], fresh["id"], mode="analysis", backend="live", authorization=str(approval))
    seal(approval, AuthorizationRecord(subject_hash=trial["plan_hash"], user_approval="Synthetic test record for mocked transport only",
        approval_reference="offline unit test; no actual provider", created_at=now()))
    plan, _ = load_plan(trial["plan"])
    folder = Path(trial["plan"]).parent
    write(folder / "dispatch.json", {"plan_hash": trial["plan_hash"], "settings": plan.settings})
    write(folder / "running.json", {"fixture_id": fresh["id"]})
    result = run_worker(trial["plan"])
    assert result.execution == "finished", result.failure
    assert result.provider_dispatches == 1 and result.provider_attempts == 2
    assert result.cache["status"] == "not_observed" and result.cache["read_tokens"] is None
    boundary = read(folder / "boundary.json")
    dispatch = next(c["payload"] for c in boundary["captures"] if c["kind"] == "provider_dispatch")
    assert any(t["name"] == "AnalyticalPlan" for t in dispatch["toolConfig"]["tools"])


def test_structured_provider_failures_remain_distinct():
    from botocore.exceptions import ClientError
    from src.agent_runtime import InvocationLimitError
    provider = ClientError({"Error": {"Code": "ServiceUnavailableException", "Message": "Test outage"}}, "ConverseStream")
    assert classify_failure(provider)["category"] == "provider"
    assert classify_failure(provider)["code"] == "ServiceUnavailableException"
    assert classify_failure(InvocationLimitError("local"))["category"] == "local_limit"
    assert classify_failure(OSError("disk"))["category"] == "storage"
    assert classify_failure(RuntimeError("Reported model cost budget exhausted"))["category"] == "global_budget"
