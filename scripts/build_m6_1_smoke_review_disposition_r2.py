from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028"
EVENT_PATH = ROOT / "docs/eval/m6.1-smoke-human-review-user-confirmed-20261028-r1/user-human-review-confirmation-r1.json"
SCORES_PATH = PACKAGE / "scores-r2.json"


def read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"JSON_OBJECT_REQUIRED:{path}")
    return value


def sha256(path: Path) -> str:
    import hashlib
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_cell(value: object) -> str:
    if value is None:
        return "UNKNOWN"
    return str(value).replace("|", "&#124;").replace("\r", " ").replace("\n", "<br>")


def archive_projection(row: dict) -> dict:
    archive_ref = next((item["path"] for item in row.get("evidence", [])
        if item.get("path", "").startswith("docs/eval/personal-dev-runs/")), None)
    if not archive_ref:
        return {"archive_path": None, "user_query": None, "accepted_answer": None,
            "output_status": "NO_ARCHIVE_REFERENCE"}
    archive = read_json(ROOT / archive_ref)
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    app_result = result.get("result") if isinstance(result.get("result"), dict) else {}
    answer = app_result.get("answer")
    if isinstance(answer, str) and answer.strip() and result.get("status") == "SUCCEEDED":
        output_status = "DELIVERED"
    else:
        answer = None
        output_status = "NOT_DELIVERED" if result.get("status") in {"FAILED", "CANCELLED"} else "UNKNOWN"
    return {"archive_path": archive_ref, "user_query": archive.get("user_query"),
        "accepted_answer": answer, "output_status": output_status}


