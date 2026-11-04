"""Freeze only after a real, blind, next-day review CSV is complete."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {"blind_id", "dataset_sha256", "independent_required_conditions", "independent_evidence_ids",
          "independent_conclusion", "conclusion_review", "review_decision", "reviewer", "reviewed_at", "review_notes"}


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def freeze(dataset_path: Path, review_csv: Path, id_map_path: Path, output_dir: Path, *, now: datetime | None = None) -> Path:
    raw = dataset_path.read_bytes()
    dataset_sha = hashlib.sha256(raw).hexdigest()
    dataset = json.loads(raw)
    idmap = json.loads(id_map_path.read_text(encoding="utf-8"))
    if idmap.get("dataset_sha256") != dataset_sha:
        raise ValueError("blind-id map does not match reviewed dataset SHA-256")
    cases = {c["case_id"]: c for c in dataset["cases"]}
    test_cases = {cid: c for cid, c in cases.items() if c.get("dataset_split") == "test_candidate"}
    approved_times = [datetime.fromisoformat(c["reviewed_at"].replace("Z", "+00:00")) for c in cases.values() if c.get("review_status") == "APPROVED" and c.get("reviewed_at")]
    if not approved_times:
        raise ValueError("source dataset has no auditable human review timestamp")
    earliest_allowed = max(approved_times) + timedelta(hours=24)
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or now.astimezone(timezone.utc) < earliest_allowed.astimezone(timezone.utc):
        raise ValueError(f"next-day blind-review gate has not elapsed; earliest UTC: {earliest_allowed.astimezone(timezone.utc).isoformat()}")
    with review_csv.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or not FIELDS.issubset(reader.fieldnames):
            raise ValueError("blind-review CSV is missing required fields")
        rows = list(reader)
    if len(rows) != len(test_cases) or len({r["blind_id"] for r in rows}) != len(rows):
        raise ValueError("blind-review row count or blind_id uniqueness mismatch")
    by_id = {bid: (info["case_id"], info) for bid, info in idmap["blind_id_map"].items()}
    if set(r["blind_id"] for r in rows) != set(by_id):
        raise ValueError("blind-review IDs do not cover the source test_candidate exactly")
    history = []
    for row in rows:
        if row["dataset_sha256"] != dataset_sha:
            raise ValueError(f"{row['blind_id']}: dataset SHA mismatch")
        cid, meta = by_id[row["blind_id"]]
        case = test_cases.get(cid)
        if case is None or meta["case_sha256"] != hashlib.sha256(json.dumps(case, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest():
            raise ValueError(f"{row['blind_id']}: source case hash mismatch")
        if row["review_decision"] != "MATCH" or row["conclusion_review"] != "MATCH":
            raise ValueError(f"{row['blind_id']}: discrepancy or non-MATCH requires correction, not freeze")
        if not row["reviewer"].strip() or not row["review_notes"].strip():
            raise ValueError(f"{row['blind_id']}: reviewer and review notes are required")
        try:
            reviewed_at = datetime.fromisoformat(row["reviewed_at"].replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{row['blind_id']}: invalid reviewed_at") from exc
        if reviewed_at.tzinfo is None or reviewed_at.astimezone(timezone.utc) < earliest_allowed.astimezone(timezone.utc):
            raise ValueError(f"{row['blind_id']}: blind review timestamp is not at least 24 hours after first review")
        if reviewed_at.astimezone(timezone.utc) > now.astimezone(timezone.utc):
            raise ValueError(f"{row['blind_id']}: blind review timestamp is in the future")
        if not row["independent_conclusion"].strip():
            raise ValueError(f"{row['blind_id']}: independent conclusion is required")
        try:
            conditions = json.loads(row["independent_required_conditions"])
            evidence = json.loads(row["independent_evidence_ids"])
        except json.JSONDecodeError as exc:
            raise ValueError(f"{row['blind_id']}: conditions/evidence must be valid JSON") from exc
        if conditions != case.get("required_conditions") or sorted(evidence) != sorted(case.get("acceptable_evidence_ids", [])):
            raise ValueError(f"{row['blind_id']}: independent conditions/evidence differ from reviewed gold; freeze refused")
        history.append({"blind_id": row["blind_id"], "case_id": cid, "reviewer": row["reviewer"].strip(),
                        "reviewed_at": reviewed_at.isoformat(), "review_notes": row["review_notes"].strip(),
                        "independent_required_conditions": conditions, "independent_evidence_ids": evidence,
                        "independent_conclusion": row["independent_conclusion"].strip(), "decision": "MATCH"})
    result = dict(dataset)
    result["status"] = "FROZEN_TEST_CANDIDATE_AFTER_BLIND_REVIEW"
    result["blind_review"] = {"dataset_sha256_before_freeze": dataset_sha, "review_csv_sha256": digest(review_csv), "reviewed_case_count": len(history), "completed_at_utc": (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(), "history": history}
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"policy-retrieval-frozen-{dataset_sha[:12]}.json"
    if output.exists():
        raise FileExistsError(f"refusing to overwrite {output}")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--review-csv", type=Path, required=True)
    parser.add_argument("--id-map", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "eval/dataset/frozen")
    args = parser.parse_args()
    print(freeze(args.dataset, args.review_csv, args.id_map, args.output_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
