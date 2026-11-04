"""One-call diagnostic for M2.3's evidence grade node from a prior probe record.

This avoids repeating condition extraction or the full chat while diagnosing a
structured grade failure. It makes exactly one DeepSeek API request.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from app.m0.config import load_settings
from app.m1.chat import ChatFailure, DeepSeekProvider


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_record", type=Path)
    args = parser.parse_args()
    source_path = args.source_record if args.source_record.is_absolute() else ROOT / args.source_record
    source = json.loads(source_path.read_text(encoding="utf-8"))
    self_rag = source.get("self_rag") or {}
    conditions = self_rag.get("conditions") or []
    evidence = self_rag.get("evidence") or []
    if source.get("provider_mode") != "DEEPSEEK_REAL" or not conditions or not evidence:
        raise SystemExit("source record must be a real M2.3 run with extracted conditions and evidence")
    question = self_rag.get("question") or source.get("question")
    if not question:
        # The approved fixed dev sample is unambiguous; obtain the question by case ID.
        dataset = json.loads((ROOT / source["dataset_path"]).read_text(encoding="utf-8"))
        case = next(row for row in dataset["cases"] if row["case_id"] == source["case_id"])
        question = case["user_query"]
    settings = load_settings(require_api_key=True)
    provider = DeepSeekProvider(settings=settings)
    request_fingerprint = hashlib.sha256(json.dumps(
        {"question": question, "conditions": conditions, "evidence": evidence},
        ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
    result = {"record_type": "m2.3_evidence_grade_single_call_diagnostic",
              "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "BLOCKED", "source_record": source_path.relative_to(ROOT).as_posix(),
              "case_id": source.get("case_id"), "model_id": settings.model,
              "request_fingerprint_sha256": request_fingerprint,
              "condition_count": len(conditions), "evidence_count": len(evidence),
              "api_requests": 1, "secrets_recorded": False}
    try:
        response = provider.grade_evidence(question, conditions, evidence, self_rag["evaluation_at"])
        result.update({"status": "PASS", "response": response})
    except ChatFailure as exc:
        result["error"] = {"code": exc.code, "retryable": exc.retryable,
                            "diagnostic": exc.diagnostic}
    except Exception as exc:
        result["error"] = {"code": "UNEXPECTED", "exception_type": type(exc).__name__}
    target = ROOT / "docs" / "probes" / (
        "m2.3-grade-node-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "case_id": result["case_id"],
                      "condition_count": result["condition_count"], "evidence_count": result["evidence_count"],
                      "api_requests": 1, "error": result.get("error"),
                      "usage": (result.get("response") or {}).get("usage"),
                      "evidence_path": target.relative_to(ROOT).as_posix()}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
