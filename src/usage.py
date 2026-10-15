"""Summarize recorded provider usage without counting call summaries twice."""

from collections import Counter


# Standard Bedrock USD/1M tokens; existing 4.x rates effective 2026-05-27.
# https://www-cdn.anthropic.com/files/4zrzovbb/website/3684c2faafb97418665782cea0001f439f74b1d2.pdf
PRICING_BASIS = "Bedrock standard list prices; 5-minute Anthropic cache writes 1.25x input; cache reads 0.1x for Sonnet 4.6/5 and Haiku 4.5, 0.05x for Sonnet 5.5; Sonnet 5/5.5 verified 2026-10-15, other models verified 2026-10-14"
PRICES = {
    "au.anthropic.claude-sonnet-4-6": (3.30, 16.50),
    "global.anthropic.claude-sonnet-4-6": (3.00, 15.00),
    "au.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
    "us.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
    # https://platform.claude.com/docs/en/about-claude/pricing
    # AU/US routing adds the published 10% geographic premium to global rates.
    "au.anthropic.claude-sonnet-5": (2.20, 11.00),
    "us.anthropic.claude-sonnet-5": (2.20, 11.00),
    "global.anthropic.claude-sonnet-5": (2.00, 10.00),
    "au.anthropic.claude-sonnet-5-5": (2.20, 11.00),
    "us.anthropic.claude-sonnet-5-5": (2.20, 11.00),
    "global.anthropic.claude-sonnet-5-5": (2.00, 10.00),
}
CACHE_READ_MULTIPLIERS = {
    "au.anthropic.claude-sonnet-5-5": 0.05,
    "us.anthropic.claude-sonnet-5-5": 0.05,
    "global.anthropic.claude-sonnet-5-5": 0.05,
}
# Mistral's bare model ID uses regional pricing. Other regions remain unpriced.
# https://aws.amazon.com/bedrock/pricing/
REGIONAL_PRICES = {("mistral.mistral-large-3-675b-instruct", "ap-southeast-2"): (0.515, 1.545)}
AGENTS = {
    "conceptual modelling": "Conceptual",
    "generating dbt models": "dbt",
    "analytical interpretation": "Analysis",
    "analytical finding": "Finding",
    "visualisation generation": "Plotting",
}


def estimate_usage_cost(model: str, region: str, usage: dict, *, cache_ttl_supported=True) -> float | None:
    """Shared by reporting and live budgets; unknown rates/usage are never free."""
    rates = PRICES.get(model) or REGIONAL_PRICES.get((model, region))
    if not rates or not usage or not {"inputTokens", "outputTokens"} <= usage.keys():
        return None
    reads, writes = usage.get("cacheReadInputTokens", 0), usage.get("cacheWriteInputTokens", 0)
    if (reads or writes) and (model not in PRICES or not cache_ttl_supported or any(
        item.get("inputTokens", 0) and item.get("ttl") != "5m" for item in usage.get("cacheDetails", [])
    )):
        return None
    return ((usage["inputTokens"] + reads * CACHE_READ_MULTIPLIERS.get(model, 0.1) + writes * 1.25)
            * rates[0] + usage["outputTokens"] * rates[1]) / 1_000_000


