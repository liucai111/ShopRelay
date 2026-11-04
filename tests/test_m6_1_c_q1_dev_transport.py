from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hashlib
import json
import threading
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.m1.api import CSRF_HEADER, create_app
from app.m1.models import (Base, ConversationSession, Message, ObservabilityRecord,
    Run, RunEvent, Trace, User, utc_now)
from app.m1.services import create_conversation, create_run, set_demo_password
from eval.m6_1.cleanup import EvalCleanupBlocked, cleanup_exact_eval_session
from eval.m6_1.budget import BudgetLedger, BudgetLimits, BudgetStop
from eval.m6_1.contracts import EvalCase
from eval.m6_1.experiment import preregister_experiment
from eval.m6_1.execution_modes import ExecutionMode
from eval.m6_1.shoprelay_adapter import DevSmokeShopRelayAdapter
from eval.m6_1.store import ResultStore
from eval.m6_1.dev_http_transport import (
    DevAuthorizationError, DevHttpTransport, DevSmokeGrantAuthority,
)


ASSET = {"split": "dev", "scoreability": "ELIGIBLE", "review_status": "BATCH_USER_CONFIRMED",
         "exposure_status": "UNEXPOSED_VERIFIED", "version": "dev-r1", "sha256": hashlib.sha256(b"meta").hexdigest()}


class MockState:
    def __init__(self):
        self.posts = 0
        self.requests = 0
        self.run_exists = False
        self.csrf = "mock-csrf-value"
        self.session_id = "mock-session-1"
        self.session_exists = False
        self.run_id = "mock-run-1"
        self.seen_password = False
        self.post_missing_response = True
        self.post_error_status = None


def make_handler(state):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return  # Never persist request bodies or credentials in the test server log.

        def _json(self, status, payload, headers=()):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            state.requests += 1
            if self.path == "/api/eval/dev/target":
                return self._json(200, {"environment": "ISOLATED_DEV", "database": "shoprelay_m11_test"})
            if self.path == "/api/demo/csrf":
                return self._json(200, {"csrf_token": state.csrf}, (("Set-Cookie", f"shoprelay_csrf={state.csrf}; Path=/; SameSite=Lax"),))
            if self.path.startswith("/api/eval/dev/sessions/lookup?"):
                if state.session_exists:
                    marker = self.path.split("marker=", 1)[-1]
                    return self._json(200, {"session": {"session_id": state.session_id,
                        "title": f"m6.1-eval:{marker}", "created_at": "2026-10-25T00:00:00Z"}})
                return self._json(404, {"detail": {"code": "NOT_FOUND"}})
            if self.path.startswith("/api/eval/dev/runs/lookup?"):
                if state.run_exists:
                    return self._json(200, {"run": {"run_id": state.run_id,
                        "session_id": state.session_id, "status": "SUCCEEDED"}})
                return self._json(404, {"detail": {"code": "NOT_FOUND"}})
            if self.path == f"/api/runs/{state.run_id}":
                return self._json(200, {"run": {"run_id": state.run_id, "session_id": state.session_id,
                    "status": "SUCCEEDED", "model_id": "stub-test", "usage": None,
                    "latency_ms": 7, "answer": {"text": "not projected"}}})
            if self.path.startswith(f"/api/runs/{state.run_id}/events"):
                return self._json(200, {"events": [{"event_id": "e1", "run_id": state.run_id,
                    "seq": 1, "type": "route_decided", "payload": {"strategy": "DIRECT"}}], "has_more": False})
            if self.path == f"/api/runs/{state.run_id}/execution":
                return self._json(200, {"execution": {"run_id": state.run_id,
                    "session_id": state.session_id, "status": "SUCCEEDED", "strategy": "DIRECT"}})
            return self._json(404, {})

        def do_POST(self):
            state.requests += 1
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length)
            if self.headers.get("Origin") != f"http://127.0.0.1:{self.server.server_port}":
                return self._json(403, {"detail": "bad origin"})
            if self.path == "/api/demo/login":
                payload = json.loads(body)
                state.seen_password = payload.get("password") == "mock-password-never-real"
                if self.headers.get("X-CSRF-Token") != state.csrf:
                    return self._json(403, {})
                return self._json(200, {"user": {"user_id": "dev-owner"}},
                    (("Set-Cookie", "shoprelay_session=mock-session-cookie; HttpOnly; Path=/; SameSite=Lax"),))
            if self.headers.get("X-CSRF-Token") != state.csrf:
                return self._json(403, {})
            if self.path == "/api/sessions":
                title = json.loads(body)["title"]
                state.session_exists = True
                return self._json(200, {"session": {"session_id": state.session_id,
                    "title": title, "created_at": "2026-10-25T00:00:00Z"}})
            if self.path == "/api/chat/stream":
                state.posts += 1
                state.run_exists = True  # Server persisted the idempotent Run before response loss.
                if state.post_error_status is not None:
                    return self._json(state.post_error_status, {"detail": "synthetic server error"})
                if state.post_missing_response:
                    self.connection.shutdown(1)
                    self.connection.close()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("X-Run-Id", state.run_id)
                self.send_header("X-Run-Status", "RUNNING")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            return self._json(404, {})

    return Handler


