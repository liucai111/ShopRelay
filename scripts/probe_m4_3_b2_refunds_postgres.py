"""Real M4.3-B2 confirmed refund-application transaction probe.

Uses only the existing shoprelay_m11_test database and SIMULATED_TEST_DATA rows.
It creates refund applications for human review; no payment or production write
is possible through this probe. All unique rows are removed in finally cleanup.
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
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import LATEST_SCHEMA_REVISION, current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import (ActionAudit, AfterSalesEvent, AuthSession, ConversationSession, DemoCredential,
    ObservabilityRecord, Order, PendingAction, Refund, ReturnRequest, Run, RunEvent, Shipment, Trace)
from app.m1.services import set_demo_password
from app.orchestration.checkpointer import checkpoint_schema_ready, run_checkpointer_async
from scripts.g2_test_database import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _login(client: TestClient, user_id: str, password: str) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: csrf},
        json={"username": user_id, "password": password})
    if response.status_code != 200:
        raise RuntimeError("DEMO_LOGIN_FAILED")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _context(client: TestClient, headers: dict[str, str], suffix: str) -> tuple[str, str]:
    response = client.post("/api/sessions", headers=headers, json={"title": "M4.3-B2 refund probe"})
    if response.status_code != 200:
        raise RuntimeError("SESSION_CREATE_FAILED")
    session_id = response.json()["session"]["session_id"]
    response = client.post(f"/api/sessions/{session_id}/runs", headers=headers,
        json={"client_request_id": f"m43b2-{suffix}", "request_payload": {"purpose": "isolated_refund_probe"}})
    if response.status_code != 200:
        raise RuntimeError("RUN_CREATE_FAILED")
    return session_id, response.json()["run"]["run_id"]


def _payload(order_id: str, session_id: str, run_id: str, reason_code: str, key: str) -> dict:
    return {"tool_id": "create_refund_request", "session_id": session_id, "run_id": run_id,
        "target_resource_id": order_id,
        "arguments": {"order_id": order_id, "reason_code": reason_code,
                      "reason_summary": "隔离退款申请测试，提交后等待人工审核"},
        "idempotency_key": key}


def _serve_refund_api(database_url: str, port: int) -> None:
    import os
    os.environ["SHOPRELAY_ORCHESTRATOR"] = "legacy"
    os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
    import uvicorn
    app = create_app(database_url=database_url, action_writes_enabled=True,
        refund_actions_enabled=True, orchestrator_mode="legacy")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)


def _http_confirm(args: tuple[str, int, str, str, str, str]) -> tuple[int, dict]:
    host, port, cookie, csrf, action_id, args_hash = args
    body = json.dumps({"confirm": True, "arguments_hash": args_hash}).encode()
    req = urllib.request.Request(f"http://{host}:{port}/api/actions/{action_id}/confirm", data=body,
        method="POST", headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}",
            "x-csrf-token": csrf,
            "Cookie": f"shoprelay_session={cookie}; shoprelay_csrf={csrf}"})
    try:
        with urllib.request.urlopen(req, timeout=45) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def probe() -> dict:
    start = datetime.now(timezone.utc)
    phase = "environment"
    record = {"schema_version": "shoprelay.m4.3-b2-refund-postgres.v1", "started_at": _utc(),
        "status": "NOT_RUN", "database_expected": "shoprelay_m11_test", "migration": {},
        "checkpointer_ready": False, "simulated_marker": "SIMULATED_TEST_DATA",
        "refund_transaction_commits": 0, "observed_refund_rows": 0,
        "production_writes": 0, "payment_calls": 0, "mcp_write_calls": 0,
        "deepseek_requests": 0, "cases": {}, "cleanup": {"status": "NOT_RUN"}}
    engine = factory = None
    server_processes: list[multiprocessing.Process] = []
    saved_credentials: dict[str, dict] = {}
    auth_hashes: set[str] = set()
    action_ids: list[str] = []
    session_ids: list[str] = []
    run_ids: list[str] = []
    order_ids: list[str] = []
    return_ids: list[str] = []
    refund_ids: list[str] = []
    database_url = None
    app = None
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
        with engine.connect() as connection:
            record["database"] = connection.execute(text("select current_database()")).scalar_one()
            record["postgres_version"] = connection.execute(text("select current_setting('server_version')")).scalar_one()
        if record["database"] != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        phase = "migration"
        record["migration"] = {"before": current_revision(database_url), "expected": LATEST_SCHEMA_REVISION}
        upgrade_database(database_url)
        record["migration"]["after"] = current_revision(database_url)
        if record["migration"]["after"] != LATEST_SCHEMA_REVISION:
            raise RuntimeError("MIGRATION_HEAD_MISMATCH")
        phase = "checkpointer"
        settings = load_settings(require_api_key=False)
        record["checkpointer_schema"] = settings.checkpoint_schema
        record["checkpointer_ready"] = bool(run_checkpointer_async(
            checkpoint_schema_ready(database_url, settings.checkpoint_schema)))
        if not record["checkpointer_ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")
        with factory() as db:
            for user_id in ("u_demo", "u_other"):
                credential = db.get(DemoCredential, user_id)
                if credential is None:
                    raise RuntimeError("DEMO_CREDENTIAL_MISSING")
                saved_credentials[user_id] = {"password_hash": credential.password_hash,
                    "active": credential.active, "updated_at": credential.updated_at}
                set_demo_password(db, user_id, password)
        app = create_app(database_url=database_url, engine=engine, action_writes_enabled=True,
            refund_actions_enabled=True, orchestrator_mode="legacy")
        record["write_gates"] = {"actions": app.state.action_writes_enabled,
            "refunds": app.state.refund_actions_enabled, "database_name_gate": True,
            "mcp_write": False, "production": False}
        if not app.state.refund_actions_enabled:
            raise RuntimeError("REFUND_ACTION_ISOLATION_GATE_FAILED")

        suffix = secrets.token_hex(6)
        names = ("paid", "delivered", "shipped", "pending", "rollback", "expired", "rejected", "cancelled", "other_owner")
        ids = {name: f"M43B2-{name.upper()}-{suffix}" for name in names}
        order_ids.extend(ids.values())
        with factory() as db:
            for name, order_id in ids.items():
                status = "DELIVERED" if name in ("delivered", "pending") else "SHIPPED" if name == "shipped" else "PAID"
                db.add(Order(order_id=order_id, user_id="u_other" if name == "other_owner" else "u_demo",
                    status=status, paid_at="2026-09-20T10:00:00Z",
                    delivered_at="2026-10-01T10:00:00Z" if status == "DELIVERED" else None,
                    paid_cents=1299, version=1))
            db.flush()
            db.add(Shipment(shipment_id=f"M43B2-SHIP-{suffix}", order_id=ids["shipped"],
                status="IN_TRANSIT", observed_at="2026-10-01T12:00:00Z"))
            approved_id = f"M43B2-RETURN-APPROVED-{suffix}"
            pending_id = f"M43B2-RETURN-PENDING-{suffix}"
            return_ids.extend((approved_id, pending_id))
            approval_time = datetime.now(timezone.utc) - timedelta(minutes=4)
            db.add(ReturnRequest(return_id=approved_id, order_id=ids["delivered"], status="APPROVED",
                reason="SIMULATED_TEST_DATA", created_at=_utc(), submitted_at=approval_time - timedelta(hours=1),
                approved_at=approval_time, source_type="SIMULATED_TEST_DATA", source_ref=f"M43B2-APPROVAL-{suffix}"))
            db.add(ReturnRequest(return_id=pending_id, order_id=ids["pending"], status="UNDER_REVIEW",
                reason="SIMULATED_TEST_DATA", created_at=_utc(), submitted_at=approval_time - timedelta(hours=1),
                review_started_at=approval_time - timedelta(minutes=20), source_type="SIMULATED_TEST_DATA",
                source_ref=f"M43B2-PENDING-{suffix}"))
            db.flush()
            db.add(AfterSalesEvent(event_id=f"M43B2-APPROVAL-EVENT-{suffix}", order_id=ids["delivered"],
                return_id=approved_id, refund_id=None, event_type="RETURN_APPROVED", occurred_at=approval_time,
                recorded_at=approval_time, actor_type="SIMULATED_TEST_DATA", source_type="SIMULATED_TEST_DATA",
                source_ref=f"M43B2-APPROVAL-{suffix}", details_json=json.dumps({"probe_id": suffix})))
            db.commit()

        phase = "proposal_and_context"
        with TestClient(app, raise_server_exceptions=False) as owner, TestClient(app, raise_server_exceptions=False) as other:
            headers = _login(owner, "u_demo", password)
            other_headers = _login(other, "u_other", password)
            auth_hashes.update(hashlib.sha256(owner.cookies.get("shoprelay_session").encode()).hexdigest()
                for _ in (0,))
            auth_hashes.add(hashlib.sha256(other.cookies.get("shoprelay_session").encode()).hexdigest())
            session_id, run_id = _context(owner, headers, suffix)
            session_ids.append(session_id); run_ids.append(run_id)
            primary_payload = _payload(ids["paid"], session_id, run_id, "PRE_SHIPMENT", f"m43b2-main-{suffix}")
            created = owner.post("/api/actions", headers=headers, json=primary_payload)
            if created.status_code != 200:
                raise RuntimeError("REFUND_PENDING_CREATE_FAILED:" + str(created.json().get("code")))
            primary = created.json()["action"]
            action_ids.append(primary["action_id"])
            with factory() as db:
                pending_count = db.scalar(select(func.count()).select_from(Refund).where(Refund.order_id == ids["paid"]))
            record["cases"]["unconfirmed_no_write"] = {"action_status": primary["status"],
                "refund_rows": pending_count, "business_effect": created.json()["business_effect"],
                "amount_cents_derived": primary["sanitized_arguments"]["amount_cents"],
                "knowledge_version": primary["sanitized_arguments"]["policy_snapshot"]["knowledge_version"],
                "policy_chunk_versions": primary["sanitized_arguments"]["policy_snapshot"]["evidence"]}
            if primary["status"] != "PENDING_CONFIRMATION" or pending_count != 0:
                raise RuntimeError("REFUND_WRITTEN_BEFORE_CONFIRMATION")

            phase = "owner_and_hash_guards"
            record["cases"]["forged_user_id"] = owner.post("/api/actions", headers=headers,
                json={**primary_payload, "user_id": "u_other"}).status_code
            record["cases"]["cross_user_get"] = other.get(f"/api/actions/{primary['action_id']}").status_code
            record["cases"]["cross_user_confirm"] = other.post(
                f"/api/actions/{primary['action_id']}/confirm", headers=other_headers,
                json={"confirm": True, "arguments_hash": primary["arguments_hash"]}).status_code
            record["cases"]["other_owner_order"] = owner.post("/api/actions", headers=headers,
                json=_payload(ids["other_owner"], session_id, run_id, "PRE_SHIPMENT", f"m43b2-owner-{suffix}")).json().get("code")
            bad_hash = owner.post(f"/api/actions/{primary['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": "0" * 64})
            record["cases"]["hash_mismatch"] = {"status": bad_hash.status_code, "code": bad_hash.json().get("code")}
            no_csrf = owner.post(f"/api/actions/{primary['action_id']}/confirm",
                json={"confirm": True, "arguments_hash": primary["arguments_hash"]})
            record["cases"]["no_csrf"] = no_csrf.status_code
            if (record["cases"]["forged_user_id"] != 422 or record["cases"]["cross_user_get"] != 404 or
                record["cases"]["cross_user_confirm"] != 404 or record["cases"]["other_owner_order"] != "ORDER_NOT_FOUND" or
                bad_hash.status_code != 409 or no_csrf.status_code != 403):
                raise RuntimeError("REFUND_IDENTITY_OR_CONFIRMATION_GUARD_FAILED")

            phase = "business_paths"
            delivered = owner.post("/api/actions", headers=headers, json=_payload(ids["delivered"], session_id,
                run_id, "APPROVED_RETURN", f"m43b2-delivered-{suffix}"))
            if delivered.status_code != 200:
                raise RuntimeError("APPROVED_RETURN_REFUND_PROPOSAL_FAILED:" + str(delivered.json().get("code")))
            delivered_action = delivered.json()["action"]; action_ids.append(delivered_action["action_id"])
            record["cases"]["delivered_approved_return_proposal"] = {
                "status": delivered_action["status"],
                "return_id": delivered_action["sanitized_arguments"].get("return_id"),
                "approval_event_id": delivered_action["sanitized_arguments"]["fact_snapshot"].get("approval_event_id")}
            pending_return = owner.post("/api/actions", headers=headers, json=_payload(ids["pending"], session_id,
                run_id, "OTHER", f"m43b2-pending-{suffix}"))
            shipped = owner.post("/api/actions", headers=headers, json=_payload(ids["shipped"], session_id,
                run_id, "OTHER", f"m43b2-shipped-{suffix}"))
            record["cases"]["pending_return"] = {"status": pending_return.status_code,
                "code": pending_return.json().get("code")}
            record["cases"]["shipped_manual_review"] = {"status": shipped.status_code,
                "code": shipped.json().get("code")}
            if pending_return.status_code != 409 or pending_return.json().get("code") != "RETURN_NOT_APPROVED" or \
                    shipped.status_code != 409 or shipped.json().get("code") != "REFUND_REQUIRES_MANUAL_REVIEW":
                raise RuntimeError("REFUND_POLICY_BRANCHING_FAILED")

            phase = "two_process_confirmation"
            ports = [_port(), _port()]
            for port in ports:
                process = multiprocessing.get_context("spawn").Process(
                    target=_serve_refund_api, args=(database_url, port), daemon=True)
                process.start(); server_processes.append(process)
            for port in ports:
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("REFUND_CONFIRMATION_WORKER_START_TIMEOUT")
            confirm_args = [("127.0.0.1", port, owner.cookies.get("shoprelay_session"),
                owner.cookies.get("shoprelay_csrf"), primary["action_id"], primary["arguments_hash"]) for port in ports]
            with ThreadPoolExecutor(max_workers=2) as pool:
                race_results = list(pool.map(_http_confirm, confirm_args))
            record["cases"]["two_process_confirmation"] = [{"http_status": status,
                "replayed": body.get("replayed"), "action_status": body.get("action", {}).get("status"),
                "business_effect": body.get("business_effect"), "payment_effect": body.get("payment_effect"),
                "run_status": body.get("run_status")} for status, body in race_results]
            if any(status != 200 for status, _ in race_results) or sum(not body.get("replayed", False)
                    for _, body in race_results) != 1:
                raise RuntimeError("REFUND_TWO_PROCESS_IDEMPOTENCY_FAILED")
            for process in server_processes:
                process.terminate(); process.join(timeout=8)
            server_processes.clear()
            primary_result = next(body["action"]["result"] for _, body in race_results)
            primary_refund = primary_result["refund_id"]
            refund_ids.append(primary_refund)
            record["cases"]["paid_unshipped_result"] = {"refund_id": primary_refund,
                "status": primary_result["status"], "business_status": primary_result["business_status"],
                "approval_status": primary_result["approval_status"], "payment_status": primary_result["payment_status"],
                "amount_cents": primary_result["amount_cents"],
                "policy_evidence": primary_result["policy_evidence"]}
            if primary_result["status"] != "SUBMITTED" or primary_result["approval_status"] != "PENDING_REVIEW" or \
                    primary_result["payment_status"] != "NOT_EXECUTED":
                raise RuntimeError("REFUND_APPLICATION_STATUS_INVALID")

            phase = "delivered_approved_return_transaction"
            delivered_confirm = owner.post(f"/api/actions/{delivered_action['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": delivered_action["arguments_hash"]})
            if delivered_confirm.status_code != 200:
                raise RuntimeError("DELIVERED_REFUND_CONFIRM_FAILED:" + str(delivered_confirm.json().get("code")))
            delivered_result = delivered_confirm.json()["action"]["result"]
            refund_ids.append(delivered_result["refund_id"])
            record["cases"]["approved_return_refund_result"] = {
                "refund_id": delivered_result["refund_id"], "status": delivered_result["status"],
                "return_id": delivered_result["return_id"], "payment_status": delivered_result["payment_status"]}

            phase = "same_action_and_business_duplicate"
            replay = owner.post(f"/api/actions/{primary['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": primary["arguments_hash"]})
            proposal_replay = owner.post("/api/actions", headers=headers, json=primary_payload)
            duplicate = owner.post("/api/actions", headers=headers,
                json=_payload(ids["paid"], session_id, run_id, "PRE_SHIPMENT", f"m43b2-other-action-{suffix}"))
            record["cases"]["idempotent_replay"] = {"confirm_status": replay.status_code,
                "confirm_replayed": replay.json().get("replayed"), "proposal_status": proposal_replay.status_code,
                "proposal_replayed": proposal_replay.json().get("replayed"),
                "second_action": {"status": duplicate.status_code, "code": duplicate.json().get("code")}}
            if replay.status_code != 200 or not replay.json().get("replayed") or proposal_replay.status_code != 200 or \
                    not proposal_replay.json().get("replayed") or duplicate.status_code != 409 or \
                    duplicate.json().get("code") != "REFUND_ALREADY_OPEN":
                raise RuntimeError("REFUND_IDEMPOTENCY_OR_BUSINESS_UNIQUENESS_FAILED")

            phase = "rollback_expiry_reject_cancel"
            rollback_proposal = owner.post("/api/actions", headers=headers,
                json=_payload(ids["rollback"], session_id, run_id, "PRE_SHIPMENT", f"m43b2-rb-{suffix}"))
            rollback_action = rollback_proposal.json()["action"]; action_ids.append(rollback_action["action_id"])
            def fail_event(_conn, _cursor, statement, _params, _context, _many):
                if statement.lstrip().upper().startswith("INSERT INTO AFTER_SALES_EVENTS"):
                    raise RuntimeError("INJECTED_REFUND_TRANSACTION_FAILURE")
            event.listen(engine, "before_cursor_execute", fail_event)
            try:
                rolled = owner.post(f"/api/actions/{rollback_action['action_id']}/confirm", headers=headers,
                    json={"confirm": True, "arguments_hash": rollback_action["arguments_hash"]})
            finally:
                event.remove(engine, "before_cursor_execute", fail_event)
            with factory() as db:
                refund_count = db.scalar(select(func.count()).select_from(Refund).where(Refund.order_id == ids["rollback"]))
                action_state = db.get(PendingAction, rollback_action["action_id"]).status
            record["cases"]["transaction_rollback"] = {"http_status": rolled.status_code,
                "refund_rows": refund_count, "action_status": action_state}
            if rolled.status_code != 500 or refund_count != 0 or action_state != "PENDING_CONFIRMATION":
                raise RuntimeError("REFUND_TRANSACTION_ROLLBACK_FAILED")

            expired_proposal = owner.post("/api/actions", headers=headers,
                json=_payload(ids["expired"], session_id, run_id, "PRE_SHIPMENT", f"m43b2-exp-{suffix}"))
            expired_action = expired_proposal.json()["action"]; action_ids.append(expired_action["action_id"])
            with factory() as db:
                db.get(PendingAction, expired_action["action_id"]).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                db.commit()
            expired = owner.post(f"/api/actions/{expired_action['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": expired_action["arguments_hash"]})
            rejected_proposal = owner.post("/api/actions", headers=headers,
                json=_payload(ids["rejected"], session_id, run_id, "PRE_SHIPMENT", f"m43b2-rej-{suffix}"))
            rejected_action = rejected_proposal.json()["action"]; action_ids.append(rejected_action["action_id"])
            rejected = owner.post(f"/api/actions/{rejected_action['action_id']}/reject", headers=headers,
                json={"acknowledge": True})
            rejected_confirm = owner.post(f"/api/actions/{rejected_action['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": rejected_action["arguments_hash"]})
            cancelled_proposal = owner.post("/api/actions", headers=headers,
                json=_payload(ids["cancelled"], session_id, run_id, "PRE_SHIPMENT", f"m43b2-cancel-{suffix}"))
            cancelled_action = cancelled_proposal.json()["action"]; action_ids.append(cancelled_action["action_id"])
            cancelled = owner.post(f"/api/actions/{cancelled_action['action_id']}/cancel", headers=headers,
                json={"acknowledge": True})
            cancelled_confirm = owner.post(f"/api/actions/{cancelled_action['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": cancelled_action["arguments_hash"]})
            record["cases"]["closed_action_guards"] = {"expired": [expired.status_code, expired.json().get("code")],
                "rejected": [rejected.status_code, rejected_confirm.status_code],
                "cancelled": [cancelled.status_code, cancelled_confirm.status_code]}
            if expired.status_code != 410 or rejected.status_code != 200 or rejected_confirm.status_code != 409 or \
                    cancelled.status_code != 200 or cancelled_confirm.status_code != 409:
                raise RuntimeError("CLOSED_ACTION_GUARD_FAILED")

            phase = "restore_and_audit"
            rebuilt = create_app(database_url=database_url, engine=engine,
                action_writes_enabled=True, refund_actions_enabled=True, orchestrator_mode="legacy")
            with TestClient(rebuilt) as restarted:
                _login(restarted, "u_demo", password)
                action_read = restarted.get(f"/api/actions/{primary['action_id']}")
                run_read = restarted.get(f"/api/runs/{run_id}")
                record["cases"]["restart_recovery"] = {"action_status": action_read.json()["action"]["status"],
                    "run_status": run_read.json()["run"]["status"],
                    "refund_id": action_read.json()["action"]["result"]["refund_id"]}
                if action_read.status_code != 200 or run_read.status_code != 200:
                    raise RuntimeError("REFUND_RESTART_RECOVERY_FAILED")
            with factory() as db:
                refunds = db.execute(select(Refund).where(Refund.refund_id.in_(refund_ids))).scalars().all()
                events = db.execute(select(AfterSalesEvent).where(AfterSalesEvent.refund_id.in_(refund_ids))).scalars().all()
                record["cases"]["persisted_database"] = {"refund_count": len(refunds),
                    "statuses": [row.status for row in refunds],
                    "sources": [row.source_type for row in refunds],
                    "payment_confirmed_count": sum(row.payment_confirmed_at is not None for row in refunds),
                    "payment_confirmation_source_count": sum(row.payment_confirmation_source is not None for row in refunds),
                    "refund_event_count": len(events),
                    "event_types": [row.event_type for row in events],
                    "event_provenance_has_policy": all("policy_chunk_versions" in json.loads(row.details_json) for row in events)}
                run = db.get(Run, run_id)
                record["cases"]["persisted_database"]["run_status"] = run.status
                record["cases"]["persisted_database"]["audit_event_types"] = db.execute(
                    select(ActionAudit.event_type).where(ActionAudit.action_id.in_(action_ids))).scalars().all()
                record["cases"]["persisted_database"]["run_event_types"] = db.execute(
                    select(RunEvent.event_type).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)).scalars().all()
                record["cases"]["persisted_database"]["trace_count"] = db.scalar(
                    select(func.count()).select_from(Trace).where(Trace.run_id == run_id))
                record["cases"]["persisted_database"]["observability_types"] = db.execute(
                    select(ObservabilityRecord.record_type).where(ObservabilityRecord.run_id == run_id)
                ).scalars().all()
                if len(refunds) != 2 or any(row.status != "SUBMITTED" or row.source_type != "SIMULATED_TEST_DATA"
                    or row.payment_confirmed_at is not None or row.payment_confirmation_source is not None for row in refunds):
                    raise RuntimeError("REFUND_PERSISTED_STATUS_INVALID")
                if run.status != "CREATED":
                    raise RuntimeError("RUN_STATUS_WAS_FALSIFIED")
            record["refund_transaction_commits"] = 2
            record["observed_refund_rows"] = 2
            record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["error_type"] = type(exc).__name__
        record["error_code"] = f"PROBE_FAILED_{phase.upper()}"
    finally:
        for process in server_processes:
            process.terminate(); process.join(timeout=8)
        if factory is not None:
            try:
                with factory() as db:
                    if order_ids:
                        db.execute(delete(AfterSalesEvent).where(AfterSalesEvent.order_id.in_(order_ids)))
                        db.execute(delete(Refund).where(Refund.order_id.in_(order_ids)))
                        db.execute(delete(ReturnRequest).where(ReturnRequest.order_id.in_(order_ids)))
                    if action_ids:
                        db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
                        db.execute(delete(PendingAction).where(PendingAction.action_id.in_(action_ids)))
                    if run_ids:
                        db.execute(delete(Trace).where(Trace.run_id.in_(run_ids)))
                        db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids)))
                        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                        db.execute(delete(Run).where(Run.run_id.in_(run_ids)))
                    if session_ids:
                        db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(session_ids)))
                    if order_ids:
                        db.execute(delete(Shipment).where(Shipment.order_id.in_(order_ids)))
                        db.execute(delete(Order).where(Order.order_id.in_(order_ids)))
                    if auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_hashes)))
                    for user_id, values in saved_credentials.items():
                        credential = db.get(DemoCredential, user_id)
                        credential.password_hash = values["password_hash"]
                        credential.active = values["active"]
                        credential.updated_at = values["updated_at"]
                    db.commit()
                with factory() as db:
                    counts = {"orders": db.scalar(select(func.count()).select_from(Order).where(
                        Order.order_id.in_(order_ids))) if order_ids else 0,
                        "refunds": db.scalar(select(func.count()).select_from(Refund).where(
                            Refund.order_id.in_(order_ids))) if order_ids else 0,
                        "returns": db.scalar(select(func.count()).select_from(ReturnRequest).where(
                            ReturnRequest.order_id.in_(order_ids))) if order_ids else 0,
                        "actions": db.scalar(select(func.count()).select_from(PendingAction).where(
                            PendingAction.action_id.in_(action_ids))) if action_ids else 0}
                record["cleanup"] = {"status": "PASS" if not any(counts.values()) else "FAIL",
                    "exact_owned_ids": True, "temporary_orders": len(order_ids),
                    "temporary_returns": len(return_ids), "temporary_committed_refunds": len(refund_ids),
                    "temporary_actions": len(action_ids), "demo_credentials_restored": len(saved_credentials),
                    "remaining_owned_rows": counts}
                if record["cleanup"]["status"] != "PASS":
                    record["status"] = "FAIL"
            except Exception as exc:
                record["cleanup"] = {"status": "FAIL", "error_type": type(exc).__name__}
                record["status"] = "FAIL"
        if engine is not None:
            engine.dispose()
        import os
        if old_supervisor is None:
            os.environ.pop("SHOPRELAY_M3_SUPERVISOR_ENABLED", None)
        else:
            os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = old_supervisor
        record["finished_at"] = _utc()
        record["elapsed_ms"] = round((datetime.now(timezone.utc) - start).total_seconds() * 1000, 1)
        output = ROOT / "docs" / "probes" / f"m4.3-b2-refund-postgres-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.json"
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record["record_path"] = output.relative_to(ROOT).as_posix()
    return record


if __name__ == "__main__":
    result = probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
