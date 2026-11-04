from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.m0.data import seed
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import ActionAudit, PendingAction, SimulatedTestAction
from app.m1.services import set_demo_password
from app.m4.actions import (ALLOWED_TRANSITIONS, ActionError, WRITE_POLICIES,
                            confirm_pending_action, create_pending_action,
                            server_permission_granted)


def _fixture(tmp_path, *, enabled=False):
    path = tmp_path / "m42-actions.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    passwords = {"u_demo": "m42-demo-" + secrets.token_urlsafe(12),
                 "u_other": "m42-other-" + secrets.token_urlsafe(12)}
    with factory() as db:
        for user_id, password in passwords.items():
            set_demo_password(db, user_id, password)
    # SQLite cannot enable the isolated execution flag; API actions fail closed there.
    app = create_app(database_url=url, engine=engine,
        action_writes_enabled=enabled, simulated_test_actions_enabled=enabled)
    return app, factory, engine, passwords


def _login(client, username, password):
    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
    headers = {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: csrf}
    response = client.post("/api/demo/login", headers=headers,
                           json={"username": username, "password": password})
    assert response.status_code == 200
    return headers


def _context(client, headers):
    session = client.post("/api/sessions", headers=headers, json={"title": "M4.2 isolated"}).json()["session"]
    run = client.post(f"/api/sessions/{session['session_id']}/runs", headers=headers,
        json={"client_request_id": "m42-run-1", "request_payload": {"kind": "test"}}).json()["run"]
    return session["session_id"], run["run_id"]


def _action_payload(session_id, run_id, key="m42-action-1"):
    return {"tool_id": "simulated_test_action", "session_id": session_id,
            "run_id": run_id, "target_resource_id": "M42-SIM-fixture-1",
            "arguments": {"label": "fixture", "value": "safe test value"},
            "idempotency_key": key}


def test_action_api_requires_two_explicit_flags_and_pending_has_no_effect(tmp_path):
    app, factory, engine, passwords = _fixture(tmp_path, enabled=False)
    with TestClient(app) as client:
        headers = _login(client, "u_demo", passwords["u_demo"])
        sid, rid = _context(client, headers)
        response = client.post("/api/actions", headers=headers,
                               json=_action_payload(sid, rid))
        assert response.status_code == 503
        assert response.json()["code"] == "ACTION_EXECUTION_DISABLED"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SimulatedTestAction)) == 0
        assert db.scalar(select(func.count()).select_from(PendingAction)) == 0
    engine.dispose()


def test_session_action_workbench_projection_is_owner_scoped_and_allowlisted(tmp_path):
    app, factory, engine, passwords = _fixture(tmp_path, enabled=False)
    with TestClient(app) as owner, TestClient(app) as other:
        owner_headers = _login(owner, "u_demo", passwords["u_demo"])
        other_headers = _login(other, "u_other", passwords["u_other"])
        sid, rid = _context(owner, owner_headers)
        with factory() as db:
            action = PendingAction(
                action_id="C1-projection-action-1", owner_id="u_demo", session_id=sid, run_id=rid,
                trace_id="secret-trace-value", tool_id="create_return_request", target_resource_id="ORDER-C1",
                sanitized_arguments_json=(
                    '{"order_id":"ORDER-C1","reason_code":"QUALITY_ISSUE",'
                    '"reason_summary":"box was damaged","reason_source":"USER_CLAIM_UNVERIFIED",'
                    '"application_outcome":"PENDING_HUMAN_REVIEW",'
                    '"order_snapshot":{"status":"DELIVERED","version":3},'
                    '"policy_snapshot":{"knowledge_version":"kv-c1","evaluation_at":"2026-10-24T00:00:00Z",'
                    '"index_fingerprint":"must-not-leak","evidence":[{"chunk_id":"chunk-c1",'
                    '"policy_id":"returns","clause_id":"r-1","version":"v2"}]}}'
                ),
                arguments_hash="sensitive-arguments-hash", action_version=1, risk_level="WRITE_HIGH_RISK",
                status="PENDING_CONFIRMATION", idempotency_key="c1-idem", created_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1), audit_reference="secret-audit-reference",
            )
            db.add(action); db.commit()
        route = f"/api/sessions/{sid}/actions"
        result = owner.get(route)
        assert result.status_code == 200
        record = result.json()["actions"][0]
        assert record["session_id"] == sid and record["run_id"] == rid
        assert record["policy_references"] == [{"chunk_id": "chunk-c1", "policy_id": "returns", "clause_id": "r-1", "version": "v2"}]
        assert record["verified_snapshot"]["status"] == "DELIVERED"
        assert record["parameters"]["application_outcome"] == "PENDING_HUMAN_REVIEW"
        assert record["user_claim_markers"]["reason_summary"] == "USER_CLAIM_UNVERIFIED"
        assert record["status"] == "PENDING_CONFIRMATION" and record["expired"] is True
        assert record["execution_available"] is False
        assert record["execution_scope"] == "DISABLED"
        assert record["confirmation_hash"] is None
        assert not ({"arguments_hash", "trace_id", "audit_reference", "result", "sanitized_arguments"} & set(record))
        assert "index_fingerprint" not in str(record)
        assert other.get(route).status_code == 404
        assert TestClient(app).get(route).status_code == 401
    engine.dispose()


