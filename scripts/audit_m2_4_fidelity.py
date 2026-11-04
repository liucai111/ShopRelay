"""Build a local, non-authoritative claim-level audit from an existing M2.3 run."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.m2.answer_fidelity import validate_claim_review  # noqa: E402


DEFAULT_RUN = "docs/probes/m2.3-postgres-deepseek_real-20261020T025934Z.json"
DEFAULT_DATASET = "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
DEFAULT_CANDIDATE = "eval/fidelity/m2.3-multi-real-answer-review-candidate-r1.json"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_report(run_path: Path, dataset_path: Path, candidate_path: Path) -> dict[str, Any]:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    case = next((item for item in dataset["cases"] if item["case_id"] == run["case_id"]), None)
    if case is None:
        raise ValueError("run case_id is absent from the selected reviewed dataset")
    if candidate.get("case_id") != run["case_id"]:
        raise ValueError("claim candidate case_id does not match the run")
    if candidate.get("dataset_sha256") != sha256(dataset_path):
        raise ValueError("claim candidate dataset hash does not match the reviewed dataset")
    evidence = run.get("self_rag", {}).get("evidence", [])
    retrieved_ids = {item.get("chunk_id") for item in evidence}
    review = validate_claim_review(
        candidate["claims"],
        retrieved_evidence=evidence,
        evaluation_at=run["evaluation_at"],
        required_claim_ids=candidate.get("required_claim_ids", []),
    )
    cited_ids = [item.get("chunk_id") for item in run.get("citations", [])]
    return {
        "record_type": "m2_4_answer_fidelity_offline_audit",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "PENDING_HUMAN_REVIEW" if review["valid"] else "FAIL",
        "provenance_validation": "PASS" if review["valid"] else "FAIL",
        "authority": "AUTOMATED_PROVENANCE_CHECK_PLUS_UNREVIEWED_CANDIDATE_ANNOTATION",
        "human_semantic_review": "PENDING",
        "real_model_requests": 0,
        "test_candidate_scored_or_used_for_tuning": False,
        "inputs": {
            "run_record": run_path.as_posix(),
            "run_record_sha256": sha256(run_path),
            "dataset": dataset_path.as_posix(),
            "dataset_sha256": sha256(dataset_path),
            "candidate_claim_review": candidate_path.as_posix(),
            "candidate_claim_review_sha256": sha256(candidate_path),
            "case_id": run["case_id"],
            "run_id": run["run_id"],
            "dataset_split": run["dataset_split"],
            "knowledge_version": run["knowledge_version"],
            "evaluation_at": run["evaluation_at"],
        },
        "provenance": {
            "run_status": run["run_status"],
            "route_strategy": run["route_strategy"],
            "answer_kind": run["answer_kind"],
            "run_citations": cited_ids,
            "all_run_citations_unique": len(set(cited_ids)) == len(cited_ids),
            "all_citations_present_in_retrieved_evidence": all(cid in retrieved_ids for cid in cited_ids),
            "citation_effective_time_checked": True,
            "semantic_entailment_automatically_inferred": False,
        },
        "review": review,
        "findings": [
            {
                "finding_id": "M24-FID-001",
                "severity": "HIGH",
                "status": "PENDING_HUMAN_REVIEW",
                "claim_id": "claim-04a",
                "description": "The answer generalizes the refund-settlement caveat to return applications. The cited refund clause supports only that a refund application does not itself mean settlement; no cited policy establishes the same statement for a return application.",
                "evidence": "Claim candidate claim-04a and cited chunk policies/refund@2026-01#before-shipping.",
            },
            {
                "finding_id": "M24-FID-002",
                "severity": "MEDIUM",
                "status": "PENDING_HUMAN_REVIEW",
                "claim_id": "claim-07",
                "description": "The one-open-refund and no-partial-refund clauses are supported by a retrieved citation, but appear outside the question about return/refund policy sequencing and may be unnecessary answer expansion.",
                "evidence": "Claim candidate claim-07 and cited chunk policies/refund@2026-01#uniqueness.",
            },
            {
                "finding_id": "M24-FID-003",
                "severity": "MEDIUM",
                "status": "PENDING_HUMAN_REVIEW",
                "claim_id": "claim-08",
                "description": "The statement about what the interface can verify or execute is a system-capability claim, not supported by policy citations; verify it against the API contract or remove it from the policy answer.",
                "evidence": "No source is attached to the claim in the run record.",
            },
        ],
        "interpretation": "This is a single historical case audit, not a dataset metric or a human gold judgment. The previous M2.3 fidelity gate checked lifecycle/time semantics and required citations; this M2.4 audit adds claim scope, source provenance, and relevance review. It does not retroactively rewrite the M2.3 raw run or status.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", default=DEFAULT_RUN)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--candidate", default=DEFAULT_CANDIDATE)
    parser.add_argument("--output", required=True, help="New output path; existing files are never overwritten")
    args = parser.parse_args()
    output = (ROOT / args.output).resolve()
    if output.exists():
        parser.error(f"output already exists; choose a new versioned path: {output.relative_to(ROOT)}")
    report = build_report(ROOT / args.run, ROOT / args.dataset, ROOT / args.candidate)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": report["status"], "provenance_validation": report["provenance_validation"],
                      "human_semantic_review": report["human_semantic_review"],
                      "report": output.relative_to(ROOT).as_posix(), "findings": len(report["findings"]),
                      "real_model_requests": 0}, ensure_ascii=False))
    return 0 if report["provenance_validation"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
