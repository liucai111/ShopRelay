"""Bounded post-fix Self-RAG recheck on pinned M2.4 dev/validation cases.

Does not call test_candidate, does not repeat the A/B retrieval arms, and seeds
the stage-wide budget with the immutable prior experiment and one Judge retry.
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from qdrant_client import QdrantClient

from app.m0.config import NodeConfig, create_deepseek_client, load_settings
from app.m1.chat import DeepSeekProvider
from app.m2.retrieval import RetrievalConfig, RetrievalService, config_from_environment
from app.m2.self_rag import SelfRAGConfig
from scripts.probe_m2_4_paired_ablation import (
    JUDGE_PROMPT_VERSION, StageBudget,
    SYSTEM_CONTRACT_SOURCES, _percentile, build_plan, judge_one, load_inputs,
    run_answer_arms,
)

PRIOR_REQUESTS = 66
PRIOR_ESTIMATED_COST_USD = 0.049699272
DEFAULT_OUTPUT = ROOT / "eval/raw" / f"m2.4-self-rag-recheck-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case-id", action="append", required=True)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--prior-accounting", type=Path)
    parser.add_argument("--max-new-requests", type=int, default=48)
    parser.add_argument("--max-new-cost-usd", type=float, default=1.0)
    args = parser.parse_args()
    if args.max_new_requests < 1 or args.max_new_cost_usd <= 0:
        raise SystemExit("new-call limits must be positive")
    output = args.output.resolve()
    if output.exists():
        raise SystemExit(f"refusing to overwrite raw record: {output}")

    prior_requests = PRIOR_REQUESTS
    prior_cost = PRIOR_ESTIMATED_COST_USD
    if args.prior_accounting:
        prior_bytes = args.prior_accounting.read_bytes()
        prior = json.loads(prior_bytes.decode("utf-8"))
        accounting = prior.get("request_accounting") or {}
        cumulative = prior.get("cumulative") or {}
        prior_requests = int(accounting.get("cumulative_requests", accounting.get("new_requests_total",
            cumulative.get("unique_requests"))))
        prior_cost = float(accounting.get("cumulative_estimated_cost_usd", accounting.get("estimated_cost_usd_total",
            cumulative.get("estimated_cost_usd_peak_rate"))))
    settings = load_settings(require_api_key=True)
    plan = build_plan(settings)
    _, cases, _, _ = load_inputs()
    by_id = {row["case_id"]: row for row in cases}
    selected = [by_id[case_id] for case_id in args.case_id]
    if len({row["case_id"] for row in selected}) != len(selected):
        raise SystemExit("duplicate case_id")
    allowed_splits = {"dev"} if all(row["dataset_split"] == "dev" for row in selected) else {"validation"}
    if any(row["dataset_split"] not in allowed_splits for row in selected):
        raise SystemExit("a recheck record must contain only one approved split")
    if any(row.get("scoreability") not in {"SCORABLE_CANDIDATE", "UNSCORABLE_CANDIDATE"} for row in selected):
        raise SystemExit("unexpected scoreability")

    retrieval_config = RetrievalConfig(**{**config_from_environment().__dict__, "mode": "bm25", "qdrant_mode": "local"})
    retrieval = RetrievalService(config=retrieval_config, qdrant_client=QdrantClient(":memory:"),
        collection_namespace="m24_recheck_" + uuid.uuid4().hex[:8])
    budget = StageBudget(max_new_requests=args.max_new_requests,
                         max_new_cost_usd=args.max_new_cost_usd)
    budget.seed_prior(prior_requests, prior_cost)
    raw = {"record_type": "m2_4_self_rag_post_fix_raw", "run_id": str(uuid.uuid4()),
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_raw": "eval/raw/m2.4-paired-ablation-20261020T050250Z-2f0454bd.json",
        "source_raw_sha256": None, "plan_sha256": None, "git_commit": plan["commit"],
        "dataset_sha256": plan["dataset"]["sha256"], "knowledge": plan["knowledge"],
        "split": next(iter(allowed_splits)), "case_ids": [row["case_id"] for row in selected],
        "arms": ["self_rag_max_3_hops"], "sdk_retries": 0, "test_candidate_read_for_scoring_or_tuning": False,
        "status": "RUNNING", "results": [], "judgments": [],
        "human_review_status": "PENDING_HUMAN_REVIEW",
        "prior_accounting_path": str(args.prior_accounting) if args.prior_accounting else None,
        "prior_accounting_sha256": __import__("hashlib").sha256(args.prior_accounting.read_bytes()).hexdigest()
            if args.prior_accounting else None,
        "budget_seed": {"prior_requests": prior_requests, "prior_estimated_cost_usd": prior_cost,
            "request_cap": args.max_new_requests, "estimated_cost_cap_usd": args.max_new_cost_usd}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")

    def checkpoint():
        raw["request_accounting"] = {"new_requests": len(budget.calls), "cumulative_requests": budget.requests,
            "max_requests": budget.max_new_requests,
            "new_estimated_cost_usd": round(sum(float(call.get("estimated_cost_usd", 0)) for call in budget.calls), 9),
            "cumulative_estimated_cost_usd": round(budget.estimated_cost_usd + budget.unknown_cost_reserve_usd, 9),
            "max_estimated_cost_usd": budget.max_new_cost_usd, "calls": budget.calls,
            "price_source": "https://api-docs.deepseek.com/quick_start/pricing/", "not_invoice": True}
        tmp = output.with_suffix(output.suffix + ".tmp")
        tmp.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
        tmp.replace(output)

    budget.on_call = checkpoint
    try:
        provider = type("Budgeted", (), {})()
        provider.budget = budget
        # Reuse the same call wrapper and request settings as the primary experiment.
        from scripts.probe_m2_4_paired_ablation import BudgetedProvider
        provider = BudgetedProvider(DeepSeekProvider(settings=settings), budget)
        result_rows = run_answer_arms(selected, retrieval, provider, SelfRAGConfig(), raw,
            arms=["self_rag_max_3_hops"],
            on_result=lambda rows: (raw.update({"results": list(rows)}), checkpoint()))
        raw["results"] = result_rows
        judge_node = NodeConfig("m2_4_judge", "m2_4_auxiliary_evaluation", "disabled", 1600, 30.0, .05, True)
        judge_client = create_deepseek_client(settings, judge_node)
        for i, (case, result) in enumerate(zip(selected, result_rows), 1):
            if result.get("answer_kind") == "GENERATION_UNAVAILABLE" or not result.get("answer"):
                raw["judgments"].append({"case_id": case["case_id"], "status": "NOT_RUN",
                    "reason": "no validated answer was generated", "review_status": "PENDING_HUMAN_REVIEW"})
                continue
            blind_id = f"postfix-{i}-{uuid.uuid4().hex[:10]}"
            try:
                judged = judge_one(settings, budget, judge_node, judge_client, case, result, blind_id)
                raw["judgments"].append({"case_id": case["case_id"], "dataset_split": case["dataset_split"],
                    "arm": result["arm"], "blind_candidate_id": blind_id,
                    "request_sequence": budget.requests, "status": "COMPLETED", "judgment": judged["result"],
                    "raw_content": judged["raw_content"], "usage": judged["usage"],
                    "validation_attempts": judged.get("validation_attempts", []),
                    "provider_request_id": judged.get("provider_request_id"),
                    "model_id": judged["model_id"], "finish_reason": judged["finish_reason"],
                    "latency_ms": judged["latency_ms"], "estimated_cost_usd": judged["estimated_cost_usd"],
                    "prompt_version": JUDGE_PROMPT_VERSION, "judge_not_told_method": True,
                    "review_status": "PENDING_HUMAN_REVIEW"})
            except Exception as exc:
                raw["judgments"].append({"case_id": case["case_id"], "arm": result["arm"],
                    "blind_candidate_id": blind_id, "status": "FAILED", "exception_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", None), "validation_attempts": getattr(exc, "attempts", []),
                    "review_status": "PENDING_HUMAN_REVIEW"})
            checkpoint()
        raw["status"] = "COMPLETE" if all(row.get("status") == "COMPLETED" for row in raw["results"]) else "COMPLETE_WITH_ERRORS"
    except Exception as exc:
        raw["status"] = "BLOCKED_BUDGET" if type(exc).__name__ == "StageBudgetExceeded" else "FAILED"
        raw["terminal_error"] = {"exception_type": type(exc).__name__, "safe_message": str(exc)[:200]}
    finally:
        checkpoint()
    print(json.dumps({"output": str(output), "status": raw["status"],
        "new_requests": len(budget.calls), "cumulative_requests": budget.requests,
        "cumulative_estimated_cost_usd": round(budget.estimated_cost_usd + budget.unknown_cost_reserve_usd, 9),
        "answer_kinds": [row.get("answer_kind") for row in raw["results"]]}, ensure_ascii=False, indent=2))
    return 0 if raw["status"] == "COMPLETE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