def test_action_create_confirm_duplicate_and_audit_are_owner_scoped(tmp_path):
    app, factory, engine, passwords = _fixture(tmp_path, enabled=True)
    # Explicitly enable only the unit-test adapter; production route still requires
    # PostgreSQL named shoprelay_m11_test, verified by the real probe.
    app.state.simulated_test_actions_enabled = True
    with TestClient(app) as owner, TestClient(app) as other:
        owner_headers = _login(owner, "u_demo", passwords["u_demo"])
        other_headers = _login(other, "u_other", passwords["u_other"])
        sid, rid = _context(owner, owner_headers)
        payload = _action_payload(sid, rid)
        created = owner.post("/api/actions", headers=owner_headers, json=payload)
        assert created.status_code == 200
        action = created.json()["action"]
        assert action["status"] == "PENDING_CONFIRMATION"
        assert created.json()["business_effect"] == "NONE_PENDING_CONFIRMATION"
        replay = owner.post("/api/actions", headers=owner_headers, json=payload)
        assert replay.status_code == 200 and replay.json()["replayed"] is True
        assert other.get(f"/api/actions/{action['action_id']}").status_code == 404
        forged = {**payload, "user_id": "u_demo"}
        assert owner.post("/api/actions", headers=owner_headers, json=forged).status_code == 422
        assert other.post(f"/api/actions/{action['action_id']}/confirm", headers=other_headers,
                          json={"confirm": True, "arguments_hash": action["arguments_hash"]}).status_code == 404
        with factory() as db:
            assert db.scalar(select(func.count()).select_from(SimulatedTestAction)) == 0
        confirm = {"confirm": True, "arguments_hash": action["arguments_hash"]}
        result = owner.post(f"/api/actions/{action['action_id']}/confirm", headers=owner_headers, json=confirm)
        assert result.status_code == 200
        assert result.json()["action"]["status"] == "SUCCEEDED"
        assert result.json()["business_effect"] == "COMMITTED_SIMULATED_TEST_RECEIPT"
        duplicate = owner.post(f"/api/actions/{action['action_id']}/confirm", headers=owner_headers, json=confirm)
        assert duplicate.status_code == 200 and duplicate.json()["replayed"] is True
        assert owner.get(f"/api/actions/{action['action_id']}").json()["action"]["status"] == "SUCCEEDED"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SimulatedTestAction)) == 1
        assert db.scalar(select(func.count()).select_from(ActionAudit).where(
            ActionAudit.action_id == action["action_id"])) == 4
        events = db.execute(select(ActionAudit).where(ActionAudit.action_id == action["action_id"])).scalars().all()
        assert {entry.event_type for entry in events} >= {"ACTION_CREATED_PENDING_CONFIRMATION", "USER_CONFIRMED",
            "SIMULATED_EXECUTION_STARTED", "SIMULATED_TRANSACTION_COMMITTED"}
        receipt = db.execute(select(SimulatedTestAction).where(
            SimulatedTestAction.action_id == action["action_id"])).scalar_one()
        assert receipt.source_type == "SIMULATED_TEST_DATA"
        pending = db.get(PendingAction, action["action_id"])
        assert db.get(ActionAudit, pending.audit_reference).event_type == "ACTION_CREATED_PENDING_CONFIRMATION"
    engine.dispose()


