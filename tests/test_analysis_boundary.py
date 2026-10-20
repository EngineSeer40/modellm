from pathlib import Path
import duckdb
import pytest
from src.tools.analysis_tools import execute_analysis_sql

def test_analysis_sql_joins_validated_models_and_rejects_other_sources(tmp_path: Path):
    database = tmp_path / 'modellm.duckdb'
    with duckdb.connect(str(database)) as con:
        con.execute('CREATE SCHEMA raw')
        con.execute('CREATE TABLE raw.secret AS SELECT 99 AS value')
        con.execute('CREATE TABLE main.fact_sales AS SELECT 1 AS customer_key, 12 AS revenue')
        con.execute("CREATE TABLE main.dim_customer AS SELECT 1 AS customer_key, 'Acme' AS customer_name")
    frame, truncated = execute_analysis_sql(database, 'SELECT c.customer_name, f.revenue FROM main.fact_sales f JOIN main.dim_customer c USING (customer_key)', ['fact_sales', 'dim_customer'])
    assert frame.to_dict('records') == [{'customer_name': 'Acme', 'revenue': 12}]
    assert not truncated
    for sql in ('SELECT * FROM raw.secret', 'SELECT * FROM main.unvalidated', "SELECT * FROM read_csv_auto('/tmp/missing.csv')", 'SELECT * FROM main.fact_sales; DROP TABLE main.fact_sales'):
        with pytest.raises(ValueError):
            execute_analysis_sql(database, sql, ['fact_sales', 'dim_customer'])
