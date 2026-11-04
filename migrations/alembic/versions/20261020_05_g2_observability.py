"""Add graph execution mapping, idempotent event keys, and persistent observability.

Revision ID: 20261020_05
Revises: 20261019_04
"""
from alembic import op
import sqlalchemy as sa

revision = "20261020_05"
down_revision = "20261019_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("run_events", sa.Column("event_key", sa.String(160), nullable=True))
    op.create_index("uq_run_events_idempotent_key", "run_events", ["run_id", "event_key"], unique=True)
    op.create_table(
        "graph_executions",
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("thread_id", sa.String(64), nullable=False, unique=True),
        sa.Column("orchestrator", sa.String(16), nullable=False),
        sa.Column("graph_version", sa.String(32), nullable=False),
        sa.Column("state_schema_version", sa.String(32), nullable=False),
        sa.Column("execution_version", sa.String(32), nullable=False),
        sa.Column("checkpoint_id", sa.String(128)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.CheckConstraint("orchestrator = 'langgraph'", name="ck_graph_executions_orchestrator"),
        sa.CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN')", name="ck_graph_executions_status"),
    )
    op.create_index("ix_graph_executions_user_id", "graph_executions", ["user_id"])
    op.create_index("ix_graph_executions_user_status", "graph_executions", ["user_id", "status"])
    op.create_table(
        "observability_records",
        sa.Column("record_id", sa.String(192), primary_key=True),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("span_id", sa.String(64)),
        sa.Column("parent_span_id", sa.String(64)),
        sa.Column("graph_version", sa.String(32), nullable=False),
        sa.Column("record_type", sa.String(16), nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.CheckConstraint("record_type IN ('span','metric','log')", name="ck_observability_record_type"),
    )
    op.create_index("ix_observability_records_user_id", "observability_records", ["user_id"])
    op.create_index("ix_observability_records_trace_id", "observability_records", ["trace_id"])
    op.create_index("ix_observability_records_timestamp", "observability_records", ["timestamp"])
    op.create_index("ix_observability_time_type", "observability_records", ["timestamp", "record_type"])
    op.create_index("ix_observability_run_type", "observability_records", ["run_id", "record_type"])


def downgrade() -> None:
    op.drop_index("ix_observability_run_type", table_name="observability_records")
    op.drop_index("ix_observability_time_type", table_name="observability_records")
    op.drop_index("ix_observability_records_timestamp", table_name="observability_records")
    op.drop_index("ix_observability_records_trace_id", table_name="observability_records")
    op.drop_index("ix_observability_records_user_id", table_name="observability_records")
    op.drop_table("observability_records")
    op.drop_index("ix_graph_executions_user_status", table_name="graph_executions")
    op.drop_index("ix_graph_executions_user_id", table_name="graph_executions")
    op.drop_table("graph_executions")
    op.drop_index("uq_run_events_idempotent_key", table_name="run_events")
    op.drop_column("run_events", "event_key")
