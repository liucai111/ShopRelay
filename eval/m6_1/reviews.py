"""Append-only human-review task/decision records with explicit reviewer provenance."""
from __future__ import annotations

from datetime import datetime, timezone
import uuid
from typing import Any

from .store import ResultStore


class ReviewService:
    def __init__(self, store: ResultStore, *, trusted_human_reviewers: set[str] | None = None,
                 allow_synthetic_decisions: bool = False) -> None:
        self.store = store
        self.trusted_human_reviewers = trusted_human_reviewers or set()
        self.allow_synthetic_decisions = allow_synthetic_decisions

    def open_task(self, trial_id: str, reason: str, evidence_refs: list[str]) -> dict[str, Any]:
        record = {"review_id": f"review.{uuid.uuid4().hex}", "trial_id": trial_id,
                  "status": "PENDING_HUMAN_REVIEW", "reason": reason,
                  "evidence_refs": evidence_refs, "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        self.store.create_review_task(record, record["created_at_utc"])
        return record

    def decide(self, review_id: str, *, reviewer_identity: str, reviewer_type: str, decision: str, rationale: str) -> dict[str, Any]:
        if reviewer_type == "HUMAN" and not reviewer_identity.strip():
            raise ValueError("human reviewer identity is required")
        if reviewer_type == "HUMAN" and reviewer_identity not in self.trusted_human_reviewers:
            raise PermissionError("reviewer is not in the server-side reviewer allowlist")
        if reviewer_type == "SYNTHETIC_NOT_HUMAN" and reviewer_identity != "SYNTHETIC_TEST_REVIEWER":
            raise ValueError("synthetic reviewer identity must be explicit")
        if reviewer_type == "SYNTHETIC_NOT_HUMAN" and not self.allow_synthetic_decisions:
            raise PermissionError("synthetic review decisions are disabled")
        record = {"decision_id": f"review-decision.{uuid.uuid4().hex}", "review_id": review_id,
                  "reviewer_identity": reviewer_identity, "reviewer_type": reviewer_type,
                  "decision": decision, "rationale": rationale,
                  "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
        self.store.append_review_decision(record)
        return record
