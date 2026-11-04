"""Add isolated return request business idempotency constraints.

Revision ID: 20261022_12
Revises: 20261022_11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261022_12"
down_revision = "20261022_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    # Some migration-contract tests intentionally advance a minimal legacy
    # database containing only the table under test. Such a database has no
    # M3.5/M4.2 domain schema, so there is nothing to constrain here. A partially
    # present domain schema is still an error and must never be silently blessed.
    domain_tables = {"returns", "action_audit"}
    if not (domain_tables & tables):
        return
    if not domain_tables.issubset(tables):
        raise RuntimeError("M4.3-B1 requires both existing returns and action_audit tables")
    indexes = {row.get("name") for row in inspector.get_indexes("returns")}
    active = sa.text("source_type = 'SIMULATED_TEST_DATA' AND status IN ('SUBMITTED','UNDER_REVIEW','APPROVED')")
    source = sa.text("source_type = 'SIMULATED_TEST_DATA' AND source_ref IS NOT NULL")
    if "uq_returns_one_active_simulated_per_order" not in indexes:
        duplicate = op.get_bind().execute(sa.text(
            "SELECT order_id FROM returns WHERE source_type='SIMULATED_TEST_DATA' "
            "AND status IN ('SUBMITTED','UNDER_REVIEW','APPROVED') GROUP BY order_id HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicate:
            raise RuntimeError("cannot add isolated return idempotency index: duplicate active simulated applications exist")
        op.create_index("uq_returns_one_active_simulated_per_order", "returns", ["order_id"], unique=True,
                        postgresql_where=active, sqlite_where=active)
    if "uq_returns_simulated_source_ref" not in indexes:
        duplicate = op.get_bind().execute(sa.text(
            "SELECT source_ref FROM returns WHERE source_type='SIMULATED_TEST_DATA' AND source_ref IS NOT NULL "
            "GROUP BY source_ref HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicate:
            raise RuntimeError("cannot add isolated return source idempotency index: duplicate source references exist")
        op.create_index("uq_returns_simulated_source_ref", "returns", ["source_ref"], unique=True,
                        postgresql_where=source, sqlite_where=source)
    # The shared ActionAudit table remains the authority for both M4.2 test
    # receipts and the isolated M4.3-B1 return transaction lifecycle.
    constraints = {row.get("name") for row in inspector.get_check_constraints("action_audit")}
    if "ck_action_audit_event_type" in constraints:
        with op.batch_alter_table("action_audit") as batch:
            batch.drop_constraint("ck_action_audit_event_type", type_="check")
            batch.create_check_constraint("ck_action_audit_event_type",
                "event_type IN ('ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED','SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED','RETURN_EXECUTION_STARTED','RETURN_REQUEST_TRANSACTION_COMMITTED','ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN')")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    constraints = ( {row.get("name") for row in inspector.get_check_constraints("action_audit")}
        if "action_audit" in tables else set())
    if "ck_action_audit_event_type" in constraints:
        with op.batch_alter_table("action_audit") as batch:
            batch.drop_constraint("ck_action_audit_event_type", type_="check")
            batch.create_check_constraint("ck_action_audit_event_type",
                "event_type IN ('ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED','SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED','ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN')")
    indexes = ({row.get("name") for row in inspector.get_indexes("returns")}
        if "returns" in tables else set())
    for name in ("uq_returns_simulated_source_ref", "uq_returns_one_active_simulated_per_order"):
        if name in indexes:
            op.drop_index(name, table_name="returns")
