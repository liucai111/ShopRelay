"""M4.3-B4 isolated PostgreSQL ticket transaction probe.

Targets only shoprelay_m11_test, uses existing container/schema, and cleans only
the unique fixture IDs it creates. No external support system or notification is
called; all business rows are SIMULATED_TEST_DATA.
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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import delete, event, func, select, text, update

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import LATEST_SCHEMA_REVISION, current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import (ActionAudit, AuthSession, ConversationSession, DemoCredential,
    ObservabilityRecord, Order, PendingAction, Run, RunEvent, SupportTicket, SupportTicketEvent,
    Trace, utc_now)
from app.m1.services import set_demo_password
from app.m3.escalation import create_escalation_draft
from app.skills.registry import bind_skill, validate_skill_execution
from app.m4.actions import SERVER_PERMISSION_GRANTS
from scripts.g2_test_database import resolve_test_database_url

ROOT = Path(__file__).resolve().parents[1]


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _serve(database_url: str, port: int) -> None:
    import os
    os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
    import uvicorn
    app = create_app(database_url=database_url, action_writes_enabled=True,
        ticket_actions_enabled=True, orchestrator_mode="legacy")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="critical", access_log=False)


def _http_confirm(port: int, cookie: str, csrf: str, action_id: str, arguments_hash: str):
    body = json.dumps({"confirm": True, "arguments_hash": arguments_hash}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/actions/{action_id}/confirm",
        data=body, method="POST", headers={"Content-Type": "application/json",
        "Origin": f"http://127.0.0.1:{port}", CSRF_HEADER: csrf,
        "Cookie": f"shoprelay_session={cookie}; shoprelay_csrf={csrf}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode())


def _code(response) -> str | None:
    body = response.json()
    return body.get("code") or (body.get("detail") or {}).get("code")


def _login(client: TestClient, username: str, password: str):
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    origin = str(client.base_url).rstrip("/")
    response = client.post("/api/demo/login", headers={"Origin": origin, CSRF_HEADER: csrf},
        json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError(f"LOGIN_FAILED:{username}")
    return {"Origin": origin, CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def probe() -> dict:
    started = datetime.now(timezone.utc)
    phase = "database_connect"
    record = {"schema_version": "shoprelay.m4.3-b4-support-tickets-postgres.v1",
        "started_at": _utc(), "status": "NOT_RUN", "database_expected": "shoprelay_m11_test",
        "postgres_version": None, "migration": {}, "gates": {}, "cases": {},
        "counters": {"ticket_rows_created": 0, "ticket_events_created": 0,
            "ticket_reused_by_different_action": 0, "confirmation_attempts": 0,
            "deepseek_requests": 0, "mcp_write_calls": 0, "external_notifications": 0,
            "external_support_calls": 0, "payment_calls": 0, "production_writes": 0},
        "cleanup": {"status": "NOT_RUN"}}
    engine = factory = None
    clients: list[TestClient] = []
    servers = []
    fixture_actions: list[str] = []
    fixture_sessions: list[str] = []
    fixture_runs: list[str] = []
    fixture_orders: list[str] = []
    fixture_auth_hashes: list[str] = []
    saved_credentials = {}
    old_supervisor = __import__("os").environ.get("SHOPRELAY_M3_SUPERVISOR_ENABLED")
    suffix = secrets.token_hex(6)
    password = secrets.token_urlsafe(32)
    try:
        import os
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
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
        upgrade_database(database_url)
        record["migration"]["repeat_after"] = current_revision(database_url)
        record["migration"]["repeat_idempotent"] = record["migration"]["repeat_after"] == record["migration"]["after"]
        if record["migration"]["after"] != LATEST_SCHEMA_REVISION or not record["migration"]["repeat_idempotent"]:
            raise RuntimeError("MIGRATION_CHECK_FAILED")

        phase = "credentials_and_fixture"
        with factory() as db:
            for user_id in ("u_demo", "u_other"):
                credential = db.get(DemoCredential, user_id)
                if credential is None:
                    raise RuntimeError(f"DEMO_CREDENTIAL_MISSING:{user_id}")
                saved_credentials[user_id] = (credential.password_hash, credential.active, credential.updated_at)
                set_demo_password(db, user_id, password)
            for owner in ("u_demo", "u_other"):
                order_id = f"ORD-M43B4-{owner.upper()}-{suffix.upper()}"
                fixture_orders.append(order_id)
                db.add(Order(order_id=order_id, user_id=owner, status="CREATED",
                    paid_at=None, delivered_at=None, paid_cents=0, version=1))
            db.commit()

        app = create_app(database_url=database_url, engine=engine, action_writes_enabled=True,
            ticket_actions_enabled=True, orchestrator_mode="legacy")
        if not app.state.ticket_actions_enabled:
            raise RuntimeError("TICKET_ACTION_TEST_GATE_FAILED")
        record["gates"] = {"action_writes": app.state.action_writes_enabled,
            "ticket_actions": app.state.ticket_actions_enabled, "database": record["database"],
            "orchestrator": "legacy", "mcp_write": False, "production": False}
        client = TestClient(app, raise_server_exceptions=False)
        clients.append(client)
        headers = _login(client, "u_demo", password)
        main_cookie = client.cookies.get("shoprelay_session")
        main_csrf = client.cookies.get("shoprelay_csrf")
        fixture_auth_hashes.append(hashlib.sha256(main_cookie.encode()).hexdigest())
        created = client.post("/api/sessions", headers=headers, json={"title": "M4.3-B4 isolated support"})
        if created.status_code != 200:
            raise RuntimeError("SESSION_CREATE_FAILED")
        session_id = created.json()["session"]["session_id"]
        fixture_sessions.append(session_id)
        run_response = client.post(f"/api/sessions/{session_id}/runs", headers=headers,
            json={"client_request_id": f"m43b4-{suffix}", "request_payload": {"purpose": "isolated_ticket_probe"}})
        if run_response.status_code != 200:
            raise RuntimeError("RUN_CREATE_FAILED")
        run_id = run_response.json()["run"]["run_id"]
        fixture_runs.append(run_id)

        def propose(*, key: str, issue_summary: str, category: str = "RETURN_REFUND",
                    order_id: str | None = fixture_orders[0], description: str = "用户要求核实售后处理进度",
                    draft=None):
            target = order_id or "SUPPORT-GENERAL"
            action_arguments = (draft.pending_action_arguments() if draft is not None else {
                "issue_category": category, "order_id": order_id,
                "user_description": description, "issue_summary": issue_summary,
                "reason_code": "NEEDS_HUMAN_REVIEW", "related_agents": ["after_sales", "policy"],
                "verified_fact_refs": [], "unverified_claim_codes": ["USER_CLAIM_UNVERIFIED"],
                "evidence_references": []})
            return client.post("/api/actions", headers=headers, json={"tool_id": "escalate_to_human",
                "session_id": session_id, "run_id": run_id, "target_resource_id": target,
                "arguments": action_arguments, "idempotency_key": key})

        phase = "csrf_and_owner_checks"
        no_csrf = client.post("/api/actions", json={"tool_id": "escalate_to_human",
            "session_id": session_id, "run_id": run_id, "target_resource_id": fixture_orders[0],
            "arguments": {"issue_category": "RETURN_REFUND", "order_id": fixture_orders[0],
                "user_description": "验证CSRF拒绝", "issue_summary": "CSRF negative control",
                "reason_code": "CSRF_TEST"}, "idempotency_key": f"m43b4-{suffix}-no-csrf"})
        if no_csrf.status_code != 403:
            raise RuntimeError("CSRF_NOT_REQUIRED")
        forged = client.post("/api/actions", headers=headers, json={"tool_id": "escalate_to_human",
            "session_id": session_id, "run_id": run_id, "target_resource_id": fixture_orders[0],
            "arguments": {"issue_category": "OTHER", "order_id": fixture_orders[0],
                "user_description": "不允许客户端传身份", "issue_summary": "身份伪造测试",
                "reason_code": "FORGED_USER", "user_id": "u_other"},
            "idempotency_key": f"m43b4-{suffix}-forged-user"})
        if forged.status_code != 422:
            raise RuntimeError("CLIENT_USER_ID_ACCEPTED")
        route = {"strategy": "AGENT", "required_capabilities": ["escalate_to_human"]}
        skill_binding = bind_skill("请联系人工客服处理退款状态", route)
        if skill_binding is None:
            raise RuntimeError("HUMAN_ESCALATION_SKILL_NOT_BOUND")
        validate_skill_execution({**route, **skill_binding})
        draft = create_escalation_draft(permission_checker=lambda user_id, capability:
                capability in SERVER_PERMISSION_GRANTS.get(user_id, frozenset()),
            trusted_user_id="u_demo", authenticated=True, owner_verified=True,
            reason_code="NEEDS_HUMAN_REVIEW", order_ref=fixture_orders[0],
            summary_code="RETURN_REFUND_REVIEW", issue_category="RETURN_REFUND",
            user_description="用户称售后申请没有更新，联系 liu@example.invalid 或 13800138000",
            issue_summary="需要客服核对售后状态",
            related_agents=["after_sales", "policy"], unverified_claim_codes=["USER_CLAIM_UNVERIFIED"],
            evidence_refs=[])
        if draft.ticket_created or draft.notification_sent or draft.execution_status != "NOT_EXECUTED":
            raise RuntimeError("AGENT_DRAFT_EXECUTED_WITHOUT_CONFIRMATION")
        record["cases"]["agent_skill_structured_draft"] = {"skill_id": skill_binding["skill_id"],
            "proposal_tool": "escalate_to_human", "draft_status": draft.status,
            "ticket_created": draft.ticket_created, "execution_status": draft.execution_status,
            "tool_calls": 0}
        unauthorized_order = propose(key=f"m43b4-{suffix}-foreign-order", issue_summary="他人订单工单",
            order_id=fixture_orders[1])
        record["cases"]["foreign_order_rejected"] = {"status": unauthorized_order.status_code,
            "error_code": _code(unauthorized_order)}
        if unauthorized_order.status_code != 404:
            raise RuntimeError("FOREIGN_ORDER_DISCLOSURE")

        phase = "unconfirmed_proposal"
        main_proposal = propose(key=f"m43b4-{suffix}-main", issue_summary="需要客服核对售后状态",
            description="请核实联系 liu@example.invalid 或 13800138000 的申请", draft=draft)
        if main_proposal.status_code != 200:
            raise RuntimeError(f"TICKET_PROPOSAL_FAILED:{main_proposal.status_code}:{main_proposal.text[:300]}")
        main_action = main_proposal.json()["action"]
        fixture_actions.append(main_action["action_id"])
        with factory() as db:
            unconfirmed_count = db.scalar(select(func.count()).select_from(SupportTicket))
            pending_status = db.get(PendingAction, main_action["action_id"]).status
        record["cases"]["unconfirmed_creates_no_ticket"] = {"action_status": pending_status,
            "ticket_rows": unconfirmed_count}
        if pending_status != "PENDING_CONFIRMATION" or unconfirmed_count != 0:
            raise RuntimeError("TICKET_CREATED_BEFORE_CONFIRMATION")

        other = TestClient(app, raise_server_exceptions=False)
        clients.append(other)
        other_headers = _login(other, "u_other", password)
        other_cookie = other.cookies.get("shoprelay_session")
        fixture_auth_hashes.append(hashlib.sha256(other_cookie.encode()).hexdigest())
        wrong_owner = other.post(f"/api/actions/{main_action['action_id']}/confirm", headers=other_headers,
            json={"confirm": True, "arguments_hash": main_action["arguments_hash"]})
        if wrong_owner.status_code != 404:
            raise RuntimeError("CROSS_OWNER_ACTION_ACCESS")

        phase = "confirmed_ticket_transaction"
        confirm_started = time.perf_counter()
        committed = client.post(f"/api/actions/{main_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": main_action["arguments_hash"]})
        latency_ms = round((time.perf_counter() - confirm_started) * 1000, 3)
        if committed.status_code != 200:
            raise RuntimeError(f"TICKET_CONFIRM_FAILED:{committed.status_code}:{committed.text[:300]}")
        ticket_result = committed.json().get("ticket") or committed.json()["action"].get("result")
        ticket_id = ticket_result["ticket_id"]
        record["counters"]["confirmation_attempts"] += 1
        record["counters"]["ticket_rows_created"] += 1
        record["counters"]["ticket_events_created"] += 1
        record["cases"]["ticket_submitted"] = {"http_status": committed.status_code,
            "action_status": committed.json()["action"]["status"], "ticket_id": ticket_id,
            "ticket_status": ticket_result["status"], "business_effect": committed.json()["business_effect"],
            "assigned": False, "notification_sent": False, "handled": False,
            "latency_ms": latency_ms,
            "sanitized_description": committed.json()["action"]["sanitized_arguments"].get("user_description")}
        if ticket_result["status"] != "SUBMITTED_PENDING_ACCEPTANCE":
            raise RuntimeError("FALSE_TICKET_LIFECYCLE_STATUS")
        same_replay = client.post(f"/api/actions/{main_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": main_action["arguments_hash"]})
        ticket_read = client.get(f"/api/tickets/{ticket_id}")
        foreign_ticket = other.get(f"/api/tickets/{ticket_id}")
        if same_replay.status_code != 200 or not same_replay.json()["replayed"] or ticket_read.status_code != 200 or foreign_ticket.status_code != 404:
            raise RuntimeError("TICKET_REPLAY_OR_OWNER_READ_FAILED")
        if "[REDACTED_EMAIL]" not in ticket_read.json()["ticket"]["user_description"] or "[REDACTED_PHONE]" not in ticket_read.json()["ticket"]["user_description"]:
            raise RuntimeError("TICKET_DESCRIPTION_REDACTION_FAILED")
        record["cases"]["same_action_replay_and_owner_read"] = {"replay_http": same_replay.status_code,
            "replayed": same_replay.json()["replayed"], "owner_read": ticket_read.status_code,
            "other_user_read": foreign_ticket.status_code}

        different_action = propose(key=f"m43b4-{suffix}-main-duplicate", issue_summary="需要客服核对售后状态",
            description="重复草案的描述不同但归一化议题相同")
        if different_action.status_code != 200:
            raise RuntimeError("DIFFERENT_ACTION_PROPOSAL_FAILED")
        duplicate_action = different_action.json()["action"]
        fixture_actions.append(duplicate_action["action_id"])
        duplicate_confirm = client.post(f"/api/actions/{duplicate_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": duplicate_action["arguments_hash"]})
        if duplicate_confirm.status_code != 200 or not duplicate_confirm.json()["action"]["result"].get("reused_existing"):
            raise RuntimeError("OPEN_TICKET_DEDUPE_FAILED")
        record["counters"]["ticket_reused_by_different_action"] += 1
        with factory() as db:
            open_rows = db.scalar(select(func.count()).select_from(SupportTicket).where(
                SupportTicket.owner_id == "u_demo", SupportTicket.dedupe_key == db.get(SupportTicket, ticket_id).dedupe_key))
        if open_rows != 1:
            raise RuntimeError("OPEN_TICKET_DUPLICATED")

        general = propose(key=f"m43b4-{suffix}-general", issue_summary="需要解释如何查询人工服务进度",
            category="POLICY_QUESTION", order_id=None, description="一般客服流程咨询")
        if general.status_code != 200:
            raise RuntimeError("GENERAL_TICKET_PROPOSAL_FAILED")
        general_action = general.json()["action"]
        fixture_actions.append(general_action["action_id"])
        general_confirm = client.post(f"/api/actions/{general_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": general_action["arguments_hash"]})
        if general_confirm.status_code != 200 or general_confirm.json()["action"]["result"]["status"] != "SUBMITTED_PENDING_ACCEPTANCE":
            raise RuntimeError("GENERAL_TICKET_SUBMISSION_FAILED")
        record["counters"]["ticket_rows_created"] += 1
        record["counters"]["ticket_events_created"] += 1

        phase = "closed_action_states"
        expired_proposal = propose(key=f"m43b4-{suffix}-expired", issue_summary="expired action probe",
            category="OTHER", order_id=None, description="SIMULATED_TEST_DATA expired action")
        expired_action = expired_proposal.json()["action"]
        fixture_actions.append(expired_action["action_id"])
        with factory() as db:
            db.execute(update(PendingAction).where(
                PendingAction.action_id == expired_action["action_id"]).values(
                    expires_at=utc_now() - timedelta(seconds=1)))
            db.commit()
        expired_response = client.post(f"/api/actions/{expired_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": expired_action["arguments_hash"]})
        rejected_proposal = propose(key=f"m43b4-{suffix}-rejected", issue_summary="rejected action probe",
            category="OTHER", order_id=None, description="SIMULATED_TEST_DATA rejected action")
        rejected_action = rejected_proposal.json()["action"]
        fixture_actions.append(rejected_action["action_id"])
        reject_response = client.post(f"/api/actions/{rejected_action['action_id']}/reject", headers=headers,
            json={"acknowledge": True})
        rejected_confirm = client.post(f"/api/actions/{rejected_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": rejected_action["arguments_hash"]})
        with factory() as db:
            invalid_state_ticket_rows = db.scalar(select(func.count()).select_from(SupportTicket).where(
                SupportTicket.action_id.in_([expired_action["action_id"], rejected_action["action_id"]])))
        if (expired_response.status_code != 410 or reject_response.status_code != 200 or
            rejected_confirm.status_code != 409 or invalid_state_ticket_rows != 0):
            raise RuntimeError("CLOSED_ACTION_STATE_GUARD_FAILED")
        record["cases"]["expired_and_rejected_actions"] = {"expired_http": expired_response.status_code,
            "expired_code": _code(expired_response), "reject_http": reject_response.status_code,
            "rejected_confirm_http": rejected_confirm.status_code,
            "rejected_confirm_code": _code(rejected_confirm), "ticket_rows": invalid_state_ticket_rows}

        phase = "transaction_failure_rollback"
        rollback_proposal = propose(key=f"m43b4-{suffix}-rollback", issue_summary="注入事务故障后回滚工单",
            category="OTHER", order_id=None, description="SIMULATED_TEST_DATA rollback case")
        rollback_action = rollback_proposal.json()["action"]
        fixture_actions.append(rollback_action["action_id"])
        inject = {"armed": True}
        def fail_ticket_event(conn, cursor, statement, parameters, context, executemany):
            if inject["armed"] and statement.lstrip().lower().startswith("insert into support_ticket_events"):
                inject["armed"] = False
                raise RuntimeError("M43B4_INJECTED_TICKET_EVENT_FAILURE")
        event.listen(engine, "before_cursor_execute", fail_ticket_event)
        try:
            failed = client.post(f"/api/actions/{rollback_action['action_id']}/confirm", headers=headers,
                json={"confirm": True, "arguments_hash": rollback_action["arguments_hash"]})
        finally:
            event.remove(engine, "before_cursor_execute", fail_ticket_event)
        with factory() as db:
            rollback_state = {"ticket_rows": db.scalar(select(func.count()).select_from(SupportTicket).where(
                    SupportTicket.action_id == rollback_action["action_id"])),
                "event_rows": db.scalar(select(func.count()).select_from(SupportTicketEvent).where(
                    SupportTicketEvent.source_ref == f"action:{rollback_action['action_id']}:ticket-submitted")),
                "action_status": db.get(PendingAction, rollback_action["action_id"]).status,
                "audit_rows": db.scalar(select(func.count()).select_from(ActionAudit).where(
                    ActionAudit.action_id == rollback_action["action_id"]))}
        if inject["armed"] or failed.status_code < 500 or rollback_state != {
            "ticket_rows": 0, "event_rows": 0, "action_status": "PENDING_CONFIRMATION", "audit_rows": 1}:
            raise RuntimeError(f"TICKET_TRANSACTION_ROLLBACK_FAILED:{rollback_state}")
        record["cases"]["transaction_rollback"] = {"http_status": failed.status_code,
            "injection_fired": True, "after_rollback": rollback_state}
        recovered = client.post(f"/api/actions/{rollback_action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": rollback_action["arguments_hash"]})
        if recovered.status_code != 200:
            raise RuntimeError("TICKET_ACTION_RECOVERY_FAILED")
        record["counters"]["ticket_rows_created"] += 1
        record["counters"]["ticket_events_created"] += 1

        phase = "two_process_confirmation"
        race = propose(key=f"m43b4-{suffix}-race", issue_summary="并发确认只提交一次人工工单",
            category="OTHER", order_id=None, description="SIMULATED_TEST_DATA concurrent case")
        race_action = race.json()["action"]
        fixture_actions.append(race_action["action_id"])
        ports = [_port(), _port()]
        context = multiprocessing.get_context("spawn")
        for port in ports:
            proc = context.Process(target=_serve, args=(database_url, port), daemon=True)
            proc.start()
            servers.append(proc)
        for _ in range(100):
            if all(_port_open(port) for port in ports):
                break
            time.sleep(0.1)
        if not all(_port_open(port) for port in ports):
            raise RuntimeError("TICKET_API_PROCESSES_NOT_READY")
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = [f.result() for f in [pool.submit(_http_confirm, port, main_cookie,
                main_csrf, race_action["action_id"], race_action["arguments_hash"]) for port in ports]]
        with factory() as db:
            race_ticket_count = db.scalar(select(func.count()).select_from(SupportTicket).where(
                SupportTicket.action_id == race_action["action_id"]))
            race_event_count = db.scalar(select(func.count()).select_from(SupportTicketEvent).where(
                SupportTicketEvent.source_ref == f"action:{race_action['action_id']}:ticket-submitted"))
        if any(status != 200 for status, _ in results) or race_ticket_count != 1 or race_event_count != 1:
            raise RuntimeError("TWO_PROCESS_TICKET_IDEMPOTENCY_FAILED")
        record["counters"]["confirmation_attempts"] += 2
        record["counters"]["ticket_rows_created"] += 1
        record["counters"]["ticket_events_created"] += 1
        record["cases"]["two_process_confirm"] = {"http_statuses": [x[0] for x in results],
            "ticket_rows": race_ticket_count, "ticket_events": race_event_count}
        for proc in servers:
            proc.terminate()
            proc.join(timeout=5)
        servers.clear()

        phase = "restart_and_trace_audit"
        reopened = create_app(database_url=database_url, engine=engine,
            action_writes_enabled=True, ticket_actions_enabled=True, orchestrator_mode="legacy")
        with TestClient(reopened, raise_server_exceptions=False) as restarted:
            restarted.cookies.set("shoprelay_session", main_cookie)
            restarted.cookies.set("shoprelay_csrf", main_csrf)
            restored = restarted.get(f"/api/tickets/{ticket_id}")
            owner_list = restarted.get("/api/tickets")
            if restored.status_code != 200 or restored.json()["ticket"]["ticket_id"] != ticket_id:
                raise RuntimeError("TICKET_RESTART_RECOVERY_FAILED")
            if not any(item["ticket_id"] == ticket_id for item in owner_list.json()["tickets"]):
                raise RuntimeError("TICKET_OWNER_LIST_MISSING")
        with factory() as db:
            audit_count = db.scalar(select(func.count()).select_from(ActionAudit).where(
                ActionAudit.action_id == main_action["action_id"]))
            run_event_count = db.scalar(select(func.count()).select_from(RunEvent).where(
                RunEvent.run_id == run_id, RunEvent.event_key.like(f"action:{main_action['action_id']}:%")))
            trace_count = db.scalar(select(func.count()).select_from(Trace).where(Trace.run_id == run_id))
            span_count = db.scalar(select(func.count()).select_from(ObservabilityRecord).where(
                ObservabilityRecord.run_id == run_id, ObservabilityRecord.record_type == "span"))
            run_status = db.get(Run, run_id).status
        record["cases"]["run_trace_audit"] = {"action_audit_rows": audit_count,
            "run_action_events": run_event_count, "trace_rows_for_run": trace_count,
            "span_rows": span_count, "run_status_unchanged": run_status == "CREATED"}
        if audit_count < 4 or run_event_count < 3 or trace_count < 1 or span_count < 1 or run_status != "CREATED":
            raise RuntimeError("TICKET_OBSERVABILITY_LINKAGE_FAILED")
        record["status"] = "PASS"
    except Exception as exc:
        record["status"] = "FAIL"
        record["failure"] = {"phase": phase, "type": type(exc).__name__, "message": str(exc)[:500]}
    finally:
        for proc in servers:
            proc.terminate()
            proc.join(timeout=5)
        for client in clients:
            try:
                client.close()
            except Exception:
                pass
        if factory is not None:
            try:
                with factory() as db:
                    # Exact probe-ID cleanup only, in FK-safe order.
                    ticket_ids = db.execute(select(SupportTicket.ticket_id).where(
                        SupportTicket.action_id.in_(fixture_actions))).scalars().all() if fixture_actions else []
                    if ticket_ids:
                        db.execute(delete(SupportTicketEvent).where(SupportTicketEvent.ticket_id.in_(ticket_ids)))
                    if fixture_actions:
                        db.execute(delete(SupportTicket).where(SupportTicket.action_id.in_(fixture_actions)))
                        db.execute(delete(ActionAudit).where(ActionAudit.action_id.in_(fixture_actions)))
                        db.execute(delete(PendingAction).where(PendingAction.action_id.in_(fixture_actions)))
                    if fixture_runs:
                        db.execute(delete(RunEvent).where(RunEvent.run_id.in_(fixture_runs)))
                        db.execute(delete(Trace).where(Trace.run_id.in_(fixture_runs)))
                        db.execute(delete(ObservabilityRecord).where(ObservabilityRecord.run_id.in_(fixture_runs)))
                        db.execute(delete(Run).where(Run.run_id.in_(fixture_runs)))
                    if fixture_sessions:
                        db.execute(delete(ConversationSession).where(ConversationSession.session_id.in_(fixture_sessions)))
                    if fixture_auth_hashes:
                        db.execute(delete(AuthSession).where(AuthSession.token_hash.in_(fixture_auth_hashes)))
                    if fixture_orders:
                        db.execute(delete(Order).where(Order.order_id.in_(fixture_orders)))
                    for user_id, saved_credential in saved_credentials.items():
                        credential = db.get(DemoCredential, user_id)
                        credential.password_hash, credential.active, credential.updated_at = saved_credential
                    db.commit()
                with factory() as db:
                    residual = {"tickets": db.scalar(select(func.count()).select_from(SupportTicket).where(
                            SupportTicket.source_ref.like("action:%:ticket"))),
                        "ticket_events": db.scalar(select(func.count()).select_from(SupportTicketEvent).where(
                            SupportTicketEvent.source_ref.like("action:%:ticket-submitted"))),
                        "actions": db.scalar(select(func.count()).select_from(PendingAction).where(
                            PendingAction.action_id.in_(fixture_actions))) if fixture_actions else 0,
                        "sessions": db.scalar(select(func.count()).select_from(ConversationSession).where(
                            ConversationSession.session_id.in_(fixture_sessions))) if fixture_sessions else 0,
                        "runs": db.scalar(select(func.count()).select_from(Run).where(
                            Run.run_id.in_(fixture_runs))) if fixture_runs else 0,
                        "orders": db.scalar(select(func.count()).select_from(Order).where(
                            Order.order_id.in_(fixture_orders))) if fixture_orders else 0}
                record["cleanup"] = {"status": "PASS" if all(value == 0 for value in residual.values()) else "FAIL",
                    "exact_fixture_ids_only": True, "residual": residual}
                if record["cleanup"]["status"] != "PASS":
                    record["status"] = "FAIL"
                    record.setdefault("failure", {"phase": "cleanup", "type": "RuntimeError", "message": "fixture residuals remain"})
            except Exception as exc:
                record["cleanup"] = {"status": "FAIL", "error_type": type(exc).__name__, "message": str(exc)[:300]}
                record["status"] = "FAIL"
            engine.dispose()
        if old_supervisor is None:
            __import__("os").environ.pop("SHOPRELAY_M3_SUPERVISOR_ENABLED", None)
        else:
            __import__("os").environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = old_supervisor
        record["finished_at"] = _utc()
        record["duration_ms"] = round((datetime.now(timezone.utc) - started).total_seconds() * 1000, 3)
        record["git_head"] = __import__("subprocess").check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
        record["fixture_counts"] = {"actions": len(fixture_actions), "sessions": len(fixture_sessions),
            "runs": len(fixture_runs), "orders": len(fixture_orders), "auth_hashes": len(fixture_auth_hashes)}
        out = ROOT / "docs" / "probes" / f"m4.3-b4-support-tickets-postgres-{started.strftime('%Y%m%dT%H%M%SZ')}-{suffix}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        with out.open("x", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
        record["record_path"] = str(out.relative_to(ROOT)).replace("\\", "/")
    return record


if __name__ == "__main__":
    result = probe()
    print(json.dumps(result, ensure_ascii=False, indent=2))
    raise SystemExit(0 if result["status"] == "PASS" else 1)
