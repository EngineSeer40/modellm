import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from .core.contracts import DbtImplementation
from .data import identifier


def write_project(
    project: Path,
    database: Path,
    source_names: list[str],
    implementation: DbtImplementation,
) -> None:
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
    if not implementation.files or not any(
        f.path.endswith(".sql") for f in implementation.files
    ):
        raise ValueError("dbt implementation must contain SQL models")
    if not any(f.path.endswith(".yml") for f in implementation.files):
        raise ValueError("dbt implementation must contain schema.yml with tests")
    for file in implementation.files:
        path = Path(file.path)
        if (
            len(path.parts) != 1
            or path.suffix not in {".sql", ".yml"}
            or path.name == "sources.yml"
        ):
            raise ValueError(f"Invalid dbt path: {file.path}")
        if path.suffix == ".yml":
            document = yaml.safe_load(file.content)
            if not isinstance(document, dict):
                raise ValueError("schema.yml must contain a YAML mapping")
            if "sources" in document:
                raise ValueError("schema.yml must omit sources:; the application writes all source definitions in sources.yml")
        (models / path).write_text(file.content)


def run_dbt(project: Path, command: str) -> dict:
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
    env = dict(os.environ, DBT_SEND_ANONYMOUS_USAGE_STATS="false")
    try:
        result = subprocess.run(
            args,
            cwd=project,
            env=env,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
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