@pytest.fixture
def mock_server():
    state = MockState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def authority_for(base):
    return DevSmokeGrantAuthority(issuer="dev-issuer", trusted_issuers={"dev-issuer"},
        signing_key=b"d" * 32, allowed_targets={base}, allowed_assets={"dev-r1": ASSET})


def memory_database():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as db:
        db.add_all([User(user_id="u_demo", display_name="Synthetic owner"),
                    User(user_id="u_other", display_name="Synthetic other")])
        db.commit()
    return engine, factory


def test_dev_transport_cookie_csrf_unknown_post_and_lookup_without_repost(mock_server):
    state, base = mock_server
    authority = authority_for(base)
    grant = authority.issue(asset_id="dev-r1", asset_version="dev-r1", asset_sha256=ASSET["sha256"],
        subject="dev-owner", base_url=base)
    transport = DevHttpTransport(base_url=base, grant=grant, authority=authority,
        subject="dev-owner", username="synthetic-user", password="mock-password-never-real")
    assert transport.authenticate() == "dev-owner"
    session = transport.create_session(title="m6.1-eval:" + "a" * 32)["session"]
    assert session["session_id"] == state.session_id
    with pytest.raises(TimeoutError):
        transport.post_chat_stream(session_id=state.session_id, message="synthetic question",
                                   client_request_id="m61c:" + "a" * 32 + ":case01")
    assert state.posts == 1
    found = transport.lookup_run(session_id=state.session_id,
                                 client_request_id="m61c:" + "a" * 32 + ":case01")
    assert found["run_id"] == state.run_id
    replay = transport.post_chat_stream(session_id=state.session_id, message="synthetic question",
                                        client_request_id="m61c:" + "a" * 32 + ":case01")
    assert replay["recovered"] is True
    assert state.posts == 1
    assert state.requests == transport._requests == 9
    assert state.seen_password is True
    assert "mock-password-never-real" not in repr(transport)
    assert transport._password == ""
    request_count = state.requests
    with pytest.raises(DevAuthorizationError):
        transport.get_run(run_id="../../api/demo/me")
    with pytest.raises(DevAuthorizationError):
        transport.post_chat_stream(session_id="../../sessions", message="synthetic",
            client_request_id="m61c:" + "a" * 32 + ":case01")
    assert state.requests == request_count


def test_dev_grant_rejects_synthetic_candidate_and_unallowlisted_targets(mock_server):
    _, base = mock_server
    authority = authority_for(base)
    with pytest.raises(DevAuthorizationError):
        authority.issue(asset_id="test_candidate", asset_version="dev-r1",
            asset_sha256=ASSET["sha256"], subject="dev-owner", base_url=base)
    with pytest.raises(DevAuthorizationError):
        authority.issue(asset_id="dev-r1", asset_version="dev-r1",
            asset_sha256=ASSET["sha256"], subject="dev-owner", base_url="https://example.invalid")
    with pytest.raises(DevAuthorizationError):
        DevSmokeGrantAuthority(issuer="dev-issuer", trusted_issuers={"dev-issuer"},
            signing_key=b"d" * 32, allowed_targets={base}, allowed_assets={"test_candidate": ASSET})
    uncertain = {**ASSET, "exposure_status": "NOT_VERIFIED"}
    uncertain_authority = DevSmokeGrantAuthority(issuer="dev-issuer", trusted_issuers={"dev-issuer"},
        signing_key=b"d" * 32, allowed_targets={base}, allowed_assets={"dev-r1": uncertain})
    with pytest.raises(DevAuthorizationError):
        uncertain_authority.issue(asset_id="dev-r1", asset_version="dev-r1",
            asset_sha256=uncertain["sha256"], subject="dev-owner", base_url=base)


