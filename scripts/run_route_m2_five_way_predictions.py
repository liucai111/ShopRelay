"""Collect deterministic route predictions for APPROVED five-way cases only.

This is an explicit post-review runner. It never loads candidate labels as gold,
never enables the LLM classifier, and rejects test-candidate paths.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.m1 import routing
from app.m1.routing import ROUTE_VERSION, decide_route

ROOT = Path(__file__).resolve().parents[1]
REVIEWED_DIR = (ROOT / "eval/dataset/reviewed").resolve()
RAW_DIR = (ROOT / "eval/raw").resolve()


def canonical_hash(value: Any) -> str:
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def router_configuration() -> dict[str, Any]:
    source_path = Path(routing.__file__).resolve()
    return {
        "route_version": ROUTE_VERSION,
        "router_source_sha256": hashlib.sha256(source_path.read_bytes()).hexdigest(),
        "classifier_enabled": False,
        "execution": "deterministic_decide_route_without_classifier",
    }


def configuration_sha256() -> str:
    return canonical_hash(router_configuration())


def current_commit() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, check=True,
                          capture_output=True, text=True).stdout.strip()


def load_reviewed_dataset(path: Path) -> dict[str, Any]:
    resolved = path.resolve()
    if "test_candidate" in str(resolved).casefold():
        raise ValueError("test-candidate paths are outside this runner")
    if resolved.parent != REVIEWED_DIR or not resolved.name.startswith("route-m2.2-five-way-reviewed-"):
        raise ValueError("only a versioned five-way reviewed dataset under eval/dataset/reviewed is accepted")
    data = json.loads(resolved.read_text(encoding="utf-8"))
    if data.get("status") != "VERSIONED_HUMAN_REVIEW_IMPORT":
        raise ValueError("candidate data cannot be used to generate formal predictions")
    if data.get("review_import", {}).get("test_candidate_rows_included") is not False:
        raise ValueError("reviewed dataset must explicitly exclude test-candidate rows")
    cases = data.get("cases", [])
    if not cases or canonical_hash(cases) != data.get("dataset_sha256_canonical_cases"):
        raise ValueError("reviewed dataset cases hash mismatch")
    for split in ("dev", "independent_validation_candidate"):
        split_cases = [case for case in cases if case.get("dataset_split") == split]
        expected = data.get(f"{split}_sha256_canonical_cases")
        if not split_cases or canonical_hash(split_cases) != expected:
            raise ValueError(f"reviewed {split} cases hash mismatch")
    return data


def approved_cases_for_split(data: dict[str, Any], split: str) -> list[dict[str, Any]]:
    if split not in {"dev", "independent_validation_candidate"}:
        raise ValueError("only dev and independent_validation_candidate can be run")
    rows = [case for case in data["cases"]
            if case.get("dataset_split") == split and case.get("review_status") == "APPROVED"]
    if not rows:
        raise ValueError(f"no APPROVED cases in split {split}")
    missing = [case["case_id"] for case in rows
               if not all(case.get(key) for key in ("reviewer", "reviewed_at", "review_rationale"))]
    if missing:
        raise ValueError(f"APPROVED cases lack real review metadata: {missing[:5]}")
    return rows


def validate_validation_lock(split: str, *, locked_commit: str | None,
                             expected_configuration_sha256: str | None,
                             confirm_locked_router: bool) -> tuple[str, str]:
    commit = current_commit()
    config_sha = configuration_sha256()
    if split == "independent_validation_candidate":
        if not confirm_locked_router or not locked_commit or not expected_configuration_sha256:
            raise ValueError("validation requires explicit lock confirmation, commit, and configuration SHA-256")
        if locked_commit != commit:
            raise ValueError("locked commit must equal current HEAD")
        if expected_configuration_sha256 != config_sha:
            raise ValueError("locked configuration SHA-256 does not match current deterministic Router")
        dirty = subprocess.run(["git", "status", "--porcelain", "--", "app/m1/routing.py"],
                               cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
        if dirty:
            raise ValueError("Router source has uncommitted changes; validation is not locked")
    return commit, config_sha


def collect_predictions(dataset_path: Path, split: str, output: Path, *,
                        locked_commit: str | None = None,
                        expected_configuration_sha256: str | None = None,
                        confirm_locked_router: bool = False) -> dict[str, Any]:
    dataset_path = dataset_path.resolve()
    output = output.resolve()
    if "test_candidate" in str(output).casefold() or output.parent != RAW_DIR:
        raise ValueError("prediction output must be a new file under eval/raw and outside test-candidate paths")
    if output.exists():
        raise FileExistsError(f"refusing to overwrite prediction record: {output}")
    data = load_reviewed_dataset(dataset_path)
    cases = approved_cases_for_split(data, split)
    commit, config_sha = validate_validation_lock(
        split, locked_commit=locked_commit,
        expected_configuration_sha256=expected_configuration_sha256,
        confirm_locked_router=confirm_locked_router,
    )
    predictions = []
    for case in cases:
        decision = decide_route(case["user_query"])
        if decision.decision_source == "LLM" or decision.usage is not None:
            raise RuntimeError("deterministic prediction runner unexpectedly used a model classifier")
        predictions.append({
            "case_id": case["case_id"],
            "predicted_strategy": decision.strategy,
            "sub_strategy": decision.sub_strategy,
            "decision_source": decision.decision_source,
            "reason_code": decision.reason_code,
            "safety_decision": decision.safety_decision,
            "needs_clarification": decision.needs_clarification,
            "required_capabilities": decision.required_capabilities,
            "latency_ms": decision.latency_ms,
        })
    record = {
        "record_type": "five_way_route_predictions",
        "run_id": f"route-m2-five-way-{split}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S%fZ}",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "dataset_path": dataset_path.relative_to(ROOT).as_posix(),
        "dataset_version": data.get("dataset_version"),
        "dataset_sha256_canonical_cases": data["dataset_sha256_canonical_cases"],
        "split": split,
        "approved_sample_count": len(cases),
        "router_commit": commit,
        "router_configuration": router_configuration(),
        "configuration_sha256": config_sha,
        "classifier_enabled": False,
        "model_calls": 0,
        "test_candidate_used": False,
        "predictions": predictions,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8", newline="") as stream:
        json.dump(record, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--split", required=True, choices=("dev", "independent_validation_candidate"))
    parser.add_argument("--output", required=True)
    parser.add_argument("--locked-commit")
    parser.add_argument("--configuration-sha256")
    parser.add_argument("--confirm-locked-router", action="store_true")
    args = parser.parse_args()
    try:
        record = collect_predictions(
            Path(args.dataset), args.split, Path(args.output),
            locked_commit=args.locked_commit,
            expected_configuration_sha256=args.configuration_sha256,
            confirm_locked_router=args.confirm_locked_router,
        )
    except (ValueError, FileExistsError, json.JSONDecodeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(f"{record['run_id']} approved={record['approved_sample_count']} model_calls=0")


if __name__ == "__main__":
    main()
