"""Release boundaries use real files/SQL/processes, and local SDK stubs only."""

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from strands import Agent

from src.config import DEFAULT_MODEL, ReleaseSettings, use_settings
from src.core.contracts import DbtFile, DbtImplementation, PlotSpec
from src.database import connect
from src.dbt import run_dbt, write_project
from src.execution import RunBudget, RunManager
from src.sql_policy import validate_dbt_files
from src.storage import cleanup_run_workspaces
from src.workspace import RunWorkspace, atomic_json, validate_submission
from test_context_cost import LocalModel

UPLOADS = [("accounts.csv", b"id\n1\n"), ("events.csv", b"id\n1\n")]


def settings(tmp_path, **kwargs):
    return replace(ReleaseSettings(), runs_root=str(tmp_path / "runs"), **kwargs)


def implementation(sql="SELECT * FROM {{ source('raw', 'accounts') }}", schema=None):
    return DbtImplementation(explanation="fixture", files=[
        DbtFile(path="dim_account.sql", content=sql),
        DbtFile(path="schema.yml", content=schema or "version: 2\nmodels:\n  - name: dim_account\n    columns:\n      - name: id\n        data_tests: [unique, not_null]\n"),
    ])


def test_prod_settings_are_strict_serializable_and_independent(monkeypatch):
    monkeypatch.setenv("MODELLM_PROD", "true")
    monkeypatch.setenv("MODELLM_LIVE_ACCESS_CODE", "secret-value")
    monkeypatch.setenv("MODELLM_COST_EXPERIMENTS", "conceptual_cache")
    prod = ReleaseSettings.from_env()
    assert prod.prod and not prod.live_enabled and prod.max_model_calls == 60
    assert "secret-value" not in json.dumps(prod.snapshot())
    monkeypatch.setenv("MODELLM_PROD", "false")
    local = ReleaseSettings.from_env()
    assert local.live_enabled and local.max_model_calls is None and local.max_file_bytes is None
    monkeypatch.setenv("MODELLM_MAX_MODEL_CALLS", "2")
    assert ReleaseSettings.from_env().max_model_calls == 2
    monkeypatch.setenv("MODELLM_PROD", "1")
    with pytest.raises(ValueError, match="true or false"):
        ReleaseSettings.from_env()


@pytest.mark.parametrize("uploads", [
    [("../accounts.csv", b"1"), UPLOADS[1]],
    [("a\\accounts.csv", b"1"), UPLOADS[1]],
    [("Account-A.csv", b"1"), ("account_a.json", b"2")],
    [(".csv", b"1"), UPLOADS[1]],
])
def test_upload_names_rejected_before_writes(tmp_path, uploads):
    with pytest.raises(ValueError):
        validate_submission(uploads, "request", settings(tmp_path))
    assert not (tmp_path / "runs").exists()


def test_workspace_ownership_expiry_and_active_size_budget(tmp_path):
    config = settings(tmp_path, workspace_bytes=100000, retention_seconds=1)
    first = RunWorkspace.create(Path(config.runs_root), "a")
    second = RunWorkspace.create(Path(config.runs_root), "b")
    paths = first.persist(UPLOADS, config)
    second.persist(UPLOADS, config)
    assert paths[0] != second.input_dir / paths[0].name
    # Links cannot turn owned cleanup into deletion of an unrelated directory.
    outside = tmp_path / "private"
    outside.mkdir()
    (outside / "keep").write_text("keep")
    (first.run_dir / "link").symlink_to(outside, target_is_directory=True)
    result = cleanup_run_workspaces(first.root, active={second.run_id}, settings=config, now=time.time()+5)
    assert result["removed_workspaces"] == 1
    assert second.run_dir.exists() and (outside / "keep").exists()


