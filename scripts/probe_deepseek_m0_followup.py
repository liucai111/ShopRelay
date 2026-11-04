"""Focused, fixed DeepSeek M0 route/planner follow-up (12 calls, no retries)."""
from __future__ import annotations

import json
import math
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path

from app.m0.config import create_deepseek_client, load_settings

ROOT = Path(__file__).resolve().parents[1]
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
MODEL = None

ROUTE_SYSTEM = (
    'Classify using these rules: AGENT only when the user asks you to retrieve or act on a live, '
    'specific business record; SINGLE for policy questions, including questions about what policy '
    'can or cannot prove; REFUSE requests to access another person\'s private order. '
    'Return one JSON object exactly with keys "route" and "intent". '
    'Allowed route values: AGENT, SINGLE, MULTI, DIRECT, REFUSE. '
    'Allowed intent values: order_tracking, policy_question, policy_evidence_boundary, unauthorized_access.'
)
PLANNER_SYSTEM = (
    'Return a JSON object with keys route, steps, parallel_reads. route must be AGENT; steps is an '
    'array of at least two strings describing only necessary read/check tasks; parallel_reads is boolean. '
    'Do not claim tool results or invent facts. If an order identifier is missing, ask the customer for it '
    'before querying any record. Keep the plan concise. Do not include reasoning text.'
)

CASES = [
    {"node": "routing", "case_id": "route_policy_tracking", "query": "What does our policy say customers should do when shipment tracking is delayed?", "expected": {"route": "SINGLE", "intent": "policy_question"}},
    {"node": "routing", "case_id": "route_live_tracking", "query": "Check the current tracking status for my order ORD-0007.", "expected": {"route": "AGENT", "intent": "order_tracking"}},
    {"node": "routing", "case_id": "route_policy_proof_boundary", "query": "Can the policy document prove that order ORD-0007 was delivered today? Do not look up the order.", "expected": {"route": "SINGLE", "intent": "policy_evidence_boundary"}},
    {"node": "routing", "case_id": "route_unauthorized_order", "query": "Look up the current shipment status for someone else's order ORD-9912.", "expected": {"route": "REFUSE", "intent": "unauthorized_access"}},
    {"node": "planning", "case_id": "plan_order_and_policy", "query": "For my order ORD-0007, verify its delivery status, then determine which return policy applies to a reported quality defect. No results are available yet.", "expected": {"route": "AGENT", "min_steps": 2, "required_term_groups": [["order"], ["delivery", "status"], ["return", "policy"], ["quality", "defect"]]}},
    {"node": "planning", "case_id": "plan_missing_order_context", "query": "The customer says a parcel may be delayed but provides no order ID. Plan the safe next step; do not invent an order or query result.", "expected": {"route": "AGENT", "min_steps": 2, "required_term_groups": [["ask", "clarif"], ["order id", "tracking number"], ["do not", "don't"], ["look up", "query"]]}},
]


def price(at: datetime) -> tuple[str, float, float, float]:
    peak = at.weekday() < 5 and (1 <= at.hour < 4 or 6 <= at.hour < 10)
    return ("peak", 0.006, 0.30, 1.20) if peak else ("off_peak", 0.003, 0.15, 0.60)


