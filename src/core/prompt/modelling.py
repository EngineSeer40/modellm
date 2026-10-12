DIMENSIONAL_MODEL = """
Implement the supplied AnalyticalPlan as a small dbt-duckdb project. Return model .sql files and schema.yml only, relative to models. 
Use {{ source('raw', 'dataset') }} and {{ ref('model') }}. Preserve declared atomic grains. 
Include not_null, unique and relationship tests where justified; handle known orphan keys deliberately. 
Do not use macros, packages, Python models, external files or arbitrary commands. 
SQL must be DuckDB compatible. Do not redefine sources.yml; the application writes it.

Conventions:
- Naming of *.sql files must preserve the fact_* and dim_* plan names  

"""
