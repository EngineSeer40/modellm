"""Cost controls exercise the real SDK boundaries without provider calls."""

import asyncio
import copy
import json
import re
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest
from pydantic import ValidationError
from strands import Agent
from strands.hooks.events import AfterToolCallEvent
from local_model import LocalModel, Response
from strands.types.tools import ToolContext

from src import agents, agent_runtime
from src.analysis import (
    accept_analyses,
)
from src.visualisation import accepted_result_plot_schema, plot_capabilities, plot_descriptions, render_plot, validate_plot_spec
from src.config import ReleaseSettings, use_settings
from src.execution import use_budget
from src.context_cost import COST_EXPERIMENT_STAGES, configured_cost_experiments, tool_result_offload_plugins
from src.core.contracts import AcceptedAnalysisResult, AnalyticalPlan, AnalysisSpec, ConceptualPlan, QueryDefinition, PlottingPlan, ResultFilter
from src.tools.analysis_tools import DuckDBAnalysisTools


@pytest.fixture
def analytical_spec():
    return AnalysisSpec(
        title="Recorded values", question="What values were recorded?", concept_id="values",
        population_definition="All recorded events", observation_rule=None,
        rationale="Retain event grain", grain="one row per event", dimensions=[], measures=[],
        visualisation_goal="Show the distribution", query=QueryDefinition(kind="direct", sql="SELECT * FROM fact_events"),
    )


def test_controls_default_on_validate_and_honor_explicit_snapshot(monkeypatch):
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
    plan = ConceptualPlan(business_processes=[], dimensions=[], facts=[], assumptions=[], unresolved_questions=[])
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


def test_analysis_prompt_preserves_preview_controls_and_repair_context(monkeypatch, analytical_spec, tmp_path):
    captured = []
    monkeypatch.setattr(agents, "invoke", lambda *args, **kwargs: captured.append((args, kwargs)))
    plan = ConceptualPlan(business_processes=[], dimensions=[], facts=[], assumptions=[], unresolved_questions=[])
    args = ("request", plan, {"fact_events": {"value": "INTEGER"}}, {}, lambda *a: None,
            tmp_path / "models.duckdb")
    with use_settings(ReleaseSettings(max_analyses=5, max_charts=7)):
        for controls in (frozenset(), frozenset({"small_analysis_previews"})):
            agents.analyse(*args, cost_experiments=controls)
        previous = AnalyticalPlan(analyses=[analytical_spec], qualifications=[], warnings=[])
        agents.analyse(*args, previous=previous, feedback="Rejected query", cost_experiments=frozenset())
    assert captured[0][0][4] == agents.ANALYSIS
    assert captured[1][0][4].endswith(agents.ANALYSIS_PREVIEW_GUIDANCE)
    stable, repair = [json.loads(block["text"]) for block in captured[0][0][0]]
    assert stable["limits"] == {"max_analyses": 5, "max_charts": 7}
    assert stable["validated_schemas"] == args[2]
    assert stable["available_plots"] == plot_descriptions()
    assert set(stable["available_plots"]) == set(plot_capabilities())
    assert repair == {"previous_analysis": None, "previous_acceptance_error": ""}
    # Preview choice and repair feedback must not alter the cached evidence block.
    assert captured[0][0][0][0] == captured[1][0][0][0] == captured[2][0][0][0]
    repaired = json.loads(captured[2][0][0][1]["text"])
    assert repaired["previous_analysis"] == previous.model_dump()
    assert repaired["previous_acceptance_error"] == "Rejected query"
    first, second = [item[1]["tools"][-1].tool_spec for item in captured[:2]]
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
    accepted = accept_analyses(database, ["fact_events"], AnalyticalPlan(
        analyses=[analytical_spec], qualifications=[], warnings=[],
    ))
    assert accepted[0][1].row_count == 620


