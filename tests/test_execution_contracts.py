"""Run-scoped contracts use discovered fields rather than a fixed business schema."""
from pathlib import Path
import duckdb
import pytest
from src.analysis import (
    AnalysisAcceptanceError, accept_analyses,
)
from src.visualisation import (
    render_plot, validate_plot_spec,
)
from src.modelling_validation import (
    validate_built_models,
)
from src.core.contracts import (
    AcceptedAnalysisResult, AnalysisDimension, AnalysisFinding, AnalysisMeasure, AnalysisPlan, AnalysisSpec, AnalyticalPlan, BusinessProcessPlan, DimensionPlan, EvidenceAggregate, FactDimensionLink, FactPlan, FindingStatement, PlotSpec, RatioComponents, ResultFilter, SharedQualification, SourceMatchPath, ModelOutput, FieldDefinition, FieldRef, MatchStep, MatchPair, OutputKeyPair, QueryDefinition, EvidenceRequest, EvidenceSort, EvidenceRowRef, FindingCell,
)
from src.modelling_validation import (
    plan_issues,
)
from src.tools.result_tools import DuckDBResultTools, EvidenceRegistry, render_finding

def source_plan() -> AnalyticalPlan:
    """Use names that have no relationship to the recorded customer fixture."""
    return AnalyticalPlan(assumptions=[], unresolved_questions=[], business_processes=[BusinessProcessPlan(name='observation', description='Specimens were observed')], dimensions=[DimensionPlan(name='dim_specimen', business_entity='Specimen', grain='one row per specimen', source_datasets=['registry'], natural_key_fields=['specimen id'], surrogate_key='specimen id', output_fields=[ModelOutput(name='specimen id', description='Registry identifier', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='registry', column='Specimen Code')])), ModelOutput(name='field code', description='Field match code', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='registry', column='Field Ref')])), ModelOutput(name='lab code', description='Lab match code', definition=FieldDefinition(kind='copy', inputs=[FieldRef(kind='source', dataset='registry', column='Lab Ref')]))], grain_key_fields=['specimen id'])], facts=[FactPlan(name='fact_observations', business_process='observation', fact_type='transaction', grain='one row per observation', source_datasets=['field_notes', 'lab_notes'], dimension_links=[FactDimensionLink(dimension_model='dim_specimen', source_paths=[SourceMatchPath(source_dataset='field_notes', steps=[MatchStep(pairs=[MatchPair(left=FieldRef(kind='source', column='Field Ref', dataset='field_notes'), right=FieldRef(kind='model', column='field code', model='dim_specimen'))])]), SourceMatchPath(source_dataset='lab_notes', steps=[MatchStep(pairs=[MatchPair(left=FieldRef(kind='source', column='Lab Ref', dataset='lab_notes'), right=FieldRef(kind='model', column='lab code', model='dim_specimen'))])])], unmatched_policy='null', key_pairs=[OutputKeyPair(fact_field='specimen link', dimension_field='specimen id')])], output_fields=[ModelOutput(name='observation id', description='Observation identity', definition=FieldDefinition(kind='generate', inputs=[], explanation='Generated from source observation identity')), ModelOutput(name='specimen link', description='Specimen reference', definition=FieldDefinition(kind='generate', inputs=[], explanation='Matched through declared source paths')), ModelOutput(name='observation count', description='One observation', additivity='additive', definition=FieldDefinition(kind='constant', inputs=[], explanation='constant 1', value=1))], grain_key_fields=['observation id'], modelling_rationale='Reusable recorded observations at the declared grain')])

def profiles() -> dict:
    return {'registry': {'columns': [{'name': name} for name in ('Specimen Code', 'Field Ref', 'Lab Ref')]}, 'field_notes': {'columns': [{'name': 'Field Ref'}]}, 'lab_notes': {'columns': [{'name': 'Lab Ref'}]}}

def analysis_spec(sql: str, *, components: RatioComponents | None=None) -> AnalysisSpec:
    measures = [AnalysisMeasure(field='observed', semantic_type='count', description='Observed count', additivity='additive', components=components)]
    return AnalysisSpec(title='Observation result', question='How many observations?', concept_id='observations', population_definition='Observed specimens', rationale='Count the accepted observations', grain='one row per specimen', dimensions=[AnalysisDimension(field='specimen', semantic_type='categorical', description='Specimen')], measures=measures, visualisation_goal='Show observations by specimen', query=QueryDefinition(kind='direct', sql=sql), observation_rule=None)

def test_source_references_are_exact_and_support_multiple_paths():
    plan = source_plan()
    assert plan_issues(plan, profiles()) == []
    changed = source_plan()
    changed.dimensions[0].output_fields[1].definition.inputs[0].column = 'field_ref'
    issues = plan_issues(changed, profiles())
    assert any(('registry.field_ref' in issue.message and 'Field Ref' in issue.expected for issue in issues))

