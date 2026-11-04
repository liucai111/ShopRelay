"""Explicit bridge from an approved synthetic fixture bundle to Shipment rows.

This is a composable reset phase, not an application/Dev Smoke Runner hook. The
caller must already have loaded the fixture's Orders in the caller-owned test
database and must supply an explicit synthetic-owner to database-owner mapping.
No database connection, commit, service startup, or fixture discovery occurs here.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

from sqlalchemy.orm import Session

from eval.m6_1.shipment_fixture_loader import (
    ShipmentFixtureError,
    ShipmentLoadReceipt,
    _assert_isolated_database,
    cleanup_shipment_fixtures,
    load_shipment_fixtures,
)


@dataclass(frozen=True)
class ShipmentFixtureResetReceipt:
    """Receipts for exact cleanup; empty means the bundle defined no shipments."""

    probe_id: str
    shipment_receipts: tuple[ShipmentLoadReceipt, ...]


def load_bundle_shipments(
    db: Session,
    fixture_bundle: Mapping[str, Any],
    *,
    owner_id_map: Mapping[str, str],
    probe_id: str,
    personal_database=None,
) -> ShipmentFixtureResetReceipt:
    """Load only ``fixtures.shipments`` after validating its fixture ownership.

    Synthetic owner IDs are never treated as database identities. Each shipment
    must point to exactly one fixture order and that order's owner must be present
    in ``owner_id_map``. The underlying loader independently verifies the actual
    persisted order owner before inserting rows.
    """
    _assert_isolated_database(db, personal_database)
    if not isinstance(fixture_bundle, Mapping):
        raise ShipmentFixtureError("FIXTURE_BUNDLE_INVALID")
    if not isinstance(probe_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", probe_id):
        raise ShipmentFixtureError("INVALID_OWNER_OR_PROBE_SCOPE")
    fixtures = fixture_bundle.get("fixtures")
    if not isinstance(fixtures, Mapping):
        raise ShipmentFixtureError("FIXTURE_BUNDLE_FIXTURES_REQUIRED")
    shipments = fixtures.get("shipments")
    orders = fixtures.get("orders_and_snapshots")
    users = fixtures.get("users")
    if not isinstance(shipments, list) or not isinstance(orders, list) or not isinstance(users, list):
        raise ShipmentFixtureError("FIXTURE_BUNDLE_COLLECTION_INVALID")
    if not isinstance(owner_id_map, Mapping) or not owner_id_map:
        raise ShipmentFixtureError("OWNER_ID_MAP_REQUIRED")

    fixture_users: set[str] = set()
    for user in users:
        if not isinstance(user, Mapping) or not isinstance(user.get("user_id"), str):
            raise ShipmentFixtureError("FIXTURE_USER_INVALID")
        if user["user_id"] in fixture_users:
            raise ShipmentFixtureError("DUPLICATE_FIXTURE_USER")
        fixture_users.add(user["user_id"])
    if any(not isinstance(alias, str) or not isinstance(real_id, str) or not real_id.strip()
           for alias, real_id in owner_id_map.items()):
        raise ShipmentFixtureError("OWNER_ID_MAP_INVALID")
    if len(set(owner_id_map.values())) != len(owner_id_map):
        raise ShipmentFixtureError("OWNER_ID_MAP_COLLISION")
    if not set(owner_id_map).issubset(fixture_users):
        raise ShipmentFixtureError("OWNER_ID_MAP_ALIAS_UNKNOWN")

    order_owners: dict[str, str] = {}
    for order in orders:
        if not isinstance(order, Mapping):
            raise ShipmentFixtureError("FIXTURE_ORDER_INVALID")
        order_id, owner_alias = order.get("order_id"), order.get("owner_id")
        if not isinstance(order_id, str) or not order_id.strip() or not isinstance(owner_alias, str):
            raise ShipmentFixtureError("FIXTURE_ORDER_OWNER_REQUIRED")
        if order_id in order_owners:
            raise ShipmentFixtureError("DUPLICATE_FIXTURE_ORDER")
        if owner_alias not in fixture_users:
            raise ShipmentFixtureError("FIXTURE_ORDER_OWNER_UNKNOWN")
        order_owners[order_id] = owner_alias

    if not shipments:
        return ShipmentFixtureResetReceipt(probe_id=probe_id, shipment_receipts=())

    grouped: dict[str, list[dict[str, Any]]] = {}
    seen_ids: set[str] = set()
    for shipment in shipments:
        if not isinstance(shipment, Mapping):
            raise ShipmentFixtureError("FIXTURE_FIELDS_INVALID")
        fixture_id, order_id = shipment.get("shipment_id"), shipment.get("order_id")
        if fixture_id in seen_ids:
            raise ShipmentFixtureError("DUPLICATE_FIXTURE_ID")
        seen_ids.add(fixture_id)
        if not isinstance(order_id, str) or order_id not in order_owners:
            raise ShipmentFixtureError("SHIPMENT_ORDER_FIXTURE_MISSING")
        owner_alias = order_owners[order_id]
        owner_id = owner_id_map.get(owner_alias)
        if owner_id is None:
            raise ShipmentFixtureError("SHIPMENT_OWNER_MAPPING_MISSING")
        grouped.setdefault(owner_id, []).append(dict(shipment))

    receipts: list[ShipmentLoadReceipt] = []
    # Keep all owners atomic from the caller's transaction perspective. Each
    # nested loader savepoint remains contained by this outer savepoint.
    try:
        with db.begin_nested():
            for owner_id, rows in grouped.items():
                receipts.append(load_shipment_fixtures(
                    db, rows, owner_id=owner_id, probe_id=probe_id, personal_database=personal_database))
    except ShipmentFixtureError:
        raise
    return ShipmentFixtureResetReceipt(probe_id=probe_id, shipment_receipts=tuple(receipts))


def cleanup_bundle_shipments(db: Session, receipt: ShipmentFixtureResetReceipt, *, personal_database=None) -> int:
    """Clean only rows in the returned receipts; repeating cleanup is a no-op."""
    _assert_isolated_database(db, personal_database)
    if not isinstance(receipt, ShipmentFixtureResetReceipt):
        raise ShipmentFixtureError("INVALID_RESET_RECEIPT")
    if not isinstance(receipt.probe_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}", receipt.probe_id):
        raise ShipmentFixtureError("INVALID_RESET_RECEIPT")
    total = 0
    with db.begin_nested():
        for item in receipt.shipment_receipts:
            if item.probe_id != receipt.probe_id:
                raise ShipmentFixtureError("CLEANUP_RECEIPT_PROBE_MISMATCH")
            total += cleanup_shipment_fixtures(db, item, personal_database=personal_database)
    return total