def test_analysis_offload_preserves_tool_results_and_retrieval(tmp_path):
    directory = tmp_path / "context" / "call"
    plugins = tool_result_offload_plugins("analytical interpretation", frozenset({"offload_analysis"}), directory)
    agent = Agent(model=LocalModel(), plugins=plugins, callback_handler=None)
    record = {"columns": ["value"], "row_count": 100, "truncated": False,
              "rows": [{"value": f"row_{i}:" + "data " * 100} for i in range(100)]}
    original = copy.deepcopy(record)

    async def inspect(tool, content, tool_id):
        event = AfterToolCallEvent(agent=agent, selected_tool=None,
            tool_use={"name": tool, "toolUseId": tool_id, "input": {}}, invocation_state={},
            result={"toolUseId": tool_id, "status": "success", "content": [{"json": content}]})
        await agent.hooks.invoke_callbacks_async(event)
        return event.result

    async def run():
        small = await inspect("query_data", {"value": 1}, "small")
        assert small["content"] == [{"json": {"value": 1}}]
        unrelated = await inspect("OtherTool", record, "unrelated")
        assert unrelated["content"] == [{"json": record}]
        result = await inspect("query_data", record, "large")
        preview = result["content"][0]["text"]
        assert len(preview) < len(json.dumps(record))
        reference = re.search(r"\[Stored references:\]\n  (\S+)", preview)[1]
        context = ToolContext(tool_use={"name": "retrieve_offloaded_content", "toolUseId": "read", "input": {}},
                              agent=agent, invocation_state={})
        retrieved = await plugins[0].retrieve_offloaded_content(reference=reference, tool_context=context)
        assert retrieved["content"][0]["json"] == original

    asyncio.run(run())
    assert record == original
    assert tool_result_offload_plugins("analytical interpretation", frozenset(), directory) == []
    assert tool_result_offload_plugins("analytical interpretation", frozenset({"offload_finding"}), directory) == []


def test_invoke_records_controls_cache_boundary_and_plugins(monkeypatch, tmp_path):
    import strands
    import strands.models

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
    plan = ConceptualPlan(business_processes=[], dimensions=[], facts=[], assumptions=[], unresolved_questions=[])
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


def test_unknown_template_is_rejected_before_rendering():
    with pytest.raises(ValidationError, match="Unknown Plotly template"):
        PlottingPlan(function="scatter_map", columns={"lat": "latitude", "lon": "longitude"},
                 filters=[], options={"template": "plotly_light"})


@pytest.mark.parametrize(("defect", "error"), [
    ("column", "missing accepted-result column: color=variable"),
    ("filter", "Plot filters must equal the supplied presentation selection"),
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
    else: bad["filters"] = [{"field": "position", "operator": "eq", "values": [1]}]
    models, configs = [], []

    class FakeBedrockModel(LocalModel):
        def __init__(self, **kwargs):
            configs.append(kwargs)
            super().__init__([("AcceptedResultPlottingPlan", bad), ("AcceptedResultPlottingPlan", good)])
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
    assert set(first_schema.model_fields) == set(PlottingPlan.model_fields)
    payload = {"function": "bar", "columns": {"y": "value"},
               "filters": [item.model_dump() for item in selection], "options": {}}
    selection[0].values[0] = "monthly"
    assert first_schema.model_validate(payload).model_dump() == payload
    assert second_schema.model_validate({"function": "bar", "columns": {"y": "other"},
                                         "filters": [], "options": {}})
    with pytest.raises(ValidationError, match="missing accepted-result column"):
        second_schema.model_validate({**payload, "filters": []})
    assert first_schema.model_validate(payload)
    with pytest.raises(ValidationError, match="Unsupported plot column argument"):
        first_schema.model_validate({**payload, "columns": {"y": "value", "orientation": "h"}})


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
    instances = plotting_model([('AcceptedResultPlottingPlan', bad), ('AcceptedResultPlottingPlan', good)])
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
    instances = plotting_model([('AcceptedResultPlottingPlan', bad)] * 5)
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
    instances = plotting_model([('AcceptedResultPlottingPlan', bad)] * 5)
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"',
        row_count=1, columns={'value': 'INTEGER'}, profile=[])
    settings = ReleaseSettings(max_model_calls=1)
    with use_settings(settings), use_budget(settings):
        with pytest.raises(EventLoopException, match='Internal model calls exceeds'):
            agents.plot(analytical_spec, result, lambda *e: None, tmp_path / 'models.duckdb',
                        original_request='Explore the values', frame=pd.DataFrame({'value': [1]}))
    assert len(instances[0].requests) == 1
