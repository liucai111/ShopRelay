"""M6.2 exact-manifest D1 personal Dev subset execution.

This adapter deliberately reuses D1Coordinator's fixture receipt, guarded real
application lifecycle, SQLAlchemy repositories, BudgetLedger and cleanup.
It does not relax or replace the legacy exact-40 batch contract.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

from .budget import BudgetLimits, BudgetStop
from .personal_dev import (D1Coordinator, D1PreflightError, OWNER_MAP, SUPPORTED_TOOLS,
                           create_budget_ledger, implementation_digest, plan_case)

DATASET_REL = "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
DATASET_SHA256 = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
PACKAGE_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r4-20261029"
SOURCE_MATRIX_REL = "docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json"
AUTH_TYPE = "M62_PERSONAL_DEV_EXACT_SUBSET_NONSCORING"
TERMINAL_RUNS = {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()


def load_exact_profile(root: Path, *, manifest_path: Path, rules_path: Path,
                       package_rel: str | None = None, dataset_rel: str = DATASET_REL,
                       dataset_sha256: str = DATASET_SHA256,
                       matrix_rel: str = SOURCE_MATRIX_REL) -> tuple[dict, dict, dict]:
    """Verify exact case-set/order and immutable input bindings before DB use."""
    root = root.resolve(strict=True)
    package = root / (package_rel or PACKAGE_REL)
    if manifest_path.resolve(strict=True).parent != package or rules_path.resolve(strict=True).parent != package:
        raise D1PreflightError("M62_PROFILE_PATH_OUTSIDE_FIXED_PACKAGE")
    manifest, rules = _json(manifest_path), _json(rules_path)
    matrix_path = (root / manifest.get("admission_matrix_path", "")).resolve(strict=True)
    expected_matrix = (root / matrix_rel).resolve(strict=True)
    if matrix_path != expected_matrix:
        raise D1PreflightError("M62_MATRIX_PATH_OUTSIDE_APPROVED_SOURCE")
    matrix = _json(matrix_path)
    data_path = root / dataset_rel
    if (manifest["dataset"]["path"] != dataset_rel or
            _sha(data_path) != dataset_sha256 or _sha(data_path) != manifest["dataset"]["sha256"]):
        raise D1PreflightError("M62_DATASET_HASH_MISMATCH")
    if manifest.get("case_universe_count") != 40 or manifest.get("case_admission_counts") != matrix.get("admission_counts"):
        raise D1PreflightError("M62_UNIVERSE_OR_ADMISSION_COUNTS_MISMATCH")
    if manifest.get("admission_matrix_sha256") != _sha(matrix_path):
        raise D1PreflightError("M62_MATRIX_HASH_MISMATCH")
    ids = manifest.get("authorized_candidate_case_ids") or manifest.get("proposed_executable_case_ids")
    ready_ids = {c["case_id"] for c in matrix["cases"] if c["admission_status"] == "READY"}
    ordered = manifest.get("proposed_order")
    readiness_exception = manifest.get("readiness_exception")
    if manifest.get("profile_version") == "m6.2-r324-four-first-runs-r2":
        expected_exception_ids = ["D1-PRODUCT-005", "D1-AFTERSALES-019", "D1-AFTERSALES-020", "D1-CROSSAGENT-032"]
        if (not isinstance(readiness_exception, dict) or
                readiness_exception.get("case_ids") != expected_exception_ids or
                readiness_exception.get("source_status") != "OFFLINE_APPLICATION_ADMISSION_PASS_PENDING_NEW_EXECUTION_AUTH" or
                readiness_exception.get("source_path") !=
                    "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029/reports/12-case-offline-remediation-r17.json" or
                not isinstance(readiness_exception.get("source_sha256"), str) or
                len(readiness_exception["source_sha256"]) != 64):
            raise D1PreflightError("M62_R10_READINESS_EXCEPTION_BINDING_INVALID")
        ready_ids.update(expected_exception_ids)
    elif readiness_exception is not None:
        raise D1PreflightError("M62_UNEXPECTED_READINESS_EXCEPTION")
    if (not isinstance(ids, list) or not ids or len(ids) != len(set(ids)) or not set(ids).issubset(ready_ids) or
            not isinstance(ordered, list) or len(ordered) != len(ids) or set(ordered) != set(ids)):
        raise D1PreflightError("M62_CASE_SET_OR_ORDER_MISMATCH")
    if rules.get("case_order") != ordered or rules.get("source_matrix_case_ids_sha256") != hashlib.sha256(
            "\n".join(c["case_id"] for c in matrix["cases"]).encode()).hexdigest():
        raise D1PreflightError("M62_LOCKED_RULES_SCOPE_MISMATCH")
    if manifest.get("locked_rules_sha256") != _sha(rules_path):
        raise D1PreflightError("M62_LOCKED_RULES_HASH_MISMATCH")
    if manifest.get("repository_audit", {}).get("sha256") != _sha(root / manifest["repository_audit"]["path"]):
        raise D1PreflightError("M62_REPOSITORY_AUDIT_HASH_MISMATCH")
    if manifest.get("bindings", {}).get("implementation_digest") != implementation_digest(root):
        raise D1PreflightError("M62_IMPLEMENTATION_BINDING_MISMATCH")
    return manifest, rules, matrix


def derive_limits(case_count: int, *, per_case_overrides: dict | None = None,
                  batch_overrides: dict | None = None) -> tuple[BudgetLimits, BudgetLimits]:
    if isinstance(case_count, bool) or not isinstance(case_count, int) or not 1 <= case_count <= 31:
        raise D1PreflightError("M62_CASE_COUNT_OUT_OF_RANGE")
    # The legacy BudgetLimits is used as the durable experiment envelope;
    # PerCaseBudgetLedger below enforces the independent caps.
    per_case_values = dict(provider_calls=6, embedding_calls=0, judge_calls=0,
        total_tokens=120000, estimated_usd=0.18, tool_calls=8,
        max_calls_per_trial=8, max_concurrent_trials=1, max_duration_seconds=180, max_retries=0)
    per_case_values.update(per_case_overrides or {})
    per_case = BudgetLimits(**per_case_values)
    batch_values = dict(provider_calls=per_case.provider_calls*case_count,
        embedding_calls=per_case.embedding_calls*case_count, judge_calls=per_case.judge_calls*case_count,
        total_tokens=per_case.total_tokens*case_count,
        estimated_usd=per_case.estimated_usd*case_count,
        tool_calls=per_case.tool_calls*case_count,
        max_calls_per_trial=per_case.max_calls_per_trial*case_count,
        max_concurrent_trials=1,
        max_duration_seconds=min(7200,per_case.max_duration_seconds*case_count),
        max_retries=per_case.max_retries)
    batch_values.update(batch_overrides or {})
    batch = BudgetLimits(**batch_values)
    return per_case, batch


def plan_without_unauthorized_targets(plan: dict) -> dict:
    """Create the minimal fixture plan for a declared zero-business-read case."""
    case = plan["case"]
    if case["input"].get("expected_tool_calls"):
        raise D1PreflightError("M62_ZERO_READ_PLAN_HAS_DECLARED_TOOLS")
    alias = case["input"].get("user_id")
    if alias not in OWNER_MAP:
        raise D1PreflightError("M62_ZERO_READ_OWNER_ALIAS_INVALID")
    safe = dict(plan)
    safe.update({"owner_id": OWNER_MAP[alias], "orders": [], "order_ids": [], "facts": [],
        "bundle": {"fixtures": {"users": plan["bundle"].get("fixtures", {}).get("users", []),
            "orders_and_snapshots": [], "shipments": []}}})
    return safe


def app_admission(coordinator: D1Coordinator, plan: dict, *, per_case_reads: int = 8) -> dict:
    """Load exact fixture and probe each declared read via real app repositories.

    Parameters are sourced from the user input or prior allowed repository
    results. No expected answer is used to synthesize a tool argument.
    """
    case = plan["case"]
    declared = case["input"].get("expected_tool_calls", [])
    allowed = [x.get("tool_id") for x in declared if x.get("mode") == "read_only"]
    if len(allowed) != len(set(allowed)) or not set(allowed).issubset(SUPPORTED_TOOLS):
        raise D1PreflightError("M62_CASE_TOOL_CAPABILITY_INVALID")
    if len(allowed) > per_case_reads:
        raise D1PreflightError("M62_PREFLIGHT_READ_CAP_EXCEEDED")
    if not allowed:
        # A refusal case is run with only the authenticated actor in its plan;
        # the named target order/owner Fixture is intentionally stripped and
        # no receipt or repository is created during preflight.
        case_input = plan["case"]["input"]
        alias = case_input.get("user_id")
        if alias not in OWNER_MAP:
            raise D1PreflightError("M62_REFUSAL_OWNER_ALIAS_INVALID")
        return {"status": "NO_BUSINESS_READS_REQUIRED", "case_id": case["case_id"],
            "owner_id": OWNER_MAP[alias], "allowed_capabilities": [], "repository_reads": [],
            "read_count": 0, "fixture_receipt": None, "receipt_cleanup": "NOT_APPLICABLE_NO_TARGET_FIXTURE"}
    coupon_code = None
    if "get_coupon" in allowed:
        # The repository adapter below may use a different schema if the
        # legacy coupon table is unavailable; it still performs an exact,
        # owner-free lookup of a user-provided code only.
        from app.m3.policy_tools import coupon_code_from_query
        coupon_code = coupon_code_from_query(plan["message"])
        if coupon_code is None:
            raise D1PreflightError("M62_COUPON_CODE_NOT_DERIVABLE_FROM_USER_INPUT")
    receipt = coordinator.prepare(plan)
    reads_done: list[dict] = []
    cleanup_result = {"cleanup_status": "NOT_ATTEMPTED", "lease_released": False, "residual_case_rows": "UNKNOWN"}
    blocked_reason = None
    try:
        repos = coordinator.repositories(plan, receipt, maximum=per_case_reads)
        query = plan["message"]
        order_ids = list(plan.get("order_ids", []))
        if "search_knowledge" in allowed:
            result = repos["policy"].search(query, mode="BM25", result_ref=f"m62-preflight:{case['case_id']}")
            reads_done.append({"tool": "search_knowledge", "source": "real_fixture_retrieval", "refs": [r.get("chunk_id") for r in result.get("evidence", [])]})
        if "get_order" in allowed:
            for order_id in order_ids:
                result = repos["order"].get_order(order_id, plan["owner_id"])
                reads_done.append({"tool": "get_order", "source": result.get("source"), "order_id": order_id, "owner_validated": True})
        order_items_by_order: dict[str, list[dict]] = {}
        if "get_order_items" in allowed:
            for order_id in order_ids:
                result = repos["order"].get_order_items(order_id, plan["owner_id"])
                items = result.get("items", [])
                order_items_by_order[order_id] = items if isinstance(items, list) else []
                reads_done.append({"tool": "get_order_items", "source": result.get("source"), "order_id": order_id,
                    "count": len(order_items_by_order[order_id]),
                    "product_ids": list(dict.fromkeys(item.get("product_id") for item in order_items_by_order[order_id]
                        if isinstance(item.get("product_id"), str) and item["product_id"].strip()))})
        if "track_shipment" in allowed:
            for order_id in order_ids:
                result = repos["order"].track_shipment(order_id, plan["owner_id"])
                reads_done.append({"tool": "track_shipment", "source": result.get("source"), "order_id": order_id, "availability": result.get("availability")})
        if "get_refund_status" in allowed:
            for order_id in order_ids:
                result = repos["after_sales"].get_refund_status(order_id, plan["owner_id"])
                reads_done.append({"tool": "get_refund_status", "source": result.get("source"), "order_id": order_id, "availability": result.get("availability")})
        # Product IDs may come from an explicit user identifier, the audited
        # exact-name resolver, an allowed search_products result, or an allowed
        # upstream get_order_items result. Never infer them from expected
        # outcomes or fixture references, and never call search_products unless
        # this case explicitly allows it.
        product_ids: list[str] = []
        product_filters = None
        product_search_result = None
        if {"get_product_detail", "check_inventory"}.intersection(allowed):
            product_ids = list(repos["product"].resolve_product_references(query) or [])
        if "get_product_detail" in allowed:
            if not product_ids:
                from app.m3.product_tools import parse_product_request_many
                explicit, filters, _stock = parse_product_request_many(query)
                product_ids = list(explicit)
                product_filters = filters
                if not product_ids and "get_order_items" in allowed:
                    upstream_ids = list(dict.fromkeys(
                        item.get("product_id")
                        for items in order_items_by_order.values()
                        for item in items
                        if isinstance(item, dict) and isinstance(item.get("product_id"), str)
                        and item["product_id"].strip()))
                    if len(upstream_ids) == 1:
                        product_ids = upstream_ids
                if not product_ids and "search_products" in allowed:
                    product_filters = repos["product"].resolve_product_filters(query, product_filters)
                    product_search_result = repos["product"].search_products(product_filters)
                    product_ids = [x["product_id"] for x in product_search_result.get("products", [])]
                    reads_done.append({"tool": "search_products", "source": product_search_result.get("source"),
                        "candidate_count": len(product_ids), "truncated": product_search_result.get("truncated")})
                if not product_ids:
                    raise D1PreflightError("PRODUCT_ID_NOT_DERIVABLE_WITHOUT_ALLOWED_SEARCH")
            for product_id in dict.fromkeys(product_ids):
                result = repos["product"].get_product_detail(product_id)
                reads_done.append({"tool": "get_product_detail", "source": result.get("source"), "product_id": product_id,
                    "availability": result.get("availability")})
        if "search_products" in allowed:
            from app.m3.product_tools import parse_product_request_many
            _explicit, filters, _stock = parse_product_request_many(query)
            filters = repos["product"].resolve_product_filters(query, filters)
            result = product_search_result or repos["product"].search_products(filters)
            # Reuse a search already performed to resolve a downstream detail
            # or inventory call. Do not double-count or reread the same page.
            if not any(row.get("tool") == "search_products" for row in reads_done):
                reads_done.append({"tool": "search_products", "source": result.get("source"),
                    "candidate_count": len(result.get("products", [])), "truncated": result.get("truncated")})
            if not product_ids:
                product_ids = [x["product_id"] for x in result.get("products", []) if x.get("product_id")]
        if "check_inventory" in allowed:
            ids = product_ids
            if not ids and "get_order_items" in allowed:
                ids = list(dict.fromkeys(item.get("product_id") for items in order_items_by_order.values()
                    for item in items if isinstance(item, dict) and isinstance(item.get("product_id"), str)))
            if not ids:
                raise D1PreflightError("PRODUCT_ID_NOT_DERIVABLE_FROM_USER_OR_ALLOWED_UPSTREAM")
            for product_id in dict.fromkeys(ids):
                result = repos["product"].check_inventory(product_id)
                reads_done.append({"tool": "check_inventory", "source": result.get("source"), "product_id": product_id,
                    "availability": result.get("availability"), "error_code": result.get("error_code")})
        if "get_coupon" in allowed:
            # Never infer coupon code. Only accept a code literally provided by user.
            result = repos["coupon"].get_coupon(coupon_code)
            reads_done.append({"tool": "get_coupon", "availability": result.get("availability"), "code_source": "USER_QUERY"})
        if "get_warranty" in allowed:
            # Use only a product ID explicitly stated or returned by an allowed
            # get_order_items result in this same receipt.
            product_id = None
            if "get_order_items" in allowed:
                for order_id, items in order_items_by_order.items():
                    if len(items) == 1:
                        product_id = items[0].get("product_id")
                        if not any(row.get("tool") == "get_order_items" and row.get("order_id") == order_id
                                   for row in reads_done):
                            reads_done.append({"tool": "get_order_items", "source": "business_database", "order_id": order_id, "product_id": product_id, "owner_validated": True})
            if not product_id:
                raise D1PreflightError("M62_WARRANTY_PRODUCT_ID_NOT_DERIVABLE")
            result = repos["policy"].search(f"商品保修政策 {product_id}", mode="BM25", result_ref=f"m62-preflight-warranty:{case['case_id']}")
            reads_done.append({"tool": "get_warranty", "source": "shoprelay_warranty_policy", "product_id": product_id,
                "policy_refs": [r.get("chunk_id") for r in result.get("evidence", [])]})
        if len(reads_done) > per_case_reads:
            raise D1PreflightError("M62_PREFLIGHT_READ_CAP_EXCEEDED")
        preflight_status = "APPLICATION_REPOSITORY_PREFLIGHT_PASS"
    except D1PreflightError as exc:
        if str(exc) in {"PRODUCT_ID_NOT_DERIVABLE_WITHOUT_ALLOWED_SEARCH",
                        "PRODUCT_ID_NOT_DERIVABLE_FROM_USER_OR_ALLOWED_UPSTREAM"}:
            preflight_status = "APPLICATION_ADMISSION_BLOCKED_PARAMETER_NOT_DERIVABLE"
            blocked_reason = str(exc)
        else:
            preflight_status = "APPLICATION_REPOSITORY_PREFLIGHT_FAIL"
            raise
    except Exception:
        preflight_status = "APPLICATION_REPOSITORY_PREFLIGHT_FAIL"
        raise
    finally:
        # Cleanup is idempotent only for a live lease; execute once and let any
        # mismatch propagate rather than silently treating it as success.
        with coordinator.factory() as db:
            coordinator.target.check_session(db)
            marker = db.execute(__import__("sqlalchemy").text(
                "SELECT lease_id FROM d1_personal_target")).scalar_one()
        if marker == receipt.lease_id:
            coordinator.cleanup(receipt)
        cleanup_result = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        if cleanup_result["cleanup_status"] != "CLEANED_AND_RELEASED" or cleanup_result["residual_case_rows"] != 0:
            raise D1PreflightError("M62_PREFLIGHT_RECEIPT_CLEANUP_NOT_EXACT")
    return {"status": preflight_status, "case_id": case["case_id"],
        "owner_id": plan["owner_id"], "allowed_capabilities": allowed, "repository_reads": reads_done,
        "read_count": len(reads_done), "fixture_receipt": receipt, "receipt_cleanup": cleanup_result,
        "blocked_reason": blocked_reason}


def score_archive(case: dict, archive: dict, *, budget_snapshot: dict, cleanup_status: str) -> dict:
    """Evidence-derived technical score; semantics stay explicitly reviewable."""
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    obs = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    runs = obs.get("runs") if isinstance(obs.get("runs"), list) else []
    events = obs.get("run_events") if isinstance(obs.get("run_events"), list) else []
    traces = obs.get("traces") if isinstance(obs.get("traces"), list) else []
    messages = obs.get("messages") if isinstance(obs.get("messages"), list) else []
    run_id = result.get("run_id")
    terminal = result.get("status") in TERMINAL_RUNS and len(runs) == 1 and runs[0].get("run_id") == run_id
    event_run_ids = {e.get("run_id") for e in events if e.get("run_id")}
    trace_run_ids = {t.get("run_id") for t in traces if t.get("run_id")}
    association = bool(run_id) and bool(events) and bool(traces) and event_run_ids.issubset({run_id}) and trace_run_ids.issubset({run_id})
    case_input = case.get("input", {})
    allow = {r.get("tool_id") for r in case_input.get("expected_tool_calls", []) if r.get("mode") == "read_only"}
    tool_events = []
    policy_events: dict[str, list[dict]] = {"started": [], "finished": []}
    for event in events:
        if event.get("event_type") == "policy_retrieval_started":
            policy_events["started"].append(event)
        elif event.get("event_type") == "policy_retrieval_finished":
            policy_events["finished"].append(event)
        if event.get("event_type") not in {"tool_started", "tool_finished"}:
            continue
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            payload = {}
        tool_events.append({"event_type": event.get("event_type"),
            "tool_id": payload.get("tool_id") or payload.get("tool") or payload.get("capability"),
            "payload_available": bool(payload), "tool_call_id": payload.get("tool_call_id"),
            "result_ref": payload.get("result_ref"), "run_id": payload.get("run_id"),
            "trace_id": payload.get("trace_id"), "owner_scope": payload.get("owner_scope"),
            "status": payload.get("status")})
    observed = {x["tool_id"] for x in tool_events if x.get("tool_id")}
    policy_calls: dict[str, dict[str, dict]] = {}
    for phase, rows in policy_events.items():
        for event in rows:
            try:
                payload = json.loads(event.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and call_id:
                policy_calls.setdefault(call_id, {})[phase] = payload
    completed_policy_calls = set()
    for call_id, phases in policy_calls.items():
        start, finish = phases.get("started"), phases.get("finished")
        if (start and finish and start.get("run_id") == run_id and finish.get("run_id") == run_id and
                start.get("trace_id") and start.get("trace_id") == finish.get("trace_id") and
                start.get("result_ref") == finish.get("result_ref") and
                finish.get("status") == "SUCCEEDED" and finish.get("evidence_ids")):
            completed_policy_calls.add(call_id)
    route = (result.get("result") or {}).get("route") if isinstance(result.get("result"), dict) else None
    if not isinstance(route, dict) and runs:
        try:
            persisted = json.loads(runs[0].get("result_json") or "{}")
            route = persisted.get("route") if isinstance(persisted, dict) else None
        except (TypeError, json.JSONDecodeError):
            route = None
    route = route if isinstance(route, dict) else {}
    route_source = route.get("decision_source")
    fallback_error = route.get("fallback_error")
    provider_execution = ("FAIL" if result.get("status") == "MODEL_UNAVAILABLE" or
        route_source == "FALLBACK" or fallback_error else
        "PASS" if route_source == "LLM" else "NOT_APPLICABLE" if route_source == "RULE" else "UNKNOWN")
    route_caps = route.get("required_capabilities", [])
    route_scope = isinstance(route_caps, list) and set(route_caps).issubset(SUPPORTED_TOOLS) and set(route_caps).issubset(allow)
    complete_event_snapshot = bool(events) and bool(route) and association
    route_decision_persisted = any(e.get("event_type") == "route_decided" for e in events)
    final_answer = next((m.get("content") for m in reversed(messages) if m.get("role") == "assistant" and isinstance(m.get("content"), str)), None)
    result_payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    audit_rows = result_payload.get("tool_call_audit") if isinstance(result_payload.get("tool_call_audit"), list) else None
    tool_refs = result_payload.get("tool_references") if isinstance(result_payload.get("tool_references"), list) else []
    finished_tool_events = [row for row in tool_events if row.get("event_type") == "tool_finished"]
    if tool_events:
        references_by_pair = {(row.get("tool"), row.get("result_ref")) for row in tool_refs
            if isinstance(row, dict) and isinstance(row.get("tool"), str) and isinstance(row.get("result_ref"), str)}
        audit_by_id = {row.get("tool_call_id"): row for row in audit_rows or []
            if isinstance(row, dict) and isinstance(row.get("tool_call_id"), str)}
        event_ids = {row.get("tool_call_id") for row in finished_tool_events if row.get("tool_call_id")}
        audit_linked = (audit_rows is not None and len(event_ids) == len(finished_tool_events) and
            event_ids == set(audit_by_id) and all(
                audit_by_id[call_id].get("run_id") == run_id and
                audit_by_id[call_id].get("trace_id") == result_payload.get("trace_id") and
                audit_by_id[call_id].get("result_ref") == next((event.get("result_ref") for event in finished_tool_events
                    if event.get("tool_call_id") == call_id), None) and
                (audit_by_id[call_id].get("tool"), audit_by_id[call_id].get("result_ref")) in references_by_pair
                for call_id in event_ids))
    elif completed_policy_calls:
        audit_linked = complete_event_snapshot
    else:
        audit_linked = (audit_rows is not None and not audit_rows and complete_event_snapshot and
            route_decision_persisted and route.get("strategy") in {"DIRECT", "REFUSE"} and
            not policy_events["started"] and not policy_events["finished"])
    owner_tools = {"get_order", "get_order_items", "track_shipment", "get_refund_status"}
    owner_audits = [row for row in audit_rows or [] if isinstance(row, dict) and row.get("tool") in owner_tools]
    owner_scope_ok = all(row.get("owner_scope") == "AUTHENTICATED_OWNER_FILTER" for row in owner_audits)
    owner_scope_status = "PASS" if owner_audits and owner_scope_ok else "FAIL" if owner_audits else "NOT_APPLICABLE"
    budget_clean = (budget_snapshot.get("complete") is True and budget_snapshot.get("settled") is True and
                    budget_snapshot.get("active_reservations") == 0 and
                    not budget_snapshot.get("unknown_tokens", True) and not budget_snapshot.get("unknown_cost", True) and
                    cleanup_status == "RECEIPT_CLEANED_AND_LEASE_RELEASED")
    expected_policy = "search_knowledge" in allow
    expected_business = allow - {"search_knowledge"}
    missing_business = sorted(expected_business - observed)
    if tool_events:
        expected_tool_audit = complete_event_snapshot and all(e.get("tool_id") for e in tool_events)
        tool_audit_reason = "TOOL_EVENTS_PRESENT_AND_IDENTIFIED" if expected_tool_audit else "TOOL_EVENT_PAYLOAD_MISSING"
    else:
        # A linked policy-only invocation is auditable even when the policy
        # adapter records its typed call events instead of generic business
        # tool events. Otherwise zero calls pass only for an explicit,
        # persisted DIRECT/REFUSE route; all([]) is never treated as proof.
        expected_tool_audit = bool(completed_policy_calls) and complete_event_snapshot
        if expected_tool_audit:
            tool_audit_reason = "POLICY_RETRIEVAL_ONLY_WITH_LINKED_EVENTS"
        else:
            expected_tool_audit = complete_event_snapshot and route_decision_persisted and route.get("strategy") in {
                "DIRECT", "REFUSE"} and not policy_events["started"] and not policy_events["finished"]
            tool_audit_reason = ("EXPLICIT_ZERO_CALL_ROUTE_WITH_COMPLETE_EVENT_SNAPSHOT"
                if expected_tool_audit else "NO_CALL_SNAPSHOT_UNPROVEN")
    policy_evidence = ("PASS" if completed_policy_calls else "FAIL" if expected_policy and complete_event_snapshot else
                       "UNKNOWN" if expected_policy else "NOT_APPLICABLE")
    checks = {
        "run_terminal": "PASS" if terminal else "FAIL",
        "run_trace_association": "PASS" if association else "UNKNOWN",
        "route_within_allowlist": "PASS" if route and route_scope else "FAIL" if route else "UNKNOWN",
        "tool_scope": "PASS" if observed.issubset(allow) else "FAIL",
        "tool_audit_completeness": "PASS" if expected_tool_audit else "UNKNOWN",
        "tool_result_audit_linkage": "PASS" if audit_linked else "UNKNOWN",
        "owner_scope_evidence": owner_scope_status,
        "expected_business_tool_execution": "FAIL" if missing_business else "PASS" if expected_business else "NOT_APPLICABLE",
        "policy_retrieval_audit": policy_evidence,
        "user_visible_answer_present": "PASS" if final_answer and final_answer.strip() else "FAIL",
        "provider_execution": provider_execution,
        "budget_and_cleanup": "PASS" if budget_clean else "UNKNOWN",
    }
    # Run outcome and semantic answer quality are deliberately distinct.
    required_checks = ["run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
        "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup"]
    if owner_audits:
        required_checks.append("owner_scope_evidence")
    if expected_policy:
        required_checks.append("policy_retrieval_audit")
    critical = ("run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
        "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup")
    if owner_audits:
        critical += ("owner_scope_evidence",)
    if checks["user_visible_answer_present"] == "FAIL":
        task_outcome = "FAIL"
    elif provider_execution in {"FAIL", "UNKNOWN"}:
        task_outcome = "UNKNOWN"
    elif any(checks[x] != "PASS" for x in critical):
        task_outcome = "UNKNOWN"
    elif missing_business:
        task_outcome = "FAIL"
    elif expected_policy and checks["policy_retrieval_audit"] == "FAIL":
        task_outcome = "FAIL"
    elif all(checks[x] == "PASS" for x in required_checks):
        task_outcome = "UNSCORED_PENDING_AI_REVIEW"
    else:
        task_outcome = "UNKNOWN"
    return {"schema_version": "m6.2-r324-code-grader-v1", "case_id": case["case_id"], "run_id": run_id,
        "program_checks": checks, "tool_audit_reason": tool_audit_reason,
        "missing_expected_business_tools": missing_business, "task_outcome": task_outcome,
        "observed_tools": sorted(observed), "allowed_tools": sorted(allow),
        "tool_audit_count": len(audit_rows) if audit_rows is not None else None,
        "policy_retrieval_call_ids": sorted(completed_policy_calls),
        "assistant_answer": final_answer, "agent_started": any(e.get("event_type") == "agent_started" for e in events),
        "semantic_review": "REQUIRED" if final_answer else "UNSCORABLE_NO_DELIVERED_ANSWER",
        "grading_status": "PROVISIONAL_DEV_CHECKS_NOT_GRADED"}


def batch_stop_reason(grader: dict, allowed_capabilities: list[str]) -> str | None:
    """Separate technical/system stops from safe task-quality failures."""
    checks = grader.get("program_checks") if isinstance(grader.get("program_checks"), dict) else {}
    required = ("run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
        "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup",
        "user_visible_answer_present")
    if any(checks.get(name) != "PASS" for name in required):
        return "CRITICAL_EXECUTION_EVIDENCE_OR_DELIVERY_FAILURE"
    if checks.get("provider_execution") in {"FAIL", "UNKNOWN"}:
        return "PROVIDER_OR_PUBLIC_CONTRACT_FAILURE"
    owner_tools = {"get_order", "get_order_items", "track_shipment", "get_refund_status"}
    if owner_tools.intersection(allowed_capabilities) and checks.get("owner_scope_evidence") != "PASS":
        return "OWNER_SCOPE_EVIDENCE_FAILURE"
    if "search_knowledge" in allowed_capabilities and checks.get("policy_retrieval_audit") != "PASS":
        return "POLICY_RETRIEVAL_AUDIT_FAILURE"
    return None


def _warranty_composite_result_is_linked(archive: dict) -> bool:
    """Validate get_warranty's typed projection to its underlying policy result.

    The warranty capability is a composed read: its single audited call points
    at the policy retrieval result, while the user-facing projection publishes
    both a warranty wrapper reference and the nested search_knowledge reference.
    Accept that shape only when the same-run event/audit, both typed references,
    order-line product, and returned policy metadata all agree.
    """
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    events = observations.get("run_events") if isinstance(observations.get("run_events"), list) else []
    run_id = result.get("run_id")
    trace_id = payload.get("trace_id")
    audits = payload.get("tool_call_audit") if isinstance(payload.get("tool_call_audit"), list) else []
    refs = payload.get("tool_references") if isinstance(payload.get("tool_references"), list) else []
    if not run_id or not trace_id or not audits or not refs:
        return False

    finished: dict[str, dict] = {}
    for event in events:
        if event.get("event_type") != "tool_finished":
            continue
        try:
            event_payload = json.loads(event.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            return False
        call_id = event_payload.get("tool_call_id")
        if not isinstance(call_id, str) or not call_id or call_id in finished:
            return False
        finished[call_id] = event_payload
    audit_by_id = {row.get("tool_call_id"): row for row in audits
                   if isinstance(row, dict) and isinstance(row.get("tool_call_id"), str)}
    if len(audit_by_id) != len(audits) or set(audit_by_id) != set(finished):
        return False

    reference_pairs = {(row.get("tool"), row.get("source"), row.get("result_ref"))
                       for row in refs if isinstance(row, dict)}
    warranty_audits = []
    for call_id, event in finished.items():
        audit = audit_by_id[call_id]
        if (event.get("run_id") != run_id or event.get("trace_id") != trace_id or
                audit.get("run_id") != run_id or audit.get("trace_id") != trace_id or
                event.get("result_ref") != audit.get("result_ref") or
                audit.get("executed") is not True or audit.get("status") != "COMPLETED"):
            return False
        tool = audit.get("tool")
        ref = audit.get("result_ref")
        if tool == "get_warranty":
            warranty_audits.append(audit)
            # get_warranty audits its internal policy read; its returned result
            # has a distinct typed warranty projection plus the same retrieval ref.
            if ("search_knowledge", "shoprelay_policy_retrieval", ref) not in reference_pairs:
                return False
        elif tool == "search_knowledge":
            if (tool, "shoprelay_policy_retrieval", ref) not in reference_pairs:
                return False
        elif (tool, "business_database", ref) not in reference_pairs:
            return False
    if len(warranty_audits) != 1:
        return False

    business = payload.get("business_results") if isinstance(payload.get("business_results"), dict) else {}
    order = business.get("order_logistics") if isinstance(business.get("order_logistics"), dict) else {}
    items_result = order.get("get_order_items") if isinstance(order.get("get_order_items"), dict) else {}
    items = items_result.get("items") if isinstance(items_result.get("items"), list) else []
    product_ids = {item.get("product_id") for item in items if isinstance(item, dict)
                   and isinstance(item.get("product_id"), str)}
    policy = business.get("policy") if isinstance(business.get("policy"), dict) else {}
    product_id = policy.get("product_id")
    if not product_id or product_ids != {product_id}:
        return False
    warranty_ref = next((row.get("result_ref") for row in refs if isinstance(row, dict) and
                         row.get("tool") == "get_warranty" and
                         row.get("source") == "shoprelay_warranty_policy"), None)
    if not isinstance(warranty_ref, str) or not warranty_ref:
        return False
    policy_ref = warranty_audits[0].get("result_ref")
    if not isinstance(policy_ref, str) or policy.get("knowledge_version") != warranty_audits[0].get("knowledge_version"):
        return False
    if policy.get("index_fingerprint") != warranty_audits[0].get("index_fingerprint"):
        return False
    evidence_ids = policy.get("evidence_ids")
    citations = payload.get("citations") if isinstance(payload.get("citations"), list) else []
    citation_ids = {row.get("chunk_id") for row in citations if isinstance(row, dict)}
    return (isinstance(evidence_ids, list) and bool(evidence_ids) and
            set(evidence_ids).issubset(citation_ids) and
            payload.get("tool_execution_status") and any(
                isinstance(row, dict) and row.get("capability") == "get_warranty" and
                row.get("executed") is True and row.get("status") == "COMPLETED" and
                isinstance(row.get("evidence_ids"), list) and
                row["evidence_ids"] == warranty_audits[0].get("evidence_ids") and
                set(evidence_ids).issubset(row["evidence_ids"])
                for row in payload["tool_execution_status"]))


def score_archive_r6(case: dict, archive: dict, *, budget_snapshot: dict, cleanup_status: str) -> dict:
    """R6 classification separates a known single classifier rejection from system failure.

    R5's scoring contract is intentionally left unchanged for historical recomputation.
    A local semantic rejection is a continuable task failure only when the Provider
    response, bounded diagnostic, usage, same-Run trace, zero-call audit and cleanup
    are all evidenced. Parse/schema and unknown failures remain blocking.
    """
    grade = score_archive(case, archive, budget_snapshot=budget_snapshot, cleanup_status=cleanup_status)
    if grade["program_checks"].get("tool_result_audit_linkage") == "UNKNOWN" and \
            _warranty_composite_result_is_linked(archive):
        grade["program_checks"]["tool_result_audit_linkage"] = "PASS"
        grade["warranty_composite_reference_linkage"] = "PASS"
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    payload = result.get("result") if isinstance(result.get("result"), dict) else {}
    route = payload.get("route") if isinstance(payload.get("route"), dict) else {}
    diagnostic = route.get("provider_diagnostic") if isinstance(route.get("provider_diagnostic"), dict) else {}
    summary = diagnostic.get("response_summary") if isinstance(diagnostic.get("response_summary"), dict) else {}
    usage = summary.get("usage") if isinstance(summary.get("usage"), dict) else {}
    usage_recorded = bool(usage) and all(key in {"prompt_tokens", "completion_tokens", "total_tokens"} and
        isinstance(value, int) and not isinstance(value, bool) and value >= 0 for key, value in usage.items())
    safe_single_rejection = (
        result.get("status") in TERMINAL_RUNS and
        route.get("decision_source") == "FALLBACK" and
        diagnostic.get("failure_phase") == "CLASSIFIER_SEMANTIC_CONTRACT" and
        diagnostic.get("error_category") == "CLASSIFIER_OUTPUT_INVALID" and
        diagnostic.get("provider_response_received") is True and
        diagnostic.get("request_delivery") == "RESPONSE_RECEIVED" and
        isinstance(diagnostic.get("route_error_code"), str) and
        isinstance(diagnostic.get("field_path"), str) and
        summary.get("provider_response_received") is True and
        summary.get("usage_status") == "RECORDED" and
        usage_recorded and
        grade["program_checks"].get("run_trace_association") == "PASS" and
        grade["program_checks"].get("tool_audit_completeness") == "PASS" and
        grade["program_checks"].get("tool_result_audit_linkage") == "PASS" and
        grade["program_checks"].get("budget_and_cleanup") == "PASS"
    )
    grade["schema_version"] = "m6.2-r324-r6-code-grader-v1"
    grade["classification"] = ("KNOWN_SINGLE_CLASSIFIER_CONTRACT_FAILURE_CONTINUABLE"
        if safe_single_rejection else "UNCLASSIFIED_OR_NONCONTINUABLE_FAILURE"
        if grade["program_checks"].get("provider_execution") in {"FAIL", "UNKNOWN"} else "NO_PROVIDER_FAILURE")
    grade["task_outcome"] = "FAIL" if safe_single_rejection else grade["task_outcome"]
    grade["continuable_single_model_failure"] = safe_single_rejection
    grade["failure_evidence"] = ({"route_stage": diagnostic.get("route_stage"),
        "route_error_code": diagnostic.get("route_error_code"),
        "field_path": diagnostic.get("field_path"),
        "response_summary": summary} if safe_single_rejection else None)
    return grade


def _r7_rejection_record(archive: dict) -> dict:
    """Recover a bounded rejected-answer audit from same-Run durable failure data."""
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    run_id = result.get("run_id")
    observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    runs = observations.get("runs") if isinstance(observations.get("runs"), list) else []
    traces = observations.get("traces") if isinstance(observations.get("traces"), list) else []
    candidates = []
    if len(runs) == 1 and runs[0].get("run_id") == run_id:
        raw = runs[0].get("error_detail_json")
        if isinstance(raw, str):
            try:
                detail = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                detail = {}
            if isinstance(detail, dict) and isinstance(detail.get("answer_rejection_audit"), dict):
                candidates.append((detail["answer_rejection_audit"], "run.error_detail_json"))
    for row in traces:
        if row.get("run_id") != run_id or not isinstance(row.get("event_json"), str):
            continue
        try:
            payload = json.loads(row["event_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        audit = payload.get("answer_rejection_audit") if isinstance(payload, dict) else None
        if isinstance(audit, dict):
            candidates.append((audit, "trace.error_detail"))
    if not candidates:
        return {"status": "NOT_CAPTURED", "reason": "NO_SAME_RUN_REJECTION_AUDIT"}
    canonical = json.dumps(candidates[0][0], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    if any(json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != canonical
           for row, _source in candidates[1:]):
        return {"status": "UNKNOWN", "reason": "CONFLICTING_SAME_RUN_REJECTION_AUDITS"}
    audit, source = candidates[0]
    if (audit.get("run_id") != run_id or not isinstance(audit.get("trace_id"), str) or
            audit.get("diagnostic_schema_version") != "policy-answer-rejection-audit-v1"):
        return {"status": "UNKNOWN", "reason": "REJECTION_AUDIT_BINDING_INVALID"}
    return {"status": "CAPTURED_REDACTED", "source": source, "audit": audit}


def score_archive_r7(case: dict, archive: dict, *, budget_snapshot: dict, cleanup_status: str) -> dict:
    """R7 projection keeps rejected candidate distinct from user-delivered output."""
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    runs = observations.get("runs") if isinstance(observations.get("runs"), list) else []
    events = observations.get("run_events") if isinstance(observations.get("run_events"), list) else []
    run_id = result.get("run_id")
    event_route = None
    for event in events:
        if event.get("run_id") != run_id or event.get("event_type") != "route_decided":
            continue
        try:
            payload = json.loads(event.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and payload:
            if event_route is not None:
                return {"schema_version": "m6.2-r324-r7-code-grader-v1", "case_id": case.get("case_id"),
                    "run_id": run_id, "decision": "STOP", "classification": "EVALUATOR_FAILURE",
                    "blocking_reason": "MULTIPLE_ROUTE_DECISIONS_FOR_RUN"}
            event_route = payload
    audit_projection = _r7_rejection_record(archive)
    audit = audit_projection.get("audit") if audit_projection.get("status") == "CAPTURED_REDACTED" else None
    # Build a scoring-only view from durable same-Run events. Do not mutate the
    # source archive or manufacture a successful response.
    projected = json.loads(json.dumps(archive, ensure_ascii=False))
    outer = projected.get("result") if isinstance(projected.get("result"), dict) else {}
    inner = outer.get("result") if isinstance(outer.get("result"), dict) else {}
    if event_route:
        inner["route"] = event_route
        outer["result"] = inner
    if audit is not None:
        inner["answer_rejection_audit"] = audit
        outer["result"] = inner
    projected["result"] = outer
    grade = score_archive_r6(case, projected, budget_snapshot=budget_snapshot, cleanup_status=cleanup_status)
    grade["schema_version"] = "m6.2-r324-r7-code-grader-v1"
    grade["route_projection_source"] = "SAME_RUN_ROUTE_DECIDED_EVENT" if event_route else "MISSING"
    grade["rejection_audit_projection"] = {key: value for key, value in audit_projection.items()
        if key != "audit"}
    if audit is not None:
        retrieved_ids = set()
        for event in events:
            if event.get("run_id") != run_id or event.get("event_type") != "policy_retrieval_finished":
                continue
            try:
                retrieval = json.loads(event.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            if retrieval.get("status") == "SUCCEEDED":
                retrieved_ids.update(x for x in retrieval.get("evidence_ids", []) if isinstance(x, str))
        for invocation in archive.get("read_invocations", []):
            call = invocation.get("tool_audit")
            if call and call["run_id"] == run_id and call["result_ref"] and call["status"] == "COMPLETED":
                retrieved_ids.update(row["chunk_id"] for row in invocation.get("retrieval_result", {}).get("evidence", []))
        citations = audit.get("candidate_citation_ids")
        citation_scope = ("PASS" if isinstance(citations, list) and bool(citations) and
                          set(citations).issubset(retrieved_ids) else "FAIL" if isinstance(citations, list)
                          else "UNKNOWN")
        candidate = audit.get("candidate_answer_redacted")
        candidate_encoding = ("DAMAGED_REPLACEMENT_CHARACTERS" if isinstance(candidate, str) and
                              "\ufffd" in candidate else "TEXT_CAPTURED") if isinstance(candidate, str) else "NOT_CAPTURED"
        candidate_semantically_readable = isinstance(candidate, str) and bool(candidate.strip()) and "\ufffd" not in candidate
        grade["candidate_assessment"] = {
            "status": "REJECTED_CANDIDATE_NOT_DELIVERED" if isinstance(candidate, str) and candidate else
                      "UNSCORABLE_NOT_CAPTURED",
            "redacted_text": candidate if isinstance(candidate, str) else None,
            "encoding_quality": candidate_encoding,
            "citation_ids": citations if isinstance(citations, list) else None,
            "citation_scope": citation_scope,
            "validator_version": audit.get("validator_version"),
            "failure_classification": audit.get("classification"),
            "validation_findings": audit.get("validation_findings"),
            "candidate_semantic_score": "PENDING_REVIEW" if candidate_semantically_readable else "UNSCORABLE",
            "is_user_output": False,
        }
        grade["program_checks"]["user_visible_answer_present"] = (
            "PASS" if any(m.get("role") == "assistant" and isinstance(m.get("content"), str) and
                          m["content"].strip() for m in (observations.get("messages") or [])) else "FAIL")
        if event_route:
            caps = event_route.get("required_capabilities")
            allow = {r.get("tool_id") for r in case.get("input", {}).get("expected_tool_calls", [])
                     if r.get("mode") == "read_only"}
            grade["program_checks"]["route_within_allowlist"] = (
                "PASS" if isinstance(caps, list) and set(caps).issubset(allow) else "FAIL")
        retrieval_calls = [e for e in events if e.get("event_type") == "policy_retrieval_finished" and
                           e.get("run_id") == run_id]
        if retrieval_calls:
            grade["program_checks"]["policy_retrieval_audit"] = (
                "PASS" if audit.get("run_id") == run_id and audit.get("trace_id") and
                all(link.get("run_id") == run_id for link in audit.get("retrieval_links", [])) else "UNKNOWN")
        # A rejected answer is a delivered-output failure, even if its candidate
        # was safely archived. Preserve the semantic candidate as a separate item.
        grade["task_outcome"] = "FAIL" if grade["program_checks"]["user_visible_answer_present"] == "FAIL" else grade["task_outcome"]
    grade["failure_classification"] = (
        "KNOWN_SINGLE_ANSWER_VALIDATION_REJECTION" if audit is not None and
        result.get("status") == "FAILED" and result.get("failure_code") in {
            "ANSWER_SEMANTIC_VALIDATION_FAILED", "MODEL_INVALID_RESPONSE", "ANSWER_ASPECT_CITATION_MISSING"}
        else "UNCLASSIFIED_OR_EVIDENCE_INCOMPLETE" if result.get("status") == "FAILED" else "NONE")
    grade["continue_decision"] = r7_continue_decision(grade, result=result, rejection_audit=audit,
        cleanup_status=cleanup_status)
    return grade


def r7_continue_decision(grade: dict, *, result: dict, rejection_audit: dict | None,
                         cleanup_status: str) -> dict:
    """Versioned fail-closed stop rule for a safely archived single-case rejection."""
    checks = grade.get("program_checks") if isinstance(grade.get("program_checks"), dict) else {}
    critical = ("run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
                "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup")
    failures = [name for name in critical if checks.get(name) != "PASS"]
    for name in ("owner_scope_evidence", "policy_retrieval_audit"):
        if checks.get(name) not in {"PASS", "NOT_APPLICABLE"}:
            failures.append(name)
    if cleanup_status != "RECEIPT_CLEANED_AND_LEASE_RELEASED":
        failures.append("cleanup")
    if failures:
        return {"decision": "STOP", "blocking_items": failures}
    if result.get("status") == "FAILED":
        allowed_rejections = {"POLICY_SEMANTIC_VALIDATION_REJECTED", "CITATION_OUTSIDE_CURRENT_EVIDENCE",
                              "INVALID_OR_EMPTY_CANDIDATE", "REQUIRED_ASPECT_CITATION_MISSING"}
        usage = (rejection_audit or {}).get("usage_summary")
        if (rejection_audit is None or rejection_audit.get("classification") not in allowed_rejections or
                rejection_audit.get("run_id") != result.get("run_id") or
                not rejection_audit.get("trace_id") or not rejection_audit.get("validation_findings") or
                not isinstance(usage, dict) or any(not isinstance(usage.get(key), int) or usage[key] < 0
                    for key in ("input_units", "output_units", "total_units"))):
            return {"decision": "STOP", "blocking_items": ["failure_stage_or_usage_unknown"]}
        return {"decision": "CONTINUE", "blocking_items": [],
                "recorded_case_failure": "DELIVERY_FAIL_CANDIDATE_SCORED_SEPARATELY"}
    if checks.get("provider_execution") == "UNKNOWN":
        return {"decision": "STOP", "blocking_items": ["provider_usage_or_status_unknown"]}
    return {"decision": "CONTINUE", "blocking_items": []}


def batch_stop_reason_r7(grader: dict) -> str | None:
    """Apply R7's frozen evidence-based stop decision, independent of task score."""
    decision = grader.get("continue_decision")
    if not isinstance(decision, dict) or decision.get("decision") not in {"CONTINUE", "STOP"}:
        return "CHECKER_DECISION_MISSING_OR_INVALID"
    if decision["decision"] == "STOP":
        return "R7_BLOCKING_EVIDENCE_OR_SAFETY_FAILURE:" + ",".join(decision.get("blocking_items", []))
    return None


