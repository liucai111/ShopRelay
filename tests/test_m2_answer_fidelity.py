from app.m2.answer_fidelity import validate_claim_review


AT = "2026-10-18T00:00:00Z"
EVIDENCE = [
    {"chunk_id": "return#quality", "effective_from": "2026-07-01T00:00:00Z", "effective_to": None},
    {"chunk_id": "refund#approved-return", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None},
]


def claim(cid, status="SUPPORTED", scope="REQUESTED", evidence=None, **extra):
    return {"claim_id": cid, "text": f"claim {cid}", "status": status, "scope": scope,
            "evidence_ids": ["return#quality"] if evidence is None else evidence,
            "review_status": "PENDING_HUMAN_REVIEW", **extra}


def test_claim_review_counts_statuses_and_keeps_human_gate():
    result = validate_claim_review(
        [claim("r1"), claim("r2", "UNSUPPORTED", "OUT_OF_SCOPE", [])],
        retrieved_evidence=EVIDENCE, evaluation_at=AT, required_claim_ids=["r1"],
    )
    assert result["valid"]
    assert result["status_counts"]["SUPPORTED"] == 1
    assert result["status_counts"]["UNSUPPORTED"] == 1
    assert result["required_claims_supported"] == 1
    assert result["human_review_complete"] is False


def test_citation_must_be_retrieved_and_effective_at_pinned_time():
    expired = {"chunk_id": "old#rule", "effective_from": "2025-01-01T00:00:00Z", "effective_to": "2026-01-01T00:00:00Z"}
    result = validate_claim_review(
        [claim("c1", evidence=["old#rule", "foreign#rule"])],
        retrieved_evidence=EVIDENCE + [expired], evaluation_at=AT,
    )
    assert result["valid"] is False
    assert {e["code"] for e in result["errors"]} == {
        "CITATION_NOT_EFFECTIVE_AT_EVALUATION_TIME", "CITATION_NOT_IN_RETRIEVED_EVIDENCE",
        "SUPPORTED_CLAIM_WITHOUT_VALID_EVIDENCE",
    }


def test_supported_claim_without_evidence_is_invalid_but_unsupported_is_retained():
    result = validate_claim_review(
        [claim("unsupported", "UNSUPPORTED", "OUT_OF_SCOPE", []), claim("unsupported", "SUPPORTED", evidence=[])],
        retrieved_evidence=EVIDENCE, evaluation_at=AT,
    )
    codes = [e["code"] for e in result["errors"]]
    assert "DUPLICATE_CLAIM_ID" in codes
    assert "SUPPORTED_CLAIM_WITHOUT_VALID_EVIDENCE" in codes
    assert result["claim_count"] == 2


def test_human_review_requires_identity_time_and_rationale():
    item = claim("c1", review_status="HUMAN_REVIEWED")
    result = validate_claim_review([item], retrieved_evidence=EVIDENCE, evaluation_at=AT)
    assert result["valid"] is False
    assert "INCOMPLETE_HUMAN_REVIEW_METADATA" in {e["code"] for e in result["errors"]}


def test_missing_required_claim_is_reported():
    result = validate_claim_review([], retrieved_evidence=EVIDENCE, evaluation_at=AT, required_claim_ids=["gold-rule"])
    assert result["required_claims_missing_or_not_supported"] == ["gold-rule"]
    assert "REQUIRED_CLAIM_NOT_ANNOTATED" in {e["code"] for e in result["errors"]}


def test_user_provided_fact_can_be_traced_without_fabricated_policy_citation():
    item = claim("context", source_type="USER_PROVIDED", source_ref="case query", evidence=[])
    result = validate_claim_review([item], retrieved_evidence=EVIDENCE, evaluation_at=AT)
    assert result["valid"]
    assert result["items"][0]["valid_retrieved_evidence_ids"] == []
