"""Add auditable return/refund lifecycle data for the isolated After-sales Agent.

Revision ID: 20261022_08
Revises: 20261021_07

M0 may already have the minimal `returns`/`refunds` tables. Adopt them additively,
tagging old rows LEGACY_UNVERIFIED instead of inferring approval or provenance.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text

revision = "20261022_08"
down_revision = "20261021_07"
branch_labels = None
depends_on = None

LEGACY = "LEGACY_UNVERIFIED"
RETURN_STATUSES = "'SUBMITTED','UNDER_REVIEW','APPROVED','REJECTED','CANCELLED','CLOSED'"
REFUND_STATUSES = "'SUBMITTED','UNDER_REVIEW','APPROVED','PROCESSING','COMPLETED','REJECTED','FAILED','CANCELLED'"


def _inspector():
    return inspect(op.get_bind())


def _ensure_table(name: str, columns: list[sa.Column], constraints: list[sa.Constraint]) -> None:
    if name not in set(_inspector().get_table_names()):
        op.create_table(name, *columns, *constraints)


def _ensure_columns(name: str, columns: list[sa.Column]) -> None:
    present = {column["name"] for column in _inspector().get_columns(name)}
    missing = [column for column in columns if column.name not in present]
    if missing:
        # Batch mode keeps this compatible with SQLite M0 tables as well as PG.
        with op.batch_alter_table(name) as batch:
            for column in missing:
                batch.add_column(column)


def _ensure_checks(name: str, checks: list[tuple[str, str]]) -> None:
    present = {item.get("name") for item in _inspector().get_check_constraints(name)}
    missing = [(constraint_name, sql) for constraint_name, sql in checks if constraint_name not in present]
    if missing:
        with op.batch_alter_table(name) as batch:
            for constraint_name, sql in missing:
                batch.create_check_constraint(constraint_name, sql)


def _ensure_foreign_key(name: str, constraint_name: str, local: str, remote_table: str, remote: str) -> None:
    present = {item.get("name") for item in _inspector().get_foreign_keys(name)}
    if constraint_name not in present:
        with op.batch_alter_table(name) as batch:
            batch.create_foreign_key(constraint_name, remote_table, [local], [remote])


def upgrade() -> None:
    tables = set(_inspector().get_table_names())
    if "orders" not in tables:
        raise RuntimeError("after-sales migration requires the existing orders table")

    created_returns = "returns" not in tables
    created_refunds = "refunds" not in tables
    utc = sa.DateTime(timezone=True)
    if "returns" not in tables:
        _ensure_table("returns", [
            sa.Column("return_id", sa.String(64), primary_key=True),
            sa.Column("order_id", sa.String(64), sa.ForeignKey("orders.order_id"), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("submitted_at", utc, nullable=False),
            sa.Column("review_started_at", utc),
            sa.Column("approved_at", utc),
            sa.Column("rejected_at", utc),
            sa.Column("cancelled_at", utc),
            sa.Column("closed_at", utc),
            sa.Column("evidence_ref", sa.String(256)),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_ref", sa.String(160), nullable=False),
        ], [
            sa.CheckConstraint(f"status IN ({RETURN_STATUSES})", name="ck_returns_status"),
            sa.CheckConstraint("source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','LEGACY_UNVERIFIED')",
                               name="ck_returns_source_type"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR submitted_at IS NOT NULL",
                               name="ck_returns_submitted_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'UNDER_REVIEW' OR review_started_at IS NOT NULL",
                               name="ck_returns_review_time"),
            sa.CheckConstraint("review_started_at IS NULL OR submitted_at IS NULL OR review_started_at >= submitted_at",
                               name="ck_returns_review_after_submission"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'APPROVED' OR approved_at IS NOT NULL",
                               name="ck_returns_approved_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'REJECTED' OR rejected_at IS NOT NULL",
                               name="ck_returns_rejected_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'CANCELLED' OR cancelled_at IS NOT NULL",
                               name="ck_returns_cancelled_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'CLOSED' OR closed_at IS NOT NULL",
                               name="ck_returns_closed_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR source_ref IS NOT NULL",
                               name="ck_returns_source_ref"),
            sa.CheckConstraint("approved_at IS NULL OR submitted_at IS NULL OR approved_at >= submitted_at",
                               name="ck_returns_approval_after_submission"),
            sa.CheckConstraint("rejected_at IS NULL OR submitted_at IS NULL OR rejected_at >= submitted_at",
                               name="ck_returns_rejection_after_submission"),
            sa.CheckConstraint("cancelled_at IS NULL OR submitted_at IS NULL OR cancelled_at >= submitted_at",
                               name="ck_returns_cancellation_after_submission"),
            sa.CheckConstraint("closed_at IS NULL OR approved_at IS NULL OR closed_at >= approved_at",
                               name="ck_returns_close_after_approval"),
        ])
    else:
        _ensure_columns("returns", [
            sa.Column("submitted_at", utc), sa.Column("review_started_at", utc),
            sa.Column("approved_at", utc), sa.Column("rejected_at", utc),
            sa.Column("cancelled_at", utc),
            sa.Column("closed_at", utc),
            sa.Column("evidence_ref", sa.String(256)),
            sa.Column("source_type", sa.String(32), nullable=False, server_default=LEGACY),
            sa.Column("source_ref", sa.String(160)),
        ])
        _ensure_checks("returns", [
            ("ck_returns_status", f"source_type = '{LEGACY}' OR status IN ({RETURN_STATUSES})"),
            ("ck_returns_source_type", "source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','LEGACY_UNVERIFIED')"),
            ("ck_returns_review_time", f"source_type = '{LEGACY}' OR status <> 'UNDER_REVIEW' OR review_started_at IS NOT NULL"),
            ("ck_returns_review_after_submission", "review_started_at IS NULL OR submitted_at IS NULL OR review_started_at >= submitted_at"),
            ("ck_returns_approved_time", f"source_type = '{LEGACY}' OR status <> 'APPROVED' OR approved_at IS NOT NULL"),
            ("ck_returns_rejected_time", f"source_type = '{LEGACY}' OR status <> 'REJECTED' OR rejected_at IS NOT NULL"),
            ("ck_returns_cancelled_time", f"source_type = '{LEGACY}' OR status <> 'CANCELLED' OR cancelled_at IS NOT NULL"),
            ("ck_returns_closed_time", f"source_type = '{LEGACY}' OR status <> 'CLOSED' OR closed_at IS NOT NULL"),
            ("ck_returns_source_ref", f"source_type = '{LEGACY}' OR source_ref IS NOT NULL"),
            ("ck_returns_approval_after_submission", "approved_at IS NULL OR submitted_at IS NULL OR approved_at >= submitted_at"),
            ("ck_returns_rejection_after_submission", "rejected_at IS NULL OR submitted_at IS NULL OR rejected_at >= submitted_at"),
            ("ck_returns_cancellation_after_submission", "cancelled_at IS NULL OR submitted_at IS NULL OR cancelled_at >= submitted_at"),
            ("ck_returns_close_after_approval", "closed_at IS NULL OR approved_at IS NULL OR closed_at >= approved_at"),
        ])

    if "refunds" not in set(_inspector().get_table_names()):
        _ensure_table("refunds", [
            sa.Column("refund_id", sa.String(64), primary_key=True),
            sa.Column("order_id", sa.String(64), sa.ForeignKey("orders.order_id"), nullable=False),
            sa.Column("return_id", sa.String(64), sa.ForeignKey("returns.return_id")),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("amount_cents", sa.Integer(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("created_at", sa.String(40), nullable=False),
            sa.Column("submitted_at", utc, nullable=False),
            sa.Column("review_started_at", utc),
            sa.Column("approved_at", utc),
            sa.Column("rejected_at", utc),
            sa.Column("cancelled_at", utc),
            sa.Column("processed_at", utc),
            sa.Column("failed_at", utc),
            sa.Column("payment_confirmed_at", utc),
            sa.Column("payment_confirmation_source", sa.String(64)),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_ref", sa.String(160), nullable=False),
        ], [
            sa.CheckConstraint(f"status IN ({REFUND_STATUSES})", name="ck_refunds_status"),
            sa.CheckConstraint("amount_cents >= 0", name="ck_refunds_amount_nonnegative"),
            sa.CheckConstraint("source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','LEGACY_UNVERIFIED')",
                               name="ck_refunds_source_type"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'UNDER_REVIEW' OR review_started_at IS NOT NULL",
                               name="ck_refunds_review_time"),
            sa.CheckConstraint("review_started_at IS NULL OR submitted_at IS NULL OR review_started_at >= submitted_at",
                               name="ck_refunds_review_after_submission"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status NOT IN ('APPROVED','PROCESSING','COMPLETED') OR approved_at IS NOT NULL",
                               name="ck_refunds_approved_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'REJECTED' OR rejected_at IS NOT NULL",
                               name="ck_refunds_rejected_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'CANCELLED' OR cancelled_at IS NOT NULL",
                               name="ck_refunds_cancelled_time"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'FAILED' OR failed_at IS NOT NULL",
                               name="ck_refunds_failed_time"),
            sa.CheckConstraint("status <> 'COMPLETED' OR (processed_at IS NOT NULL AND payment_confirmed_at IS NOT NULL AND payment_confirmation_source IS NOT NULL)",
                               name="ck_refunds_completion_confirmation"),
            sa.CheckConstraint("payment_confirmation_source IS NULL OR payment_confirmation_source = 'PAYMENT_PROVIDER'",
                               name="ck_refunds_payment_source"),
            sa.CheckConstraint("payment_confirmed_at IS NULL OR processed_at IS NULL OR payment_confirmed_at >= processed_at",
                               name="ck_refunds_payment_after_processing"),
            sa.CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR source_ref IS NOT NULL",
                               name="ck_refunds_source_ref"),
            sa.CheckConstraint("approved_at IS NULL OR submitted_at IS NULL OR approved_at >= submitted_at",
                               name="ck_refunds_approval_after_submission"),
            sa.CheckConstraint("rejected_at IS NULL OR submitted_at IS NULL OR rejected_at >= submitted_at",
                               name="ck_refunds_rejection_after_submission"),
            sa.CheckConstraint("cancelled_at IS NULL OR submitted_at IS NULL OR cancelled_at >= submitted_at",
                               name="ck_refunds_cancellation_after_submission"),
            sa.CheckConstraint("failed_at IS NULL OR processed_at IS NULL OR failed_at >= processed_at",
                               name="ck_refunds_failure_after_processing"),
        ])
    else:
        _ensure_columns("refunds", [
            sa.Column("return_id", sa.String(64)), sa.Column("submitted_at", utc),
            sa.Column("review_started_at", utc), sa.Column("approved_at", utc),
            sa.Column("rejected_at", utc), sa.Column("cancelled_at", utc),
            sa.Column("processed_at", utc), sa.Column("failed_at", utc),
            sa.Column("payment_confirmed_at", utc),
            sa.Column("payment_confirmation_source", sa.String(64)),
            sa.Column("source_type", sa.String(32), nullable=False, server_default=LEGACY),
            sa.Column("source_ref", sa.String(160)),
        ])
        _ensure_foreign_key("refunds", "fk_refunds_return_id_returns", "return_id", "returns", "return_id")
        _ensure_checks("refunds", [
            ("ck_refunds_status", f"source_type = '{LEGACY}' OR status IN ({REFUND_STATUSES})"),
            ("ck_refunds_amount_nonnegative", "amount_cents >= 0"),
            ("ck_refunds_source_type", "source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','LEGACY_UNVERIFIED')"),
            ("ck_refunds_review_time", f"source_type = '{LEGACY}' OR status <> 'UNDER_REVIEW' OR review_started_at IS NOT NULL"),
            ("ck_refunds_review_after_submission", "review_started_at IS NULL OR submitted_at IS NULL OR review_started_at >= submitted_at"),
            ("ck_refunds_approved_time", f"source_type = '{LEGACY}' OR status NOT IN ('APPROVED','PROCESSING','COMPLETED') OR approved_at IS NOT NULL"),
            ("ck_refunds_rejected_time", f"source_type = '{LEGACY}' OR status <> 'REJECTED' OR rejected_at IS NOT NULL"),
            ("ck_refunds_cancelled_time", f"source_type = '{LEGACY}' OR status <> 'CANCELLED' OR cancelled_at IS NOT NULL"),
            ("ck_refunds_failed_time", f"source_type = '{LEGACY}' OR status <> 'FAILED' OR failed_at IS NOT NULL"),
            ("ck_refunds_completion_confirmation", "status <> 'COMPLETED' OR (processed_at IS NOT NULL AND payment_confirmed_at IS NOT NULL AND payment_confirmation_source IS NOT NULL)"),
            ("ck_refunds_payment_source", "payment_confirmation_source IS NULL OR payment_confirmation_source = 'PAYMENT_PROVIDER'"),
            ("ck_refunds_payment_after_processing", "payment_confirmed_at IS NULL OR processed_at IS NULL OR payment_confirmed_at >= processed_at"),
            ("ck_refunds_source_ref", f"source_type = '{LEGACY}' OR source_ref IS NOT NULL"),
            ("ck_refunds_approval_after_submission", "approved_at IS NULL OR submitted_at IS NULL OR approved_at >= submitted_at"),
            ("ck_refunds_rejection_after_submission", "rejected_at IS NULL OR submitted_at IS NULL OR rejected_at >= submitted_at"),
            ("ck_refunds_cancellation_after_submission", "cancelled_at IS NULL OR submitted_at IS NULL OR cancelled_at >= submitted_at"),
            ("ck_refunds_failure_after_processing", "failed_at IS NULL OR processed_at IS NULL OR failed_at >= processed_at"),
        ])

    # An approval or completion must have a matching append-only audit event before
    # the read service treats it as verified. Event rows themselves retain provenance.
    if "after_sales_events" not in set(_inspector().get_table_names()):
        op.create_table(
            "after_sales_events",
            sa.Column("event_id", sa.String(64), primary_key=True),
            sa.Column("order_id", sa.String(64), sa.ForeignKey("orders.order_id"), nullable=False),
            sa.Column("return_id", sa.String(64), sa.ForeignKey("returns.return_id")),
            sa.Column("refund_id", sa.String(64), sa.ForeignKey("refunds.refund_id")),
            sa.Column("event_type", sa.String(40), nullable=False),
            sa.Column("occurred_at", utc, nullable=False),
            sa.Column("recorded_at", utc, nullable=False),
            sa.Column("actor_type", sa.String(32), nullable=False),
            sa.Column("source_type", sa.String(32), nullable=False),
            sa.Column("source_ref", sa.String(160), nullable=False),
            sa.Column("details_json", sa.Text(), nullable=False, server_default="{}"),
            sa.CheckConstraint("(return_id IS NOT NULL AND refund_id IS NULL) OR (return_id IS NULL AND refund_id IS NOT NULL)",
                               name="ck_after_sales_events_exactly_one_aggregate"),
            sa.CheckConstraint("event_type IN ('RETURN_SUBMITTED','RETURN_REVIEW_STARTED','RETURN_APPROVED','RETURN_REJECTED','RETURN_CANCELLED','RETURN_CLOSED','REFUND_SUBMITTED','REFUND_REVIEW_STARTED','REFUND_APPROVED','REFUND_PROCESSING','REFUND_COMPLETED','REFUND_REJECTED','REFUND_FAILED','REFUND_CANCELLED')",
                               name="ck_after_sales_events_type"),
            sa.CheckConstraint("actor_type IN ('SYSTEM','HUMAN_REVIEWER','PAYMENT_PROVIDER','SIMULATED_TEST_DATA')",
                               name="ck_after_sales_events_actor"),
            sa.CheckConstraint("source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','PAYMENT_PROVIDER','LEGACY_UNVERIFIED')",
                               name="ck_after_sales_events_source"),
            sa.CheckConstraint("source_ref <> ''", name="ck_after_sales_events_source_ref"),
        )
        op.create_index("ix_after_sales_events_order_time", "after_sales_events", ["order_id", "occurred_at"])
        op.create_index("ix_after_sales_events_return_time", "after_sales_events", ["return_id", "occurred_at"])
        op.create_index("ix_after_sales_events_refund_time", "after_sales_events", ["refund_id", "occurred_at"])

    if "after_sales_schema_meta" not in set(_inspector().get_table_names()):
        op.create_table(
            "after_sales_schema_meta",
            sa.Column("revision_id", sa.String(32), primary_key=True),
            sa.Column("created_returns", sa.Boolean(), nullable=False),
            sa.Column("created_refunds", sa.Boolean(), nullable=False),
        )
        op.get_bind().execute(sa.text(
            "INSERT INTO after_sales_schema_meta (revision_id, created_returns, created_refunds) "
            "VALUES (:revision, :returns, :refunds)"),
            {"revision": revision, "returns": created_returns, "refunds": created_refunds})

    # Match the M0 single-open-full-refund invariant, also covering processing.
    index_names = {item.get("name") for item in _inspector().get_indexes("refunds")}
    if "uq_refunds_one_active_per_order" not in index_names:
        predicate = text("status IN ('SUBMITTED','UNDER_REVIEW','APPROVED','PROCESSING')")
        op.create_index("uq_refunds_one_active_per_order", "refunds", ["order_id"], unique=True,
                        postgresql_where=predicate, sqlite_where=predicate)


def downgrade() -> None:
    tables = set(_inspector().get_table_names())
    for name in ("after_sales_events", "refunds", "returns"):
        if name in tables:
            count = op.get_bind().execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar_one()
            if count:
                raise RuntimeError(f"refusing M3.5 downgrade while {name} contains data")
    created_returns = created_refunds = False
    if "after_sales_schema_meta" in tables:
        flags = op.get_bind().execute(text(
            "SELECT created_returns, created_refunds FROM after_sales_schema_meta WHERE revision_id = :revision"),
            {"revision": revision}).first()
        if flags:
            created_returns, created_refunds = bool(flags[0]), bool(flags[1])
    if "after_sales_events" in tables:
        for index in ("ix_after_sales_events_refund_time", "ix_after_sales_events_return_time",
                      "ix_after_sales_events_order_time"):
            op.drop_index(index, table_name="after_sales_events")
        op.drop_table("after_sales_events")
    # Drop only tables this revision created, and only while empty. Adopted M0
    # tables retain their additive columns; no historic application or audit data
    # is deleted by a downgrade.
    if "refunds" in tables and "uq_refunds_one_active_per_order" in {
            item.get("name") for item in _inspector().get_indexes("refunds")}:
        op.drop_index("uq_refunds_one_active_per_order", table_name="refunds")
    if created_refunds and "refunds" in tables:
        op.drop_table("refunds")
    if created_returns and "returns" in tables:
        op.drop_table("returns")
    if "after_sales_schema_meta" in tables:
        op.drop_table("after_sales_schema_meta")
