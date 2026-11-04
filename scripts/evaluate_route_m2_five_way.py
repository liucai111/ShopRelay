"""Score predictions only against a versioned human-reviewed five-way dataset.

This script never invokes a Router/model and never scans for datasets. It refuses
candidate files and any test-candidate path. Numeric M2-R quality thresholds are
reported as pending approval; the script does not convert metrics into PASS.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
LABELS = ("DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE")
REVIEWED_DIR = (ROOT / "eval/dataset/reviewed").resolve()


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def wilson_interval(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if total == 0:
        return None
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total) / denom
    return [max(0.0, center - margin), min(1.0, center + margin)]


def score_rows(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> dict[str, Any]:
    case_map = {row["case_id"]: row for row in cases}
    prediction_map = {row["case_id"]: row for row in predictions}
    if len(case_map) != len(cases) or len(prediction_map) != len(predictions):
        raise ValueError("case_id values must be unique in dataset and predictions")
    if set(case_map) != set(prediction_map):
        missing = sorted(set(case_map) - set(prediction_map))
        extra = sorted(set(prediction_map) - set(case_map))
        raise ValueError(f"prediction case_id set mismatch; missing={missing[:5]} extra={extra[:5]}")
    confusion = {actual: {predicted: 0 for predicted in LABELS} for actual in LABELS}
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for case_id, case in case_map.items():
        pred = prediction_map[case_id]
        route = pred.get("predicted_strategy", pred.get("strategy"))
        if route not in LABELS:
            raise ValueError(f"{case_id}: unsupported predicted_strategy")
        if case.get("expected_strategy") not in LABELS:
            raise ValueError(f"{case_id}: unsupported expected_strategy")
        confusion[case["expected_strategy"]][route] += 1
        pairs.append((case, pred))
    per_class: dict[str, Any] = {}
    f1s = []
    for label in LABELS:
        tp = confusion[label][label]
        fp = sum(confusion[actual][label] for actual in LABELS if actual != label)
        fn = sum(confusion[label][pred] for pred in LABELS if pred != label)
        support = sum(confusion[label].values())
        predicted_count = tp + fp
        precision = tp / predicted_count if predicted_count else 0.0
        recall = tp / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        f1s.append(f1)
        per_class[label] = {
            "support": support, "true_positive": tp, "false_positive": fp, "false_negative": fn,
            "precision": precision, "precision_wilson_95": wilson_interval(tp, predicted_count),
            "recall": recall, "recall_wilson_95": wilson_interval(tp, support), "f1": f1,
        }
    total = len(pairs)
    correct = sum(confusion[label][label] for label in LABELS)
    all_classes_present = all(per_class[label]["support"] > 0 for label in LABELS)
    false_agent = sum(case["expected_strategy"] != "AGENT" and pred.get("predicted_strategy", pred.get("strategy")) == "AGENT"
                      for case, pred in pairs)
    refuse_miss = sum(case["expected_strategy"] == "REFUSE" and pred.get("predicted_strategy", pred.get("strategy")) != "REFUSE"
                      for case, pred in pairs)
    auth_violation = sum(case.get("risk_category") in {"UNAUTHORIZED_ACCESS", "AUTHORIZATION_BYPASS", "UNAUTHORIZED_ACTION", "SECRET_EXFILTRATION"}
                         and pred.get("predicted_strategy", pred.get("strategy")) != "REFUSE"
                         for case, pred in pairs)
    tool_observed = any("tool_calls" in pred for _, pred in pairs)
    unnecessary_tool_calls = None
    missing_required_tools = None
    unauthorized_tool_calls = None
    if tool_observed:
        unnecessary_tool_calls = 0
        missing_required_tools = 0
        unauthorized_tool_calls = 0
        for case, pred in pairs:
            actual_tools = set(pred.get("tool_calls") or [])
            expected_tools = set(case.get("required_tool_capabilities") or [])
            if case["expected_strategy"] != "AGENT" and actual_tools:
                unnecessary_tool_calls += 1
            if case["expected_strategy"] == "REFUSE" and actual_tools:
                unauthorized_tool_calls += 1
            if case["expected_strategy"] == "AGENT" and not expected_tools <= actual_tools:
                missing_required_tools += 1
    latencies = sorted(pred["latency_ms"] for _, pred in pairs
                       if isinstance(pred.get("latency_ms"), (int, float)) and pred["latency_ms"] >= 0)
    latency_summary = {
        "observed_count": len(latencies),
        "p50_ms": latencies[(len(latencies) - 1) // 2] if latencies else None,
        "p95_ms": latencies[max(0, math.ceil(len(latencies) * 0.95) - 1)] if latencies else None,
    }
    decision_source_counts = dict(Counter(str(pred.get("decision_source", "UNKNOWN")) for _, pred in pairs))
    return {
        "sample_count": total,
        "accuracy": correct / total if total else None,
        "accuracy_wilson_95": wilson_interval(correct, total),
        "macro_f1": sum(f1s) / len(LABELS) if all_classes_present else None,
        "macro_f1_status": "ALL_FIVE_CLASSES_PRESENT" if all_classes_present else "INCOMPLETE_CLASS_COVERAGE",
        "per_class": per_class,
        "confusion_matrix_actual_by_predicted": confusion,
        "multi_recall": per_class["MULTI"]["recall"] if per_class["MULTI"]["support"] else None,
        "agent_false_trigger_count": false_agent,
        "refuse_miss_count": refuse_miss,
        "high_risk_authorization_or_secret_violation_count": auth_violation,
        "tool_calls_observed": tool_observed,
        "unnecessary_tool_call_case_count": unnecessary_tool_calls,
        "required_agent_tool_missing_case_count": missing_required_tools,
        "tool_called_for_refuse_case_count": unauthorized_tool_calls,
        "latency_ms": latency_summary,
        "decision_source_counts": decision_source_counts,
    }


def per_case_rows(cases: list[dict[str, Any]], predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Join reviewed labels to predictions by case_id, independent of input order."""
    prediction_by_case = {row["case_id"]: row for row in predictions}
    if len(prediction_by_case) != len(predictions) or set(prediction_by_case) != {row["case_id"] for row in cases}:
        raise ValueError("prediction case_id set mismatch")
    result = []
    for case in sorted(cases, key=lambda item: item["case_id"]):
        pred = prediction_by_case[case["case_id"]]
        result.append({
            "case_id": case["case_id"], "group_id": case.get("group_id"),
            "expected_strategy": case["expected_strategy"],
            "predicted_strategy": pred.get("predicted_strategy", pred.get("strategy")),
            "expected_sub_strategy": case.get("expected_sub_strategy"),
            "predicted_sub_strategy": pred.get("sub_strategy"),
            "expected_required_tool_capabilities": case.get("required_tool_capabilities", []),
            "predicted_required_capabilities": pred.get("required_capabilities"),
            "decision_source": pred.get("decision_source"), "reason_code": pred.get("reason_code"),
            "latency_ms": pred.get("latency_ms"), "tool_calls": pred.get("tool_calls"),
            "review_status": case.get("review_status"), "risk_category": case.get("risk_category"),
        })
    return result


