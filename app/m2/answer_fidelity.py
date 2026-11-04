"""Auditable claim-level answer fidelity review primitives.

This module validates annotation structure and evidence provenance. It deliberately
does not infer whether natural-language claims are true; semantic judgments need
human review or a separately budgeted, non-authoritative judge.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping


CLAIM_STATUSES = {"SUPPORTED", "CONTRADICTED", "UNSUPPORTED", "UNCERTAIN"}
REVIEW_STATUSES = {"PENDING_HUMAN_REVIEW", "HUMAN_REVIEWED"}
CLAIM_SCOPES = {"REQUESTED", "RELEVANT_CONTEXT", "OUT_OF_SCOPE", "SYSTEM_CAPABILITY"}


def _parse_utc(value: str | datetime) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(timezone.utc)


def validate_claim_review(
    claims: Iterable[Mapping[str, Any]],
    *,
    retrieved_evidence: Iterable[Mapping[str, Any]],
    evaluation_at: str | datetime,
    required_claim_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Validate a claim review's shape, citation provenance, and coverage counts.

    ``status`` and ``relevance`` are reviewer-supplied assessments, not inferred
    by this function. Unsupported or out-of-scope claims remain visible in the
    returned report and are never silently discarded.
    """
    at = _parse_utc(evaluation_at)
    evidence_by_id: dict[str, Mapping[str, Any]] = {}
    for evidence in retrieved_evidence:
        evidence_id = str(evidence.get("chunk_id", ""))
        if evidence_id:
            evidence_by_id[evidence_id] = evidence

    required = set(required_claim_ids)
    seen: set[str] = set()
    errors: list[dict[str, str]] = []
    items: list[dict[str, Any]] = []
    statuses: Counter[str] = Counter()
    scopes: Counter[str] = Counter()
    covered_required: set[str] = set()

    for raw in claims:
        claim = dict(raw)
        claim_id = str(claim.get("claim_id", ""))
        status = str(claim.get("status", ""))
        scope = str(claim.get("scope", ""))
        review_status = str(claim.get("review_status", ""))
        if not claim_id:
            errors.append({"claim_id": "", "code": "MISSING_CLAIM_ID"})
        elif claim_id in seen:
            errors.append({"claim_id": claim_id, "code": "DUPLICATE_CLAIM_ID"})
        seen.add(claim_id)
        if status not in CLAIM_STATUSES:
            errors.append({"claim_id": claim_id, "code": "INVALID_STATUS"})
        if scope not in CLAIM_SCOPES:
            errors.append({"claim_id": claim_id, "code": "INVALID_SCOPE"})
        if review_status not in REVIEW_STATUSES:
            errors.append({"claim_id": claim_id, "code": "INVALID_REVIEW_STATUS"})
        if review_status == "HUMAN_REVIEWED" and not all(
            claim.get(field) for field in ("reviewer", "reviewed_at", "review_notes")
        ):
            errors.append({"claim_id": claim_id, "code": "INCOMPLETE_HUMAN_REVIEW_METADATA"})
        if not claim.get("text"):
            errors.append({"claim_id": claim_id, "code": "MISSING_CLAIM_TEXT"})

        citations = list(dict.fromkeys(str(v) for v in claim.get("evidence_ids", [])))
        valid_ids: list[str] = []
        for evidence_id in citations:
            evidence = evidence_by_id.get(evidence_id)
            if evidence is None:
                errors.append({"claim_id": claim_id, "code": "CITATION_NOT_IN_RETRIEVED_EVIDENCE"})
                continue
            start = evidence.get("effective_from")
            end = evidence.get("effective_to")
            try:
                effective = (start is None or _parse_utc(start) <= at) and (end is None or at < _parse_utc(end))
            except (TypeError, ValueError):
                effective = False
            if not effective:
                errors.append({"claim_id": claim_id, "code": "CITATION_NOT_EFFECTIVE_AT_EVALUATION_TIME"})
                continue
            valid_ids.append(evidence_id)
        non_policy_source = claim.get("source_type") in {
            "USER_PROVIDED", "BUSINESS_TOOL", "SYSTEM_CONTRACT", "DERIVED"
        } and bool(claim.get("source_ref"))
        if status == "SUPPORTED" and not valid_ids and not non_policy_source:
            errors.append({"claim_id": claim_id, "code": "SUPPORTED_CLAIM_WITHOUT_VALID_EVIDENCE"})
        if claim_id in required and status == "SUPPORTED":
            covered_required.add(claim_id)

        statuses[status] += 1
        scopes[scope] += 1
        items.append({
            "claim_id": claim_id,
            "status": status,
            "scope": scope,
            "review_status": review_status,
            "source_type": claim.get("source_type", "POLICY"),
            "source_ref": claim.get("source_ref"),
            "evidence_ids": citations,
            "valid_retrieved_evidence_ids": valid_ids,
            "relevance": claim.get("relevance"),
        })

    unknown_required = sorted(required - seen)
    for claim_id in unknown_required:
        errors.append({"claim_id": claim_id, "code": "REQUIRED_CLAIM_NOT_ANNOTATED"})
    unresolved = sum(1 for item in items if item["status"] == "UNCERTAIN")
    return {
        "valid": not errors,
        "evaluation_at": at.isoformat().replace("+00:00", "Z"),
        "claim_count": len(items),
        "status_counts": {status: statuses.get(status, 0) for status in sorted(CLAIM_STATUSES)},
        "scope_counts": dict(scopes),
        "required_claim_count": len(required),
        "required_claims_supported": len(covered_required),
        "required_claims_missing_or_not_supported": sorted(required - covered_required),
        "uncertain_claim_count": unresolved,
        "human_review_complete": bool(items) and all(i["review_status"] == "HUMAN_REVIEWED" for i in items),
        "items": items,
        "errors": errors,
    }
