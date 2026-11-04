from __future__ import annotations

import json
import uuid
from datetime import timedelta

import pytest

from app.m0.data import seed
from app.m1.db import LATEST_SCHEMA_REVISION, current_revision, make_engine, make_session_factory, upgrade_database
from app.m1.models import ConversationSession, Message, PendingAction, SessionMemorySummary, utc_now
from app.m1.services import create_run
from app.m5.memory import (MemoryBudget, clear_session_memory, estimate_tokens,
                           persist_auto_summary, prepare_memory_context, read_session_summary)


@pytest.fixture
def memory_db(tmp_path):
    path = tmp_path / f"memory-{uuid.uuid4().hex}.sqlite3"
    seed(path)
    url = f"sqlite:///{path.as_posix()}"
    upgrade_database(url)
    engine = make_engine(url)
    factory = make_session_factory(engine)
    now = utc_now()
    with factory() as db:
        for owner in ("u_demo", "u_other"):
            db.add(ConversationSession(session_id=f"session-{owner}", user_id=owner,
                created_at=now, updated_at=now, title="memory test"))
        db.commit()
    try:
        yield factory
    finally:
        engine.dispose()


def add_messages(db, session_id: str, count: int, content: str) -> None:
    now = utc_now()
    db.add_all([Message(message_id=f"{session_id}-m{i:03d}", session_id=session_id,
        role="user" if i % 2 else "assistant", content=f"{content} {i}", created_at=now,
        sequence=i, status="RECORDED") for i in range(1, count + 1)])
    db.commit()


def budget_for(effective: int, *, recent: int = 6) -> MemoryBudget:
    return MemoryBudget(model_window_tokens=effective + 1024, output_reserve_tokens=512,
        safety_margin_tokens=512, base_prompt_reserve_tokens=0, recent_message_count=recent)


def test_soft_threshold_compacts_verbose_history_and_keeps_latest_raw_turn(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 30, "x" * 2000)
        plan = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="本轮问题", run_id="run-soft", budget=budget_for(30000))
        assert plan["level"] == "SOFT"
        assert plan["thresholds"] == {"soft": 0.5, "hard": 0.7, "auto_compact": 0.85}
        assert plan["estimated_tokens_before"] >= 0.5 * plan["effective_context_tokens"]
        assert "session-u_demo-m030" in plan["memory_context"]
        assert plan["summary_payload"] is None
        assert plan["summary_model_calls"] == 0
        assert "session-u_demo-m024" in plan["source_message_ids"]


def test_hard_threshold_creates_transient_summary_without_persisting(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 30, "short-history")
        # The six most recent raw turns dominate the remaining context after Soft.
        for i in range(31, 37):
            db.add(Message(message_id=f"session-u_demo-m{i:03d}", session_id="session-u_demo",
                role="user" if i % 2 else "assistant", content="z" * 14000,
                created_at=utc_now(), sequence=i, status="RECORDED"))
        db.commit()
        plan = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="current", run_id="run-hard", budget=budget_for(3400))
        assert plan["level"] == "HARD"
        assert plan["estimated_tokens_after"] >= 0.7 * plan["effective_context_tokens"]
        assert plan["summary_payload"] is None
        assert db.query(SessionMemorySummary).count() == 0
        assert "session-u_demo-m030" in plan["source_message_ids"]


