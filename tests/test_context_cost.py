"""Cost controls exercise the real SDK boundaries without provider calls."""

import asyncio
import copy
import json
import re
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from pydantic import BaseModel, ValidationError
from strands import Agent
from strands.hooks.events import AfterToolCallEvent
from strands.models import Model
from strands.types.tools import ToolContext

from src import agents, agent_runtime
from src.analysis import (
    accept_analyses,
)
from src.visualisation import (
    accepted_result_plot_schema, plot_capabilities, plot_descriptions, render_plot,
    validate_plot_arguments, validate_plot_spec,
)
from src.config import ReleaseSettings, use_settings
from src.execution import use_budget
from src.context_cost import COST_EXPERIMENT_STAGES, configured_cost_experiments, tool_result_offload_plugins
from src.core.contracts import (
    AcceptedAnalysisResult, AnalysisFinding, AnalysisPlan, AnalysisSpec, AnalyticalPlan,
    EarlyValidatedAnalysisFinding, EvidenceRequest, FindingStatement, QueryDefinition,
    PlotSpec, ResultFilter, registered_plot_templates,
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
    with use_settings(ReleaseSettings(max_analyses=5, max_charts=7)):
        for controls in (frozenset(), frozenset({"small_analysis_previews"})):
            agents.analyse("request", plan, {"fact_events": {"value": "INTEGER"}}, {}, lambda *a: None,
                           tmp_path / "models.duckdb", cost_experiments=controls)
    assert captured[0][0][0] == captured[1][0][0]
    assert captured[0][0][4] == agents.ANALYSIS
    assert captured[1][0][4].endswith(agents.ANALYSIS_PREVIEW_GUIDANCE)
    stable, repair = [json.loads(block["text"]) for block in captured[0][0][0]]
    assert stable["limits"] == {"max_analyses": 5, "max_charts": 7}
    assert stable["validated_schemas"] == {"fact_events": {"value": "INTEGER"}}
    assert stable["available_plots"] == plot_descriptions()
    assert set(stable["available_plots"]) == set(plot_capabilities())
    assert repair == {"previous_analysis": None, "previous_acceptance_error": ""}
    first, second = [item[1]["tools"][-1].tool_spec for item in captured]
    assert first["name"] == second["name"] == "query_data"
    assert first["inputSchema"]["json"]["properties"]["limit"]["default"] == 500
    assert second["inputSchema"]["json"]["properties"]["limit"]["default"] == 20


def test_analysis_repair_preserves_stable_cache_prefix(monkeypatch, analytical_spec, tmp_path):
    captured = []
    monkeypatch.setattr(agents, "invoke", lambda *args, **kwargs: captured.append(args))
    plan = AnalyticalPlan(business_processes=[], dimensions=[], facts=[], assumptions=[], unresolved_questions=[])
    previous = AnalysisPlan(analyses=[analytical_spec], qualifications=[], warnings=[])
    args = ("request", plan, {"fact_events": {"value": "INTEGER"}}, {}, lambda *a: None,
            tmp_path / "models.duckdb")
    agents.analyse(*args)
    agents.analyse(*args, previous=previous, feedback="Rejected query")
    assert captured[0][0][0] == captured[1][0][0]
    repair = json.loads(captured[1][0][1]["text"])
    assert repair == {"previous_analysis": previous.model_dump(), "previous_acceptance_error": "Rejected query"}


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
        agents.interpret(analytical_spec, result, lambda *a: None, db, executed_sql="SELECT * FROM fact_events",
                         previous=finding, cost_experiments=controls)
    assert captured[0][1] is AnalysisFinding
    assert captured[1][1] is EarlyValidatedAnalysisFinding
    assert captured[0][0] == captured[1][0]


def test_finding_receives_executed_definition_and_keeps_repair_outside_cache_prefix(
    monkeypatch, analytical_spec, evidence_and_finding, tmp_path,
):
    registry, finding = evidence_and_finding
    captured = []
    monkeypatch.setattr(agents, "invoke", lambda *args, **kwargs: captured.append((args, kwargs)))
    db = DuckDBResultTools(tmp_path / "models.duckdb", registry.relation, {"value": "INTEGER"}, registry=registry)
    result = AcceptedAnalysisResult(result_id="result_00", relation=registry.relation, row_count=1,
                                    columns={"value": "INTEGER"}, profile=[])
    sql = "WITH __modellm_population AS (SELECT * FROM fact_events) SELECT * FROM __modellm_population"
    agents.interpret(analytical_spec, result, lambda *a: None, db, executed_sql=sql)
    registry.register(EvidenceRequest(kind="rows", fields=["value"]),
                      [{"value": 15}], {"value": "INTEGER"}, False, False)
    agents.interpret(analytical_spec, result, lambda *a: None, db, executed_sql=sql,
                     previous=finding, feedback="Missing evidence binding")
    assert captured[0][0][0][0] == captured[1][0][0][0]
    stable, repair = [json.loads(block["text"]) for block in captured[1][0][0]]
    assert stable["executed_sql"] == sql
    assert stable["visualisation_goal"] == analytical_spec.visualisation_goal
    assert stable["result"] == result.model_dump()
    assert "registered_evidence" not in stable
    assert repair["registered_evidence"] == registry.snapshot()
    assert repair["previous_finding"] == finding.model_dump()
    assert repair["previous_finding_error"] == "Missing evidence binding"
    assert [tool.tool_name for tool in captured[1][1]["tools"]] == ["inspect_result"]


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
        agent_runtime.invoke(prompt, Response, "conceptual modelling", lambda *e: events.append(e), "system",
                      cost_experiments=controls)
        assert ("cache_config" in captured["model"]) == bool(controls)
        assert captured["prompt"] == (prompt[:1] + [{"cachePoint": {"type": "default"}}] + prompt[1:] if controls else prompt)
        assert "plugins" not in captured["agent"]
        for e in events:
            if e[1] == "model_call":
                assert e[3]["cost_experiments"] == sorted(controls)
                assert e[3]["strands_version"]
    # Existing stages keep caching when all experiments are disabled.
    agent_runtime.invoke("prompt", Response, "analytical interpretation", lambda *a: None, "system", cost_experiments=frozenset())
    assert "cache_config" in captured["model"]
    agent_runtime.invoke("prompt", Response, "analytical interpretation", lambda *a: None, "system",
                  cost_experiments=frozenset({"offload_analysis"}), context_directory=tmp_path / "context")
    assert len(captured["agent"]["plugins"]) == 1
    # Use a provider ID without a Claude marker to verify the cache support guard.
    monkeypatch.setenv("MODELLM_BEDROCK_MODEL_ID", "test.provider")
    agent_runtime.invoke("prompt", Response, "conceptual modelling", lambda *a: None, "system",
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
        captured.update(json.loads(prompt[0]["text"]))
    monkeypatch.setattr(agents, "invoke", invoke)
    pio.templates["test_registered_template"] = pio.templates["plotly_white"]
    try:
        agents.plot(analytical_spec, AcceptedAnalysisResult(
            result_id="result_00", relation='analysis."result_00"', row_count=1,
            columns={"value": "INTEGER"}, profile=[],
        ), lambda *args: None, tmp_path / "models.duckdb",
                    original_request="Map the recorded values", frame=pd.DataFrame({"value": [1]}))
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


@pytest.mark.parametrize(("defect", "error"), [
    ("column", "missing accepted-result column: color=variable"),
    ("argument", "Unsupported plot column argument for line: orientation"),
    ("filter", "Plot filters must equal the supplied presentation selection"),
    ("template", "Unknown Plotly template: 'plotly_light'"),
])
def test_plot_agent_corrects_result_contract_inside_one_budgeted_invocation(
    monkeypatch, analytical_spec, tmp_path, defect, error,
):
    import pandas as pd
    import strands.models

    good = {"function": "line", "columns": {"x": ["mean_reading", "p75_reading"], "y": "position"},
            "filters": [], "options": {"orientation": "h"}}
    bad = copy.deepcopy(good)
    if defect == "column": bad["columns"]["color"] = "variable"
    elif defect == "argument": bad["columns"]["orientation"] = "h"
    elif defect == "filter": bad["filters"] = [{"field": "position", "operator": "eq", "values": [1]}]
    else: bad["options"]["template"] = "plotly_light"
    models, configs = [], []

    class FakeBedrockModel(LocalModel):
        def __init__(self, **kwargs):
            configs.append(kwargs)
            super().__init__([("AcceptedResultPlotSpec", bad), ("AcceptedResultPlotSpec", good)])
            self.client = SimpleNamespace(meta=SimpleNamespace(region_name=kwargs["region_name"]))
            models.append(self)

    monkeypatch.setattr(strands.models, "BedrockModel", FakeBedrockModel)
    result = AcceptedAnalysisResult(result_id="result_00", relation='analysis."result_00"', row_count=2,
        columns={"position": "DOUBLE", "mean_reading": "DOUBLE", "p75_reading": "DOUBLE"}, profile=[])
    settings = ReleaseSettings(max_model_calls=2)
    events = []
    frame = pd.DataFrame({"position": [1, 2], "mean_reading": [2, 3], "p75_reading": [3, 4]})
    with use_settings(settings), use_budget(settings) as budget:
        plot = agents.plot(analytical_spec, result,
                           lambda *e: events.append(e), tmp_path / "models.duckdb",
                           original_request="Compare mean and upper-quartile readings", frame=frame)
        assert budget.calls == 2
    assert len(models) == 1
    assert len(models[0].requests) == 2
    assert error in json.dumps(models[0].requests[1])
    assert configs[0]["model_id"] == settings.plotting_model_id
    assert "cache_config" in configs[0]
    assert len([e for e in events if e[1] == "model_call" and e[3]["status"] == "started"]) == 1
    assert plot.model_dump() == good
    frame = pd.DataFrame({"position": [1, 2], "mean_reading": [2, 3], "p75_reading": [3, 4]})
    assert len(render_plot(frame, plot, "Reading profile").data) == 2
    # The final workflow guard still catches contracts bypassed with model_copy/construct.
    with pytest.raises(ValueError, match=re.escape(error)):
        validate_plot_spec(plot.model_copy(update=bad), result)


def test_scoped_plot_schemas_preserve_fields_and_isolate_result_and_selection():
    first = AcceptedAnalysisResult(result_id="first", relation='analysis."first"', row_count=1,
                                   columns={"value": "DOUBLE", "variant": "VARCHAR"}, profile=[])
    second = first.model_copy(update={"result_id": "second", "columns": {"other": "DOUBLE"}})
    selection = [ResultFilter(field="variant", operator="eq", values=["daily"])]
    first_schema = accepted_result_plot_schema(first, selection,
        frame=pd.DataFrame({"value": [1], "variant": ["daily"]}))
    second_schema = accepted_result_plot_schema(second, frame=pd.DataFrame({"other": [2]}))
    assert first_schema.model_json_schema() == second_schema.model_json_schema()
    assert set(first_schema.model_fields) == set(PlotSpec.model_fields)
    payload = {"function": "bar", "columns": {"y": "value"},
               "filters": [item.model_dump() for item in selection], "options": {}}
    selection[0].values[0] = "monthly"
    assert first_schema.model_validate(payload).model_dump() == payload
    assert second_schema.model_validate({"function": "bar", "columns": {"y": "other"},
                                         "filters": [], "options": {}})
    with pytest.raises(ValidationError, match="missing accepted-result column"):
        second_schema.model_validate({**payload, "filters": []})
    assert first_schema.model_validate(payload)


@pytest.fixture
def plotting_model(monkeypatch):
    import strands.models

    def configure(submissions):
        instances = []

        class FakeBedrockModel(LocalModel):
            def __init__(self, **kwargs):
                super().__init__(submissions)
                self.client = SimpleNamespace(meta=SimpleNamespace(region_name=kwargs['region_name']))
                instances.append(self)

        monkeypatch.setattr(strands.models, 'BedrockModel', FakeBedrockModel)
        return instances

    return configure


def test_plot_heals_render_failure_without_changing_nullable_data(plotting_model, analytical_spec, tmp_path):
    frame = pd.DataFrame({'position': [1, 2, 3], 'reading': [10.0, None, 30.0]})
    original = frame.copy(deep=True)
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"',
        row_count=3, columns={'position': 'INTEGER', 'reading': 'DOUBLE'}, profile=[])
    bad = {'function': 'scatter', 'columns': {'x': 'position', 'y': 'reading', 'size': 'reading'},
           'filters': [], 'options': {}}
    good = {**bad, 'columns': {'x': 'position', 'y': 'reading', 'color': 'reading'}}
    instances = plotting_model([('AcceptedResultPlotSpec', bad), ('AcceptedResultPlotSpec', good)])
    events = []
    settings = ReleaseSettings(max_model_calls=4)
    with use_settings(settings), use_budget(settings) as budget:
        plot = agents.plot(analytical_spec, result, lambda *e: events.append(e), tmp_path / 'models.duckdb',
                           original_request='Explore the readings', frame=frame)
        assert budget.calls == 2
    assert len(instances) == 1
    assert "Invalid element(s) received for the 'size' property" in json.dumps(instances[0].requests[1])
    assert 'cachePoint' in instances[0].requests[0][0]['content'][1]
    assert plot.model_dump() == good
    pd.testing.assert_frame_equal(frame, original)


def test_plot_turn_limit_stops_uncorrectable_proposals(plotting_model, analytical_spec, tmp_path):
    bad = {'function': 'scatter', 'columns': {'x': 'missing'}, 'filters': [], 'options': {}}
    instances = plotting_model([('AcceptedResultPlotSpec', bad)] * 5)
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"',
        row_count=1, columns={'value': 'INTEGER'}, profile=[])
    events = []
    settings = ReleaseSettings(max_model_calls=10)
    with use_settings(settings), use_budget(settings) as budget:
        with pytest.raises(agent_runtime.InvocationLimitError, match='limit_turns'):
            agents.plot(analytical_spec, result, lambda *e: events.append(e), tmp_path / 'models.duckdb',
                        original_request='Explore the values', frame=pd.DataFrame({'value': [1]}))
        assert budget.calls == 4
    assert len(instances[0].requests) == 4
    assert events[-1][3]['category'] == 'invocation_limit'


def test_global_budget_is_not_local_plot_exhaustion(plotting_model, analytical_spec, tmp_path):
    from strands.types.exceptions import EventLoopException

    bad = {'function': 'scatter', 'columns': {'x': 'missing'}, 'filters': [], 'options': {}}
    instances = plotting_model([('AcceptedResultPlotSpec', bad)] * 5)
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"',
        row_count=1, columns={'value': 'INTEGER'}, profile=[])
    settings = ReleaseSettings(max_model_calls=1)
    with use_settings(settings), use_budget(settings):
        with pytest.raises(EventLoopException, match='Internal model calls exceeds'):
            agents.plot(analytical_spec, result, lambda *e: None, tmp_path / 'models.duckdb',
                        original_request='Explore the values', frame=pd.DataFrame({'value': [1]}))
    assert len(instances[0].requests) == 1
