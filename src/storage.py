import json
import os
import time
from pathlib import Path


DEFAULT_DUCKDB_RETENTION_DAYS = 14
DEFAULT_DUCKDB_BUDGET_BYTES = 2 * 1024**3
STALE_RUNNING_AFTER_SECONDS = 24 * 60 * 60


def cleanup_run_databases(root: Path, *, now: float | None = None) -> dict:
    """Expire old per-run DuckDB files and enforce a retained database size budget."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    now = time.time() if now is None else now
    retention_days = int(os.environ.get("MODELLM_DUCKDB_RETENTION_DAYS", DEFAULT_DUCKDB_RETENTION_DAYS))
    budget_bytes = int(os.environ.get("MODELLM_DUCKDB_BUDGET_BYTES", DEFAULT_DUCKDB_BUDGET_BYTES))
    if retention_days < 1 or budget_bytes < 1:
        raise ValueError("DuckDB retention days and budget bytes must be positive")

    retention_seconds = retention_days * 24 * 60 * 60
    retained = []
    removed_count = 0
    removed_bytes = 0

    def remove_database(database: Path, wal: Path, size: int) -> None:
        nonlocal removed_count, removed_bytes
        for path in (database, wal):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        removed_count += 1
        removed_bytes += size

    for run_dir in root.iterdir():
        if not run_dir.is_dir():
            continue
        database = run_dir / "analytics.duckdb"
        wal = run_dir / "analytics.duckdb.wal"
        existing = [path for path in (database, wal) if path.is_file()]
        if not existing:
            continue
        size = sum(path.stat().st_size for path in existing)
        db_time = max(path.stat().st_mtime for path in existing)
        result = run_dir / "result.json"
        result_time = result.stat().st_mtime if result.exists() else db_time
        try:
            status = json.loads(result.read_text()).get("status") if result.exists() else None
        except (OSError, json.JSONDecodeError):
            status = None

        last_activity = max(db_time, result_time)
        stale_running = status == "running" and now - last_activity >= STALE_RUNNING_AFTER_SECONDS
        if stale_running or now - result_time >= retention_seconds:
            remove_database(database, wal, size)
            continue

        active = status == "running"
        retained.append((result_time, database, wal, size, active))

    total_bytes = sum(item[3] for item in retained)
    for _, database, wal, size, active in sorted(retained, key=lambda item: item[0]):
        if total_bytes <= budget_bytes:
            break
        if active:
            continue
        remove_database(database, wal, size)
        total_bytes -= size

    return {"removed_databases": removed_count, "bytes_freed": removed_bytes, "retained_bytes": total_bytes}
