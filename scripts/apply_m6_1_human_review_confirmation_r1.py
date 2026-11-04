from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/eval/m6.1-human-review-preflight-60-r1-20261028"
EVENT_ID = "M61D1-HUMAN-REVIEW-60-CHAT-SELF-CONFIRMED-20261028-R1"
CONFIRMED_AT_UTC = "2026-10-28T08:44:22Z"
CONFIRMATION_TEXT = "确认采纳 60 条预审建议"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON_OBJECT_REQUIRED:{path}")
    return value


def write(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    preflight_path = PACKAGE / "proposed-review-decisions-r1.json"
    checklist_path = PACKAGE / "user-confirmation-checklist-r1.md"
    bindings_path = PACKAGE / "source-bindings-r1.json"
    manifest_path = PACKAGE / "SHA256SUMS-r1.md"
    preflight = read(preflight_path)
    bindings = read(bindings_path)
    source_by_path = {row["path"]: row for row in bindings["sources"] if row.get("path")}
    expected_sources = {
        "eval/fidelity/m2.4-q2-human-review-r2.csv": "answers_sha256",
        "eval/review/route-m2.1-dev-validation-review-r1.csv": "routes_sha256",
    }
    source_hashes: dict[str, str] = {}
    for rel, label in expected_sources.items():
        path = ROOT / rel
        bound = source_by_path.get(rel, {}).get("sha256")
        actual = sha(path)
        if not bound or bound != actual:
            raise ValueError(f"SOURCE_CSV_BINDING_MISMATCH:{rel}")
        source_hashes[label] = actual
    if len(preflight.get("answer_pre_reviews", [])) != 8 or len(preflight.get("route_pre_reviews", [])) != 52:
        raise ValueError("CONFIRMATION_SCOPE_COUNT_MISMATCH")
    if preflight.get("human_review_fields_written") is not False:
        raise ValueError("PREFLIGHT_ALREADY_CONTAINS_HUMAN_DECISION")

    accepted_answers = []
    confirmed = copy.deepcopy(preflight)
    for original, derived in zip(preflight["answer_pre_reviews"], confirmed["answer_pre_reviews"], strict=True):
        decision = original["pre_review"]["recommendation"]
        accepted_answers.append({"item_id": original["item_id"], "case_id": original["case_id"],
            "decision": decision, "full_answer_scope": original["answer_evidence_scope"]})
        derived["derived_review_status"] = "USER_CONFIRMED"
        derived["user_confirmed_decision"] = decision
        derived["human_fields"] = {"reviewer_decision": decision,
            "reviewer": "CHAT_USER_SELF_CONFIRMED", "reviewed_at_utc": CONFIRMED_AT_UTC,
            "review_rationale": "User explicitly confirmed the scoped AI-assisted evidence pre-review recommendation; see the bound pre-review record and item rationale.",
            "status": "USER_CONFIRMED_DERIVED_DECISION"}

    accepted_routes = []
    for original, derived in zip(preflight["route_pre_reviews"], confirmed["route_pre_reviews"], strict=True):
        decision = original["pre_review_recommendation"]
        accepted_routes.append({"case_id": original["case_id"], "decision": decision,
            "confirmed_label": original["suggested_label"], "changed_fields": original["changed_fields"]})
        derived["derived_review_status"] = "USER_CONFIRMED"
        derived["user_confirmed_decision"] = decision
        derived["user_confirmed_label"] = original["suggested_label"]
        derived["human_fields"] = {"reviewer_decision": decision,
            "reviewer": "CHAT_USER_SELF_CONFIRMED", "reviewed_at_utc": CONFIRMED_AT_UTC,
            "review_rationale": "User explicitly confirmed the scoped AI-assisted route-label recommendation; see the bound pre-review record and item rationale.",
            "status": "USER_CONFIRMED_DERIVED_DECISION"}

    event = {
        "schema_version": "m6.1-user-confirmed-60-item-review-event-v1",
        "event_id": EVENT_ID,
        "status": "USER_CONFIRMATION_RECORDED",
        "confirmed_at_utc": CONFIRMED_AT_UTC,
        "timestamp_basis": "Actual current UTC clock at the time the user confirmation was received and processed.",
        "actor": {"type": "CHAT_USER_SELF_CONFIRMED", "legal_name": None,
            "independent_reviewer": False, "blind_review": False},
        "method": "AI_ASSISTED_EVIDENCE_PRE_REVIEW_EXPLICITLY_CONFIRMED_BY_HUMAN_USER",
        "confirmation": {"origin": "current_chat", "verbatim": CONFIRMATION_TEXT,
            "meaning": "Confirm all 60 row-level recommendations in the bound pre-review R1 package."},
        "scope": {"answer_disputes": 8, "route_samples": 52,
            "route_split_counts": {"dev": 28, "validation": 24},
            "excludes": ["the separately completed eight-case Smoke review (not repeated or enlarged)",
                "other dataset rows outside the two specified CSVs", "Candidate/Frozen", "Gold approval", "independent or blind review"]},
        "proposal_binding": {"pre_review_path": "docs/eval/m6.1-human-review-preflight-60-r1-20261028/proposed-review-decisions-r1.json",
            "pre_review_sha256": sha(preflight_path),
            "confirmation_checklist_path": "docs/eval/m6.1-human-review-preflight-60-r1-20261028/user-confirmation-checklist-r1.md",
            "confirmation_checklist_sha256": sha(checklist_path),
            "pre_review_sha256_manifest_path": "docs/eval/m6.1-human-review-preflight-60-r1-20261028/SHA256SUMS-r1.md",
            "pre_review_sha256_manifest_sha256": sha(manifest_path),
            "source_csv_sha256": source_hashes},
        "accepted_decision_counts": {"answers": preflight["summary"]["answer_disputes"],
            "routes": preflight["summary"]["routes"]},
        "accepted_answer_decisions": accepted_answers,
        "accepted_route_decisions": accepted_routes,
        "formal_grade": "NOT_GRADED",
        "gold_approval": False,
        "source_csvs_modified": False,
    }
    confirmed["schema_version"] = "m6.1-user-confirmed-60-item-review-decisions-v1"
    confirmed["classification"] = "USER_CONFIRMED_DERIVED_REVIEW; not independent review, not blind review, not Gold"
    confirmed["review_event"] = {"event_id": EVENT_ID, "confirmed_at_utc": CONFIRMED_AT_UTC,
        "actor_type": "CHAT_USER_SELF_CONFIRMED", "confirmation_text": CONFIRMATION_TEXT,
        "event_sha256": None}
    confirmed["human_review_fields_written"] = False
    confirmed["source_csvs_modified"] = False
    confirmed["historical_source_statuses_preserved"] = True

    event_path = PACKAGE / "confirmation-event-r1.json"
    confirmed_path = PACKAGE / "confirmed-review-decisions-r1.json"
    write(event_path, event)
    confirmed["review_event"]["event_sha256"] = sha(event_path)
    write(confirmed_path, confirmed)

    exit_matrix = {
        "schema_version": "m6.1-exit-determination-after-confirmed-60-review-r1",
        "stage_status": "M6.1_COMPLETE",
        "determined_at_utc": CONFIRMED_AT_UTC,
        "basis": "The prior R2 exit matrix passed the Smoke engineering/recompute/cleanup clause and identified only the separately indexed eight answer disputes and 52 Dev/Validation route reviews as blockers. The bound user-confirmed derived record closes exactly those 60 review obligations.",
        "requirements": [
            {"id": "smoke-engineering-execution-scoring-recompute-cleanup", "status": "PASS",
                "source": "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/exit-matrix-r2.json"},
            {"id": "eight-historical-answer-disputes", "status": "USER_CONFIRMED_REVIEW_COMPLETE",
                "count": 8, "source": "docs/eval/m6.1-human-review-preflight-60-r1-20261028/confirmed-review-decisions-r1.json"},
            {"id": "dev-validation-route-samples", "status": "USER_CONFIRMED_REVIEW_COMPLETE",
                "count": 52, "split_counts": {"dev": 28, "validation": 24},
                "source": "docs/eval/m6.1-human-review-preflight-60-r1-20261028/confirmed-review-decisions-r1.json"},
            {"id": "candidate-frozen-protection", "status": "PASS_SCOPE_PRESERVED",
                "detail": "No Candidate/Frozen content or labels accessed; this confirmation does not approve or unseal those assets."}
        ],
        "review_outcomes": {"answer_disputes": preflight["summary"]["answer_disputes"],
            "route_recommendations": preflight["summary"]["routes"]},
        "formal_gates_unchanged": {"gold": "NOT_APPROVED", "registry": "NOT_AUTHORIZED", "m2": "BLOCKED",
            "dev_smoke": "NOT_AUTHORIZED", "frozen_test": "NOT_AUTHORIZED"},
        "m6_2_started": False,
        "no_new_smoke_run": True,
        "no_provider_embedding_or_judge_calls": True,
    }
    write(PACKAGE / "m6.1-exit-determination-r1.json", exit_matrix)

    report = f"""# M6.1 confirmed review closeout — R1

Confirmation event: `{EVENT_ID}`\x20\x20
Actor type: `CHAT_USER_SELF_CONFIRMED`\x20\x20
Recorded UTC: `{CONFIRMED_AT_UTC}`\x20\x20
User text: “{CONFIRMATION_TEXT}”\x20\x20
Method: AI-assisted evidence pre-review explicitly confirmed by the user.

## Scope and result

The user confirmed the exact 60 row-level recommendations bound to the pre-review package: 8 historical answer-dispute rows and 52 route rows (dev 28, validation 24). This is not the already completed eight-case Smoke review, not review of all 40/52 dataset rows, not an independent or blind review, and not Gold approval.

Confirmed answer dispositions: PASS 4; PARTIAL 3; FAIL 0; INSUFFICIENT_EVIDENCE 1. Confirmed route dispositions: RETAIN 49; MODIFY 3. The three confirmed label changes are the multi-clause return/refund cases `-01/-02/-04` to MULTI/MULTI; the per-row records contain all other fields.

The accepted recommendations include partial and insufficient-evidence outcomes; confirmation does not turn them into PASS. The three claim-only entries remain scoped to their recorded disputed claims because the specified source material did not include their full answers.

## Audit treatment

The original review CSVs remain byte-for-byte unchanged and their rows continue to say `PENDING_HUMAN_REVIEW`; this event and the per-row derived review record are the confirmed adjudication artifact. No legal name or independent reviewer is inferred. No historical Smoke run, score, authorization, stop or report was changed.

The prior R2 exit matrix found the Smoke engineering clause met and these 60 reviews as the remaining blockers. With this confirmation recorded, the derived exit determination is `M6.1_COMPLETE`. Formal Gold remains NOT_APPROVED, Registry NOT_AUTHORIZED, M2 BLOCKED, Dev Smoke NOT_AUTHORIZED, and Frozen Test NOT_AUTHORIZED. M6.2 has not started.

No real Run, Provider, Embedding or Judge call was made. No Candidate/Frozen content was accessed.
"""
    (PACKAGE / "confirmed-review-closeout-r1.md").write_text(report, encoding="utf-8")
    print(json.dumps({"status": "RECORDED", "event_id": EVENT_ID,
        "confirmed_at_utc": CONFIRMED_AT_UTC,
        "answer_items": len(accepted_answers), "route_items": len(accepted_routes),
        "stage_status": "M6.1_COMPLETE"}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
