"""One immutable settings snapshot per run; preview secrets never belong here."""

import os
import math
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DEFAULT_MODEL = "au.anthropic.claude-sonnet-4-6"
DEFAULT_PLOTTING_MODEL = "au.anthropic.claude-haiku-4-5-20251001-v1:0"


def boolean(name: str, default: bool) -> bool:
    value = os.environ.get(name, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{name} must be true or false")
    return value == "true"


@dataclass(frozen=True)
class ReleaseSettings:
    prod: bool = False
    live_enabled: bool = True
    runs_root: str = "runs"
    region: str = "ap-southeast-2"
    model_id: str = DEFAULT_MODEL
    plotting_model_id: str = DEFAULT_PLOTTING_MODEL
    max_file_bytes: int | None = None
    max_upload_bytes: int | None = None
    max_prompt_chars: int | None = None
    max_source_rows: int | None = None
    max_source_columns: int | None = None
    max_profile_chars: int | None = None
    daily_runs: int | None = None
    cooldown_seconds: int | None = None
    run_seconds: int | None = None
    max_output_tokens: int | None = None
    max_model_calls: int | None = None
    max_cost_usd: float | None = None
    max_analyses: int | None = None
    max_charts: int | None = None
    dbt_seconds: int = 180
    read_seconds: int = 3600
    threads: int = 1
    memory_mb: int = 1024
    spill_mb: int = 512
    workspace_bytes: int = 2 * 1024**3
    retention_seconds: int = 86400

    @classmethod
    def from_env(cls):
        prod = boolean("MODELLM_PROD", False)
        limits = dict(max_file_bytes=20*1024**2, max_upload_bytes=60*1024**2,
                      max_prompt_chars=12000, max_source_rows=250000, max_source_columns=100,
                      max_profile_chars=200000, daily_runs=10, cooldown_seconds=60,
                      run_seconds=1800, max_output_tokens=16384, max_model_calls=60,
                      max_cost_usd=3.0, max_analyses=8, max_charts=12)
        values = {}
        for name, default in limits.items():
            value = os.environ.get(f"MODELLM_{name.upper()}")
            values[name] = type(default)(value) if value is not None else default if prod else None
        for name, default in dict(dbt_seconds=180, read_seconds=180 if prod else 3600,
                                  threads=1, memory_mb=1024, spill_mb=512,
                                  workspace_bytes=2*1024**3, retention_seconds=86400).items():
            values[name] = int(os.environ.get(f"MODELLM_{name.upper()}", default))
        settings = cls(prod=prod, live_enabled=boolean("MODELLM_LIVE_ENABLED", not prod),
                       runs_root=str(Path(os.environ.get("MODELLM_RUNS_ROOT", "/data/runs" if prod else "runs")).resolve()),
                       region=os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "ap-southeast-2"),
                       model_id=os.environ.get("MODELLM_BEDROCK_MODEL_ID") or DEFAULT_MODEL,
                       plotting_model_id=os.environ.get("MODELLM_PLOTTING_MODEL_ID") or DEFAULT_PLOTTING_MODEL,
                       **values)
        settings.validate()
        return settings

    def validate(self):
        for field in fields(self):
            value = getattr(self, field.name)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and (not math.isfinite(value) or value <= 0):
                raise ValueError(f"{field.name} must be positive")
        if self.max_cost_usd is not None:
            from .usage import PRICES, REGIONAL_PRICES
            for model in (self.model_id, self.plotting_model_id):
                if model not in PRICES and (model, self.region) not in REGIONAL_PRICES:
                    raise ValueError(f"No cost estimate available for configured model: {model}")

    def snapshot(self) -> dict:
        return asdict(self)


def check_limit(value: int | float, limit: int | float | None, label: str):
    if limit is not None and value > limit:
        raise ValueError(f"{label} exceeds the configured limit ({limit})")


_settings = ContextVar("release_settings", default=None)


def current_settings() -> ReleaseSettings:
    return _settings.get() or ReleaseSettings.from_env()


@contextmanager
def use_settings(settings: ReleaseSettings):
    token = _settings.set(settings)
    try:
        yield settings
    finally:
        _settings.reset(token)
