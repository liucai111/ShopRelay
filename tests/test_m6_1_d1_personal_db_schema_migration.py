import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from eval.m6_1.personal_dev_database import (
    PROFILE,
    PersonalDevDatabase,
    PersonalDatabaseError,
    migrate_personal_policy_event_schema,
)


OLD_SCHEMA = """
CREATE TABLE d1_personal_target (profile TEXT PRIMARY KEY, identity TEXT NOT NULL,
    path TEXT NOT NULL, lease_id TEXT, receipt TEXT);
CREATE TABLE users (user_id TEXT PRIMARY KEY, display_name TEXT NOT NULL);
CREATE TABLE sessions (session_id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
    created_at TEXT NOT NULL, title TEXT, updated_at TEXT,
    FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE);
CREATE TABLE runs (run_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
    status TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE);
CREATE TABLE controlled_pending_actions (action_id TEXT PRIMARY KEY);
CREATE TABLE run_events (
    event_id VARCHAR(64) NOT NULL,
    run_id VARCHAR(64) NOT NULL,
    session_id VARCHAR(64) NOT NULL,
    trace_id VARCHAR(64) NOT NULL,
    seq INTEGER NOT NULL,
    timestamp DATETIME NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    payload_json TEXT NOT NULL,
    event_key VARCHAR(160),
    PRIMARY KEY (event_id),
    CONSTRAINT uq_run_events_run_seq UNIQUE (run_id, seq),
    CONSTRAINT ck_run_events_type CHECK (event_type IN ('started','route_decided','agent_started',
        'retrieval_hop','tool_started','tool_finished','action_pending','answer','done','error')),
    FOREIGN KEY(run_id) REFERENCES runs (run_id) ON DELETE CASCADE,
    FOREIGN KEY(session_id) REFERENCES sessions (session_id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX uq_run_events_idempotent_key ON run_events (run_id, event_key);
"""


def make_legacy_database(path: Path, *, status="SUCCEEDED"):
    identity = "test-personal-target-identity"
    connection = sqlite3.connect(path)
    try:
        connection.executescript(OLD_SCHEMA)
        connection.execute("INSERT INTO d1_personal_target VALUES(?,?,?,?,?)",
            (PROFILE, identity, str(path.absolute()), None, None))
        connection.execute("INSERT INTO users VALUES(?,?)", ("u1", "Synthetic"))
        connection.execute("INSERT INTO sessions VALUES(?,?,?,?,?)",
            ("s1", "u1", "2026-10-27T00:00:00Z", None, None))
        connection.execute("INSERT INTO runs VALUES(?,?,?,?)", ("r1", "u1", "s1", status))
        connection.execute("INSERT INTO run_events VALUES(?,?,?,?,?,?,?,?,?)",
            ("e1", "r1", "s1", "t1", 1, "2026-10-27T00:00:00Z", "started", "{}", None))
        connection.commit()
    finally:
        connection.close()
    return identity


def sqlite_backup(source: Path, destination: Path):
    src = sqlite3.connect(f"file:{source.resolve().as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()


def test_legacy_schema_migration_preserves_rows_indexes_and_accepts_policy_events(tmp_path):
    target = tmp_path / "personal.sqlite3"
    backup = tmp_path / "personal-before.sqlite3"
    identity = make_legacy_database(target)
    sqlite_backup(target, backup)
    report = migrate_personal_policy_event_schema(target, expected_identity=identity,
        expected_marker_path=target.absolute(), backup_path=backup)
    assert report["status"] == "UPGRADED"
    assert report["run_events_rows_preserved"] == 1
    assert report["foreign_key_check_after"] == "PASS"
    engine = create_engine(f"sqlite+pysqlite:///{target.as_posix()}")
    try:
        schema = PersonalDevDatabase.check_policy_event_schema(engine)
        assert "policy_retrieval_started" in schema
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO run_events VALUES(?,?,?,?,?,?,?,?,?)",
                ("e2", "r1", "s1", "t1", 2, "2026-10-27T00:01:00Z",
                 "policy_retrieval_started", "{}", None))
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT count(*) FROM run_events").scalar_one() == 2
            assert connection.exec_driver_sql(
                "SELECT count(*) FROM sqlite_master WHERE type='index' AND name='uq_run_events_idempotent_key'").scalar_one() == 1
    finally:
        engine.dispose()


def test_active_run_prevents_upgrade_without_changing_schema_or_backup(tmp_path):
    target = tmp_path / "personal.sqlite3"
    backup = tmp_path / "personal-before.sqlite3"
    identity = make_legacy_database(target, status="RUNNING")
    sqlite_backup(target, backup)
    before = sqlite3.connect(target)
    try:
        old_sql = before.execute("SELECT sql FROM sqlite_master WHERE name='run_events'").fetchone()[0]
    finally:
        before.close()
    with pytest.raises(PersonalDatabaseError, match="ACTIVE_RUN_OR_PENDING_ACTION"):
        migrate_personal_policy_event_schema(target, expected_identity=identity,
            expected_marker_path=target.absolute(), backup_path=backup)
    after = sqlite3.connect(target)
    try:
        new_sql = after.execute("SELECT sql FROM sqlite_master WHERE name='run_events'").fetchone()[0]
        assert new_sql == old_sql
        assert after.execute("SELECT status FROM runs WHERE run_id='r1'").fetchone()[0] == "RUNNING"
    finally:
        after.close()


def test_old_schema_is_rejected_by_database_readiness_check(tmp_path):
    target = tmp_path / "old.sqlite3"
    make_legacy_database(target)
    engine = create_engine(f"sqlite+pysqlite:///{target.as_posix()}")
    try:
        with pytest.raises(PersonalDatabaseError, match="POLICY_EVENT_SCHEMA_NOT_UPGRADED"):
            PersonalDevDatabase.check_policy_event_schema(engine)
    finally:
        engine.dispose()
