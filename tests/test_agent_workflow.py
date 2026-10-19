"""The only workflow path accepts uploaded data and agent decisions."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from src import agents
from src.core.contracts import AnalysisDimension, AnalysisFinding, AnalysisMeasure, AnalysisPlan, AnalysisSpec, AnalyticalPlan, BusinessProcessPlan, DbtFile, DbtImplementation, DimensionPlan, FactDimensionLink, FactPlan, FindingStatement, EvidenceAggregate, PlotSpec, SourceMatchPath, ModelOutput, FieldDefinition, FieldRef, MatchStep, MatchPair, OutputKeyPair, QueryDefinition, EvidenceRequest, EvidenceSort, EvidenceRowRef, FindingCell
from src.workflow import run
import src.workflow as workflow_module

@pytest.fixture
def workflow_case(tmp_path: Path, monkeypatch):
    accounts = tmp_path / 'accounts.csv'
    events = tmp_path / 'events.csv'
    accounts.write_text('account_id,account_name\n1,North\n2,South\n')
    events.write_text('event_id,account_id,event_date,amount\n10,1,2026-01-01,12\n11,1,2026-01-02,8\n12,2,2026-01-02,5\n')
    plan = AnalyticalPlan(assumptions=[], unresolved_questions=[], business_processes=[BusinessProcessPlan(name='event_recording', description='Record each event')], dimensions=[DimensionPlan(name='dim_account', business_entity='Account', grain='one row per account', source_datasets=['accounts'], natural_key_fields=['account_key'], surrogate_key='account_key', output_fields=[ModelOutput(name='account_key', description='Account ID', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='accounts', column='account_id')])), ModelOutput(name='account_name', description='Account name', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='accounts', column='account_name')]))], grain_key_fields=['account_key'])], facts=[FactPlan(name='fact_events', business_process='event_recording', fact_type='transaction', grain='one row per event', source_datasets=['events'], dimension_links=[FactDimensionLink(dimension_model='dim_account', source_paths=[SourceMatchPath(source_dataset='events', steps=[MatchStep(pairs=[MatchPair(left=FieldRef(kind='source', column='account_id', dataset='events'), right=FieldRef(kind='model', column='account_key', model='dim_account'))])])], unmatched_policy='exclude', key_pairs=[OutputKeyPair(fact_field='account_key', dimension_field='account_key')])], output_fields=[ModelOutput(name='event_id', description='Event ID', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='events', column='event_id')])), ModelOutput(name='account_key', description='Account match', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='events', column='account_id')])), ModelOutput(name='event_date', description='Event date', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='events', column='event_date')])), ModelOutput(name='amount', description='Event amount', additivity='additive', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='events', column='amount')]))], grain_key_fields=['event_id'], modelling_rationale='Reusable recorded observations at the declared grain')])
    implementation = DbtImplementation(files=[DbtFile(path='dim_account.sql', content="SELECT account_id AS account_key, account_id, account_name FROM {{ source('raw', 'accounts') }}"), DbtFile(path='fact_events.sql', content="SELECT event_id, account_id AS account_key, event_date, amount FROM {{ source('raw', 'events') }}"), DbtFile(path='schema.yml', content="version: 2\nmodels:\n  - name: dim_account\n    columns:\n      - name: account_key\n        data_tests: [unique, not_null]\n  - name: fact_events\n    columns:\n      - name: event_id\n        data_tests: [unique, not_null]\n      - name: account_key\n        data_tests:\n          - not_null\n          - relationships:\n              arguments:\n                to: ref('dim_account')\n                field: account_key\n")], explanation='Event grain with shared account context')
    analysis = AnalysisPlan(analyses=[AnalysisSpec(title='Amount by account', concept_id='amount_by_account', population_definition='Recorded events with matching accounts', question='Which accounts have the largest recorded amount?', rationale='Sum atomic event amounts using the validated account relationship.', grain='one row per account', dimensions=[AnalysisDimension(field='account_name', semantic_type='categorical', description='Account')], measures=[AnalysisMeasure(field='total_amount', semantic_type='numeric', description='Total recorded amount', additivity='additive')], visualisation_goal='Compare account totals', query=QueryDefinition(kind='direct', sql='SELECT d.account_name, SUM(f.amount) AS total_amount FROM main.fact_events f JOIN main.dim_account d USING (account_key) GROUP BY d.account_name ORDER BY total_amount DESC'), observation_rule=None)], qualifications=[], warnings=[])
    monkeypatch.setattr(agents, 'conceptual', lambda *args, **kwargs: plan)
    implementation_failures = []

    def fake_implement(*args, **kwargs):
        implementation_failures.append(kwargs.get('failure', ''))
        if len(implementation_failures) == 1:
            return DbtImplementation(files=[file for file in implementation.files if file.path != 'fact_events.sql'], explanation='Incomplete first proposal')
        return implementation
    monkeypatch.setattr(agents, 'implement', fake_implement)
    real_run_dbt = workflow_module.run_dbt
    compile_calls = 0

    def sequential_dbt_failures(project, command):
        nonlocal compile_calls
        if command == 'compile':
            compile_calls += 1
            if compile_calls <= 1:
                return {'command': command, 'ok': False, 'output': f'independent dbt error {compile_calls}'}
        return real_run_dbt(project, command)
    monkeypatch.setattr(workflow_module, 'run_dbt', sequential_dbt_failures)
    analysis_calls = []

    def fake_analyse(*args, **kwargs):
        analysis_calls.append(kwargs.get('feedback', ''))
        proposed = analysis.model_copy(deep=True)
        if len(analysis_calls) == 1:
            proposed.analyses[0].query.sql = 'SELECT d.account_name, missing AS total_amount FROM main.dim_account d'
        return proposed
    monkeypatch.setattr(agents, 'analyse', fake_analyse)

    plot_requests = []

    def fake_interpret(*args, **kwargs):
        raise AssertionError('Finding is disabled and must not be invoked')
    monkeypatch.setattr(agents, 'interpret', fake_interpret)

    def fake_plot(*args, original_request, **kwargs):
        plot_requests.append(original_request)
        return PlotSpec(function='bar', columns={'x': 'account_name', 'y': 'total_amount'}, filters=[], options={})
    monkeypatch.setattr(agents, 'plot', fake_plot)
    return SimpleNamespace(paths=[accounts, events], root=tmp_path / 'runs',
                           implementation_failures=implementation_failures, analysis_calls=analysis_calls,
                           plot_requests=plot_requests)


def test_uploaded_data_follows_agent_path_through_dbt_and_plot(workflow_case):
    implementation_failures = workflow_case.implementation_failures
    analysis_calls = workflow_case.analysis_calls
    result = run(workflow_case.paths, 'Compare accounts', workflow_case.root)
    assert result['status'] == 'complete', result.get('error')
    assert result['repair_attempts'] == 2
    assert implementation_failures[0] == ''
    assert 'fact_events' in implementation_failures[1]
    assert 'independent dbt error 1' in implementation_failures[2]
    assert len(result['dbt_attempts']) == 3
    assert [item['status'] for item in result['dbt_attempts']] == ['rejected', 'rejected', 'accepted']
    assert all((Path(item['artifact']).exists() for item in result['dbt_attempts']))
    assert len(result['analysis_attempts']) == 2
    assert result['analysis_attempts'][0]['status'] == 'rejected'
    assert 'missing' in analysis_calls[1]
    assert set(result['model_names']) == {'dim_account', 'fact_events'}
    assert result['analysis_results'][0]['rows'] == 2
    assert result['plot_specs'][0]['function'] == 'bar'
    assert result['findings'] == []
    assert result['finding_attempts'] == []
    assert result['evidence'] == []
    assert not any(event['stage'] == 'analytical finding' for event in result['events'])
    assert not any(agent['agent'] == 'Finding' for agent in result['usage']['agents'])
    assert workflow_case.plot_requests == ['Compare accounts']
    assert [item['kind'] for item in result['events']].count('contract_check') >= 4
    assert (Path(result['run_dir']) / result['charts'][0]['file']).exists()


@pytest.mark.parametrize('failure', ['plotting', 'tracker'])
def test_failure_boundaries_preserve_attempts_and_terminal_records(workflow_case, monkeypatch, failure):
    calls = []
    if failure == 'plotting':
        def reject_plot(*args, **kwargs):
            calls.append('plotting')
            return PlotSpec(function='unsupported', columns={}, filters=[], options={})
        monkeypatch.setattr(agents, 'plot', reject_plot)
    else:
        def reject_ledger(*args):
            calls.append('tracker')
            raise OSError('Ledger unavailable')
        monkeypatch.setattr(workflow_module, 'record_run', reject_ledger)
    events = []
    state = run(workflow_case.paths, 'Compare accounts', workflow_case.root, on_event=events.append)
    assert state['status'] == 'complete'
    terminal = [event for event in state['events'] if event['stage'] == 'dashboard completion']
    assert len(terminal) == 1
    assert terminal[0]['kind'] == 'done'
    if failure == 'plotting':
        assert calls == ['plotting']  # Plot rejection has no extra repair invocation.
        assert state['plot_attempts'][0]['status'] == 'skipped'
        assert not state['charts']
    else:
        assert calls == ['tracker', 'tracker']
        assert state['experiment_tracker_error'] == 'OSError: Ledger unavailable'
        assert not any(event['stage'] == 'experiment tracking' for event in events)
    import json
    assert json.loads((Path(state['run_dir']) / 'result.json').read_text()) == state
    assert not (Path(state['run_dir']) / 'input').exists()