def test_eight_case_template_is_unreviewed_and_has_expected_route_coverage():
    path = Path("eval/m6_1/templates/m6.1-c-dev-smoke-case-template-v1.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["status"] == "TEMPLATE_ONLY_NOT_AUTHORIZED"
    assert payload["independent_review_status"] == "NOT_REVIEWED"
    assert len(payload["cases"]) == 8
    counts = {}
    for case in payload["cases"]:
        route = case["expected_route_proposal"]
        counts[route] = counts.get(route, 0) + 1
        assert case["expected_outcome"] == "PENDING_HUMAN_REVIEW"
        assert case["reviewer_identity"] == "UNKNOWN" and case["reviewed_at_utc"] is None
        assert case["data_source"] == "NEW_SYNTHETIC_EXAMPLE"
    assert counts == {"DIRECT": 1, "SINGLE": 2, "MULTI": 2, "AGENT": 2, "REFUSE": 1}


def test_dev_target_endpoint_is_hidden_by_default():
    app = create_app(database_url="sqlite:///:memory:")
    with TestClient(app) as client:
        assert client.get("/api/eval/dev/target").status_code == 404


def test_dev_run_lookup_is_authenticated_and_owner_scoped():
    engine, factory = memory_database()
    db_url = "sqlite://"
    password = "synthetic-only-password-123"
    with factory() as db:
        set_demo_password(db, "u_demo", password)
        set_demo_password(db, "u_other", "synthetic-only-password-456")
        owner_session = create_conversation(db, "u_demo", "synthetic")
        foreign_session = create_conversation(db, "u_other", "synthetic")
        run, _ = create_run(db, "u_demo", owner_session.session_id, "m61c:lookup:case01",
            {"message": "synthetic", "requested_session_id": owner_session.session_id},
            idempotency_scope=f"chat:{owner_session.session_id}")
        run_id = run.run_id
    app = create_app(database_url=db_url, engine=engine)
    app.state.eval_dev_api_enabled = True  # Route behavior test only; the real enable predicate is tested separately.
    try:
        with TestClient(app) as client:
            assert client.get("/api/eval/dev/runs/lookup", params={
                "session_id": owner_session.session_id, "client_request_id": "m61c:lookup:case01"}).status_code == 401
            csrf = client.get("/api/demo/csrf").json()["csrf_token"]
            headers = {"Origin": str(client.base_url).rstrip("/"), CSRF_HEADER: csrf}
            assert client.post("/api/demo/login", headers=headers,
                json={"username": "u_demo", "password": password}).status_code == 200
            own = client.get("/api/eval/dev/runs/lookup", params={
                "session_id": owner_session.session_id, "client_request_id": "m61c:lookup:case01"})
            assert own.status_code == 200
            assert own.json()["run"] == {"run_id": run_id, "session_id": owner_session.session_id,
                "status": "CREATED", "created_at": own.json()["run"]["created_at"]}
            foreign = client.get("/api/eval/dev/runs/lookup", params={
                "session_id": foreign_session.session_id, "client_request_id": "m61c:lookup:case01"})
            assert foreign.status_code == 404
            assert "answer" not in own.json()["run"]
    finally:
        engine.dispose()


def test_exact_cleanup_removes_only_marked_synthetic_scope_and_reports_residuals():
    engine, factory = memory_database()
    marker = "a" * 32
    run_id = None
    session_id = None
    try:
        with factory() as db:
            session = create_conversation(db, "u_demo", f"m6.1-eval:{marker}")
            session_id = session.session_id
            request_id = f"m61c:{marker}:case01"
            run, _ = create_run(db, "u_demo", session_id, request_id,
                {"message": "synthetic", "requested_session_id": session_id},
                idempotency_scope=f"chat:{session_id}")
            run.status = "SUCCEEDED"
            run_id = run.run_id
            db.add(Message(message_id="msg-cleanup-q1", session_id=session_id, role="user",
                content="synthetic", created_at=utc_now(), sequence=1, status="RECORDED"))
            db.add(RunEvent(event_id="evt-cleanup-q1", run_id=run_id, session_id=session_id,
                trace_id="trace-cleanup-q1", seq=1, timestamp=utc_now(), event_type="started", payload_json="{}"))
            db.add(Trace(trace_id="trace-cleanup-q1", user_id="u_demo", run_id=run_id,
                event_json="{}", created_at=utc_now(), event_type="started", status="RECORDED"))
            db.add(ObservabilityRecord(record_id="span-cleanup-q1", run_id=run_id, user_id="u_demo",
                trace_id="trace-cleanup-q1", graph_version="test", record_type="span",
                timestamp=utc_now(), payload_json="{}"))
            db.commit()
            counts = cleanup_exact_eval_session(db, owner_id="u_demo", session_id=session_id,
                run_ids=(run_id,), marker=marker, checkpoint_schema="langgraph_checkpoint",
                allow_sqlite_test=True)
            assert counts["messages"] == 1 and counts["runs"] == 1 and counts["sessions"] == 1
            assert counts["run_events"] == 1 and counts["traces"] == 2 and counts["spans"] == 1
            assert counts["remaining_objects"] == 0
            assert db.get(ConversationSession, session_id) is None
            assert db.get(Run, run_id) is None
            assert db.get(Message, "msg-cleanup-q1") is None
        with factory() as db:
            # A neighboring ordinary session is outside the submitted exact scope.
            ordinary = create_conversation(db, "u_demo", "ordinary preserved")
            assert db.get(ConversationSession, ordinary.session_id) is not None
    finally:
        engine.dispose()


def test_exact_cleanup_refuses_cross_owner_artifacts_before_any_deletion():
    engine, factory = memory_database()
    marker = "e" * 32
    try:
        with factory() as db:
            session = create_conversation(db, "u_demo", f"m6.1-eval:{marker}")
            own, _ = create_run(db, "u_demo", session.session_id, f"m61c:{marker}:case01",
                {"message": "synthetic", "requested_session_id": session.session_id},
                idempotency_scope=f"chat:{session.session_id}")
            own.status = "SUCCEEDED"
            foreign = Run(run_id="foreign-run-q1", user_id="u_other", session_id=session.session_id,
                client_request_id="foreign-request", idempotency_scope=f"chat:{session.session_id}",
                request_hash="f" * 64, status="FAILED", created_at=utc_now(), updated_at=utc_now())
            db.add(foreign)
            db.commit()
            with pytest.raises(EvalCleanupBlocked, match="FOREIGN_OWNER_RUN_IN_SESSION"):
                cleanup_exact_eval_session(db, owner_id="u_demo", session_id=session.session_id,
                    run_ids=(own.run_id,), marker=marker,
                    checkpoint_schema="langgraph_checkpoint", allow_sqlite_test=True)
            assert db.get(ConversationSession, session.session_id) is not None
            assert db.get(Run, own.run_id) is not None
            assert db.get(Run, foreign.run_id) is not None
    finally:
        engine.dispose()


def _dev_ledger(path, *, provider_calls=40, embedding_calls=8):
    store = ResultStore(path)
    limits = BudgetLimits(provider_calls=provider_calls, embedding_calls=embedding_calls,
        judge_calls=0, total_tokens=120_000, estimated_usd=5.0, tool_calls=0,
        max_calls_per_trial=0, max_concurrent_trials=1, max_duration_seconds=7200, max_retries=0)
    config = {"fixture_version": "dev-smoke-r1", "fixture_sha256": ASSET["sha256"],
        "grader_version": "not-run", "judge_version": "disabled", "retriever_config": {},
        "knowledge_index_version": "dev-index-not-opened", "model_versions": {"provider": "not-invoked-in-mock"},
        "prompt_versions": {"executor": "locked-in-separate-approval"}, "tool_permissions": ["read_only"],
        "retry_policy": {"max_retries": 0, "automatic_replay": False}, "random_seed": 1,
        "environment": {"mode": "DEV_SMOKE", "kind": "isolated_dev", "database": "shoprelay_m11_test"},
        "budget_limits": limits.to_dict(), "data_version": "dev-r1"}
    preregister_experiment(store=store, experiment_id="exp.dev.mock", config=config,
        git_commit="synthetic-test-only", strategy="DEV_SMOKE", scoring_version="not-authorized")
    return store, BudgetLedger(store, "exp.dev.mock", limits)


def test_dev_adapter_runs_only_against_mock_and_records_conservative_budget(mock_server):
    # Use a unique disposable ledger file under the writable evidence directory; no ShopRelay DB is touched.
    state, base = mock_server
    authority = authority_for(base)
    grant = authority.issue(asset_id="dev-r1", asset_version="dev-r1", asset_sha256=ASSET["sha256"],
        subject="dev-owner", base_url=base)
    marker = "b" * 32
    path = Path("docs/probes") / f".m61cq1-ledger-{uuid.uuid4().hex}.sqlite3"
    store, ledger = _dev_ledger(path)
    transport = DevHttpTransport(base_url=base, grant=grant, authority=authority,
        subject="dev-owner", username="synthetic-user", password="mock-password-never-real")
    case = EvalCase(case_id="dev-case-01", group_id="dev-group-01", split="dev",
        input={"message": "Synthetic, non-business integration prompt"}, expected_outcome={"status": "PENDING_HUMAN_REVIEW"},
        rubric_refs=(), fixture_ref=None, risk_level="LOW", data_version="dev-r1")
    try:
        adapter = DevSmokeShopRelayAdapter(transport=transport, grant_authority=authority,
            subject="dev-owner", budget_ledger=ledger)
        observation = adapter.execute_case(mode=ExecutionMode.DEV_SMOKE, grant=grant, case=case,
            experiment_id="exp.dev.mock", client_request_id=f"m61c:{marker}:case01",
            evaluation_marker=marker)
        assert observation.run_ids == (state.run_id,)
        assert observation.lifecycle_status == "SUCCEEDED"
        assert observation.runs[0].provider_usage_status == "STUB"
        assert state.posts == 1
        assert state.requests == transport._requests == 11
        snapshot = ledger.snapshot()
        assert snapshot["calls_by_category"] == {"provider": 5, "embedding": 2}
        assert snapshot["calls_status"] == "CONSERVATIVE_UPPER_BOUND"
        assert snapshot["tokens"] is None and snapshot["estimated_usd"] is None
        replay = adapter.execute_case(mode=ExecutionMode.DEV_SMOKE, grant=grant, case=case,
            experiment_id="exp.dev.mock", client_request_id=f"m61c:{marker}:case01",
            evaluation_marker=marker)
        assert replay.run_ids == observation.run_ids
        assert state.posts == 1
        assert state.requests == transport._requests == 16
        assert ledger.snapshot()["calls_by_category"] == {"provider": 5, "embedding": 2}
    finally:
        # Remove only the uniquely named SQLite ledger created by this test.
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)


