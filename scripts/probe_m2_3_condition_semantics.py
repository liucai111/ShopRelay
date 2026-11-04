"""One-call semantic check of M2.3 condition extraction against approved dev gold."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from app.m0.config import load_settings
from app.m1.chat import ChatFailure, DeepSeekProvider
from app.m2.self_rag import _PHASE_ORDER, _policy_stages

CASE_ID = "pr2-multi_clause_return_refund-03"
DATASET_PATH = ROOT / "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"


def semantics(text: str) -> dict:
    windows = sorted({f"{amount}{unit}" for amount, unit in
                      re.findall(r"(\d+)\s*(天|日|小时|周|个月|月)", text or "")})
    stages = sorted(_policy_stages(text or ""), key=lambda value: _PHASE_ORDER[value])
    return {"stages": stages, "windows": windows}


def main() -> None:
    dataset_bytes = DATASET_PATH.read_bytes()
    dataset = json.loads(dataset_bytes.decode("utf-8"))
    case = next(row for row in dataset["cases"] if row["case_id"] == CASE_ID)
    if case.get("review_status") != "APPROVED" or case.get("dataset_split") not in {"dev", "validation"}:
        raise SystemExit("Refusing to call model for a non-approved dev/validation sample")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    settings = load_settings(require_api_key=True)
    node = settings.nodes["condition_extraction"]
    record = {"record_type": "m2.3_condition_semantics_single_call",
              "created_at_utc": datetime.now(timezone.utc).isoformat(), "status": "BLOCKED",
              "case_id": CASE_ID, "dataset_path": DATASET_PATH.relative_to(ROOT).as_posix(),
              "dataset_version": dataset.get("dataset_version"),
              "dataset_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
              "dataset_split": case["dataset_split"], "question": case["user_query"],
              "model": {"id": settings.model, "node": "condition_extraction",
              "thinking": node.thinking, "max_tokens": node.max_tokens,
                        "timeout_seconds": node.timeout_seconds, "prompt_version": "m2.3-condition-extraction-v6"},
              "api_requests": 1, "secrets_recorded": False, "test_candidate_read": False,
              "gold_required_semantics": [
                  {"condition_id": row["condition_id"], **semantics(row["text"])}
                  for row in case["required_conditions"] if row.get("required")],
              "response": None, "semantic_audit": None}
    try:
        response = DeepSeekProvider(settings=settings).extract_conditions(case["user_query"])
        result = response["conditions_result"]
        required = [item for item in result["conditions"] if item.get("required")]
        gold = [{"condition_id": row["condition_id"], **semantics(row["text"])}
                for row in case["required_conditions"] if row.get("required")]
        candidate_semantics = [{"condition_id": row["condition_id"], "kind": row["kind"],
                               "source_type": row["source_type"], "required": row["required"],
                               "text": row["text"], **semantics(row["text"])} for row in required]
        issues = []
        if result["request_mode"] != "POLICY_EXPLANATION":
            issues.append("request mode must be POLICY_EXPLANATION")
        if any(row["kind"] != "POLICY_RULE" or row["source_type"] != "POLICY" for row in required):
            issues.append("a required item is not sourced as a policy rule")
        if any(not row["stages"] and not row["windows"] for row in candidate_semantics):
            issues.append("a required condition has no identifiable policy action, lifecycle phase, or time window")
        gold_stages = set().union(*(set(row["stages"]) for row in gold)) if gold else set()
        candidate_stages = set().union(*(set(row["stages"]) for row in candidate_semantics)) if candidate_semantics else set()
        gold_windows = set().union(*(set(row["windows"]) for row in gold)) if gold else set()
        candidate_windows = set().union(*(set(row["windows"]) for row in candidate_semantics)) if candidate_semantics else set()
        if candidate_stages != gold_stages:
            issues.append("required lifecycle phases differ from approved policy requirements")
        if candidate_windows != gold_windows:
            issues.append("required time windows differ from approved policy requirements")
        # Each independently approved gold rule must map to a candidate rule; a single
        # broad condition cannot erase the distinction between the two policy clauses.
        unmatched = []
        remaining = list(candidate_semantics)
        for gold_rule in gold:
            match = next((item for item in remaining
                          if set(gold_rule["stages"]).issubset(item["stages"]) and
                          set(gold_rule["windows"]).issubset(item["windows"])), None)
            if match is None:
                unmatched.append(gold_rule["condition_id"])
            else:
                remaining.remove(match)
        if unmatched:
            issues.append("approved policy rules are not represented independently: " + ", ".join(unmatched))
        record["response"] = response
        record["candidate_required_semantics"] = candidate_semantics
        record["semantic_audit"] = {"status": "PASS" if not issues else "FAIL",
                                    "issues": issues, "gold_stages": sorted(gold_stages),
                                    "candidate_stages": sorted(candidate_stages),
                                    "gold_windows": sorted(gold_windows),
                                    "candidate_windows": sorted(candidate_windows),
                                    "criterion": "semantic stage/window and one-to-one rule coverage; no condition-count threshold"}
        record["status"] = "PASS" if not issues and response.get("finish_reason") == "stop" else "BLOCKED"
    except ChatFailure as exc:
        diagnostic = dict(exc.diagnostic)
        if "raw_content" in diagnostic:
            record["raw_output"] = diagnostic["raw_content"]
        for key in ("usage", "finish_reason", "latency_ms", "thinking", "max_tokens", "timeout_seconds"):
            if key in diagnostic:
                record[key] = diagnostic[key]
        record["error"] = {"code": exc.code, "retryable": exc.retryable,
                            "provider_exception": diagnostic.get("provider_exception"),
                            "validation_errors": diagnostic.get("validation_errors")}
        record["status"] = "BLOCKED"
    except Exception as exc:
        record["error"] = {"code": "UNEXPECTED", "exception_type": type(exc).__name__}
        record["status"] = "BLOCKED"

    out = ROOT / "docs/probes" / f"m2.3-condition-semantics-{stamp}.json"
    out.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    usage = (record.get("response") or {}).get("usage") or record.get("usage")
    print(json.dumps({"status": record["status"], "prompt_version": record["model"]["prompt_version"],
                      "request_mode": ((record.get("response") or {}).get("conditions_result") or {}).get("request_mode"),
                      "semantic_audit": record.get("semantic_audit"), "usage": usage,
                      "latency_ms": ((record.get("response") or {}).get("latency_ms") or record.get("latency_ms")),
                      "cost_is_invoice": False, "price_source": PRICE_SOURCE,
                      "evidence_path": out.relative_to(ROOT).as_posix()}, ensure_ascii=False, indent=2))
    if record["status"] != "PASS":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
