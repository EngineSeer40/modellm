"""Import unfamiliar post-dbt data without domain adapters; originals are never opened for writes."""

import json
import shutil
from pathlib import Path

import pandas as pd

from src.analysis import summarize_models
from src.config import ReleaseSettings, use_settings
from src.core.contracts import ConceptualPlan, AnalyticalPlan, AnalysisSpec, AcceptedAnalysisResult
from src.context_cost import configured_cost_experiments
from src.data import schemas
from src.database import connect
from src.modelling_validation import validate_built_models
from src.visualisation import representation_selections

from .contracts import ArtifactRef, PlottingFixture, Scenario, UpstreamFixture
from .io import digest, file_hash, read, safe_id, seal, unseal, write


def fixture_dir(root, fixture_id):
    return Path(root).resolve() / "fixtures" / safe_id(fixture_id)


def load_fixture(root, fixture_id):
    data, fingerprint = unseal(fixture_dir(root, fixture_id) / "fixture.json")
    return UpstreamFixture.model_validate(data), fingerprint


def load_scenario(root, scenario_id):
    data, fingerprint = unseal(Path(root) / "scenarios" / f"{safe_id(scenario_id)}.json")
    return Scenario.model_validate(data), fingerprint


def accepted_metadata(database, relation, result_id):
    # Relations must come from an enumerated accepted table, never arbitrary SQL.
    with connect(database, read_only=True) as con:
        names = {'analysis."' + r[0].replace('"', '""') + '"' for r in con.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='analysis' AND table_type='BASE TABLE'"
        ).fetchall()}
        if relation not in names:
            raise ValueError(f"Not a preserved accepted table: {relation}")
        columns = {r[0]: r[1] for r in con.execute(f"DESCRIBE SELECT * FROM {relation}").fetchall()}
        count = con.execute(f"SELECT count(*) FROM {relation}").fetchone()[0]
        profile = con.execute(f"SUMMARIZE SELECT * FROM {relation}").fetchdf()
        records = profile.astype(object).where(pd.notna(profile), None).to_dict("records")
    return AcceptedAnalysisResult(result_id=result_id, relation=relation, row_count=count,
                                  columns=columns, profile=records)