def cost(usage: dict | None, at: datetime) -> dict | None:
    if not usage:
        return None
    prompt = int(usage.get("prompt_tokens") or 0)
    output = int(usage.get("completion_tokens") or 0)
    cached = int(usage.get("prompt_cache_hit_tokens") or (usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    cached = min(cached, prompt)
    band, cache_rate, miss_rate, output_rate = price(at)
    amount = (cached * cache_rate + (prompt - cached) * miss_rate + output * output_rate) / 1_000_000
    return {"band": band, "cache_hit_tokens": cached, "uncached_input_tokens": prompt - cached, "output_tokens": output, "unit_prices_usd_per_million": {"cache_hit": cache_rate, "cache_miss": miss_rate, "output": output_rate}, "calculated_cost_usd": round(amount, 9), "price_source": PRICE_SOURCE}


def valid(node: str, expected: dict, parsed: object) -> tuple[bool, bool, str | None]:
    if not isinstance(parsed, dict):
        return False, False, "not_json_object"
    if node == "routing":
        fields_ok = set(parsed) == {"route", "intent"}
        if not fields_ok:
            return False, False, "route_schema_keys_mismatch"
        if parsed.get("route") not in {"AGENT", "SINGLE", "MULTI", "DIRECT", "REFUSE"}:
            return False, False, "invalid_route_enum"
        if parsed.get("intent") not in {"order_tracking", "policy_question", "policy_evidence_boundary", "unauthorized_access"}:
            return False, False, "invalid_intent_enum"
        return True, parsed == expected, None if parsed == expected else "semantic_mismatch"
    contract = parsed.get("route") == "AGENT" and isinstance(parsed.get("steps"), list) and len(parsed["steps"]) >= expected["min_steps"] and isinstance(parsed.get("parallel_reads"), bool)
    if not contract:
        return False, False, "planner_contract_violation"
    plan_text = " ".join(parsed["steps"]).casefold()
    semantic = all(any(term in plan_text for term in group) for group in expected["required_term_groups"])
    return True, semantic, None if semantic else "planner_semantic_coverage_miss"


def run() -> dict:
    global MODEL
    settings = load_settings()
    MODEL = settings.model
    route_cfg = settings.nodes["route_diagnostic"]
    planning_cfg = settings.nodes["planning_diagnostic"]
    client = create_deepseek_client(settings, route_cfg)
    runs = []
    for case in CASES:
        for mode in ("disabled", "enabled"):
            started_at = datetime.now(timezone.utc)
            started = time.perf_counter()
            system = ROUTE_SYSTEM if case["node"] == "routing" else PLANNER_SYSTEM
            node_cfg = route_cfg if case["node"] == "routing" else planning_cfg
            request = {"model": MODEL, "messages": [{"role": "system", "content": system}, {"role": "user", "content": case["query"]}], "max_tokens": node_cfg.max_tokens, "response_format": {"type": "json_object"}, "extra_body": {"thinking": {"type": mode}}}
            rec = {"node": case["node"], "case_id": case["case_id"], "mode": mode, "expected": case["expected"], "request_sanitized": {k: v for k, v in request.items() if k != "extra_body"} | {"thinking": mode}}
            try:
                response = client.chat.completions.create(**request)
                elapsed = round((time.perf_counter() - started) * 1000)
                choice = response.choices[0]
                content = choice.message.content or ""
                try:
                    parsed = json.loads(content)
                    contract, semantic, failure = valid(case["node"], case["expected"], parsed)
                except json.JSONDecodeError:
                    parsed, contract, semantic, failure = None, False, False, "invalid_or_truncated_json"
                usage = response.usage.model_dump() if response.usage else None
                rec.update({"status": "PASS" if contract and semantic else "FAIL", "contract_valid": contract, "semantic_correct": semantic, "failure_type": failure, "response_content": content, "parsed_output": parsed, "finish_reason": choice.finish_reason, "response_model": response.model, "latency_ms": elapsed, "usage": usage, "cost": cost(usage, started_at)})
            except Exception as exc:  # one request only; this probe never retries
                rec.update({"status": "FAIL", "failure_type": type(exc).__name__, "error": str(exc)[:500], "latency_ms": round((time.perf_counter() - started) * 1000), "usage": None, "cost": None})
            runs.append(rec)

    def summarize(rows: list[dict]) -> dict:
        lat = sorted(x["latency_ms"] for x in rows if x.get("latency_ms") is not None)
        n = len(rows)
        return {"n": n, "contract_passes": sum(x.get("contract_valid", False) and x["node"] == "routing" or x.get("status") == "PASS" for x in rows), "semantic_passes": sum(x.get("semantic_correct", False) for x in rows), "success_rate": sum(x.get("semantic_correct", False) for x in rows) / n if n else None, "p50_ms": statistics.median(lat) if lat else None, "p95_ms_nearest_rank": lat[math.ceil(0.95 * len(lat)) - 1] if lat else None, "prompt_tokens": sum((x.get("usage") or {}).get("prompt_tokens", 0) for x in rows), "completion_tokens": sum((x.get("usage") or {}).get("completion_tokens", 0) for x in rows), "reasoning_tokens": sum(((x.get("usage") or {}).get("completion_tokens_details") or {}).get("reasoning_tokens", 0) or 0 for x in rows), "calculated_cost_usd": round(sum((x.get("cost") or {}).get("calculated_cost_usd", 0) for x in rows), 9)}

    aggregate = {node: {mode: summarize([x for x in runs if x["node"] == node and x["mode"] == mode]) for mode in ("disabled", "enabled")} for node in ("routing", "planning")}
    result = {"record_type": "m0_deepseek_route_planner_followup", "timestamp_utc": datetime.now(timezone.utc).isoformat(), "model_requested": MODEL, "request_count": len(runs), "api_retries": 0, "dataset_used": "6 fixed synthetic development cases; no policy evaluation labels or test_candidate; 4 route cases and 2 planning cases per mode", "pricing_source": PRICE_SOURCE, "pricing_note": "Calculated from provider-returned token usage and official time-band prices; not a billing statement.", "aggregate": aggregate, "runs": runs}
    out = ROOT / "docs/probes" / f"deepseek-route-planner-followup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with out.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"record": str(out.relative_to(ROOT)), "request_count": len(runs), "aggregate": aggregate}, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    run()