def test_auto_compaction_persists_version_and_reloads_after_restart(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 74, "历史任务背景及一般讨论 " + "x" * 1000)
        for i in range(75, 81):
            message_content = "z" * 14000
            if i == 79:
                message_content += " 更正：之前说的状态不正确，真实状态应重新核验。"
            db.add(Message(message_id=f"session-u_demo-m{i:03d}", session_id="session-u_demo",
                role="user" if i % 2 else "assistant", content=message_content,
                created_at=utc_now(), sequence=i, status="RECORDED"))
        db.commit()
        run, _ = create_run(db, "u_demo", "session-u_demo", "memory-run-1", {"message": "current"})
        plan = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="current", run_id=run.run_id, budget=budget_for(2500))
        assert plan["level"] == "AUTO_COMPACT"
        assert plan["summary_payload"]
        assert plan["summary_model_calls"] == 0
        assert "更正：之前说的状态不正确" in plan["memory_context"]
        persisted, version = persist_auto_summary(db, owner_id="u_demo", session_id="session-u_demo",
            run_id=run.run_id, plan=plan)
        assert persisted and version == 1
        replayed, replay_version = persist_auto_summary(db, owner_id="u_demo", session_id="session-u_demo",
            run_id=run.run_id, plan=plan)
        assert replayed and replay_version == version
        assert db.query(SessionMemorySummary).filter_by(session_id="session-u_demo").count() == 1
        # A new DB session simulates a process restart and reads only this owner's versioned summary.
        db.close()
    with memory_db() as db:
        restored = read_session_summary(db, owner_id="u_demo", session_id="session-u_demo")
        assert restored is not None
        next_plan = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="follow-up", run_id="memory-run-2", budget=budget_for(2500))
        assert next_plan["summary_version"] == 1
        assert "历史压缩摘要" in next_plan["memory_context"]


def test_auto_compaction_skips_empty_source_delta_without_advancing_version(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 80, "background " + "q" * 1000)
        run, _ = create_run(db, "u_demo", "session-u_demo", "memory-empty-delta-first", {"message": "current"})
        first = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="current", run_id=run.run_id, budget=budget_for(2500))
        assert first["summary_payload"]
        ok, version = persist_auto_summary(db, owner_id="u_demo", session_id="session-u_demo",
            run_id=run.run_id, plan=first)
        assert ok and version == 1
        persisted = db.query(SessionMemorySummary).filter_by(session_id="session-u_demo").one()
        payload = json.loads(persisted.summary_json)
        covered_ids = [row.message_id for row in db.query(Message).filter_by(session_id="session-u_demo").all()]
        payload["source_message_ids"] = covered_ids
        payload["source_through_sequence"] = 80
        persisted.source_message_ids_json = json.dumps(covered_ids)
        persisted.source_through_sequence = 80
        persisted.summary_json = json.dumps(payload, ensure_ascii=False)
        db.commit()

        # A new Run adds only oversized recent messages beyond the durable
        # watermark. They can be compacted transiently, but are not an empty
        # durable delta and must not create another empty summary version.
        now = utc_now()
        db.add_all([
            Message(message_id="session-u_demo-large-081", session_id="session-u_demo", role="user",
                content="z" * 14000, created_at=now, sequence=81, status="RECORDED"),
            Message(message_id="session-u_demo-large-082", session_id="session-u_demo", role="assistant",
                content="z" * 14000, created_at=now, sequence=82, status="RECORDED"),
        ])
        db.commit()
        run2, _ = create_run(db, "u_demo", "session-u_demo", "memory-empty-delta-second", {"message": "current"})
        second = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="current", run_id=run2.run_id, budget=budget_for(512))
        assert second["level"] == "AUTO_COMPACT"
        assert second["summary_payload"] is None
        ok2, version2 = persist_auto_summary(db, owner_id="u_demo", session_id="session-u_demo",
            run_id=run2.run_id, plan=second)
        assert ok2 and version2 == 1
        summaries = db.query(SessionMemorySummary).filter_by(session_id="session-u_demo").all()
        assert len(summaries) == 1
        assert json.loads(summaries[0].source_message_ids_json)


