"""Receipt-scoped, no-model Repository admission audit for the r3.2.4 D1 Dev set.

This is an evidence collector, not an Agent Runner. It loads only the fixed
synthetic r3.2.4 bundle into the marked personal SQLite target, invokes the
declared read adapters with arguments derived from the user query or prior
allowed read results, records outcomes, and performs exact receipt cleanup.
It never imports or calls a Provider, Agent, Judge, Embedding, HTTP transport,
or formal evaluation path.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from app.m1.routing import decide_route
from app.m3.product_tools import parse_product_request_many
from eval.m6_1.personal_dev import D1Coordinator, D1PreflightError, OWNER_MAP, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_runtime import validate_case_route_scope


ROOT = Path(__file__).resolve().parents[1]
ASSET = Path("eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json")
ASSET_SHA256 = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
PER_CASE_READ_CAP = 8
TOTAL_READ_CAP = 320


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe_result(tool: str, result: dict[str, Any]) -> dict[str, Any]:
    out = {k: result.get(k) for k in ("source", "order_id", "availability", "error_code", "status",
        "count", "knowledge_version", "index_fingerprint", "evaluation_at", "truncated") if k in result}
    if tool in {"search_knowledge"}:
        out["evidence"] = [{k: row.get(k) for k in ("chunk_id", "version", "knowledge_version",
            "effective_from", "effective_to")} for row in result.get("evidence", [])]
    elif tool in {"search_products"}:
        out["products"] = [{k: row.get(k) for k in ("product_id", "name", "fixture_provenance")}
            for row in result.get("products", [])]
    elif tool == "recommend_products":
        out["recommendations"] = [{"product": {k: row.get("product", {}).get(k)
            for k in ("product_id", "name", "fixture_provenance")}, "match_status": row.get("match_status")}
            for row in result.get("recommendations", [])]
    elif tool == "get_product_detail":
        p = result.get("product") or {}
        out["product"] = {k: p.get(k) for k in ("product_id", "name", "fixture_provenance", "recorded_attributes")}
    elif tool == "get_order":
        out.update({k: result.get(k) for k in ("status", "fixture_provenance", "unknown_fields", "additional_recorded_facts")})
    elif tool == "get_order_items":
        out["items"] = [{k: item.get(k) for k in ("product_id", "name", "quantity", "fixture_provenance",
            "product_fixture_provenance")} for item in result.get("items", [])]
    elif tool == "track_shipment":
        out["records"] = [{k: row.get(k) for k in ("status", "observed_at")} for row in result.get("records", [])]
    elif tool == "check_inventory":
        out["records"] = [{k: row.get(k) for k in ("product_id", "availability", "error_code", "observation_time",
            "snapshot_sellable_qty", "fixture_provenance", "product_fixture_provenance")} for row in result.get("records", [])]
    elif tool == "get_refund_status":
        out["application_record_state"] = result.get("application_record_state")
        out["return_state"] = (result.get("returns") or {}).get("application_record_state")
        out["refund_state"] = (result.get("refunds") or {}).get("application_record_state")
    return out


def audit_case(coordinator: D1Coordinator, bundle: dict[str, Any], case: dict[str, Any]) -> dict[str, Any]:
    case_id = case["case_id"]
    row: dict[str, Any] = {"case_id": case_id, "group_id": case.get("group_id"),
        "split": case.get("split"), "query": case.get("input", {}).get("user_query"),
        "expected_strategy": case.get("input", {}).get("expected_strategy"),
        "expected_intent": case.get("input", {}).get("expected_intent"),
        "scenario": case.get("expected_outcome", {}).get("outcome_type"),
        "risk_level": case.get("risk_level"),
        "fixture_refs": case.get("input", {}).get("fixture_refs", []),
        "owner_alias": case.get("input", {}).get("user_id"),
        "owner_id": None,
        "owner_validation_basis": "plan_case owner map is passed unchanged to owner-scoped Repository calls",
        "required_conditions": case.get("input", {}).get("required_conditions", []),
        "expected_business_result": case.get("expected_outcome", {}).get("business_outcome"),
        "hard_gates": case.get("expected_outcome", {}).get("hard_gates", []),
        "semantic_rubric": case.get("expected_outcome", {}).get("semantic_quality_rubric", {}),
        "review_status": case.get("input", {}).get("review_status"),
        "reviewer_id": case.get("input", {}).get("reviewer_id"),
        "allowed_capabilities": [x.get("tool_id") for x in case.get("input", {}).get("expected_tool_calls", [])
            if x.get("mode") == "read_only"],
        "capability_argument_sources": [], "repository_checks": [], "repository_reads": 0,
        "cleanup": "NOT_STARTED", "readiness": "PENDING"}
    try:
        plan = plan_case(bundle, case_id)
        row["owner_id"] = plan.get("owner_id")
        row["plan_case"] = "PASS"
    except Exception as exc:
        row.update({"plan_case": "FAIL", "readiness": "BLOCKED_CONTRACT_OR_DATA",
            "error_type": type(exc).__name__, "error": str(exc)[:240]})
        return row

    try:
        route = decide_route(plan["message"], available_capabilities=set(row["allowed_capabilities"]))
        route_dict = route.to_dict() if hasattr(route, "to_dict") else dict(route)
        row["rule_only_route"] = route_dict
        try:
            validate_case_route_scope(plan, route_dict)
            row["rule_route_scope"] = "PASS"
        except Exception as exc:
            row["rule_route_scope"] = "FAIL_CLOSED"
            row["rule_route_scope_reason"] = str(exc)
    except Exception as exc:
        row["rule_only_route"] = {"status": "UNKNOWN", "error_type": type(exc).__name__}
        row["rule_route_scope"] = "UNKNOWN"

    tools = row["allowed_capabilities"]
    if not tools:
        row.update({"readiness": "READY", "repository_reads": 0,
            "cleanup": "NO_FIXTURE_RECEIPT_REQUIRED",
            "execution_note": "No business fixture is loaded and no business read is allowed; runtime refusal/clarification remains unverified."})
        return row

    receipt = None
    prior: dict[str, Any] = {}
    products: list[dict[str, Any]] = []
    query = plan["message"]
    order_ids = re.findall(r"ORD-SYN-\d+", query.upper())
    order_id = order_ids[-1] if order_ids else None
    owner_id = plan["owner_id"]
    try:
        receipt = coordinator.prepare(plan)
        reads = coordinator.repositories(plan, receipt, maximum=PER_CASE_READ_CAP)
        for tool in tools:
            before = reads["store"].allowance.calls
            try:
                if tool in {"search_products", "recommend_products"}:
                    _explicit_ids, filters, _wants_stock = parse_product_request_many(query)
                    filters = reads["product"].resolve_product_filters(query, filters)
                    if tool == "search_products":
                        result = reads["product"].search_products(filters)
                        products = result.get("products") or []
                    else:
                        result = reads["product"].recommend_products(filters)
                        products = [r.get("product") for r in result.get("recommendations", [])]
                elif tool == "get_product_detail":
                    ids, _filters, _wants_stock = parse_product_request_many(query)
                    candidate_ids = list(ids) if ids else reads["product"].resolve_product_references(query)
                    if not candidate_ids and products:
                        candidate_ids = [p.get("product_id") for p in products if p and p.get("product_id")]
                    if not candidate_ids and "get_order_items" in prior:
                        candidate_ids = list(dict.fromkeys(i.get("product_id") for i in prior["get_order_items"].get("items", [])
                            if isinstance(i.get("product_id"), str)))
                    if not candidate_ids:
                        row.setdefault("argument_blockers", []).append({"tool": tool,
                            "reason": "No identifier can be derived from user input or a prior allowed result."})
                        continue
                    result = [reads["product"].get_product_detail(pid) for pid in candidate_ids]
                    result = {"source": "shoprelay_product_catalog", "products": [r.get("product") for r in result],
                        "availability": "AVAILABLE" if all(r.get("availability") == "AVAILABLE" for r in result) else "PARTIAL"}
                elif tool == "check_inventory":
                    ids, _filters, _wants_stock = parse_product_request_many(query)
                    candidate_ids = list(ids) if ids else reads["product"].resolve_product_references(query)
                    if not candidate_ids and products:
                        candidate_ids = [p.get("product_id") for p in products if p and p.get("product_id")]
                    if len(candidate_ids) != 1:
                        row.setdefault("argument_blockers", []).append({"tool": tool,
                            "reason": f"Expected one query/upstream product ID; got {len(candidate_ids)}."})
                        continue
                    result = reads["product"].check_inventory(candidate_ids[0])
                elif tool in {"get_order", "get_order_items", "track_shipment", "get_refund_status"}:
                    if not order_id:
                        row.setdefault("argument_blockers", []).append({"tool": tool,
                            "reason": "Exact order ID is absent from the user query."})
                        continue
                    repo = reads["order"] if tool != "get_refund_status" else reads["after_sales"]
                    result = getattr(repo, tool)(order_id, owner_id)
                elif tool == "search_knowledge":
                    result = reads["policy"].search(query, mode="BM25", result_ref=f"preflight-{case_id}")
                elif tool == "get_coupon":
                    # No code is inferred from an owner alias, fixture id, or expected result.
                    code_candidates = re.findall(r"(?<![A-Z0-9])[A-Z0-9][A-Z0-9_-]{2,63}(?![A-Z0-9])", query.upper())
                    code_candidates = [c for c in code_candidates if c not in {"SYN-USER-A", "SYN-USER-B"}
                        and ("coupon" in query.lower() or "优惠券" in query)]
                    if len(code_candidates) != 1:
                        row.setdefault("argument_blockers", []).append({"tool": tool,
                            "reason": "No unique coupon_code is present in the user input; no lookup performed."})
                        continue
                    result = reads["coupon"].get_coupon(code_candidates[0])
                elif tool == "get_warranty":
                    product_ids = re.findall(r"(?<![A-Z0-9])P-[A-Z0-9_-]{2,48}(?![A-Z0-9])", query.upper())
                    if not product_ids and "get_order_items" in prior:
                        product_ids = list(dict.fromkeys(i.get("product_id") for i in prior["get_order_items"].get("items", [])
                            if isinstance(i.get("product_id"), str)))
                    if len(set(product_ids)) != 1:
                        row.setdefault("argument_blockers", []).append({"tool": tool,
                            "reason": "A unique product_id cannot be derived from the query or prior allowed order-item result."})
                        continue
                    result = reads["policy"].search(f"warranty {product_ids[0]}", mode="BM25",
                        result_ref=f"preflight-{case_id}-warranty")
                else:
                    row.setdefault("argument_blockers", []).append({"tool": tool,
                        "reason": "Declared read capability has no exact offline adapter."})
                    continue

                # get_product_detail can represent a bounded comparison over every search result.
                if tool == "get_product_detail" and "products" in result:
                    result_for_archive = {"source": result["source"], "availability": result["availability"],
                        "products": [{k: p.get(k) for k in ("product_id", "name", "fixture_provenance", "recorded_attributes")}
                            for p in result["products"]]}
                    prior[tool] = {"products": result["products"]}
                else:
                    result_for_archive = _safe_result(tool, result)
                    prior[tool] = result
                after = reads["store"].allowance.calls
                row["repository_reads"] += after - before
                row["repository_checks"].append({"tool": tool, "argument_source": (
                    "USER_QUERY" if tool in {"get_order", "get_order_items", "track_shipment", "get_refund_status",
                        "search_knowledge", "get_coupon", "search_products", "recommend_products"} else
                    "PRIOR_ALLOWED_TOOL_RESULT_OR_USER_QUERY"), "result": result_for_archive})
            except Exception as exc:
                row.setdefault("tool_errors", []).append({"tool": tool, "error_type": type(exc).__name__,
                    "error_code": getattr(exc, "code", None) or str(exc)[:160]})
        # Count result lookups performed by get_product_detail even though it can return multiple rows.
        row["repository_reads"] = reads["store"].allowance.calls
        if row["repository_reads"] > PER_CASE_READ_CAP:
            row["readiness"] = "BLOCKED_ENGINEERING"
            row.setdefault("blocking_reasons", []).append("Per-case offline Repository read cap exceeded.")
        elif row.get("tool_errors") or row.get("argument_blockers"):
            row["readiness"] = "BLOCKED_CONTRACT_OR_DATA"
        else:
            row["readiness"] = "READY"
        row["fixture_rows_loaded"] = len(plan.get("facts", []))
        row["fixture_receipt"] = {"lease_id": receipt.lease_id, "database_identity": receipt.database_identity,
            "order_ids": list(receipt.order_ids), "fact_source_ids": [f["source_id"] for f in receipt.fact_receipts]}
    except Exception as exc:
        row.update({"readiness": "BLOCKED_ENGINEERING", "error_type": type(exc).__name__,
            "error": str(exc)[:240]})
    finally:
        if receipt is not None:
            try:
                coordinator.cleanup(receipt)
                proof = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
                row["cleanup"] = proof["cleanup_status"]
                row["cleanup_residual_rows"] = proof["residual_case_rows"]
                row["cleanup_lease_released"] = proof["lease_released"]
            except Exception as exc:
                row["cleanup"] = "FAILED"
                row["cleanup_error_type"] = type(exc).__name__
                row["readiness"] = "BLOCKED_ENGINEERING"
    return row


def build(root: Path = ROOT) -> dict[str, Any]:
    asset = root / ASSET
    raw = asset.read_bytes()
    if _sha(raw) != ASSET_SHA256:
        raise ValueError("D1_R324_DATASET_HASH_MISMATCH")
    bundle = json.loads(raw.decode("utf-8"))
    if len(bundle.get("cases", [])) != 40 or len({r.get("case_id") for r in bundle["cases"]}) != 40:
        raise ValueError("D1_R324_CASE_SET_INVALID")
    target = PersonalDevDatabase(root)
    engine, factory = target.open()
    coordinator = D1Coordinator(target, factory)
    try:
        with factory() as db:
            target.check_session(db)
            marker = db.execute(__import__("sqlalchemy").text(
                "SELECT profile,identity,path,lease_id,receipt FROM d1_personal_target")).one()
            if marker.lease_id is not None or marker.receipt is not None:
                raise ValueError("PERSONAL_DATABASE_ACTIVE_LEASE_OR_RECEIPT")
            preexisting = {name: bool(db.execute(__import__("sqlalchemy").select(model).limit(1)).first())
                for name, model in (("users", __import__("app.m1.models", fromlist=["User"]).User),
                    ("orders", __import__("app.m1.models", fromlist=["Order"]).Order),
                    ("shipments", __import__("app.m1.models", fromlist=["Shipment"]).Shipment),
                    ("runs", __import__("app.m1.models", fromlist=["Run"]).Run),
                    ("pending_actions", __import__("app.m1.models", fromlist=["PendingAction"]).PendingAction))}
            preexisting["fixture_facts"] = bool(db.execute(__import__("sqlalchemy").text(
                "SELECT 1 FROM d1_fixture_facts LIMIT 1")).first())
        if any(preexisting.values()):
            raise ValueError("PERSONAL_DATABASE_NOT_EMPTY_OR_HAS_RESIDUAL_FIXTURE_DATA")
        rows = [audit_case(coordinator, bundle, case) for case in bundle["cases"]]
        reads = sum(row.get("repository_reads", 0) for row in rows)
        if reads > TOTAL_READ_CAP:
            raise ValueError("TOTAL_OFFLINE_REPOSITORY_READ_CAP_EXCEEDED")
        with factory() as db:
            target.check_session(db)
            marker_after = db.execute(__import__("sqlalchemy").text(
                "SELECT lease_id,receipt FROM d1_personal_target")).one()
            postexisting = {name: bool(db.execute(__import__("sqlalchemy").select(model).limit(1)).first())
                for name, model in (("users", __import__("app.m1.models", fromlist=["User"]).User),
                    ("orders", __import__("app.m1.models", fromlist=["Order"]).Order),
                    ("shipments", __import__("app.m1.models", fromlist=["Shipment"]).Shipment),
                    ("runs", __import__("app.m1.models", fromlist=["Run"]).Run),
                    ("pending_actions", __import__("app.m1.models", fromlist=["PendingAction"]).PendingAction))}
            postexisting["fixture_facts"] = bool(db.execute(__import__("sqlalchemy").text(
                "SELECT 1 FROM d1_fixture_facts LIMIT 1")).first())
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["readiness"]] = counts.get(row["readiness"], 0) + 1
        return {"schema_version": "m6.2-r324-all40-repository-admission-r1",
            "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "classification": "ISOLATED_NO_MODEL_REPOSITORY_ADMISSION_ONLY",
            "dataset": {"path": ASSET.as_posix(), "version": bundle["version"], "sha256": _sha(raw),
                "case_count": len(rows)},
            "database": {"profile": "M6.1-D1-PERSONAL-DEV", "path": str(target.path),
                "identity": target.identity, "schema": "PERSONAL_DATABASE_SCHEMA_CHECKED",
                "policy_event_schema": "ACCEPTS_POLICY_RETRIEVAL_EVENTS", "preexisting_rows": preexisting,
                "post_probe_rows": postexisting, "lease_or_receipt_after": marker_after.lease_id is not None or marker_after.receipt is not None},
            "limits": {"per_case_reads": PER_CASE_READ_CAP, "total_reads": TOTAL_READ_CAP,
                "actual_repository_reads": reads, "agent_runs": 0, "provider_requests": 0,
                "judge_requests": 0, "embedding_requests": 0},
            "counts": counts, "case_count_conservation": sum(counts.values()) == 40,
            "cases": rows,
            "limitations": ["No Agent/application Run or real Provider path was invoked.",
                "Deterministic rule route is not an LLM route result.",
                "Repository reads establish source availability only; task answer quality remains unscored.",
                "No case outcome was converted to a formal review, Gold, Registry eligibility, or Frozen result."]}
    finally:
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    target = args.output.resolve(strict=False)
    if target.exists():
        raise SystemExit("OUTPUT_ALREADY_EXISTS")
    target.parent.mkdir(parents=True, exist_ok=True)
    result = build()
    data = (json.dumps(result, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    target.write_bytes(data)
    print(json.dumps({"output": str(target.relative_to(ROOT)), "sha256": _sha(data),
        "counts": result["counts"], "repository_reads": result["limits"]["actual_repository_reads"],
        "database_identity": result["database"]["identity"], "provider_requests": 0, "agent_runs": 0}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
