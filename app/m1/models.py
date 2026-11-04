from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, ForeignKeyConstraint,
                        Index, Integer, String, Text, UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class UTCDateTime(TypeDecorator):
    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware UTC")
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, dialect):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc) if value else None


class UTCTextDateTime(TypeDecorator):
    """ISO-8601 UTC codec for the M0 text timestamp columns reused by the API."""
    impl = Text
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect):
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("datetime values must be timezone-aware UTC")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def process_result_value(self, value, dialect):
        if value is None:
            return None
        if isinstance(value, datetime):
            parsed = value
        else:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    """M0 identity table, mapped here for foreign-key validation; account creation stays in M0 seed."""
    __tablename__ = "users"
    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)


class DemoCredential(Base):
    __tablename__ = "demo_credentials"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ConversationSession(Base):
    __tablename__ = "sessions"
    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(UTCTextDateTime(), nullable=False)
    title: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class Message(Base):
    __tablename__ = "messages"
    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCTextDateTime(), nullable=False)
    sequence: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="RECORDED")
    __table_args__ = (UniqueConstraint("session_id", "sequence", name="uq_messages_session_sequence"),)


class SessionMemorySummary(Base):
    """Versioned owner-scoped context summary; never an authority for identity or live facts."""
    __tablename__ = "session_memory_summaries"
    summary_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"), unique=True)
    source_through_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    source_message_ids_json: Mapped[str] = mapped_column(Text, nullable=False)
    summary_json: Mapped[str | None] = mapped_column(Text)
    estimated_tokens_before: Mapped[int] = mapped_column(Integer, nullable=False)
    estimated_tokens_after: Mapped[int] = mapped_column(Integer, nullable=False)
    is_clear_marker: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("session_id", "version", name="uq_session_memory_version"),
                      Index("ix_session_memory_owner_session_version", "owner_id", "session_id", "version"),
                      CheckConstraint("version >= 1", name="ck_session_memory_version_positive"),
                      CheckConstraint("source_through_sequence >= 0", name="ck_session_memory_sequence_nonnegative"),
                      CheckConstraint("estimated_tokens_before >= 0 AND estimated_tokens_after >= 0",
                                      name="ck_session_memory_tokens_nonnegative"),
                      CheckConstraint("(is_clear_marker AND summary_json IS NULL) OR "
                                      "((NOT is_clear_marker) AND summary_json IS NOT NULL)",
                                      name="ck_session_memory_marker_payload"))


class Run(Base):
    __tablename__ = "runs"
    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    client_request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_scope: Mapped[str] = mapped_column(String(192), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="CREATED")
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_detail_json: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str | None] = mapped_column(String(128))
    usage_json: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    execution_owner_id: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    __table_args__ = (UniqueConstraint("user_id", "idempotency_scope", "client_request_id", name="uq_runs_idempotency"),)


class RunEvent(Base):
    __tablename__ = "run_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False)
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    event_key: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        UniqueConstraint("run_id", "seq", name="uq_run_events_run_seq"),
        Index("uq_run_events_idempotent_key", "run_id", "event_key", unique=True),
        CheckConstraint("event_type IN ('started','route_decided','agent_started','retrieval_hop','tool_started','tool_finished','policy_retrieval_started','policy_retrieval_finished','action_pending','answer','done','error')",
                        name="ck_run_events_type"),
    )


class Trace(Base):
    __tablename__ = "traces"
    trace_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.user_id", ondelete="SET NULL"), index=True)
    event_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCTextDateTime(), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"), index=True)
    event_type: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="RECORDED")


class GraphExecutionRecord(Base):
    """Owner-scoped business mapping; it contains no checkpoint payload or message text."""
    __tablename__ = "graph_executions"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    orchestrator: Mapped[str] = mapped_column(String(16), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(32), nullable=False)
    state_schema_version: Mapped[str] = mapped_column(String(32), nullable=False)
    execution_version: Mapped[str] = mapped_column(String(32), nullable=False)
    checkpoint_id: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    __table_args__ = (
        CheckConstraint("orchestrator = 'langgraph'", name="ck_graph_executions_orchestrator"),
        CheckConstraint("status IN ('RUNNING','SUCCEEDED','FAILED','UNKNOWN')", name="ck_graph_executions_status"),
        Index("ix_graph_executions_user_status", "user_id", "status"),
    )


