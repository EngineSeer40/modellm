import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from .contracts import AnalyticalPlan, DbtImplementation
from .data import identifier


def demo_implementation() -> DbtImplementation:
    from .contracts import DbtFile

    return DbtImplementation(explanation="Atomic completed order items with customer and product dimensions; unmatched customers use an unknown member and unmatched products/orders are excluded.", files=[
        DbtFile(path="dim_customer.sql", content="""{{ config(materialized='table') }}
select cast(customer_id as varchar) as customer_key, customer_id, customer_name,
       coalesce(segment, 'Unknown') as segment, region
from {{ source('raw', 'customers') }}
union all
select 'unknown', null, 'Unknown customer', 'Unknown', 'Unknown'
"""),
        DbtFile(path="dim_product.sql", content="""{{ config(materialized='table') }}
select cast(product_id as varchar) as product_key, product_id, product_name,
       coalesce(category, 'Unknown') as category
from {{ source('raw', 'products') }}
"""),
        DbtFile(path="fact_order_items.sql", content="""{{ config(materialized='table') }}
select i.order_item_id, o.order_id, cast(o.order_date as date) as order_date,
       coalesce(c.customer_key, 'unknown') as customer_key,
       p.product_key, i.quantity, i.unit_price,
       cast(i.quantity * i.unit_price as decimal(18,2)) as revenue
from {{ source('raw', 'order_items') }} i
join {{ source('raw', 'orders') }} o on i.order_id = o.order_id
join {{ ref('dim_product') }} p on i.product_id = p.product_id
left join {{ ref('dim_customer') }} c on o.customer_id = c.customer_id
where lower(o.status) = 'completed'
"""),
        DbtFile(path="schema.yml", content=yaml.safe_dump({"version": 2, "models": [
            {"name": "dim_customer", "columns": [{"name": "customer_key", "data_tests": ["not_null", "unique"]}, {"name": "customer_id", "data_tests": ["unique"]}]},
            {"name": "dim_product", "columns": [{"name": "product_key", "data_tests": ["not_null", "unique"]}, {"name": "product_id", "data_tests": ["not_null", "unique"]}]},
            {"name": "fact_order_items", "columns": [
                {"name": "order_item_id", "data_tests": ["not_null", "unique"]},
                {"name": "customer_key", "data_tests": ["not_null", {"relationships": {"arguments": {"to": "ref('dim_customer')", "field": "customer_key"}}}]},
                {"name": "product_key", "data_tests": ["not_null", {"relationships": {"arguments": {"to": "ref('dim_product')", "field": "product_key"}}}]},
                {"name": "revenue", "data_tests": ["not_null"]},
            ]},
        ]}, sort_keys=False)),
    ])


def write_project(project: Path, database: Path, source_names: list[str], implementation: DbtImplementation) -> None:
    models = project / "models"
    models.mkdir(parents=True, exist_ok=True)
    for old in models.iterdir():
        if old.is_file() and old.suffix in {".sql", ".yml"}:
            old.unlink()
    (project / "dbt_project.yml").write_text(yaml.safe_dump({"name": "modellm", "version": "1.0.0", "config-version": 2, "profile": "modellm", "model-paths": ["models"], "models": {"modellm": {"+materialized": "table"}}}))
    (project / "profiles.yml").write_text(yaml.safe_dump({"modellm": {"target": "dev", "outputs": {"dev": {"type": "duckdb", "path": str(database.resolve()), "schema": "main", "threads": 1}}}}))
    (models / "sources.yml").write_text(yaml.safe_dump({"version": 2, "sources": [{"name": "raw", "schema": "raw", "tables": [{"name": identifier(n)} for n in source_names]}]}, sort_keys=False))
    if not implementation.files or not any(f.path.endswith(".sql") for f in implementation.files):
        raise ValueError("dbt implementation must contain SQL models")
    if not any(f.path.endswith(".yml") for f in implementation.files):
        raise ValueError("dbt implementation must contain schema.yml with tests")
    for file in implementation.files:
        path = Path(file.path)
        if len(path.parts) != 1 or path.suffix not in {".sql", ".yml"} or path.name == "sources.yml":
            raise ValueError(f"Invalid dbt path: {file.path}")
        if path.suffix == ".yml":
            yaml.safe_load(file.content)
        (models / path).write_text(file.content)


def run_dbt(project: Path, command: str) -> dict:
    project = project.resolve()
    environment_dbt = Path(sys.executable).parent / "dbt"
    executable = str(environment_dbt) if environment_dbt.exists() else "dbt"
    args = [
        executable, command,
        "--project-dir", str(project), "--profiles-dir", str(project),
        "--no-use-colors", "--no-static-parser", "--no-partial-parse",
    ]
    env = dict(os.environ, DBT_SEND_ANONYMOUS_USAGE_STATS="false")
    try:
        result = subprocess.run(args, cwd=project, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
        return {"command": command, "ok": result.returncode == 0, "output": result.stdout[-16000:]}
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "ok": False, "output": str(exc)}


def dbt_results(project: Path) -> list[dict]:
    path = project / "target" / "run_results.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [{"name": r["unique_id"], "status": r["status"], "message": r.get("message", "")} for r in data.get("results", [])]
