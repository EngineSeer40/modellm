"""A local CSV run ledger; result.json remains the detailed record."""

import csv
import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
from collections import Counter
from datetime import datetime
from pathlib import Path

from .usage import summarize_usage

COLUMNS = (
    "run_id", "request_id", "scenario", "status", "started_at_utc", "ended_at_utc",
    "duration_seconds", "failure_stage", "contract_version", "models", "datasets",
    "analyses", "charts", "conceptual_repairs", "dbt_repairs", "analysis_repairs",
    "finding_repairs", "model_calls", "tool_errors", "input_tokens", "output_tokens",
    "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
    "total_tokens", "estimated_cost_usd", "usage_status", "pricing_basis",
    "git_commit", "git_dirty", "code_fingerprint", "input_fingerprint",
    "run_folder", "result_file", "request_summary", "error", "notes",
)
AGENT_COLUMNS = ("run_id", "agent", "models", "model_calls", "input_tokens",
                 "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
                 "output_tokens", "total_tokens", "estimated_cost_usd", "usage_status")


def request_id(request: str) -> str:
    return "req_" + hashlib.sha256(" ".join(request.split()).encode()).hexdigest()[:12]


def provenance(paths: list[Path]) -> dict:
    """Capture new-run provenance; never invent it for historical runs."""
    repo = Path(__file__).resolve().parents[1]
    digest = hashlib.sha256()
    files = sorted((repo / "src").rglob("*.py"))
    files += [repo / name for name in ("app.py", "pyproject.toml", "uv.lock")]
    for path in files:
        digest.update(path.relative_to(repo).as_posix().encode() + b"\0")
        digest.update(path.read_bytes())
    record = {"code_fingerprint": digest.hexdigest(), "app_version": os.environ.get("MODELLM_APP_VERSION", "0.1.0")}
    if os.environ.get("MODELLM_GIT_SHA"):
        record["git_commit"] = os.environ["MODELLM_GIT_SHA"]
    try:
        inputs = []
        for path in paths:
            with path.open("rb") as stream:
                inputs.append((path.name, hashlib.file_digest(stream, "sha256").hexdigest()))
        record["input_fingerprint"] = hashlib.sha256(json.dumps(sorted(inputs)).encode()).hexdigest()
    except OSError as exc:
        record["input_fingerprint_error"] = str(exc)
    try:
        record["git_commit"] = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        record["git_dirty"] = subprocess.run(
            ["git", "diff", "--quiet", "HEAD"], cwd=repo, stderr=subprocess.DEVNULL,
        ).returncode != 0
    except (OSError, subprocess.CalledProcessError):
        pass
    return record


def _read_csv(path):
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _repairs(state, key, stage):
    attempts = state.get(key)
    if isinstance(attempts, list):
        return sum(item.get("attempt", 1) > 1 for item in attempts)
    return sum(e.get("stage") == stage and e.get("kind") == "repair" for e in state.get("events", []))


def _row(run_dir, state):
    events = state.get("events", [])
    started = state.get("started_at_utc") or next((e["time"] for e in events if e.get("time")), "")
    terminal = state.get("status") in {"complete", "failed"}
    ended = next((e["time"] for e in reversed(events) if e.get("time") and e.get("stage") == "dashboard completion"), "") if terminal else ""
    if terminal and not ended:
        ended = next((e["time"] for e in reversed(events) if e.get("time")), "")
    duration = ""
    if started and ended:
        duration = round((datetime.fromisoformat(ended) - datetime.fromisoformat(started)).total_seconds(), 1)
    tool_errors = set()
    for index, event in enumerate(events):
        detail = event.get("detail", {})
        for block in detail.get("message", {}).get("content", []):
            result = block.get("toolResult", {})
            if result.get("status") == "error":
                tool_errors.add(result.get("toolUseId", index))
    last_stage = next((e.get("stage", "") for e in reversed(events) if e.get("stage") not in {"dashboard completion", "experiment tracking"}), "")
    identity = state.get("experiment", {})
    request = state.get("request", "")
    usage = state.get("usage") or summarize_usage(events)
    total = usage["total"]
    models = sorted({model for row in usage["agents"] for model in row["models"].split(";") if model})
    return {
        "run_id": state.get("run_id", run_dir.name), "request_id": request_id(request) if request else "",
        "status": state.get("status", "unknown"), "started_at_utc": started, "ended_at_utc": ended,
        "duration_seconds": duration, "failure_stage": last_stage if state.get("status") == "failed" else "",
        "contract_version": state.get("contract_version", ""), "models": ";".join(models),
        "datasets": len(state["profiles"]) if "profiles" in state else "",
        "analyses": len(state["analysis_results"]) if "analysis_results" in state else "",
        "charts": len(state["charts"]) if "charts" in state else "",
        "conceptual_repairs": _repairs(state, "conceptual_attempts", "conceptual modelling"),
        "dbt_repairs": _repairs(state, "dbt_attempts", "validation and repair"),
        "analysis_repairs": _repairs(state, "analysis_attempts", "analytical interpretation"),
        "finding_repairs": _repairs(state, "finding_attempts", "analytical finding"),
        "model_calls": total["model_calls"], "tool_errors": len(tool_errors),
        **{key: total[key] for key in ("input_tokens", "output_tokens", "total_tokens", "estimated_cost_usd", "usage_status")},
        **{key: total.get(key) for key in ("uncached_input_tokens", "cache_read_tokens", "cache_write_tokens")},
        "pricing_basis": usage["pricing_basis"], "_agents": usage["agents"],
        **{key: identity.get(key, "") for key in ("git_commit", "git_dirty", "code_fingerprint", "input_fingerprint")},
        "run_folder": f"../{run_dir.name}/", "result_file": f"../{run_dir.name}/result.json",
        "request_summary": " ".join(request.split())[:200], "error": " ".join(state.get("error", "").split())[:500],
    }


