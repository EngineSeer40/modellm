"""Cost controls exercise the real SDK boundaries without provider calls."""

import asyncio
import copy
import json
import re
from types import SimpleNamespace

import duckdb
import pytest
from pydantic import BaseModel, ValidationError
from strands import Agent
from strands.hooks.events import AfterToolCallEvent
from strands.models import Model
from strands.types.tools import ToolContext

from src import agents
from src.analysis import accept_analyses, render_plot, validate_plot_arguments
from src.context_cost import COST_EXPERIMENT_STAGES, configured_cost_experiments, tool_result_offload_plugins
from src.core.contracts import (
    AcceptedAnalysisResult, AnalysisFinding, AnalysisPlan, AnalysisSpec, AnalyticalPlan,
    EarlyValidatedAnalysisFinding, EvidenceRequest, FindingStatement, QueryDefinition,
    PlotSpec, registered_plot_templates,
)
from src.tools.analysis_tools import DuckDBAnalysisTools
from src.tools.result_tools import DuckDBResultTools, EvidenceRegistry, render_finding


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


@pytest.fixture
def analytical_spec():
    return AnalysisSpec(
        title="Recorded values", question="What values were recorded?", concept_id="values",
        population_definition="All recorded events", observation_rule=None,
        rationale="Retain event grain", grain="one row per event", dimensions=[], measures=[],
        visualisation_goal="Show the distribution", query=QueryDefinition(kind="direct", sql="SELECT * FROM fact_events"),
    )


@pytest.fixture
def evidence_and_finding():
    registry = EvidenceRegistry("accepted", 'analysis."result_00"')
    record = registry.register(EvidenceRequest(kind="rows", fields=["value"]),
                               [{"value": 12}], {"value": "INTEGER"}, False, False)
    finding = AnalysisFinding(statements=[FindingStatement(
        template="Recorded {value}.",
        selected_rows={"event": {"evidence_id": record["id"], "row_id": record["rows"][0]["row_id"]}},
        bindings={"value": {"row": "event", "column": "value"}},
    )], caveats=[])
    return registry, finding


@pytest.mark.parametrize("prod", ["true", "false"])
def test_controls_default_on_validate_and_honor_explicit_snapshot(monkeypatch, prod):
    monkeypatch.setenv("MODELLM_PROD", prod)
    monkeypatch.delenv("MODELLM_COST_EXPERIMENTS", raising=False)
    assert configured_cost_experiments() == frozenset(COST_EXPERIMENT_STAGES)
    monkeypatch.setenv("MODELLM_COST_EXPERIMENTS", "")
    assert configured_cost_experiments() == frozenset()
    monkeypatch.delenv("MODELLM_COST_EXPERIMENTS")
    assert configured_cost_experiments(frozenset()) == frozenset()
    monkeypatch.setenv("MODELLM_COST_EXPERIMENTS", " conceptual_cache, conceptual_cache,small_analysis_previews ")
    snapshot = configured_cost_experiments()
    assert snapshot == {"conceptual_cache", "small_analysis_previews"}
    monkeypatch.setenv("MODELLM_COST_EXPERIMENTS", "typo")
    assert configured_cost_experiments(snapshot) == snapshot
    with pytest.raises(ValueError, match="typo"):
        configured_cost_experiments()


def test_conceptual_controls_preserve_evidence_and_separate_only_repair_feedback(monkeypatch):
    captured = []
    monkeypatch.setattr(agents, "invoke", lambda *args, **kwargs: captured.append((args, kwargs)))
    plan = AnalyticalPlan(business_processes=[], dimensions=[], facts=[], assumptions=[], unresolved_questions=[])
    args = ("request", {"events": {"columns": []}}, [], lambda *args: None)
    agents.conceptual(*args, previous=plan, cost_experiments=frozenset())
    baseline = captured[-1][0]
    assert baseline[4] == agents.CONCEPTUAL_MODEL
    payload = json.loads(baseline[0])
    assert payload == {"request": "request", "profiles": args[1], "candidate_relationships": [],
                       "previous_plan_issues": [], "previous_plan": plan.model_dump()}
    agents.conceptual(*args, previous=plan, cost_experiments=frozenset({"conceptual_cache", "conceptual_guidance"}))
    enabled = captured[-1][0]
    stable, repair = [json.loads(block["text"]) for block in enabled[0]]
    assert {**stable, **repair} == payload
    assert "previous_plan" not in stable
    assert enabled[4] == agents.CONCEPTUAL_MODEL + agents.CONCEPTUAL_REFERENCE_GUIDANCE


