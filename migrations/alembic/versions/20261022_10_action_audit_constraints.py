"""Constrain M4.2 action audit event vocabulary.

Revision ID: 20261022_10
Revises: 20261022_09
"""
from alembic import op
import sqlalchemy as sa

revision = "20261022_10"
down_revision = "20261022_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("action_audit") as batch:
        batch.create_check_constraint("ck_action_audit_event_type",
            "event_type IN ('ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED','SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED','ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN')")
        batch.create_check_constraint("ck_action_audit_to_status",
            "to_status IN ('PENDING_CONFIRMATION','CONFIRMED','EXECUTING','SUCCEEDED','FAILED','UNKNOWN_RESULT','REJECTED','CANCELLED','EXPIRED')")


def downgrade() -> None:
    with op.batch_alter_table("action_audit") as batch:
        batch.drop_constraint("ck_action_audit_to_status", type_="check")
        batch.drop_constraint("ck_action_audit_event_type", type_="check")