@pytest.mark.parametrize("sql", [
    "{{ config(pre_hook='select 1') }} SELECT * FROM {{ source('raw','accounts') }}",
    "{% set x = env_var('AWS_SECRET_ACCESS_KEY') %} SELECT * FROM {{ source('raw','accounts') }}",
    "SELECT '{{ cycler.__init__.__globals__ }}' FROM {{ source('raw','accounts') }}",
    "SELECT * FROM {{ source('other','accounts') }}",
    "SELECT * FROM {{ ref('unknown') }}",
    "SELECT * FROM {{ source('raw','accounts') }}; COPY raw.accounts TO '/tmp/leak.csv'",
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM raw.other",
    "SELECT * INTO stolen FROM {{ source('raw','accounts') }}",
])
def test_generated_sql_rejected_before_dbt(tmp_path, sql):
    project = tmp_path / "dbt"
    with pytest.raises(ValueError):
        write_project(project, tmp_path / "models.duckdb", ["accounts"], implementation(sql))
    assert not project.exists()


@pytest.mark.parametrize("extra", [
    "    config: {pre_hook: 'select 1'}\n", "    tests: [custom_test]\n",
    "    description: \"{{ env_var('HOME') }}\"\n", "    meta: {plugin: anything}\n",
])
def test_generated_yaml_rejects_execution_extensions(extra):
    schema = "version: 2\nmodels:\n  - name: dim_account\n" + extra
    with pytest.raises(ValueError):
        validate_dbt_files(implementation(schema=schema), ["accounts"])


def test_restricted_dbt_build_and_environment(tmp_path, monkeypatch):
    database = tmp_path / "models.duckdb"
    with connect(database, ingest=True) as con:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE TABLE raw.accounts AS SELECT 1 AS id")
    project = tmp_path / "dbt"
    write_project(project, database, ["accounts"], implementation())
    real_run = subprocess.run
    def inspect_env(*args, **kwargs):
        env = kwargs["env"]
        assert not any(key.startswith("AWS_") for key in env)
        assert "MODELLM_LIVE_ACCESS_CODE" not in env
        assert not any(key.startswith("LANGFUSE_") for key in env)
        return real_run(*args, **kwargs)
    monkeypatch.setenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "/sensitive")
    monkeypatch.setenv("MODELLM_LIVE_ACCESS_CODE", "private")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-private")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-private")
    monkeypatch.setattr(subprocess, "run", inspect_env)
    result = run_dbt(project, "build")
    assert result["ok"], result["output"]
    with connect(database, read_only=True) as con:
        assert con.execute("SELECT * FROM main.dim_account").fetchall() == [(1,)]
        with pytest.raises(Exception, match="(disabled|Cannot change)"):
            con.execute("SET enable_external_access=true")
        with pytest.raises(Exception):
            con.execute("SELECT * FROM read_csv('/etc/passwd')")
    with pytest.raises(ValueError):
        run_dbt(project, "run-operation")


def test_sdk_internal_cycles_share_one_budget():
    # An invalid structured tool response normally causes another SDK model call.
    model = LocalModel([("PlotSpec", {"function":"bar", "columns":{}, "filters":[], "options":{"template":"missing"}}),
                        ("PlotSpec", {"function":"bar", "columns":{}, "filters":[], "options":{}})])
    budget = RunBudget(ReleaseSettings(max_model_calls=1))
    agent = Agent(model=model, callback_handler=None, hooks=[budget.hooks(DEFAULT_MODEL, "ap-southeast-2")])
    async def consume():
        async for _ in agent.stream_async("plot", structured_output_model=PlotSpec):
            pass
    with pytest.raises(Exception, match="Internal model calls"):
        asyncio.run(consume())
    assert len(model.requests) == 1 and budget.calls == 1
    assert budget.cost > 0 and not budget.usage_unknown


def test_missing_usage_and_cache_pricing_cannot_count_as_free():
    budget = RunBudget(ReleaseSettings(max_cost_usd=3))
    budget.before_model()
    budget.record_response({}, DEFAULT_MODEL, "ap-southeast-2")
    with pytest.raises(RuntimeError, match="usage or pricing"):
        budget.before_model()
    priced = RunBudget(ReleaseSettings(max_cost_usd=.000001))
    priced.record_response({"metadata":{"usage":{"inputTokens":10,"outputTokens":10,"cacheReadInputTokens":100,"cacheWriteInputTokens":50}}}, DEFAULT_MODEL, "ap-southeast-2")
    assert priced.cost == pytest.approx(((10+10+62.5)*3.3+10*16.5)/1e6)
    with pytest.raises(RuntimeError, match="cost budget"):
        priced.before_model()


