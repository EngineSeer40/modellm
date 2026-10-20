"""Sonnet 5/5.5 pricing and SDK request compatibility, without Bedrock calls."""

import json
from types import SimpleNamespace

import pytest

from src import agent_runtime as agents
from src.config import ReleaseSettings
from src.execution import RunBudget
from src.usage import estimate_usage_cost, summarize_usage
from local_model import LocalModel, Response


def test_cached_cost_covers_supported_model_aliases():
    usage = {"inputTokens": 100, "outputTokens": 50,
             "cacheReadInputTokens": 1000, "cacheWriteInputTokens": 200}
    cases = [
        ("au.anthropic.claude-sonnet-5", .00154),
        ("us.anthropic.claude-sonnet-5", .00154),
        ("global.anthropic.claude-sonnet-5", .00140),
        ("au.anthropic.claude-sonnet-5-5", .00143),
        ("us.anthropic.claude-sonnet-5-5", .00143),
        ("global.anthropic.claude-sonnet-5-5", .00130),
        ("us.anthropic.claude-haiku-4-5-20251001-v1:0", .00077),
    ]
    for model, expected in cases:
        region = "us-east-1" if model.startswith("us.") else "ap-southeast-2"
        assert estimate_usage_cost(model, region, usage) == pytest.approx(expected), model
    unknown_ttl = dict(usage, cacheDetails=[{"inputTokens": 200, "ttl": "1h"}])
    assert estimate_usage_cost(cases[0][0], "ap-southeast-2", unknown_ttl) is None
    assert estimate_usage_cost(cases[0][0], "ap-southeast-2", {}) is None
    assert estimate_usage_cost("unknown-model", "ap-southeast-2", usage) is None


@pytest.mark.parametrize(("model", "expected"), [
    ("global.anthropic.claude-sonnet-5", .00140),
    ("au.anthropic.claude-sonnet-5-5", .00143),
    ("us.anthropic.claude-haiku-4-5-20251001-v1:0", .00077),
])
def test_cached_cost_agrees_between_budget_and_report(monkeypatch, model, expected):
    monkeypatch.setenv("MODELLM_PROD", "true")
    monkeypatch.setenv("MODELLM_BEDROCK_MODEL_ID", model)
    if model.startswith("us."):
        monkeypatch.setenv("AWS_REGION", "us-east-1")
        monkeypatch.setenv("MODELLM_PLOTTING_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0")
    config = ReleaseSettings.from_env()
    assert config.model_id == model
    usage = {"inputTokens": 100, "outputTokens": 50,
             "cacheReadInputTokens": 1000, "cacheWriteInputTokens": 200}
    budget = RunBudget(config)
    budget.record_response({"metadata": {"usage": usage}}, model, config.region)
    assert not budget.usage_unknown
    assert budget.cost == pytest.approx(expected)
    events = [
        {"stage": "conceptual modelling", "kind": "model_call", "detail": {
            "status": "started", "call_id": "test", "model_id": model,
            "region": config.region, "prompt_cache": {"ttl": "5m"}}},
        {"stage": "conceptual modelling", "kind": "model_message", "detail": {
            "call_id": "test", "message": {"metadata": {"usage": usage}}}},
        {"stage": "conceptual modelling", "kind": "model_call", "detail": {
            "call_id": "test", "status": "completed", "accumulated_usage": usage}},
    ]
    report = summarize_usage(events)
    assert report["total"]["estimated_cost_usd"] == pytest.approx(expected)
    assert report["total"]["total_tokens"] == 1350


@pytest.mark.parametrize("model_id", [
    "us.anthropic.claude-sonnet-5", "au.anthropic.claude-sonnet-5-5",
])
def test_sonnet_keeps_sdk_correction_and_end_turn_fallback(monkeypatch, model_id):
    import strands.models

    captured = {}
    choices = []

    class FakeBedrockModel(LocalModel):
        def __init__(self, **kwargs):
            captured.update(kwargs)
            super().__init__([("Response", {"value": 23}), ("Response", {"value": "ok"})])
            self.client = SimpleNamespace(meta=SimpleNamespace(region_name=kwargs["region_name"]))
            self.first = True

        async def stream(self, messages, tool_specs=None, system_prompt=None, *, tool_choice=None, **kwargs):
            choices.append(tool_choice)
            if self.first:
                self.first = False
                yield {"messageStart": {"role": "assistant"}}
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": {"text": "Ready to provide the result."}}}
                yield {"contentBlockStop": {}}
                yield {"messageStop": {"stopReason": "end_turn"}}
                yield {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 10, "totalTokens": 20},
                                    "metrics": {"latencyMs": 1}}}
                return
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                yield event

    monkeypatch.setattr(strands.models, "BedrockModel", FakeBedrockModel)
    monkeypatch.setenv("MODELLM_BEDROCK_MODEL_ID", model_id)
    region = "us-east-1" if model_id.startswith("us.") else "ap-southeast-2"
    monkeypatch.setenv("AWS_REGION", region)
    events = []
    result = agents.invoke([{"text": "stable evidence"}, {"text": "repair context"}], Response,
                           "conceptual modelling", lambda *event: events.append(event), "Return a result")
    assert result.value == "ok"
    assert "temperature" not in captured
    assert "cache_config" in captured
    assert captured["use_native_token_count"] is False
    assert captured["region_name"] == region
    assert len(choices) == 3  # Initial text, invalid schema submission, corrected submission.
    if model_id.endswith("sonnet-5-5"):
        assert choices == [{"auto": {}}] * 3
    else:
        # Sonnet 5 accepts the SDK's forced-tool fallback without the 5.5 adapter.
        assert choices == [None, {"tool": {"name": "Response"}}, {"tool": {"name": "Response"}}]
    messages = [event[3]["message"] for event in events if event[1] == "model_message"]
    assert "Validation failed" in json.dumps(messages)
    started = next(event[3] for event in events if event[1] == "model_call")
    assert started["model_id"] == model_id
    assert started["region"] == region
    assert started["prompt_cache"]["enabled"]
    assert started["reasoning_effort"] == "high (provider default)"
