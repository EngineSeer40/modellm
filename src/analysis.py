"""Accept, profile and load analytical results from validated DuckDB models."""

import math
import re
from pathlib import Path

from .database import connect
import pandas as pd
import sqlglot
from sqlglot import exp

from .core.contracts import (
    AcceptedAnalysisResult,
    AnalysisPlan,
    AnalysisSpec,
)
from .data import identifier
from .sql_policy import POPULATION_CTE, validate_analysis_sql, validate_variant_sql


class AnalysisAcceptanceError(RuntimeError):
    def __init__(self, index: int, title: str, error: Exception):
        self.index = index
        self.title = title
        self.database_error = str(error)
        super().__init__(f"Analysis {index} ({title}) failed: {error}")


def analysis_statement(spec: AnalysisSpec, plan: AnalysisPlan, model_names: list[str]) -> str:
    """Return the exact final SQL to execute, including an optional shared qualification."""
    if spec.query.kind == "direct":
        return validate_analysis_sql(spec.query.sql, model_names)
    qualifications = {item.qualification_id: item for item in plan.qualifications}
    if len(qualifications) != len(plan.qualifications):
        raise ValueError("Shared qualification IDs must be distinct")
    qualification = qualifications.get(spec.query.qualification_id)
    if qualification is None:
        raise ValueError(f"Unknown shared qualification: {spec.query.qualification_id}")
    base = validate_analysis_sql(qualification.sql, model_names)
    variant = validate_variant_sql(spec.query.sql)
    combined = f"WITH {POPULATION_CTE} AS ({base}) SELECT * FROM ({variant}) AS accepted_variant"
    return validate_analysis_sql(combined, model_names)


def accept_analyses(
    database: Path,
    model_names: list[str],
    analysis: AnalysisPlan,
) -> list[tuple[AnalysisSpec, AcceptedAnalysisResult]]:
    accepted: list[tuple[AnalysisSpec, AcceptedAnalysisResult]] = []

    try:
        ids = [q.qualification_id for q in analysis.qualifications]
        if len(ids) != len(set(ids)):
            raise ValueError("Shared qualification IDs must be distinct")
        for qualification in analysis.qualifications:
            validate_analysis_sql(qualification.sql, model_names)
            parsed = sqlglot.parse_one(qualification.sql, read="duckdb")
            if any(cte.alias_or_name.lower() == POPULATION_CTE for cte in parsed.find_all(exp.CTE)):
                raise ValueError(f"Qualification cannot redefine {POPULATION_CTE}")
    except Exception as exc:
        raise AnalysisAcceptanceError(-1, "shared qualifications", exc) from exc

    with connect(database) as con:
        con.begin()
        try:
            con.execute("CREATE SCHEMA IF NOT EXISTS analysis")
            for index, spec in enumerate(analysis.analyses):
                try:
                    statement = analysis_statement(spec, analysis, model_names)
                    result_id = f"result_{index:02d}"
                    relation = f'analysis."{result_id}"'
                    con.execute(f"CREATE OR REPLACE TABLE {relation} AS {statement}")
                    row_count = con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
                    described = con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()
                    columns = {row[0]: row[1] for row in described}
                    profile_frame = con.execute(f"SUMMARIZE SELECT * FROM {relation}").fetchdf()
                    profile_rows = (
                        profile_frame.astype(object)
                        .where(pd.notna(profile_frame), None)
                        .to_dict("records")
                    )
                    accepted_result = AcceptedAnalysisResult(
                        result_id=result_id,
                        relation=relation,
                        row_count=row_count,
                        columns=columns,
                        profile=profile_rows,
                    )
                    validate_result_semantics(spec, accepted_result)
                    accepted.append((spec, accepted_result))
                except Exception as exc:
                    raise AnalysisAcceptanceError(index, spec.title, exc) from exc
            con.commit()
        except Exception:
            con.rollback()
            raise

    return accepted


def validate_result_semantics(
    spec: AnalysisSpec,
    result: AcceptedAnalysisResult,
) -> None:
    available = set(result.columns)

    declared = {item.field for item in [*spec.dimensions, *spec.measures]}

    missing = declared - available
    if missing:
        raise ValueError(
            f"Analysis semantics reference missing columns "
            f"for {spec.title}: {sorted(missing)}"
        )
    if spec.variant_field and spec.variant_field not in available:
        raise ValueError(f"{spec.title}: missing variant field {spec.variant_field}")
    numeric = re.compile(r"^(?:U?(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|INT\d*|DECIMAL|DOUBLE|FLOAT|REAL)", re.I)
    for measure in spec.measures:
        components = measure.components
        if components is None:
            continue
        if not math.isfinite(components.scale) or components.scale <= 0:
            raise ValueError(f"{spec.title}: invalid ratio scale for {measure.field}")
        for field in (components.numerator_field, components.denominator_field):
            if field not in available:
                raise ValueError(f"{spec.title}: {measure.field} component {field} is missing")
            if not numeric.search(result.columns[field]):
                raise ValueError(f"{spec.title}: {measure.field} component {field} is not numeric")


def summarize_models(database: Path, model_names: list[str]) -> dict:
    summaries = {}
    with connect(database, read_only=True) as con:
        for name in model_names:
            table = f'main."{identifier(name)}"'
            summaries[name] = {
                "rows": con.execute(f"SELECT count(*) FROM {table}").fetchone()[0],
                "preview": con.execute(f"SELECT * FROM {table} LIMIT 3")
                .df()
                .astype(str)
                .to_dict("records"),
            }
    return summaries


def load_result_frame(
    database: Path,
    result: AcceptedAnalysisResult,
) -> pd.DataFrame:
    with connect(database, read_only=True) as con:
        return con.execute(f"SELECT * FROM {result.relation}").df()


def result_metrics(
    database: Path, results: list[tuple[AnalysisSpec, AcceptedAnalysisResult]]
) -> dict:
    """Use scalar accepted results as cards without assuming a business measure."""
    cards = {}
    for spec, result in results:
        if result.row_count != 1:
            continue
        frame = load_result_frame(database, result)
        for column in frame.select_dtypes(include="number").columns:
            value = frame.iloc[0][column]
            if pd.notna(value):
                cards[f"{spec.title}: {column}"] = float(value)
    return cards