def test_worker_deadline_kills_descendants_releases_slot_and_preserves_usage(tmp_path, monkeypatch):
    import src.execution as execution
    real_popen = subprocess.Popen
    pid_file = tmp_path / "descendant.pid"
    script = ("import subprocess,sys,time; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)']); "
              f"open({str(pid_file)!r},'w').write(str(p.pid)); time.sleep(60)")
    def hanging_worker(args, **kwargs):
        assert "MODELLM_LIVE_ACCESS_CODE" not in kwargs["env"]
        assert kwargs["stderr"] is None
        return real_popen([sys.executable, "-c", script], **kwargs)
    monkeypatch.setattr(execution.subprocess, "Popen", hanging_worker)
    monkeypatch.setenv("MODELLM_LIVE_ACCESS_CODE", "private")
    manager = RunManager(settings(tmp_path, run_seconds=1, daily_runs=1))
    try:
        job = manager.submit(UPLOADS, "request", "owner")
        atomic_json(job["workspace"].result_path, {
            "status": "running", "events": [
                {"stage":"analytical finding", "kind":"model_call", "detail": {
                    "status":"started", "call_id":"accepted", "model_id":DEFAULT_MODEL, "region":"ap-southeast-2"}},
                {"stage":"analytical finding", "kind":"model_message", "detail": {
                    "call_id":"accepted", "message":{"metadata":{"usage":{"inputTokens":10,"outputTokens":10}}}}},
                {"stage":"analytical finding", "kind":"model_call", "detail": {
                    "status":"completed", "call_id":"accepted"}},
            ]})
        with pytest.raises(ValueError, match="Another analysis"):
            manager.submit(UPLOADS, "request", "other")
        state = job["future"].result(timeout=10)
        assert state["status"] == "failed" and "deadline" in state["error"]
        assert manager.active is None and not job["workspace"].input_dir.exists()
        assert state["usage"]["total"]["total_tokens"] == 20
        assert state["usage"]["total"]["estimated_cost_usd"] > 0
        assert state["usage"]["total"]["usage_status"] == "partial"
        pid = int(pid_file.read_text())
        stat = Path(f"/proc/{pid}/stat")
        assert not stat.exists() or stat.read_text().split()[2] == "Z"
        with pytest.raises(ValueError, match="Daily live runs"):
            manager.submit(UPLOADS, "request", "other")
    finally:
        manager.shutdown()
    restarted = RunManager(manager.settings)
    try:
        with pytest.raises(ValueError, match="Daily live runs"):
            restarted.submit(UPLOADS, "request", "other")
    finally:
        restarted.shutdown()


def test_ingest_rejects_source_shape_before_profiling(tmp_path):
    from src.data import load_raw
    files = [tmp_path / "accounts.csv", tmp_path / "events.csv"]
    for path in files:
        path.write_text("id,value\n1,10\n2,20\n3,30\n")
    with use_settings(settings(tmp_path, max_source_rows=1)):
        with pytest.raises(ValueError, match="Source rows"):
            load_raw(tmp_path / "limited.duckdb", files)
    with connect(tmp_path / "limited.duckdb", read_only=True) as con:
        assert con.execute("SELECT count(*) FROM raw.accounts").fetchone()[0] == 2


