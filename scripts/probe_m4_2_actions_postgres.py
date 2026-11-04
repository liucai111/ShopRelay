"""M4.2 real isolated PostgreSQL pending-action/confirmation probe.

Only writes SIMULATED_TEST_DATA receipts in shoprelay_m11_test. It restores demo
credential hashes and removes only IDs created by this invocation.
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
from concurrent.futures import ProcessPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text, update

from scripts.g2_test_database import resolve_test_database_url
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import (LATEST_SCHEMA_REVISION, current_revision, make_engine,
                       make_session_factory, upgrade_database)
from app.m1.models import (ActionAudit, AuthSession, DemoCredential, ObservabilityRecord,
                           PendingAction, Run, RunEvent, SimulatedTestAction)
from app.m1.services import set_demo_password
from app.orchestration.checkpointer import checkpoint_schema_ready, run_checkpointer_async
from app.m0.config import load_settings

ROOT = Path(__file__).resolve().parents[1]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _login(client: TestClient, user: str, password: str) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    headers = {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: csrf}
    response = client.post("/api/demo/login", headers=headers,
                           json={"username": user, "password": password})
    if response.status_code != 200:
        raise RuntimeError("DEMO_LOGIN_FAILED")
    return {"Origin": str(client.base_url).rstrip("/"),
            CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _new_context(client: TestClient, headers: dict, suffix: str) -> tuple[str, str]:
    session = client.post("/api/sessions", headers=headers,
                          json={"title": "M4.2 isolated probe"})
    if session.status_code != 200:
        raise RuntimeError("SESSION_CREATE_FAILED")
    sid = session.json()["session"]["session_id"]
    run = client.post(f"/api/sessions/{sid}/runs", headers=headers,
        json={"client_request_id": f"m42-{suffix}", "request_payload": {"purpose": "isolated_probe"}})
    if run.status_code != 200:
        raise RuntimeError("RUN_CREATE_FAILED")
    return sid, run.json()["run"]["run_id"]


def _payload(session_id: str, run_id: str, key: str, target: str) -> dict:
    return {"tool_id": "simulated_test_action", "session_id": session_id,
        "run_id": run_id, "target_resource_id": target,
        "arguments": {"label": "m42-probe", "value": "isolated receipt"},
        "idempotency_key": key}


def _serve_isolated_action_api(database_url: str, port: int) -> None:
    import os
    os.environ["SHOPRELAY_ORCHESTRATOR"] = "legacy"
    os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
    import uvicorn
    app = create_app(database_url=database_url, action_writes_enabled=True,
                     simulated_test_actions_enabled=True)
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)


def _http_confirm(args: tuple[str, int, str, str, str, str]) -> tuple[int, dict]:
    host, port, session_cookie, csrf, action_id, args_hash = args
    body = json.dumps({"confirm": True, "arguments_hash": args_hash}).encode("utf-8")
    request = urllib.request.Request(f"http://{host}:{port}/api/actions/{action_id}/confirm",
        data=body, method="POST", headers={"Content-Type": "application/json",
            "Origin": f"http://{host}:{port}", "x-csrf-token": csrf,
            "Cookie": f"shoprelay_session={session_cookie}; shoprelay_csrf={csrf}"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def probe() -> dict:
    record: dict = {"schema_version": "shoprelay.m4.2-postgres-actions.v1",
        "started_at": _utc(), "status": "NOT_RUN", "database_expected": "shoprelay_m11_test",
        "migration_revision": {"before": None, "after": None, "expected": LATEST_SCHEMA_REVISION},
        "simulated_action_executions": 0, "business_write_executions": 0,
        "deepseek_requests": 0, "mcp_write_calls": 0, "cases": {}, "cleanup": {}}
    engine = factory = None
    database_url = None
    app = None
    saved_credentials: dict[str, dict] = {}
    auth_hashes: set[str] = set()
    action_ids: list[str] = []
    run_ids: list[str] = []
    session_ids: list[str] = []
    started = datetime.now(timezone.utc)
    password = secrets.token_urlsafe(32)
    user_hash = "u_demo"
    other_hash = "u_other"
    api_module = None
    original_audit = None
    try:
        url = resolve_test_database_url()
        if url.database != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        database_url = url.render_as_string(hide_password=False)
        engine = make_engine(database_url)
        factory = make_session_factory(engine)
        with engine.connect() as connection:
            record["database"] = connection.execute(text("select current_database()")).scalar_one()
            record["postgres_version"] = connection.execute(text("select current_setting('server_version')")).scalar_one()
            if record["database"] != "shoprelay_m11_test":
                raise RuntimeError("UNSAFE_DATABASE_TARGET")
            record["postgresql_port"] = url.port
        record["migration_revision"]["before"] = current_revision(database_url)
        # Upgrade is scoped by the validated URL above; no database or volume reset.
        upgrade_database(database_url)
        record["migration_revision"]["after"] = current_revision(database_url)
        if record["migration_revision"]["after"] != LATEST_SCHEMA_REVISION:
            raise RuntimeError("MIGRATION_HEAD_MISMATCH")
        settings = load_settings(require_api_key=False)
        record["checkpointer_schema"] = settings.checkpoint_schema
        record["checkpointer_ready"] = bool(run_checkpointer_async(
            checkpoint_schema_ready(database_url, settings.checkpoint_schema)))
        if not record["checkpointer_ready"]:
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")

        with factory() as db:
            for user_id in (user_hash, other_hash):
                credential = db.get(DemoCredential, user_id)
                if credential is None:
                    raise RuntimeError("DEMO_CREDENTIAL_MISSING")
                saved_credentials[user_id] = {"password_hash": credential.password_hash,
                    "active": credential.active, "updated_at": credential.updated_at}
                set_demo_password(db, user_id, password)
            db.commit()

        app = create_app(database_url=database_url, engine=engine,
            action_writes_enabled=True, simulated_test_actions_enabled=True)
        record["write_flags"] = {"action_writes_enabled": app.state.action_writes_enabled,
            "simulated_test_actions_enabled": app.state.simulated_test_actions_enabled,
            "isolated_database_gate": app.state.simulated_test_actions_enabled}
        if not app.state.simulated_test_actions_enabled:
            raise RuntimeError("SIMULATED_ACTION_ENVIRONMENT_GATE_FAILED")

        with TestClient(app) as owner, TestClient(app) as other:
            owner_headers = _login(owner, user_hash, password)
            other_headers = _login(other, other_hash, password)
            owner_cookie = owner.cookies.get("shoprelay_session")
            other_cookie = other.cookies.get("shoprelay_session")
            auth_hashes.update({hashlib.sha256(owner_cookie.encode()).hexdigest(),
                                hashlib.sha256(other_cookie.encode()).hexdigest()})
            sid, rid = _new_context(owner, owner_headers, secrets.token_hex(8))
            session_ids.append(sid); run_ids.append(rid)
            payload = _payload(sid, rid, "m42-" + secrets.token_hex(12),
                               "M42-SIM-" + secrets.token_hex(8).upper())
            created = owner.post("/api/actions", headers=owner_headers, json=payload)
            if created.status_code != 200:
                raise RuntimeError("PENDING_ACTION_CREATE_FAILED")
            action = created.json()["action"]
            action_id = action["action_id"]
            action_ids.append(action_id)
            record["cases"]["pending_before_confirmation"] = {
                "status": action["status"], "arguments_hash": action["arguments_hash"],
                "business_effect": created.json()["business_effect"]}
            with factory() as db:
                before_count = db.scalar(select(func.count()).select_from(SimulatedTestAction))
            record["receipt_count_before_confirmation"] = before_count
            if before_count != 0:
                raise RuntimeError("BUSINESS_EFFECT_BEFORE_CONFIRMATION")

            record["cases"]["forged_user_id"] = {"status": owner.post("/api/actions",
                headers=owner_headers, json={**payload, "user_id": other_hash}).status_code}
            if record["cases"]["forged_user_id"]["status"] != 422:
                raise RuntimeError("FORGED_USER_ID_ACCEPTED")
            record["cases"]["cross_user_get"] = {"status": other.get(f"/api/actions/{action_id}").status_code}
            other_confirm = other.post(f"/api/actions/{action_id}/confirm", headers=other_headers,
                json={"confirm": True, "arguments_hash": action["arguments_hash"]})
            record["cases"]["cross_user_confirm"] = {"status": other_confirm.status_code,
                "code": other_confirm.json().get("code")}
            if record["cases"]["cross_user_get"]["status"] != 404 or other_confirm.status_code != 404:
                raise RuntimeError("CROSS_USER_ACTION_ACCESS")
            no_csrf = owner.post(f"/api/actions/{action_id}/confirm", json={
                "confirm": True, "arguments_hash": action["arguments_hash"]})
            record["cases"]["csrf_required"] = {"status": no_csrf.status_code, "code": no_csrf.json().get("code")}
            if no_csrf.status_code != 403:
                raise RuntimeError("CSRF_NOT_ENFORCED")

            confirm_payload = {"confirm": True, "arguments_hash": action["arguments_hash"]}
            # Two independent API workers/sessions race the same confirmation.
            owner_session_cookie = owner.cookies.get("shoprelay_session")
            csrf_cookie = owner.cookies.get("shoprelay_csrf")
            origin = str(owner.base_url).rstrip("/")

            # Separate OS processes send the same authenticated API confirmation
            # concurrently. PostgreSQL owns the claim lock, not Python process state.
            with socket.socket() as probe_socket:
                probe_socket.bind(("127.0.0.1", 0))
                port = probe_socket.getsockname()[1]
            server_process = multiprocessing.get_context("spawn").Process(
                target=_serve_isolated_action_api, args=(database_url, port), daemon=True)
            server_process.start()
            ready = False
            for _ in range(100):
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                        ready = True
                        break
                except OSError:
                    time.sleep(0.05)
            if not ready:
                server_process.terminate(); server_process.join(timeout=5)
                raise RuntimeError("ISOLATED_API_SERVER_NOT_READY")
            race_args = ("127.0.0.1", port, owner_session_cookie, csrf_cookie,
                         action_id, action["arguments_hash"])
            try:
                with ProcessPoolExecutor(max_workers=2, mp_context=multiprocessing.get_context("spawn")) as pool:
                    race = list(pool.map(_http_confirm, (race_args, race_args)))
            finally:
                server_process.terminate()
                server_process.join(timeout=10)
                if server_process.is_alive():
                    raise RuntimeError("ISOLATED_API_SERVER_DID_NOT_STOP")
            record["cases"]["concurrent_confirm"] = [{"status": status,
                "action_status": body.get("action", {}).get("status"),
                "replayed": body.get("replayed"), "trace_id": body.get("trace_id")} for status, body in race]
            if any(status != 200 for status, _ in race) or any(
                    body.get("action", {}).get("status") != "SUCCEEDED" for _, body in race):
                raise RuntimeError("CONCURRENT_CONFIRM_FAILED")
            if sum(not bool(body.get("replayed")) for _, body in race) != 1:
                raise RuntimeError("CONCURRENT_CONFIRM_NOT_SINGLE_WINNER")
            with factory() as db:
                receipt_count = db.scalar(select(func.count()).select_from(SimulatedTestAction).where(
                    SimulatedTestAction.action_id == action_id))
            record["receipt_count_after_race"] = receipt_count
            if receipt_count != 1:
                raise RuntimeError("SIMULATED_RECEIPT_NOT_EXACTLY_ONCE")
            replay = owner.post(f"/api/actions/{action_id}/confirm", headers=owner_headers,
                json=confirm_payload)
            record["cases"]["lost_response_replay"] = {"status": replay.status_code,
                "replayed": replay.json().get("replayed"), "action_status": replay.json().get("action", {}).get("status")}
            if replay.status_code != 200 or not replay.json().get("replayed"):
                raise RuntimeError("IDEMPOTENT_REPLAY_FAILED")

            # Reject and cancel are terminal and have no business effects.
            for terminal in ("reject", "cancel"):
                tsid, trid = _new_context(owner, owner_headers, secrets.token_hex(8))
                session_ids.append(tsid); run_ids.append(trid)
                body = _payload(tsid, trid, "m42-" + secrets.token_hex(12),
                                "M42-SIM-" + secrets.token_hex(8).upper())
                pending = owner.post("/api/actions", headers=owner_headers, json=body).json()["action"]
                action_ids.append(pending["action_id"])
                closed = owner.post(f"/api/actions/{pending['action_id']}/{terminal}",
                    headers=owner_headers, json={"acknowledge": True})
                expected = "REJECTED" if terminal == "reject" else "CANCELLED"
                if closed.status_code != 200 or closed.json()["action"]["status"] != expected:
                    raise RuntimeError("ACTION_REJECT_CANCEL_FAILED")
                denied = owner.post(f"/api/actions/{pending['action_id']}/confirm",
                    headers=owner_headers, json={"confirm": True, "arguments_hash": pending["arguments_hash"]})
                if denied.status_code != 409:
                    raise RuntimeError("CLOSED_ACTION_EXECUTED")

            # Verify Run event ordering and recovery through a newly constructed app.
            events = owner.get(f"/api/runs/{rid}/events").json()
            run_events = events.get("events", [])
            relevant = [item["type"] for item in run_events if item.get("type") in {"action_pending", "tool_started", "tool_finished"}]
            record["run_events"] = relevant
            if relevant != ["action_pending", "tool_started", "tool_finished"]:
                raise RuntimeError("ACTION_RUN_EVENT_ORDER_INVALID")
            record["cases"]["run_owner"] = owner.get(f"/api/runs/{rid}").status_code
            record["cases"]["other_run"] = other.get(f"/api/runs/{rid}").status_code
            if record["cases"]["run_owner"] != 200 or record["cases"]["other_run"] != 404:
                raise RuntimeError("RUN_OWNER_ISOLATION_FAILED")

        fresh_app = create_app(database_url=database_url, engine=engine,
            action_writes_enabled=True, simulated_test_actions_enabled=True)
        with TestClient(fresh_app) as fresh:
            fresh.cookies.set("shoprelay_session", owner_session_cookie)
            recovered = fresh.get(f"/api/actions/{action_id}")
            recovered_events = fresh.get(f"/api/runs/{rid}/events")
            record["restart_recovery"] = {"action_status": recovered.json().get("action", {}).get("status"),
                "run_event_status": recovered_events.status_code,
                "event_types": [item["type"] for item in recovered_events.json().get("events", [])]}
            if recovered.status_code != 200 or recovered.json()["action"]["status"] != "SUCCEEDED" or recovered_events.status_code != 200:
                raise RuntimeError("ACTION_RESTART_RECOVERY_FAILED")

            with factory() as db:
                action = db.get(PendingAction, action_id)
            audits = db.execute(select(ActionAudit).where(ActionAudit.action_id == action_id)
                                .order_by(ActionAudit.occurred_at)).scalars().all()
            records = db.execute(select(ObservabilityRecord).where(
                ObservabilityRecord.run_id == rid).order_by(ObservabilityRecord.timestamp)).scalars().all()
            span_payloads = [json.loads(row.payload_json) for row in records
                             if row.record_type == "span" and
                             json.loads(row.payload_json).get("node_name") == "simulated_action_execute"]
            metric_payloads = [json.loads(row.payload_json) for row in records if row.record_type == "metric"]
            confirm_trace_ids = {item["trace_id"] for item in record["cases"]["concurrent_confirm"]}
            allowed_trace_ids = confirm_trace_ids | {action.trace_id}
            record["audit"] = [{"event_type": row.event_type, "from_status": row.from_status,
                "to_status": row.to_status, "transaction_reference": bool(row.transaction_reference),
                "trace_linked": (row.trace_id == action.trace_id if row.event_type == "ACTION_CREATED_PENDING_CONFIRMATION"
                                 else row.trace_id in confirm_trace_ids)} for row in audits]
            record["observability"] = {"spans": sum(row.record_type == "span" for row in records),
                "trace_ids_match": all(row.trace_id in allowed_trace_ids for row in records),
                "trace_ids": sorted({row.trace_id for row in records}),
                "sensitive_values_absent": password not in " ".join(row.payload_json for row in records),
                "execution_span_duration_ms": [span["duration_ms"] for span in span_payloads],
                "metric_records": len(metric_payloads)}
            if len(span_payloads) != 1 or len(metric_payloads) != 1:
                raise RuntimeError("ACTION_METRIC_OR_SPAN_MISSING")
            if not record["observability"]["trace_ids_match"] or not record["observability"]["sensitive_values_absent"]:
                raise RuntimeError("OBSERVABILITY_LINK_OR_REDACTION_FAILED")
            if not any(row.transaction_reference for row in audits):
                raise RuntimeError("TRANSACTION_AUDIT_MISSING")

        # Simulate a failure after the receipt insert but before commit. The
        # surrounding PostgreSQL transaction must roll back every effect.
        with TestClient(fresh_app) as lifecycle_client:
            lifecycle_client.cookies.set("shoprelay_session", owner_session_cookie)
            lifecycle_client.cookies.set("shoprelay_csrf", csrf_cookie)
            lifecycle_headers = {"Origin": origin, CSRF_HEADER: csrf_cookie}
            rsid, rrid = _new_context(lifecycle_client, lifecycle_headers, secrets.token_hex(8))
            session_ids.append(rsid); run_ids.append(rrid)
            rollback_payload = _payload(rsid, rrid, "m42-" + secrets.token_hex(12),
                "M42-SIM-" + secrets.token_hex(8).upper())
            rollback_action = lifecycle_client.post("/api/actions", headers=lifecycle_headers,
                json=rollback_payload).json()["action"]
            action_ids.append(rollback_action["action_id"])
            from app.m4 import actions as action_service
            original_audit = action_service._audit

            def fail_before_commit(db, row, event_type, before, after, **kwargs):
                if event_type == "SIMULATED_TRANSACTION_COMMITTED":
                    raise RuntimeError("INJECTED_FAILURE_BEFORE_COMMIT")
                return original_audit(db, row, event_type, before, after, **kwargs)

            action_service._audit = fail_before_commit
            try:
                with factory() as db:
                    try:
                        action_service.confirm_pending_action(db, owner_id=user_hash,
                            action_id=rollback_action["action_id"], simulated_enabled=True,
                            environment_name="shoprelay_m11_test",
                            confirmed_arguments_hash=rollback_action["arguments_hash"])
                    except RuntimeError as exc:
                        if "INJECTED_FAILURE_BEFORE_COMMIT" not in str(exc):
                            raise
                        db.rollback()
                    else:
                        raise RuntimeError("TRANSACTION_FAILURE_NOT_INJECTED")
            finally:
                action_service._audit = original_audit
            with factory() as db:
                rollback_row = db.get(PendingAction, rollback_action["action_id"])
                rollback_receipts = db.scalar(select(func.count()).select_from(SimulatedTestAction).where(
                    SimulatedTestAction.action_id == rollback_action["action_id"]))
                rollback_audits = db.scalar(select(func.count()).select_from(ActionAudit).where(
                    ActionAudit.action_id == rollback_action["action_id"]))
                record["cases"]["transaction_rollback"] = {"action_status": rollback_row.status,
                    "receipt_count": rollback_receipts, "audit_count": rollback_audits}
                if rollback_row.status != "PENDING_CONFIRMATION" or rollback_receipts != 0 or rollback_audits != 1:
                    raise RuntimeError("TRANSACTION_ROLLBACK_INCOMPLETE")

            # Expiry is enforced by the server clock, not a client-provided time.
            esid, erid = _new_context(lifecycle_client, lifecycle_headers, secrets.token_hex(8))
            session_ids.append(esid); run_ids.append(erid)
            expired_payload = _payload(esid, erid, "m42-" + secrets.token_hex(12),
                "M42-SIM-" + secrets.token_hex(8).upper())
            expired = lifecycle_client.post("/api/actions", headers=lifecycle_headers, json=expired_payload).json()["action"]
            action_ids.append(expired["action_id"])
            with factory.begin() as db:
                db.execute(update(PendingAction).where(PendingAction.action_id == expired["action_id"])
                    .values(expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)))
            expired_response = lifecycle_client.post(f"/api/actions/{expired['action_id']}/confirm",
                headers=lifecycle_headers, json={"confirm": True, "arguments_hash": expired["arguments_hash"]})
            record["cases"]["expired_action"] = {"status": expired_response.status_code,
                "code": expired_response.json().get("code")}
            if expired_response.status_code != 410:
                raise RuntimeError("EXPIRED_ACTION_NOT_REJECTED")
            with factory() as db:
                expired_row = db.get(PendingAction, expired["action_id"])
                expired_receipts = db.scalar(select(func.count()).select_from(SimulatedTestAction).where(
                    SimulatedTestAction.action_id == expired["action_id"]))
                if expired_row.status != "EXPIRED" or expired_receipts != 0:
                    raise RuntimeError("EXPIRED_ACTION_EFFECT")
        record["simulated_action_executions"] = 1
        record["business_write_executions"] = 0
        record["finished_at"] = _utc()
        record["duration_ms"] = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 2)
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "BLOCKED" if type(exc).__name__ in {"OperationalError", "InterfaceError"} else "FAIL"
        record["failure_phase"] = record.get("phase", "postgres_action_probe")
        record["failure_type"] = type(exc).__name__
        record["failure_code"] = str(exc)[:160]
        record["finished_at"] = _utc()
        record["duration_ms"] = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 2)
    finally:
        if factory:
            try:
                with factory() as db:
                    if action_ids:
                        db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
                        db.execute(delete(SimulatedTestAction).where(SimulatedTestAction.action_id.in_(action_ids)))
                        db.execute(delete(PendingAction).where(PendingAction.action_id.in_(action_ids)))
                    if run_ids:
                        db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids)))
                        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(run_ids)))
                        db.execute(delete(Run).where(Run.run_id.in_(run_ids)))
                    if session_ids:
                        # Runs cascade to these sessions; explicitly delete only the isolated test sessions.
                        from app.m1.models import ConversationSession
                        db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(session_ids)))
                    for token_hash in auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash == token_hash))
                    db.commit()
                for user_id, prior in saved_credentials.items():
                    with factory.begin() as db:
                        credential = db.get(DemoCredential, user_id)
                        if credential:
                            credential.password_hash = prior["password_hash"]
                            credential.active = prior["active"]
                            credential.updated_at = prior["updated_at"]
                record["cleanup"] = {"action_ids_removed": len(action_ids),
                    "run_ids_removed": len(run_ids), "session_ids_removed": len(session_ids),
                    "auth_sessions_removed": len(auth_hashes), "demo_credentials_restored": len(saved_credentials)}
            except Exception:
                record["cleanup"] = {"status": "FAILED"}
        if engine:
            engine.dispose()
    return record


def main() -> int:
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("output already exists; choose a new versioned path")
    result = probe()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in result.items()
        if key not in {"database_url", "password", "credentials"}}, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
