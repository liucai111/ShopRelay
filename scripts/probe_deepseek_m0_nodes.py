"""Small, fixed DeepSeek node-configuration comparison for M0.

Runs one diagnostic request per node and thinking mode (10 requests total),
records sanitized inputs/final outputs/usage, and never records reasoning text
or API credentials. Success is a per-case schema/contract check, not a quality
claim for a future frozen evaluation set.
"""
from __future__ import annotations

import json
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.m0.config import create_deepseek_client, load_settings

ROOT = Path(__file__).resolve().parents[1]
MODEL = None
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"

NODES = [
    {
        "node": "simple_intent_routing",
        "kind": "json",
        "system": ('Classify the task before selecting a route. Use AGENT only when the user asks to retrieve or act on a live, specific business record. '
                   'Use SINGLE for a policy question, including what policy can or cannot prove. Use REFUSE for another person\'s private order. '
                   'Return JSON only: {"route":"AGENT|SINGLE|MULTI|DIRECT|REFUSE","intent":"order_tracking|policy_question|policy_evidence_boundary|unauthorized_access"}.'),
        "user": "Probe prompt supplied in ASCII-safe table below.",
        "check": lambda x: x.get("route") == "AGENT" and x.get("intent") == "order_tracking",
    },
    {
        "node": "complex_planning",
        "kind": "json",
        "system": 'Return JSON only: {"route":"AGENT","steps":["tool_or_task"],"parallel_reads":boolean}.',
        "user": "Probe prompt supplied in ASCII-safe table below.",
        "check": lambda x: x.get("route") == "AGENT" and isinstance(x.get("steps"), list) and len(x["steps"]) >= 2,
    },
    {
        "node": "evidence_sufficiency",
        "kind": "json",
        "system": 'Return JSON only: {"sufficient":boolean,"missing_aspect":"string","decision":"answer|clarify|refuse"}.',
        "user": "Probe prompt supplied in ASCII-safe table below.",
        "check": lambda x: x.get("sufficient") is False and x.get("decision") in ("clarify", "refuse") and bool(x.get("missing_aspect")),
    },
    {
        "node": "structured_result_generation",
        "kind": "json",
        "system": 'Return JSON only: {"answer":"string","citations":["id"],"unsupported_claims":[]}. Use only the supplied evidence.',
        "user": "Probe prompt supplied in ASCII-safe table below.",
        "check": lambda x: bool(x.get("answer")) and "refund@2026-01#before-shipping" in x.get("citations", []) and not x.get("unsupported_claims"),
    },
    {
        "node": "tool_call_parameter_generation",
        "kind": "tool",
        "system": "For order tracking requests, call track_shipment with the exact order_id supplied. Do not answer without the tool.",
        "user": "Probe prompt supplied in ASCII-safe table below.",
        "check": None,
    },
]
TOOLS = [{"type": "function", "function": {"name": "track_shipment", "description": "Look up shipment events by order ID", "parameters": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"], "additionalProperties": False}}}]

# Keep prompts ASCII in this probe so Windows console/source encodings cannot
# alter the request payload. The contracts themselves are the same M0 nodes.
for _node, _prompt in zip(NODES, [
    "Classify: customer asks where order ORD-0007 is because shipment tracking has not changed for three days.",
    "Plan verification for order ORD-0007 delivered status and a reported product quality defect; customer asks whether return and refund are possible. Include required business facts and policy checks; invent no results.",
    "Question: has order ORD-0007 been delivered? Evidence only says live shipment status must come from carrier/order events. No order-tool result exists. Decide whether evidence is sufficient.",
    "Question: after a return is approved, may the customer request a refund? Evidence refund@2026-01#before-shipping says delivered orders require an approved return before a refund request.",
    "Look up the current shipment status for order ORD-0007.",
]):
    _node["user"] = _prompt


def price_band(at: datetime) -> tuple[str, float, float, float]:
    peak = at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)
    return ("peak", 0.006, 0.30, 1.20) if peak else ("off_peak", 0.003, 0.15, 0.60)


def usage_cost(raw: dict | None, at: datetime) -> dict | None:
    if not raw:
        return None
    prompt = int(raw.get("prompt_tokens") or 0)
    completion = int(raw.get("completion_tokens") or 0)
    details = raw.get("prompt_tokens_details") or {}
    cached = int(raw.get("prompt_cache_hit_tokens") or details.get("cached_tokens") or 0)
    miss = int(raw.get("prompt_cache_miss_tokens") or max(0, prompt - cached))
    # Clamp/repair only inconsistent detail fields; preserve the unmodified usage nearby.
    if cached + miss != prompt:
        cached = min(cached, prompt)
        miss = prompt - cached
    band, cache_rate, miss_rate, out_rate = price_band(at)
    cost = (cached * cache_rate + miss * miss_rate + completion * out_rate) / 1_000_000
    return {"band": band, "cached_input_tokens": cached, "uncached_input_tokens": miss, "output_tokens": completion, "unit_prices_usd_per_million": {"cache_hit": cache_rate, "cache_miss": miss_rate, "output": out_rate}, "calculated_api_cost_usd": round(cost, 9), "price_source": PRICE_SOURCE}


def run() -> dict:
    global MODEL
    settings = load_settings()
    MODEL = settings.model
    node_configs = {
        "simple_intent_routing": settings.nodes["route_diagnostic"],
        "complex_planning": settings.nodes["planning_diagnostic"],
        "evidence_sufficiency": settings.nodes["evidence_sufficiency_diagnostic"],
        "structured_result_generation": settings.nodes["policy_answer"],
        "tool_call_parameter_generation": settings.nodes["route_diagnostic"],
    }
    client = create_deepseek_client(settings, settings.nodes["route_diagnostic"])
    results = []
    for node in NODES:
        for mode in ("disabled", "enabled"):
            started_at = datetime.now(timezone.utc)
            started = time.perf_counter()
            node_config = node_configs[node["node"]]
            output_budget = node_config.max_tokens
            request: dict = {"model": MODEL, "messages": [{"role": "system", "content": node["system"]}, {"role": "user", "content": node["user"]}], "max_tokens": output_budget, "extra_body": {"thinking": {"type": mode}}}
            if node["kind"] == "json":
                request["response_format"] = {"type": "json_object"}
            else:
                request["tools"] = TOOLS
                request["tool_choice"] = "auto"
            item: dict = {"node": node["node"], "mode": mode, "started_at_utc": started_at.isoformat(), "request_sanitized": {"model": MODEL, "messages": request["messages"], "response_format": request.get("response_format"), "tools": request.get("tools"), "tool_choice": request.get("tool_choice"), "max_tokens": output_budget}}
            try:
                response = client.chat.completions.create(**request)
                msg = response.choices[0].message
                raw_usage = response.usage.model_dump() if response.usage else None
                item.update({"status": "PASS", "response_model": response.model, "finish_reason": response.choices[0].finish_reason, "latency_ms": round((time.perf_counter() - started) * 1000), "usage": raw_usage, "cost": usage_cost(raw_usage, started_at)})
                if node["kind"] == "json":
                    text = msg.content or ""
                    try:
                        parsed = json.loads(text)
                        valid = isinstance(parsed, dict)
                        meets = valid and bool(node["check"](parsed))
                        item["response_content"] = text
                        item["parsed_output"] = parsed
                    except (json.JSONDecodeError, TypeError):
                        valid, meets = False, False
                        item["response_content"] = text
                    item["json_valid"] = valid
                    item["contract_check"] = "PASS" if meets else "FAIL"
                    if not meets:
                        item["status"] = "FAIL"
                else:
                    calls = msg.tool_calls or []
                    call_records = []
                    for call in calls:
                        try:
                            arguments = json.loads(call.function.arguments)
                            valid_args = isinstance(arguments, dict)
                        except json.JSONDecodeError:
                            arguments, valid_args = call.function.arguments, False
                        call_records.append({"name": call.function.name, "arguments": arguments, "arguments_valid_json": valid_args})
                    item["tool_calls"] = call_records
                    valid = bool(call_records) and all(c["arguments_valid_json"] for c in call_records)
                    meets = valid and call_records[0]["name"] == "track_shipment" and call_records[0]["arguments"].get("order_id") == "ORD-0007"
                    item["contract_check"] = "PASS" if meets else "FAIL"
                    if not meets:
                        item["status"] = "FAIL"
            except Exception as exc:
                item.update({"status": "FAIL", "error_type": type(exc).__name__, "latency_ms": round((time.perf_counter() - started) * 1000), "usage": None, "cost": None})
            results.append(item)
    by_mode = {}
    for mode in ("disabled", "enabled"):
        selected = [x for x in results if x["mode"] == mode]
        lat = sorted(x["latency_ms"] for x in selected if x.get("latency_ms") is not None)
        by_mode[mode] = {"n": len(selected), "pass": sum(x["status"] == "PASS" for x in selected), "contract_pass": sum(x.get("contract_check") == "PASS" for x in selected), "latency_p50_ms": statistics.median(lat) if lat else None, "latency_p95_ms_nearest_rank": lat[max(0, __import__("math").ceil(.95 * len(lat)) - 1)] if lat else None, "total_calculated_api_cost_usd": round(sum((x.get("cost") or {}).get("calculated_api_cost_usd", 0) for x in selected), 9)}
    result = {"record_type": "m0_deepseek_node_mode_comparison", "timestamp_utc": datetime.now(timezone.utc).isoformat(), "status": "PASS_DIAGNOSTIC_ONLY" if all(x["status"] == "PASS" for x in results) else "FAIL_OR_PARTIAL", "diagnostic_only": True, "dataset_used": "synthetic fixed node contract prompts; no r7 labels used", "request_count": len(results), "api_retries": 0, "model_requested": MODEL, "price_source": PRICE_SOURCE, "price_accessed_at_utc": datetime.now(timezone.utc).isoformat(), "pricing_notes": "USD calculated from returned usage; time band determined from request-start UTC under official weekday peak schedule; excludes provider billing adjustments/taxes.", "aggregate_by_mode": by_mode, "runs": results}
    output = ROOT / "docs/probes" / f"deepseek-node-modes-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with output.open("x", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(json.dumps({"record": str(output.relative_to(ROOT)), "summary": {k: v for k, v in result.items() if k != "runs"}}, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    run()
