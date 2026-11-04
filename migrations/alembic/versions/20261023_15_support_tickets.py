"""Add isolated, owner-scoped support ticket persistence for M4.3-B4.

Revision ID: 20261023_15
Revises: 20261022_14
"""
from alembic import op
import sqlalchemy as sa

revision = "20261023_15"
down_revision = "20261022_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "support_tickets",
        sa.Column("ticket_id", sa.String(64), primary_key=True),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("order_id", sa.String(64), sa.ForeignKey("orders.order_id", ondelete="SET NULL")),
        sa.Column("session_id", sa.String(64), sa.ForeignKey("sessions.session_id", ondelete="SET NULL")),
        sa.Column("source_run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL")),
        sa.Column("issue_category", sa.String(40), nullable=False),
        sa.Column("priority", sa.String(16), nullable=False),
        sa.Column("user_description", sa.Text(), nullable=False),
        sa.Column("issue_summary", sa.String(600), nullable=False),
        sa.Column("related_agents_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("verified_fact_refs_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("unverified_claim_codes_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("evidence_references_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("reason", sa.String(64), nullable=False),
        sa.Column("status", sa.String(40), nullable=False, server_default="SUBMITTED_PENDING_ACCEPTANCE"),
        sa.Column("action_id", sa.String(64), sa.ForeignKey("controlled_pending_actions.action_id", ondelete="RESTRICT"), nullable=False, unique=True),
        sa.Column("audit_reference", sa.String(64), nullable=False, unique=True),
        sa.Column("dedupe_key", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False, server_default="SIMULATED_TEST_DATA"),
        sa.Column("source_ref", sa.String(160), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("issue_category IN ('ACCOUNT_ACCESS','ORDER_STATUS','RETURN_REFUND','PRODUCT_QUALITY','POLICY_QUESTION','PRIVACY_SECURITY','OTHER')", name="ck_support_tickets_category"),
        sa.CheckConstraint("priority IN ('NORMAL','HIGH')", name="ck_support_tickets_priority"),
        sa.CheckConstraint("status IN ('SUBMITTED_PENDING_ACCEPTANCE','ASSIGNED','IN_PROGRESS','RESOLVED','CLOSED')", name="ck_support_tickets_status"),
        sa.CheckConstraint("source_type = 'SIMULATED_TEST_DATA'", name="ck_support_tickets_source"),
        sa.CheckConstraint("length(issue_summary) BETWEEN 1 AND 600", name="ck_support_tickets_summary_length"),
    )
    op.create_index("ix_support_tickets_owner_id", "support_tickets", ["owner_id"])
    op.create_index("ix_support_tickets_order_id", "support_tickets", ["order_id"])
    op.create_index("ix_support_tickets_session_id", "support_tickets", ["session_id"])
    op.create_index("ix_support_tickets_source_run_id", "support_tickets", ["source_run_id"])
    op.create_index("uq_support_tickets_open_draft", "support_tickets", ["owner_id", "dedupe_key"], unique=True,
                    postgresql_where=sa.text("status IN ('SUBMITTED_PENDING_ACCEPTANCE','ASSIGNED','IN_PROGRESS')"),
                    sqlite_where=sa.text("status IN ('SUBMITTED_PENDING_ACCEPTANCE','ASSIGNED','IN_PROGRESS')"))
    op.create_table(
        "support_ticket_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("ticket_id", sa.String(64), sa.ForeignKey("support_tickets.ticket_id", ondelete="CASCADE"), nullable=False),
        sa.Column("owner_id", sa.String(64), sa.ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False),
        sa.Column("event_type", sa.String(40), nullable=False),
        sa.Column("from_status", sa.String(40)),
        sa.Column("to_status", sa.String(40), nullable=False),
        sa.Column("actor_type", sa.String(24), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(160), nullable=False, unique=True),
        sa.Column("trace_id", sa.String(64), nullable=False),
        sa.Column("run_id", sa.String(64), sa.ForeignKey("runs.run_id", ondelete="SET NULL")),
        sa.Column("audit_reference", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
        sa.CheckConstraint("event_type = 'TICKET_SUBMITTED'", name="ck_support_ticket_event_type"),
        sa.CheckConstraint("to_status = 'SUBMITTED_PENDING_ACCEPTANCE'", name="ck_support_ticket_event_status"),
        sa.CheckConstraint("actor_type = 'SIMULATED_TEST_DATA'", name="ck_support_ticket_event_actor"),
        sa.CheckConstraint("source_type = 'SIMULATED_TEST_DATA'", name="ck_support_ticket_event_source"),
    )
    op.create_index("ix_support_ticket_events_ticket_id", "support_ticket_events", ["ticket_id"])
    op.create_index("ix_support_ticket_events_owner_id", "support_ticket_events", ["owner_id"])
    op.create_index("ix_support_ticket_events_trace_id", "support_ticket_events", ["trace_id"])
    op.create_index("ix_support_ticket_events_run_id", "support_ticket_events", ["run_id"])


def downgrade() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT COUNT(*) FROM support_tickets")).scalar_one():
        raise RuntimeError("refusing M4.3-B4 downgrade while support ticket records exist")
    op.drop_table("support_ticket_events")
    op.drop_table("support_tickets")
