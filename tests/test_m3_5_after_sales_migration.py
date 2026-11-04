import importlib.util

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError


MIGRATION_PATH = "migrations/alembic/versions/20261022_08_after_sales_status.py"
SPEC = importlib.util.spec_from_file_location("after_sales_migration", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def _run(engine, function):
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            function()


def _database():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (order_id VARCHAR(64) PRIMARY KEY)"))
    return engine


def test_after_sales_migration_upgrade_and_empty_downgrade_are_reversible():
    engine = _database()
    _run(engine, MIGRATION.upgrade)
    tables = set(inspect(engine).get_table_names())
    assert {"returns", "refunds", "after_sales_events", "after_sales_schema_meta"} <= tables
    with engine.connect() as connection:
        flags = connection.execute(text(
            "SELECT created_returns, created_refunds FROM after_sales_schema_meta"
        )).one()
        assert tuple(flags) == (True, True)
    _run(engine, MIGRATION.downgrade)
    tables = set(inspect(engine).get_table_names())
    assert "orders" in tables
    assert not {"returns", "refunds", "after_sales_events", "after_sales_schema_meta"} & tables
    engine.dispose()


def test_after_sales_migration_adopts_legacy_rows_without_asserting_their_status():
    engine = _database()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE returns (return_id VARCHAR(64) PRIMARY KEY, order_id VARCHAR(64) "
            "NOT NULL REFERENCES orders(order_id), status VARCHAR(32) NOT NULL, reason TEXT NOT NULL, "
            "created_at VARCHAR(40) NOT NULL)"
        ))
        connection.execute(text(
            "CREATE TABLE refunds (refund_id VARCHAR(64) PRIMARY KEY, order_id VARCHAR(64) "
            "NOT NULL REFERENCES orders(order_id), status VARCHAR(32) NOT NULL, amount_cents INTEGER "
            "NOT NULL, reason TEXT NOT NULL, created_at VARCHAR(40) NOT NULL)"
        ))
        connection.execute(text("INSERT INTO orders VALUES ('ORD-LEGACY')"))
        connection.execute(text(
            "INSERT INTO returns VALUES ('RT-LEGACY','ORD-LEGACY','APPROVED','legacy','2026-01-01T00:00:00Z')"
        ))
        connection.execute(text(
            "INSERT INTO returns VALUES ('RT-OLD-STATUS','ORD-LEGACY','PENDING','legacy','2026-01-02T00:00:00Z')"
        ))
    _run(engine, MIGRATION.upgrade)
    with engine.connect() as connection:
        row = connection.execute(text(
            "SELECT status, source_type, submitted_at FROM returns WHERE return_id='RT-LEGACY'"
        )).one()
        assert tuple(row) == ("APPROVED", "LEGACY_UNVERIFIED", None)
        old = connection.execute(text(
            "SELECT status,source_type,submitted_at FROM returns WHERE return_id='RT-OLD-STATUS'"
        )).one()
        assert tuple(old) == ("PENDING", "LEGACY_UNVERIFIED", None)
    # Existing application rows prevent destructive rollback.
    with pytest.raises(RuntimeError, match="refusing M3.5 downgrade"):
        _run(engine, MIGRATION.downgrade)
    with engine.connect() as connection:
        assert connection.execute(text(
            "SELECT source_type FROM returns WHERE return_id='RT-LEGACY'"
        )).scalar_one() == "LEGACY_UNVERIFIED"
    engine.dispose()


def test_after_sales_constraints_reject_unknown_status_and_unconfirmed_refund_completion():
    engine = _database()
    _run(engine, MIGRATION.upgrade)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO orders VALUES ('ORD-CHECK')"))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO returns(return_id,order_id,status,reason,created_at,submitted_at,source_type,source_ref) "
                "VALUES ('RT-BAD','ORD-CHECK','PENDING','x','2026-10-01','2026-10-01','SIMULATED_TEST_DATA','fixture')"
            ))
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.execute(text(
                "INSERT INTO refunds(refund_id,order_id,status,amount_cents,reason,created_at,submitted_at,"
                "processed_at,source_type,source_ref) VALUES "
                "('RF-BAD','ORD-CHECK','COMPLETED',100,'x','2026-10-01','2026-10-01','2026-10-02',"
                "'SIMULATED_TEST_DATA','fixture')"
            ))
    engine.dispose()
