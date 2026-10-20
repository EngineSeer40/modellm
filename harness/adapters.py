"""Process-local adapters around production bindings. Nothing is patched on import."""

import copy
import importlib
import inspect
import threading
from collections import deque
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

import pandas as pd

from src import agents, agent_runtime, analysis, visualisation, workflow
from src.visualisation import PlotValidationError

from .io import digest, identifier, now, write
from .replay import BoundedMocks, ScriptedModel, UnmatchedMock


def factory(reference):
    module, separator, name = reference.partition(":")
    if not separator or not module.startswith("harness."):
        raise ValueError("Factory references must be harness.module:callable")
    value = getattr(importlib.import_module(module), name)
    if not callable(value):
        raise ValueError(f"Not callable: {reference}")
    return value


def frame_hash(frame):
    return digest({"columns": list(frame.columns), "dtypes": [str(t) for t in frame.dtypes],
                   "rows": frame.astype(object).where(pd.notna(frame), None).to_dict("records")})


def classify_failure(exc):
    from botocore.exceptions import ClientError, BotoCoreError
    from src.agent_runtime import InvocationLimitError
    current = exc
    seen = set()
    while current and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, ClientError):
            return {"category": "provider", "code": current.response.get("Error", {}).get("Code"),
                    "type": type(exc).__name__, "message": str(exc)}
        if isinstance(current, (BotoCoreError, TimeoutError, ConnectionError)):
            return {"category": "infrastructure", "type": type(exc).__name__, "message": str(exc)}
        current = current.__cause__ or current.__context__
    category = ("local_limit" if isinstance(exc, InvocationLimitError) else
                "encoding" if isinstance(exc, PlotValidationError) else
                "unmatched_mock" if isinstance(exc, UnmatchedMock) else
                "storage" if isinstance(exc, OSError) else "application")
    if any(s in str(exc) for s in ("Internal model calls", "cost budget", "deadline", "provider usage or pricing")):
        category = "global_budget"
    return {"category": category, "type": type(exc).__name__, "message": str(exc)}