def _route_scope_rejection_from_archive(archive: dict) -> dict | None:
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    runs = observations.get("runs") if isinstance(observations.get("runs"), list) else []
    run_id = result.get("run_id")
    if len(runs) != 1 or runs[0].get("run_id") != run_id:
        return None
    raw = runs[0].get("error_detail_json")
    try:
        detail = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, json.JSONDecodeError):
        return None
    diagnostic = detail.get("route_capability_scope") if isinstance(detail, dict) else None
    if (not isinstance(diagnostic, dict) or
            diagnostic.get("schema_version") != "route-capability-scope-diagnostic-v1" or
            diagnostic.get("run_id") != run_id or not isinstance(diagnostic.get("trace_id"), str)):
        return None
    records = observations.get("observability_records") if isinstance(
        observations.get("observability_records"), list) else []
    router_error = False
    for row in records:
        if (row.get("run_id") != run_id or row.get("trace_id") != diagnostic["trace_id"] or
                row.get("record_type") != "span"):
            continue
        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        if payload.get("node_name") == "router" and payload.get("status") == "ERROR":
            router_error = True
            break
    if not router_error:
        return None
    return diagnostic


def score_archive_r8(case: dict, archive: dict, *, budget_snapshot: dict, cleanup_status: str) -> dict:
    """R8 distinguishes a fully evidenced model capability-choice failure from an audit failure."""
    grade = score_archive_r7(case, archive, budget_snapshot=budget_snapshot, cleanup_status=cleanup_status)
    result = archive.get("result") if isinstance(archive.get("result"), dict) else {}
    observations = result.get("observations") if isinstance(result.get("observations"), dict) else {}
    events = observations.get("run_events") if isinstance(observations.get("run_events"), list) else []
    run_id = result.get("run_id")
    diagnostic = _route_scope_rejection_from_archive(archive)
    allow = {row.get("tool_id") for row in case.get("input", {}).get("expected_tool_calls", [])
             if row.get("mode") == "read_only"}
    started = [row for row in events if row.get("run_id") == run_id and
               row.get("event_type") in {"tool_started", "policy_retrieval_started"}]
    finished = [row for row in events if row.get("run_id") == run_id and
                row.get("event_type") in {"tool_finished", "policy_retrieval_finished"}]
    requested = set(diagnostic.get("requested_capabilities", [])) if diagnostic else set()
    disallowed = set(diagnostic.get("disallowed_capabilities", [])) if diagnostic else set()
    usage = diagnostic.get("usage") if diagnostic else None
    usage_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None
    category_calls = budget_snapshot.get("calls_by_category") if isinstance(budget_snapshot, dict) else None
    complete_budget = (isinstance(budget_snapshot, dict) and budget_snapshot.get("complete") is True and
        budget_snapshot.get("settled") is True and budget_snapshot.get("active_reservations") == 0 and
        budget_snapshot.get("unknown_tokens") is False and budget_snapshot.get("unknown_cost") is False and
        budget_snapshot.get("snapshot_kind") == "FINAL" and not budget_snapshot.get("integrity_errors") and
        isinstance(category_calls, dict) and category_calls.get("provider") == 1 and
        category_calls.get("tool") == 0 and category_calls.get("judge") == 0 and
        category_calls.get("embedding") == 0 and isinstance(usage_tokens, int) and usage_tokens >= 0 and
        budget_snapshot.get("tokens") == usage_tokens)
    rejection_gates = {"terminal_failed": result.get("status") == "FAILED",
        "diagnostic_bound": bool(diagnostic),
        "response_received": bool(diagnostic and diagnostic.get("provider_response_received") is True),
        "requested_capabilities_nonempty": bool(requested),
        "disallowed_matches_scope_difference": bool(diagnostic and disallowed == requested - allow and disallowed),
        "allowed_scope_matches_case": bool(diagnostic and diagnostic.get("allowed_capabilities") == sorted(allow)),
        "usage_recorded": bool(diagnostic and diagnostic.get("usage_status") == "RECORDED"),
        "budget_known_settled": complete_budget,
        "zero_tool_or_retrieval_events": not started and not finished,
        "cleanup_proven": cleanup_status == "RECEIPT_CLEANED_AND_LEASE_RELEASED"}
    scope_rejection_complete = bool(
        result.get("status") == "FAILED" and diagnostic and
        diagnostic.get("provider_response_received") is True and
        requested and disallowed == requested - allow and disallowed and
        diagnostic.get("allowed_capabilities") == sorted(allow) and
        diagnostic.get("usage_status") == "RECORDED" and complete_budget and
        not started and not finished and cleanup_status == "RECEIPT_CLEANED_AND_LEASE_RELEASED")
    grade["schema_version"] = "m6.2-r324-r8-code-grader-v1"
    base_continue_decision = grade.get("continue_decision")
    scope_status = ("COMPLETE_SAFE_MODEL_CAPABILITY_REJECTION" if scope_rejection_complete else
        "UNKNOWN_OR_INCOMPLETE" if diagnostic is None and
            result.get("failure_code") == "ROUTE_CAPABILITY_SCOPE_REJECTED" else
        "NOT_APPLICABLE" if diagnostic is None else "REJECTION_NOT_SAFE_TO_CONTINUE")
    grade["route_scope_rejection"] = ({"status": scope_status,
        "run_id": run_id, "trace_id": diagnostic["trace_id"],
        "candidate_strategy": diagnostic["candidate_strategy"],
        "candidate_sub_strategy": diagnostic.get("candidate_sub_strategy"),
        "allowed_capabilities": diagnostic["allowed_capabilities"],
        "requested_capabilities": diagnostic["requested_capabilities"],
        "disallowed_capabilities": diagnostic["disallowed_capabilities"],
        "tool_or_retrieval_events": 0, "gates": rejection_gates} if scope_rejection_complete else
        {"status": scope_status, "gates": rejection_gates})
    if scope_rejection_complete:
        checks = grade.setdefault("program_checks", {})
        checks.update({"route_within_allowlist": "FAIL", "tool_scope": "PASS",
            "tool_audit_completeness": "PASS", "tool_result_audit_linkage": "PASS",
            "provider_execution": "PASS", "policy_retrieval_audit": "NOT_APPLICABLE"})
        grade["task_outcome"] = "FAIL"
        grade["failure_classification"] = "KNOWN_SINGLE_MODEL_CAPABILITY_SELECTION_FAILURE"
        grade["continue_decision"] = {"decision": "CONTINUE", "blocking_items": [],
            "recorded_case_failure": "CAPABILITY_SCOPE_REJECTED_WITH_COMPLETE_ZERO_EXECUTION_AUDIT"}
    elif diagnostic is not None:
        grade["continue_decision"] = {"decision": "STOP", "blocking_items": [
            "route_scope_rejection_or_its_zero-execution/accounting evidence incomplete"]}
    elif (isinstance(base_continue_decision, dict) and
            base_continue_decision.get("decision") == "CONTINUE" and
            base_continue_decision.get("blocking_items") == []):
        # A normal completed application path is governed by the existing
        # evidence-aware R7 decision. R8's special STOP applies only when a
        # capability-scope rejection was observed but its audit is incomplete.
        grade["continue_decision"] = base_continue_decision
    else:
        grade["continue_decision"] = base_continue_decision or {
            "decision": "STOP", "blocking_items": ["base_execution_decision_missing"]}
    return grade


