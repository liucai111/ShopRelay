from __future__ import annotations

import json
import tempfile
import uuid
from pathlib import Path

from app.m0.data import seed
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import ConversationSession, Message, utc_now
from app.m1.services import create_run
from app.m5.memory import MemoryBudget, persist_auto_summary, prepare_memory_context


def _budget(effective: int, *, recent: int = 6) -> MemoryBudget:
    return MemoryBudget(model_window_tokens=effective + 1024, output_reserve_tokens=512,
        safety_margin_tokens=512, base_prompt_reserve_tokens=0, recent_message_count=recent)


def _session(db, owner: str, label: str, count: int, *, long_recent: bool = False) -> str:
    session_id = f"m51-{owner}-{label}"
    now = utc_now()
    db.add(ConversationSession(session_id=session_id, user_id=owner, created_at=now,
        updated_at=now, title="synthetic memory probe"))
    for sequence in range(1, count + 1):
        if long_recent and sequence > count - 6:
            content = "合成的历史咨询背景 " + "x" * 14000
        elif long_recent:
            content = "短历史"
        else:
            content = "合成的历史咨询背景 " + "x" * 2000
        if long_recent and sequence == count - 2:
            content += " 更正：之前说的状态不正确，真实状态应重新核验。"
        db.add(Message(message_id=f"{session_id}-{sequence:03d}", session_id=session_id,
            role="user" if sequence % 2 else "assistant", content=content,
            created_at=now, sequence=sequence, status="RECORDED"))
    db.commit()
    return session_id


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="shoprelay-m51-") as temporary:
        database = Path(temporary) / "memory.sqlite3"
        seed(database)
        url = f"sqlite:///{database.as_posix()}"
        upgrade_database(url)
        engine = make_engine(url)
        factory = make_session_factory(engine)
        results = []
        with factory() as db:
            for label, owner, count, effective, long_recent in (
                ("soft", "u_demo", 30, 30000, False),
                ("hard", "u_demo", 36, 3400, True),
                ("auto", "u_demo", 80, 2500, True),
            ):
                session_id = _session(db, owner, label, count, long_recent=long_recent)
                run_id = None
                if label == "auto":
                    run, _ = create_run(db, owner, session_id, f"m51-run-{label}",
                                        {"message": "synthetic follow-up"})
                    run_id = run.run_id
                plan = prepare_memory_context(db, owner_id=owner, session_id=session_id,
                    current_query="synthetic follow-up", run_id=run_id or f"m51-run-{label}",
                    budget=_budget(effective))
                persisted = None
                version = None
                if label == "auto":
                    persisted, version = persist_auto_summary(db, owner_id=owner,
                        session_id=session_id, run_id=run_id, plan=plan)
                results.append({"threshold_case": label, "message_count": count,
                    "effective_context_tokens": plan["effective_context_tokens"],
                    "triggered_level": plan["level"],
                    "estimated_tokens_before": plan["estimated_tokens_before"],
                    "estimated_tokens_after": plan["estimated_tokens_after"],
                    "compression_ratio": plan["compression_ratio"],
                    "thresholds": plan["thresholds"],
                    "recent_messages_in_context": min(count, 6),
                    "summary_model_calls": plan["summary_model_calls"],
                    "summary_cost_estimate_usd": plan["summary_cost_estimate_usd"],
                    "summary_generation_latency_ms": plan["summary_latency_ms"],
                    "persisted": persisted, "summary_version": version,
                    "source_message_count": len(plan["source_message_ids"])})
        engine.dispose()
        print(json.dumps({"schema_version": 1, "probe": "M5.1 deterministic long-session context compression",
            "database": "temporary SQLite; discarded on exit", "provider_calls": 0,
            "provider_usage": None, "provider_cost_usd": 0.0,
            "token_estimator": "ceil(UTF-8 bytes / 3.2); approximate, not tokenizer-exact",
            "cases": results,
            "limits": ["PostgreSQL migration/persistence remains unverified while the configured container is stopped.",
                       "No real model summary was generated; summaries are extractive deterministic records."]},
            ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