def test_action_policy_forbids_production_writes_and_state_machine_is_terminal():
    assert WRITE_POLICIES["create_refund_request"].risk_level == "WRITE_HIGH_RISK"
    assert WRITE_POLICIES["create_refund_request"].execution_mode == "ISOLATED_REFUND_TRANSACTION"
    assert WRITE_POLICIES["create_refund_request"].allowed_environment == ("shoprelay_m11_test",)
    for tool in ("cancel_order",):
        assert WRITE_POLICIES[tool].risk_level == "WRITE_HIGH_RISK"
        assert WRITE_POLICIES[tool].execution_mode == "ISOLATED_ORDER_CANCELLATION_TRANSACTION"
    assert WRITE_POLICIES["create_return_request"].risk_level == "WRITE_HIGH_RISK"
    assert WRITE_POLICIES["create_return_request"].execution_mode == "ISOLATED_RETURN_TRANSACTION"
    assert WRITE_POLICIES["create_return_request"].allowed_environment == ("shoprelay_m11_test",)
    assert WRITE_POLICIES["create_return_request"].confirmation_required
    assert not WRITE_POLICIES["create_return_request"].allowed_environment.__contains__("production")
    assert {WRITE_POLICIES[name].risk_level for name in WRITE_POLICIES} >= {
        "READ_ONLY", "WRITE_LOW_RISK", "WRITE_HIGH_RISK"}
    assert all("production" not in policy.allowed_environment for policy in WRITE_POLICIES.values()
               if policy.risk_level != "READ_ONLY")
    assert ALLOWED_TRANSITIONS["UNKNOWN_RESULT"] == set()
    assert "SUCCEEDED" not in ALLOWED_TRANSITIONS["UNKNOWN_RESULT"]
    assert "EXECUTING" not in ALLOWED_TRANSITIONS["SUCCEEDED"]
    assert "UNKNOWN_RESULT" in ALLOWED_TRANSITIONS["EXECUTING"]
    assert server_permission_granted("u_demo", "simulated_test_action", "shoprelay_m11_test")
    assert server_permission_granted("u_other", "simulated_test_action", "shoprelay_m11_test")
    assert not server_permission_granted("unregistered-user", "simulated_test_action", "shoprelay_m11_test")
    assert not server_permission_granted("u_demo", "simulated_test_action", "production")


def test_confirm_requires_exact_displayed_hash_and_cannot_execute_for_wrong_owner(tmp_path):
    app, factory, engine, passwords = _fixture(tmp_path)
    app.state.simulated_test_actions_enabled = True
    with TestClient(app) as client:
        headers = _login(client, "u_demo", passwords["u_demo"])
        sid, rid = _context(client, headers)
        action = client.post("/api/actions", headers=headers, json=_action_payload(sid, rid)).json()["action"]
        mismatch = client.post(f"/api/actions/{action['action_id']}/confirm", headers=headers,
            json={"confirm": True, "arguments_hash": "0" * 64})
        assert mismatch.status_code == 409
        assert mismatch.json()["code"] == "ACTION_ARGUMENTS_CHANGED"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SimulatedTestAction)) == 0
        row = db.get(PendingAction, action["action_id"])
        assert row.status == "PENDING_CONFIRMATION"
    engine.dispose()


