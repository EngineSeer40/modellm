import json
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from src import agent_runtime
from src.config import ReleaseSettings, use_settings
from harness.replay import ScriptedModel, BoundedMocks, UnmatchedMock


class Response(BaseModel):
    value: str


@pytest.mark.parametrize("stage", ["analytical interpretation", "visualisation generation"])
@pytest.mark.parametrize("policy", ["default", "disabled"])
def test_cache_switch_preserves_context_and_records_effective_policy(monkeypatch, stage, policy):
    import strands.models
    configs, requests, events = [], [], []

    class Local(ScriptedModel):
        def __init__(self, **kwargs):
            configs.append(kwargs)
            super().__init__([{"input": {"value": "ok"}}], "Response", kwargs["region_name"], lambda k, v: requests.append(v))

    monkeypatch.setattr(strands.models, "BedrockModel", Local)
    prompt = [{"text": "stable"}, {"text": "dynamic"}]
    with use_settings(ReleaseSettings()):
        result = agent_runtime.invoke(prompt, Response, stage, lambda *e: events.append(e), "instructions",
                                      cost_experiments=frozenset(), cache_policy=policy)
    assert result.value == "ok"
    assert ("cache_config" in configs[0]) == (policy == "default")
    assert ("cachePoint" in json.dumps(requests[0])) == (policy == "default")
    assert prompt == [{"text": "stable"}, {"text": "dynamic"}]
    started = next(e[3] for e in events if e[1] == "model_call" and e[3]["status"] == "started")
    assert started["cache_policy"] == policy
    assert started["prompt_cache"]["enabled"] == (policy == "default")


def test_invalid_cache_policy_fails_before_provider():
    with pytest.raises(ValueError, match="Unknown cache policy"):
        agent_runtime.invoke("hi", Response, "analysis", lambda *a: None, "hi", cache_policy="unknown")
    with pytest.raises(ValueError, match="explicit cache checkpoints"):
        agent_runtime.invoke([{"cachePoint": {"type": "default"}}], Response, "analysis", lambda *a: None, "hi", cache_policy="disabled")


def test_recorded_mocks_are_exact_and_sequential():
    mock = BoundedMocks("fixture", [{"tool": "query_data", "input": {"sql": "SELECT 1"}, "response": {"rows": [{"x": 1}]}}])
    with pytest.raises(UnmatchedMock):
        mock.answer("fixture", "query_data", {"sql": "SELECT 2"})
    with pytest.raises(UnmatchedMock):
        mock.answer("another", "query_data", {"sql": "SELECT 1"})
    assert mock.answer("fixture", "query_data", {"sql": "SELECT 1"})["rows"][0]["x"] == 1
    with pytest.raises(UnmatchedMock):
        mock.answer("fixture", "query_data", {"sql": "SELECT 1"})
