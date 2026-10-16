from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from src import agent_runtime as agents


class Response(BaseModel):
    value: str


@pytest.mark.parametrize(
    ("override", "expected"),
    [
        (None, "au.anthropic.claude-sonnet-4-6"),
        ("au.anthropic.claude-haiku-4-5-20251001-v1:0", "au.anthropic.claude-haiku-4-5-20251001-v1:0"),
        ("test.model-id", "test.model-id"),
    ],
)
def test_selected_model_reaches_strands(monkeypatch, override, expected):
    import strands
    import strands.models

    if override is None:
        monkeypatch.delenv("MODELLM_BEDROCK_MODEL_ID", raising=False)
    else:
        monkeypatch.setenv("MODELLM_BEDROCK_MODEL_ID", override)

    selected = []

    class FakeBedrockModel:
        def __init__(self, *, model_id, temperature, **kwargs):
            selected.append((model_id, temperature))
            self.client = SimpleNamespace(meta=SimpleNamespace(region_name="test-region"))

    class FakeAgent:
        def __init__(self, *, model, system_prompt, tools, **kwargs):
            assert model is not None

        async def stream_async(self, prompt, *, structured_output_model):
            yield {"result": SimpleNamespace(
                structured_output=structured_output_model(value="ok"),
                metrics=SimpleNamespace(get_summary=lambda: {"input_tokens": 1}),
            )}

    monkeypatch.setattr(strands.models, "BedrockModel", FakeBedrockModel)
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    events = []

    result = agents.invoke("prompt", Response, "test", lambda *event: events.append(event), "system")

    assert result.value == "ok"
    assert selected == [(expected, 0.1)]
    assert events[0][:3] == ("test", "model_call", f"Calling {expected}")
    assert expected in events[-1][2]


@pytest.mark.parametrize("prod", ["true", "false"])
def test_stage_defaults_and_explicit_haiku_override(monkeypatch, prod):
    monkeypatch.setenv("MODELLM_PROD", prod)
    monkeypatch.delenv("MODELLM_BEDROCK_MODEL_ID", raising=False)
    monkeypatch.delenv("MODELLM_PLOTTING_MODEL_ID", raising=False)
    main_stages = ("conceptual modelling", "generating dbt models",
                   "analytical interpretation", "analytical finding")
    for stage in main_stages:
        assert agents.configured_model_id(stage) == "au.anthropic.claude-sonnet-4-6"
    assert agents.configured_model_id("visualisation generation") == "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setenv("MODELLM_BEDROCK_MODEL_ID", "au.anthropic.claude-haiku-4-5-20251001-v1:0")
    for stage in main_stages:
        assert agents.configured_model_id(stage) == "au.anthropic.claude-haiku-4-5-20251001-v1:0"
    monkeypatch.setenv("MODELLM_PLOTTING_MODEL_ID", "au.anthropic.claude-sonnet-4-6")
    assert agents.configured_model_id("visualisation generation") == "au.anthropic.claude-sonnet-4-6"
