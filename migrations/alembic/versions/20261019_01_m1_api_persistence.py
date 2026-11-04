"""Add M1.1 demo auth, runs, and persistence metadata.

Revision ID: 20261019_01
Revises: None
"""
from alembic import op
import sqlalchemy as sa

revision = "20261019_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    required = {"users", "sessions", "messages", "traces", "orders"}
    missing = required.difference(inspector.get_table_names())
    if missing:
        raise RuntimeError(f"M0 schema must be migrated and seeded before M1: missing {sorted(missing)}")

    op.create_table(
        "demo_credentials",
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "auth_sessions",
        sa.Column("token_hash", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])
    op.create_table(
        "runs",
        sa.Column("run_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False),
        sa.Column("client_request_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="CREATED"),
        sa.Column("error_code", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "session_id", "client_request_id", name="uq_runs_idempotency"),
    )
    op.create_index("ix_runs_user_id", "runs", ["user_id"])
    op.create_index("ix_runs_session_id", "runs", ["session_id"])
    with op.batch_alter_table("sessions") as batch:
        batch.add_column(sa.Column("title", sa.String(200), nullable=True))
        batch.add_column(sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True))
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("sequence", sa.Integer(), nullable=True))
    op.create_index("uq_messages_session_sequence", "messages", ["session_id", "sequence"], unique=True)
    with op.batch_alter_table("traces") as batch:
        batch.add_column(sa.Column("run_id", sa.String(64), nullable=True))
        batch.add_column(sa.Column("event_type", sa.String(64), nullable=True))
        batch.create_foreign_key("fk_traces_run_id_runs", "runs", ["run_id"], ["run_id"], ondelete="SET NULL")
    op.create_index("ix_traces_user_id", "traces", ["user_id"])
    op.create_index("ix_traces_run_id", "traces", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_traces_run_id", table_name="traces")
    op.drop_index("ix_traces_user_id", table_name="traces")
    op.drop_index("uq_messages_session_sequence", table_name="messages")
    with op.batch_alter_table("traces") as batch:
        batch.drop_constraint("fk_traces_run_id_runs", type_="foreignkey")
        batch.drop_column("event_type")
        batch.drop_column("run_id")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("sequence")
    with op.batch_alter_table("sessions") as batch:
        batch.drop_column("updated_at")
        batch.drop_column("title")
    op.drop_index("ix_runs_session_id", table_name="runs")
    op.drop_index("ix_runs_user_id", table_name="runs")
    op.drop_table("runs")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_table("demo_credentials")
