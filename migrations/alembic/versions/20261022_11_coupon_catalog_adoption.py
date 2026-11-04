"""Adopt the M0 coupon catalog table when an isolated database lacks it.

Revision ID: 20261022_11
Revises: 20261022_10
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "20261022_11"
down_revision = "20261022_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    change_type = None
    if "coupons" not in tables:
        op.create_table(
            "coupons",
            sa.Column("coupon_code", sa.String(64), primary_key=True),
            sa.Column("policy_id", sa.String(128), nullable=False),
            sa.Column("version", sa.String(64), nullable=False),
            sa.Column("min_spend_cents", sa.Integer(), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=True),
            sa.CheckConstraint("min_spend_cents >= 0", name="ck_coupons_min_spend_nonnegative"),
        )
        change_type = "CREATED_TABLE"
    elif "source_type" not in {column["name"] for column in inspect(op.get_bind()).get_columns("coupons")}:
        # Preserve the original M0 catalog and its rows while recording the
        # provenance of M4.3 isolated fixtures in a nullable additive column.
        op.add_column("coupons", sa.Column("source_type", sa.String(32), nullable=True))
        change_type = "ADDED_SOURCE_TYPE"
    if change_type:
        # The marker makes downgrade exact: adopted M0 tables are never dropped.
        op.create_table("_m43_coupon_catalog_migration_marker",
            sa.Column("revision", sa.String(32), primary_key=True),
            sa.Column("change_type", sa.String(32), nullable=False))
        op.execute(sa.text(
            "INSERT INTO _m43_coupon_catalog_migration_marker (revision, change_type) "
            "VALUES ('20261022_11', :change_type)").bindparams(change_type=change_type))


def downgrade() -> None:
    tables = set(inspect(op.get_bind()).get_table_names())
    if "_m43_coupon_catalog_migration_marker" in tables:
        change_type = op.get_bind().execute(sa.text(
            "SELECT change_type FROM _m43_coupon_catalog_migration_marker WHERE revision = '20261022_11'"
        )).scalar_one_or_none()
        if change_type == "CREATED_TABLE":
            op.drop_table("coupons")
        elif change_type == "ADDED_SOURCE_TYPE":
            op.drop_column("coupons", "source_type")
        op.drop_table("_m43_coupon_catalog_migration_marker")