def _import(root, fixture_id, database, plan, request, settings, controls, provenance,
            record=None, declared_schemas=None):
    target = fixture_dir(root, fixture_id)
    target.mkdir(parents=True, exist_ok=False)
    database = Path(database).resolve()
    before = file_hash(database)
    wal = Path(str(database) + ".wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("Source has an active WAL; supply a stable exported snapshot")
    copied = target / "modellm.duckdb"
    shutil.copyfile(database, copied)
    if before != file_hash(database) or before != file_hash(copied) or (wal.exists() and wal.stat().st_size):
        raise ValueError("Source changed during import")
    plan = ConceptualPlan.model_validate(plan)
    settings = ReleaseSettings(**settings)
    settings.validate()
    controls = sorted(configured_cost_experiments(frozenset(controls)))
    names = sorted(m.name for m in [*plan.facts, *plan.dimensions])
    with use_settings(settings):
        actual = schemas(copied, names)
        if declared_schemas is not None and actual != declared_schemas:
            raise ValueError("Supplied model schemas disagree with actual database tables")
        with connect(copied, read_only=True) as con:
            for name in names:
                kind = con.execute("SELECT table_type FROM information_schema.tables WHERE table_schema='main' AND table_name=?", [name]).fetchone()
                if not kind or kind[0] != "BASE TABLE":
                    raise ValueError("Frozen upstream models must be materialized tables")
        issues = validate_built_models(copied, plan, actual)
        summaries = summarize_models(copied, names)
        plotting = []
        if record and record.get("analysis"):
            proposal = AnalyticalPlan.model_validate(record["analysis"])
            for i, metadata in enumerate(record.get("analysis_results", [])):
                relation = metadata["relation"]
                result_id = metadata.get("result_id", f"result_{i:02d}")
                # Canonical relation from table ID prevents SQL/path injection.
                if relation.replace('"', '') != f"analysis.{result_id}" or not result_id.replace('_', '').isalnum():
                    raise ValueError("Invalid saved relation identity")
                accepted = accepted_metadata(copied, f'analysis."{result_id}"', result_id)
                if accepted.row_count != metadata["rows"] or accepted.columns != metadata["columns"]:
                    raise ValueError("Saved accepted metadata disagrees with preserved table")
                plotting.append(PlottingFixture(spec=proposal.analyses[i], result=accepted, source_index=i,
                    historical_acceptance_id=metadata.get("acceptance_id")))
    if record:
        shutil.copyfile(provenance["record_path"], target / "historical.json")
        if file_hash(target / "historical.json") != provenance["record_sha256"]:
            raise ValueError("Source record changed during import")
        chart_refs = []
        for chart in record.get("charts", []):
            name = chart["file"]
            if Path(name).name != name:
                raise ValueError("Saved chart paths must be flat artifact filenames")
            origin = Path(provenance["record_path"]).parent / name
            if origin.is_file():
                evidence = target / "evidence" / name
                evidence.parent.mkdir(exist_ok=True)
                expected = file_hash(origin)
                shutil.copyfile(origin, evidence)
                if file_hash(evidence) != expected or file_hash(origin) != expected:
                    raise ValueError("Source chart changed during import")
                chart_refs.append({"path": f"evidence/{name}", "sha256": expected, "classification": "recorded"})
            else:
                chart_refs.append({"source": str(origin), "classification": "reported", "missing": True})
        provenance = {**provenance, "charts": chart_refs}
    provenance = {**provenance, "database_path": str(database), "database_sha256": before}
    fixture = UpstreamFixture(fixture_id=fixture_id,
        database=ArtifactRef(path="modellm.duckdb", sha256=file_hash(copied), classification="recorded",
                             source=str(database)), plan=plan, model_names=names, model_schemas=actual,
        summaries=summaries, request=request, settings=settings.snapshot(), controls=controls,
        settings_provenance=provenance.pop("settings_provenance"), source=provenance,
        plotting=plotting, validation_issues=issues)
    fingerprint = seal(target / "fixture.json", fixture)
    scenario = Scenario(scenario_id=fixture_id, fixture_id=fixture_id, fixture_hash=fingerprint, request=request)
    seal(Path(root) / "scenarios" / f"{fixture_id}.json", scenario)
    if before != file_hash(database):
        raise ValueError("Source changed during validation/import")
    return {"fixture_id": fixture_id, "fixture_hash": fingerprint, "validation_issues": issues,
            "plotting_results": len(plotting)}


def import_run(root, run, fixture_id, settings=None, controls=None):
    source = Path(run).resolve()
    if source.is_dir():
        source /= "result.json"
    record = json.loads(source.read_text())
    effective_settings = settings if settings is not None else record.get("release_settings")
    effective_controls = controls if controls is not None else record.get("cost_experiments")
    if effective_settings is None or effective_controls is None:
        raise ValueError("Missing historical settings/controls: supply an explicit settings file and controls list; these are reconstructed")
    return _import(root, fixture_id, source.parent / "modellm.duckdb", record["plan"],
        record["request"], effective_settings, effective_controls,
        {"record_path": str(source), "record_sha256": file_hash(source),
         "original_provenance": record.get("experiment", {}),
         "settings_provenance": "explicit reconstructed profile" if settings is not None else "recorded release_settings"},
        record, record.get("model_schemas"))


def import_bundle(root, bundle, fixture_id):
    bundle = Path(bundle).resolve()
    source = read(bundle)
    allowed = {"database", "plan", "request", "settings", "controls", "model_schemas"}
    if set(source) - allowed:
        raise ValueError(f"Unknown bundle keys: {sorted(set(source)-allowed)}")
    return _import(root, fixture_id, bundle.parent / source["database"], source["plan"],
        source["request"], source["settings"], source["controls"],
        {"bundle_path": str(bundle), "bundle_sha256": file_hash(bundle), "settings_provenance": "explicit bundle profile"},
        declared_schemas=source.get("model_schemas"))


def verify_fixture(root, fixture_id):
    fixture, fingerprint = load_fixture(root, fixture_id)
    database = fixture_dir(root, fixture_id) / fixture.database.path
    if file_hash(database) != fixture.database.sha256:
        raise ValueError("Frozen fixture database hash mismatch")
    return {"fixture_id": fixture_id, "fixture_hash": fingerprint, "validation_issues": fixture.validation_issues}


def create_scenario(root, fixture_id, scenario_id, request, reason, checks=None):
    fixture, fingerprint = load_fixture(root, fixture_id)
    scenario = Scenario(scenario_id=scenario_id, fixture_id=fixture_id, fixture_hash=fingerprint,
        request=request, parent_id=fixture_id, reason=reason, checks=checks or [])
    seal(Path(root) / "scenarios" / f"{safe_id(scenario_id)}.json", scenario)
    return scenario.model_dump()


def create_plotting_fixture(root, fixture_id, spec, relation, result_id, new_id=None, request=None, reason=None):
    # Derivations are separate files; never modify the imported fixture manifest.
    fixture, fingerprint = load_fixture(root, fixture_id)
    import tempfile
    with tempfile.TemporaryDirectory(dir=Path(root).resolve()) as staging:
        database = Path(staging) / "modellm.duckdb"
        shutil.copyfile(fixture_dir(root, fixture_id) / fixture.database.path, database)
        with use_settings(ReleaseSettings(**fixture.settings)):
            result = accepted_metadata(database, relation, result_id)
    from src.analysis import validate_result_semantics
    spec = AnalysisSpec.model_validate(spec)
    validate_result_semantics(spec, result)
    if request is not None and request != fixture.request and not reason:
        raise ValueError("An intentional request change needs a reason")
    derived = PlottingFixture(spec=spec, result=result, source_index=0)
    child_id = safe_id(new_id or f"{fixture_id}-result-{digest(derived)[:8]}")
    target = fixture_dir(root, child_id)
    target.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(fixture_dir(root, fixture_id) / fixture.database.path, target / "modellm.duckdb")
    child = fixture.model_copy(deep=True, update={"fixture_id": child_id, "plotting": [derived],
        "request": request or fixture.request, "source": {**fixture.source, "parent_fixture_hash": fingerprint,
        "registered_result_spec_hash": digest(spec), "change_reason": reason}})
    child_hash = seal(target / "fixture.json", child)
    seal(Path(root) / "scenarios" / f"{child_id}.json", Scenario(scenario_id=child_id,
        fixture_id=child_id, fixture_hash=child_hash, request=child.request, parent_id=fixture_id,
        reason=reason or "Explicit accepted-result registration"))
    return {"fixture_id": child_id, "fixture_hash": child_hash, "result_index": 0}


def clone_trial_database(source, destination, fixture, mode):
    shutil.copyfile(source, destination)
    if mode in {"analysis", "pipeline"}:
        with use_settings(ReleaseSettings(**fixture.settings)), connect(destination) as con:
            con.execute("DROP SCHEMA IF EXISTS analysis CASCADE")
            con.execute("DROP SCHEMA IF EXISTS raw CASCADE")
    return destination


def catalog(root):
    return [verify_fixture(root, p.name) for p in sorted((Path(root) / "fixtures").glob("*"))
            if (p / "fixture.json").is_file() and not (p / "unregistered.json").exists()]


def unregister(root, fixture_id):
    folder = fixture_dir(root, fixture_id)
    load_fixture(root, fixture_id)
    for p in (Path(root) / "outputs" / "trials").glob("*/running.json"):
        if read(p).get("fixture_id") == fixture_id:
            raise ValueError("Fixture is in use by an active trial")
    write(folder / "unregistered.json", {"fixture_id": fixture_id}, exclusive=True)
    return {"unregistered": fixture_id, "source_and_trial_data_preserved": True}
