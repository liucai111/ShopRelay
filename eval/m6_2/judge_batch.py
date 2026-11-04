"""Bounded one-shot Judge sample sequencer with caller-owned durable accounting."""
from __future__ import annotations

import time
from typing import Callable, Any
from .judge_contract import parse_response


def execute_samples(samples: list[dict[str, Any]], *,
                    send: Callable[[dict[str, Any]], Any],
                    reserve_before_send: Callable[[str], None],
                    settle_after_response: Callable[[str, Any, dict[str, Any]], None]) -> dict[str, Any]:
    """Attempt each sample at most once; stop on any shared format/usage failure.

    The durable reserve callback runs before send. The settle callback must
    persist the raw response and usage before this loop can continue.
    """
    results: list[dict[str, Any]] = []
    stop_reason = None
    for sample in samples:
        sample_id = sample["calibration_sample_id"]
        payload = sample["judge_payload"]
        reserve_before_send(sample_id)
        try:
            started = time.monotonic()
            response = send(payload)
        except Exception as exc:
            result = {"sample_id": sample_id, "status": "REQUEST_EXCEPTION", "format_valid": False,
                      "errors": ["request_exception", type(exc).__name__[:80]], "usage": None,
                      "response_received": False, "elapsed_seconds": round(time.monotonic() - started, 6)}
            settle_after_response(sample_id, None, result)
            results.append(result)
            stop_reason = "REQUEST_OR_USAGE_UNKNOWN"
            break

        choice = response.choices[0] if getattr(response, "choices", None) else None
        message = choice.message if choice else None
        content = getattr(message, "content", None)
        usage_obj = getattr(response, "usage", None)
        usage = None if usage_obj is None else {
            "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
            "completion_tokens": getattr(usage_obj, "completion_tokens", None),
            "total_tokens": getattr(usage_obj, "total_tokens", None),
        }
        refs = {source["source_ref"] for source in payload["applicable_sources"]}
        status, parsed, errors = parse_response(content, refs)
        result = {"sample_id": sample_id, "status": status, "format_valid": status == "VALID",
                  "errors": errors, "response": parsed, "response_content": content,
                  "usage": usage, "response_received": True,
                  "model": getattr(response, "model", None),
                  "finish_reason": getattr(choice, "finish_reason", None),
                  "elapsed_seconds": round(time.monotonic() - started, 6)}
        settle_after_response(sample_id, response, result)
        results.append(result)
        if usage is None or any(not isinstance(usage.get(k), int) or isinstance(usage.get(k), bool) or usage[k] < 0 for k in
                                ("prompt_tokens", "completion_tokens", "total_tokens")):
            stop_reason = "REQUEST_OR_USAGE_UNKNOWN"
            break
        if usage["total_tokens"] != usage["prompt_tokens"] + usage["completion_tokens"]:
            stop_reason = "REQUEST_OR_USAGE_UNKNOWN"
            break
        if status != "VALID":
            stop_reason = "FORMAT_FAILURE"
            break
    return {"results": results, "stop_reason": stop_reason,
            "completed_samples": len(results), "remaining_samples": [x["calibration_sample_id"] for x in samples[len(results):]]}