def test_dev_adapter_recovers_ambiguous_server_error_without_reposting(mock_server):
    state, base = mock_server
    state.post_missing_response = False
    state.post_error_status = 503
    authority = authority_for(base)
    grant = authority.issue(asset_id="dev-r1", asset_version="dev-r1", asset_sha256=ASSET["sha256"],
        subject="dev-owner", base_url=base)
    marker = "d" * 32
    path = Path("docs/probes") / f".m61cq1-ledger-{uuid.uuid4().hex}.sqlite3"
    store, ledger = _dev_ledger(path)
    transport = DevHttpTransport(base_url=base, grant=grant, authority=authority,
        subject="dev-owner", username="synthetic-user", password="mock-password-never-real")
    case = EvalCase(case_id="dev-case-503", group_id="dev-group-01", split="dev",
        input={"message": "Synthetic only"}, expected_outcome={"status": "PENDING_HUMAN_REVIEW"},
        rubric_refs=(), fixture_ref=None, risk_level="LOW", data_version="dev-r1")
    try:
        adapter = DevSmokeShopRelayAdapter(transport=transport, grant_authority=authority,
            subject="dev-owner", budget_ledger=ledger)
        observation = adapter.execute_case(mode=ExecutionMode.DEV_SMOKE, grant=grant, case=case,
            experiment_id="exp.dev.mock", client_request_id=f"m61c:{marker}:case01",
            evaluation_marker=marker)
        assert observation.run_ids == (state.run_id,)
        assert state.posts == 1
        assert ledger.snapshot()["calls_by_category"] == {"provider": 5, "embedding": 2}
        assert ledger.snapshot()["calls_status"] == "CONSERVATIVE_UPPER_BOUND"
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)


