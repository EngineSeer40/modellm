"""Plot selection should receive result meaning and statistics, not sample rows."""
import json
from src import agents
from src.core.contracts import AcceptedAnalysisResult, AnalysisDimension, AnalysisMeasure, AnalysisSpec, PlotSpec, ResultColumnProfile, ModelOutput, FieldDefinition, FieldRef, MatchStep, MatchPair, OutputKeyPair, QueryDefinition, EvidenceRequest, EvidenceSort, EvidenceRowRef, FindingCell

def test_plot_prompt_uses_result_profile_and_semantics_without_sample_rows(monkeypatch, tmp_path):
    spec = AnalysisSpec(title='Daily cohort events', concept_id='events_by_cohort', population_definition='Recorded events', question='How do events vary by day and cohort?', rationale='Compare the recorded event counts at daily cohort grain.', grain='one row per day and cohort', dimensions=[AnalysisDimension(field='event_day', semantic_type='temporal', description='Event date'), AnalysisDimension(field='cohort', semantic_type='categorical', description='Cohort')], measures=[AnalysisMeasure(field='event_count', semantic_type='count', description='Recorded events', additivity='additive')], visualisation_goal='Show the daily pattern for both cohorts.', query=QueryDefinition(kind='direct', sql='SELECT event_day, cohort, event_count FROM main.fact_events'), observation_rule=None)
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"', row_count=730, columns={'event_day': 'DATE', 'cohort': 'VARCHAR', 'event_count': 'INTEGER'}, profile=[ResultColumnProfile(column_name='event_count', column_type='INTEGER', count=730)])
    captured = {}

    def fake_invoke(prompt, schema, stage, emit, system_prompt, tools=None, **kwargs):
        captured['payload'] = json.loads(prompt)
        return PlotSpec(function='line', columns={'x': 'event_day', 'y': 'event_count'}, filters=[], options={})
    monkeypatch.setattr(agents, 'invoke', fake_invoke)
    agents.plot(spec, result, {'text': 'Both cohorts contribute events.', 'evidence': []}, lambda *args: None, tmp_path / 'modellm.duckdb')
    payload = captured['payload']
    assert 'sample_rows' not in json.dumps(payload)
    assert 'main.fact_events' not in json.dumps(payload)
    result_context = payload['analytical_result']
    assert 'profile' in result_context
    assert 'dimensions' in result_context
    assert 'measures' in result_context
    assert 'grain' in result_context
