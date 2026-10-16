"""Own paths and atomic records for one run; workflow state stays in workflow.py."""

import json
import re
import shutil
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ReleaseSettings, check_limit
from .data import identifier


def atomic_json(path: Path, value: Mapping[str, Any]):
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, default=str)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class RunWorkspace:
    root: Path
    run_id: str

    @property
    def run_dir(self):
        return self.root / self.run_id

    @property
    def input_dir(self):
        return self.run_dir / "input"

    @property
    def database(self):
        return self.run_dir / "modellm.duckdb"

    @property
    def dbt_dir(self):
        return self.run_dir / "dbt"

    @property
    def result_path(self):
        return self.run_dir / "result.json"

    @classmethod
    def create(cls, root: Path, owner: str = "standalone"):
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        run_id = f"{datetime.now(timezone.utc):%y%m%d_%H%M}_{uuid.uuid4().hex[:12]}"
        workspace = cls(root, run_id)
        workspace.run_dir.mkdir(mode=0o700)
        workspace.input_dir.mkdir(mode=0o700)
        atomic_json(workspace.run_dir / "workspace.json", {
            "format": "modellm-workspace-v1", "run_id": run_id, "owner": owner,
            "created_at": datetime.now(timezone.utc).timestamp(),
        })
        return workspace

    @classmethod
    def open(cls, directory: Path):
        if directory.is_symlink():
            raise ValueError("Workspace must not be a symlink")
        directory = directory.resolve()
        if (directory / "workspace.json").is_symlink() or (directory / "input").is_symlink():
            raise ValueError("Workspace marker and inputs must not be symlinks")
        marker = json.loads((directory / "workspace.json").read_text())
        if marker.get("format") != "modellm-workspace-v1" or marker.get("run_id") != directory.name:
            raise ValueError("Invalid workspace marker")
        return cls(directory.parent, directory.name)

    def persist(self, uploads: list[tuple[str, bytes]], settings: ReleaseSettings) -> list[Path]:
        validate_submission(uploads, "validated", settings, check_prompt=False)
        paths = []
        for name, content in uploads:
            path = self.input_dir / name
            with path.open("xb") as stream:
                stream.write(content)
            paths.append(path)
        return paths

    def remove_inputs(self):
        # Never follow a caller's source path or an input-directory symlink.
        if self.input_dir.is_symlink():
            self.input_dir.unlink()
        elif self.input_dir.exists():
            shutil.rmtree(self.input_dir)


def validate_submission(uploads, request, settings, *, check_prompt=True):
    if check_prompt:
        if not request.strip():
            raise ValueError("Describe the business question you want to answer")
        check_limit(len(request), settings.max_prompt_chars, "Business question length")
    if not 2 <= len(uploads) <= 6:
        raise ValueError("Upload between 2 and 6 related datasets")
    names = set()
    for name, content in uploads:
        if not name or name in {".", ".."} or "/" in name or "\\" in name or "\0" in name:
            raise ValueError("Invalid upload filename")
        path = Path(name)
        if path.suffix.lower() not in {".csv", ".parquet", ".json", ".jsonl", ".ndjson"}:
            raise ValueError(f"Unsupported upload type: {path.suffix}")
        canonical = identifier(re.sub(r"[^a-z0-9]+", "_", path.stem.lower()).strip("_"))
        if canonical in names:
            raise ValueError("Dataset names must be distinct after normalization")
        names.add(canonical)
        check_limit(len(content), settings.max_file_bytes, "Individual upload size")
    check_limit(sum(len(content) for _, content in uploads), settings.max_upload_bytes, "Total upload size")