def batch_stop_reason_r8(grader: dict) -> str | None:
    decision = grader.get("continue_decision") if isinstance(grader, dict) else None
    if not isinstance(decision, dict) or decision.get("decision") not in {"CONTINUE", "STOP"}:
        return "R8_DECISION_MISSING_OR_INVALID"
    blockers = decision.get("blocking_items")
    if (not isinstance(blockers, list) or
            (decision["decision"] == "CONTINUE" and blockers) or
            (decision["decision"] == "STOP" and not blockers)):
        return "R8_DECISION_BLOCKER_CONSISTENCY_INVALID"
    return None if decision["decision"] == "CONTINUE" else "R8_BLOCKING_EVIDENCE_OR_SAFETY_FAILURE"


def score_archive_current(case: dict, archive: dict, *, budget_snapshot: dict, cleanup_status: str) -> dict:
    """Missing required retrieval is a task failure when zero execution is proven.

    Preserve prior graders for historical replay. A started/incomplete retrieval,
    missing route/events, or failed lifecycle never qualifies for this rule.
    """
    grade = score_archive_r8(case, archive, budget_snapshot=budget_snapshot, cleanup_status=cleanup_status)
    result = archive["result"]
    events = result["observations"]["run_events"]
    routes = [json.loads(e["payload_json"]) for e in events if e["event_type"] == "route_decided"]
    decision = grade["continue_decision"]
    if (result["status"] == "SUCCEEDED" and len(routes) == 1 and
            "search_knowledge" not in routes[0]["required_capabilities"] and
            not any(e["event_type"] in {"policy_retrieval_started", "policy_retrieval_finished"} for e in events) and
            decision == {"decision": "STOP", "blocking_items": ["policy_retrieval_audit"]}):
        grade["task_outcome"] = "FAIL"
        grade["failure_classification"] = "REQUIRED_POLICY_RETRIEVAL_NOT_SELECTED_TASK_FAILURE"
        grade["continue_decision"] = {"decision": "CONTINUE", "blocking_items": [],
            "recorded_case_failure": "COMPLETE_ROUTE_AND_EVENT_SNAPSHOT_PROVES_NO_POLICY_CALL"}
    grade["schema_version"] = "m62-code-grader-v3"
    return grade


