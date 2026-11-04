"""Verify C2 demo-account auth and owner scoping with exact credential restoration.

This probe is limited to the existing shoprelay_m11_test database. It uses
random in-memory passwords only for real login API calls, restores the exact
credential hashes present at startup, creates unconfirmed test-only actions,
and removes rows by generated IDs. It never confirms or executes an action.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, select, text

from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.models import (ActionAudit, AuthSession, DemoCredential, GraphExecutionRecord,
    Message, ObservabilityRecord, PendingAction, Run, RunEvent, SessionMemorySummary,
    SupportTicket, SupportTicketEvent, Trace, ConversationSession)
from app.m1.services import set_demo_password
from scripts.g2_test_database import resolve_test_database_url
from scripts.probe_m5_2_c2_browser import _delete_messages_for_sessions, _restore_demo_credentials


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "probes" / "m5.2-c2-credentials-20261024-r4.json"
OWNERS = ("u_demo", "u_other")


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _login(client: TestClient, owner_id: str, password: str,
           auth_hashes: list[str]) -> dict[str, str]:
    csrf = client.get("/api/demo/csrf")
    if csrf.status_code != 200:
        raise RuntimeError("CSRF_ENDPOINT_FAILED")
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={
        "Origin": origin, CSRF_HEADER: csrf.json()["csrf_token"]},
        json={"username": owner_id, "password": password})
    if response.status_code != 200:
        raise RuntimeError(f"AUTH_FAILED:{owner_id}:{response.status_code}")
    cookie = client.cookies.get("shoprelay_session")
    if cookie:
        auth_hashes.append(sha256(str(cookie).encode()).hexdigest())
    profile = client.get("/api/demo/me")
    profile_user = profile.json().get("user", {}) if profile.status_code == 200 else {}
    if profile.status_code != 200 or not profile_user.get("display_name"):
        raise RuntimeError(f"AUTH_PROFILE_MISMATCH:{owner_id}")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def main() -> int:
    url = resolve_test_database_url()
    if (url.get_backend_name() != "postgresql" or url.database != "shoprelay_m11_test"
            or url.host not in {"127.0.0.1", "localhost", "::1"}):
        raise SystemExit("UNSAFE_DATABASE_TARGET")
    database_url = url.render_as_string(hide_password=False)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    probe_id = secrets.token_hex(8).upper()
    saved: dict[str, tuple[str, bool, datetime]] = {}
    session_ids: list[str] = []
    run_ids: list[str] = []
    action_ids: list[str] = []
    auth_hashes: list[str] = []
    clients: dict[str, TestClient] = {}
    record: dict = {"schema_version": "shoprelay.m5.2-c2-credentials.v1",
        "started_at": _utc(), "probe_id": probe_id, "database": "shoprelay_m11_test",
        "status": "RUNNING", "users": {}, "isolation": {}, "cleanup": {"status": "PENDING"},
        "password_values_or_hashes_recorded": False, "business_transactions": 0}
    app = None
    restore_status = "NOT_ATTEMPTED"
    try:
        with factory() as db:
            pg_version = db.execute(text("select current_setting('server_version')")).scalar_one()
            database = db.execute(text("select current_database()")).scalar_one()
            if database != "shoprelay_m11_test":
                raise RuntimeError("UNSAFE_DATABASE_TARGET")
            for owner_id in OWNERS:
                row = db.get(DemoCredential, owner_id)
                if row is None or not row.active:
                    raise RuntimeError(f"DEMO_CREDENTIAL_UNAVAILABLE:{owner_id}")
                saved[owner_id] = (row.password_hash, row.active, row.updated_at)
        record.update({"postgres_version": pg_version,
            "migration_revision": current_revision(database_url),
            "users": {owner_id: {"exists": True, "active_before": saved[owner_id][1],
                "active_restored": None, "credential_hash_restored_exactly": None,
                "user_selected_password_tested": False,
                "login_password_source": "ephemeral random value; original saved hash restored exactly"}
                for owner_id in OWNERS}})
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

        passwords = {owner_id: secrets.token_urlsafe(24) for owner_id in OWNERS}
        with factory() as db:
            for owner_id, password in passwords.items():
                set_demo_password(db, owner_id, password)

        app = create_app(database_url=database_url, engine=engine,
            action_writes_enabled=True, simulated_test_actions_enabled=True,
            return_actions_enabled=False, refund_actions_enabled=False,
            cancel_actions_enabled=False, ticket_actions_enabled=True,
            orchestrator_mode="legacy")
        if not app.state.action_writes_enabled or not app.state.simulated_test_actions_enabled:
            raise RuntimeError("TEST_PROPOSAL_GATE_UNAVAILABLE")

        clients = {owner_id: TestClient(app, raise_server_exceptions=False) for owner_id in OWNERS}
        headers: dict[str, dict[str, str]] = {}
        try:
            for owner_id, client in clients.items():
                headers[owner_id] = _login(client, owner_id, passwords[owner_id], auth_hashes)
                record["users"][owner_id]["login_http"] = 200
                record["users"][owner_id]["profile_http"] = 200

            for owner_id, client in clients.items():
                session = client.post("/api/sessions", headers=headers[owner_id],
                    json={"title": f"C2 credentials probe {probe_id}"})
                if session.status_code != 200:
                    raise RuntimeError(f"SESSION_CREATE_FAILED:{owner_id}:{session.status_code}")
                sid = session.json()["session"]["session_id"]
                session_ids.append(sid)
                run = client.post(f"/api/sessions/{sid}/runs", headers=headers[owner_id],
                    json={"client_request_id": f"c2-cred-{probe_id}-{owner_id}",
                          "request_payload": {"kind": "owner-scope-probe"}})
                if run.status_code != 200:
                    raise RuntimeError(f"RUN_CREATE_FAILED:{owner_id}:{run.status_code}")
                rid = run.json()["run"]["run_id"]
                run_ids.append(rid)
                action = client.post("/api/actions", headers=headers[owner_id], json={
                    "tool_id": "escalate_to_human", "session_id": sid, "run_id": rid,
                    "target_resource_id": "SUPPORT-GENERAL",
                    "arguments": {"issue_category": "OTHER", "order_id": None,
                        "user_description": "temporary isolated authentication probe",
                        "issue_summary": "owner access isolation only",
                        "reason_code": "C2_CREDENTIAL_OWNER_SCOPE", "related_agents": [],
                        "verified_fact_refs": [], "unverified_claim_codes": [],
                        "evidence_references": []},
                    "idempotency_key": f"c2-credentials-{probe_id}-{owner_id}"})
                if action.status_code != 200 or action.json()["action"]["status"] != "PENDING_CONFIRMATION":
                    raise RuntimeError(f"ACTION_PROPOSAL_FAILED:{owner_id}:{action.status_code}")
                aid = action.json()["action"]["action_id"]
                action_ids.append(aid)
                record["users"][owner_id].update({"session_id": sid, "run_id": rid, "action_id": aid})

            owner_client, other_client = clients["u_demo"], clients["u_other"]
            owner_resource = record["users"]["u_demo"]
            other_resource = record["users"]["u_other"]
            sid, rid, aid = owner_resource["session_id"], owner_resource["run_id"], owner_resource["action_id"]
            record["isolation"] = {
                "owner_session_actions": owner_client.get(f"/api/sessions/{sid}/actions").status_code,
                "other_session_actions": other_client.get(f"/api/sessions/{sid}/actions").status_code,
                "owner_action": owner_client.get(f"/api/actions/{aid}").status_code,
                "other_action": other_client.get(f"/api/actions/{aid}").status_code,
                "owner_run": owner_client.get(f"/api/runs/{rid}/execution").status_code,
                "other_run": other_client.get(f"/api/runs/{rid}/execution").status_code,
                "anonymous_session_actions": TestClient(app).get(f"/api/sessions/{sid}/actions").status_code,
                "anonymous_action": TestClient(app).get(f"/api/actions/{aid}").status_code,
                "anonymous_run": TestClient(app).get(f"/api/runs/{rid}/execution").status_code,
                "action_status_before_cleanup": owner_client.get(f"/api/actions/{aid}").json()["action"]["status"],
                "business_effect": "NONE_PENDING_CONFIRMATION"
            }
            expected = (200, 404, 200, 404, 200, 404, 401, 401, 401, "PENDING_CONFIRMATION")
            actual = tuple(record["isolation"][key] for key in (
                "owner_session_actions", "other_session_actions", "owner_action", "other_action",
                "owner_run", "other_run", "anonymous_session_actions", "anonymous_action",
                "anonymous_run", "action_status_before_cleanup"))
            if actual != expected:
                raise RuntimeError("OWNER_SCOPE_ASSERTION_FAILED")
        finally:
            for client in clients.values():
                client.close()

        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["failure_code"] = str(exc).split(":", 1)[0]
    finally:
        for client in clients.values():
            try:
                client.close()
            except Exception:
                pass
        restore_status = _restore_demo_credentials(factory, saved)
        residuals = {"actions": -1, "runs": -1, "sessions": -1, "messages": -1,
                     "actions_business_effects": -1}
        cleanup_error = None
        try:
            with factory() as db:
                if action_ids:
                    db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(action_ids)))
                    db.execute(delete(PendingAction).where(PendingAction.action_id.in_(action_ids)))
                if auth_hashes:
                    db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(auth_hashes)))
                if run_ids:
                    trace_ids = list(db.execute(select(Trace.trace_id).where(Trace.run_id.in_(run_ids))).scalars())
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
                residuals = {
                    "actions": db.scalar(select(func.count()).select_from(PendingAction).where(PendingAction.action_id.in_(action_ids))) if action_ids else 0,
                    "action_audits": db.scalar(select(func.count()).select_from(ActionAudit).where(ActionAudit.action_id.in_(action_ids))) if action_ids else 0,
                    "runs": db.scalar(select(func.count()).select_from(Run).where(Run.run_id.in_(run_ids))) if run_ids else 0,
                    "run_events": db.scalar(select(func.count()).select_from(RunEvent).where(RunEvent.run_id.in_(run_ids))) if run_ids else 0,
                    "traces": db.scalar(select(func.count()).select_from(Trace).where(Trace.run_id.in_(run_ids))) if run_ids else 0,
                    "observability": db.scalar(select(func.count()).select_from(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(run_ids))) if run_ids else 0,
                    "graph_execution": db.scalar(select(func.count()).select_from(GraphExecutionRecord).where(GraphExecutionRecord.run_id.in_(run_ids))) if run_ids else 0,
                    "sessions": db.scalar(select(func.count()).select_from(ConversationSession).where(ConversationSession.session_id.in_(session_ids))) if session_ids else 0,
                    "messages": db.scalar(select(func.count()).select_from(Message).where(Message.session_id.in_(session_ids))) if session_ids else 0,
                    "summaries": db.scalar(select(func.count()).select_from(SessionMemorySummary).where(SessionMemorySummary.session_id.in_(session_ids))) if session_ids else 0,
                    "auth_sessions": db.scalar(select(func.count()).select_from(AuthSession).where(AuthSession.token_hash.in_(auth_hashes))) if auth_hashes else 0,
                    "tickets": db.scalar(select(func.count()).select_from(SupportTicket).where(SupportTicket.action_id.in_(action_ids))) if action_ids else 0,
                    "ticket_events": db.scalar(select(func.count()).select_from(SupportTicketEvent).where(SupportTicketEvent.ticket_id.in_(
                        select(SupportTicket.ticket_id).where(SupportTicket.action_id.in_(action_ids))))) if action_ids else 0,
                    "business_transactions": 0,
                }
                credentials = {row.user_id: row for row in db.execute(select(DemoCredential).where(
                    DemoCredential.user_id.in_(OWNERS))).scalars()}
                for owner_id in OWNERS:
                    original = saved.get(owner_id)
                    record["users"].setdefault(owner_id, {}).update({
                        "active_restored": credentials[owner_id].active if owner_id in credentials else None,
                        "credential_hash_restored_exactly": bool(original and owner_id in credentials
                            and credentials[owner_id].password_hash == original[0]
                            and credentials[owner_id].active == original[1]
                            and credentials[owner_id].updated_at == original[2])})
        except Exception as exc:
            cleanup_error = type(exc).__name__
        record["credential_restore"] = restore_status
        record["cleanup"] = {"status": "PASS" if not any(v < 0 for v in residuals.values()) and not any(residuals.values()) else "FAIL",
            "residual_counts": residuals, "auth_sessions_deleted": len(auth_hashes),
            "error_type": cleanup_error}
        record["finished_at"] = _utc()
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if app is not None:
            try:
                app.state.engine.dispose()
            except Exception:
                pass
        engine.dispose()
    return 0 if record["status"] == "PASS" and restore_status == "PASS" and record["cleanup"]["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
