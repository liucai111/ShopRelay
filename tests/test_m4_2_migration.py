import importlib.util

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text


PATH = "migrations/alembic/versions/20261022_09_controlled_actions.py"
SPEC = importlib.util.spec_from_file_location("m42_migration", PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)
SPEC10 = importlib.util.spec_from_file_location(
    "m42_audit_constraints", "migrations/alembic/versions/20261022_10_action_audit_constraints.py")
MIGRATION10 = importlib.util.module_from_spec(SPEC10)
SPEC10.loader.exec_module(MIGRATION10)


def _run(engine, fn):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            fn()


def _base():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users(user_id VARCHAR(64) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE sessions(session_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL)"))
        connection.execute(text("CREATE TABLE runs(run_id VARCHAR(64) PRIMARY KEY, user_id VARCHAR(64) NOT NULL, session_id VARCHAR(64) NOT NULL)"))
    return engine


def test_controlled_action_migration_empty_upgrade_and_downgrade():
    engine = _base()
    _run(engine, MIGRATION.upgrade)
    _run(engine, MIGRATION10.upgrade)
    assert {"controlled_pending_actions", "action_audit", "simulated_test_actions"} <= set(inspect(engine).get_table_names())
    _run(engine, MIGRATION10.downgrade)
    _run(engine, MIGRATION.downgrade)
    assert not {"controlled_pending_actions", "action_audit", "simulated_test_actions"} & set(inspect(engine).get_table_names())
    assert {"users", "sessions", "runs"} <= set(inspect(engine).get_table_names())
    engine.dispose()


def test_controlled_action_migration_refuses_to_drop_historical_actions():
    engine = _base()
    _run(engine, MIGRATION.upgrade)
    _run(engine, MIGRATION10.upgrade)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users VALUES ('u')"))
        connection.execute(text("INSERT INTO sessions VALUES ('s','u')"))
        connection.execute(text("INSERT INTO runs VALUES ('r','u','s')"))
        connection.execute(text(
            "INSERT INTO controlled_pending_actions(action_id,owner_id,session_id,run_id,trace_id,tool_id,"
            "target_resource_id,sanitized_arguments_json,arguments_hash,action_version,risk_level,status,"
            "idempotency_key,created_at,expires_at,audit_reference) VALUES "
            "('a','u','s','r','t','simulated_test_action','M42-SIM-1','{}','h',1,'WRITE_LOW_RISK',"
            "'PENDING_CONFIRMATION','k','2026-10-22T00:00:00Z','2026-10-22T01:00:00Z','a')"
        ))
    with pytest.raises(RuntimeError, match="refusing M4.2 downgrade"):
        _run(engine, MIGRATION.downgrade)
    assert "controlled_pending_actions" in inspect(engine).get_table_names()
    engine.dispose()