def review_task_read_coverage(grade: dict, requirements: dict) -> dict:
    """Versioned task evidence review, separate from exact historical tool-list checks.

    A group is satisfied by one documented legal alternative. Owner/audit
    evidence remains mandatory; no empty-set comparison proves zero calls.
    """
    critical = ("run_terminal", "run_trace_association", "tool_scope",
                "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup")
    checks = grade["program_checks"]
    if any(checks[name] != "PASS" for name in critical):
        return {"status": "UNKNOWN", "reason": "CRITICAL_EXECUTION_EVIDENCE_INCOMPLETE"}
    if requirements["owner_required"] and checks["owner_scope_evidence"] != "PASS":
        return {"status": "UNKNOWN", "reason": "OWNER_EVIDENCE_MISSING"}
    observed = set(grade["observed_tools"])
    if grade["policy_retrieval_call_ids"] and checks["policy_retrieval_audit"] == "PASS":
        observed.add("search_knowledge")
    missing = []
    for group in requirements["required_evidence_groups"]:
        options = group["any_of_tool_sets"]
        if not options or any(not option for option in options):
            raise D1PreflightError("TASK_EVIDENCE_ALTERNATIVE_MUST_BE_NONEMPTY")
        if not any(set(option).issubset(observed) for option in options):
            missing.append(group["fact"])
    if requirements["zero_calls_required"] and (observed or grade["tool_audit_reason"] !=
            "EXPLICIT_ZERO_CALL_ROUTE_WITH_COMPLETE_EVENT_SNAPSHOT"):
        return {"status": "FAIL", "reason": "ZERO_CALL_CONTRACT_NOT_PROVEN"}
    return {"status": "FAIL" if missing else "PASS", "missing_facts": missing,
        "observed_capabilities": sorted(observed), "rule_version": requirements["version"],
        "historical_tool_list_verdict": checks["expected_business_tool_execution"]}


