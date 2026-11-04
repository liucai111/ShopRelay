from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from app.m1.routing import ROUTE_VERSION, decide_route
import scripts.route_review as review
import scripts.audit_m2_closeout as audit_tool
import scripts.evaluate_route_m2_1 as route_eval


def _source_rows() -> list[dict]:
    cases = []
    for index in range(52):
        split = "dev" if index < 28 else "validation"
        cases.append({
            "case_id": f"route-reuse-{index:03d}",
            "group_id": f"g-{index // 4}",
            "dataset_split": split,
            "source": "HUMAN_REVIEWED_POLICY_CASE_ROUTE_LABEL_PENDING",
            "source_case_id": f"pr2-{index:03d}",
            "user_query": f"政策问题 {index}",
            "expected_strategy": "SINGLE",
            "expected_sub_strategy": "BM25",
            "expected_intent": "policy_question",
            "expected_safety_decision": "ALLOW",
            "expected_reason_code": "POLICY_QUESTION",
            "expected_needs_clarification": False,
            "required_capabilities": ["search_knowledge"],
            "review_status": "PENDING_REVIEW",
            "reviewer": None,
            "reviewed_at": None,
            "review_notes": None,
        })
    return cases


def _write_source_csv(path: Path) -> None:
    fields = ["case_id", "group_id", "dataset_split", "source_case_id", "user_query",
              "expected_strategy", "expected_sub_strategy", "expected_intent",
              "expected_safety_decision", "expected_reason_code", "expected_needs_clarification",
              "required_capabilities"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in _source_rows():
            copied = {key: value for key, value in row.items() if key in fields}
            copied["required_capabilities"] = json.dumps(row["required_capabilities"], separators=(",", ":"))
            writer.writerow(copied)


def test_route_review_export_import_versions_human_decisions_and_history(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "ROOT", tmp_path)
    source = tmp_path / "source.csv"
    review_csv = tmp_path / "review.csv"
    output = tmp_path / "reviewed.json"
    _write_source_csv(source)
    exported = review.export_review(source, review_csv)
    assert exported["count"] == 52
    assert exported["split_counts"] == {"dev": 28, "validation": 24}

    with review_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
        fields = review.FIELDS
    rows[0].update({"reviewer_decision": "APPROVE_AS_IS", "reviewer": "Human Reviewer",
                    "reviewed_at": "2026-10-20T10:00:00Z", "review_rationale": "Reviewed independently."})
    with review_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    result = review.import_review(source, review_csv, output)
    payload = json.loads(output.read_text(encoding="utf-8"))
    approved = next(case for case in payload["cases"] if case["case_id"] == rows[0]["case_id"])
    assert approved["review_status"] == "APPROVED"
    assert approved["reviewer"] == "Human Reviewer"
    assert approved["reviewed_at"] == "2026-10-20T10:00:00Z"
    assert len(approved["review_history"]) == 1
    assert result["decision_counts"]["APPROVE_AS_IS"] == 1
    assert payload["route_review_import"]["test_candidate_rows_modified"] == 0
    with pytest.raises(FileExistsError):
        review.import_review(source, review_csv, output)


def test_route_review_approval_requires_identity_time_and_rationale(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "ROOT", tmp_path)
    source = tmp_path / "source.csv"
    review_csv = tmp_path / "review.csv"
    _write_source_csv(source)
    review.export_review(source, review_csv)
    with review_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["reviewer_decision"] = "APPROVE_AS_IS"
    with review_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=review.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="reviewer and review_rationale"):
        review.import_review(source, review_csv, tmp_path / "bad.json")


