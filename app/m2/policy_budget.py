"""Run-level bounds for bounded SINGLE/MULTI policy execution."""
from __future__ import annotations

import os
import time


def _cost(usage: dict | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    try:
        prompt = int(usage["prompt_tokens"])
        completion = int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    details = usage.get("prompt_tokens_details") or {}
    cached = min(prompt, int(details.get("cached_tokens") or details.get("cache_hit_tokens") or
                             usage.get("prompt_cache_hit_tokens") or usage.get("cache_hit_tokens") or 0))
    return round(((prompt - cached) * .30 + cached * .006 + completion * 1.20) / 1_000_000, 9)


class PolicyExecutionBudget:
    """Finite run budget shared by the router, aspect planner, retrieval and answer node."""

    def __init__(self, *, route_count: int = 0, route_usage: dict | None = None,
                 route_latency_ms: int = 0):
        self.max_requests = int(os.environ.get("SHOPRELAY_MAX_MODEL_REQUESTS", "12"))
        self.max_tokens = int(os.environ.get("SHOPRELAY_MAX_MODEL_TOKENS_PER_CHAT", "12288"))
        self.max_cost = float(os.environ.get("SHOPRELAY_MAX_ESTIMATED_COST_USD_PER_CHAT", ".10"))
        self.max_queries = int(os.environ.get("SHOPRELAY_MAX_RETRIEVAL_QUERIES", "6"))
        self.max_wall = float(os.environ.get("SHOPRELAY_MAX_CHAT_WALL_SECONDS", "105"))
        if min(self.max_requests, self.max_tokens, self.max_queries) < 1 or min(self.max_cost, self.max_wall) <= 0:
            raise ValueError("policy execution budgets must be positive")
        self.started = time.monotonic() - max(0, route_latency_ms) / 1000
        self.requests = max(0, route_count)
        self.queries = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cost = 0.0
        self.latency_ms = max(0, route_latency_ms)
        self.usage_unknown = False
        self.calls: list[dict] = []
        if route_count:
            route_cost = _cost(route_usage)
            if route_cost is None:
                self.usage_unknown = True
            else:
                self.input_tokens += int(route_usage["prompt_tokens"])
                self.output_tokens += int(route_usage["completion_tokens"])
                self.cost += route_cost
                self.calls.append({"node": "route", "status": "COMPLETED", "prompt_tokens": self.input_tokens,
                    "completion_tokens": self.output_tokens, "latency_ms": max(0, route_latency_ms),
                    "estimated_cost_usd": route_cost, "cost_is_estimated": True})

    def reserve_model(self, node, input_text: str) -> tuple[bool, str | None, float]:
        estimated_input = max(1, len(input_text.encode("utf-8")) // 2)
        estimate = ((estimated_input * .30) + (int(node.max_tokens) * 1.20)) / 1_000_000
        if self.usage_unknown:
            return False, "previous_usage_unknown", estimate
        if self.requests >= self.max_requests:
            return False, "model_request_limit", estimate
        if self.input_tokens + self.output_tokens + estimated_input + int(node.max_tokens) > self.max_tokens:
            return False, "token_limit", estimate
        if estimate > float(node.max_cost_usd):
            return False, "node_cost_limit", estimate
        if self.cost + estimate > self.max_cost:
            return False, "chat_cost_limit", estimate
        if time.monotonic() - self.started + float(node.timeout_seconds) > self.max_wall:
            return False, "wall_clock_limit", estimate
        self.requests += 1
        return True, None, estimate

    def record_model(self, node_name: str, response: dict, fallback_cost: float) -> None:
        usage = response.get("usage") if isinstance(response, dict) else None
        cost = _cost(usage)
        if cost is None:
            self.usage_unknown = True
            cost = fallback_cost
            prompt = completion = None
        else:
            prompt, completion = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
            self.input_tokens += prompt
            self.output_tokens += completion
            cost = response.get("estimated_cost_usd", usage.get("cost_estimate_usd_peak_rate", cost))
            cost = float(cost)
        latency = int(response.get("latency_ms") or 0)
        self.latency_ms += latency
        self.cost += cost
        self.calls.append({"node": node_name, "status": "COMPLETED" if usage else "USAGE_UNKNOWN",
            "model_id": response.get("model_id"), "prompt_tokens": prompt, "completion_tokens": completion,
            "latency_ms": latency, "estimated_cost_usd": cost, "finish_reason": response.get("finish_reason"),
            "prompt_version": response.get("prompt_version"), "cost_is_estimated": True})

    def record_failure(self, node_name: str, estimate: float, code: str, diagnostic: dict | None) -> None:
        diagnostic = diagnostic or {}
        usage = diagnostic.get("usage")
        cost = _cost(usage)
        if cost is None:
            self.usage_unknown = True
            cost = estimate
            prompt = completion = None
        else:
            prompt, completion = int(usage["prompt_tokens"]), int(usage["completion_tokens"])
            self.input_tokens += prompt
            self.output_tokens += completion
        latency = int(diagnostic.get("latency_ms") or 0)
        self.latency_ms += latency
        self.cost += cost
        self.calls.append({"node": node_name, "status": "FAILED" if usage else "FAILED_USAGE_UNKNOWN",
            "prompt_tokens": prompt, "completion_tokens": completion, "latency_ms": latency,
            "estimated_cost_usd": cost, "error_code": code,
            "finish_reason": diagnostic.get("finish_reason"), "cost_is_estimated": True})

    def reserve_query(self, timeout_seconds: float) -> tuple[bool, str | None]:
        if self.queries >= self.max_queries:
            return False, "retrieval_query_limit"
        if time.monotonic() - self.started + max(0, timeout_seconds) > self.max_wall:
            return False, "wall_clock_limit"
        self.queries += 1
        return True, None

    def record_retrieval(self, latency_ms: float | int | None) -> None:
        self.latency_ms += max(0, int(latency_ms or 0))

    def snapshot(self) -> dict:
        return {"limits": {"max_model_requests": self.max_requests, "max_total_tokens": self.max_tokens,
                "max_estimated_cost_usd": self.max_cost, "max_retrieval_queries": self.max_queries,
                "max_wall_seconds": self.max_wall}, "model_requests": self.requests,
                "retrieval_queries": self.queries,
                "input_tokens": None if self.usage_unknown else self.input_tokens,
                "output_tokens": None if self.usage_unknown else self.output_tokens,
                "tokens_unknown": self.usage_unknown, "estimated_cost_usd": round(self.cost, 9),
                "cost_basis": "DeepSeek usage at official peak rates; preflight estimate when usage is unavailable; not an invoice",
                "latency_ms": self.latency_ms, "calls": list(self.calls),
                "elapsed_ms": round((time.monotonic() - self.started) * 1000)}
