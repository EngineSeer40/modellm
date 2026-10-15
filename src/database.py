"""Shared DuckDB policy; only trusted ingestion may read application-owned files."""

from pathlib import Path

import duckdb

from .config import current_settings


def connection_options(database: Path, *, ingest=False) -> dict:
    settings = current_settings()
    spill = database.resolve().parent / "spill"
    spill.mkdir(exist_ok=True, mode=0o700)
    return {
        "enable_external_access": ingest,
        "autoload_known_extensions": False,
        "autoinstall_known_extensions": False,
        "allow_unsigned_extensions": False,
        "lock_configuration": True,
        "threads": settings.threads,
        "memory_limit": f"{settings.memory_mb}MB",
        "temp_directory": str(spill),
        "max_temp_directory_size": f"{settings.spill_mb}MB",
    }


def connect(database: Path, *, read_only=False, ingest=False):
    return duckdb.connect(str(database), read_only=read_only,
                          config=connection_options(database, ingest=ingest))