def test_analysis_tool_selection_preserves_baseline_prompt(monkeypatch, analytical_spec, tmp_path):
    captured = []
    monkeypatch.setattr(agents, "invoke", lambda *args, **kwargs: captured.append((args, kwargs)))
    plan = AnalyticalPlan(business_processes=[], dimensions=[], facts=[], assumptions=[], unresolved_questions=[])
    for controls in (frozenset(), frozenset({"small_analysis_previews"})):
        agents.analyse("request", plan, {"fact_events": {"value": "INTEGER"}}, {}, lambda *a: None,
                       tmp_path / "models.duckdb", cost_experiments=controls)
    assert captured[0][0][0] == captured[1][0][0]
    assert captured[0][0][4] == agents.ANALYSIS
    assert captured[1][0][4].endswith(agents.ANALYSIS_PREVIEW_GUIDANCE)
    first, second = [item[1]["tools"][-1].tool_spec for item in captured]
    assert first["name"] == second["name"] == "query_data"
    assert first["inputSchema"]["json"]["properties"]["limit"]["default"] == 500
    assert second["inputSchema"]["json"]["properties"]["limit"]["default"] == 20


def test_small_preview_keeps_explicit_limits_and_complete_acceptance(tmp_path, analytical_spec):
    database = tmp_path / "models.duckdb"
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE fact_events AS SELECT range AS value FROM range(620)")
    tools = DuckDBAnalysisTools(database, ["fact_events"])
    sql = "SELECT * FROM fact_events ORDER BY value"
    assert tools.query_data(sql)["row_count"] == 500
    preview = tools.query_data_small_preview(sql)
    assert preview["row_count"] == 20 and preview["truncated"]
    assert tools.query_data_small_preview(sql, limit=100)["row_count"] == 100
    assert tools.query_data_small_preview(sql, limit=900)["row_count"] == 500
    assert tools.query_data_small_preview(sql + " LIMIT 3")["truncated"] is False
    accepted = accept_analyses(database, ["fact_events"], AnalysisPlan(
        analyses=[analytical_spec], qualifications=[], warnings=[],
    ))
    assert accepted[0][1].row_count == 620


@pytest.mark.parametrize("defect", ["extra_binding", "missing_binding", "unused_alias", "unknown_alias", "format", "conversion", "attribute", "braces"])
def test_early_validation_and_final_render_share_shape_rules(evidence_and_finding, defect):
    registry, finding = evidence_and_finding
    data = finding.model_dump()
    statement = data["statements"][0]
    if defect == "extra_binding": statement["bindings"]["extra"] = statement["bindings"]["value"]
    elif defect == "missing_binding": statement["bindings"] = {}
    elif defect == "unused_alias": statement["selected_rows"]["unused"] = statement["selected_rows"]["event"]
    elif defect == "unknown_alias": statement["bindings"]["value"]["row"] = "missing"
    else: statement["template"] = {"format": "{value:.2f}", "conversion": "{value!r}",
                                    "attribute": "{value.real}", "braces": "{value"}[defect]
    baseline = AnalysisFinding.model_validate(data)
    with pytest.raises(ValidationError): EarlyValidatedAnalysisFinding.model_validate(data)
    with pytest.raises(ValueError, match="Finding evidence validation failed"):
        render_finding(baseline, registry)


def test_valid_finding_renders_identically_but_still_checks_evidence(evidence_and_finding):
    registry, finding = evidence_and_finding
    early = EarlyValidatedAnalysisFinding.model_validate(finding.model_dump())
    assert render_finding(early, registry) == render_finding(finding, registry)
    missing_column = early.model_copy(deep=True)
    missing_column.statements[0].bindings["value"].column = "absent"
    assert EarlyValidatedAnalysisFinding.model_validate(missing_column.model_dump())
    with pytest.raises(ValueError, match="missing evidence column"):
        render_finding(missing_column, registry)


