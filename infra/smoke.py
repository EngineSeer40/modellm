"""No-AWS image check: run real restricted dbt while Streamlit remains healthy."""
import json
import os
import threading
import urllib.request
from dataclasses import replace
from pathlib import Path

from src.config import ReleaseSettings, use_settings
from src.core.contracts import DbtFile, DbtImplementation
from src.database import connect
from src.dbt import run_dbt, write_project
from src.execution import RunManager
from src.workspace import RunWorkspace

assert os.getuid() == 10001
config = replace(ReleaseSettings.from_env(), live_enabled=True)
workspace = RunWorkspace.create(Path(config.runs_root), "smoke")
with use_settings(config):
    with connect(workspace.database, ingest=True) as con:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE TABLE raw.accounts AS SELECT 1 AS id")
    proposal = DbtImplementation(explanation="synthetic smoke", files=[
        DbtFile(path="dim_account.sql", content="SELECT * FROM {{ source('raw','accounts') }}"),
        DbtFile(path="schema.yml", content="version: 2\nmodels:\n  - name: dim_account\n    columns:\n      - name: id\n        data_tests: [unique, not_null]\n"),
    ])
    write_project(workspace.dbt_dir, workspace.database, ["accounts"], proposal)
    results = []
    thread = threading.Thread(target=lambda: results.append(run_dbt(workspace.dbt_dir, "build")))
    thread.start()
    checks = 0
    while thread.is_alive():
        with urllib.request.urlopen("http://127.0.0.1:8501/_stcore/health", timeout=3) as response:
            assert response.status == 200
        checks += 1
        thread.join(timeout=.2)
    assert results[0]["ok"], results[0]["output"]
    assert checks > 0
    with connect(workspace.database, read_only=True) as con:
        assert con.execute("SELECT * FROM main.dim_account").fetchall() == [(1,)]
manager = RunManager(config)
try:
    # Deliberate ingestion failure proves worker wiring without invoking Bedrock.
    job = manager.submit([("accounts.csv", b"id\n1\n"), ("events.json", b'{"records":[{"id":1}]}')], "Worker smoke", "smoke")
    state = job["future"].result(timeout=30)
    assert state["status"] == "failed" and "wrapped record array" in state["error"], state.get("error")
    assert not job["workspace"].input_dir.exists()
finally:
    manager.shutdown()
print(json.dumps({"health_checks_during_dbt": checks, "dbt": "passed", "worker": "passed", "uid": os.getuid()}))
