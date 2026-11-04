"""Add isolated order-cancellation lifecycle audit.

Revision ID: 20261022_14
Revises: 20261022_13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261022_14"
down_revision = "20261022_13"
branch_labels = None
depends_on = None


_AUDIT_EVENTS = (
    "'ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED',"
    "'SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED',"
    "'RETURN_EXECUTION_STARTED','RETURN_REQUEST_TRANSACTION_COMMITTED',"
    "'REFUND_EXECUTION_STARTED','REFUND_REQUEST_TRANSACTION_COMMITTED',"
    "'ORDER_CANCEL_EXECUTION_STARTED','ORDER_CANCEL_TRANSACTION_COMMITTED',"
    "'ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN'"
)


def upgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    required = {"orders", "order_items", "action_audit"}
    if not (required & tables):
        return
    if not required.issubset(tables):
        raise RuntimeError("M4.3-B3 requires existing orders, order_items and action_audit tables")
    if "order_events" not in tables:
        op.create_table(
            "order_events",
            sa.Column("event_id", sa.String(64), primary_key=True),
            sa.Column("order_id", sa.String(64), sa.ForeignKey("orders.order_id"), nullable=False),
            sa.Column("event_type", sa.String(40), nullable=False),
            sa.Column("from_status", sa.String(32), nullable=False),
            sa.Column("to_status", sa.String(32), nullable=False),
            sa.Column("from_version", sa.Integer(), nullable=False),
            sa.Column("to_version", sa.Integer(), nullable=False),
            sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("actor_type", sa.String(32), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_ref", sa.String(160), nullable=False, unique=True),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.CheckConstraint("event_type = 'ORDER_CANCELLED'", name="ck_order_events_type"),
            sa.CheckConstraint("from_status IN ('CREATED','PAID') AND to_status = 'CANCELLED'",
                               name="ck_order_events_cancel_transition"),
            sa.CheckConstraint("to_version = from_version + 1", name="ck_order_events_version_step"),
            sa.CheckConstraint("actor_type IN ('SIMULATED_TEST_DATA','BUSINESS_SYSTEM')",
                               name="ck_order_events_actor"),
            sa.CheckConstraint("source_type IN ('SIMULATED_TEST_DATA','BUSINESS_SYSTEM')",
                               name="ck_order_events_source"),
        )
        op.create_index("ix_order_events_order_id", "order_events", ["order_id"])
        op.create_index("uq_order_events_one_cancel", "order_events", ["order_id"], unique=True,
                        postgresql_where=sa.text("event_type = 'ORDER_CANCELLED'"),
                        sqlite_where=sa.text("event_type = 'ORDER_CANCELLED'"))

    if "order_inventory_reservations" not in tables:
        op.create_table(
            "order_inventory_reservations",
            sa.Column("reservation_id", sa.String(64), primary_key=True),
            sa.Column("order_id", sa.String(64), nullable=False),
            sa.Column("product_id", sa.String(64), nullable=False),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(24), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_ref", sa.String(160), nullable=False, unique=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["order_id", "product_id"],
                                   ["order_items.order_id", "order_items.product_id"]),
            sa.CheckConstraint("quantity > 0", name="ck_order_inventory_reservation_quantity"),
            sa.CheckConstraint("status IN ('RESERVED','RELEASED','CONSUMED')",
                               name="ck_order_inventory_reservation_status"),
            sa.CheckConstraint("source_type IN ('SIMULATED_TEST_DATA','BUSINESS_SYSTEM')",
                               name="ck_order_inventory_reservation_source"),
            sa.CheckConstraint("(status = 'RELEASED' AND released_at IS NOT NULL) OR "
                               "(status <> 'RELEASED' AND released_at IS NULL)",
                               name="ck_order_inventory_reservation_release_time"),
        )
        op.create_index("uq_order_inventory_reservation_active_line",
                        "order_inventory_reservations", ["order_id", "product_id"], unique=True,
                        postgresql_where=sa.text("status = 'RESERVED'"),
                        sqlite_where=sa.text("status = 'RESERVED'"))

    constraints = {item.get("name") for item in inspect(bind).get_check_constraints("action_audit")}
    if "ck_action_audit_event_type" in constraints:
        with op.batch_alter_table("action_audit") as batch:
            batch.drop_constraint("ck_action_audit_event_type", type_="check")
            batch.create_check_constraint("ck_action_audit_event_type", f"event_type IN ({_AUDIT_EVENTS})")


def downgrade() -> None:
    bind = op.get_bind()
    tables = set(inspect(bind).get_table_names())
    if "order_events" in tables:
        count = bind.execute(sa.text("SELECT COUNT(*) FROM order_events WHERE event_type='ORDER_CANCELLED'")).scalar_one()
        if count:
            raise RuntimeError("refusing M4.3-B3 downgrade while order cancellation history exists")
    if "order_inventory_reservations" in tables:
        count = bind.execute(sa.text("SELECT COUNT(*) FROM order_inventory_reservations")).scalar_one()
        if count:
            raise RuntimeError("refusing M4.3-B3 downgrade while order reservation ledger rows exist")
    if "action_audit" in tables:
        count = bind.execute(sa.text("SELECT COUNT(*) FROM action_audit WHERE event_type IN "
            "('ORDER_CANCEL_EXECUTION_STARTED','ORDER_CANCEL_TRANSACTION_COMMITTED')")).scalar_one()
        if count:
            raise RuntimeError("refusing M4.3-B3 downgrade while cancellation action audit exists")
    if "action_audit" in tables:
        constraints = {item.get("name") for item in inspect(bind).get_check_constraints("action_audit")}
        if "ck_action_audit_event_type" in constraints:
            with op.batch_alter_table("action_audit") as batch:
                batch.drop_constraint("ck_action_audit_event_type", type_="check")
                batch.create_check_constraint("ck_action_audit_event_type",
                    "event_type IN ('ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED',"
                    "'SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED',"
                    "'RETURN_EXECUTION_STARTED','RETURN_REQUEST_TRANSACTION_COMMITTED',"
                    "'REFUND_EXECUTION_STARTED','REFUND_REQUEST_TRANSACTION_COMMITTED',"
                    "'ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN')")
    if "order_events" in tables:
        op.drop_table("order_events")
    if "order_inventory_reservations" in tables:
        op.drop_table("order_inventory_reservations")