def batch_stop_reason_r9(grader: dict) -> str | None:
    """Continue after evidenced task failures; stop only on critical execution uncertainty."""
    if not isinstance(grader, dict) or not isinstance(grader.get("program_checks"), dict):
        return "R9_CHECKER_OUTPUT_MISSING_OR_INVALID"
    checks = grader["program_checks"]
    decision = grader.get("continue_decision")
    if not isinstance(decision, dict) or decision.get("decision") not in {"CONTINUE", "STOP"}:
        return "R9_CONTINUE_DECISION_MISSING_OR_INVALID"
    blockers = decision.get("blocking_items")
    if not isinstance(blockers, list) or (decision["decision"] == "CONTINUE" and blockers) or \
            (decision["decision"] == "STOP" and not blockers):
        return "R9_DECISION_BLOCKER_CONSISTENCY_INVALID"
    if decision["decision"] == "STOP":
        return "R9_LOCKED_STOP_DECISION_WITH_BLOCKERS"
    route_rejection = grader.get("route_scope_rejection")
    safely_rejected = isinstance(route_rejection, dict) and \
        route_rejection.get("status") == "COMPLETE_SAFE_MODEL_CAPABILITY_REJECTION"
    critical = ("run_terminal", "run_trace_association", "tool_scope", "tool_audit_completeness",
        "tool_result_audit_linkage", "budget_and_cleanup")
    if any(checks.get(name) != "PASS" for name in critical):
        return "R9_CRITICAL_EXECUTION_EVIDENCE_OR_SAFETY_FAILURE"
    if not safely_rejected and checks.get("route_within_allowlist") != "PASS":
        return "R9_ROUTE_SCOPE_EVIDENCE_FAILURE"
    if checks.get("provider_execution") == "UNKNOWN" or checks.get("budget_and_cleanup") != "PASS":
        return "R9_PROVIDER_USAGE_OR_CLEANUP_UNKNOWN"
    if checks.get("provider_execution") == "FAIL" and grader.get("provider_failure_continuable") is not True:
        return "R9_PROVIDER_FAILURE_NOT_PROVEN_CONTINUABLE"
    return None


