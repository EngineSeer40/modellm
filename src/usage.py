"""Summarize recorded provider usage without counting call summaries twice."""

from collections import Counter


# Standard Bedrock USD/1M tokens; Anthropic list prices effective 2026-05-27.
# https://www-cdn.anthropic.com/files/4zrzovbb/website/3684c2faafb97418665782cea0001f439f74b1d2.pdf
PRICING_BASIS = "Bedrock standard list prices; Anthropic includes 5-minute cache writes (1.25x input) and reads (0.1x input), effective 2026-05-27; Mistral Sydney; verified 2026-10-14"
PRICES = {
    "au.anthropic.claude-sonnet-4-6": (3.30, 16.50),
    "global.anthropic.claude-sonnet-4-6": (3.00, 15.00),
    "au.anthropic.claude-haiku-4-5-20251001-v1:0": (1.10, 5.50),
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
        rates = PRICES.get(call["model"]) or REGIONAL_PRICES.get((call["model"], call["region"]))
        cache_priced = call["model"] in PRICES and call["cache_ttl_supported"]
        if not rates or ((reads or writes) and not cache_priced):
            agent["estimated_cost_usd"] = None
        elif agent["estimated_cost_usd"] is not None:
            agent["estimated_cost_usd"] += (
                (inputs + reads * 0.1 + writes * 1.25) * rates[0] + outputs * rates[1]
            ) / 1_000_000

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
