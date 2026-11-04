"""Real M4.3-B1 return Pending Action probe for shoprelay_m11_test only.

The probe creates unique SIMULATED_TEST_DATA order rows, confirms one application,
checks independent-process idempotency and rollback, then deletes only its own IDs.
It never calls DeepSeek, MCP write tools, payment services, or production databases.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text

from scripts.g2_test_database import resolve_test_database_url
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import LATEST_SCHEMA_REVISION, current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import (ActionAudit, AfterSalesEvent, AuthSession, ConversationSession, DemoCredential,
                           ObservabilityRecord, Order, PendingAction, ReturnRequest, Run, RunEvent, Trace)
from app.m1.services import set_demo_password
from app.m0.config import load_settings
from app.orchestration.checkpointer import checkpoint_schema_ready, run_checkpointer_async

ROOT = Path(__file__).resolve().parents[1]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _free_port() -> int:
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


def _new_context(client: TestClient, headers: dict[str, str], suffix: str) -> tuple[str, str]:
    session = client.post("/api/sessions", headers=headers,
        json={"title": "M4.3-B1 isolated return probe"})
    if session.status_code != 200:
        raise RuntimeError("SESSION_CREATE_FAILED")
    sid = session.json()["session"]["session_id"]
    run = client.post(f"/api/sessions/{sid}/runs", headers=headers,
        json={"client_request_id": f"m43b1-{suffix}", "request_payload": {"purpose": "isolated_return_probe"}})
    if run.status_code != 200:
        raise RuntimeError("RUN_CREATE_FAILED")
    return sid, run.json()["run"]["run_id"]


def _order_payload(order_id: str, sid: str, rid: str, key: str) -> dict:
    return {"tool_id": "create_return_request", "session_id": sid, "run_id": rid,
        "target_resource_id": order_id, "arguments": {"order_id": order_id,
            "reason_code": "QUALITY_ISSUE", "reason_summary": "模拟测试商品申报外观瑕疵，待人工审核"},
        "idempotency_key": key}


def _serve_return_api(database_url: str, port: int) -> None:
    import os
    os.environ["SHOPRELAY_ORCHESTRATOR"] = "legacy"
    os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
    import uvicorn
    app = create_app(database_url=database_url, action_writes_enabled=True,
        return_actions_enabled=True, orchestrator_mode="legacy")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)


def _http_confirm(args: tuple[str, int, str, str, str, str]) -> tuple[int, dict]:
    host, port, session_cookie, csrf, action_id, args_hash = args
    body = json.dumps({"confirm": True, "arguments_hash": args_hash}).encode("utf-8")
    req = urllib.request.Request(f"http://{host}:{port}/api/actions/{action_id}/confirm", data=body,
        method="POST", headers={"Content-Type": "application/json", "Origin": f"http://{host}:{port}",
            "x-csrf-token": csrf,
            "Cookie": f"shoprelay_session={session_cookie}; shoprelay_csrf={csrf}"})
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def probe() -> dict:
    started = datetime.now(timezone.utc)
    phase = "environment"
    record: dict = {"schema_version": "shoprelay.m4.3-b1-return-postgres.v1", "started_at": _utc(),
        "status": "NOT_RUN", "database_expected": "shoprelay_m11_test", "migration": {},
        "checkpointer_ready": False, "return_writes": 0, "production_writes": 0,
        "mcp_write_calls": 0, "payment_calls": 0, "deepseek_requests": 0,
        "cases": {}, "cleanup": {"status": "NOT_RUN"}}
    engine = factory = None
    saved_credentials: dict[str, dict] = {}
    auth_hashes: set[str] = set()
    action_ids: list[str] = []
    session_ids: list[str] = []
    run_ids: list[str] = []
    order_ids: list[str] = []
    return_ids: list[str] = []
    app = None
    server_processes: list[multiprocessing.Process] = []
    database_url = None
    password = secrets.token_urlsafe(32)
    injection_listener = None
    injected_order = None
    old_supervisor_flag = __import__("os").environ.get("SHOPRELAY_M3_SUPERVISOR_ENABLED")
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
                row = db.get(DemoCredential, user_id)
                if row is None:
                    raise RuntimeError("DEMO_CREDENTIAL_MISSING")
                saved_credentials[user_id] = {"password_hash": row.password_hash,
                    "active": row.active, "updated_at": row.updated_at}
                set_demo_password(db, user_id, password)
            db.commit()
        app = create_app(database_url=database_url, engine=engine, action_writes_enabled=True,
            return_actions_enabled=True, orchestrator_mode="legacy")
        record["write_gates"] = {"actions": app.state.action_writes_enabled,
            "returns": app.state.return_actions_enabled, "database_name_gate": True,
            "mcp_write": False, "production": False}
        if not app.state.return_actions_enabled:
            raise RuntimeError("RETURN_ACTION_ISOLATION_GATE_FAILED")

        suffix = secrets.token_hex(6)
        primary_order = "M43B1-SIM-" + suffix
        rollback_order = "M43B1-ROLLBACK-" + suffix
        expired_order = "M43B1-EXPIRED-" + suffix
        rejected_order = "M43B1-REJECTED-" + suffix
        order_ids.extend((primary_order, rollback_order, expired_order, rejected_order))
        with factory() as db:
            for order_id in order_ids:
                db.add(Order(order_id=order_id, user_id="u_demo", status="DELIVERED",
                    paid_at="2026-09-20T10:00:00Z", delivered_at="2026-10-01T10:00:00Z",
                    paid_cents=1299, version=1))
            db.commit()
        record["synthetic_data_marker"] = "SIMULATED_TEST_DATA"

        phase = "create_and_authorize_pending"
        with TestClient(app, raise_server_exceptions=False) as owner, \
                TestClient(app, raise_server_exceptions=False) as other:
            owner_headers = _login(owner, "u_demo", password)
            other_headers = _login(other, "u_other", password)
            auth_hashes.add(hashlib.sha256(owner.cookies.get("shoprelay_session").encode()).hexdigest())
            auth_hashes.add(hashlib.sha256(other.cookies.get("shoprelay_session").encode()).hexdigest())
            sid, rid = _new_context(owner, owner_headers, suffix)
            session_ids.append(sid); run_ids.append(rid)
            payload = _order_payload(primary_order, sid, rid, "m43b1-" + secrets.token_hex(12))
            created = owner.post("/api/actions", headers=owner_headers, json=payload)
            if created.status_code != 200:
                raise RuntimeError("RETURN_PENDING_ACTION_CREATE_FAILED:" + str(created.json().get("code")))
            action = created.json()["action"]
            action_ids.append(action["action_id"])
            record["cases"]["pending_before_confirmation"] = {"status": action["status"],
                "business_effect": created.json()["business_effect"], "arguments_hash": action["arguments_hash"],
                "eligibility": action["sanitized_arguments"]["application_outcome"],
                "reason_source": action["sanitized_arguments"]["reason_source"],
                "knowledge_version": action["sanitized_arguments"]["policy_snapshot"]["knowledge_version"],
                "policy_chunk_ids": [x["chunk_id"] for x in action["sanitized_arguments"]["policy_snapshot"]["evidence"]]}
            with factory() as db:
                before = db.scalar(select(func.count()).select_from(ReturnRequest).where(ReturnRequest.order_id == primary_order))
            record["cases"]["pending_before_confirmation"]["return_count"] = before
            if before != 0 or action["status"] != "PENDING_CONFIRMATION":
                raise RuntimeError("RETURN_WRITTEN_BEFORE_CONFIRMATION")
            record["cases"]["forged_user_id"] = owner.post("/api/actions", headers=owner_headers,
                json={**payload, "user_id": "u_other"}).status_code
            record["cases"]["cross_user_get"] = other.get(f"/api/actions/{action['action_id']}").status_code
            cross_confirm = other.post(f"/api/actions/{action['action_id']}/confirm", headers=other_headers,
                json={"confirm": True, "arguments_hash": action["arguments_hash"]})
            record["cases"]["cross_user_confirm"] = cross_confirm.status_code
            mismatch = owner.post(f"/api/actions/{action['action_id']}/confirm", headers=owner_headers,
                json={"confirm": True, "arguments_hash": "0" * 64})
            record["cases"]["hash_mismatch"] = {"status": mismatch.status_code, "code": mismatch.json().get("code")}
            if (record["cases"]["forged_user_id"] != 422 or record["cases"]["cross_user_get"] != 404 or
                    record["cases"]["cross_user_confirm"] != 404 or mismatch.status_code != 409):
                raise RuntimeError("RETURN_ACTION_AUTH_OR_HASH_GUARD_FAILED")

            phase = "two_process_confirmation"
            # A pair of separate service processes race the same action against PostgreSQL.
            ports = [_free_port(), _free_port()]
            for port in ports:
                process = multiprocessing.get_context("spawn").Process(
                    target=_serve_return_api, args=(database_url, port), daemon=True)
                process.start(); server_processes.append(process)
            for port in ports:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("ACTION_WORKER_START_TIMEOUT")
            confirm_args = [("127.0.0.1", port, owner.cookies.get("shoprelay_session"),
                owner.cookies.get("shoprelay_csrf"), action["action_id"], action["arguments_hash"]) for port in ports]
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(_http_confirm, confirm_args))
            record["cases"]["two_process_confirmation"] = [{"http_status": status,
                "replayed": body.get("replayed"), "action_status": body.get("action", {}).get("status"),
                "business_effect": body.get("business_effect")} for status, body in results]
            if any(status != 200 for status, _ in results) or sum(not body.get("replayed", False) for _, body in results) != 1:
                raise RuntimeError("TWO_PROCESS_ACTION_IDEMPOTENCY_FAILED")
            record["return_writes"] = 1
            phase = "stop_confirmation_workers"
            for process in server_processes:
                process.terminate(); process.join(timeout=5)
            server_processes.clear()
            phase = "inspect_committed_return"
            first_return_id = next(body["action"]["result"]["return_id"] for _, body in results)
            return_ids.append(first_return_id)
            with factory() as db:
                return_row = db.get(ReturnRequest, first_return_id)
                record["cases"]["confirmed_return"] = {"return_id": first_return_id,
                    "status": return_row.status, "source_type": return_row.source_type,
                    "source_ref_matches_action": return_row.source_ref == action["action_id"],
                    "submitted_at": return_row.submitted_at.isoformat().replace("+00:00", "Z"),
                    "reason_is_claim_only": "外观瑕疵" in return_row.reason}
                if return_row.status != "SUBMITTED" or return_row.source_type != "SIMULATED_TEST_DATA":
                    raise RuntimeError("RETURN_STATUS_OR_SOURCE_INVALID")
                event_row = db.execute(select(AfterSalesEvent).where(
                    AfterSalesEvent.return_id == first_return_id)).scalar_one()
                event_details = json.loads(event_row.details_json)
                record["cases"]["confirmed_return"]["event"] = {"type": event_row.event_type,
                    "actor_type": event_row.actor_type, "source_type": event_row.source_type,
                    "reason_source": event_details.get("reason_source"), "review_required": event_details.get("review_required")}
                record["cases"]["confirmed_return"]["action_audit_events"] = db.execute(
                    select(ActionAudit.event_type).where(ActionAudit.action_id == action["action_id"])
                ).scalars().all()
                record["cases"]["confirmed_return"]["run_event_types"] = db.execute(
                    select(RunEvent.event_type).where(RunEvent.run_id == rid).order_by(RunEvent.seq)
                ).scalars().all()
                record["cases"]["confirmed_return"]["observability_record_types"] = db.execute(
                    select(ObservabilityRecord.record_type).where(ObservabilityRecord.run_id == rid)
                ).scalars().all()
                record["cases"]["confirmed_return"]["trace_events"] = db.execute(
                    select(Trace.event_type, Trace.status).where(Trace.run_id == rid)
                ).all()
                record["cases"]["confirmed_return"]["trace_events"] = [
                    {"event_type": event_type, "status": status}
                    for event_type, status in record["cases"]["confirmed_return"]["trace_events"]]
                if event_details.get("reason_source") != "USER_CLAIM_UNVERIFIED" or event_details.get("review_required") is not True:
                    raise RuntimeError("RETURN_REASON_PROVENANCE_INVALID")

            phase = "same_action_replay"
            replay = owner.post(f"/api/actions/{action['action_id']}/confirm", headers=owner_headers,
                json={"confirm": True, "arguments_hash": action["arguments_hash"]})
            record["cases"]["same_action_replay"] = {"status": replay.status_code,
                "replayed": replay.json().get("replayed"), "return_id": replay.json().get("action", {}).get("result", {}).get("return_id")}
            if replay.status_code != 200 or not replay.json().get("replayed") or \
                    record["cases"]["same_action_replay"]["return_id"] != first_return_id:
                raise RuntimeError("RETURN_ACTION_REPLAY_FAILED")
            create_replay = owner.post("/api/actions", headers=owner_headers, json=payload)
            record["cases"]["same_idempotency_proposal_replay"] = {
                "status": create_replay.status_code,
                "replayed": create_replay.json().get("replayed"),
                "action_id_matches": create_replay.json().get("action", {}).get("action_id") == action["action_id"],
                "business_effect": create_replay.json().get("business_effect")}
            if create_replay.status_code != 200 or not create_replay.json().get("replayed") or \
                    create_replay.json().get("action", {}).get("action_id") != action["action_id"]:
                raise RuntimeError("RETURN_PROPOSAL_IDEMPOTENCY_REPLAY_FAILED")

            phase = "different_action_duplicate_guard"
            # A second Action ID cannot bypass the one-active-return business invariant.
            payload2 = _order_payload(primary_order, sid, rid, "m43b1-dup-" + secrets.token_hex(10))
            second = owner.post("/api/actions", headers=owner_headers, json=payload2)
            record["cases"]["different_action_same_order"] = {"status": second.status_code,
                "code": second.json().get("code"), "action_created": bool(second.json().get("action"))}
            if second.status_code != 409 or second.json().get("code") != "RETURN_ALREADY_OPEN" or second.json().get("action"):
                raise RuntimeError("BUSINESS_DUPLICATE_RETURN_NOT_BLOCKED")

            phase = "transaction_rollback"
            # Create a separate pending proposal and inject failure at the business table insert.
            rollback_payload = _order_payload(rollback_order, sid, rid, "m43b1-rb-" + secrets.token_hex(10))
            rollback_response = owner.post("/api/actions", headers=owner_headers, json=rollback_payload)
            if rollback_response.status_code != 200:
                record["cases"]["transaction_rollback"] = {"proposal_status": rollback_response.status_code,
                    "proposal_error_code": rollback_response.json().get("code")}
                raise RuntimeError("ROLLBACK_PROPOSAL_FAILED")
            rollback_action = rollback_response.json()["action"]; action_ids.append(rollback_action["action_id"])
            def fail_return_insert(_conn, _cursor, statement, _parameters, _context, _many):
                if statement.lstrip().upper().startswith("INSERT INTO AFTER_SALES_EVENTS"):
                    raise RuntimeError("INJECTED_RETURN_TRANSACTION_FAILURE")
            event.listen(engine, "before_cursor_execute", fail_return_insert)
            try:
                failed = owner.post(f"/api/actions/{rollback_action['action_id']}/confirm", headers=owner_headers,
                    json={"confirm": True, "arguments_hash": rollback_action["arguments_hash"]})
            finally:
                event.remove(engine, "before_cursor_execute", fail_return_insert)
            record["cases"]["transaction_rollback"] = {"http_status": failed.status_code}
            with factory() as db:
                rollback_action_row = db.get(PendingAction, rollback_action["action_id"])
                rollback_count = db.scalar(select(func.count()).select_from(ReturnRequest).where(
                    ReturnRequest.order_id == rollback_order))
                record["cases"]["transaction_rollback"].update({"action_status": rollback_action_row.status,
                    "return_count": rollback_count,
                    "audit_events": db.execute(select(ActionAudit.event_type).where(
                        ActionAudit.action_id == rollback_action["action_id"])).scalars().all()})
            if failed.status_code != 500 or rollback_action_row.status != "PENDING_CONFIRMATION" or rollback_count != 0:
                raise RuntimeError("RETURN_TRANSACTION_ROLLBACK_FAILED")

            cancel = owner.post(f"/api/actions/{rollback_action['action_id']}/cancel", headers=owner_headers,
                json={"acknowledge": True})
            cancel_confirm = owner.post(f"/api/actions/{rollback_action['action_id']}/confirm", headers=owner_headers,
                json={"confirm": True, "arguments_hash": rollback_action["arguments_hash"]})
            record["cases"]["cancelled_action"] = {"cancel_status": cancel.status_code,
                "confirm_status": cancel_confirm.status_code, "confirm_code": cancel_confirm.json().get("code")}
            if cancel.status_code != 200 or cancel.json()["action"]["status"] != "CANCELLED" or \
                    cancel_confirm.status_code != 409:
                raise RuntimeError("CANCELLED_ACTION_EXECUTED")

            phase = "expired_action_guard"
            expired_payload = _order_payload(expired_order, sid, rid, "m43b1-exp-" + secrets.token_hex(10))
            expired_create = owner.post("/api/actions", headers=owner_headers, json=expired_payload)
            if expired_create.status_code != 200:
                raise RuntimeError("EXPIRED_ACTION_PROPOSAL_FAILED")
            expired_action = expired_create.json()["action"]; action_ids.append(expired_action["action_id"])
            with factory() as db:
                expired_row = db.get(PendingAction, expired_action["action_id"])
                expired_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
                db.commit()
            expired_confirm = owner.post(f"/api/actions/{expired_action['action_id']}/confirm",
                headers=owner_headers, json={"confirm": True, "arguments_hash": expired_action["arguments_hash"]})
            record["cases"]["expired_action"] = {"status": expired_confirm.status_code,
                "code": expired_confirm.json().get("code"),
                "stored_status": owner.get(f"/api/actions/{expired_action['action_id']}").json()["action"]["status"]}
            if expired_confirm.status_code != 410 or record["cases"]["expired_action"]["stored_status"] != "EXPIRED":
                raise RuntimeError("EXPIRED_ACTION_EXECUTED")

            phase = "rejected_action_guard"
            rejected_payload = _order_payload(rejected_order, sid, rid, "m43b1-rej-" + secrets.token_hex(10))
            rejected_create = owner.post("/api/actions", headers=owner_headers, json=rejected_payload)
            if rejected_create.status_code != 200:
                raise RuntimeError("REJECTED_ACTION_PROPOSAL_FAILED")
            rejected_action = rejected_create.json()["action"]; action_ids.append(rejected_action["action_id"])
            rejected = owner.post(f"/api/actions/{rejected_action['action_id']}/reject", headers=owner_headers,
                json={"acknowledge": True})
            rejected_confirm = owner.post(f"/api/actions/{rejected_action['action_id']}/confirm", headers=owner_headers,
                json={"confirm": True, "arguments_hash": rejected_action["arguments_hash"]})
            record["cases"]["rejected_action"] = {"reject_status": rejected.status_code,
                "confirm_status": rejected_confirm.status_code, "confirm_code": rejected_confirm.json().get("code")}
            if rejected.status_code != 200 or rejected.json()["action"]["status"] != "REJECTED" or \
                    rejected_confirm.status_code != 409:
                raise RuntimeError("REJECTED_ACTION_EXECUTED")

            phase = "csrf_guard"
            record["cases"]["no_csrf"] = owner.post(f"/api/actions/{action['action_id']}/confirm", json={
                "confirm": True, "arguments_hash": action["arguments_hash"]}).status_code
            if record["cases"]["no_csrf"] != 403:
                raise RuntimeError("CSRF_NOT_ENFORCED")

            phase = "restart_recovery"
            # Reconstruct the API and verify the same persisted action/run remains readable.
            restarted_app = create_app(database_url=database_url, engine=engine,
                action_writes_enabled=True, return_actions_enabled=True, orchestrator_mode="legacy")
            with TestClient(restarted_app) as restarted:
                _login(restarted, "u_demo", password)
                auth_hashes.add(hashlib.sha256(restarted.cookies.get("shoprelay_session").encode()).hexdigest())
                persisted_action = restarted.get(f"/api/actions/{action['action_id']}")
                persisted_run = restarted.get(f"/api/runs/{rid}")
                record["cases"]["restart_recovery"] = {"action_status": persisted_action.json()["action"]["status"],
                    "run_status": persisted_run.json()["run"]["status"],
                    "return_id": persisted_action.json()["action"]["result"]["return_id"]}
                if persisted_action.status_code != 200 or persisted_run.status_code != 200 or \
                        persisted_action.json()["action"]["status"] != "SUCCEEDED":
                    raise RuntimeError("RETURN_RESTART_RECOVERY_FAILED")

        record["return_writes"] = 1
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["error_type"] = type(exc).__name__
        record["error_code"] = f"PROBE_FAILED_{phase.upper()}"
    finally:
        for process in server_processes:
            process.terminate(); process.join(timeout=5)
        if factory is not None:
            try:
                with factory() as db:
                    for return_id in return_ids:
                        db.execute(delete(AfterSalesEvent).where(AfterSalesEvent.return_id == return_id))
                        db.execute(delete(ReturnRequest).where(ReturnRequest.return_id == return_id))
                    # The injected rollback path must not leave a return row, but include exact order scope.
                    if order_ids:
                        db.execute(delete(AfterSalesEvent).where(AfterSalesEvent.order_id.in_(order_ids)))
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
                        db.execute(delete(Order).where(Order.order_id.in_(order_ids), Order.user_id == "u_demo"))
                    if auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_hashes)))
                    for user_id, values in saved_credentials.items():
                        credential = db.get(DemoCredential, user_id)
                        credential.password_hash = values["password_hash"]
                        credential.active = values["active"]
                        credential.updated_at = values["updated_at"]
                    db.commit()
                record["cleanup"] = {"status": "PASS", "owned_order_count": len(order_ids),
                    "owned_action_count": len(action_ids), "owned_return_count": len(return_ids),
                    "demo_credentials_restored": len(saved_credentials)}
            except Exception as exc:
                record["cleanup"] = {"status": "FAIL", "error_type": type(exc).__name__}
                record["status"] = "FAIL"
        if engine is not None:
            engine.dispose()
        import os
        if old_supervisor_flag is None:
            os.environ.pop("SHOPRELAY_M3_SUPERVISOR_ENABLED", None)
        else:
            os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = old_supervisor_flag
        record["finished_at"] = _utc()
        record["elapsed_ms"] = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 1)
        output = ROOT / "docs" / "probes" / f"m4.3-b1-return-postgres-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:6]}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        record["record_path"] = output.relative_to(ROOT).as_posix()
    return record


if __name__ == "__main__":
    result = probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
