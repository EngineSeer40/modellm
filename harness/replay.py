"""Bounded local SDK responses and exact recorded tool mocks, never a provider fallback."""

import copy
import json
from collections import deque

from strands.models import Model

from .io import canonical


class UnmatchedMock(ValueError):
    pass


class BoundedMocks:
    def __init__(self, fixture_hash, records):
        self.fixture_hash = fixture_hash
        self.records = deque(copy.deepcopy(records))

    def answer(self, fixture_hash, tool, arguments):
        if fixture_hash != self.fixture_hash or not self.records:
            raise UnmatchedMock("No matching recorded tool response")
        expected = self.records[0]
        if expected["tool"] != tool or canonical(expected["input"]) != canonical(arguments):
            raise UnmatchedMock(f"Unmatched recorded request for {tool}; new queries need the real database")
        self.records.popleft()
        return copy.deepcopy(expected["response"])


def pointer(document, path):
    for part in path.strip("/").split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        document = document[int(part)] if isinstance(document, list) else document[part]
    return document


def extract_exchange(record, call_id):
    messages = [e["detail"]["message"] for e in record["events"]
                if e.get("kind") == "model_message" and e.get("detail", {}).get("call_id") == call_id]
    if not messages:
        raise ValueError(f"No recorded exchange for call {call_id}")
    return copy.deepcopy(messages)


class ScriptedModel(Model):
    """Emit local tool-use streams while Strands executes actual tools and validators."""

    def __init__(self, responses, output_name, region, capture):
        from types import SimpleNamespace
        self.responses = iter(copy.deepcopy(responses))
        self.output_name = output_name
        self.capture = capture
        self.client = SimpleNamespace(meta=SimpleNamespace(region_name=region))

    def get_config(self):
        return {"model_id": "harness-scripted"}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        raise AssertionError("Use the actual SDK structured-output tool")
        yield

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        import asyncio
        # Keep the offline loop wakeable while the SDK executes synchronous tools
        # on its worker threads. There is no network stream to wake this loop.
        if not hasattr(self, "_heartbeat"):
            loop = asyncio.get_running_loop()
            def wake():
                self._heartbeat = loop.call_later(0.05, wake)
            self._heartbeat = loop.call_later(0.05, wake)
        try:
            response = next(self.responses)
        except StopIteration as exc:
            raise UnmatchedMock("Script exhausted; no provider fallback exists") from exc
        self.capture("sdk_request", {"messages": messages, "tool_specs": tool_specs,
            "system_prompt": system_prompt, **{k: v for k, v in kwargs.items() if k in {"tool_choice", "system_prompt_content"}}})
        if response.get("error"):
            raise RuntimeError(response["error"])
        tools = response.get("tools", [response])
        yield {"messageStart": {"role": "assistant"}}
        for index, submission in enumerate(tools):
            name = submission.get("tool", "$output")
            if name == "$output":
                name = self.output_name
            yield {"contentBlockStart": {"start": {"toolUse": {
                "toolUseId": f"local_{len(messages)}_{index}", "name": name}}}}
            yield {"contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(submission["input"])}}}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}
        yield {"metadata": {"usage": {"inputTokens": 10, "outputTokens": 10, "totalTokens": 20},
                            "metrics": {"latencyMs": 1}}}


def script_from_exchange(messages, output_tools):
    """Extract submissions only; fresh validators/tools generate fresh feedback."""
    responses = []
    for message in messages:
        tools = []
        if message.get("role") != "assistant":
            continue
        for block in message.get("content", []):
            use = block.get("toolUse")
            if use:
                tools.append({"tool": "$output" if use["name"] in output_tools else use["name"], "input": use["input"]})
        if tools:
            responses.append({"tools": tools})
    return responses