def test_finding_schema_is_selected_only_when_enabled(monkeypatch, analytical_spec, evidence_and_finding, tmp_path):
    registry, finding = evidence_and_finding
    captured = []
    monkeypatch.setattr(agents, "invoke", lambda *args, **kwargs: captured.append(args))
    db = DuckDBResultTools(tmp_path / "models.duckdb", registry.relation, {"value": "INTEGER"}, registry=registry)
    result = AcceptedAnalysisResult(result_id="result_00", relation=registry.relation, row_count=1,
                                    columns={"value": "INTEGER"}, profile=[])
    for controls in (frozenset(), frozenset({"early_finding_validation"})):
        agents.interpret(analytical_spec, result, lambda *a: None, db, previous=finding, cost_experiments=controls)
    assert captured[0][1] is AnalysisFinding
    assert captured[1][1] is EarlyValidatedAnalysisFinding
    assert captured[0][0] == captured[1][0]


def test_sdk_corrects_shape_in_same_conversation(evidence_and_finding):
    registry, finding = evidence_and_finding
    bad = finding.model_dump()
    bad["statements"][0]["bindings"]["extra"] = {"row": "event", "column": "value"}
    model = LocalModel([("EarlyValidatedAnalysisFinding", bad),
                        ("EarlyValidatedAnalysisFinding", finding.model_dump())])
    agent = Agent(model=model, callback_handler=None)
    async def run():
        events = [event async for event in agent.stream_async(
            "Correct the finding", structured_output_model=EarlyValidatedAnalysisFinding,
        )]
        return events[-1]["result"].structured_output
    accepted = asyncio.run(run())
    assert len(model.requests) == 2
    assert "bindings without placeholders ['extra']" in json.dumps(model.requests[1])
    assert render_finding(accepted, registry)[0] == "Recorded 12."


@pytest.mark.parametrize(("stage", "control", "tool_name"), [
    ("analytical interpretation", "offload_analysis", "query_data"),
    ("analytical finding", "offload_finding", "inspect_result"),
])
def test_sdk_offloads_only_selected_results_and_preserves_evidence(tmp_path, stage, control, tool_name):
    directory = tmp_path / "context" / "call"
    plugins = tool_result_offload_plugins(stage, frozenset({control}), directory)
    agent = Agent(model=LocalModel(), plugins=plugins, callback_handler=None)
    registry = EvidenceRegistry("accepted", 'analysis."result_00"')
    request = EvidenceRequest(kind="rows", fields=["value"], limit=100)
    record = registry.register(request, [{"value": f"row_{i}:" + "data " * 100} for i in range(100)],
                               {"value": "VARCHAR"}, False, False)
    original = registry.snapshot()

    async def inspect(tool, content, tool_id):
        event = AfterToolCallEvent(agent=agent, selected_tool=None,
                                   tool_use={"name": tool, "toolUseId": tool_id, "input": {}},
                                   invocation_state={}, result={"toolUseId": tool_id, "status": "success",
                                                                "content": [{"json": content}]})
        await agent.hooks.invoke_callbacks_async(event)
        return event.result

    async def run():
        small = await inspect(tool_name, {"value": 1}, "small")
        assert small["content"] == [{"json": {"value": 1}}]
        unrelated = await inspect("OtherTool", record, "unrelated")
        assert unrelated["content"] == [{"json": record}]
        result = await inspect(tool_name, record, "large")
        preview = result["content"][0]["text"]
        assert len(preview) < len(json.dumps(record))
        reference = re.search(r"\[Stored references:\]\n  (\S+)", preview)[1]
        context = ToolContext(tool_use={"name": "retrieve_offloaded_content", "toolUseId": "read", "input": {}},
                              agent=agent, invocation_state={})
        retrieved = await plugins[0].retrieve_offloaded_content(reference=reference, tool_context=context)
        recovered = retrieved["content"][0]["json"]
        assert recovered == record
        finding = EarlyValidatedAnalysisFinding(statements=[FindingStatement(
            template="Observed {value}.",
            selected_rows={"event": {"evidence_id": recovered["id"], "row_id": recovered["rows"][-1]["row_id"]}},
            bindings={"value": {"row": "event", "column": "value"}},
        )], caveats=[])
        assert render_finding(finding, registry)[0] == f"Observed {record['rows'][-1]['values']['value']}."
        selected = await plugins[0].retrieve_offloaded_content(
            reference=reference, tool_context=context, pattern="row_99:", context_lines=1,
        )
        assert "row_99:" in json.dumps(selected)

    asyncio.run(run())
    assert registry.snapshot() == original
    assert list(directory.rglob("*"))
    assert tool_result_offload_plugins(stage, frozenset(), directory) == []
    other = "offload_finding" if control == "offload_analysis" else "offload_analysis"
    assert tool_result_offload_plugins(stage, frozenset({other}), directory) == []