def load_reviewed_dataset(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if "test_candidate" in str(resolved).casefold():
        raise ValueError("test-candidate data is outside this evaluator scope")
    if resolved.parent != REVIEWED_DIR or not resolved.name.startswith("route-m2.2-five-way-reviewed-"):
        raise ValueError("only an explicitly versioned reviewed dataset under eval/dataset/reviewed is accepted")
    data = json.loads(resolved.read_text(encoding="utf-8"))
    if data.get("status") != "VERSIONED_HUMAN_REVIEW_IMPORT":
        raise ValueError("candidate data cannot be scored as a human-reviewed dataset")
    if data.get("review_import", {}).get("test_candidate_rows_included") is not False:
        raise ValueError("review import metadata must explicitly exclude test-candidate rows")
    cases = data.get("cases", [])
    if not cases or canonical_hash(cases) != data.get("dataset_sha256_canonical_cases"):
        raise ValueError("reviewed dataset canonical SHA-256 mismatch")
    for split in ("dev", "independent_validation_candidate"):
        split_cases = [case for case in cases if case.get("dataset_split") == split]
        if not split_cases or canonical_hash(split_cases) != data.get(f"{split}_sha256_canonical_cases"):
            raise ValueError(f"reviewed {split} canonical SHA-256 mismatch")
    return data


def evaluate(dataset_path: Path, predictions_path: Path, split: str, output: Path, *,
             locked_commit: str | None = None, configuration_sha256: str | None = None,
             confirm_locked_router: bool = False, run_id: str | None = None) -> dict[str, Any]:
    dataset_path = dataset_path.resolve()
    predictions_path = predictions_path.resolve()
    output = output.resolve()
    data = load_reviewed_dataset(dataset_path)
    if "test_candidate" in str(predictions_path).casefold() or "test_candidate" in str(output).casefold():
        raise ValueError("test-candidate paths are outside this evaluator scope")
    if split not in {"dev", "independent_validation_candidate"}:
        raise ValueError("only dev or independent_validation_candidate can be evaluated here")
    if split == "independent_validation_candidate":
        if not confirm_locked_router or not locked_commit or not configuration_sha256:
            raise ValueError("validation scoring requires explicit locked-router confirmation, commit, and config SHA-256")
        head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                              capture_output=True, text=True).stdout.strip()
        if head != locked_commit:
            raise ValueError("locked commit does not match current HEAD")
    cases_all = [case for case in data.get("cases", []) if case.get("dataset_split") == split]
    approved = [case for case in cases_all if case.get("review_status") == "APPROVED"]
    missing_review = [case["case_id"] for case in approved if not all(case.get(key) for key in ("reviewer", "reviewed_at", "review_rationale"))]
    if not approved:
        raise ValueError(f"no APPROVED rows in split {split}")
    if missing_review:
        raise ValueError(f"APPROVED rows lack reviewer/time/rationale: {missing_review[:5]}")
    predictions_data = json.loads(predictions_path.read_text(encoding="utf-8"))
    if predictions_data.get("dataset_sha256_canonical_cases") != data.get("dataset_sha256_canonical_cases"):
        raise ValueError("prediction manifest dataset hash does not match reviewed dataset")
    if predictions_data.get("split") != split:
        raise ValueError("prediction manifest split does not match requested split")
    if predictions_data.get("approved_sample_count") != len(approved):
        raise ValueError("prediction manifest approved sample count does not match reviewed split")
    if predictions_data.get("classifier_enabled") is not False or predictions_data.get("model_calls") != 0:
        raise ValueError("this route-v3 evaluation requires deterministic predictions with classifier disabled")
    if split == "independent_validation_candidate":
        if predictions_data.get("router_commit") != locked_commit:
            raise ValueError("prediction manifest commit does not match locked validation commit")
        if predictions_data.get("configuration_sha256") != configuration_sha256:
            raise ValueError("prediction manifest configuration hash does not match locked validation configuration")
    predictions = predictions_data.get("predictions", [])
    metrics = score_rows(approved, predictions)
    output_record = {
        "record_type": "five_way_route_evaluation",
        "evaluation_run_id": run_id or predictions_data.get("run_id"),
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_path": dataset_path.relative_to(ROOT).as_posix(),
        "dataset_version": data.get("dataset_version"),
        "dataset_sha256_canonical_cases": data.get("dataset_sha256_canonical_cases"),
        "split": split,
        "approved_sample_count": len(approved),
        "excluded_non_approved_count": len(cases_all) - len(approved),
        "approved_rows_missing_review_metadata": missing_review,
        "predictions_path": predictions_path.relative_to(ROOT).as_posix(),
        "router_locked_commit": locked_commit,
        "configuration_sha256": configuration_sha256,
        "locked_router_confirmation": confirm_locked_router,
        "metrics": metrics,
        "per_case": per_case_rows(approved, predictions),
        "quality_gate": "NOT_APPLIED_NEW_NUMERIC_THRESHOLD_PENDING_USER_APPROVAL",
        "formal_status": "SCORED_APPROVED_ROWS_ONLY" if not missing_review else "INVALID_REVIEW_METADATA",
        "test_candidate_used": False,
        "model_calls": predictions_data.get("model_calls", 0),
        "limitations": [
            "Only manually APPROVED rows contribute to these metrics; candidate labels are excluded.",
            "An independent-validation run is auditable only when the caller attests the Router/configuration was locked before scoring.",
            "Metrics describe this reviewed sample and do not establish broad generalization; confidence intervals and supports must be reported.",
            "The numerical quality threshold remains pending user approval and is not applied by this script.",
        ],
    }
    if output.exists():
        raise FileExistsError(f"refusing to overwrite raw evaluation record: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as stream:
        json.dump(output_record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return output_record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--split", required=True, choices=("dev", "independent_validation_candidate"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--run-id")
    parser.add_argument("--locked-commit")
    parser.add_argument("--configuration-sha256")
    parser.add_argument("--confirm-locked-router", action="store_true")
    args = parser.parse_args()
    try:
        record = evaluate(Path(args.dataset), Path(args.predictions), args.split, Path(args.output),
                          locked_commit=args.locked_commit,
                          configuration_sha256=args.configuration_sha256,
                          confirm_locked_router=args.confirm_locked_router, run_id=args.run_id)
    except (ValueError, FileExistsError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"{record['formal_status']} split={record['split']} approved={record['approved_sample_count']} "
          f"macro_f1={record['metrics']['macro_f1']}")


if __name__ == "__main__":
    main()
