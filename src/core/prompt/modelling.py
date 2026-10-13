DIMENSIONAL_MODEL = """
Implement the supplied AnalyticalPlan as a small dbt-duckdb project. Return model .sql files and schema.yml only, relative to models. 
Use {{ source('raw', 'dataset') }} and {{ ref('model') }}. Preserve declared atomic grains. 
Emit every planned output field and implement each declared source match path. Each fact join must use fields actually emitted by its referenced dimension. Apply the declared unmatched-row policy. Source-derived measures must use their declared inputs; explain any intentional constant. Quote source column names when necessary.
Include not_null, unique and relationship tests where justified; handle known orphan keys deliberately. 
Do not use macros, packages, Python models, external files or arbitrary commands. 
SQL must be DuckDB compatible. 
Do not redefine sources.yml; the application writes it.
Do not include a top-level 'sources:' section in schema.yml; the application owns all source declarations.
Naming of *.sql files must preserve the fact_*.sql and dim_*.sql plan names. 
Generate only the planned dim_*.sql and fact_*.sql models, don’t add staging/helper models.
Put .sql files directly under models/ with no nesting.

"""