class ObservabilityRecord(Base):
    """Append-only, idempotent span/metric/log records linked to a business Run."""
    __tablename__ = "observability_records"
    record_id: Mapped[str] = mapped_column(String(192), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    span_id: Mapped[str | None] = mapped_column(String(64))
    parent_span_id: Mapped[str | None] = mapped_column(String(64))
    graph_version: Mapped[str] = mapped_column(String(32), nullable=False)
    record_type: Mapped[str] = mapped_column(String(16), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now, index=True)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    __table_args__ = (
        CheckConstraint("record_type IN ('span','metric','log')", name="ck_observability_record_type"),
        Index("ix_observability_time_type", "timestamp", "record_type"),
        Index("ix_observability_run_type", "run_id", "record_type"),
    )


class Order(Base):
    __tablename__ = "orders"
    order_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    paid_at: Mapped[str | None] = mapped_column(String(40))
    delivered_at: Mapped[str | None] = mapped_column(String(40))
    paid_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)


class OrderEvent(Base):
    """Append-only order lifecycle audit, including isolated cancellation commits."""
    __tablename__ = "order_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False)
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    from_version: Mapped[int] = mapped_column(Integer, nullable=False)
    to_version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (
        CheckConstraint("event_type = 'ORDER_CANCELLED'", name="ck_order_events_type"),
        CheckConstraint("from_status IN ('CREATED','PAID') AND to_status = 'CANCELLED'",
                        name="ck_order_events_cancel_transition"),
        CheckConstraint("to_version = from_version + 1", name="ck_order_events_version_step"),
        CheckConstraint("actor_type IN ('SIMULATED_TEST_DATA','BUSINESS_SYSTEM')",
                        name="ck_order_events_actor"),
        CheckConstraint("source_type IN ('SIMULATED_TEST_DATA','BUSINESS_SYSTEM')",
                        name="ck_order_events_source"),
        Index("uq_order_events_one_cancel", "order_id", unique=True,
              postgresql_where=text("event_type = 'ORDER_CANCELLED'"),
              sqlite_where=text("event_type = 'ORDER_CANCELLED'")),
    )


class Product(Base):
    """Read-only mapping for the existing M0 product table; no schema is created here."""
    __tablename__ = "products"
    product_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    price_cents: Mapped[int] = mapped_column(Integer, nullable=False)


class Inventory(Base):
    """Existing M0 inventory snapshot; absence is different from zero stock."""
    __tablename__ = "inventory"
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"), primary_key=True)
    available_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    reserved_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class OrderInventoryReservation(Base):
    """Order-scoped reservation ledger; aggregate inventory alone cannot prove ownership."""
    __tablename__ = "order_inventory_reservations"
    reservation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(64), nullable=False)
    product_id: Mapped[str] = mapped_column(String(64), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    released_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)
    __table_args__ = (
        ForeignKeyConstraint(["order_id", "product_id"],
                             ["order_items.order_id", "order_items.product_id"]),
        CheckConstraint("quantity > 0", name="ck_order_inventory_reservation_quantity"),
        CheckConstraint("status IN ('RESERVED','RELEASED','CONSUMED')",
                        name="ck_order_inventory_reservation_status"),
        CheckConstraint("source_type IN ('SIMULATED_TEST_DATA','BUSINESS_SYSTEM')",
                        name="ck_order_inventory_reservation_source"),
        CheckConstraint("(status = 'RELEASED' AND released_at IS NOT NULL) OR "
                        "(status <> 'RELEASED' AND released_at IS NULL)",
                        name="ck_order_inventory_reservation_release_time"),
        Index("uq_order_inventory_reservation_active_line", "order_id", "product_id", unique=True,
              postgresql_where=text("status = 'RESERVED'"), sqlite_where=text("status = 'RESERVED'")),
    )


class Coupon(Base):
    """M0 coupon catalog metadata; it does not prove user entitlement or validity."""
    __tablename__ = "coupons"
    coupon_code: Mapped[str] = mapped_column(String(64), primary_key=True)
    policy_id: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    min_spend_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(32), nullable=True)