@contextmanager
def stage_adapters(plan, record, sidecar):
    import strands.models
    from strands.tools.structured_output.structured_output_tool import StructuredOutputTool
    original_invoke = agents.invoke
    original_analyse, original_plot = agents.analyse, agents.plot
    original_selection = workflow.representation_selections
    original_validate = visualisation.validate_plot_candidate
    original_semantics = analysis.validate_result_semantics
    original_model = strands.models.BedrockModel
    scripts = deque(plan.script)
    stage_context = {}
    lock = threading.RLock()
    path = record.workspace.run_dir / "boundary.json"

    def save():
        with lock:
            write(path, sidecar)

    def capture(kind, payload):
        # Native runtime call IDs are linked through the emitter below.
        with lock:
            sidecar["captures"].append({"kind": kind, "at": now(),
                "invocation_id": stage_context.get("invocation_id"), "payload": copy.deepcopy(payload)})
            save()

    def analyse(*args, **kwargs):
        stage_context.clear()
        stage_context.update(stage="analysis")
        value = original_analyse(*args, **kwargs)
        sidecar["proposals"].append(value.model_dump())
        save()
        return value

    def plot(spec, result, emit, database, selection=None, **kwargs):
        stage_context.clear()
        stage_context.update(stage="plotting", spec=spec, result=result, frame=kwargs["frame"],
                             selection=selection or [], title=kwargs.get("title", spec.title))
        fingerprint = digest([result.result_id, [s.model_dump() for s in selection or []]])
        view = next((v for v in sidecar["schedule"] if v["selection_fingerprint"] == fingerprint), None)
        attempt = {"view_id": view["view_id"] if view else None, "result_id": result.result_id,
            "acceptance_id": result.acceptance_id, "selection": [s.model_dump() for s in selection or []],
            "title": stage_context["title"], "frame_hash": frame_hash(kwargs["frame"]), "status": "dispatched"}
        sidecar["plot_calls"].append(attempt)
        save()
        try:
            candidate = original_plot(spec, result, emit, database, selection=selection, **kwargs)
            attempt.update(status="returned", plot=candidate.model_dump())
            return candidate
        except Exception as exc:
            attempt.update(status="failed", failure=classify_failure(exc))
            raise
        finally:
            attempt["invocation_id"] = stage_context.get("invocation_id")
            save()

    def selections(database, spec, result):
        schedule = original_selection(database, spec, result)
        from src.analysis import load_result_frame
        from src.database import connect
        from src.visualisation import filter_frame
        full_frame = load_result_frame(database, result)
        counts = {}
        if spec.variant_field:
            field = '"' + spec.variant_field.replace('"', '""') + '"'
            with connect(database, read_only=True) as con:
                for value, count in con.execute(f"SELECT {field}, count(*) FROM {result.relation} GROUP BY {field}").fetchall():
                    scalar = value if value is None or isinstance(value, (str, int, float, bool)) else str(value)
                    counts[digest(scalar)] = count
        chosen = []
        for ordinal, (label, selection) in enumerate(schedule):
            fp = digest([result.result_id, [s.model_dump() for s in selection]])
            view = {"view_id": digest([plan.trial_id, fp]), "selection_fingerprint": fp,
                "result_id": result.result_id, "acceptance_id": result.acceptance_id,
                "concept_id": spec.concept_id, "relation": result.relation, "ordinal": ordinal,
                "label": label, "title": spec.title if label is None else f"{spec.title} — {label}",
                "selection": [s.model_dump() for s in selection],
                "selected": plan.view_ordinal is None or plan.view_ordinal == ordinal}
            key = selection[0].values[0] if selection and selection[0].values else None
            view["sql_selected_rows"] = counts[digest(key)] if selection else result.row_count
            view["frame_selected_rows"] = len(filter_frame(full_frame, selection))
            if not any(v["view_id"] == view["view_id"] for v in sidecar["schedule"]):
                sidecar["schedule"].append(view)
            if view["selected"]:
                chosen.append((label, selection))
        save()
        if plan.view_ordinal is not None and not chosen:
            raise ValueError("Requested view ordinal is not in the exact schedule")
        return chosen

    def summaries(database, names):
        if Path(database).resolve() != record.workspace.database.resolve() or set(names) != set(plan.fixture.model_names):
            raise ValueError("Summary request does not match frozen upstream bundle")
        return copy.deepcopy(plan.fixture.summaries)

    def validate(frame, candidate, result, selection, title):
        figure = original_validate(frame, candidate, result, selection, title)
        if plan.variant.plotting_validator:
            try:
                factory(plan.variant.plotting_validator)(frame, candidate, result, selection, figure)
            except (ValueError, TypeError, KeyError, NotImplementedError) as exc:
                raise PlotValidationError(str(exc)) from exc
        return figure

    def semantics(spec, result):
        original_semantics(spec, result)
        if plan.variant.acceptance_validator:
            factory(plan.variant.acceptance_validator)(spec, result)

    def invoke(prompt, schema, stage, emit, system_prompt, tools=None, **kwargs):
        stage_key = "plotting" if stage == "visualisation generation" else "analysis"
        invocation_id = identifier("invocation")
        stage_context["invocation_id"] = invocation_id
        stage_context["output_name"] = schema.__name__
        prompt = copy.deepcopy(prompt)
        original_payload = copy.deepcopy(prompt)
        system_prompt = plan.variant.system_prompts.get(stage_key, system_prompt)
        if plan.variant.context_factory:
            prompt = factory(plan.variant.context_factory)(prompt, stage_context)
        if plan.variant.context_layout == "flat" and isinstance(prompt, list):
            import json
            merged = {}
            for block in prompt:
                values = json.loads(block["text"])
                if merged.keys() & values.keys():
                    raise ValueError("Cannot flatten colliding context keys")
                merged.update(values)
            prompt = json.dumps(merged, default=str)
        if plan.variant.schema_factory:
            candidate = factory(plan.variant.schema_factory)(schema, stage_context)
            if not isinstance(candidate, type) or not issubclass(candidate, schema):
                raise ValueError("Schema factory must extend the supplied production/scoped class")
            if set(candidate.model_fields) != set(schema.model_fields) and not plan.variant.context_factory:
                raise ValueError("New contract fields require an explicit downstream context adapter")
            schema = candidate
        stage_context["output_name"] = schema.__name__
        if stage_key == "plotting":
            kwargs["limits"] = {"turns": plan.variant.plotting_turns}
        if plan.backend == "scripted" and scripts and scripts[0].get("tool_mocks"):
            from strands import tool
            mocks = BoundedMocks(plan.fixture_hash, scripts[0]["tool_mocks"])
            mocked_names = {m["tool"] for m in scripts[0]["tool_mocks"]}
            wrapped = []
            for original in tools or []:
                spec = original.tool_spec
                if spec["name"] not in mocked_names:
                    wrapped.append(original)
                    continue
                def make_mock(tool_spec, original_tool):
                    import inspect
                    def answer(**arguments):
                        try:
                            response = mocks.answer(plan.fixture_hash, tool_spec["name"], arguments)
                            capture("mock_match", {"tool": tool_spec["name"], "input": arguments})
                            return response
                        except UnmatchedMock:
                            capture("mock_mismatch", {"tool": tool_spec["name"], "input": arguments})
                            raise
                    # Strands validates Python parameters before invoking the
                    # tool, even when an explicit JSON schema is supplied.
                    answer.__signature__ = inspect.signature(original_tool.__wrapped__, eval_str=True)
                    return tool(name=tool_spec["name"], description=tool_spec["description"],
                                inputSchema=tool_spec["inputSchema"]["json"])(answer)
                wrapped.append(make_mock(spec, original))
            tools = wrapped
        invocation = {"invocation_id": invocation_id, "stage": stage_key, "original_prompt": original_payload,
            "prompt": prompt, "system_prompt": system_prompt,
            "tool_specs": [t.tool_spec for t in tools or []],
            "output_tool_spec": StructuredOutputTool(schema).tool_spec,
            "limits": kwargs.get("limits"), "cache_policy": plan.variant.cache_policy}
        invocation["context_hash"] = digest(prompt)
        invocation["prefix_hash"] = digest([system_prompt, invocation["tool_specs"], invocation["output_tool_spec"],
            prompt[0] if isinstance(prompt, list) else prompt])
        sidecar["invocations"].append(invocation)
        save()

        def emit_linked(*args, **kw):
            if len(args) >= 4 and args[1] == "model_call" and args[3].get("status") == "started":
                invocation["call_id"] = args[3]["call_id"]
                save()
            return emit(*args, **kw)

        return original_invoke(prompt, schema, stage, emit_linked, system_prompt, tools,
                               **kwargs, cache_policy=plan.variant.cache_policy)

    class LocalProvider(ScriptedModel):
        def __init__(self, **kwargs):
            if not scripts:
                raise UnmatchedMock("No scripted invocation remains")
            script = scripts.popleft()
            if script.get("stage") != stage_context["stage"]:
                raise UnmatchedMock("Scripted invocation stage does not match executed stage")
            super().__init__(script["responses"], stage_context["output_name"], kwargs["region_name"], capture)

    class RecordingProvider(original_model):
        def __init__(self, **kwargs):
            from dataclasses import asdict, is_dataclass
            config = {k: v for k, v in kwargs.items() if k not in {"boto_client_config", "cache_config"}}
            if "boto_client_config" in kwargs:
                config["timeouts_and_retries"] = {k: getattr(kwargs["boto_client_config"], k) for k in ("read_timeout", "connect_timeout", "retries")}
            cache = kwargs.get("cache_config")
            config["cache_config"] = asdict(cache) if is_dataclass(cache) else cache.model_dump() if hasattr(cache, "model_dump") else None
            capture("model_configuration", config)
            super().__init__(**kwargs)
            owning_invocation = stage_context.get("invocation_id")

            def parameters(params, **unused):
                with lock:
                    sidecar["captures"].append({"kind": "provider_dispatch", "at": now(),
                        "invocation_id": owning_invocation, "payload": copy.deepcopy(params)})
                    save()

            def sending(**unused):
                with lock:
                    sidecar["captures"].append({"kind": "provider_attempt", "at": now(),
                        "invocation_id": owning_invocation, "payload": {}})
                    save()

            for operation in ("ConverseStream", "Converse"):
                self.client.meta.events.register(f"before-parameter-build.bedrock-runtime.{operation}", parameters)
                self.client.meta.events.register(f"before-send.bedrock-runtime.{operation}", sending)

        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            capture("sdk_request", {"messages": messages, "tool_specs": tool_specs,
                "system_prompt": system_prompt, **{k: v for k, v in kwargs.items() if k in {"tool_choice", "system_prompt_content"}}})
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                if "metadata" in event:
                    capture("provider_metadata", event["metadata"])
                yield event

    with ExitStack() as stack:
        for module, name, adapter in (
            (agents, "invoke", invoke), (agents, "analyse", analyse), (agents, "plot", plot),
            (workflow, "summarize_models", summaries), (workflow, "representation_selections", selections),
            (visualisation, "validate_plot_candidate", validate), (workflow, "validate_plot_candidate", validate),
            (analysis, "validate_result_semantics", semantics),
            (strands.models, "BedrockModel", LocalProvider if plan.backend == "scripted" else RecordingProvider),
        ):
            stack.enter_context(patch.object(module, name, adapter))
        # Fail closed even if future runtime code attempts a different AWS client path.
        if plan.backend != "live":
            stack.enter_context(patch("botocore.session.Session.create_client", side_effect=AssertionError("Offline harness cannot create AWS clients")))
        yield sidecar
