from src.core.contracts import AnalyticalPlan, BusinessProcessPlan, FactPlan, plan_issues, ModelOutput, FieldDefinition, FieldRef, MatchStep, MatchPair, OutputKeyPair, QueryDefinition, EvidenceRequest, EvidenceSort, EvidenceRowRef, FindingCell

def test_plan_links_are_checked_before_dbt():
    plan = AnalyticalPlan(assumptions=[], unresolved_questions=[], business_processes=[BusinessProcessPlan(name='event_occurrence', description='Recorded events')], dimensions=[], facts=[FactPlan(name='fact_events', business_process='event_occurrence', fact_type='transaction', grain='one row per event', source_datasets=['events'], output_fields=[ModelOutput(name='event_count', description='One per event', additivity='additive', definition=FieldDefinition(kind='constant', inputs=[], explanation='constant 1', value=1))], modelling_rationale='Reusable recorded observations at the declared grain', dimension_links=[], grain_key_fields=[])])
    profiles = {'events': {'columns': [{'name': 'event_id'}]}}
    assert plan_issues(plan, profiles) == []
    plan.business_processes.append(BusinessProcessPlan(name='account_lifecycle', description='Account changes'))
    plan.facts[0].output_fields.append(ModelOutput(name='event_total', description='Derived count at the declared grain', definition=FieldDefinition(kind='derive', inputs=[FieldRef(kind='source', dataset='events', column='missing')], explanation='Use the actual input value'), additivity='additive'))
    profiles['accounts'] = {'columns': [{'name': 'account_id'}]}
    issues = plan_issues(plan, profiles)
    assert {issue.code for issue in issues} == {'unimplemented_process', 'missing_source_field'}