class OrderItem(Base):
    """Read-only mapping for existing order line items."""
    __tablename__ = "order_items"
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), primary_key=True)
    product_id: Mapped[str] = mapped_column(ForeignKey("products.product_id"), primary_key=True)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    paid_cents: Mapped[int] = mapped_column(Integer, nullable=False)


class Shipment(Base):
    """Read-only mapping for the existing shipment event table."""
    __tablename__ = "shipments"
    shipment_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    observed_at: Mapped[str] = mapped_column(String(40), nullable=False)


class Refund(Base):
    """Read-only mapping for an auditable refund request lifecycle."""
    __tablename__ = "refunds"
    refund_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), nullable=False, index=True)
    return_id: Mapped[str | None] = mapped_column(ForeignKey("returns.return_id"))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    review_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    failed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    payment_confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    payment_confirmation_source: Mapped[str | None] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="LEGACY_UNVERIFIED")
    source_ref: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        CheckConstraint("status IN ('SUBMITTED','UNDER_REVIEW','APPROVED','PROCESSING','COMPLETED','REJECTED','FAILED','CANCELLED')",
                        name="ck_refunds_status"),
        CheckConstraint("amount_cents >= 0", name="ck_refunds_amount_nonnegative"),
        CheckConstraint("source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','LEGACY_UNVERIFIED')",
                        name="ck_refunds_source_type"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'UNDER_REVIEW' OR review_started_at IS NOT NULL",
                        name="ck_refunds_review_time"),
        CheckConstraint("review_started_at IS NULL OR submitted_at IS NULL OR review_started_at >= submitted_at",
                        name="ck_refunds_review_after_submission"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status NOT IN ('APPROVED','PROCESSING','COMPLETED') OR approved_at IS NOT NULL",
                        name="ck_refunds_approved_time"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'REJECTED' OR rejected_at IS NOT NULL",
                        name="ck_refunds_rejected_time"),
        CheckConstraint("status <> 'COMPLETED' OR (processed_at IS NOT NULL AND payment_confirmed_at IS NOT NULL AND payment_confirmation_source IS NOT NULL)",
                        name="ck_refunds_completion_confirmation"),
        CheckConstraint("payment_confirmation_source IS NULL OR payment_confirmation_source = 'PAYMENT_PROVIDER'",
                        name="ck_refunds_payment_source"),
        CheckConstraint("payment_confirmed_at IS NULL OR processed_at IS NULL OR payment_confirmed_at >= processed_at",
                        name="ck_refunds_payment_after_processing"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR source_ref IS NOT NULL",
                        name="ck_refunds_source_ref"),
        CheckConstraint("approved_at IS NULL OR submitted_at IS NULL OR approved_at >= submitted_at",
                        name="ck_refunds_approval_after_submission"),
        CheckConstraint("rejected_at IS NULL OR submitted_at IS NULL OR rejected_at >= submitted_at",
                        name="ck_refunds_rejection_after_submission"),
        CheckConstraint("cancelled_at IS NULL OR submitted_at IS NULL OR cancelled_at >= submitted_at",
                        name="ck_refunds_cancellation_after_submission"),
        CheckConstraint("failed_at IS NULL OR processed_at IS NULL OR failed_at >= processed_at",
                        name="ck_refunds_failure_after_processing"),
        Index("uq_refunds_one_active_per_order", "order_id", unique=True,
              postgresql_where=text("status IN ('SUBMITTED','UNDER_REVIEW','APPROVED','PROCESSING')"),
              sqlite_where=text("status IN ('SUBMITTED','UNDER_REVIEW','APPROVED','PROCESSING')")),
        Index("uq_refunds_simulated_source_ref", "source_ref", unique=True,
              postgresql_where=text("source_type = 'SIMULATED_TEST_DATA' AND source_ref IS NOT NULL"),
              sqlite_where=text("source_type = 'SIMULATED_TEST_DATA' AND source_ref IS NOT NULL")),
    )


