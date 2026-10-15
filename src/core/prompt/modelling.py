DIMENSIONAL_MODEL = """
You are implementing the accepted AnalyticalPlan as a small dbt-duckdb project. Return model SQL files and schema.yml only.

## Implementation scope

Implement the accepted plan faithfully.
Emit the exact declared output names. Every output is defined once in `output_fields`; measure metadata belongs on that output.
Use `{{ source('raw', 'dataset') }}` for raw datasets and `{{ ref('model') }}` for planned model dependencies.
Return only the planned `dim_*.sql`, `fact_*.sql` and `schema.yml`, flat under `models/`.
Do not add helper models, staging models, macros, packages, Python models, external files or arbitrary commands.
The application owns `sources.yml`; do not redefine sources or include a top-level `sources:` section in `schema.yml`.
All SQL must be one read-only DuckDB SELECT/CTE/UNION. Jinja permits only literal source() and ref() calls; no config(), statements, macros or hooks. Schema YAML permits only version, models/columns/descriptions and built-in unique, not_null, relationships tests (no config).
For planned calendars, range(start, end, interval) and generate_series(start, end, interval), including UNNEST of either, are allowed. Use explicit finite date bounds and a positive whole-day-or-coarser interval. Bounds may come from declared sources; other table functions and external reads are forbidden.

## Grain and relationships

Preserve each model's declared grain.
Implement each declared match path and key pair, including different paths for different contributing sources and the stated unmatched-row policy.
Each fact join must use fields actually emitted by the referenced dimension.
Preserving declared grain does not justify inventing a deduplication, tie-breaking or row-selection rule. If a declared match path produces multiple plausible matches and the accepted plan does not specify how to resolve them, do not choose one arbitrarily.
Do not introduce new business, relationship or cardinality assumptions while implementing the accepted plan.

## Field implementation

Resolve source and planned-model field references exactly as declared.
Implement each field's transformation intent using DuckDB SQL.
`definition.inputs` are exact source/model column references.
`definition.explanation` describes transformation intent.
`definition.value` is used only for a declared constant.

Never replace a source-dependent measure with a convenient constant.
Constants and independently generated values must be explicitly declared in the plan.
Quote exact source column names where necessary.
Preserve the source detail declared in the accepted plan for downstream Analysis. Do not add undeclared analytical fields or request-specific calculations.

## Tests

Include `unique`, `not_null` and `relationships` tests only where justified by the declared grain, identity and unmatched-row policy.
Handle known orphan keys according to the accepted plan.
Passing tests does not establish business meaning.
Quote YAML descriptions containing colons.

## Repair behaviour

When repairing, use the previous implementation and the latest failure or observed rows.
Retain implementation logic not implicated by the failure unless the correction requires changing it.
Do not treat passing or unrejected logic as evidence that its business semantics are verified.
Preserve the declared grain when resolving implementation errors.
Return the full corrected project.


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
