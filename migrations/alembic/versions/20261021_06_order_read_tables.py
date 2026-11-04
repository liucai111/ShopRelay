"""Add M0 read models missing from the isolated M1 PostgreSQL test database.

Revision ID: 20261021_06
Revises: 20261020_05

The M0 SQLite schema already defines these tables. Some historical PostgreSQL
test databases were created by a minimal M1 probe and only contain users and
orders. Create only absent tables so an already seeded M0 database is left
untouched.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261021_06"
down_revision = "20261020_05"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    tables = _tables()
    if "products" not in tables:
        op.create_table(
            "products",
            sa.Column("product_id", sa.String(64), primary_key=True),
            sa.Column("name", sa.String(200), nullable=False),
            sa.Column("category", sa.String(100), nullable=False),
            sa.Column("price_cents", sa.Integer(), nullable=False),
            sa.CheckConstraint("price_cents >= 0", name="ck_products_price_nonnegative"),
        )
    if "order_items" not in tables:
        op.create_table(
            "order_items",
            sa.Column("order_id", sa.String(64), sa.ForeignKey("orders.order_id"), primary_key=True),
            sa.Column("product_id", sa.String(64), sa.ForeignKey("products.product_id"), primary_key=True),
            sa.Column("quantity", sa.Integer(), nullable=False),
            sa.Column("paid_cents", sa.Integer(), nullable=False),
            sa.CheckConstraint("quantity > 0", name="ck_order_items_quantity_positive"),
        )
    if "shipments" not in tables:
        op.create_table(
            "shipments",
            sa.Column("shipment_id", sa.String(64), primary_key=True),
            sa.Column("order_id", sa.String(64), sa.ForeignKey("orders.order_id"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("observed_at", sa.String(40), nullable=False),
        )
        op.create_index("ix_shipments_order_id", "shipments", ["order_id"])


def downgrade() -> None:
    # The revision intentionally reuses tables that may predate Alembic in an
    # M0 installation. A generic downgrade cannot distinguish adopted tables
    # from tables created here without risking M0 business data, so retain them.
    pass