class ReturnRequest(Base):
    """Read-only mapping for an auditable return application lifecycle."""
    __tablename__ = "returns"
    return_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[str] = mapped_column(String(40), nullable=False)
    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    review_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    rejected_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    cancelled_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    evidence_ref: Mapped[str | None] = mapped_column(String(256))
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="LEGACY_UNVERIFIED")
    source_ref: Mapped[str | None] = mapped_column(String(160))
    __table_args__ = (
        CheckConstraint("status IN ('SUBMITTED','UNDER_REVIEW','APPROVED','REJECTED','CANCELLED','CLOSED')",
                        name="ck_returns_status"),
        CheckConstraint("source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','LEGACY_UNVERIFIED')",
                        name="ck_returns_source_type"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'UNDER_REVIEW' OR review_started_at IS NOT NULL",
                        name="ck_returns_review_time"),
        CheckConstraint("review_started_at IS NULL OR submitted_at IS NULL OR review_started_at >= submitted_at",
                        name="ck_returns_review_after_submission"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'APPROVED' OR approved_at IS NOT NULL",
                        name="ck_returns_approved_time"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'REJECTED' OR rejected_at IS NOT NULL",
                        name="ck_returns_rejected_time"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'CANCELLED' OR cancelled_at IS NOT NULL",
                        name="ck_returns_cancelled_time"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR status <> 'CLOSED' OR closed_at IS NOT NULL",
                        name="ck_returns_closed_time"),
        CheckConstraint("source_type = 'LEGACY_UNVERIFIED' OR source_ref IS NOT NULL",
                        name="ck_returns_source_ref"),
        CheckConstraint("approved_at IS NULL OR submitted_at IS NULL OR approved_at >= submitted_at",
                        name="ck_returns_approval_after_submission"),
        CheckConstraint("rejected_at IS NULL OR submitted_at IS NULL OR rejected_at >= submitted_at",
                        name="ck_returns_rejection_after_submission"),
        CheckConstraint("cancelled_at IS NULL OR submitted_at IS NULL OR cancelled_at >= submitted_at",
                        name="ck_returns_cancellation_after_submission"),
        CheckConstraint("closed_at IS NULL OR approved_at IS NULL OR closed_at >= approved_at",
                        name="ck_returns_close_after_approval"),
        Index("uq_returns_one_active_simulated_per_order", "order_id", unique=True,
              postgresql_where=text("source_type = 'SIMULATED_TEST_DATA' AND status IN ('SUBMITTED','UNDER_REVIEW','APPROVED')"),
              sqlite_where=text("source_type = 'SIMULATED_TEST_DATA' AND status IN ('SUBMITTED','UNDER_REVIEW','APPROVED')")),
        Index("uq_returns_simulated_source_ref", "source_ref", unique=True,
              postgresql_where=text("source_type = 'SIMULATED_TEST_DATA' AND source_ref IS NOT NULL"),
              sqlite_where=text("source_type = 'SIMULATED_TEST_DATA' AND source_ref IS NOT NULL")),
    )


class AfterSalesEvent(Base):
    """Append-only provenance events for return/refund status transitions."""
    __tablename__ = "after_sales_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"), nullable=False, index=True)
    return_id: Mapped[str | None] = mapped_column(ForeignKey("returns.return_id"))
    refund_id: Mapped[str | None] = mapped_column(ForeignKey("refunds.refund_id"))
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (
        CheckConstraint("(return_id IS NOT NULL AND refund_id IS NULL) OR (return_id IS NULL AND refund_id IS NOT NULL)",
                        name="ck_after_sales_events_exactly_one_aggregate"),
        CheckConstraint("event_type IN ('RETURN_SUBMITTED','RETURN_REVIEW_STARTED','RETURN_APPROVED','RETURN_REJECTED','RETURN_CANCELLED','RETURN_CLOSED','REFUND_SUBMITTED','REFUND_REVIEW_STARTED','REFUND_APPROVED','REFUND_PROCESSING','REFUND_COMPLETED','REFUND_REJECTED','REFUND_FAILED','REFUND_CANCELLED')",
                        name="ck_after_sales_events_type"),
        CheckConstraint("actor_type IN ('SYSTEM','HUMAN_REVIEWER','PAYMENT_PROVIDER','SIMULATED_TEST_DATA')",
                        name="ck_after_sales_events_actor"),
        CheckConstraint("source_type IN ('BUSINESS_SYSTEM','SIMULATED_TEST_DATA','PAYMENT_PROVIDER','LEGACY_UNVERIFIED')",
                        name="ck_after_sales_events_source"),
        CheckConstraint("source_ref <> ''", name="ck_after_sales_events_source_ref"),
        Index("ix_after_sales_events_order_time", "order_id", "occurred_at"),
        Index("ix_after_sales_events_return_time", "return_id", "occurred_at"),
        Index("ix_after_sales_events_refund_time", "refund_id", "occurred_at"),
    )