def test_source_contract_is_invariant_to_run_specific_names():
    plan = source_plan()
    original = profiles()
    datasets = {'registry': 'catalogue', 'field_notes': 'observations_a', 'lab_notes': 'observations_b'}
    columns = {'Specimen Code': 'Object Identifier', 'Field Ref': 'Survey Token', 'Lab Ref': 'Assay Token'}
    renamed_profiles = {datasets[name]: {'columns': [{'name': columns[column['name']]} for column in value['columns']]} for name, value in original.items()}
    for model in plan.dimensions + plan.facts:
        model.source_datasets = [datasets[name] for name in model.source_datasets]
        for output in model.output_fields:
            for ref in output.definition.inputs:
                ref.dataset = datasets[ref.dataset]
                ref.column = columns[ref.column]
    for fact in plan.facts:
        for link in fact.dimension_links:
            for path in link.source_paths:
                path.source_dataset = datasets[path.source_dataset]
                for step in path.steps:
                    for pair in step.pairs:
                        for endpoint in (pair.left, pair.right):
                            if endpoint.kind == 'source':
                                endpoint.dataset = datasets[endpoint.dataset]
                                endpoint.column = columns[endpoint.column]
    assert plan_issues(plan, renamed_profiles) == []

def test_built_outputs_and_declared_grain_are_checked(tmp_path: Path):
    database = tmp_path / 'models.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute('CREATE TABLE main.dim_specimen AS SELECT 1 AS "specimen id", 7 AS "field code"')
        con.execute('CREATE TABLE main.fact_observations AS SELECT 1 AS "observation id", 1 AS "specimen link", 1 AS "observation count" UNION ALL SELECT 1, 1, 1')
    built = {'dim_specimen': {'specimen id': 'INTEGER', 'field code': 'INTEGER'}, 'fact_observations': {'observation id': 'INTEGER', 'specimen link': 'INTEGER', 'observation count': 'INTEGER'}}
    issues = validate_built_models(database, source_plan(), built)
    assert any(('lab code' in issue for issue in issues))
    assert any(('duplicates declared grain keys' in issue for issue in issues))

def test_analysis_acceptance_rolls_back_all_results_and_retries_cleanly(tmp_path: Path):
    database = tmp_path / 'modellm.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE main.fact_observations AS SELECT 'fern' AS specimen, 2 AS observed")
    good = 'SELECT specimen, observed FROM main.fact_observations'
    bad = 'SELECT specimen, missing AS observed FROM main.fact_observations'
    with pytest.raises(AnalysisAcceptanceError, match='missing'):
        accept_analyses(database, ['fact_observations'], AnalysisPlan(analyses=[analysis_spec(good), analysis_spec(bad)], qualifications=[], warnings=[]))
    with duckdb.connect(str(database), read_only=True) as con:
        tables = con.execute("SELECT table_name FROM information_schema.tables WHERE table_schema = 'analysis'").fetchall()
    assert tables == []
    accepted = accept_analyses(database, ['fact_observations'], AnalysisPlan(analyses=[analysis_spec(good)], qualifications=[], warnings=[]))
    assert accepted[0][1].row_count == 1

def test_shared_qualification_variants_cannot_read_other_sources(tmp_path: Path):
    database = tmp_path / 'variants.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE main.fact_observations AS SELECT 'fern' AS specimen, 2 AS observed")
        con.execute('CREATE SCHEMA raw')
        con.execute('CREATE TABLE raw.secret AS SELECT 99 AS observed')
    qualification = SharedQualification(qualification_id='observations', sql='SELECT specimen, observed FROM main.fact_observations', grain='one row per observation', population_definition='Observed specimens')
    spec = analysis_spec('SELECT specimen, observed FROM main.fact_observations')
    spec.query = QueryDefinition(kind='qualified', qualification_id='observations', sql='SELECT specimen, SUM(observed) AS observed FROM __modellm_population GROUP BY specimen')
    accepted = accept_analyses(database, ['fact_observations'], AnalysisPlan(qualifications=[qualification], analyses=[spec], warnings=[]))
    assert accepted[0][1].row_count == 1
    spec.query.sql = 'SELECT p.specimen, s.observed FROM __modellm_population p JOIN raw.secret s ON true'
    with pytest.raises(AnalysisAcceptanceError, match='another table|shared population'):
        accept_analyses(database, ['fact_observations'], AnalysisPlan(qualifications=[qualification], analyses=[spec], warnings=[]))

