"""Adopt the M0 inventory snapshot table when absent in isolated API databases.

Revision ID: 20261021_07
Revises: 20261021_06

M0 already defines inventory. This conditional revision repairs minimal M1/M3
test databases without altering a pre-existing business table.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261021_07"
down_revision = "20261021_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if "inventory" not in set(inspect(op.get_bind()).get_table_names()):
        op.create_table(
            "inventory",
            sa.Column("product_id", sa.String(64), sa.ForeignKey("products.product_id"), primary_key=True),
            sa.Column("available_qty", sa.Integer(), nullable=False),
            sa.Column("reserved_qty", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.CheckConstraint("available_qty >= 0", name="ck_inventory_available_nonnegative"),
            sa.CheckConstraint("reserved_qty >= 0", name="ck_inventory_reserved_nonnegative"),
        )


def downgrade() -> None:
    # Existing M0 inventory tables cannot be distinguished safely from the
    # table adopted by this revision; retain data on downgrade.
    pass