def _summary(rows):
    groups = {}
    for row in rows:
        label = row.get("scenario") or row.get("request_id") or "unidentified"
        groups.setdefault(label, Counter())[row["status"]] += 1
    text = ["# Experiment overview", "", "Generated from ledger.csv. Completion is a workflow outcome, not a semantic evaluation score.", "",
            "| Scenario / request | Complete | Failed | Running | Unreadable / other |", "|---|---:|---:|---:|---:|"]
    for label, counts in sorted(groups.items()):
        text.append(f"| {label} | {counts['complete']} | {counts['failed']} | {counts['running']} | {sum(counts.values()) - counts['complete'] - counts['failed'] - counts['running']} |")
    text += ["", "## Recent runs", "", "| Run | Scenario / request | Result | Seconds | Repairs | Analyses / charts | Tokens | Est. USD |", "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in sorted(rows, key=lambda r: (r.get("started_at_utc", ""), r["run_id"]), reverse=True)[:12]:
        repairs = sum(int(row.get(k) or 0) for k in ("conceptual_repairs", "dbt_repairs", "analysis_repairs", "finding_repairs"))
        cost = row.get("estimated_cost_usd")
        cost_text = f"{float(cost):.4f}" if cost not in (None, "") else ""
        text.append(f"| [{row['run_id']}]({row['result_file']}) | {row.get('scenario') or row.get('request_id') or 'unidentified'} | {row['status']} | {row.get('duration_seconds', '')} | {repairs} | {row.get('analyses', '')} / {row.get('charts', '')} | {row.get('total_tokens') or ''} | {cost_text} |")
    text += ["", "Token totals and USD cost estimates are in ledger.csv; per-agent details are in agent_usage.csv. Blank values are unavailable, not zero."]
    return "\n".join(text) + "\n"


def _write_csv(path, columns, rows):
    with tempfile.NamedTemporaryFile("w", dir=path.parent, newline="", encoding="utf-8", delete=False) as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
        temporary = Path(stream.name)
    temporary.replace(path)


def _write(root, records):
    folder = root / "experiments"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / ".ledger.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        rows = {row["run_id"]: row for row in _read_csv(folder / "ledger.csv")}
        agents = {(row["run_id"], row["agent"]): row for row in _read_csv(folder / "agent_usage.csv")}
        labels = {row["request_id"]: row["scenario"] for row in _read_csv(folder / "scenarios.csv")}
        for record in records:
            run_id = record["run_id"]
            agents = {key: value for key, value in agents.items() if key[0] != run_id}
            for agent in record.pop("_agents", []):
                agents[(run_id, agent["agent"])] = {
                    "run_id": run_id, **{key: agent.get(key) for key in AGENT_COLUMNS if key != "run_id"},
                }
            old = rows.get(record["run_id"], {})
            record["notes"] = old.get("notes", "")
            record["scenario"] = labels.get(record.get("request_id"), old.get("scenario", ""))
            rows[record["run_id"]] = record
        ordered = sorted(rows.values(), key=lambda row: (row.get("started_at_utc", ""), row["run_id"]))
        _write_csv(folder / "ledger.csv", COLUMNS, ordered)
        _write_csv(folder / "agent_usage.csv", AGENT_COLUMNS, [agents[key] for key in sorted(agents)])
        (folder / "overview.md").write_text(_summary(ordered))


def record_run(root: Path, state: dict):
    _write(root, [_row(Path(state["run_dir"]), state)])


def refresh(root: Path):
    records = []
    for path in sorted(root.glob("*/result.json")):
        try:
            state = json.loads(path.read_text())
            records.append(_row(path.parent, state))
        except (OSError, ValueError) as exc:
            records.append({"run_id": path.parent.name, "status": "unreadable",
                            "run_folder": f"../{path.parent.name}/", "result_file": f"../{path.parent.name}/result.json",
                            "error": f"{type(exc).__name__}: {exc}"})
    _write(root, records)
    return len(records)


if __name__ == "__main__":
    import sys
    root = Path(sys.argv[1] if len(sys.argv) > 1 else "runs").resolve()
    print(f"Recorded {refresh(root)} runs in {root / 'experiments' / 'ledger.csv'}")
