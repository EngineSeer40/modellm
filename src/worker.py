"""Local workflow child process; manifests are application-owned, never browser input."""

import argparse
import json
from pathlib import Path

from .config import ReleaseSettings
from .workflow import run
from .workspace import RunWorkspace
from .telemetry import flush_telemetry


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", required=True, type=Path)
    args = parser.parse_args()
    workspace = RunWorkspace.open(args.run_dir)
    request = json.loads((workspace.run_dir / "request.json").read_text())
    settings = ReleaseSettings(**request["settings"])
    settings.validate()
    paths = [Path(path) for path in request["paths"]]
    if any(path.is_symlink() or path.resolve().parent != workspace.input_dir for path in paths):
        raise ValueError("Worker inputs must belong to its workspace")
    # Future cloud exporter/provider setup belongs here, once per worker.
    try:
        run(paths, request["request"], workspace.root, workspace=workspace, settings=settings)
    finally:
        flush_telemetry()


if __name__ == "__main__":
    main()