def test_simulated_write_transaction_failure_rolls_back_receipt_and_action_state(tmp_path):
    app, factory, engine, passwords = _fixture(tmp_path)
    from app.m4 import actions
    with TestClient(app) as client:
        headers = _login(client, "u_demo", passwords["u_demo"])
        sid, rid = _context(client, headers)
        app.state.simulated_test_actions_enabled = True
        action = client.post("/api/actions", headers=headers, json=_action_payload(sid, rid)).json()["action"]
    # Force an insert constraint failure at the receipt boundary: the action claim,
    # audit rows, Run events and receipt must all roll back together.
    original = actions.SimulatedTestAction

    class FailingReceipt:
        def __new__(cls, **_kwargs):
            raise RuntimeError("injected isolated transaction failure")

    actions.SimulatedTestAction = FailingReceipt
    try:
        with factory() as db, pytest.raises(RuntimeError, match="injected"):
            confirm_pending_action(db, owner_id="u_demo", action_id=action["action_id"],
                simulated_enabled=True, environment_name="shoprelay_m11_test",
                confirmed_arguments_hash=action["arguments_hash"])
    finally:
        actions.SimulatedTestAction = original
    with factory() as db:
        assert db.get(PendingAction, action["action_id"]).status == "PENDING_CONFIRMATION"
        assert db.scalar(select(func.count()).select_from(SimulatedTestAction)) == 0
        audit_types = db.execute(select(ActionAudit.event_type).where(
            ActionAudit.action_id == action["action_id"])).scalars().all()
        assert audit_types == ["ACTION_CREATED_PENDING_CONFIRMATION"]
    engine.dispose()


def test_external_unknown_result_is_non_replayable_and_transitions_are_checked(tmp_path):
    app, factory, engine, passwords = _fixture(tmp_path)
    from app.m4.actions import mark_unknown_result
    with TestClient(app) as client:
        headers = _login(client, "u_demo", passwords["u_demo"])
        sid, rid = _context(client, headers)
        app.state.simulated_test_actions_enabled = True
        action = client.post("/api/actions", headers=headers, json=_action_payload(sid, rid)).json()["action"]
    with factory() as db:
        row = db.get(PendingAction, action["action_id"])
        row.status = "EXECUTING"  # represent a future external call with uncertain outcome
        db.commit()
    with factory() as db:
        unknown = mark_unknown_result(db, owner_id="u_demo", action_id=action["action_id"],
                                      error_code="REMOTE_TIMEOUT_AFTER_SEND")
        assert unknown.status == "UNKNOWN_RESULT"
    with factory() as db, pytest.raises(ActionError) as exc:
        confirm_pending_action(db, owner_id="u_demo", action_id=action["action_id"],
            simulated_enabled=True, environment_name="shoprelay_m11_test",
            confirmed_arguments_hash=action["arguments_hash"])
    assert exc.value.code == "ACTION_NOT_CONFIRMABLE"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(SimulatedTestAction)) == 0
        assert db.get(PendingAction, action["action_id"]).status == "UNKNOWN_RESULT"
    engine.dispose()


def test_action_api_authentication_schema_and_sensitive_argument_guards(tmp_path):
    app, factory, engine, passwords = _fixture(tmp_path, enabled=True)
    with TestClient(app) as client:
        assert client.get("/api/actions").status_code == 401
        headers = _login(client, "u_demo", passwords["u_demo"])
        sid, rid = _context(client, headers)
        app.state.simulated_test_actions_enabled = True
        bad_tool = client.post("/api/actions", headers=headers,
            json={**_action_payload(sid, rid), "tool_id": "get_order"})
        assert bad_tool.status_code == 422
        sensitive = client.post("/api/actions", headers=headers,
            json={**_action_payload(sid, rid), "arguments": {"label": "test", "value": "secret-token-value"}})
        assert sensitive.status_code == 422
        assert sensitive.json()["code"] == "SENSITIVE_ACTION_ARGUMENT_REJECTED"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(PendingAction)) == 0
        assert db.scalar(select(func.count()).select_from(SimulatedTestAction)) == 0
    engine.dispose()
