"""Append a reproducible route diagnostic; formal scores require route-approved labels."""
from __future__ import annotations

import json
import hashlib
import subprocess
import uuid
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.m1.routing import PROMPT_VERSION, ROUTE_VERSION, decide_route
from app.m0.config import load_settings

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "eval/raw"


def _metrics(rows: list[dict]) -> dict:
    if not rows:
        return {"status": "NOT_RUN", "sample_count": 0}
    labels = ("DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE")
    confusion = defaultdict(Counter)
    for row in rows:
        confusion[row["expected_strategy_candidate"]][row["predicted_strategy"]] += 1
    per_class = {}
    f1s = []
    for label in labels:
        tp = confusion[label][label]
        fp = sum(confusion[other][label] for other in labels if other != label)
        fn = sum(confusion[label][other] for other in labels if other != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        per_class[label] = {"support": sum(confusion[label].values()), "precision": precision,
                            "recall": recall, "f1": f1}
        f1s.append(f1)
    correct = sum(confusion[label][label] for label in labels)
    present = [label for label in labels if per_class[label]["support"] > 0]
    return {"status": "PASS", "sample_count": len(rows), "accuracy": correct / len(rows),
            "macro_f1": sum(f1s) / len(labels),
            "macro_f1_present_classes": (sum(per_class[label]["f1"] for label in present) / len(present)
                                         if present else None),
            "present_classes": present, "per_class": per_class,
            "confusion_matrix": {key: dict(value) for key, value in confusion.items()}}


def load_reviewed_dataset(dataset_path: Path) -> dict:
    dataset_path = dataset_path.resolve()
    reviewed_dir = (ROOT / "eval/dataset").resolve()
    valid_name = (dataset_path.name.startswith("route-m2.1-dev-validation-reviewed-") or
                  dataset_path.name == "route-m2.1-dev-validation-user-confirmed.json" or
                  dataset_path.name.startswith("route-m2.1-dev-validation-user-confirmed-r"))
    if dataset_path.parent != reviewed_dir or not valid_name:
        raise ValueError("only versioned dev/validation reviewed datasets are accepted")
    data = json.loads(dataset_path.read_text(encoding="utf-8"))
    import_meta = data.get("route_review_import", {})
    if data.get("status") != "VERSIONED_HUMAN_REVIEW_IMPORT" or import_meta.get("test_candidate_rows_included") is not False:
        raise ValueError("dataset lacks human-review import and test-candidate exclusion metadata")
    if import_meta.get("confirmation_actor") == "CHAT_USER_SELF_CONFIRMED":
        if (import_meta.get("per_case_reviewed_at_recorded") is not False or
                import_meta.get("independent_blind_review") is not False or
                import_meta.get("identity_verification") != "NOT_PERFORMED"):
            raise ValueError("chat-confirmed import has inconsistent review-time or identity metadata")
    cases = data.get("cases", [])
    if not cases or any(case.get("dataset_split") not in {"dev", "validation"} for case in cases):
        raise ValueError("reviewed route dataset must contain dev/validation rows only")
    return data


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True,
                        help="versioned reviewed dev/validation JSON; candidate/test datasets are not accepted")
    parser.add_argument("--output-dir", default=str(OUT))
    args = parser.parse_args()
    dataset_path = Path(args.dataset).resolve()
    output_dir = Path(args.output_dir).resolve()
    if ROOT not in output_dir.parents:
        raise SystemExit("output directory must be inside ShopRelay")
    data = load_reviewed_dataset(dataset_path)
    cases = data["cases"]
    predictions = []
    for case in cases:
        decision = decide_route(case["user_query"])
        predictions.append({"case_id": case["case_id"], "group_id": case["group_id"],
            "dataset_split": case["dataset_split"], "review_status": case["review_status"],
            "expected_strategy_candidate": case["expected_strategy"], "predicted_strategy": decision.strategy,
            "decision_source": decision.decision_source, "reason_code": decision.reason_code,
            "latency_ms": decision.latency_ms, "error_reason": None})
    approved = [row for row in predictions if row["review_status"] == "APPROVED"]
    formal_metrics = {"overall": _metrics(approved), "by_split": {
        split: _metrics([row for row in approved if row["dataset_split"] == split])
        for split in ("dev", "validation")}}
    source_counts = Counter(row["decision_source"] for row in predictions)
    confusion = defaultdict(Counter)
    unapproved_confusion = defaultdict(Counter)
    unapproved_correct = 0
    unapproved_rows = [row for row in predictions if row["review_status"] != "APPROVED"]
    for row in approved:
        confusion[row["expected_strategy_candidate"]][row["predicted_strategy"]] += 1
    for row in unapproved_rows:
        expected = row["expected_strategy_candidate"]
        predicted = row["predicted_strategy"]
        unapproved_confusion[expected][predicted] += 1
        unapproved_correct += expected == predicted
    unapproved_metrics = {}
    for label in ("DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"):
        tp = unapproved_confusion[label][label]
        fp = sum(unapproved_confusion[other][label] for other in unapproved_confusion if other != label)
        fn = sum(count for guessed, count in unapproved_confusion[label].items() if guessed != label)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        unapproved_metrics[label] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision,
            "recall": recall, "f1": 2*precision*recall/(precision+recall) if precision+recall else 0.0}
    latencies = sorted(row["latency_ms"] for row in predictions)
    settings = load_settings(require_api_key=False)
    node = settings.nodes["route"]
    configuration = {"route_version": ROUTE_VERSION, "prompt_version": PROMPT_VERSION,
        "thinking": node.thinking, "max_tokens": node.max_tokens,
        "timeout_seconds": node.timeout_seconds, "max_cost_usd": node.max_cost_usd,
        "model": settings.model}
    configuration_hash = hashlib.sha256(json.dumps(configuration, sort_keys=True).encode()).hexdigest()
    git_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                                capture_output=True, text=True).stdout.strip()
    now = datetime.now(timezone.utc)
    run_id = f"route-m2.1-{now:%Y%m%dT%H%M%S%fZ}-{uuid.uuid4().hex[:8]}"
    record = {
        "record_type": "route_m2_1_rule_diagnostic", "run_id": run_id, "created_at_utc": now.isoformat(),
        "status": "DIAGNOSTIC_ONLY", "git_commit": git_commit,
        "working_tree_includes_uncommitted_m2_1_changes": True,
        "configuration": configuration, "configuration_sha256": configuration_hash,
        "dataset": str(dataset_path.relative_to(ROOT)).replace("\\", "/"),
        "dataset_version": data["dataset_version"], "dataset_sha256_canonical_cases": data["dataset_sha256_canonical_cases"],
        "route_version": ROUTE_VERSION, "prompt_version": PROMPT_VERSION,
        "execution": "deterministic rules only; no DeepSeek API calls; no test_candidate rows evaluated",
        "sample_count": len(predictions), "approved_route_label_count": len(approved),
        "formal_metrics": formal_metrics,
        "unapproved_label_diagnostic_only": {
            "status": "DIAGNOSTIC_ONLY" if unapproved_rows else "NOT_RUN_NO_UNAPPROVED_ROWS",
            "warning": "Unapproved expected labels are diagnostics only; they are never included in formal metrics.",
            "agreement_count": unapproved_correct, "sample_count": len(unapproved_rows),
            "agreement_rate": unapproved_correct / len(unapproved_rows) if unapproved_rows else None,
            "per_class_diagnostic": unapproved_metrics if unapproved_rows else {},
            "macro_f1_diagnostic": (sum(item["f1"] for item in unapproved_metrics.values()) / len(unapproved_metrics)
                                    if unapproved_rows else None),
            "confusion_matrix": {key: dict(value) for key, value in unapproved_confusion.items()},
            "incorrect_cases": [row["case_id"] for row in unapproved_rows if row["expected_strategy_candidate"] != row["predicted_strategy"]],
        },
        "diagnostic_counts_by_predicted_strategy": dict(Counter(row["predicted_strategy"] for row in predictions)),
        "decision_source_counts": dict(source_counts),
        "latency_ms": {"p50": latencies[(len(latencies)-1)//2] if latencies else None,
                       "p95": latencies[min(len(latencies)-1, int(len(latencies)*.95))] if latencies else None},
        "model_api_calls": 0, "usage": None, "estimated_cost_usd": 0,
        "confusion_matrix_on_approved_route_labels": {key: dict(value) for key, value in confusion.items()},
        "per_case": predictions,
    }
    output_dir.mkdir(exist_ok=True)
    target = output_dir / f"{run_id}.json"
    with target.open("x", encoding="utf-8") as handle:
        json.dump(record, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    print(f"{target.relative_to(ROOT)} samples={len(predictions)} approved_route_labels={len(approved)} api_calls=0")


if __name__ == "__main__":
    main()
