"""Persist ordered SSE events and bounded worker leases.

Revision ID: 20261019_04
Revises: 20261019_03
"""
from alembic import op
import sqlalchemy as sa

revision = "20261019_04"
down_revision = "20261019_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("execution_owner_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "run_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
        sa.CheckConstraint(
            "event_type IN ('started','route_decided','agent_started','retrieval_hop','tool_started','tool_finished','action_pending','answer','done','error')",
            name="ck_run_events_type",
        ),
    )


def downgrade() -> None:
    op.drop_table("run_events")
    with op.batch_alter_table("runs") as batch:
        batch.drop_column("lease_expires_at")
        batch.drop_column("execution_owner_id")