class PendingAction(Base):
    """Owner-scoped immutable proposal for an explicitly confirmed action."""
    __tablename__ = "controlled_pending_actions"
    action_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id", ondelete="CASCADE"), nullable=False, index=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False, index=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tool_id: Mapped[str] = mapped_column(String(96), nullable=False)
    target_resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    sanitized_arguments_json: Mapped[str] = mapped_column(Text, nullable=False)
    arguments_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    action_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    risk_level: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False)
    confirmed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    execution_started_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    result_json: Mapped[str | None] = mapped_column(Text)
    error_code: Mapped[str | None] = mapped_column(String(64))
    audit_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("owner_id", "idempotency_key", name="uq_controlled_action_owner_idempotency"),
        CheckConstraint("risk_level IN ('WRITE_LOW_RISK','WRITE_HIGH_RISK')", name="ck_controlled_action_risk"),
        CheckConstraint("status IN ('CREATED','PENDING_CONFIRMATION','CONFIRMED','EXECUTING','SUCCEEDED','FAILED','UNKNOWN_RESULT','REJECTED','CANCELLED','EXPIRED')",
                        name="ck_controlled_action_status"),
        CheckConstraint("action_version >= 1", name="ck_controlled_action_version"),
        Index("ix_controlled_action_owner_created", "owner_id", "created_at"),
    )


