DIMENSIONAL_MODEL = """
Implement the supplied AnalyticalPlan as a small dbt-duckdb project. Return model .sql files and schema.yml only, relative to models. 
Use {{ source('raw', 'dataset') }} and {{ ref('model') }}. Preserve declared atomic grains. 
Emit every planned output field and implement each declared source match path. Each fact join must use fields actually emitted by its referenced dimension. Apply the declared unmatched-row policy. Source-derived measures must use their declared inputs; explain any intentional constant. Quote source column names when necessary.
Each model output is declared once in output_fields, with optional measure metadata. Read definition.inputs as exact source/model column references and definition.explanation as transformation intent; definition.value is used only for a declared constant.
On repair, use the previous implementation and the actual failure/observed rows. Preserve the declared grain when resolving joins. Quote YAML descriptions containing colons.
Include not_null, unique and relationship tests where justified; handle known orphan keys deliberately. 
Do not use macros, packages, Python models, external files or arbitrary commands. 
SQL must be DuckDB compatible. 
Do not redefine sources.yml; the application writes it.
Do not include a top-level 'sources:' section in schema.yml; the application owns all source declarations.
Naming of *.sql files must preserve the fact_*.sql and dim_*.sql plan names. 
Generate only the planned dim_*.sql and fact_*.sql models, don’t add staging/helper models.
Put .sql files directly under models/ with no nesting.

"""

DIMENSIONAL_MODEL_2 = """
Implement the accepted AnalyticalPlan as a small dbt-duckdb project. Return
model SQL files and schema.yml. Every output is defined once in output_fields;
measure metadata lives on that output. Emit the exact declared output names.

Preserve each model's declared grain. Resolve source/planned field references
and implement the transformation intent with DuckDB SQL. Use source() for raw
datasets and ref() for model dependencies. Implement each declared match path
and key pair, including differing paths for contributing sources and the stated
unmatched-row policy. Quote exact column names where necessary.

Never replace a source-dependent measure with a convenient constant. Constants
and independent generation are explicitly declared. Preserve source detail for
Analysis; request-specific calculations and chart requirements must not change
the reusable model. Explain implementation assumptions.

Include uniqueness, not-null and relationship tests only where justified by
grain, identity and unmatched policy. Passing tests does not prove business
meaning. When repairing, inspect the previous implementation and latest failure,
retain valid logic, and return the full corrected project.

Return only planned dim_*.sql/fact_*.sql and schema.yml, flat under models/.
No helper models, packages, macros, Python models, external files or commands.
The application owns sources.yml; do not redefine sources or include a sources:
section in schema.yml. All SQL must be DuckDB compatible.
"""