def test_auto_compaction_trims_untrusted_context_and_flags_unfit_required_context(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 80, "long but compressible context " + "x" * 1200)
        run, _ = create_run(db, "u_demo", "session-u_demo", "memory-trim-pending-action", {"message": "q"})
        action = PendingAction(action_id="memory-trim-action", owner_id="u_demo", session_id="session-u_demo",
            run_id=run.run_id, trace_id="trace", tool_id="create_return_request", target_resource_id="ORD-123",
            sanitized_arguments_json='{"order_id":"ORD-123"}', arguments_hash="b" * 64,
            action_version=1, risk_level="WRITE_HIGH_RISK", status="PENDING_CONFIRMATION",
            idempotency_key="memory-trim-action", expires_at=utc_now() + timedelta(hours=1), audit_reference="audit")
        db.add(action)
        db.commit()
        plan = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="ordinary policy question", run_id=run.run_id, budget=budget_for(2500))
        assert plan["level"] == "AUTO_COMPACT"
        assert plan["context_budget_exceeded"] is False
        assert plan["estimated_tokens_after"] <= plan["effective_context_tokens"]
        assert plan["context_budget_handling"] == "HISTORICAL_CONTEXT_TRIMMED"
        assert "memory-trim-action" in plan["memory_context"]
        assert '"arguments_hash"' not in plan["memory_context"]

        oversized_query = "退款政策问题 " + "x" * 20000
        over = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query=oversized_query, run_id=run.run_id, budget=budget_for(2500))
        assert over["context_budget_exceeded"] is True
        assert over["context_budget_overage_tokens"] > 0
        assert over["context_budget_handling"] == "BUDGET_EXCEEDED_PRECALL"
        assert "memory-trim-action" in over["memory_context"]


def test_policy_model_is_not_called_when_required_context_remains_over_budget(memory_db, monkeypatch):
    from pathlib import Path
    from app.m1.chat_execution import execute_claimed_chat
    from app.m1.services import claim_run
    from app.m0.config import load_settings

    monkeypatch.setenv("SHOPRELAY_CONTEXT_WINDOW_TOKENS", "3524")
    with memory_db() as db:
        run, _ = create_run(db, "u_demo", "session-u_demo", "memory-precall-budget", {"message": "x"})
        assert claim_run(db, run, execution_owner_id="memory-test")

        class ProviderSpy:
            settings = load_settings(require_api_key=False)
            calls = 0
            def answer(self, *_args, **_kwargs):
                self.calls += 1
                raise AssertionError("model provider must not be called over budget")
        provider = ProviderSpy()
        query = "退款政策允许什么？" + "x" * 9990
        result = execute_claimed_chat(db, run=run, user_id="u_demo", message=query,
            trace_id="memory-budget-trace", provider=provider, index_path=Path("data/index.json"))
        assert result["status"] == "FAILED"
        assert result["failure"].code == "CONTEXT_BUDGET_EXCEEDED"
        assert provider.calls == 0


def test_summary_clear_is_owner_scoped_and_does_not_resurrect_old_history(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 12, "old conversation")
        run, _ = create_run(db, "u_demo", "session-u_demo", "memory-clear-run", {"message": "q"})
        payload = {"schema_version": 1, "items": [], "source_message_ids": [],
                   "source_through_sequence": 12, "fact_authority": "NONE"}
        record = SessionMemorySummary(summary_id=str(uuid.uuid4()), owner_id="u_demo",
            session_id="session-u_demo", version=1, source_run_id=run.run_id,
            source_through_sequence=12, source_message_ids_json="[]",
            summary_json=json.dumps(payload), estimated_tokens_before=100,
            estimated_tokens_after=10, is_clear_marker=False, created_at=utc_now())
        db.add(record)
        db.commit()
        assert clear_session_memory(db, owner_id="u_other", session_id="session-u_demo") is False
        assert clear_session_memory(db, owner_id="u_demo", session_id="session-u_demo") is True
        assert read_session_summary(db, owner_id="u_demo", session_id="session-u_demo") is None
        plan = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="new question", run_id="new-run", budget=budget_for(8192))
        assert "old conversation" not in plan["memory_context"]


