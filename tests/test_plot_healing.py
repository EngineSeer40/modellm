"""Plot failures stay local; execution and storage failures remain visible."""

from types import SimpleNamespace

import duckdb
import pandas as pd
import plotly.graph_objects as go
import pytest
from pydantic import ValidationError

from src import agents
from src.agent_runtime import InvocationLimitError
from src.config import ReleaseSettings, use_settings
from src.core.contracts import AcceptedAnalysisResult, AnalysisSpec, PlottingPlan, QueryDefinition
from src.run import RunRecord
from src.workflow import _render_visualisations
from src.workspace import RunWorkspace
from src.visualisation import accepted_result_plot_schema


@pytest.fixture
def plot_case(tmp_path):
    workspace = RunWorkspace.create(tmp_path / 'runs')
    record = RunRecord(workspace, {'run_id': workspace.run_id, 'run_dir': str(workspace.run_dir),
        'status': 'running', 'request': 'Explore the readings', 'events': []})
    accepted = []
    with duckdb.connect(str(workspace.database)) as con:
        con.execute('CREATE SCHEMA analysis')
        for index in range(3):
            relation = f'analysis.result_{index:02d}'
            con.execute(f'CREATE TABLE {relation} AS SELECT 1 AS position, 10.0 AS reading')
            spec = AnalysisSpec(title=f'Readings {index}', question='What readings were recorded?',
                concept_id='readings', population_definition='All records', observation_rule=None,
                rationale='Explore recorded values', grain='one row per record', dimensions=[], measures=[],
                visualisation_goal='Show the readings', query=QueryDefinition(kind='direct', sql='SELECT 1'))
            result = AcceptedAnalysisResult(result_id=f'result_{index:02d}', relation=relation,
                row_count=1, columns={'position': 'INTEGER', 'reading': 'DECIMAL(3,1)'}, profile=[])
            accepted.append((spec, result))
    return SimpleNamespace(record=record, accepted=accepted)


@pytest.mark.parametrize('failure', ['limit', 'encoding'])
def test_failed_chart_is_skipped_between_successful_charts(plot_case, monkeypatch, failure):
    calls = []

    def plot(spec, *args, **kwargs):
        calls.append(spec.title)
        if spec.title == 'Readings 1':
            if failure == 'limit':
                raise InvocationLimitError('four plotting turns exhausted')
            return PlottingPlan(function='unsupported', columns={}, filters=[], options={})
        return PlottingPlan(function='scatter', columns={'x': 'position', 'y': 'reading'}, filters=[], options={})

    monkeypatch.setattr(agents, 'plot', plot)
    with use_settings(ReleaseSettings(max_charts=2)):
        _render_visualisations(plot_case.record, plot_case.accepted, frozenset())
    state = plot_case.record.state
    assert calls == ['Readings 0', 'Readings 1', 'Readings 2']
    assert [item['status'] for item in state['plot_attempts']] == ['accepted', 'skipped', 'accepted']
    assert [chart['result_index'] for chart in state['charts']] == [0, 2]
    assert all(chart['concept_id'] == 'readings' for chart in state['charts'])
    assert all((plot_case.record.workspace.run_dir / chart['file']).exists() for chart in state['charts'])


@pytest.mark.parametrize('error', [ValueError('Internal model calls exceeds configured limit'),
    RuntimeError('Reported model cost budget exhausted'),
    TimeoutError('Provider connection timed out')])
def test_global_and_transport_errors_are_not_skipped(plot_case, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(agents, 'plot', fail)
    with pytest.raises(type(error), match=str(error)):
        _render_visualisations(plot_case.record, plot_case.accepted, frozenset())
    assert not plot_case.record.state['plot_attempts']


def test_artifact_write_failure_is_not_skipped(plot_case, monkeypatch):
    monkeypatch.setattr(agents, 'plot', lambda *a, **kw: PlottingPlan(function='scatter',
        columns={'x': 'position', 'y': 'reading'}, filters=[], options={}))
    def fail(*args, **kwargs):
        raise OSError('Artifact storage unavailable')
    monkeypatch.setattr(go.Figure, 'write_json', fail)
    with pytest.raises(OSError, match='Artifact storage unavailable'):
        _render_visualisations(plot_case.record, plot_case.accepted, frozenset())
    assert not plot_case.record.state['plot_attempts']


@pytest.mark.parametrize('function', ['bar', 'line'])
def test_nonempty_wide_plot_must_draw_traces(function):
    frame = pd.DataFrame({'position': [1, 2, 3], 'group': ['A', 'A', 'B'],
                          'first': [2, 3, 4], 'second': [5, 6, 7]})
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"',
                                    row_count=3, columns={name: str(dtype)
                                                          for name, dtype in frame.dtypes.items()}, profile=[])
    schema = accepted_result_plot_schema(result, frame=frame)
    bad = {'function': function, 'columns': {'x': 'position', 'y': ['first', 'second'],
            'color': 'group', 'hover_data': ['first', 'second']}, 'filters': [], 'options': {}}
    with pytest.raises(ValidationError, match='no traces'):
        schema.model_validate(bad)
    corrected = {**bad, 'columns': {key: value for key, value in bad['columns'].items()
                                   if key != 'hover_data'}}
    assert schema.model_validate(corrected).function == function