def test_route_review_batch_confirmation_does_not_fabricate_per_case_review_time(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "ROOT", tmp_path)
    source = tmp_path / "eval" / "review" / "route-m2.1-dev-validation-review-r1.csv"
    review_csv = tmp_path / "eval" / "review" / "confirmed.csv"
    output = tmp_path / "eval" / "dataset" / "route-m2.1-dev-validation-user-confirmed.json"
    source.parent.mkdir(parents=True)
    _write_source_csv(source)
    review.export_review(source, tmp_path / "eval" / "review" / "route-m2.1-dev-validation-review-r2.csv")
    with (tmp_path / "eval" / "review" / "route-m2.1-dev-validation-review-r2.csv").open(
            "r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        row.update({"reviewer_decision": "APPROVE_AS_IS", "reviewer": "CHAT_USER_SELF_CONFIRMED",
                    "reviewed_at": "2026-10-21T03:36:40Z", "review_rationale": "User confirmed in chat."})
    with review_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=review.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    original_template = tmp_path / "eval" / "review" / "route-m2.1-dev-validation-review-r2.csv"
    import hashlib
    sha = lambda data: hashlib.sha256(data).hexdigest()
    confirmation = tmp_path / "docs" / "confirmation.json"
    confirmation.parent.mkdir()
    record = {
        "record_type": "shoprelay_chat_user_batch_review_confirmation",
        "confirmation_actor": "CHAT_USER_SELF_CONFIRMED", "identity_verification": "NOT_PERFORMED",
        "independent_blind_review": False, "test_candidate_edited": False,
        "record_written_at_utc": "2026-10-21T03:36:40Z",
        "review_time_semantics": "batch record generation time; not separate per-row manual review event timestamps",
        "output_sha256": {"ShopRelay_路由审核_用户确认版_52条.csv": sha(review_csv.read_bytes())},
        "input_original_sha256": {"route-m2.1-dev-validation-review-r2.csv": sha(original_template.read_bytes())},
        "route": {"total": 52, "approved_as_is": 52, "approved_with_correction": 0,
                  "split_counts": {"dev": 28, "validation": 24}},
    }
    confirmation.write_text(json.dumps(record), encoding="utf-8")
    result = review.import_review(source, review_csv, output, confirmation)
    payload = json.loads(output.read_text(encoding="utf-8"))
    row = payload["cases"][0]
    assert row["review_status"] == "APPROVED"
    assert row["reviewer"] == "CHAT_USER_SELF_CONFIRMED"
    assert row["reviewed_at"] is None
    assert row["review_recorded_at_utc"] == "2026-10-21T03:36:40Z"
    assert row["review_history"][0]["event_type"] == "CHAT_USER_SELF_CONFIRMED_ROUTE_IMPORT"
    assert result["decision_counts"]["APPROVE_AS_IS"] == 52
    assert payload["route_review_import"]["per_case_reviewed_at_recorded"] is False


def test_route_review_rejects_stale_base_hash_and_case_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(review, "ROOT", tmp_path)
    source = tmp_path / "source.csv"
    review_csv = tmp_path / "review.csv"
    _write_source_csv(source)
    review.export_review(source, review_csv)
    with review_csv.open("r", encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    rows[0]["base_case_sha256"] = "0" * 64
    with review_csv.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=review.FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    with pytest.raises(ValueError, match="stale base case hash"):
        review.import_review(source, review_csv, tmp_path / "stale.json")


def test_route_failure_case_now_routes_to_policy_and_live_lookup_stays_live():
    query = "原因不同的两笔整单退款，能同时保持开放状态吗？"
    route = decide_route(query)
    assert route.strategy == "SINGLE"
    assert route.reason_code == "POLICY_QUESTION"
    assert route.route_version == ROUTE_VERSION == "m2.1-route-v6"
    current = decide_route("查询订单当前退款申请状态")
    assert current.strategy in {"AGENT", "DIRECT"}
    assert current.reason_code in {"LIVE_BUSINESS_FACT", "MISSING_ORDER_IDENTIFIER"}


def test_m2_closeout_audit_is_limited_to_review_csv_sources(tmp_path, monkeypatch):
    route = tmp_path / "route.csv"
    answers = tmp_path / "answers.csv"
    _write_source_csv(route)
    with answers.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=["case_id", "review_status", "reviewer_decision"])
        writer.writeheader()
        for index in range(8):
            writer.writerow({"case_id": f"dispute-{index}", "review_status": "PENDING_HUMAN_REVIEW",
                             "reviewer_decision": ""})
    monkeypatch.setattr(audit_tool, "ROOT", tmp_path)
    monkeypatch.setattr(audit_tool, "ROUTE_SOURCE", route)
    monkeypatch.setattr(audit_tool, "ANSWER_SOURCE", answers)
    result = audit_tool.audit()
    assert result["route_candidates"]["count"] == 52
    assert result["route_candidates"]["split_counts"] == {"dev": 28, "validation": 24}
    assert result["route_candidates"]["duplicate_case_ids"] == 0
    assert result["route_candidates"]["groups_crossing_splits"] == []
    assert result["answer_disputes"]["count"] == 8
    assert result["test_candidate"]["rows_read"] == 0


def test_route_evaluator_refuses_candidate_dataset_path_before_read(tmp_path, monkeypatch):
    monkeypatch.setattr(route_eval, "ROOT", tmp_path)
    candidate = tmp_path / "eval" / "dataset" / "route-m2.1-candidate-r1.json"
    candidate.parent.mkdir(parents=True)
    candidate.write_text("not opened", encoding="utf-8")
    with pytest.raises(ValueError, match="only versioned dev/validation reviewed datasets"):
        route_eval.load_reviewed_dataset(candidate)
