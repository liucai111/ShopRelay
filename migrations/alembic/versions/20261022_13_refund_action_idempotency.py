"""Add isolated refund idempotency and audit event constraints.

Revision ID: 20261022_13
Revises: 20261022_12
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261022_13"
down_revision = "20261022_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    domain_tables = {"refunds", "action_audit"}
    if not (domain_tables & tables):
        return
    if not domain_tables.issubset(tables):
        raise RuntimeError("M4.3-B2 requires both existing refunds and action_audit tables")

    indexes = {item.get("name") for item in inspector.get_indexes("refunds")}
    if "uq_refunds_simulated_source_ref" not in indexes:
        duplicate = op.get_bind().execute(sa.text(
            "SELECT source_ref FROM refunds WHERE source_type='SIMULATED_TEST_DATA' "
            "AND source_ref IS NOT NULL GROUP BY source_ref HAVING COUNT(*) > 1 LIMIT 1"
        )).first()
        if duplicate:
            raise RuntimeError("cannot add simulated refund idempotency index: duplicate source references exist")
        predicate = sa.text("source_type = 'SIMULATED_TEST_DATA' AND source_ref IS NOT NULL")
        op.create_index("uq_refunds_simulated_source_ref", "refunds", ["source_ref"], unique=True,
                        postgresql_where=predicate, sqlite_where=predicate)

    constraints = {item.get("name") for item in inspector.get_check_constraints("action_audit")}
    if "ck_action_audit_event_type" in constraints:
        with op.batch_alter_table("action_audit") as batch:
            batch.drop_constraint("ck_action_audit_event_type", type_="check")
            batch.create_check_constraint("ck_action_audit_event_type",
                "event_type IN ('ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED',"
                "'SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED',"
                "'RETURN_EXECUTION_STARTED','RETURN_REQUEST_TRANSACTION_COMMITTED',"
                "'REFUND_EXECUTION_STARTED','REFUND_REQUEST_TRANSACTION_COMMITTED',"
                "'ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN')")


def downgrade() -> None:
    inspector = inspect(op.get_bind())
    tables = set(inspector.get_table_names())
    if "action_audit" in tables:
        refund_history = op.get_bind().execute(sa.text(
            "SELECT COUNT(*) FROM action_audit WHERE event_type IN "
            "('REFUND_EXECUTION_STARTED','REFUND_REQUEST_TRANSACTION_COMMITTED')"
        )).scalar_one()
        if refund_history:
            raise RuntimeError("refusing M4.3-B2 downgrade while refund audit history exists")
    if "action_audit" in tables:
        constraints = {item.get("name") for item in inspector.get_check_constraints("action_audit")}
        if "ck_action_audit_event_type" in constraints:
            with op.batch_alter_table("action_audit") as batch:
                batch.drop_constraint("ck_action_audit_event_type", type_="check")
                batch.create_check_constraint("ck_action_audit_event_type",
                    "event_type IN ('ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED',"
                    "'SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED',"
                    "'RETURN_EXECUTION_STARTED','RETURN_REQUEST_TRANSACTION_COMMITTED',"
                    "'ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN')")
    if "refunds" in tables:
        indexes = {item.get("name") for item in inspector.get_indexes("refunds")}
        if "uq_refunds_simulated_source_ref" in indexes:
            op.drop_index("uq_refunds_simulated_source_ref", table_name="refunds")
