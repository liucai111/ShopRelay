from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy import select

from app.m0.data import ROOT, seed
from app.m1.api import create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import ConversationSession, Order, Run, utc_now
from app.m2.retrieval import RetrievalService, config_from_environment
from app.m4.actions import ActionError, create_pending_action, prepare_return_action
from app.orchestration.graph import _safe_route
from app.skills.registry import SKILL_REGISTRY, bind_skill, validate_skill_execution
from app.tools.catalog import TOOL_CATALOG


@pytest.fixture
def return_db(tmp_path):
    path = tmp_path / "m43b1-return.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    order_id = "M43-B1-SIM-" + uuid.uuid4().hex[:16]
    now = utc_now()
    with factory() as db:
        db.add(Order(order_id=order_id, user_id="u_demo", status="DELIVERED",
            paid_at="2026-09-01T10:00:00Z", delivered_at="2026-10-01T10:00:00Z",
            paid_cents=12000, version=1))
        session_id = "m43b1-session-" + uuid.uuid4().hex[:10]
        run_id = "m43b1-run-" + uuid.uuid4().hex[:10]
        db.add(ConversationSession(session_id=session_id, user_id="u_demo", created_at=now,
                                   title="isolated return test"))
        db.add(Run(run_id=run_id, user_id="u_demo", session_id=session_id,
            client_request_id=uuid.uuid4().hex, idempotency_scope="m43b1-test",
            request_hash="a" * 64, status="SUCCEEDED"))
        db.commit()
    retrieval = RetrievalService(ROOT / "data" / "index.json", config_from_environment())
    yield factory, engine, order_id, session_id, run_id, retrieval
    engine.dispose()


def test_return_policy_prep_uses_owner_order_and_effective_policy_but_does_not_claim_eligibility(return_db):
    factory, _, order_id, _, _, retrieval = return_db
    with factory() as db:
        arguments, proposal = prepare_return_action(db, owner_id="u_demo", order_id=order_id,
            reason_code="QUALITY_ISSUE", reason_summary="商品存在外观瑕疵", retrieval_service=retrieval)
        assert arguments["reason_source"] == "USER_CLAIM_UNVERIFIED"
        assert arguments["application_outcome"] == "PENDING_HUMAN_REVIEW"
        assert proposal["eligibility"] == "NOT_DETERMINED"
        assert proposal["review_required"] is True
        snapshot = arguments["policy_snapshot"]
        assert snapshot["retrieval_mode"] == "BM25"
        assert snapshot["knowledge_version"]
        assert snapshot["index_fingerprint"]
        assert snapshot["evidence"]
        assert all(item["policy_id"] == "return-standard" for item in snapshot["evidence"])
        assert "approval" not in json.dumps(arguments, ensure_ascii=False).lower()


def test_return_preparation_is_owner_scoped_and_rejects_missing_verified_order_facts(return_db):
    factory, _, order_id, _, _, retrieval = return_db
    with factory() as db, pytest.raises(ActionError) as wrong_owner:
        prepare_return_action(db, owner_id="u_other", order_id=order_id,
            reason_code="OTHER", reason_summary="申请退货", retrieval_service=retrieval)
    assert wrong_owner.value.code == "ORDER_NOT_FOUND"
    with factory() as db:
        row = db.execute(select(Order).where(Order.order_id == order_id)).scalar_one()
        row.delivered_at = None
        db.commit()
    with factory() as db, pytest.raises(ActionError) as missing:
        prepare_return_action(db, owner_id="u_demo", order_id=order_id,
            reason_code="OTHER", reason_summary="申请退货", retrieval_service=retrieval)
    assert missing.value.code == "RETURN_ORDER_FACTS_INSUFFICIENT"


def test_return_executor_fails_closed_on_non_postgres_and_catalog_skill_do_not_enable_auto_write(return_db):
    factory, _, order_id, session_id, run_id, _ = return_db
    url = str(factory.kw["bind"].url)
    sqlite_app = create_app(database_url=url, engine=factory.kw["bind"],
        action_writes_enabled=True, return_actions_enabled=True, orchestrator_mode="legacy")
    assert sqlite_app.state.action_writes_enabled is True
    assert sqlite_app.state.return_actions_enabled is False
    with factory() as db, pytest.raises(ActionError) as exc:
        create_pending_action(db, owner_id="u_demo", session_id=session_id, run_id=run_id,
            trace_id="trace-test", tool_id="create_return_request", target_resource_id=order_id,
            arguments={}, idempotency_key=uuid.uuid4().hex, environment_name="shoprelay_m11_test",
            return_actions_enabled=True)
    assert exc.value.code == "TOOL_NOT_AUTHORIZED"
    tool = TOOL_CATALOG["create_return_request"]
    assert tool.risk_level == "WRITE_HIGH_RISK"
    assert tool.confirmation_required is True and tool.mcp_allowed is False
    skill = SKILL_REGISTRY["return_handling"]
    assert "create_return_request" in skill.proposal_tools
    assert "create_return_request" not in skill.allowed_tools
    binding = bind_skill("我要申请退货", {"strategy": "AGENT",
        "required_capabilities": ["get_order", "search_knowledge", "get_refund_status"]})
    assert binding["skill_proposable_tools"] == ["create_return_request", "create_refund_request"]
    assert "create_return_request" not in binding["skill_allowed_tools"]
    safe = _safe_route({"strategy": "AGENT", **binding})
    assert safe["skill_proposable_tools"] == ["create_return_request", "create_refund_request"]
    with pytest.raises(ValueError, match="SKILL_TOOL_SCOPE_VIOLATION"):
        validate_skill_execution({"skill_id": skill.skill_id, "skill_version": skill.version,
            "required_capabilities": ["create_return_request"]})
