"""The agent preview size must not determine an accepted result's grain."""
from pathlib import Path
import duckdb
from src import agents
from src.analysis import accept_analyses, load_result_frame
from src.core.contracts import AnalysisDimension, AnalysisMeasure, AnalysisPlan, AnalysisSpec, AnalyticalPlan, BusinessProcessPlan, FactPlan, ModelOutput, FieldDefinition, FieldRef, MatchStep, MatchPair, OutputKeyPair, QueryDefinition, EvidenceRequest, EvidenceSort, EvidenceRowRef, FindingCell
from src.tools.analysis_tools import validate_analysis_sql

def test_final_analysis_keeps_all_rows_after_a_truncated_agent_preview(tmp_path: Path, monkeypatch):
    database = tmp_path / 'modellm.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute("\n            CREATE TABLE main.fact_events AS\n            SELECT\n                DATE '2025-01-01' + CAST(FLOOR(i / 2) AS INTEGER) AS event_day,\n                CASE WHEN i % 2 = 0 THEN 'A' ELSE 'B' END AS cohort,\n                1 AS event_count\n            FROM range(730) AS events(i)\n            ")
    sql = 'SELECT event_day, cohort, event_count FROM main.fact_events ORDER BY event_day, cohort'
    assert validate_analysis_sql(sql, ['fact_events']) == sql
    proposed = AnalysisPlan(analyses=[AnalysisSpec(title='Daily cohort events', concept_id='events_by_cohort', population_definition='Recorded events', question='How do events vary by day and cohort?', rationale="Retain each event's date and cohort for a daily comparison.", grain='one row per event and cohort', dimensions=[AnalysisDimension(field='event_day', semantic_type='temporal', description='Event date'), AnalysisDimension(field='cohort', semantic_type='categorical', description='Cohort')], measures=[AnalysisMeasure(field='event_count', semantic_type='count', description='One recorded event', additivity='additive')], visualisation_goal='Show the daily pattern for both cohorts.', query=QueryDefinition(kind='direct', sql=sql), observation_rule=None)], qualifications=[], warnings=[])
    plan = AnalyticalPlan(assumptions=[], unresolved_questions=[], business_processes=[BusinessProcessPlan(name='event_occurrence', description='One event recorded for one cohort on one day')], dimensions=[], facts=[FactPlan(name='fact_events', business_process='event_occurrence', fact_type='transaction', grain='one row per event', source_datasets=['events'], output_fields=[ModelOutput(name='event_count', description='One event', additivity='additive', definition=FieldDefinition(kind='constant', inputs=[], explanation='constant 1', value=1))], modelling_rationale='Reusable recorded observations at the declared grain', dimension_links=[], grain_key_fields=[])])

    def fake_invoke(prompt, schema, stage, emit, system_prompt, tools, **kwargs):
        assert schema is AnalysisPlan
        preview = tools[2](sql=sql, limit=500)
        assert preview['row_count'] == 500
        assert preview['truncated'] is True
        return proposed
    monkeypatch.setattr(agents, 'invoke', fake_invoke)
    analysis = agents.analyse('Compare daily cohorts', plan, {'fact_events': {'event_day': 'DATE', 'cohort': 'VARCHAR', 'event_count': 'INTEGER'}}, {}, lambda *args: None, database)
    results = accept_analyses(database, ['fact_events'], analysis)
    assert len(results) == 1
    accepted = results[0][1]
    assert accepted.row_count == 730
    assert accepted.relation == 'analysis."result_00"'
    assert accepted.profile
    frame = load_result_frame(database, accepted)
    assert len(frame) == 730
    assert frame.groupby('cohort').size().to_dict() == {'A': 365, 'B': 365}
