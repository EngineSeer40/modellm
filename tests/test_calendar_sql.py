"""Replay real calendar proposals through the release policy and restricted dbt."""

from pathlib import Path

import pytest

from src.core.contracts import DbtFile, DimensionalPlan
from src.database import connect
from src.dbt import run_dbt, write_project
from src.sql_policy import validate_dbt_files
from src.sql_policy import (
    validate_analysis_sql,
)


FIXTURES = Path(__file__).parent / "fixtures" / "calendar_sql"
SOURCES = ["transactions_online", "transactions_instore", "invoices"]
SERIES = "generate_series(date '2023-01-01', date '2023-01-03', interval '1 day')"


def calendar_implementation(sql):
    return DimensionalPlan(explanation="Calendar regression", files=[
        DbtFile(path="dim_date.sql", content=sql),
        DbtFile(path="schema.yml", content="version: 2\nmodels:\n  - name: dim_date\n"),
    ])


def test_historical_and_failed_calendars_build_under_restricted_dbt(tmp_path):
    # Three completed historical runs plus the latest initial/repair proposals.
    # The latest third proposal repeats the second calendar SQL verbatim.
    files, schema_models = [], []
    for fixture in sorted(FIXTURES.glob("*.sql")):
        name = "dim_date_" + fixture.stem
        files.append(DbtFile(path=name + ".sql", content=fixture.read_text()))
        schema_models.append(f"  - name: {name}\n    columns:\n      - name: date_day\n        data_tests: [unique, not_null]\n")
    assert len(files) == 5
    files.append(DbtFile(path="schema.yml", content="version: 2\nmodels:\n" + "".join(schema_models)))
    implementation = DimensionalPlan(explanation="Saved calendar SQL", files=files)
    database = tmp_path / "models.duckdb"
    with connect(database, ingest=True) as con:
        con.execute("CREATE SCHEMA raw")
        for name in SOURCES:
            column = "invoice_date" if name == "invoices" else "transaction_ts"
            con.execute(f"CREATE TABLE raw.{name} AS SELECT d AS {column} FROM "
                        "generate_series(timestamp '2023-01-01', timestamp '2023-01-03', interval '1 day') dates(d)")
    project = tmp_path / "dbt"
    write_project(project, database, SOURCES, implementation)
    result = run_dbt(project, "build")
    assert result["ok"], result["output"]
    with connect(database, read_only=True) as con:
        for file in files[:-1]:
            rows, distinct, minimum, maximum = con.execute(
                f'SELECT count(*), count(distinct date_day), min(date_day), max(date_day) FROM main."{Path(file.path).stem}"'
            ).fetchone()
            assert rows == distinct and rows in (3, 1461)
            assert str(minimum) == "2023-01-01"
            assert str(maximum) == ("2023-01-03" if rows == 3 else "2026-12-31")


@pytest.mark.parametrize("sql", [
    f"SELECT * FROM {SERIES}",
    f"SELECT unnest({SERIES}) AS date_day",
    f"SELECT * FROM unnest({SERIES}) AS dates(date_day)",
])
def test_calendars_are_allowed_only_for_dbt(sql):
    validate_dbt_files(calendar_implementation(sql), SOURCES)
    with pytest.raises(ValueError):
        validate_analysis_sql(sql, ["fact_events"])


@pytest.mark.parametrize("sql, reason", [
    ("SELECT * FROM range(1000000000)", "finite start/end"),
    ("SELECT * FROM generate_series(0, 1000000000, 1)", "whole-day"),
    ("SELECT * FROM generate_series(date '2023-01-01',date '2023-01-03',interval '0 day')", "whole-day"),
    ("SELECT * FROM generate_series(date '2023-01-01',date '2023-01-03',interval '1 second')", "whole-day"),
    ("SELECT * FROM generate_series(date '-infinity',date 'infinity',interval '1 day')", "finite start/end"),
    (f"SELECT * FROM raw.{SERIES}", "relation/table function"),
    (f"SELECT unnest([1,2]) FROM {SERIES}", "table function"),
    (f"SELECT * FROM {SERIES}, read_csv('/etc/passwd')", "relation/table function"),
    (f"SELECT * FROM {SERIES}, private.accounts", "unvalidated model"),
    (f"SELECT unnest(generate_series((SELECT min(d) FROM private.accounts), date '2023-01-03', interval '1 day'))", "unvalidated model"),
])
def test_calendar_exception_keeps_other_sql_restricted(sql, reason):
    with pytest.raises(ValueError, match=f"dim_date.sql: .*{reason}"):
        validate_dbt_files(calendar_implementation(sql), SOURCES)
