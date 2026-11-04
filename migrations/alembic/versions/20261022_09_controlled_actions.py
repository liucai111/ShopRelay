"""Add isolated M4.2 controlled action, audit, and simulated receipt tables.

Revision ID: 20261022_09
Revises: 20261022_08
"""
from alembic import op
import sqlalchemy as sa

revision = "20261022_09"
down_revision = "20261022_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    utc = sa.DateTime(timezone=True)
    op.create_table(
        "controlled_pending_actions",
        sa.Column("action_id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("tool_id", sa.String(96), nullable=False),
        sa.Column("target_resource_id", sa.String(128), nullable=False),
        sa.Column("sanitized_arguments_json", sa.Text(), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False),
        sa.Column("action_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("risk_level", sa.String(24), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", utc, nullable=False),
        sa.Column("expires_at", utc, nullable=False),
        sa.Column("confirmed_at", utc),
        sa.Column("execution_started_at", utc),
        sa.Column("finished_at", utc),
        sa.Column("result_json", sa.Text()),
        sa.Column("error_code", sa.String(64)),
        sa.Column("audit_reference", sa.String(64), nullable=False),
        sa.UniqueConstraint("owner_id", "idempotency_key", name="uq_controlled_action_owner_idempotency"),
        sa.CheckConstraint("risk_level IN ('WRITE_LOW_RISK','WRITE_HIGH_RISK')", name="ck_controlled_action_risk"),
        sa.CheckConstraint("status IN ('CREATED','PENDING_CONFIRMATION','CONFIRMED','EXECUTING','SUCCEEDED','FAILED','UNKNOWN_RESULT','REJECTED','CANCELLED','EXPIRED')", name="ck_controlled_action_status"),
        sa.CheckConstraint("action_version >= 1", name="ck_controlled_action_version"),
    )
    op.create_index("ix_controlled_pending_actions_owner_id", "controlled_pending_actions", ["owner_id"])
    op.create_index("ix_controlled_pending_actions_session_id", "controlled_pending_actions", ["session_id"])
    op.create_index("ix_controlled_pending_actions_run_id", "controlled_pending_actions", ["run_id"])
    op.create_index("ix_controlled_pending_actions_trace_id", "controlled_pending_actions", ["trace_id"])
    op.create_index("ix_controlled_pending_actions_status", "controlled_pending_actions", ["status"])
    op.create_index("ix_controlled_action_owner_created", "controlled_pending_actions", ["owner_id", "created_at"])

    op.create_table(
        "action_audit",
        sa.Column("audit_id", sa.String(64), primary_key=True),
        sa.Column("action_id", sa.String(64), sa.ForeignKey("controlled_pending_actions.action_id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(32)),
        sa.Column("to_status", sa.String(32), nullable=False),
        sa.Column("occurred_at", utc, nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("transaction_reference", sa.String(64), unique=True),
        sa.Column("before_summary_json", sa.Text()),
        sa.Column("after_summary_json", sa.Text(), nullable=False),
        sa.Column("error_code", sa.String(64)),
    )
    op.create_index("ix_action_audit_action_id", "action_audit", ["action_id"])
    op.create_index("ix_action_audit_owner_id", "action_audit", ["owner_id"])
    op.create_index("ix_action_audit_trace_id", "action_audit", ["trace_id"])

    op.create_table(
        "simulated_test_actions",
        sa.Column("receipt_id", sa.String(64), primary_key=True),
        sa.Column("action_id", sa.String(64), sa.ForeignKey("controlled_pending_actions.action_id", ondelete="CASCADE"), nullable=False, unique=True),
        sa.Column("target_resource_id", sa.String(128), nullable=False),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("value", sa.String(200), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("created_at", utc, nullable=False),
        sa.CheckConstraint("source_type = 'SIMULATED_TEST_DATA'", name="ck_sim_action_source"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM simulated_test_actions")).scalar_one():
        raise RuntimeError("refusing M4.2 downgrade while simulated action receipts exist")
    if bind.execute(sa.text("SELECT COUNT(*) FROM action_audit")).scalar_one():
        raise RuntimeError("refusing M4.2 downgrade while action audit history exists")
    if bind.execute(sa.text("SELECT COUNT(*) FROM controlled_pending_actions")).scalar_one():
        raise RuntimeError("refusing M4.2 downgrade while pending action history exists")
    op.drop_table("simulated_test_actions")
    op.drop_index("ix_action_audit_trace_id", table_name="action_audit")
    op.drop_index("ix_action_audit_owner_id", table_name="action_audit")
    op.drop_index("ix_action_audit_action_id", table_name="action_audit")
    op.drop_table("action_audit")
    for name in ("ix_controlled_action_owner_created", "ix_controlled_pending_actions_status",
                 "ix_controlled_pending_actions_trace_id", "ix_controlled_pending_actions_run_id",
                 "ix_controlled_pending_actions_session_id", "ix_controlled_pending_actions_owner_id"):
        op.drop_index(name, table_name="controlled_pending_actions")
    op.drop_table("controlled_pending_actions")
