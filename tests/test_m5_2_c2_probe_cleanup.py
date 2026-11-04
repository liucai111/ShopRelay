from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.m1.models import Base, ConversationSession, DemoCredential, Message, User
from scripts import probe_m5_2_c2_browser as probe


def _credential_db(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'c2-credentials.sqlite3').as_posix()}")
    Base.metadata.create_all(engine, tables=[User.__table__, DemoCredential.__table__])
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        for user_id in ("u_demo", "u_other", "isolated_unrelated"):
            db.add(User(user_id=user_id, display_name=user_id))
        db.flush()
        db.add_all([
            DemoCredential(user_id="u_demo", password_hash="old-demo-hash", active=True,
                           updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc)),
            DemoCredential(user_id="u_other", password_hash="old-other-hash", active=False,
                           updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc)),
            DemoCredential(user_id="isolated_unrelated", password_hash="unrelated-hash", active=True,
                           updated_at=datetime(2026, 1, 3, tzinfo=timezone.utc)),
        ])
        db.commit()
    return engine, factory


def test_credential_restore_is_exactly_scoped_and_preserves_unrelated_account(tmp_path):
    engine, factory = _credential_db(tmp_path)
    saved = {
        "u_demo": ("old-demo-hash", True, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        "u_other": ("old-other-hash", False, datetime(2026, 1, 2, tzinfo=timezone.utc)),
    }
    with factory() as db:
        for user_id in saved:
            row = db.get(DemoCredential, user_id)
            row.password_hash, row.active = "temporary-probe-hash", True
        db.commit()

    assert probe._restore_demo_credentials(factory, saved) == "PASS"
    with factory() as db:
        values = {row.user_id: (row.password_hash, row.active, row.updated_at)
                  for row in db.execute(select(DemoCredential)).scalars()}
    assert values["u_demo"] == saved["u_demo"]
    assert values["u_other"] == saved["u_other"]
    assert values["isolated_unrelated"] == (
        "unrelated-hash", True, datetime(2026, 1, 3, tzinfo=timezone.utc))
    engine.dispose()


def test_credential_restore_failure_disables_only_captured_demo_accounts(tmp_path):
    engine, factory = _credential_db(tmp_path)
    with factory() as db:
        for user_id in ("u_demo", "u_other"):
            row = db.get(DemoCredential, user_id)
            row.password_hash, row.active = "temporary-probe-hash", True
        db.commit()

    calls = 0

    def fail_first_factory_call():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected restore transaction failure")
        return factory()

    saved = {
        "u_demo": ("old-demo-hash", True, datetime(2026, 1, 1, tzinfo=timezone.utc)),
        "u_other": ("old-other-hash", True, datetime(2026, 1, 2, tzinfo=timezone.utc)),
    }
    assert probe._restore_demo_credentials(fail_first_factory_call, saved) == "FAILED_DISABLED"
    with factory() as db:
        active = {row.user_id: row.active for row in db.execute(select(DemoCredential)).scalars()}
    assert active == {"u_demo": False, "u_other": False, "isolated_unrelated": True}
    engine.dispose()


def test_message_cleanup_uses_session_fk_and_does_not_touch_other_session(tmp_path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'c2-messages.sqlite3').as_posix()}")
    Base.metadata.create_all(engine, tables=[User.__table__, ConversationSession.__table__, Message.__table__])
    factory = sessionmaker(engine, expire_on_commit=False)
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    with factory() as db:
        db.add_all([User(user_id="u_demo", display_name="demo"),
                    User(user_id="u_other", display_name="other")])
        db.flush()
        db.add_all([ConversationSession(session_id="probe-session", user_id="u_demo", created_at=now),
                    ConversationSession(session_id="other-session", user_id="u_other", created_at=now)])
        db.flush()
        db.add_all([Message(message_id="probe-message", session_id="probe-session", role="user",
                            content="probe", created_at=now),
                    Message(message_id="other-message", session_id="other-session", role="user",
                            content="keep", created_at=now)])
        db.commit()
        probe._delete_messages_for_sessions(db, ["probe-session"])
        db.commit()
        remaining = list(db.execute(select(Message.message_id)).scalars())
    assert remaining == ["other-message"]
    engine.dispose()


def test_probe_json_writer_never_persists_credentials_or_session_hashes(tmp_path, monkeypatch):
    path = tmp_path / "probe.json"
    monkeypatch.setattr(probe, "OUT", path)
    probe._save({"status": "TEST", "auth_session_hashes": ["do-not-write"],
                 "temporary_password_suffix": "do-not-write", "result": "safe"})
    body = path.read_text(encoding="utf-8")
    assert "do-not-write" not in body
    assert "auth_session_hashes" not in body
    assert "temporary_password_suffix" not in body
    assert '"result": "safe"' in body