def test_dev_budget_exhaustion_stops_before_session_or_chat_post(mock_server):
    state, base = mock_server
    authority = authority_for(base)
    grant = authority.issue(asset_id="dev-r1", asset_version="dev-r1", asset_sha256=ASSET["sha256"],
        subject="dev-owner", base_url=base)
    path = Path("docs/probes") / f".m61cq1-ledger-{uuid.uuid4().hex}.sqlite3"
    store, ledger = _dev_ledger(path, provider_calls=4)
    transport = DevHttpTransport(base_url=base, grant=grant, authority=authority,
        subject="dev-owner", username="synthetic-user", password="mock-password-never-real")
    case = EvalCase(case_id="dev-case-budget", group_id="dev-group-01", split="dev",
        input={"message": "Synthetic only"}, expected_outcome={"status": "PENDING_HUMAN_REVIEW"},
        rubric_refs=(), fixture_ref=None, risk_level="LOW", data_version="dev-r1")
    try:
        adapter = DevSmokeShopRelayAdapter(transport=transport, grant_authority=authority,
            subject="dev-owner", budget_ledger=ledger)
        with pytest.raises(BudgetStop):
            adapter.execute_case(mode=ExecutionMode.DEV_SMOKE, grant=grant, case=case,
                experiment_id="exp.dev.mock", client_request_id="m61c:" + "c" * 32 + ":case01",
                evaluation_marker="c" * 32)
        assert state.posts == 0
    finally:
        for suffix in ("", "-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
