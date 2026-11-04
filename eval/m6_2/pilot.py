"""Static-only preparation checks for the first non-Frozen M6.2 Dev Pilot."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from eval.m6_2.outcome_scoring import DATASET, ROOT, sha256
from app.m0.config import load_settings


PILOT_CASES = (
    "D1-PRODUCT-002", "D1-PRODUCT-003", "D1-POLICY-017",
    "D1-POLICY-018", "D1-SAFETY-036", "D1-SAFETY-037",
)
REGISTERED_READ_CAPABILITIES = {
    "search_products", "get_product_detail", "recommend_products",
    "search_knowledge", "get_order",
}
RUNTIME_SOURCES = (
    "app/m0/config.py",
    "app/m1/routing.py", "app/m1/chat_execution.py", "app/m3/agents.py",
    "app/m3/product_tools.py", "app/m3/order_tools.py", "app/m2/self_rag.py",
    "eval/m6_1/personal_dev.py", "eval/m6_1/personal_dev_runtime.py",
    "eval/m6_1/fixture_read_models.py", "eval/m6_1/personal_smoke.py",
    "eval/m6_2/outcome_scoring.py", "eval/m6_2/pilot.py",
    "scripts/run_m6_2_outcome_scoring.py", "scripts/check_m6_2_dev_pilot_static.py",
)


def _fixture_ids(fixtures: dict[str, Any]) -> set[str]:
    identifiers: set[str] = set()
    for value in fixtures.values():
        rows = value if isinstance(value, list) else [value]
        for row in rows:
            if isinstance(row, dict):
                for field in ("id", "evidence_id", "product_id", "order_id"):
                    if isinstance(row.get(field), str):
                        identifiers.add(row[field])
    return identifiers


def build_static_pilot_preflight(root: Path = ROOT) -> dict[str, Any]:
    dataset_path = root / DATASET
    dataset_sha = sha256(dataset_path)
    if dataset_sha != "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969":
        raise ValueError("PILOT_DATASET_HASH_MISMATCH")
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    cases = {row["case_id"]: row for row in dataset.get("cases", [])}
    fixture_ids = _fixture_ids(dataset.get("fixtures", {}))
    rows = []
    errors: list[str] = []
    for case_id in PILOT_CASES:
        case = cases.get(case_id)
        if not case:
            errors.append(f"CASE_MISSING:{case_id}")
            continue
        caps = sorted({row.get("tool_id") for row in case.get("input", {}).get("expected_tool_calls", [])
                       if isinstance(row, dict) and isinstance(row.get("tool_id"), str)})
        fixture_refs = case.get("input", {}).get("fixture_refs", [])
        missing = sorted(set(fixture_refs) - fixture_ids)
        unsupported = sorted(set(caps) - REGISTERED_READ_CAPABILITIES)
        if case.get("split") != "dev":
            errors.append(f"NON_DEV_SPLIT:{case_id}")
        if missing:
            errors.append(f"FIXTURE_REFERENCE_MISSING:{case_id}:{','.join(missing)}")
        if unsupported:
            errors.append(f"CAPABILITY_NOT_REGISTERED:{case_id}:{','.join(unsupported)}")
        rows.append({
            "case_id": case_id, "group_id": case.get("group_id"),
            "split": case.get("split"),
            "user_query": case.get("input", {}).get("user_query"),
            "task_category": case.get("expected_outcome", {}).get("outcome_type"),
            "expected_strategy_reference_only": case.get("input", {}).get("expected_strategy"),
            "read_capabilities": caps, "capabilities_registered_in_application": not unsupported,
            "fixture_refs": fixture_refs, "fixture_refs_present_in_dataset": not missing,
            "required_conditions": case.get("input", {}).get("required_conditions", []),
            "expected_outcome_checker_only": case.get("expected_outcome"),
            "dataset_review_status": case.get("input", {}).get("review_status"),
            "fixture_repository_readability": "NOT_TESTED; static check only confirms dataset fixture reference exists",
            "source_scope": "r3.2.4 synthetic Dev only; expected values never passed to model",
        })
    source_hashes = {}
    for relative in RUNTIME_SOURCES:
        path = root / relative
        if not path.is_file():
            errors.append(f"RUNTIME_SOURCE_MISSING:{relative}")
        else:
            source_hashes[relative] = sha256(path)
    canonical = json.dumps({"dataset_sha256": dataset_sha, "runtime_sources": source_hashes,
                            "cases": list(PILOT_CASES)}, sort_keys=True, separators=(",", ":"))
    code_binding = hashlib.sha256(canonical.encode()).hexdigest()
    settings = load_settings()
    endpoint = urlsplit(settings.base_url)
    safe_endpoint = f"{endpoint.scheme}://{endpoint.hostname or ''}{endpoint.path}"
    return {
        "schema_version": "m6.2-nonfrozen-dev-pilot-static-preflight-v1",
        "classification": "STATIC_NO_MODEL_PREFLIGHT_ONLY",
        "status": "STATIC_PREFLIGHT_PASSED" if not errors else "STATIC_PREFLIGHT_BLOCKED",
        "provider_called": False, "embedding_called": False, "judge_called": False,
        "run_created": False, "authorization_created_or_activated": False,
        "dataset": {"path": DATASET.as_posix(), "version": dataset.get("version"), "sha256": dataset_sha},
        "case_order": list(PILOT_CASES), "case_count": len(PILOT_CASES), "cases": rows,
        "code_binding": {"algorithm": "sha256(canonical JSON of explicit runtime source hashes, dataset SHA and case order)",
                         "digest": code_binding, "runtime_sources": source_hashes},
        "model_provider_binding": {"model": settings.model, "endpoint": safe_endpoint,
            "credentials_in_endpoint": bool(endpoint.username or endpoint.password),
            "binding_status": "OBSERVED_STATICALLY; RECHECK_AND_BIND_AT_AUTHORIZATION",
            "no_model_request_made": True},
        "database_identity_schema_lease_budget": "NOT_CHECKED_BY_STATIC_PREFLIGHT",
        "pilot_runner_profile": "NOT_YET_CONNECTED_TO_EXISTING_RUNNER; no real-run admission claimed",
        "runtime_admission_open_items": ["M6.2 exact six-case runner profile and checker are not wired into M6.1 runner allowlist",
            "personal SQLite identity/schema/lease/reservation must be checked by runtime admission",
            "fixture repository read must be verified in isolated SQLite before authorization",
            "case-level synthetic outcome rubrics remain PENDING_HUMAN_REVIEW and are not Gold references"],
        "errors": errors,
    }