def summarize_usage(events: list[dict]) -> dict:
    calls, active, seen = {}, {}, set()
    for index, event in enumerate(events):
        stage = event.get("stage", "")
        detail = event.get("detail", {})
        message = event.get("message", "")
        started = event.get("kind") == "model_call" and (
            detail.get("status") == "started" or message.startswith("Calling ")
        )
        if started:
            active[stage] = detail.get("call_id", f"legacy_{index}")
        call_id = detail.get("call_id") or active.get(stage)
        if not call_id:
            continue
        call = calls.setdefault(call_id, {
            "agent": AGENTS.get(stage, stage), "model": "", "region": "", "completed": False,
            "messages": Counter(), "summary": {}, "responses": 0, "cache_ttl_supported": True,
        })
        if started:
            call["region"] = detail.get("region", "")
            call["model"] = detail.get("model_id") or (
                message.removeprefix("Calling ") if message.startswith("Calling ") else ""
            )
            call["cache_ttl_supported"] = detail.get("prompt_cache", {}).get("ttl") in (None, "5m")
        if event.get("kind") == "model_call" and detail.get("status") == "completed":
            call["completed"] = True
        if isinstance(detail.get("accumulated_usage"), dict):
            call["summary"] = detail["accumulated_usage"]
        response = detail.get("message", {})
        usage = response.get("metadata", {}).get("usage", {}) if isinstance(response, dict) else {}
        if usage:
            response_id = (call_id, response.get("tracking_id") or index)
            if response_id in seen:
                continue
            seen.add(response_id)
            call["responses"] += 1
            for key in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens"):
                call["messages"][key] += usage.get(key, 0)
            if any(item.get("inputTokens", 0) and item.get("ttl") != "5m"
                   for item in usage.get("cacheDetails", [])):
                call["cache_ttl_supported"] = False

    agents = {}
    for call in calls.values():
        agent = agents.setdefault(call["agent"], {
            "agent": call["agent"], "models": set(), "model_calls": 0,
            "reported_calls": 0, "input_tokens": 0, "output_tokens": 0,
            "uncached_input_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0,
            "total_tokens": 0, "estimated_cost_usd": 0.0, "partial": False,
        })
        agent["model_calls"] += 1
        if call["model"]:
            agent["models"].add(call["model"])
        # Provider response records survive failures; summaries are a fallback only.
        usage = call["messages"] if call["responses"] else call["summary"]
        if not usage:
            agent["partial"] = True
            continue
        agent["reported_calls"] += 1
        agent["partial"] |= not call["completed"]
        inputs, outputs = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
        reads, writes = usage.get("cacheReadInputTokens", 0), usage.get("cacheWriteInputTokens", 0)
        # Bedrock inputTokens excludes both cache reads and writes.
        agent["input_tokens"] += inputs + reads + writes
        agent["uncached_input_tokens"] += inputs
        agent["cache_read_tokens"] += reads
        agent["cache_write_tokens"] += writes
        agent["output_tokens"] += outputs
        agent["total_tokens"] += inputs + reads + writes + outputs
        cost = estimate_usage_cost(call["model"], call["region"], usage,
                                   cache_ttl_supported=call["cache_ttl_supported"])
        if cost is None:
            agent["estimated_cost_usd"] = None
        elif agent["estimated_cost_usd"] is not None:
            agent["estimated_cost_usd"] += cost

    rows = []
    for agent in agents.values():
        agent["models"] = ";".join(sorted(agent["models"]))
        agent["usage_status"] = (
            "unavailable" if not agent["reported_calls"] else "partial" if agent["partial"] else "reported"
        )
        if not agent["reported_calls"]:
            for key in ("input_tokens", "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
                        "output_tokens", "total_tokens", "estimated_cost_usd"):
                agent[key] = None
        elif agent["estimated_cost_usd"] is not None:
            agent["estimated_cost_usd"] = round(agent["estimated_cost_usd"], 6)
        rows.append(agent)
    totals = {
        "model_calls": sum(row["model_calls"] for row in rows),
        "input_tokens": sum(row["input_tokens"] or 0 for row in rows),
        "uncached_input_tokens": sum(row["uncached_input_tokens"] or 0 for row in rows),
        "cache_read_tokens": sum(row["cache_read_tokens"] or 0 for row in rows),
        "cache_write_tokens": sum(row["cache_write_tokens"] or 0 for row in rows),
        "output_tokens": sum(row["output_tokens"] or 0 for row in rows),
        "total_tokens": sum(row["total_tokens"] or 0 for row in rows),
        "estimated_cost_usd": None,
        "usage_status": "unavailable" if not any(row["reported_calls"] for row in rows)
                        else "partial" if any(row["partial"] for row in rows) else "reported",
    }
    if totals["usage_status"] == "unavailable":
        for key in ("input_tokens", "uncached_input_tokens", "cache_read_tokens", "cache_write_tokens",
                    "output_tokens", "total_tokens"):
            totals[key] = None
    elif rows and all(row["estimated_cost_usd"] is not None for row in rows):
        totals["estimated_cost_usd"] = round(sum(row["estimated_cost_usd"] for row in rows), 6)
    return {"agents": rows, "total": totals, "pricing_basis": PRICING_BASIS}
