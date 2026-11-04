"""Bounded M4.3-B3 probe; only the existing shoprelay_m11_test database is accepted.

All rows carry SIMULATED_TEST_DATA provenance where the domain schema supports it;
fixture identifiers are unique and only those identifiers are removed in finally.
No payment, production, MCP or DeepSeek calls are made.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import secrets
import socket
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import LATEST_SCHEMA_REVISION, current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import (ActionAudit, AfterSalesEvent, AuthSession, ConversationSession, DemoCredential,
    Inventory, ObservabilityRecord, Order, OrderEvent, OrderInventoryReservation, OrderItem, PendingAction, Product, Refund,
    ReturnRequest, Run, RunEvent, Shipment, Trace, utc_now)
from app.m1.services import set_demo_password
from scripts.g2_test_database import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error_code(response) -> str | None:
    body = response.json()
    return body.get("code") or (body.get("detail") or {}).get("code")


def _serve(database_url: str, port: int) -> None:
    import os
    os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
    import uvicorn
    app = create_app(database_url=database_url, action_writes_enabled=True,
        cancel_actions_enabled=True, orchestrator_mode="legacy")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _http_confirm(port: int, cookie: str, csrf: str, action_id: str, arguments_hash: str) -> tuple[int, dict]:
    body = json.dumps({"confirm": True, "arguments_hash": arguments_hash}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/actions/{action_id}/confirm", data=body,
        method="POST", headers={"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{port}",
        CSRF_HEADER: csrf, "Cookie": f"shoprelay_session={cookie}; shoprelay_csrf={csrf}"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def probe() -> dict:
    started = datetime.now(timezone.utc)
    phase = "resolve_database"
    record = {"schema_version": "shoprelay.m4.3-b3-cancel-postgres.v2", "started_at": _utc(),
        "status": "NOT_RUN", "database_expected": "shoprelay_m11_test", "migration": {},
        "simulated_marker": "SIMULATED_TEST_DATA", "cancel_transactions": 0,
        "cancel_order_rows": 0, "refund_applications_created": 0, "payment_calls": 0,
        "production_writes": 0, "mcp_write_calls": 0, "deepseek_requests": 0,
        "agent_calls": 0, "cases": {}, "cleanup": {"status": "NOT_RUN"}}
    engine = factory = app = None
    clients = []
    servers = []
    saved_credential = None
    fixture_order_ids: list[str] = []
    reservations_before_cleanup = 0
    fixture_refund_ids: list[str] = []
    fixture_action_ids: list[str] = []
    fixture_session_ids: list[str] = []
    fixture_run_ids: list[str] = []
    fixture_auth_hashes: list[str] = []
    product_id = None
    original_inventory = None
    password = secrets.token_urlsafe(32)
    old_supervisor = __import__("os").environ.get("SHOPRELAY_M3_SUPERVISOR_ENABLED")
    try:
        import os
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
        phase = "database_connect"
        url = resolve_test_database_url()
        if url.get_backend_name() != "postgresql" or url.database != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        database_url = url.render_as_string(hide_password=False)
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        with engine.connect() as conn:
            record["database"] = conn.execute(text("select current_database()")).scalar_one()
            record["postgres_version"] = conn.execute(text("select current_setting('server_version')")).scalar_one()
        if record["database"] != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        phase = "migration"
        record["migration"]["before"] = current_revision(database_url)
        record["migration"]["expected"] = LATEST_SCHEMA_REVISION
        upgrade_database(database_url)
        record["migration"]["after"] = current_revision(database_url)
        if record["migration"]["after"] != LATEST_SCHEMA_REVISION:
            raise RuntimeError("MIGRATION_HEAD_MISMATCH")
        upgrade_database(database_url)
        record["migration"]["repeat_after"] = current_revision(database_url)
        record["migration"]["repeat_is_idempotent"] = (
            record["migration"]["repeat_after"] == record["migration"]["after"])
        if not record["migration"]["repeat_is_idempotent"]:
            raise RuntimeError("MIGRATION_REPEAT_MISMATCH")
        settings = load_settings(require_api_key=False)
        record["checkpointer_schema"] = settings.checkpoint_schema
        phase = "demo_credentials"
        with factory() as db:
            credential = db.get(DemoCredential, "u_demo")
            if credential is None:
                raise RuntimeError("DEMO_CREDENTIAL_MISSING")
            saved_credential = (credential.password_hash, credential.active, credential.updated_at)
            set_demo_password(db, "u_demo", password)
        suffix = secrets.token_hex(6)
        product_id = f"P-M43B3-{suffix}"
        names = ("unpaid", "paid", "shipped", "delivered", "other_owner", "open_refund",
                 "open_return", "rollback", "race", "missing_ledger")
        ids = {name: f"M43B3-{name.upper()}-{suffix}" for name in names}
        fixture_order_ids.extend(ids.values())
        fixture_session_ids.append(f"M43B3-SESSION-{suffix}")
        fixture_run_ids.append(f"M43B3-RUN-{suffix}")
        phase = "fixture_data"
        with factory() as db:
            db.add(Product(product_id=product_id, name="M4.3-B3 isolated fixture", category="ACCESSORY", price_cents=2599))
            db.flush()
            inventory = Inventory(product_id=product_id, available_qty=100,
                reserved_qty=sum(name != "missing_ledger" for name in names), version=1)
            original_inventory = (inventory.available_qty, inventory.reserved_qty, inventory.version)
            db.add(inventory)
            for name, order_id in ids.items():
                status = "CREATED" if name in {"unpaid", "other_owner", "rollback", "race"} else "PAID"
                if name in {"shipped", "delivered", "missing_ledger", "open_return"}:
                    status = {"shipped": "SHIPPED", "delivered": "DELIVERED",
                              "missing_ledger": "CREATED", "open_return": "CREATED"}[name]
                paid = status != "CREATED"
                db.add(Order(order_id=order_id, user_id="u_other" if name == "other_owner" else "u_demo",
                    status=status, paid_at="2026-09-20T10:00:00Z" if paid else None,
                    delivered_at="2026-10-01T10:00:00Z" if status == "DELIVERED" else None,
                    paid_cents=2599 if paid else 0, version=1))
                db.add(OrderItem(order_id=order_id, product_id=product_id, quantity=1,
                    paid_cents=2599 if paid else 0))
                db.flush()
                if name != "missing_ledger":
                    db.add(OrderInventoryReservation(reservation_id=f"M43B3-RES-{name}-{suffix}",
                        order_id=order_id, product_id=product_id, quantity=1, status="RESERVED",
                        source_type="SIMULATED_TEST_DATA", source_ref=f"M43B3-RES-SRC-{name}-{suffix}",
                        created_at=utc_now()))
            db.flush()
            db.add(Shipment(shipment_id=f"M43B3-SHIP-{suffix}", order_id=ids["shipped"],
                status="IN_TRANSIT", observed_at=_utc()))
            open_refund_id = f"M43B3-OPEN-REFUND-{suffix}"
            fixture_refund_ids.append(open_refund_id)
            db.add(Refund(refund_id=open_refund_id, order_id=ids["open_refund"], return_id=None,
                status="SUBMITTED", amount_cents=2599, reason="fixture conflict", created_at=_utc(),
                submitted_at=utc_now(), source_type="SIMULATED_TEST_DATA", source_ref=f"M43B3-OPEN-SRC-{suffix}"))
            db.add(ReturnRequest(return_id=f"M43B3-OPEN-RETURN-{suffix}", order_id=ids["open_return"],
                status="SUBMITTED", reason="fixture conflict", created_at=_utc(), submitted_at=utc_now(),
                source_type="SIMULATED_TEST_DATA", source_ref=f"M43B3-OPEN-RET-SRC-{suffix}"))
            db.add(ConversationSession(session_id=fixture_session_ids[0], user_id="u_demo",
                created_at=utc_now(), title="isolated B3 cancellation probe"))
            db.add(Run(run_id=fixture_run_ids[0], user_id="u_demo", session_id=fixture_session_ids[0],
                client_request_id=f"m43b3-{suffix}", idempotency_scope="m43b3-probe",
                request_hash="b" * 64, status="CREATED"))
            db.commit()
        app = create_app(database_url=database_url, engine=engine, action_writes_enabled=True,
            cancel_actions_enabled=True, orchestrator_mode="legacy")
        if not app.state.cancel_actions_enabled:
            raise RuntimeError("CANCEL_ACTION_ISOLATION_GATE_FAILED")
        record["gates"] = {"actions": app.state.action_writes_enabled,
            "cancel": app.state.cancel_actions_enabled, "production": False,
            "mcp_write": False, "payment": False, "orchestrator": "legacy"}
        phase = "proposal_and_auth"
        client = TestClient(app, raise_server_exceptions=False)
        clients.append(client)
        csrf_token = client.get("/api/demo/csrf").json()["csrf_token"]
        origin = str(client.base_url).rstrip("/")
        login = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: csrf_token},
            json={"username": "u_demo", "password": password})
        if login.status_code != 200:
            raise RuntimeError("DEMO_LOGIN_FAILED")
        session_cookie = client.cookies.get("shoprelay_session")
        csrf = client.cookies.get("shoprelay_csrf")
        fixture_auth_hashes.append(hashlib.sha256(session_cookie.encode()).hexdigest())
        headers = {"Origin": origin, CSRF_HEADER: csrf}
        session = client.post("/api/sessions", headers=headers, json={"title": "M4.3-B3 cancellation"})
        if session.status_code != 200:
            raise RuntimeError("SESSION_CREATE_FAILED")
        session_id = session.json()["session"]["session_id"]
        fixture_session_ids.append(session_id)
        run = client.post(f"/api/sessions/{session_id}/runs", headers=headers,
            json={"client_request_id": f"m43b3-live-{suffix}", "request_payload": {"purpose": "isolated_cancel"}})
        if run.status_code != 200:
            raise RuntimeError("RUN_CREATE_FAILED")
        run_id = run.json()["run"]["run_id"]
        fixture_run_ids.append(run_id)

        def propose(name: str, key: str) -> dict:
            response = client.post("/api/actions", headers=headers, json={"tool_id": "cancel_order",
                "session_id": session_id, "run_id": run_id, "target_resource_id": ids[name],
                "arguments": {"order_id": ids[name], "reason_code": "USER_REQUEST",
                    "reason_summary": "SIMULATED_TEST_DATA: controlled cancellation probe"},
                "idempotency_key": key})
            return response

        unpaid = propose("unpaid", f"m43b3-{suffix}-unpaid")
        if unpaid.status_code != 200:
            raise RuntimeError("UNPAID_PROPOSAL_FAILED")
        unpaid_action = unpaid.json()["action"]
        fixture_action_ids.append(unpaid_action["action_id"])
        competing_unpaid = propose("unpaid", f"m43b3-{suffix}-unpaid-second-action")
        if competing_unpaid.status_code != 200:
            raise RuntimeError("SECOND_ACTION_PROPOSAL_FAILED")
        competing_action = competing_unpaid.json()["action"]
        fixture_action_ids.append(competing_action["action_id"])
        with factory() as db:
            record["cases"]["unconfirmed_no_write"] = {
                "proposal_status": unpaid_action["status"],
                "order_status": db.get(Order, ids["unpaid"]).status,
                "inventory_reserved": db.get(Inventory, product_id).reserved_qty,
                "refund_rows": db.scalar(select(func.count()).select_from(Refund).where(Refund.order_id == ids["unpaid"]))}
        if record["cases"]["unconfirmed_no_write"]["order_status"] != "CREATED" or record["cases"]["unconfirmed_no_write"]["refund_rows"] != 0:
            raise RuntimeError("WRITE_BEFORE_CONFIRMATION")
        confirm = client.post(f"/api/actions/{unpaid_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": unpaid_action["arguments_hash"]})
        if confirm.status_code != 200:
            raise RuntimeError("UNPAID_CONFIRM_FAILED")
        record["cases"]["unpaid_cancel"] = {"http_status": confirm.status_code,
            "action_status": confirm.json()["action"]["status"],
            "business_effect": confirm.json().get("business_effect")}
        record["cancel_transactions"] += 1
        same_action_replay = client.post(f"/api/actions/{unpaid_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": unpaid_action["arguments_hash"]})
        different_action_confirm = client.post(f"/api/actions/{competing_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": competing_action["arguments_hash"]})
        with factory() as db:
            unpaid_events = db.scalar(select(func.count()).select_from(OrderEvent).where(
                OrderEvent.order_id == ids["unpaid"]))
        record["cases"]["idempotency"] = {"same_action_http": same_action_replay.status_code,
            "different_action_http": different_action_confirm.status_code,
            "different_action_error": _error_code(different_action_confirm),
            "order_event_count": unpaid_events}
        if (same_action_replay.status_code != 200 or different_action_confirm.status_code != 409 or
                _error_code(different_action_confirm) not in {
                    "CANCEL_NOT_ALLOWED", "ACTION_BUSINESS_FACTS_CHANGED"} or unpaid_events != 1):
            raise RuntimeError("ORDER_CANCEL_IDEMPOTENCY_FAILED")

        paid = propose("paid", f"m43b3-{suffix}-paid")
        if paid.status_code != 200:
            raise RuntimeError("PAID_PROPOSAL_FAILED")
        paid_action = paid.json()["action"]
        fixture_action_ids.append(paid_action["action_id"])
        with factory() as db:
            before_paid = db.get(Order, ids["paid"]).status
        paid_confirm = client.post(f"/api/actions/{paid_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": paid_action["arguments_hash"]})
        if paid_confirm.status_code != 200:
            raise RuntimeError("PAID_CONFIRM_FAILED")
        paid_result = paid_confirm.json()["action"]["result"]
        fixture_refund_ids.append(paid_result["refund_id"])
        record["cases"]["paid_cancel"] = {"order_before": before_paid,
            "order_after": paid_result["order_status"], "refund_status": paid_result["refund_status"],
            "approval_status": paid_result["refund_approval_status"],
            "payment_status": paid_result["payment_status"], "refund_id": paid_result["refund_id"],
            "policy_evidence": paid_result["policy_evidence"],
            "policy_evaluation_at": paid_result["policy_evaluation_at"]}
        record["cancel_transactions"] += 1
        record["refund_applications_created"] += 1

        for name, expected in (("shipped", "CANCEL_NOT_ALLOWED"), ("delivered", "CANCEL_NOT_ALLOWED"),
                               ("open_refund", "STATE_CONFLICT"), ("open_return", "STATE_CONFLICT"),
                               ("other_owner", "ORDER_NOT_FOUND")):
            attempt = propose(name, f"m43b3-{suffix}-{name}")
            code = _error_code(attempt)
            record["cases"][f"reject_{name}"] = {"status_code": attempt.status_code, "error_code": code}
            if attempt.status_code not in {404, 409} or (name != "other_owner" and code != expected):
                raise RuntimeError(f"{name.upper()}_SAFETY_CHECK_FAILED")

        with factory() as db:
            reserved_before_missing_ledger = db.get(Inventory, product_id).reserved_qty
        missing_ledger = propose("missing_ledger", f"m43b3-{suffix}-missing-ledger")
        missing_ledger_error = _error_code(missing_ledger)
        with factory() as db:
            record["cases"]["missing_order_reservation_ledger"] = {
                "http_status": missing_ledger.status_code, "error_code": missing_ledger_error,
                "order_status": db.get(Order, ids["missing_ledger"]).status,
                "inventory_reserved_before": reserved_before_missing_ledger,
                "inventory_reserved_after": db.get(Inventory, product_id).reserved_qty,
                "reservation_rows": db.scalar(select(func.count()).select_from(
                    OrderInventoryReservation).where(
                        OrderInventoryReservation.order_id == ids["missing_ledger"]))}
        if (missing_ledger.status_code != 409 or missing_ledger_error != "CANCEL_INVENTORY_FACTS_UNKNOWN" or
                record["cases"]["missing_order_reservation_ledger"]["inventory_reserved_before"] !=
                record["cases"]["missing_order_reservation_ledger"]["inventory_reserved_after"]):
            raise RuntimeError("MISSING_ORDER_RESERVATION_NOT_FAIL_CLOSED")

        phase = "transaction_rollback"
        rollback_proposal = propose("rollback", f"m43b3-{suffix}-rollback")
        if rollback_proposal.status_code != 200:
            raise RuntimeError("ROLLBACK_PROPOSAL_FAILED")
        rollback_action = rollback_proposal.json()["action"]
        fixture_action_ids.append(rollback_action["action_id"])
        with factory() as db:
            rollback_before = {"order_status": db.get(Order, ids["rollback"]).status,
                "inventory_reserved": db.get(Inventory, product_id).reserved_qty,
                "reservation_status": db.execute(select(OrderInventoryReservation.status).where(
                    OrderInventoryReservation.order_id == ids["rollback"])).scalar_one(),
                "refund_rows": db.scalar(select(func.count()).select_from(Refund).where(
                    Refund.order_id == ids["rollback"]))}
        injected = {"armed": True}
        def fail_order_event_insert(conn, cursor, statement, parameters, context, executemany):
            if injected["armed"] and statement.lstrip().lower().startswith("insert into order_events"):
                injected["armed"] = False
                raise RuntimeError("B3_INJECTED_ORDER_EVENT_FAILURE")
        event.listen(engine, "before_cursor_execute", fail_order_event_insert)
        try:
            rollback_response = client.post(f"/api/actions/{rollback_action['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": rollback_action["arguments_hash"]})
        finally:
            event.remove(engine, "before_cursor_execute", fail_order_event_insert)
        with factory() as db:
            rollback_after = {"order_status": db.get(Order, ids["rollback"]).status,
                "inventory_reserved": db.get(Inventory, product_id).reserved_qty,
                "reservation_status": db.execute(select(OrderInventoryReservation.status).where(
                    OrderInventoryReservation.order_id == ids["rollback"])).scalar_one(),
                "refund_rows": db.scalar(select(func.count()).select_from(Refund).where(
                    Refund.order_id == ids["rollback"])),
                "action_status": db.get(PendingAction, rollback_action["action_id"]).status,
                "order_event_rows": db.scalar(select(func.count()).select_from(OrderEvent).where(
                    OrderEvent.order_id == ids["rollback"]))}
        record["cases"]["transaction_rollback"] = {"http_status": rollback_response.status_code,
            "injection_fired": not injected["armed"], "before": rollback_before, "after": rollback_after}
        if (injected["armed"] or rollback_response.status_code < 500 or rollback_after["order_status"] != "CREATED" or
                rollback_after["inventory_reserved"] != rollback_before["inventory_reserved"] or
                rollback_after["reservation_status"] != "RESERVED" or rollback_after["refund_rows"] != 0 or
                rollback_after["action_status"] != "PENDING_CONFIRMATION" or rollback_after["order_event_rows"] != 0):
            raise RuntimeError("CANCEL_TRANSACTION_ROLLBACK_FAILED")

        phase = "parallel_confirmation"
        race_proposal = propose("race", f"m43b3-{suffix}-parallel")
        if race_proposal.status_code != 200:
            raise RuntimeError("PARALLEL_PROPOSAL_FAILED")
        race_action = race_proposal.json()["action"]
        fixture_action_ids.append(race_action["action_id"])
        ports = [_free_port(), _free_port()]
        context = multiprocessing.get_context("spawn")
        for port in ports:
            process = context.Process(target=_serve, args=(database_url, port), daemon=True)
            process.start(); servers.append(process)
        for _ in range(100):
            if all(_port_open(port) for port in ports):
                break
            time.sleep(0.1)
        if not all(_port_open(port) for port in ports):
            raise RuntimeError("ACTION_API_PROCESSES_NOT_READY")
        with ThreadPoolExecutor(max_workers=2) as pool:
            calls = [pool.submit(_http_confirm, port, session_cookie, csrf,
                race_action["action_id"], race_action["arguments_hash"]) for port in ports]
            responses = [future.result() for future in calls]
        with factory() as db:
            event_count = db.scalar(select(func.count()).select_from(OrderEvent).where(OrderEvent.order_id == ids["race"]))
            current = db.get(Order, ids["race"])
        record["cases"]["two_process_confirm"] = {"responses": [status for status, _ in responses],
            "order_status": current.status, "order_version": current.version, "order_event_count": event_count}
        record["cancel_transactions"] += 1
        if event_count != 1 or current.status != "CANCELLED" or any(status != 200 for status, _ in responses):
            raise RuntimeError("TWO_PROCESS_IDEMPOTENCY_FAILED")

        phase = "restart_and_audit"
        restarted = create_app(database_url=database_url, engine=engine,
            action_writes_enabled=True, cancel_actions_enabled=True, orchestrator_mode="legacy")
        with TestClient(restarted) as reader:
            reader.cookies.set("shoprelay_session", session_cookie)
            action_read = reader.get(f"/api/actions/{race_action['action_id']}")
        with factory() as db:
            order_events = db.execute(select(OrderEvent).where(OrderEvent.order_id.in_(ids.values()))).scalars().all()
            actions = db.execute(select(PendingAction).where(PendingAction.action_id.in_(fixture_action_ids))).scalars().all()
            audits = db.execute(select(ActionAudit).where(ActionAudit.action_id.in_(fixture_action_ids))).scalars().all()
            refunds = db.execute(select(Refund).where(Refund.order_id.in_(ids.values()))).scalars().all()
            runs = db.execute(select(Run).where(Run.run_id.in_(fixture_run_ids))).scalars().all()
            spans = db.execute(select(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(fixture_run_ids))).scalars().all()
            reservations = db.execute(select(OrderInventoryReservation).where(
                OrderInventoryReservation.order_id.in_(ids.values()))).scalars().all()
            record["cases"]["persisted"] = {"order_events": len(order_events),
                "order_transitions": [[e.from_status, e.to_status, e.from_version, e.to_version] for e in order_events],
                "order_event_details": [json.loads(e.details_json) for e in order_events],
                "reservation_statuses": {name: db.execute(select(OrderInventoryReservation.status).where(
                    OrderInventoryReservation.order_id == ids[name])).scalar_one_or_none() for name in ids},
                "inventory_reserved_after": db.get(Inventory, product_id).reserved_qty,
                "action_statuses": [a.status for a in actions],
                "audit_event_types": [a.event_type for a in audits], "refund_statuses": [r.status for r in refunds],
                "refund_payment_confirmations": sum(r.payment_confirmed_at is not None for r in refunds),
                "run_statuses": [r.status for r in runs], "observability_rows": len(spans),
                "restart_action_http": action_read.status_code}
        if action_read.status_code != 200 or len(order_events) != 3 or len([e for e in audits if e.event_type == "ORDER_CANCEL_TRANSACTION_COMMITTED"]) != 3:
            raise RuntimeError("PERSISTENCE_OR_AUDIT_MISMATCH")
        record["cancel_order_rows"] = len(order_events)
        # Compatibility alias retained for consumers of the earlier v1 probe.
        record["order_cancel_rows"] = len(order_events)
        record["elapsed_ms"] = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "BLOCKED_ENVIRONMENT" if phase in {"resolve_database", "database_connect"} else "FAIL"
        record["phase"] = phase
        record["error_type"] = type(exc).__name__
        # Do not serialize URLs, credentials, or raw database exception strings.
        record["error_code"] = f"B3_PROBE_{phase.upper()}_FAILED"
    finally:
        for process in servers:
            process.terminate(); process.join(timeout=6)
        if factory is not None:
            try:
                with factory() as db:
                    if fixture_order_ids:
                        db.execute(delete(AfterSalesEvent).where(AfterSalesEvent.order_id.in_(fixture_order_ids)))
                        db.execute(delete(OrderEvent).where(OrderEvent.order_id.in_(fixture_order_ids)))
                        db.execute(delete(Refund).where(Refund.order_id.in_(fixture_order_ids)))
                        db.execute(delete(ReturnRequest).where(ReturnRequest.order_id.in_(fixture_order_ids)))
                    if fixture_action_ids:
                        db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(fixture_action_ids)))
                        db.execute(delete(PendingAction).where(PendingAction.action_id.in_(fixture_action_ids)))
                    if fixture_run_ids:
                        db.execute(delete(Trace).where(Trace.run_id.in_(fixture_run_ids)))
                        db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(fixture_run_ids)))
                        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(fixture_run_ids)))
                        db.execute(delete(Run).where(Run.run_id.in_(fixture_run_ids)))
                    if fixture_session_ids:
                        db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(fixture_session_ids)))
                    if fixture_order_ids:
                        db.execute(delete(Shipment).where(Shipment.order_id.in_(fixture_order_ids)))
                        reservations_before_cleanup = db.scalar(select(func.count()).select_from(
                            OrderInventoryReservation).where(
                                OrderInventoryReservation.order_id.in_(fixture_order_ids))) or 0
                        db.execute(delete(OrderInventoryReservation).where(
                            OrderInventoryReservation.order_id.in_(fixture_order_ids)))
                        db.execute(delete(OrderItem).where(OrderItem.order_id.in_(fixture_order_ids)))
                        db.execute(delete(Order).where(Order.order_id.in_(fixture_order_ids)))
                    if product_id:
                        db.execute(delete(Inventory).where(Inventory.product_id == product_id))
                        db.execute(delete(Product).where(Product.product_id == product_id))
                    if fixture_auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(fixture_auth_hashes)))
                    if saved_credential:
                        credential = db.get(DemoCredential, "u_demo")
                        credential.password_hash, credential.active, credential.updated_at = saved_credential
                    db.commit()
                    # Verify only this probe's exact identifiers after commit. Any residue
                    # is a cleanup failure; never broaden deletion to unrelated rows.
                    residual = {
                        "orders": db.scalar(select(func.count()).select_from(Order).where(
                            Order.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "actions": db.scalar(select(func.count()).select_from(PendingAction).where(
                            PendingAction.action_id.in_(fixture_action_ids))) if fixture_action_ids else 0,
                        "order_reservations": db.scalar(select(func.count()).select_from(
                            OrderInventoryReservation).where(OrderInventoryReservation.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "refunds": db.scalar(select(func.count()).select_from(Refund).where(
                            Refund.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "returns": db.scalar(select(func.count()).select_from(ReturnRequest).where(
                            ReturnRequest.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "order_events": db.scalar(select(func.count()).select_from(OrderEvent).where(
                            OrderEvent.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "after_sales_events": db.scalar(select(func.count()).select_from(AfterSalesEvent).where(
                            AfterSalesEvent.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "runs": db.scalar(select(func.count()).select_from(Run).where(
                            Run.run_id.in_(fixture_run_ids))) if fixture_run_ids else 0,
                        "run_events": db.scalar(select(func.count()).select_from(RunEvent).where(
                            RunEvent.run_id.in_(fixture_run_ids))) if fixture_run_ids else 0,
                        "traces": db.scalar(select(func.count()).select_from(Trace).where(
                            Trace.run_id.in_(fixture_run_ids))) if fixture_run_ids else 0,
                        "observability": db.scalar(select(func.count()).select_from(ObservabilityRecord).where(
                            ObservabilityRecord.run_id.in_(fixture_run_ids))) if fixture_run_ids else 0,
                        "sessions": db.scalar(select(func.count()).select_from(ConversationSession).where(
                            ConversationSession.session_id.in_(fixture_session_ids))) if fixture_session_ids else 0,
                        "shipments": db.scalar(select(func.count()).select_from(Shipment).where(
                            Shipment.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "order_items": db.scalar(select(func.count()).select_from(OrderItem).where(
                            OrderItem.order_id.in_(fixture_order_ids))) if fixture_order_ids else 0,
                        "inventory_rows": db.scalar(select(func.count()).select_from(Inventory).where(
                            Inventory.product_id == product_id)) if product_id else 0,
                        "products": db.scalar(select(func.count()).select_from(Product).where(
                            Product.product_id == product_id)) if product_id else 0,
                        "auth_sessions": db.scalar(select(func.count()).select_from(AuthSession).where(
                            AuthSession.token_hash.in_(fixture_auth_hashes))) if fixture_auth_hashes else 0,
                    }
                    credential_restored = False
                    if saved_credential:
                        credential = db.get(DemoCredential, "u_demo")
                        credential_restored = (credential.password_hash, credential.active, credential.updated_at) == saved_credential
                cleanup_ok = all(value == 0 for value in residual.values()) and (
                    not saved_credential or credential_restored)
                record["cleanup"] = {"status": "PASS" if cleanup_ok else "FAIL", "exact_owned_ids": True,
                    "orders_removed": len(fixture_order_ids), "actions_removed": len(fixture_action_ids),
                    "order_reservations_removed": reservations_before_cleanup,
                    "refunds_removed": len(fixture_refund_ids), "product_removed": bool(product_id),
                    "demo_credentials_restored": credential_restored if saved_credential else True,
                    "residual_rows_by_type": residual}
                if not cleanup_ok:
                    record["status"] = "FAIL"
            except Exception as exc:
                record["cleanup"] = {"status": "FAIL", "error_type": type(exc).__name__}
                record["status"] = "FAIL"
        for client in clients:
            client.close()
        if engine is not None:
            engine.dispose()
        import os
        if old_supervisor is None:
            os.environ.pop("SHOPRELAY_M3_SUPERVISOR_ENABLED", None)
        else:
            os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = old_supervisor
        record["finished_at"] = _utc()
        record["elapsed_ms"] = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
        output = ROOT / "docs" / "probes" / f"m4.3-b3-cancel-postgres-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(3)}.json"
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"status": record["status"], "phase": record.get("phase"),
            "error_type": record.get("error_type"), "record": str(output.relative_to(ROOT))}, ensure_ascii=False))
    return record


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


if __name__ == "__main__":
    probe()