@pytest.mark.parametrize(("live_enabled", "access_code", "configured"), [
    ("false", "testcode", False),
    ("true", "", False), ("true", "1234567", False), ("true", "testcode", True),
    ("true", "a-longer-access-code-for-tests", True),
])
def test_live_access_requires_enabled_runs_and_configured_code(tmp_path, monkeypatch, live_enabled, access_code, configured):
    from streamlit.testing.v1 import AppTest
    from src.ui.app import run_manager
    monkeypatch.setenv("MODELLM_PROD", "true")
    monkeypatch.setenv("MODELLM_RUNS_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("MODELLM_LIVE_ENABLED", live_enabled)
    monkeypatch.setenv("MODELLM_LIVE_ACCESS_CODE", access_code)
    run_manager.clear()
    app = AppTest.from_file(Path(__file__).resolve().parents[1] / "app.py").run(timeout=15)
    try:
        assert not app.exception
        assert [tab.label for tab in app.tabs] == ["Run", "Results", "Data Model", "Technical Details"]
        assert not app.get("file_uploader")
        assert "result" not in app.session_state
        assert not any("example" in button.label.lower() for button in app.button)
        if not configured:
            assert not app.text_input
            return
        app.text_input[0].set_value("incorrect-code")
        next(button for button in app.button if button.label == "Unlock live runs").click().run(timeout=15)
        assert not app.exception and not app.get("file_uploader")
        assert any("not accepted" in error.value for error in app.error)
        app.session_state["_access_retry_at"] = 0
        app.text_input[0].set_value(access_code)
        next(button for button in app.button if button.label == "Unlock live runs").click().run(timeout=15)
        assert not app.exception and app.get("file_uploader")
    finally:
        run_manager().shutdown()
        run_manager.clear()


def test_demo_inputs_fill_editable_draft_and_use_normal_submission(tmp_path, monkeypatch):
    from io import BytesIO
    from streamlit.testing.v1 import AppTest
    from samples.sample_questions import SAMPLE_1, SAMPLE_1_FILES
    from src.ui.app import run_manager

    monkeypatch.setenv("MODELLM_PROD", "false")
    monkeypatch.setenv("MODELLM_LIVE_ENABLED", "true")
    monkeypatch.setenv("MODELLM_RUNS_ROOT", str(tmp_path / "runs"))
    submissions = []

    def capture_submission(self, uploads, request, owner):
        submissions.append((uploads, request))
        raise ValueError("Submission captured without running a model")

    monkeypatch.setattr(RunManager, "submit", capture_submission)
    run_manager.clear()
    root = Path(__file__).resolve().parents[1]
    app = AppTest.from_file(root / "app.py").run(timeout=15)
    try:
        next(b for b in app.button if b.label == "Insert Demo Data").click().run(timeout=15)
        assert not app.exception and not submissions
        assert app.text_area[0].value == SAMPLE_1.strip()
        assert app.get("file_uploader")
        assert app.session_state["_upload_version"] == 1
        assert "_pending_demo_uploads" not in app.session_state
        assert not any(str(button.key).startswith("remove_demo_") for button in app.button)
        # AppTest cannot execute browser JavaScript. Supply the files returned by
        # the native uploader after selection/removal; the bridge is browser-tested.
        selected = []
        for name in SAMPLE_1_FILES:
            if name != "invoices.json":
                file = BytesIO((root / "samples" / name).read_bytes())
                file.name = name
                selected.append(file)
        monkeypatch.setattr("src.ui.app.st.file_uploader", lambda *args, **kwargs: selected)
        app.run(timeout=15)
        app.text_area[0].set_value("My edited question").run(timeout=15)
        next(b for b in app.button if b.label == "Start Modelling").click().run(timeout=15)
        assert not app.exception
        uploads, request = submissions[0]
        assert request == "My edited question"
        assert dict(uploads) == {
            name: (root / "samples" / name).read_bytes()
            for name in SAMPLE_1_FILES if name != "invoices.json"
        }
    finally:
        run_manager().shutdown()
        run_manager.clear()


def test_nested_cte_alias_cannot_authorize_a_real_table():
    from src.sql_policy import validate_analysis_sql
    malicious = """WITH harmless AS (WITH private AS (SELECT * FROM main.fact_events)
                   SELECT * FROM private) SELECT * FROM private JOIN harmless ON true"""
    with pytest.raises(ValueError, match="unvalidated model"):
        validate_analysis_sql(malicious, ["fact_events"])
    valid = "WITH private AS (SELECT * FROM main.fact_events) SELECT * FROM private"
    assert validate_analysis_sql(valid, ["fact_events"]) == valid


def test_deployment_parameters_preserve_application_model_and_cost_defaults(monkeypatch):
    import yaml
    from src.context_cost import configured_cost_experiments
    for name in ("MODELLM_BEDROCK_MODEL_ID", "MODELLM_PLOTTING_MODEL_ID", "MODELLM_COST_EXPERIMENTS"):
        monkeypatch.delenv(name, raising=False)
    config = ReleaseSettings.from_env()
    root = Path(__file__).resolve().parents[1]
    template = yaml.load((root / "infra/service.yaml").read_text(), Loader=yaml.BaseLoader)
    defaults = {key: value.get("Default") for key, value in template["Parameters"].items()}
    example = json.loads((root / "infra/parameters.json.example").read_text())
    for values in (defaults, example):
        assert values["ModelId"] == config.model_id
        assert values["PlottingModelId"] == config.plotting_model_id
        assert frozenset(values["CostExperiments"].split(",")) == configured_cost_experiments()


def test_deployment_bootstrap_separates_github_trust_and_role_passing():
    import yaml
    root = Path(__file__).resolve().parents[1]
    template = yaml.load((root / "infra/deployment-roles.yaml").read_text(), Loader=yaml.BaseLoader)
    roles = template["Resources"]
    user = roles["CredentialsUser"]["Properties"]
    assert "LoginProfile" not in user and "Policies" not in user and "ManagedPolicyArns" not in user
    credentials_policy = roles["CredentialsPolicy"]["Properties"]
    assert credentials_policy["Users"] == ["CredentialsUser"]
    assert credentials_policy["PolicyDocument"]["Statement"] == [{
        "Effect": "Allow", "Action": "sts:AssumeRole", "Resource": ["BuildRole.Arn", "DeployRole.Arn"],
    }]
    assert not any(resource["Type"] in {"AWS::IAM::AccessKey", "AWS::IAM::OIDCProvider"} for resource in roles.values())
    for name in ("BuildRole", "DeployRole"):
        props = roles[name]["Properties"]
        trust = props["AssumeRolePolicyDocument"]["Statement"]
        assert trust == [{"Effect": "Allow", "Principal": {"AWS": "CredentialsUser.Arn"}, "Action": "sts:AssumeRole"}]
        statements = props["Policies"][0]["PolicyDocument"]["Statement"]
        for statement in statements:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            assert all(action.startswith(("ecr:", "cloudformation:")) or action == "iam:PassRole" for action in actions)
            if "iam:PassRole" in actions:
                assert statement["Resource"] == "CloudFormationRole.Arn"
                assert statement["Condition"]["StringEquals"]["iam:PassedToService"] == "cloudformation.amazonaws.com"
    cfn = roles["CloudFormationRole"]["Properties"]
    assert cfn["AssumeRolePolicyDocument"]["Statement"] == [{
        "Effect": "Allow", "Principal": {"Service": "cloudformation.amazonaws.com"}, "Action": "sts:AssumeRole",
    }]
    assert not any("ManagedPolicyArns" in role["Properties"] for role in roles.values())
    workflow = yaml.load((root / ".github/workflows/deploy.yml").read_text(), Loader=yaml.BaseLoader)
    for job, environment, variable in (("build", "release-build", "AWS_BUILD_ROLE_ARN"),
                                       ("deploy", "production", "AWS_DEPLOY_ROLE_ARN")):
        config = workflow["jobs"][job]
        assert config["environment"] == environment
        assert "id-token" not in config["permissions"]
        auth = next(step["with"] for step in config["steps"]
                    if step.get("uses", "").startswith("aws-actions/configure-aws-credentials@"))
        assert auth["aws-access-key-id"] == "${{ secrets.AWS_ACCESS_KEY_ID }}"
        assert auth["aws-secret-access-key"] == "${{ secrets.AWS_SECRET_ACCESS_KEY }}"
        assert auth["role-to-assume"] == "${{ vars." + variable + " }}"
        assert auth["role-skip-session-tagging"] == "true"
