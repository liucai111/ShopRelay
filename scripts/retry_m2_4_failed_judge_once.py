"""One targeted retry for a structurally rejected M2.4 judge response.

This retry does not regenerate any answer and carries forward the parent
experiment's global request/cost accounting. It may be run only once for the
recorded failed candidate; SDK retries remain disabled.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from app.m0.config import NodeConfig, create_deepseek_client, load_settings
from scripts.probe_m2_4_paired_ablation import (COST_CAP_USD, PRICE_SOURCE, REQUEST_CAP,
    SELECTED_CASES, StageBudget, judge_one)

DATASET = ROOT / "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
SOURCE_RAW = ROOT / "eval/raw/m2.4-paired-ablation-20261020T050250Z-2f0454bd.json"


def save(path: Path, record: dict) -> None:
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    temp.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.exists():
        raise SystemExit("refusing to overwrite retry record")
    source_bytes = SOURCE_RAW.read_bytes()
    source = json.loads(source_bytes)
    failed = [row for row in source.get("judgments", []) if row.get("status") == "FAILED"]
    if len(failed) != 1:
        raise SystemExit(f"expected one structurally rejected judge result; found {len(failed)}")
    prior = failed[0]
    if prior.get("case_id") != "pr2-return_quality_conditions-02" or prior.get("arm") != "single_hop_bm25":
        raise SystemExit("the one-time retry target differs from the reviewed failure")
    result = next(row for row in source["results"] if row.get("case_id") == prior["case_id"] and
                  row.get("arm") == prior["arm"] and row.get("replicate") is None)
    dataset = json.loads(DATASET.read_text(encoding="utf-8"))
    cases = {row["case_id"]: row for row in dataset["cases"] if row.get("dataset_split") in {"dev", "validation"}
             and row.get("review_status") == "APPROVED"}
    case = cases[prior["case_id"]]
    settings = load_settings(require_api_key=True)
    node = NodeConfig("m2_4_judge", "m2_4_auxiliary_evaluation", "disabled", 1600, 30.0, .05, True)
    client = create_deepseek_client(settings, node)
    old_accounting = source["request_accounting"]
    budget = StageBudget()
    budget.requests = int(old_accounting["requests"])
    budget.estimated_cost_usd = float(old_accounting["measured_cost_usd"])
    budget.unknown_cost_reserve_usd = float(old_accounting["unknown_cost_reserve_usd"])
    record = {"record_type": "m2_4_single_judge_retry", "run_id": str(uuid.uuid4()),
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_raw": str(SOURCE_RAW.relative_to(ROOT)), "source_raw_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "case_id": prior["case_id"], "dataset_split": prior["dataset_split"], "arm": prior["arm"],
        "blind_candidate_id": prior["blind_candidate_id"], "source_failed_judge_request_sequence": prior.get("request_sequence"),
        "retry_reason": "The original complete Judge response cited an unsupplied contract path; strict source validation rejected it. One identical-prompt retry only; no answer regeneration or prompt change.",
        "model_id": settings.model, "sdk_retries": 0, "max_new_requests": REQUEST_CAP,
        "max_cumulative_estimated_cost_usd": COST_CAP_USD, "status": "RUNNING",
        "review_status": "PENDING_HUMAN_REVIEW", "test_candidate_read_for_scoring_or_tuning": False}
    output.parent.mkdir(parents=True, exist_ok=True)
    save(output, record)

    def checkpoint():
        record["request_accounting"] = {"cumulative_requests": budget.requests, "new_requests": budget.requests - int(old_accounting["requests"]),
            "cap": REQUEST_CAP, "cumulative_estimated_cost_usd": round(budget.estimated_cost_usd + budget.unknown_cost_reserve_usd, 9),
            "measured_cost_usd": round(budget.estimated_cost_usd, 9), "unknown_cost_reserve_usd": round(budget.unknown_cost_reserve_usd, 9),
            "cost_cap_usd": COST_CAP_USD, "new_calls": budget.calls, "price_source": PRICE_SOURCE, "not_invoice": True}
        save(output, record)

    budget.on_call = checkpoint
    try:
        response = judge_one(settings, budget, node, client, case, result, prior["blind_candidate_id"])
        record.update({"status": "COMPLETED", "judgment": response["result"], "raw_content": response["raw_content"],
            "usage": response["usage"], "finish_reason": response["finish_reason"],
            "latency_ms": response["latency_ms"], "estimated_cost_usd": response["estimated_cost_usd"],
            "request_sequence": budget.requests, "prompt_version": response["prompt_version"],
            "judge_not_told_method": True})
    except Exception as exc:
        record.update({"status": "FAILED", "exception_type": type(exc).__name__,
            "error_code": getattr(exc, "code", None), "request_sequence": budget.requests,
            "last_call": budget.calls[-1] if budget.calls else None})
    checkpoint()
    print(json.dumps({"status": record["status"], "output": str(output),
        "cumulative_requests": record.get("request_accounting", {}).get("cumulative_requests"),
        "cumulative_estimated_cost_usd": record.get("request_accounting", {}).get("cumulative_estimated_cost_usd")}, ensure_ascii=False))
    return 0 if record["status"] == "COMPLETED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
