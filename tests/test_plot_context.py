"""Plot selection should receive result meaning and statistics, not sample rows."""
import json
import pandas as pd
import pytest
from pydantic import ValidationError
from src import agents
from src.visualisation import plot_descriptions
from src.core.contracts import AcceptedAnalysisResult, AnalysisDimension, AnalysisMeasure, AnalysisSpec, PlotSpec, ResultColumnProfile, QueryDefinition, registered_plot_templates

def test_plot_prompt_uses_result_profile_and_semantics_without_sample_rows(monkeypatch, tmp_path):
    spec = AnalysisSpec(title='Daily cohort events', concept_id='events_by_cohort', population_definition='Recorded events', question='How do events vary by day and cohort?', rationale='Compare the recorded event counts at daily cohort grain.', grain='one row per day and cohort', dimensions=[AnalysisDimension(field='event_day', semantic_type='temporal', description='Event date'), AnalysisDimension(field='cohort', semantic_type='categorical', description='Cohort')], measures=[AnalysisMeasure(field='event_count', semantic_type='count', description='Recorded events', additivity='additive')], visualisation_goal='Show the daily pattern for both cohorts.', query=QueryDefinition(kind='direct', sql='SELECT event_day, cohort, event_count FROM main.fact_events'), observation_rule=None)
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"', row_count=730, columns={'event_day': 'DATE', 'cohort': 'VARCHAR', 'event_count': 'INTEGER'}, profile=[ResultColumnProfile(column_name='event_count', column_type='INTEGER', count=730)])
    captured = {}

    def fake_invoke(prompt, schema, stage, emit, system_prompt, tools=None, **kwargs):
        captured['blocks'] = prompt
        captured['payload'] = {key: value for block in prompt for key, value in json.loads(block['text']).items()}
        captured['schema'] = schema
        captured['limits'] = kwargs['limits']
        return PlotSpec(function='line', columns={'x': 'event_day', 'y': 'event_count'}, filters=[], options={})
    monkeypatch.setattr(agents, 'invoke', fake_invoke)
    request = 'Show the daily pattern for both cohorts as a multiple-line chart.'
    agents.plot(spec, result, lambda *args: None,
                tmp_path / 'modellm.duckdb', original_request=request,
                frame=pd.DataFrame({'event_day': ['2026-01-01'], 'cohort': ['A'], 'event_count': [1]}))
    payload = captured['payload']
    assert payload['original_request'] == request
    assert payload['visualisation_goal'] == spec.visualisation_goal
    assert 'concept_id' not in payload
    assert 'variant_field' not in payload
    assert 'finding' not in payload
    assert 'evidence' not in payload
    with pytest.raises(ValidationError, match='missing accepted-result column'):
        captured['schema'].model_validate({'function': 'line', 'columns': {'x': 'event_day', 'y': 'event_count', 'color': 'variable'}, 'filters': [], 'options': {}})
    assert 'sample_rows' not in json.dumps(payload)
    assert 'main.fact_events' not in json.dumps(payload)
    assert payload['available_plots'] == plot_descriptions()
    assert payload['renderer_templates'] == registered_plot_templates()
    result_context = payload['analytical_result']
    assert 'profile' in result_context
    assert 'dimensions' in result_context
    assert 'measures' in result_context
    assert 'grain' in result_context
    assert captured['limits'] == {'turns': 4}
    shared = captured['blocks'][0]
    agents.plot(spec.model_copy(update={'title': 'Another chart', 'question': 'Another question'}),
                result, lambda *args: None, tmp_path / 'modellm.duckdb', original_request='Another request',
                frame=pd.DataFrame({'event_day': ['2026-01-02'], 'cohort': ['B'], 'event_count': [2]}))
    assert captured['blocks'][0] == shared
    assert set(json.loads(shared['text'])) == {'available_plots', 'renderer_templates'}