def test_invoke_records_controls_cache_boundary_and_plugins(monkeypatch, tmp_path):
    import strands
    import strands.models

    class Response(BaseModel):
        value: str

    captured = {}
    class FakeModel:
        def __init__(self, **kwargs):
            captured["model"] = kwargs
            self.client = SimpleNamespace(meta=SimpleNamespace(region_name="test"))

    class FakeAgent:
        def __init__(self, **kwargs): captured["agent"] = kwargs
        async def stream_async(self, prompt, *, structured_output_model):
            captured["prompt"] = prompt
            yield {"result": SimpleNamespace(structured_output=Response(value="ok"),
                                               metrics=SimpleNamespace(get_summary=lambda: {}))}

    monkeypatch.setattr(strands.models, "BedrockModel", FakeModel)
    monkeypatch.setattr(strands, "Agent", FakeAgent)
    monkeypatch.setenv("MODELLM_BEDROCK_MODEL_ID", "au.anthropic.claude-sonnet-4-6")
    for controls in (frozenset(), frozenset({"conceptual_cache"})):
        events = []
        prompt = [{"text": "stable"}, {"text": "repair"}]
        agents.invoke(prompt, Response, "conceptual modelling", lambda *e: events.append(e), "system",
                      cost_experiments=controls)
        assert ("cache_config" in captured["model"]) == bool(controls)
        assert captured["prompt"] == (prompt[:1] + [{"cachePoint": {"type": "default"}}] + prompt[1:] if controls else prompt)
        assert "plugins" not in captured["agent"]
        for e in events:
            if e[1] == "model_call":
                assert e[3]["cost_experiments"] == sorted(controls)
                assert e[3]["strands_version"]
    # Existing stages keep caching when all experiments are disabled.
    agents.invoke("prompt", Response, "analytical interpretation", lambda *a: None, "system", cost_experiments=frozenset())
    assert "cache_config" in captured["model"]
    agents.invoke("prompt", Response, "analytical interpretation", lambda *a: None, "system",
                  cost_experiments=frozenset({"offload_analysis"}), context_directory=tmp_path / "context")
    assert len(captured["agent"]["plugins"]) == 1
    # Use a provider ID without a Claude marker to verify the cache support guard.
    monkeypatch.setenv("MODELLM_BEDROCK_MODEL_ID", "test.provider")
    agents.invoke("prompt", Response, "conceptual modelling", lambda *a: None, "system",
                  cost_experiments=frozenset({"conceptual_cache"}))
    assert "cache_config" not in captured["model"]


