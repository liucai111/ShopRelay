"""Loopback-only browser fixture for M5.2-C2 controlled Action confirmation.

The process refuses any database except the existing shoprelay_m11_test,
temporarily resets only the two seeded demo credentials, prepares unique
SIMULATED_TEST_DATA rows and serves an explicitly gated test app on loopback.
On shutdown it restores credentials and removes only rows carrying its probe IDs.
No production/payment/MCP/external-support calls are made.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.models import (ActionAudit, AfterSalesEvent, AuthSession, ConversationSession,
    DemoCredential, GraphExecutionRecord, Inventory, Message, ObservabilityRecord, Order,
    OrderEvent, OrderInventoryReservation, OrderItem, PendingAction, Product, Refund,
    ReturnRequest, Run, RunEvent, SessionMemorySummary, SupportTicket, SupportTicketEvent,
    Trace, utc_now)
from app.m1.services import set_demo_password
from app.m4.actions import mark_unknown_result
from scripts.g2_test_database import resolve_test_database_url


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "probes" / "m5.2-c2-browser-e2e-20261024-r2.json"
TEMP_PREFIX = "M5C2SIM-"


def _utc(value: datetime | None = None) -> str:
    value = value or datetime.now(timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _save(record: dict) -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    safe_record = dict(record)
    safe_record.pop("auth_session_hashes", None)
    safe_record.pop("temporary_password_suffix", None)
    OUT.write_text(json.dumps(safe_record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _login(client: TestClient, user_id: str, password: str) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: csrf},
        json={"username": user_id, "password": password})
    if response.status_code != 200:
        raise RuntimeError("FIXTURE_LOGIN_FAILED")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _restore_demo_credentials(factory, saved: dict) -> str:
    """Restore only captured demo rows; fail closed by disabling only those rows."""
    if not saved:
        return "PASS"
    try:
        with factory() as db:
            for owner_id, values in saved.items():
                row = db.get(DemoCredential, owner_id)
                if row is not None:
                    row.password_hash, row.active, row.updated_at = values
            db.commit()
        return "PASS"
    except Exception:
        with factory() as db:
            db.execute(update(DemoCredential).where(
                DemoCredential.user_id.in_(list(saved))).values(active=False))
            db.commit()
        return "FAILED_DISABLED"


def _delete_messages_for_sessions(db, session_ids: list[str]) -> None:
    """Delete only probe messages through their real session foreign key."""
    if session_ids:
        db.execute(delete(Message).where(Message.session_id.in_(session_ids)))


def _prepare(app, factory, suffix: str, password: str, saved: dict,
             runtime: dict, record: dict) -> None:
    now = datetime.now(timezone.utc)
    day = now.date().isoformat()
    # Credential backups live only in process memory and are restored in finally.
    with factory() as db:
        for owner_id in ("u_demo", "u_other"):
            credential = db.get(DemoCredential, owner_id)
            if credential is None:
                raise RuntimeError("DEMO_CREDENTIAL_MISSING")
            saved[owner_id] = (credential.password_hash, credential.active, credential.updated_at)
            set_demo_password(db, owner_id, password)
        db.commit()
        product_id = f"M5C2SIM-PRODUCT-{suffix}"
        order_ids = {kind: f"M5C2SIM-{kind.upper()}-{suffix}" for kind in ("RETURN", "REFUND", "CANCEL")}
        record.update({"product_id": product_id, "orders": order_ids, "sessions": {}, "runs": {},
            "actions": {}, "extra_action_ids": {}})
        db.add(Product(product_id=product_id, name="隔离验收商品", category="ACCESSORY", price_cents=1999))
        db.flush()
        db.add(Inventory(product_id=product_id, available_qty=10, reserved_qty=1, version=1))
        db.add(Order(order_id=order_ids["RETURN"], user_id="u_demo", status="DELIVERED",
            paid_at=_utc(now - timedelta(days=2)), delivered_at=_utc(now - timedelta(days=2)),
            paid_cents=1999, version=1))
        db.add(Order(order_id=order_ids["REFUND"], user_id="u_demo", status="PAID",
            paid_at=_utc(now - timedelta(days=1)), delivered_at=None, paid_cents=1999, version=1))
        db.add(Order(order_id=order_ids["CANCEL"], user_id="u_demo", status="CREATED",
            paid_at=None, delivered_at=None, paid_cents=0, version=1))
        db.flush()
        db.add(OrderItem(order_id=order_ids["CANCEL"], product_id=product_id, quantity=1, paid_cents=0))
        db.flush()
        db.add(OrderInventoryReservation(reservation_id=f"M5C2SIM-RES-{suffix}",
            order_id=order_ids["CANCEL"], product_id=product_id, quantity=1, status="RESERVED",
            source_type="SIMULATED_TEST_DATA", source_ref=f"M5C2SIM-RES-SRC-{suffix}", created_at=now))
        db.commit()

    owner = TestClient(app, raise_server_exceptions=False)
    other = TestClient(app, raise_server_exceptions=False)
    owner_headers = _login(owner, "u_demo", password)
    other_headers = _login(other, "u_other", password)
    cookies = [owner.cookies.get("shoprelay_session"), other.cookies.get("shoprelay_session")]
    from hashlib import sha256
    runtime["auth_session_hashes"] = [sha256(str(item).encode()).hexdigest() for item in cookies if item]

    session_ids: dict[str, str] = {}
    run_ids: dict[str, str] = {}
    actions: dict[str, dict] = {}
    orders = {kind: f"M5C2SIM-{kind.upper()}-{suffix}" for kind in ("RETURN", "REFUND", "CANCEL")}

    def context(label: str, client: TestClient, headers: dict[str, str]) -> tuple[str, str]:
        created = client.post("/api/sessions", headers=headers, json={"title": f"M5.2-C2 {label}"})
        if created.status_code != 200:
            raise RuntimeError("FIXTURE_SESSION_CREATE_FAILED")
        sid = created.json()["session"]["session_id"]
        run = client.post(f"/api/sessions/{sid}/runs", headers=headers,
            json={"client_request_id": f"m5c2-{suffix}-{label}", "request_payload": {"probe_id": suffix}})
        if run.status_code != 200:
            raise RuntimeError("FIXTURE_RUN_CREATE_FAILED")
        rid = run.json()["run"]["run_id"]
        session_ids[label], run_ids[label] = sid, rid
        record.setdefault("sessions", {})[label] = sid
        record.setdefault("runs", {})[label] = rid
        return sid, rid

    def propose(label: str, tool_id: str, arguments: dict, target: str,
                client: TestClient = owner, headers: dict[str, str] = owner_headers) -> dict:
        sid, rid = context(label, client, headers)
        response = client.post("/api/actions", headers=headers, json={"tool_id": tool_id,
            "session_id": sid, "run_id": rid, "target_resource_id": target,
            "arguments": arguments, "idempotency_key": f"m5c2-{suffix}-{label}"})
        if response.status_code != 200:
            raise RuntimeError(f"FIXTURE_PROPOSAL_FAILED:{label}:{response.status_code}")
        value = response.json()["action"]
        actions[label] = {"action_id": value["action_id"], "session_id": sid,
            "run_id": rid, "status": value["status"], "tool_id": tool_id, "target": target}
        record.setdefault("actions", {})[label] = actions[label]
        _save({**record, "status": "PREPARING"})
        return value

    try:
        returned = propose("return", "create_return_request", {"order_id": orders["RETURN"],
            "reason_code": "QUALITY_ISSUE", "reason_summary": "隔离验收用申请说明"}, orders["RETURN"])
        refund = propose("refund", "create_refund_request", {"order_id": orders["REFUND"],
            "reason_code": "PRE_SHIPMENT", "reason_summary": "隔离验收用申请说明"}, orders["REFUND"])
        cancellation = propose("cancel", "cancel_order", {"order_id": orders["CANCEL"],
            "reason_code": "USER_REQUEST", "reason_summary": "隔离验收用取消说明"}, orders["CANCEL"])
        ticket = propose("ticket", "escalate_to_human", {"issue_category": "OTHER", "order_id": None,
            "user_description": "隔离验收的合成描述", "issue_summary": "验证人工升级工单状态",
            "reason_code": "M5C2_BROWSER_TEST", "related_agents": [], "verified_fact_refs": [],
            "unverified_claim_codes": [], "evidence_references": []}, "SUPPORT-GENERAL")
        # Additional true proposals exercise user rejection/cancellation and a
        # naturally expired confirmation attempt. They never execute business writes.
        rejected = propose("reject", "escalate_to_human", {"issue_category": "OTHER", "order_id": None,
            "user_description": "合成拒绝状态样例", "issue_summary": "拒绝状态验收",
            "reason_code": "M5C2_REJECT_TEST", "related_agents": [], "verified_fact_refs": [],
            "unverified_claim_codes": [], "evidence_references": []}, "SUPPORT-GENERAL")
        cancelled = propose("withdraw", "escalate_to_human", {"issue_category": "OTHER", "order_id": None,
            "user_description": "合成撤回状态样例", "issue_summary": "撤回状态验收",
            "reason_code": "M5C2_CANCEL_TEST", "related_agents": [], "verified_fact_refs": [],
            "unverified_claim_codes": [], "evidence_references": []}, "SUPPORT-GENERAL")
        expired = propose("expire", "escalate_to_human", {"issue_category": "OTHER", "order_id": None,
            "user_description": "合成过期状态样例", "issue_summary": "过期状态验收",
            "reason_code": "M5C2_EXPIRE_TEST", "related_agents": [], "verified_fact_refs": [],
            "unverified_claim_codes": [], "evidence_references": []}, "SUPPORT-GENERAL")
        # This uses the existing recovery transition after an isolated injected
        # uncertain executor state; no business transaction is performed.
        unknown = propose("unknown", "escalate_to_human", {"issue_category": "OTHER", "order_id": None,
            "user_description": "合成未知状态样例", "issue_summary": "未知结果恢复状态验收",
            "reason_code": "M5C2_UNKNOWN_TEST", "related_agents": [], "verified_fact_refs": [],
            "unverified_claim_codes": [], "evidence_references": []}, "SUPPORT-GENERAL")
        with factory() as db:
            expired_row = db.get(PendingAction, expired["action_id"])
            expired_row.expires_at = utc_now() - timedelta(seconds=1)
            db.commit()
        expired_response = owner.post(f"/api/actions/{expired['action_id']}/confirm", headers=owner_headers,
            json={"confirm": True, "arguments_hash": expired["arguments_hash"]})
        if expired_response.status_code != 410:
            raise RuntimeError("EXPIRED_CONFIRMATION_NOT_REJECTED")
        with factory() as db:
            unknown_row = db.get(PendingAction, unknown["action_id"])
            unknown_row.status = "EXECUTING"
            unknown_row.action_version += 1
            db.commit()
            mark_unknown_result(db, owner_id="u_demo", action_id=unknown["action_id"],
                                error_code="C2_TEST_EXECUTOR_RESULT_UNCERTAIN")
        record.update({"probe_id": suffix, "database": "shoprelay_m11_test",
            "database_revision": record.get("database_revision"),
            "simulated_source": "SIMULATED_TEST_DATA",
            "sessions": session_ids, "runs": run_ids, "actions": actions,
            "orders": orders, "product_id": f"M5C2SIM-PRODUCT-{suffix}",
            "extra_action_ids": {"reject": rejected["action_id"], "withdraw": cancelled["action_id"],
                "expire": expired["action_id"], "unknown": unknown["action_id"]},
            "preparation": {"status": "PASS", "proposals_created": len(actions),
                "business_transactions": 0, "expired_confirmation_http": expired_response.status_code,
                "unknown_result_fixture": "test-only transition injection; no executor/write call"},
            "counters": {"business_transactions": 0, "production_writes": 0,
                "payment_calls": 0, "external_support_calls": 0, "notifications": 0,
                "mcp_write_calls": 0, "deepseek_requests": 0},
            "browser": {"status": "NOT_RUN", "cases": {}},
            "cleanup": {"status": "PENDING"}})
        _save(record)
    finally:
        owner.close()
        other.close()


def _cleanup(factory, engine, suffix: str, saved: dict, runtime: dict, record: dict) -> None:
    from sqlalchemy import or_
    session_ids = list(record.get("sessions", {}).values())
    run_ids = list(record.get("runs", {}).values())
    action_ids = [item["action_id"] for item in record.get("actions", {}).values()]
    extra_ids = list(record.get("extra_action_ids", {}).values())
    action_ids.extend(extra_ids)
    trace_ids: list[str] = []
    order_ids = list(record.get("orders", {}).values())
    product_id = record.get("product_id")
    ticket_ids: list[str] = []
    counts: dict[str, int] = {}
    # Restore credentials first, in a dedicated transaction. A later fixture
    # deletion error must never leave the demo logins on probe credentials.
    credential_restore = _restore_demo_credentials(factory, saved)
    with factory() as db:
        if action_ids:
            trace_ids = list(db.execute(select(PendingAction.trace_id).where(
                PendingAction.action_id.in_(action_ids))).scalars())
            ticket_ids = list(db.execute(select(SupportTicket.ticket_id).where(
                SupportTicket.action_id.in_(action_ids))).scalars())
            if ticket_ids:
                db.execute(delete(SupportTicketEvent).where(SupportTicketEvent.ticket_id.in_(ticket_ids)))
            db.execute(delete(SupportTicket).where(SupportTicket.action_id.in_(action_ids)))
            db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
            db.execute(delete(PendingAction).where(PendingAction.action_id.in_(action_ids)))
        auth_session_hashes = runtime.get("auth_session_hashes", [])
        if auth_session_hashes:
            db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_session_hashes)))
        if order_ids:
            db.execute(delete(AfterSalesEvent).where(AfterSalesEvent.order_id.in_(order_ids)))
            db.execute(delete(ReturnRequest).where(ReturnRequest.order_id.in_(order_ids)))
            db.execute(delete(Refund).where(Refund.order_id.in_(order_ids)))
            db.execute(delete(OrderInventoryReservation).where(OrderInventoryReservation.order_id.in_(order_ids)))
            db.execute(delete(OrderItem).where(OrderItem.order_id.in_(order_ids)))
            db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(order_ids)))
            db.execute(delete(Order).where(Order.order_id.in_(order_ids)))
        if product_id:
            db.execute(delete(Inventory).where(Inventory.product_id == product_id))
            db.execute(delete(Product).where(Product.product_id == product_id))
        if run_ids:
            db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
            db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids)))
            db.execute(delete(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids)))
            db.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
            if trace_ids:
                db.execute(delete(Trace).where(Trace.trace_id.in_(trace_ids)))
            db.execute(delete(Run).where(Run.run_id.in_(run_ids)))
        if session_ids:
            db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.session_id.in_(session_ids)))
            _delete_messages_for_sessions(db, session_ids)
            db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(session_ids)))
        db.commit()
        for key, model, column, ids in (
            ("actions", PendingAction, PendingAction.action_id, action_ids),
            ("audits", ActionAudit, ActionAudit.action_id, action_ids),
            ("sessions", ConversationSession, ConversationSession.session_id, session_ids),
            ("runs", Run, Run.run_id, run_ids),
            ("orders", Order, Order.order_id, order_ids),
            ("tickets", SupportTicket, SupportTicket.action_id, action_ids),
            ("reservations", OrderInventoryReservation, OrderInventoryReservation.order_id, order_ids),
            ("returns", ReturnRequest, ReturnRequest.order_id, order_ids),
            ("refunds", Refund, Refund.order_id, order_ids),
        ):
            counts[key] = db.scalar(select(func.count()).select_from(model).where(column.in_(ids))) if ids else 0
        counts["products"] = db.scalar(select(func.count()).select_from(Product).where(Product.product_id == product_id)) if product_id else 0
        counts["auth_sessions"] = db.scalar(select(func.count()).select_from(AuthSession).where(
            AuthSession.token_hash.in_(runtime.get("auth_session_hashes", [])))) if runtime.get("auth_session_hashes") else 0
    record["cleanup"] = {"status": "PASS" if not any(counts.values()) and credential_restore == "PASS" else "PARTIAL",
        "residual_counts": counts, "credential_restore": credential_restore,
        "restored_demo_credentials": sorted(saved.keys()) if credential_restore == "PASS" else [],
        "removed_ticket_ids": ticket_ids}
    record.pop("auth_session_hashes", None)
    record.pop("temporary_password_suffix", None)
    record["finished_at"] = _utc()
    _save(record)


def main() -> int:
    if sys.platform != "win32":
        raise SystemExit("THIS_PROBE_IS_WINDOWS_LOOPBACK_ONLY")
    url = resolve_test_database_url()
    if url.get_backend_name() != "postgresql" or url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("UNSAFE_DATABASE_TARGET")
    database_url = url.render_as_string(hide_password=False)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    with engine.connect() as connection:
        database = connection.execute(text("select current_database()")).scalar_one()
        pg = connection.execute(text("select current_setting('server_version')")).scalar_one()
    if database != "shoprelay_m11_test":
        raise SystemExit("UNSAFE_DATABASE_TARGET")
    if os.environ.get("SHOPRELAY_M5_2_C2_ALLOW_ISOLATED_WRITES") != "1":
        raise SystemExit("SET_SHOPRELAY_M5_2_C2_ALLOW_ISOLATED_WRITES=1_TO_RUN")

    suffix = secrets.token_hex(6).upper()
    # The temporary password is deliberately independent of the public probe ID.
    password = secrets.token_urlsafe(24)
    saved: dict[str, tuple[str, bool, datetime]] = {}
    runtime: dict[str, list[str]] = {}
    record = {"schema_version": "shoprelay.m5.2-c2-browser-e2e.v1", "started_at": _utc(),
        "status": "RUNNING", "postgres_version": pg, "database": database,
        "migration_revision": current_revision(database_url), "database_revision": current_revision(database_url), "probe_id": suffix,
        "simulated_source": "SIMULATED_TEST_DATA", "browser": {"status": "NOT_RUN", "cases": {}},
        "cleanup": {"status": "PENDING"}}
    app = None
    try:
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
        app = create_app(database_url=database_url, engine=engine, action_writes_enabled=True,
            simulated_test_actions_enabled=True, return_actions_enabled=True,
            refund_actions_enabled=True, cancel_actions_enabled=True, ticket_actions_enabled=True,
            orchestrator_mode="legacy")
        if not all((app.state.action_writes_enabled, app.state.simulated_test_actions_enabled,
                    app.state.return_actions_enabled, app.state.refund_actions_enabled,
                    app.state.cancel_actions_enabled, app.state.ticket_actions_enabled)):
            raise RuntimeError("ISOLATED_ACTION_GATES_NOT_ENABLED")
        _prepare(app, factory, suffix, password, saved, runtime, record)
        record["status"] = "BROWSER_READY"
        record["server"] = {"host": "127.0.0.1", "port": 8000,
            "orchestrator": "legacy", "action_writes": "ISOLATED_SIMULATED_ONLY",
            "mcp_write": False, "production": False}
        _save(record)
        import uvicorn
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning", access_log=False)
        record["status"] = "SERVER_STOPPED"
        return 0
    finally:
        try:
            if saved:
                _cleanup(factory, engine, suffix, saved, runtime, record)
        finally:
            if app is not None:
                try:
                    app.state.engine.dispose()
                except Exception:
                    pass
            engine.dispose()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0)
