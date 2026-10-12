import re
from pathlib import Path

import duckdb


def identifier(value: str) -> str:
    if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
        raise ValueError(f"Unsafe identifier: {value}")
    return value


def load_raw(database: Path, paths: list[Path]) -> list[str]:
    if not 2 <= len(paths) <= 5:
        raise ValueError("Provide 2 to 5 datasets")
    names = [identifier(re.sub(r"[^a-z0-9]+", "_", p.stem.lower()).strip("_")) for p in paths]
    if len(set(names)) != len(names):
        raise ValueError("Dataset names must be distinct")
    with duckdb.connect(str(database)) as con:
        con.execute("CREATE SCHEMA IF NOT EXISTS raw")
        for name, path in zip(names, paths):
            if path.suffix.lower() not in {".csv", ".parquet"}:
                raise ValueError(f"Unsupported file: {path.name}")
            reader = "read_csv_auto" if path.suffix.lower() == ".csv" else "read_parquet"
            con.execute(f'CREATE OR REPLACE TABLE raw."{name}" AS SELECT * FROM {reader}(?)', [str(path.resolve())])
    return names


def profile(database: Path, names: list[str]) -> dict:
    result = {}
    with duckdb.connect(str(database), read_only=True) as con:
        for name in names:
            identifier(name)
            table = f'raw."{name}"'
            count = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            fields = con.execute(f"DESCRIBE SELECT * FROM {table}").fetchall()
            columns = []
            for field, dtype, *_ in fields:
                q = '"' + field.replace('"', '""') + '"'
                nulls, distinct = con.execute(f"SELECT count(*) - count({q}), count(DISTINCT {q}) FROM {table}").fetchone()
                examples = [str(row[0]) for row in con.execute(f"SELECT DISTINCT {q} FROM {table} WHERE {q} IS NOT NULL LIMIT 5").fetchall()]
                top_values = []
                if distinct <= 20:
                    top_values = [{"value": str(value), "count": n} for value, n in con.execute(f"SELECT {q}, count(*) AS n FROM {table} GROUP BY 1 ORDER BY n DESC LIMIT 5").fetchall()]
                bounds = None
                if any(token in dtype.upper() for token in ("INT", "DECIMAL", "DOUBLE", "FLOAT", "DATE", "TIMESTAMP")):
                    low, high = con.execute(f"SELECT min({q}), max({q}) FROM {table}").fetchone()
                    bounds = {"min": str(low), "max": str(high)}
                columns.append({"name": field, "type": dtype, "nulls": nulls, "distinct": distinct, "unique": distinct == count and nulls == 0, "examples": examples, "top_values": top_values, "bounds": bounds})
            distinct_rows = con.execute(f"SELECT count(*) FROM (SELECT DISTINCT * FROM {table})").fetchone()[0]
            result[name] = {"rows": count, "columns": columns, "duplicate_rows": count - distinct_rows}
    return result


def relationships(database: Path, profiles: dict) -> list[dict]:
    found = []
    with duckdb.connect(str(database), read_only=True) as con:
        for child, cp in profiles.items():
            for column in cp["columns"]:
                key = column["name"]
                if not key.endswith("_id"):
                    continue
                for parent, pp in profiles.items():
                    if child == parent or key not in [c["name"] for c in pp["columns"]]:
                        continue
                    parent_col = next(c for c in pp["columns"] if c["name"] == key)
                    if not parent_col["unique"]:
                        continue
                    orphan = con.execute(f'SELECT count(*) FROM raw."{child}" c LEFT JOIN raw."{parent}" p ON c."{key}" = p."{key}" WHERE c."{key}" IS NOT NULL AND p."{key}" IS NULL').fetchone()[0]
                    found.append({"from": f"{child}.{key}", "to": f"{parent}.{key}", "cardinality": "many-to-one" if not column["unique"] else "one-to-one", "orphan_rows": orphan, "evidence": "matching name and unique referenced key"})
    return found


def schemas(database: Path, names: list[str], schema: str = "main") -> dict:
    result = {}
    with duckdb.connect(str(database), read_only=True) as con:
        for name in names:
            identifier(name)
            fields = con.execute(f'DESCRIBE SELECT * FROM "{schema}"."{name}"').fetchall()
            result[name] = {row[0]: row[1] for row in fields}
    return result


def preview(database: Path, schema: str, table: str, limit: int = 500):
    identifier(schema)
    identifier(table)
    with duckdb.connect(str(database), read_only=True) as con:
        return con.execute(f'SELECT * FROM "{schema}"."{table}" LIMIT {min(limit, 500)}').df()