def test_workflow_snapshots_controls_across_repairs(monkeypatch, tmp_path):
    from src import workflow
    from src.core.contracts import ValidationIssue

    monkeypatch.setenv("MODELLM_COST_EXPERIMENTS", "conceptual_guidance")
    monkeypatch.setattr(workflow, "cleanup_run_workspaces", lambda *a, **kw: {})
    monkeypatch.setattr(workflow, "provenance", lambda paths: {})
    monkeypatch.setattr(workflow, "load_raw", lambda *a: ["events"])
    monkeypatch.setattr(workflow, "profile", lambda *a: {})
    monkeypatch.setattr(workflow, "relationships", lambda *a: [])
    monkeypatch.setattr(workflow, "record_run", lambda *a: None)
    plan = AnalyticalPlan(business_processes=[], dimensions=[], facts=[], assumptions=[], unresolved_questions=[])
    observed = []
    def conceptual(*args, **kwargs):
        observed.append(kwargs["cost_experiments"])
        monkeypatch.setenv("MODELLM_COST_EXPERIMENTS", "offload_analysis")
        return plan
    monkeypatch.setattr(agents, "conceptual", conceptual)
    def issues(*args):
        return [ValidationIssue(boundary="conceptual", code="test", path="models", message="repair")]
    monkeypatch.setattr(workflow, "plan_issues", issues)
    inputs = [tmp_path / "events.csv", tmp_path / "accounts.csv"]
    for path in inputs:
        path.write_text("id\n1\n")
    state = workflow.run(inputs, "request", tmp_path / "runs")
    assert state["status"] == "failed"  # Deliberately exhaust the repair budget without model calls.
    assert observed == [frozenset({"conceptual_guidance"})] * 2
    saved = json.loads((tmp_path / "runs" / state["run_id"] / "result.json").read_text())
    assert saved["cost_experiments"] == ["conceptual_guidance"] and saved["strands_version"]


@pytest.mark.parametrize("template", ["plotly_light", "", 42, True])
def test_unknown_template_is_rejected_before_rendering(template):
    with pytest.raises(ValidationError, match="Unknown Plotly template"):
        PlotSpec(function="scatter_map", columns={"lat": "latitude", "lon": "longitude"},
                 filters=[], options={"template": template})


def test_plot_prompt_lists_live_registry_once(monkeypatch, analytical_spec, tmp_path):
    import plotly.io as pio

    captured = {}
    def invoke(prompt, *args, **kwargs):
        captured.update(json.loads(prompt))
    monkeypatch.setattr(agents, "invoke", invoke)
    pio.templates["test_registered_template"] = pio.templates["plotly_white"]
    try:
        agents.plot(analytical_spec, AcceptedAnalysisResult(
            result_id="result_00", relation='analysis."result_00"', row_count=1,
            columns={"value": "INTEGER"}, profile=[],
        ), {"text": "Recorded values", "evidence": []}, lambda *args: None, tmp_path / "models.duckdb")
        assert captured["renderer_templates"] == registered_plot_templates()
        assert "test_registered_template" in captured["renderer_templates"]
        assert "plotly_light" not in captured["renderer_templates"]
        assert PlotSpec(function="bar", columns={"x": "value"}, filters=[],
                        options={"template": "test_registered_template"})
    finally:
        del pio.templates["test_registered_template"]


def test_sdk_corrects_plot_template_then_renders_without_fresh_invocation():
    import pandas as pd

    bad = {"function": "scatter_map", "columns": {"lat": "latitude", "lon": "longitude", "size": "revenue"},
           "filters": [], "options": {"template": "plotly_light"}}
    good = {**bad, "options": {"template": "plotly_white"}}
    model = LocalModel([("PlotSpec", bad), ("PlotSpec", good)])
    agent = Agent(model=model, callback_handler=None)
    async def run():
        events = [event async for event in agent.stream_async("Plot the store map", structured_output_model=PlotSpec)]
        return events[-1]["result"].structured_output
    plot = asyncio.run(run())
    assert len(model.requests) == 2
    assert "Unknown Plotly template: 'plotly_light'" in json.dumps(model.requests[1])
    assert "plotly_white" in json.dumps(model.requests[1])
    frame = pd.DataFrame({"latitude": [-33.86], "longitude": [151.21], "revenue": [10]})
    assert len(render_plot(frame, plot, "Store map").data) == 1
    # Omission still allows Plotly's default, and runtime checks guard bypassed model validation.
    assert len(render_plot(frame, PlotSpec.model_validate({**good, "options": {}}), "Store map").data) == 1
    with pytest.raises(ValueError, match="Unknown Plotly template"):
        validate_plot_arguments(plot.model_copy(update={"options": bad["options"]}))
