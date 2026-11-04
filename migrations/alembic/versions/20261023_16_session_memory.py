"""Add owner-scoped, versioned session memory summaries for M5.1.

Revision ID: 20261023_16
Revises: 20261023_15
"""
from alembic import op
import sqlalchemy as sa

revision = "20261023_16"
down_revision = "20261023_15"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "session_memory_summaries",
        sa.Column("summary_id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("source_run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL"), unique=True),
        sa.Column("source_through_sequence", sa.Integer(), nullable=False),
        sa.Column("source_message_ids_json", sa.Text(), nullable=False),
        sa.Column("summary_json", sa.Text()),
        sa.Column("estimated_tokens_before", sa.Integer(), nullable=False),
        sa.Column("estimated_tokens_after", sa.Integer(), nullable=False),
        sa.Column("is_clear_marker", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("session_id", "version", name="uq_session_memory_version"),
        sa.CheckConstraint("version >= 1", name="ck_session_memory_version_positive"),
        sa.CheckConstraint("source_through_sequence >= 0", name="ck_session_memory_sequence_nonnegative"),
        sa.CheckConstraint("estimated_tokens_before >= 0 AND estimated_tokens_after >= 0",
                           name="ck_session_memory_tokens_nonnegative"),
        sa.CheckConstraint("(is_clear_marker AND summary_json IS NULL) OR "
                           "((NOT is_clear_marker) AND summary_json IS NOT NULL)",
                           name="ck_session_memory_marker_payload"),
    )
    op.create_index("ix_session_memory_owner_session_version", "session_memory_summaries",
                    ["owner_id", "session_id", "version"])


def downgrade() -> None:
    bind = op.get_bind()
    count = bind.execute(sa.text("SELECT count(*) FROM session_memory_summaries")).scalar_one()
    if count:
        raise RuntimeError("refusing to drop persisted session memory; clear session memory before downgrade")
    op.drop_index("ix_session_memory_owner_session_version", table_name="session_memory_summaries")
    op.drop_table("session_memory_summaries")