def batch_stop_reason_r6(grader: dict, allowed_capabilities: list[str]) -> str | None:
    """Fail closed on system/evidence issues; permit only a fully evidenced case failure."""
    checks = grader.get("program_checks") if isinstance(grader.get("program_checks"), dict) else {}
    required = ("run_terminal", "run_trace_association", "route_within_allowlist", "tool_scope",
        "tool_audit_completeness", "tool_result_audit_linkage", "budget_and_cleanup",
        "user_visible_answer_present")
    if any(checks.get(name) != "PASS" for name in required):
        return "CRITICAL_EXECUTION_EVIDENCE_OR_DELIVERY_FAILURE"
    owner_tools = {"get_order", "get_order_items", "track_shipment", "get_refund_status"}
    if owner_tools.intersection(allowed_capabilities) and checks.get("owner_scope_evidence") != "PASS":
        return "OWNER_SCOPE_EVIDENCE_FAILURE"
    if "search_knowledge" in allowed_capabilities and checks.get("policy_retrieval_audit") != "PASS":
        return "POLICY_RETRIEVAL_AUDIT_FAILURE"
    if checks.get("provider_execution") in {"FAIL", "UNKNOWN"}:
        if grader.get("continuable_single_model_failure") is True and \
                grader.get("classification") == "KNOWN_SINGLE_CLASSIFIER_CONTRACT_FAILURE_CONTINUABLE":
            return None
        return "PROVIDER_OR_PUBLIC_CONTRACT_FAILURE"
    return None


