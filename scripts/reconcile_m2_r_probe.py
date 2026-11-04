"""Create a derived, append-only M2-R probe reconciliation without API calls."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scripts.probe_m2_r_real import ROOT, _verification_passes


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def reconcile(source: Path, output: Path) -> dict[str, Any]:
    source = source.resolve()
    output = output.resolve()
    if ROOT not in source.parents or ROOT not in output.parents:
        raise ValueError("source and output must remain inside ShopRelay")
    if output.exists():
        raise FileExistsError("reconciliation output is append-only and already exists")
    latest = json.loads(source.read_text(encoding="utf-8"))
    if latest.get("record_type") != "m2_r_real_quality_probe":
        raise ValueError("source is not an M2-R real probe record")
    verification = latest.get("verification")
    if not isinstance(verification, dict):
        raise ValueError("source lacks final verification values")

    source_records: list[dict[str, Any]] = []
    unique_requests: dict[str, dict[str, Any]] = {}
    for path in sorted((ROOT / "docs/probes").glob("m2-r-real-*.json")):
        try:
            row = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if row.get("record_type") != "m2_r_real_quality_probe":
            continue
        if (
            (row.get("dataset") or {}).get("manifest_sha256")
            != (latest.get("dataset") or {}).get("manifest_sha256")
        ):
            raise ValueError("M2-R probe records do not share the same manifest")
        source_records.append({"path": path.relative_to(ROOT).as_posix(), "sha256": _sha(path),
                               "record_status": row.get("status"),
                               "stage_request_count": (row.get("request_totals") or {}).get("stage_request_count")})
        for index, request in enumerate(row.get("model_requests") or []):
            request_id = request.get("request_id")
            key = str(request_id) if request_id else f"{path.name}:sequence:{request.get('sequence', index)}"
            prior = unique_requests.get(key)
            if prior is not None and prior != request:
                raise ValueError("conflicting duplicate provider request ID in M2-R ledger")
            unique_requests[key] = request

    request_rows = list(unique_requests.values())
    usage_rows = [row.get("usage") for row in request_rows if isinstance(row.get("usage"), dict)]
    estimated = [row.get("estimated_cost_usd") for row in request_rows
                 if isinstance(row.get("estimated_cost_usd"), (int, float))]
    run_rows = latest.get("chat_runs") or []
    reconciliation = {
        "record_type": "m2_r_probe_reconciliation.v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "source_report": {"path": source.relative_to(ROOT).as_posix(), "sha256": _sha(source),
                          "record_status_before_reconciliation": latest.get("status")},
        "source_records": source_records,
        "status_correction": {
            "field": "engineering_status",
            "source_status_logic": "FAIL",
            "corrected_status": "PASS" if _verification_passes(verification) else "FAIL",
            "reason": "test_candidate_rows_included is an exclusion invariant and must be false; all positive engineering checks are true."
        },
        "verification": verification,
        "engineering_status": "PASS" if _verification_passes(verification) else "FAIL",
        "quality_status": latest.get("quality_status", "BLOCKED"),
        "m2_formal_release": latest.get("m2_formal_release", "BLOCKED"),
        "m3_authorized": latest.get("m3_authorized", False),
        "model_ledger": {
            "unique_request_count": len(request_rows),
            "requests_with_usage": len(usage_rows),
            "requests_without_usage": len(request_rows) - len(usage_rows),
            "prompt_tokens": sum(int(row.get("prompt_tokens", 0)) for row in usage_rows),
            "completion_tokens": sum(int(row.get("completion_tokens", 0)) for row in usage_rows),
            "estimated_cost_usd_peak_rate": round(sum(float(value) for value in estimated), 12)
                if len(estimated) == len(request_rows) else None,
            "account_invoice": "NOT_AVAILABLE",
            "sdk_max_retries": 0,
            "stage_request_cap": 24,
            "stage_cost_cap_usd": 1.0,
        },
        "quality_summary": latest.get("quality_summary"),
        "selected_runs": [{key: row.get(key) for key in (
            "case_id", "dataset_split", "scoreability", "orchestrator_mode", "status", "run_status",
            "answer_kind", "policy_answer_valid", "required_evidence_coverage",
            "required_evidence_citation_coverage", "actual_strategy", "error_code")}
            for row in run_rows],
        "test_candidate_rows_included": False,
        "note": "Derived reconciliation only; raw probe records are unchanged. Judge outputs remain auxiliary and pending human review.",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps(reconciliation, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                          encoding="utf-8")
    temporary.replace(output)
    return reconciliation


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, help="completed M2-R probe JSON")
    parser.add_argument("--output", required=True, help="new reconciliation JSON; existing files are never overwritten")
    args = parser.parse_args()
    result = reconcile(ROOT / args.source, ROOT / args.output)
    print(json.dumps({"record_type": result["record_type"],
                      "engineering_status": result["engineering_status"],
                      "quality_status": result["quality_status"],
                      "requests": result["model_ledger"]["unique_request_count"],
                      "estimated_cost_usd": result["model_ledger"]["estimated_cost_usd_peak_rate"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