def test_memory_is_owner_scoped_redacted_and_preserves_pending_action_state(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 2,
            '我的邮箱 alice@example.com，token=abc123，Authorization: Bearer secret-value，{"api_key":"json-secret"}')
        add_messages(db, "session-u_other", 2, "other-owner-private-marker")
        run, _ = create_run(db, "u_demo", "session-u_demo", "memory-action-run", {"message": "q"})
        action = PendingAction(action_id="action-memory", owner_id="u_demo", session_id="session-u_demo",
            run_id=run.run_id, trace_id="trace", tool_id="create_return_request", target_resource_id="ORD-123",
            sanitized_arguments_json='{"order_id":"ORD-123"}', arguments_hash="a" * 64,
            action_version=1, risk_level="WRITE_HIGH_RISK", status="PENDING_CONFIRMATION",
            idempotency_key="memory-action", expires_at=utc_now() + timedelta(hours=1), audit_reference="audit")
        db.add(action)
        db.commit()
        plan = prepare_memory_context(db, owner_id="u_demo", session_id="session-u_demo",
            current_query="test", run_id="memory-action-run", budget=budget_for(8192))
        assert "other-owner-private-marker" not in plan["memory_context"]
        assert "alice@example.com" not in plan["memory_context"]
        assert "abc123" not in plan["memory_context"]
        assert "secret-value" not in plan["memory_context"]
        assert "json-secret" not in plan["memory_context"]
        assert any(item["action_id"] == "action-memory" and item["status"] == "PENDING_CONFIRMATION"
                   for item in plan["protected_state"]["pending_actions"])
        db.refresh(action)
        assert action.status == "PENDING_CONFIRMATION"
        assert action.arguments_hash == "a" * 64


def test_summary_is_loaded_into_agent_state_projection(memory_db):
    from app.m1.state import restore_agent_state

    with memory_db() as db:
        add_messages(db, "session-u_demo", 2, "persisted-state-context")
        run, _ = create_run(db, "u_demo", "session-u_demo", "memory-state-run", {"message": "q"})
        payload = {"schema_version": 1, "items": [{"message_id": "source", "excerpt": "state excerpt"}],
                   "source_message_ids": ["source"], "source_through_sequence": 1, "fact_authority": "NONE"}
        db.add(SessionMemorySummary(summary_id="state-summary", owner_id="u_demo",
            session_id="session-u_demo", version=1, source_run_id=run.run_id,
            source_through_sequence=1, source_message_ids_json='["source"]', summary_json=json.dumps(payload),
            estimated_tokens_before=100, estimated_tokens_after=10, is_clear_marker=False, created_at=utc_now()))
        db.commit()
        state = restore_agent_state(db, "u_demo", run.run_id)
        assert state is not None and state.summary
        assert json.loads(state.summary)["source_message_ids"] == ["source"]


def test_migration_is_repeatable_and_matches_latest_revision(memory_db):
    engine = memory_db.kw["bind"]
    url = engine.url.render_as_string(hide_password=False)
    upgrade_database(url)
    upgrade_database(url)
    assert current_revision(url) == LATEST_SCHEMA_REVISION


def test_estimator_and_provider_usage_are_recorded_as_calibration_not_billable_truth():
    from app.m5.memory import usage_calibration

    plan = {"estimated_tokens_after": estimate_tokens("hello")}
    result = usage_calibration(plan, {"prompt_tokens": 10, "completion_tokens": 4})
    assert result["provider_prompt_tokens"] == 10
    assert result["estimate_to_provider_ratio"] == round(estimate_tokens("hello") / 10, 4)
    assert usage_calibration(plan, None)["provider_prompt_tokens"] is None


def test_failed_auto_summary_write_leaves_previous_version_intact(memory_db):
    with memory_db() as db:
        add_messages(db, "session-u_demo", 80, "background " + "q" * 1000)
        # A valid prior summary is retained if a later Run cannot be associated with this owner/session.
        payload = {"schema_version": 1, "items": [{"excerpt": "prior stable summary"}],
                   "source_message_ids": [], "source_through_sequence": 2, "fact_authority": "NONE"}
        db.add(SessionMemorySummary(summary_id="prior-summary", owner_id="u_demo",
            session_id="session-u_demo", version=1, source_run_id=None,
            source_through_sequence=2, source_message_ids_json="[]", summary_json=json.dumps(payload),
            estimated_tokens_before=100, estimated_tokens_after=20, is_clear_marker=False, created_at=utc_now()))
        db.commit()
        plan = {"level": "AUTO_COMPACT", "summary_payload": {**payload, "source_through_sequence": 80},
                "estimated_tokens_before": 1000, "estimated_tokens_after": 100}
        ok, version = persist_auto_summary(db, owner_id="u_demo", session_id="session-u_demo",
                                           run_id="missing-run", plan=plan)
        assert not ok and version is None
        assert read_session_summary(db, owner_id="u_demo", session_id="session-u_demo")["items"][0]["excerpt"] == "prior stable summary"
