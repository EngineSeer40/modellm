import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from .core.contracts import DimensionalPlan
from .data import identifier
from .config import current_settings
from .database import connection_options
from .execution import current_budget
from .sql_policy import validate_dbt_files


def write_project(
    project: Path,
    database: Path,
    source_names: list[str],
    implementation: DimensionalPlan,
) -> None:
    validate_dbt_files(implementation, source_names)
    models = project / "models"
    models.mkdir(parents=True, exist_ok=True)
    for old in models.iterdir():
        if old.is_file() and old.suffix in {".sql", ".yml"}:
            old.unlink()
    (project / "dbt_project.yml").write_text(
        yaml.safe_dump(
            {
                "name": "modellm",
                "version": "1.0.0",
                "config-version": 2,
                "profile": "modellm",
                "model-paths": ["models"],
                "models": {"modellm": {"+materialized": "table"}},
            }
        )
    )
    (project / "profiles.yml").write_text(
        yaml.safe_dump(
            {
                "modellm": {
                    "target": "dev",
                    "outputs": {
                        "dev": {
                            "type": "duckdb",
                            "path": str(database.resolve()),
                            "schema": "main",
                            "threads": 1,
                            "config_options": connection_options(database),
                        }
                    },
                }
            }
        )
    )
    (models / "sources.yml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "sources": [
                    {
                        "name": "raw",
                        "schema": "raw",
                        "tables": [{"name": identifier(n)} for n in source_names],
                    }
                ],
            },
            sort_keys=False,
        )
    )
    for file in implementation.files:
        (models / file.path).write_text(file.content)


def run_dbt(project: Path, command: str) -> dict:
    if command not in {"compile", "build", "test"}:
        raise ValueError("Unsupported dbt command")
    project = project.resolve()
    environment_dbt = Path(sys.executable).parent / "dbt"
    executable = str(environment_dbt) if environment_dbt.exists() else "dbt"
    args = [
        executable,
        command,
        "--project-dir",
        str(project),
        "--profiles-dir",
        str(project),
        "--no-use-colors",
        "--no-static-parser",
        "--no-partial-parse",
    ]
    # Generated dbt code receives neither task-role credential endpoints nor preview/telemetry secrets.
    home = project / "home"
    home.mkdir(exist_ok=True, mode=0o700)
    env = {key: os.environ[key] for key in ("PATH", "LANG", "LC_ALL", "SYSTEMROOT") if key in os.environ}
    env.update(HOME=str(home), TMPDIR=str(home), DBT_SEND_ANONYMOUS_USAGE_STATS="false")
    budget = current_budget()
    remaining = budget.remaining_seconds() if budget else None
    timeout = min(current_settings().dbt_seconds, remaining) if remaining else current_settings().dbt_seconds
    try:
        result = subprocess.run(
            args,
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return {
            "command": command,
            "ok": result.returncode == 0,
            "output": result.stdout[-16000:],
        }
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return {"command": command, "ok": False, "output": str(exc)}


def dbt_results(project: Path) -> list[dict]:
    path = project / "target" / "run_results.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text())
    return [
        {"name": r["unique_id"], "status": r["status"], "message": r.get("message", "")}
        for r in data.get("results", [])
    ]
