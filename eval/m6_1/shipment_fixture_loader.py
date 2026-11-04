"""Exact-scope loader for synthetic Shipment fixtures used by offline evaluation tests.

This module does not initialize the application database. Callers own the transaction;
the loader flushes inside a savepoint and never commits.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.m1.models import Order, Shipment

_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,31}$")
_SHIPMENT_FIXTURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")
# IN_TRANSIT is the only shipment state evidenced by existing repository fixtures.
SUPPORTED_SHIPMENT_STATES = frozenset({"IN_TRANSIT"})
_FIELDS = frozenset({"shipment_id", "order_id", "status", "observed_at"})


class ShipmentFixtureError(ValueError):
    """A synthetic shipment fixture is invalid or outside the exact test scope."""


def _assert_isolated_database(db: Session, personal_database=None) -> None:
    """Reject accidental writes to production or an unapproved database target."""
    if personal_database is not None:
        from .personal_dev_database import PersonalDevDatabase, FormalPostgresDatabase
        if type(personal_database) not in {PersonalDevDatabase,FormalPostgresDatabase}:
            raise ShipmentFixtureError("PERSONAL_DATABASE_CONTRACT_REQUIRED")
        personal_database.check_session(db)
        return
    try:
        bind = db.get_bind()
        dialect = bind.dialect.name
        database_name = bind.url.database
        database_host = bind.url.host
    except Exception as exc:
        raise ShipmentFixtureError("DATABASE_SCOPE_UNVERIFIED") from exc
    if dialect == "sqlite" and database_name in (None, "", ":memory:"):
        return
    if (dialect == "postgresql" and database_name == "shoprelay_m11_test" and
            database_host in {"localhost", "127.0.0.1", "::1"}):
        return
    raise ShipmentFixtureError("DATABASE_SCOPE_NOT_ALLOWED")


@dataclass(frozen=True)
class ShipmentLoadReceipt:
    probe_id: str
    owner_id: str
    shipment_ids: tuple[str, ...]
    order_ids: tuple[str, ...]


def _utc_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise ShipmentFixtureError("OBSERVED_AT_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ShipmentFixtureError("OBSERVED_AT_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ShipmentFixtureError("OBSERVED_AT_REQUIRES_TIMEZONE")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def load_shipment_fixtures(db: Session, fixtures: Iterable[dict[str, Any]], *,
                           owner_id: str, probe_id: str, personal_database=None) -> ShipmentLoadReceipt:
    """Insert strictly validated fixtures for existing owner-scoped orders.

    The returned IDs are namespaced by probe_id for exact cleanup. Duplicate IDs are
    rejected; this function deliberately does not upsert or overwrite existing rows.
    """
    if not isinstance(owner_id, str) or not owner_id.strip() or not _TOKEN.fullmatch(probe_id or ""):
        raise ShipmentFixtureError("INVALID_OWNER_OR_PROBE_SCOPE")
    _assert_isolated_database(db, personal_database)
    rows = list(fixtures)
    parsed: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, dict) or set(row) != _FIELDS:
            raise ShipmentFixtureError("FIXTURE_FIELDS_INVALID")
        fixture_id, order_id, status = row["shipment_id"], row["order_id"], row["status"]
        if not isinstance(fixture_id, str) or not _SHIPMENT_FIXTURE_ID.fullmatch(fixture_id):
            raise ShipmentFixtureError("SHIPMENT_FIXTURE_ID_INVALID")
        if fixture_id in seen:
            raise ShipmentFixtureError("DUPLICATE_FIXTURE_ID")
        seen.add(fixture_id)
        if not isinstance(order_id, str) or not order_id.strip():
            raise ShipmentFixtureError("ORDER_ID_REQUIRED")
        if status not in SUPPORTED_SHIPMENT_STATES:
            raise ShipmentFixtureError("SHIPMENT_STATUS_UNSUPPORTED")
        shipment_id = f"EV-{probe_id}-{fixture_id}"
        if len(shipment_id) > 64:
            raise ShipmentFixtureError("SHIPMENT_ID_TOO_LONG")
        parsed.append((shipment_id, order_id, status, _utc_timestamp(row["observed_at"])))

    if len({row[0] for row in parsed}) != len(parsed):
        raise ShipmentFixtureError("DUPLICATE_PERSISTED_SHIPMENT_ID")
    order_ids = sorted({row[1] for row in parsed})
    owned_orders = set(db.execute(select(Order.order_id).where(
        Order.order_id.in_(order_ids), Order.user_id == owner_id)).scalars()) if order_ids else set()
    if owned_orders != set(order_ids):
        raise ShipmentFixtureError("ORDER_OWNER_OR_ASSOCIATION_MISMATCH")
    shipment_ids = [row[0] for row in parsed]
    existing = set(db.execute(select(Shipment.shipment_id).where(
        Shipment.shipment_id.in_(shipment_ids))).scalars()) if shipment_ids else set()
    if existing:
        raise ShipmentFixtureError("SHIPMENT_ID_ALREADY_EXISTS")

    try:
        with db.begin_nested():
            db.add_all([Shipment(shipment_id=sid, order_id=oid, status=status, observed_at=observed)
                        for sid, oid, status, observed in parsed])
            db.flush()
    except IntegrityError as exc:
        raise ShipmentFixtureError("SHIPMENT_FIXTURE_CONSTRAINT_FAILED") from exc
    return ShipmentLoadReceipt(probe_id=probe_id, owner_id=owner_id,
        shipment_ids=tuple(shipment_ids), order_ids=tuple(order_ids))


def cleanup_shipment_fixtures(db: Session, receipt: ShipmentLoadReceipt, *, personal_database=None) -> int:
    """Delete only rows named by a loader receipt, with owner and namespace checks."""
    _assert_isolated_database(db, personal_database)
    if not isinstance(receipt, ShipmentLoadReceipt) or not _TOKEN.fullmatch(receipt.probe_id):
        raise ShipmentFixtureError("INVALID_CLEANUP_RECEIPT")
    expected_prefix = f"EV-{receipt.probe_id}-"
    if any(not sid.startswith(expected_prefix) for sid in receipt.shipment_ids):
        raise ShipmentFixtureError("CLEANUP_ID_OUTSIDE_PROBE_SCOPE")
    if len(set(receipt.shipment_ids)) != len(receipt.shipment_ids):
        raise ShipmentFixtureError("DUPLICATE_CLEANUP_ID")
    rows = db.execute(select(Shipment).where(
        Shipment.shipment_id.in_(receipt.shipment_ids) if receipt.shipment_ids else False)).scalars().all()
    if any(row.order_id not in receipt.order_ids for row in rows):
        raise ShipmentFixtureError("CLEANUP_ORDER_SCOPE_MISMATCH")
    if rows:
        owned = set(db.execute(select(Order.order_id).where(
            Order.order_id.in_({row.order_id for row in rows}), Order.user_id == receipt.owner_id)).scalars())
        if owned != {row.order_id for row in rows}:
            raise ShipmentFixtureError("CLEANUP_OWNER_MISMATCH")
    count = len(rows)
    for row in rows:
        db.delete(row)
    db.flush()
    return count