class ActionAudit(Base):
    """Append-only action audit events; no user-facing mutation endpoint exists."""
    __tablename__ = "action_audit"
    audit_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("controlled_pending_actions.action_id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    transaction_reference: Mapped[str | None] = mapped_column(String(64), unique=True)
    before_summary_json: Mapped[str | None] = mapped_column(Text)
    after_summary_json: Mapped[str] = mapped_column(Text, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        CheckConstraint("event_type IN ('ACTION_CREATED_PENDING_CONFIRMATION','USER_CONFIRMED','SIMULATED_EXECUTION_STARTED','SIMULATED_TRANSACTION_COMMITTED','RETURN_EXECUTION_STARTED','RETURN_REQUEST_TRANSACTION_COMMITTED','REFUND_EXECUTION_STARTED','REFUND_REQUEST_TRANSACTION_COMMITTED','ORDER_CANCEL_EXECUTION_STARTED','ORDER_CANCEL_TRANSACTION_COMMITTED','ACTION_EXPIRED','ACTION_REJECTED','ACTION_CANCELLED','EXECUTION_RESULT_UNKNOWN')",
                        name="ck_action_audit_event_type"),
        CheckConstraint("to_status IN ('PENDING_CONFIRMATION','CONFIRMED','EXECUTING','SUCCEEDED','FAILED','UNKNOWN_RESULT','REJECTED','CANCELLED','EXPIRED')",
                        name="ck_action_audit_to_status"),
    )


class SimulatedTestAction(Base):
    """Receipt written only by the isolated M4.2 test adapter, never a business tool."""
    __tablename__ = "simulated_test_actions"
    receipt_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    action_id: Mapped[str] = mapped_column(ForeignKey("controlled_pending_actions.action_id", ondelete="CASCADE"), nullable=False, unique=True)
    target_resource_id: Mapped[str] = mapped_column(String(128), nullable=False)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    __table_args__ = (CheckConstraint("source_type = 'SIMULATED_TEST_DATA'", name="ck_sim_action_source"),)


class SupportTicket(Base):
    """Owner-scoped, locally submitted support ticket; no external helpdesk implied."""
    __tablename__ = "support_tickets"
    ticket_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    order_id: Mapped[str | None] = mapped_column(ForeignKey("orders.order_id", ondelete="SET NULL"), index=True)
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.session_id", ondelete="SET NULL"), index=True)
    source_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"), index=True)
    issue_category: Mapped[str] = mapped_column(String(40), nullable=False)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)
    user_description: Mapped[str] = mapped_column(Text, nullable=False)
    issue_summary: Mapped[str] = mapped_column(String(600), nullable=False)
    related_agents_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    verified_fact_refs_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    unverified_claim_codes_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    evidence_references_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    reason: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, default="SUBMITTED_PENDING_ACCEPTANCE")
    action_id: Mapped[str] = mapped_column(ForeignKey("controlled_pending_actions.action_id", ondelete="RESTRICT"), nullable=False, unique=True)
    audit_reference: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    dedupe_key: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="SIMULATED_TEST_DATA")
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    __table_args__ = (
        CheckConstraint("issue_category IN ('ACCOUNT_ACCESS','ORDER_STATUS','RETURN_REFUND','PRODUCT_QUALITY','POLICY_QUESTION','PRIVACY_SECURITY','OTHER')",
                        name="ck_support_tickets_category"),
        CheckConstraint("priority IN ('NORMAL','HIGH')", name="ck_support_tickets_priority"),
        CheckConstraint("status IN ('SUBMITTED_PENDING_ACCEPTANCE','ASSIGNED','IN_PROGRESS','RESOLVED','CLOSED')",
                        name="ck_support_tickets_status"),
        CheckConstraint("source_type = 'SIMULATED_TEST_DATA'", name="ck_support_tickets_source"),
        CheckConstraint("length(issue_summary) BETWEEN 1 AND 600", name="ck_support_tickets_summary_length"),
        Index("uq_support_tickets_open_draft", "owner_id", "dedupe_key", unique=True,
              postgresql_where=text("status IN ('SUBMITTED_PENDING_ACCEPTANCE','ASSIGNED','IN_PROGRESS')"),
              sqlite_where=text("status IN ('SUBMITTED_PENDING_ACCEPTANCE','ASSIGNED','IN_PROGRESS')")),
    )


class SupportTicketEvent(Base):
    """Append-only ticket lifecycle; M4.3-B4 writes submission only."""
    __tablename__ = "support_ticket_events"
    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ticket_id: Mapped[str] = mapped_column(ForeignKey("support_tickets.ticket_id", ondelete="CASCADE"), nullable=False, index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="CASCADE"), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(40))
    to_status: Mapped[str] = mapped_column(String(40), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(24), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_ref: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    trace_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.run_id", ondelete="SET NULL"), index=True)
    audit_reference: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utc_now)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    __table_args__ = (
        CheckConstraint("event_type = 'TICKET_SUBMITTED'", name="ck_support_ticket_event_type"),
        CheckConstraint("to_status = 'SUBMITTED_PENDING_ACCEPTANCE'", name="ck_support_ticket_event_status"),
        CheckConstraint("actor_type = 'SIMULATED_TEST_DATA'", name="ck_support_ticket_event_actor"),
        CheckConstraint("source_type = 'SIMULATED_TEST_DATA'", name="ck_support_ticket_event_source"),
    )


def safe_trace_json(value: dict) -> str:
    forbidden = ("api_key", "password", "secret", "authorization", "cookie", "token")

    def clean(item):
        if isinstance(item, dict):
            result = {}
            for key, child in item.items():
                normalized = str(key).casefold()
                bounded_usage = (normalized in {"prompt_tokens", "completion_tokens", "total_tokens"} and
                    isinstance(child, int) and not isinstance(child, bool) and 0 <= child <= 10_000_000)
                result[str(key)] = (clean(child) if bounded_usage else
                    "[REDACTED]" if any(word in normalized for word in forbidden) else clean(child))
            return result
        if isinstance(item, list):
            return [clean(child) for child in item]
        if isinstance(item, (str, int, float, bool)) or item is None:
            return item
        return str(item)

    return json.dumps(clean(value), ensure_ascii=False, sort_keys=True)
