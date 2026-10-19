"""Small scripted SDK model shared by offline tests."""

import copy
import json

from pydantic import BaseModel
from strands.models import Model


class Response(BaseModel):
    value: str


class LocalModel(Model):
    """Script tool submissions through the SDK; token counting stays local."""

    def __init__(self, submissions=()):
        self.submissions = iter(submissions)
        self.requests = []

    def get_config(self):
        return {"model_id": "local-test"}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("The structured-output tool should handle validation")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        name, payload = next(self.submissions)
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {"toolUse": {
            "toolUseId": f"submission_{len(self.requests)}", "name": name,
        }}}}
        yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(payload)}}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 10, "totalTokens": 20},
                            "metrics": {"latencyMs": 1}}}