class DurableAttemptJournal:
    """Append-only, fsync'd attempt ledger; any started attempt blocks replay."""
    def __init__(self, path: Path, *, batch_id: str):
        self.path, self.batch_id = path, batch_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock_path = path.with_suffix(path.suffix + ".lock")

    def acquire_batch(self) -> None:
        try:
            fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"{self.batch_id}\n".encode())
            os.fsync(fd)
            os.close(fd)
        except FileExistsError as exc:
            raise D1PreflightError("M62_EXECUTION_LOCK_PRESENT_RECONCILIATION_REQUIRED") from exc

    def consumed_cases(self) -> set[str]:
        if not self.path.exists():
            return set()
        consumed = set()
        with self.path.open("r", encoding="utf-8") as stream:
            for line in stream:
                row = json.loads(line)
                if row.get("batch_id") != self.batch_id or row.get("event") == "CASE_ATTEMPT_RESERVED":
                    if row.get("batch_id") != self.batch_id:
                        raise D1PreflightError("M62_JOURNAL_CROSS_BATCH_OR_CORRUPT")
                    if row.get("event") == "CASE_ATTEMPT_RESERVED":
                        consumed.add(row.get("case_id"))
        return consumed

    def reserve_attempt(self, case_id: str, *, budget: dict, bindings: dict) -> None:
        if not self.lock_path.is_file() or self.lock_path.read_text(encoding="utf-8").strip() != self.batch_id:
            raise D1PreflightError("M62_BATCH_LOCK_NOT_OWNED")
        consumed = self.consumed_cases()
        if case_id in consumed:
            raise D1PreflightError("M62_CASE_ATTEMPT_ALREADY_CONSUMED_NO_REPLAY")
        row = {"event": "CASE_ATTEMPT_RESERVED", "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "batch_id": self.batch_id, "case_id": case_id, "budget_reserved": budget, "bindings": bindings}
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def append(self, event: str, **payload: Any) -> None:
        row = {"event": event, "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "batch_id": self.batch_id, **payload}
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def release_lock_after_safe_closeout(self) -> None:
        # A stale lock is intentionally not auto-removed on restart. The active
        # process may remove it only after archive, terminal-state and cleanup checks.
        self.lock_path.unlink(missing_ok=True)


class TaskQuotaLedger:
    """Fixed-path cumulative quota ledger shared by successor execution segments."""
    def __init__(self, path: Path, *, task_id: str, seed_sha256: str):
        self.path, self.task_id, self.seed_sha256 = path, task_id, seed_sha256

    @staticmethod
    def _hash(value: dict) -> str:
        payload = {k: v for k, v in value.items() if k != "state_sha256"}
        return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True,
            separators=(",", ":")).encode()).hexdigest()

    def read(self) -> dict:
        if not self.path.is_file():
            raise D1PreflightError("M62_TASK_QUOTA_LEDGER_MISSING")
        value = json.loads(self.path.read_text(encoding="utf-8"))
        limits15 = {"application_runs": 15, "provider_requests": 600,
            "tokens": 10000000, "local_estimated_usd": 30.0,
            "runtime_seconds": 14400, "repository_preflight_reads": 1000}
        limits16 = {**limits15, "application_runs": 16}
        limits20 = {**limits15, "application_runs": 20}
        limits27 = {**limits15, "application_runs": 27}
        limits = value.get("limits")
        limit_amendments = value.get("limit_amendments", [])
        def valid_amendment(row: Any, old: int, new: int) -> bool:
            return (isinstance(row, dict) and row.get("from_application_runs") == old and
                row.get("to_application_runs") == new and
                isinstance(row.get("source_sha256"), str) and len(row["source_sha256"]) == 64)
        supported_limits = (limits == limits15 or
            (limits == limits16 and len(limit_amendments) == 1 and
                valid_amendment(limit_amendments[0], 15, 16)) or
            (limits in (limits20, limits27) and len(limit_amendments) == (2 if limits == limits20 else 3) and
                valid_amendment(limit_amendments[0], 15, 16) and
                valid_amendment(limit_amendments[1], 16, 20) and
                limit_amendments[1].get("authorization_basis") ==
                    "CURRENT_USER_INSTRUCTION_FOUR_FIRST_RUNS" and
                (limits == limits20 or (valid_amendment(limit_amendments[2], 20, 27) and
                    limit_amendments[2].get("authorization_basis") ==
                    "CURRENT_USER_INSTRUCTION_SEVEN_FIRST_RUNS_R325"))))
        spent = value.get("spent")
        reserved = value.get("reserved")
        if (value.get("task_id") != self.task_id or value.get("seed_sha256") != self.seed_sha256 or
                value.get("state_sha256") != self._hash(value) or
                not supported_limits or not isinstance(spent, dict) or
                not isinstance(reserved, dict) or not isinstance(value.get("attempts"), dict) or
                not isinstance(value.get("case_records"), dict) or
                any(isinstance(spent.get(key), bool) or not isinstance(spent.get(key), (int, float)) or
                    spent[key] < 0 for key in limits) or
                isinstance(reserved.get("application_runs"), bool) or
                not isinstance(reserved.get("application_runs"), int) or reserved["application_runs"] < 0 or
                any(isinstance(reserved.get(key), bool) or not isinstance(reserved.get(key), (int, float)) or
                    reserved[key] < 0 for key in ("runtime_seconds", "provider_requests", "tokens", "local_estimated_usd")) or
                any(isinstance(v, bool) or not isinstance(v, int) or v != 1
                    for v in value["attempts"].values()) or
                spent["application_runs"] + reserved["application_runs"] > limits["application_runs"] or
                spent["provider_requests"] + reserved["provider_requests"] > limits["provider_requests"] or
                spent["tokens"] + reserved["tokens"] > limits["tokens"] or
                spent["local_estimated_usd"] + reserved["local_estimated_usd"] > limits["local_estimated_usd"] or
                spent["runtime_seconds"] + reserved["runtime_seconds"] > limits["runtime_seconds"] or
                spent["repository_preflight_reads"] > limits["repository_preflight_reads"]):
            raise D1PreflightError("M62_TASK_QUOTA_LEDGER_BINDING_OR_INTEGRITY_INVALID")
        return value

    def write(self, value: dict) -> None:
        value = dict(value)
        value.pop("state_sha256", None)
        value["state_sha256"] = self._hash(value)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, self.path)

    def record_preflight_reads(self, amount: int) -> dict:
        state = self.read()
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise D1PreflightError("M62_TASK_PREFLIGHT_COUNT_INVALID")
        new_total = state["spent"]["repository_preflight_reads"] + amount
        if new_total > state["limits"]["repository_preflight_reads"]:
            raise D1PreflightError("M62_TASK_PREFLIGHT_QUOTA_EXHAUSTED")
        state["spent"]["repository_preflight_reads"] = new_total
        state.setdefault("segments", []).append({"kind": "REPOSITORY_PREFLIGHT",
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reads": amount})
        self.write(state)
        # write() canonicalizes the integrity digest on a copy. Return the
        # persisted snapshot so callers cannot bind a stale pre-write digest.
        return self.read()

    def reserve_case(self, case_id: str, *, limits: dict) -> dict:
        state = self.read()
        attempts = state.setdefault("attempts", {})
        if attempts.get(case_id, 0) != 0:
            raise D1PreflightError("M62_TASK_CASE_ATTEMPT_ALREADY_CONSUMED")
        if any(row.get("status") == "ATTEMPT_RESERVED" for row in state.get("case_records", {}).values()
               if isinstance(row, dict)):
            raise D1PreflightError("M62_TASK_HAS_UNRESOLVED_CASE_ATTEMPT")
        if state["spent"]["application_runs"] + state["reserved"]["application_runs"] >= state["limits"]["application_runs"]:
            raise D1PreflightError("M62_TASK_APPLICATION_RUN_QUOTA_EXHAUSTED")
        if state["spent"]["runtime_seconds"] + state["reserved"]["runtime_seconds"] + limits["max_duration_seconds"] > state["limits"]["runtime_seconds"]:
            raise D1PreflightError("M62_TASK_RUNTIME_QUOTA_EXHAUSTED")
        attempts[case_id] = 1
        state["reserved"]["application_runs"] += 1
        state["reserved"]["runtime_seconds"] += limits["max_duration_seconds"]
        for key, budget_key in (("provider_requests", "provider_calls"), ("tokens", "total_tokens"),
                                ("local_estimated_usd", "estimated_usd")):
            if state["spent"][key] + state["reserved"].get(key, 0) + limits[budget_key] > state["limits"][key]:
                raise D1PreflightError("M62_TASK_CASE_RESERVATION_EXCEEDS_CUMULATIVE_BUDGET")
            state["reserved"][key] = state["reserved"].get(key, 0) + limits[budget_key]
        state.setdefault("case_records", {})[case_id] = {"attempt": 1,
            "status": "ATTEMPT_RESERVED", "reserved_limits": limits,
            "run_created": False, "provider_requests": 0, "tokens": None,
            "local_estimated_usd": None, "elapsed_seconds": None}
        state.setdefault("segments", []).append({"kind": "CASE_ATTEMPT_RESERVED", "case_id": case_id,
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reserved_run_slots": 1, "reserved_runtime_seconds": limits["max_duration_seconds"]})
        self.write(state)
        return state

    def reserve_recovery_case(self, case_id: str, *, limits: dict, authorization_id: str,
                              source_record_sha256: str) -> dict:
        """Consume one explicitly authorized recovery without erasing the original attempt."""
        state = self.read()
        original = state.get("case_records", {}).get(case_id)
        if (case_id != "D1-POLICY-018" or state.get("limits", {}).get("application_runs") != 16 or
                not isinstance(original, dict) or original.get("attempt") != 1 or
                original.get("status") != "CLOSED_NO_RUN_PRE_EXECUTION_ERROR" or
                original.get("run_created") is not False or original.get("provider_requests") != 0 or
                original.get("tokens") != 0 or original.get("local_estimated_usd") != 0 or
                state.get("attempts", {}).get(case_id) != 1 or not authorization_id or
                not isinstance(source_record_sha256, str) or len(source_record_sha256) != 64):
            raise D1PreflightError("M62_POLICY018_RECOVERY_SOURCE_NOT_ELIGIBLE")
        recoveries = state.setdefault("recovery_records", {})
        if case_id in recoveries or any(row.get("status") == "ATTEMPT_RESERVED"
                for row in recoveries.values() if isinstance(row, dict)):
            raise D1PreflightError("M62_POLICY018_RECOVERY_ALREADY_RESERVED_OR_CONSUMED")
        if any(row.get("status") == "ATTEMPT_RESERVED" for row in state.get("case_records", {}).values()
                if isinstance(row, dict)):
            raise D1PreflightError("M62_TASK_HAS_UNRESOLVED_CASE_ATTEMPT")
        if (state["spent"]["application_runs"] + state["reserved"]["application_runs"] >=
                state["limits"]["application_runs"] or
                state["spent"]["runtime_seconds"] + state["reserved"]["runtime_seconds"] +
                limits["max_duration_seconds"] > state["limits"]["runtime_seconds"]):
            raise D1PreflightError("M62_TASK_RECOVERY_RUN_QUOTA_EXHAUSTED")
        for key, budget_key in (("provider_requests", "provider_calls"), ("tokens", "total_tokens"),
                                ("local_estimated_usd", "estimated_usd")):
            if state["spent"][key] + state["reserved"].get(key, 0) + limits[budget_key] > state["limits"][key]:
                raise D1PreflightError("M62_TASK_CASE_RESERVATION_EXCEEDS_CUMULATIVE_BUDGET")
        state["reserved"]["application_runs"] += 1
        state["reserved"]["runtime_seconds"] += limits["max_duration_seconds"]
        state["reserved"]["provider_requests"] += limits["provider_calls"]
        state["reserved"]["tokens"] += limits["total_tokens"]
        state["reserved"]["local_estimated_usd"] += limits["estimated_usd"]
        recoveries[case_id] = {"recovery_attempt": 1, "authorization_id": authorization_id,
            "source_record_sha256": source_record_sha256, "status": "ATTEMPT_RESERVED",
            "reserved_limits": limits, "run_created": False, "provider_requests": 0,
            "tokens": None, "local_estimated_usd": None, "elapsed_seconds": None,
            "provider_reserved": {"provider_requests": 0, "tokens": 0, "local_estimated_usd": 0.0}}
        state.setdefault("segments", []).append({"kind": "POLICY018_RECOVERY_RESERVED",
            "case_id": case_id, "authorization_id": authorization_id, "source_record_sha256": source_record_sha256,
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reserved_run_slots": 1, "reserved_runtime_seconds": limits["max_duration_seconds"]})
        self.write(state)
        return state

    def reserve_provider_call(self, case_id: str, *, provider_requests: int = 1,
                              tokens: int, estimated_usd: float,
                              recovery_authorization_id: str | None = None) -> dict:
        """Persist a conservative per-request reservation before transport dispatch."""
        state = self.read()
        row = (state.get("recovery_records", {}).get(case_id) if recovery_authorization_id else
               state.get("case_records", {}).get(case_id))
        if recovery_authorization_id and (not isinstance(row, dict) or
                row.get("authorization_id") != recovery_authorization_id):
            raise D1PreflightError("M62_TASK_RECOVERY_AUTHORIZATION_MISMATCH")
        if not isinstance(row, dict) or row.get("status") != "ATTEMPT_RESERVED":
            raise D1PreflightError("M62_TASK_PROVIDER_RESERVATION_WITHOUT_ACTIVE_CASE")
        if (isinstance(provider_requests, bool) or not isinstance(provider_requests, int) or provider_requests != 1 or
                isinstance(tokens, bool) or not isinstance(tokens, int) or tokens <= 0 or
                isinstance(estimated_usd, bool) or not isinstance(estimated_usd, (int, float)) or estimated_usd < 0):
            raise D1PreflightError("M62_TASK_PROVIDER_RESERVATION_INVALID")
        reserved = state.setdefault("reserved", {})
        batch_remaining = state["limits"]["provider_requests"] - state["spent"]["provider_requests"] - reserved.get("provider_requests", 0)
        token_remaining = state["limits"]["tokens"] - state["spent"]["tokens"] - reserved.get("tokens", 0)
        cost_remaining = state["limits"]["local_estimated_usd"] - state["spent"]["local_estimated_usd"] - reserved.get("local_estimated_usd", 0.0)
        per_case = row["reserved_limits"]
        case_reserved = row.setdefault("provider_reserved", {"provider_requests": 0, "tokens": 0, "local_estimated_usd": 0.0})
        if (batch_remaining < provider_requests or token_remaining < tokens or cost_remaining < estimated_usd or
                case_reserved["provider_requests"] + provider_requests > per_case["provider_calls"] or
                case_reserved["tokens"] + tokens > per_case["total_tokens"] or
                case_reserved["local_estimated_usd"] + estimated_usd > per_case["estimated_usd"]):
            raise D1PreflightError("M62_TASK_PROVIDER_BUDGET_EXHAUSTED")
        reserved["provider_requests"] = reserved.get("provider_requests", 0) + provider_requests
        reserved["tokens"] = reserved.get("tokens", 0) + tokens
        reserved["local_estimated_usd"] = reserved.get("local_estimated_usd", 0.0) + estimated_usd
        case_reserved["provider_requests"] += provider_requests
        case_reserved["tokens"] += tokens
        case_reserved["local_estimated_usd"] += estimated_usd
        state.setdefault("segments", []).append({"kind": "PROVIDER_BUDGET_RESERVED", "case_id": case_id,
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "provider_requests": provider_requests, "tokens": tokens, "local_estimated_usd": estimated_usd})
        self.write(state)
        return state

    def settle_case(self, case_id: str, *, run_created: bool, provider_requests: int | None,
                    tokens: int | None, local_estimated_usd: float | None,
                    elapsed_seconds: float | None, status: str,
                    recovery_authorization_id: str | None = None) -> dict:
        state = self.read()
        row = (state.get("recovery_records", {}).get(case_id) if recovery_authorization_id else
               state.get("case_records", {}).get(case_id))
        if recovery_authorization_id and (not isinstance(row, dict) or
                row.get("authorization_id") != recovery_authorization_id):
            raise D1PreflightError("M62_TASK_RECOVERY_AUTHORIZATION_MISMATCH")
        if not isinstance(row, dict) or row.get("status") != "ATTEMPT_RESERVED":
            raise D1PreflightError("M62_TASK_CASE_RESERVATION_NOT_FOUND")
        state["reserved"]["application_runs"] -= 1
        if run_created:
            state["spent"]["application_runs"] += 1
        values = {"provider_requests": provider_requests, "tokens": tokens,
            "local_estimated_usd": local_estimated_usd, "runtime_seconds": elapsed_seconds}
        row_reserved = row.get("provider_reserved", {})
        for key in ("provider_requests", "tokens", "local_estimated_usd"):
            reserved_value = row_reserved.get(key, 0)
            state["reserved"][key] = max(0, state["reserved"].get(key, 0) - reserved_value)
        reserved_values = {"provider_requests": row["reserved_limits"]["provider_calls"],
            "tokens": row["reserved_limits"]["total_tokens"],
            "local_estimated_usd": row["reserved_limits"]["estimated_usd"],
            "runtime_seconds": row["reserved_limits"]["max_duration_seconds"]}
        for key, value in values.items():
            if value is None or isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
                state["unknown_usage"] = True
                # Keep the conservative reservation if actual usage is unknown.
                continue
            state["reserved"][key] = max(0, state["reserved"].get(key, 0) - reserved_values[key])
            if key == "runtime_seconds":
                state["spent"][key] += int(value + 0.999)
            else:
                state["spent"][key] += value
        row.update({"status": status, "run_created": run_created,
            "provider_requests": provider_requests, "tokens": tokens,
            "local_estimated_usd": local_estimated_usd, "elapsed_seconds": elapsed_seconds})
        state.setdefault("segments", []).append({"kind": "CASE_SETTLED", "case_id": case_id,
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status": status, "run_created": run_created})
        spent = state["spent"]
        lim = state["limits"]
        if (spent["application_runs"] > lim["application_runs"] or
                spent["provider_requests"] > lim["provider_requests"] or
                spent["tokens"] > lim["tokens"] or
                spent["local_estimated_usd"] > lim["local_estimated_usd"] or
                spent["runtime_seconds"] > lim["runtime_seconds"]):
            state["over_limit"] = True
        self.write(state)
        return state