def test_ratio_components_must_exist_in_accepted_result(tmp_path: Path):
    database = tmp_path / 'ratio.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE main.fact_observations AS SELECT 'fern' AS specimen, 2 AS observed")
    spec = analysis_spec('SELECT specimen, observed FROM main.fact_observations', components=RatioComponents(numerator_field='observed', denominator_field='possible'))
    with pytest.raises(AnalysisAcceptanceError, match='possible'):
        accept_analyses(database, ['fact_observations'], AnalysisPlan(analyses=[spec], qualifications=[], warnings=[]))

def test_empty_results_preserve_current_result_schema(tmp_path: Path):
    database = tmp_path / 'empty.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE main.fact_observations AS SELECT 'fern' AS specimen, 2 AS observed")
    spec = analysis_spec("SELECT specimen, observed FROM main.fact_observations WHERE specimen = 'moss'")
    # Current contracts accept empty results; plotting renders a figure without a new model call.
    accepted = accept_analyses(database, ['fact_observations'], AnalysisPlan(analyses=[spec], qualifications=[], warnings=[]))
    assert accepted[0][1].row_count == 0
    assert set(accepted[0][1].columns) == {'specimen', 'observed'}


def test_variant_expansion_is_checked_before_analysis_commit(tmp_path: Path):
    database = tmp_path / 'variants_limit.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE TABLE main.fact_observations AS SELECT * FROM (VALUES "
                    "('fern', 2), ('moss', 3), ('fern', 4)) t(specimen, observed)")
    spec = analysis_spec('SELECT specimen, observed FROM main.fact_observations')
    spec.variant_field = 'observed'
    plan = AnalysisPlan(analyses=[spec], qualifications=[], warnings=[])
    with pytest.raises(AnalysisAcceptanceError, match='3 figures.*limit of 2'):
        accept_analyses(database, ['fact_observations'], plan, max_charts=2)
    with duckdb.connect(str(database), read_only=True) as con:
        assert not con.execute("SELECT 1 FROM information_schema.tables "
                               "WHERE table_schema = 'analysis'").fetchall()
    spec.variant_field = 'specimen'
    assert len(accept_analyses(database, ['fact_observations'], plan, max_charts=2)) == 1

def test_findings_bind_numbers_to_accepted_result_and_plot_fields_are_scoped(tmp_path: Path):
    database = tmp_path / 'result.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute('CREATE SCHEMA analysis')
        con.execute("CREATE TABLE analysis.result_00 AS SELECT 'fern' AS specimen, 185 AS observed, 269 AS possible")
    spec = analysis_spec('SELECT specimen, observed FROM main.fact_observations')
    spec.measures.append(AnalysisMeasure(field='possible', semantic_type='count', description='Possible count', additivity='additive'))
    result = AcceptedAnalysisResult(result_id='result_00', relation='analysis."result_00"', row_count=1, columns={'specimen': 'VARCHAR', 'observed': 'INTEGER', 'possible': 'INTEGER'}, profile=[])
    registry = EvidenceRegistry(result.acceptance_id, result.relation)
    tools = DuckDBResultTools(database, result.relation, result.columns, spec, registry)
    record = tools.inspect_result(request=EvidenceRequest(kind='summary', aggregates={'n': EvidenceAggregate(function='sum', field='observed'), 'd': EvidenceAggregate(function='sum', field='possible')}).model_dump())
    finding = AnalysisFinding(statements=[FindingStatement(template='{n} of {d} observations were recorded.', selected_rows={'total': EvidenceRowRef(evidence_id=record['id'], row_id=record['rows'][0]['row_id'])}, bindings={'n': FindingCell(row='total', column='n'), 'd': FindingCell(row='total', column='d')})], caveats=[])
    rendered, evidence = render_finding(finding, registry)
    assert rendered == '185 of 269 observations were recorded.'
    assert evidence[0]['rows'][0]['values'] == {'n': 185, 'd': 269}
    finding.statements[0].template = '238 of {d} observations were recorded.'
    with pytest.raises(ValueError, match='bindings without placeholders'):
        render_finding(finding, registry)
    plot = PlotSpec(function='bar', columns={'x': 'specimen', 'y': 'observed'}, filters=[ResultFilter(field='specimen', operator='eq', values=['fern'])], options={})
    validate_plot_spec(plot, result, plot.filters)
    with duckdb.connect(str(database), read_only=True) as con:
        frame = con.execute('SELECT * FROM analysis.result_00').df()
    assert len(render_plot(frame, plot, 'Observations').data) == 1
    plot.columns['y'] = 'unknown upstream field'
    with pytest.raises(ValueError, match='missing accepted-result column'):
        validate_plot_spec(plot, result, plot.filters)