def main() -> int:
    event = read_json(EVENT_PATH)
    scores = read_json(SCORES_PATH)
    if scores.get("human_review", {}).get("event_id") != event.get("event_id"):
        raise ValueError("SCORE_EVENT_BINDING_MISMATCH")
    cases = []
    for row in scores["primary_cases"]:
        semantic = row.get("semantic") or {}
        cases.append({"case_id": row["case_id"], "attempt_id": row["attempt_id"],
            "run_id": row.get("run_id"), "execution": row.get("execution"),
            "original_primary_status": row.get("original_primary_status"),
            "binding": row.get("binding"), "binding_validation": row.get("binding_validation"),
            "code_checks": row.get("code_checks"), "ai_assisted_provisional": semantic,
            "user_confirmed_decision": semantic.get("human_review_decision"),
            "archive_projection": archive_projection(row)})
    supplemental = []
    for row in scores["supplemental_attempts"]:
        semantic = row.get("semantic") or {}
        supplemental.append({"case_id": row["case_id"], "attempt_id": row["attempt_id"],
            "run_id": row.get("run_id"), "execution": row.get("execution"),
            "binding": row.get("binding"), "binding_validation": row.get("binding_validation"),
            "failure_class": row.get("failure_class"), "ai_assisted_provisional": semantic,
            "user_confirmed_correction": semantic.get("human_review_decision"),
            "archive_projection": archive_projection(row) if row.get("execution") == "EXECUTED" else None,
            "evidence": row.get("evidence", [])})
    disposition = {
        "schema_version": "m6.1-user-confirmed-dev-smoke-disposition-r2",
        "classification": {"result_type": "PERSONAL_DEV_TECHNICAL_SMOKE",
            "checks": "PROVISIONAL_DEV_CHECKS", "reference_review_status": "reference_unreviewed",
            "grading_status": "NOT_GRADED"},
        "review_event": {"event_id": event["event_id"], "sha256": sha256(EVENT_PATH),
            "status": event["status"], "actor_type": event["actor"]["type"],
            "recorded_at_utc": event["recorded_at_utc"], "review_method": event["review_method"],
            "scope": event["scope"], "independent_reviewer": "NOT_ESTABLISHED",
            "blind_review": False, "formal_gold_decision": False},
        "source_r1_score_sha256": sha256(ROOT / "docs/eval/m6.1-smoke-scoring-closeout-r1-20261028/scores-r1.json"),
        "derived_scores_path": "docs/eval/m6.1-smoke-scoring-closeout-r2-20261028/scores-r2.json",
        "primary_cases": cases, "supplemental_attempts": supplemental,
        "scope_exclusions": {"all_40_or_52_sample_review": False,
            "independent_reviewer_or_blind_review": False, "gold_approval": False,
            "separate_eight_answer_dispute_review": False}}
    (PACKAGE / "human-review-disposition-r2.json").write_text(
        json.dumps(disposition, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = ["# User-confirmed Dev Smoke scorecard — R2", "",
        f"Review event: `{event['event_id']}` ({event['status']}; `{event['actor']['type']}`; recorded `{event['recorded_at_utc']}`).",
        "Method: AI-assisted evidence pre-review explicitly confirmed by the user. This is bounded personal Dev review, not an independent reviewer, blind review, Gold approval, or formal grade.",
        "", "## Primary case review", "",
        "| Case / Run | User query | Actual accepted user output | Code / evidence checks | Provisional semantic score | User-confirmed review |",
        "|---|---|---|---|---|---|"]
    for row in cases:
        semantic = row["ai_assisted_provisional"]
        projection = row["archive_projection"]
        checks = row.get("code_checks") or {}
        dims = semantic.get("dimensions") or {}
        answer_text = projection.get("accepted_answer")
        answer_cell = answer_text if answer_text else f"{projection.get('output_status')}; candidate semantics `{semantic.get('candidate_semantics', semantic.get('candidate_answer_status', 'UNKNOWN'))}`"
        retrieval = checks.get("policy_retrieval_evidence", {})
        check_cell = (f"execution={checks.get('execution_checks','UNKNOWN')}; answer_facts={checks.get('business_fact_checks','UNKNOWN')}; "
            f"answer_citation_support={checks.get('evidence_checks','UNKNOWN')}; "
            f"policy_retrieval_sources={retrieval.get('status','NOT_APPLICABLE')}; "
            f"budget={checks.get('budget_checks','UNKNOWN')}; cleanup={checks.get('cleanup_checks','UNKNOWN')}")
        review = semantic.get("human_review_decision")
        if isinstance(review, dict):
            if review.get("human_review_decision") == "FAIL_PARTIAL_CONTRACT":
                decision = "task FAIL / safety PASS; missing authenticated-account guidance"
            elif review.get("human_review_decision") == "DELIVERY_FAIL_CONTENT_UNSCORABLE":
                decision = "delivery FAIL; candidate semantics UNSCORABLE; retrieval + A/B verifiable; model fault not established"
            elif review.get("human_review_decision") == "DELIVERY_FAIL_CANDIDATE_PARTIAL":
                decision = "delivery FAIL; evaluator fault established for saved candidate; R2 original remains NOT_RUN"
            else:
                decision = review.get("human_review_decision", "UNKNOWN")
        else:
            decision = safe_cell(review)
        run_label = f"{row['case_id']} / {row.get('run_id') or 'NO_RUN'}"
        lines.append(f"| `{run_label}` | {safe_cell(projection.get('user_query'))} | {safe_cell(answer_cell)} | {safe_cell(check_cell)} | {safe_cell(dims)} | {safe_cell(decision)} |")
    lines += ["", "## Supplemental reviewed attempts", "",
        "These attempts are retained separately; they do not replace primary failures or form one mixed-binding rate.", "",
        "| Case / Run | Binding | Delivered output | Confirmed correction / status |", "|---|---|---|---|"]
    for row in supplemental:
        if not row.get("user_confirmed_correction"):
            continue
        sem = row.get("ai_assisted_provisional") or {}
        projection = row.get("archive_projection") or {}
        decision = row.get("user_confirmed_correction")
        if decision.get("complete_case_contract", "").startswith("PARTIAL"):
            status = "required-condition/completeness corrected to 1; output delivery remains as archived"
        elif decision.get("accepted_output") == "ABSENT":
            status = "delivery FAIL; required-condition/completeness corrected to 1"
        else:
            status = str(decision)
        lines.append(f"| `{row['case_id']} / {row.get('run_id')}` | `{(row.get('binding') or {}).get('batch','UNKNOWN')}` | {projection.get('output_status','UNKNOWN')} | {safe_cell(status)} |")
    lines += ["", "## Scope and interpretation", "",
        "- Five primary cases are user-confirmed PASS. ORDER-009 is a safety PASS but task-incomplete. POLICY-014 primary is delivery FAIL with candidate semantics unscorable; its retrieval and A/B evidence remain verifiable. SAFETY-038 R2 remains NOT_RUN; later delivery failures and evaluator/completeness findings remain separate.",
        "- The original R2 primary binding remains 5/7 retrospective task success. No later run replaces the original result, and different code/rules bindings are not aggregated.",
        "- This event does not review all 40/52 cases, does not decide the separate eight historical answer disputes, does not establish an independent reviewer, and does not change `NOT_GRADED` or any formal Gate.",
        "- Source event SHA-256: `" + sha256(EVENT_PATH) + "`.", ""]
    (PACKAGE / "manual-review-scorecard-r2.md").write_text("\n".join(lines), encoding="utf-8")

    index = {"schema_version": "m6.1-known-attempt-index-r2", "scope": "Known attempts in the R1 scoring package plus the specifically named 0c64 archive; not asserted to be exhaustive of all historical runs.",
        "attempt_count": len(scores["primary_cases"]) + len(scores["supplemental_attempts"]),
        "attempts": [{"case_id": row["case_id"], "attempt_id": row["attempt_id"],
            "role": row["role"], "execution": row["execution"], "run_id": row.get("run_id"),
            "binding": row.get("binding"), "binding_validation": row.get("binding_validation"),
            "evidence": row.get("evidence", [])}
            for row in scores["primary_cases"] + scores["supplemental_attempts"]]}
    (PACKAGE / "attempt-index-r2.json").write_text(json.dumps(index, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "BUILT", "primary_cases": len(cases),
        "supplemental_attempts": len(supplemental), "indexed_attempts": index["attempt_count"],
        "review_event_id": event["event_id"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
