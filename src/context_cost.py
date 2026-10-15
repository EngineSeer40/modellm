"""Reduce model context and fresh repair calls; full artifacts and acceptance remain authoritative."""

import json
import os
from pathlib import Path


COST_EXPERIMENT_STAGES = {
    "conceptual_guidance": "conceptual modelling",
    "conceptual_cache": "conceptual modelling",
    "early_finding_validation": "analytical finding",
    "small_analysis_previews": "analytical interpretation",
    "offload_analysis": "analytical interpretation",
    "offload_finding": "analytical finding",
}


def configured_cost_experiments(experiments: frozenset[str] | None = None) -> frozenset[str]:
    if experiments is None:
        experiments = frozenset(name.strip() for name in os.environ.get(
            "MODELLM_COST_EXPERIMENTS", ""
        ).split(",") if name.strip())
    unknown = experiments - COST_EXPERIMENT_STAGES.keys()
    if unknown:
        raise ValueError(f"Unknown MODELLM_COST_EXPERIMENTS: {', '.join(sorted(unknown))}")
    return frozenset(experiments)


def split_stable_and_repair_context(stable: dict, repair: dict) -> list[dict]:
    """Keep changing repair feedback outside the evidence cache prefix; omit no context."""
    return [{"text": json.dumps(part, default=str)} for part in (stable, repair)]


def tool_result_offload_plugins(stage: str, experiments: frozenset[str], directory: Path | None) -> list:
    """Let Strands preview large tool outputs and retrieve originals, without editing messages."""
    tool_name = {
        "analytical interpretation": ("offload_analysis", "query_data"),
        "analytical finding": ("offload_finding", "inspect_result"),
    }.get(stage)
    if tool_name is None or tool_name[0] not in experiments:
        return []
    if directory is None:
        raise ValueError("Tool-result offloading needs a per-call artifact directory")

    from strands.storage import LocalFileStorage
    from strands.vended_plugins.context_offloader import ContextOffloader

    return [ContextOffloader(
        storage=LocalFileStorage(str(directory)),
        max_result_tokens=2500, preview_tokens=1000,
        include_retrieval_tool=True, evict_after_cycles=None,
        should_offload=lambda name, token_count, **kwargs: name == tool_name[1],
    )]
