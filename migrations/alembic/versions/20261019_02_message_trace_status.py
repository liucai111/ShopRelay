"""Add explicit persistence status to messages and traces.

Revision ID: 20261019_02
Revises: 20261019_01
"""
from alembic import op
import sqlalchemy as sa

revision = "20261019_02"
down_revision = "20261019_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("messages") as batch:
        batch.add_column(sa.Column("status", sa.String(24), nullable=False, server_default="RECORDED"))
    with op.batch_alter_table("traces") as batch:
        batch.add_column(sa.Column("status", sa.String(32), nullable=False, server_default="RECORDED"))


def downgrade() -> None:
    with op.batch_alter_table("traces") as batch:
        batch.drop_column("status")
    with op.batch_alter_table("messages") as batch:
        batch.drop_column("status")
