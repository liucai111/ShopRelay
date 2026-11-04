"""Source-preserving sparse read projections for synthetic D1 fixtures.

No production table is relaxed and no missing business fact is filled. This
table lives only in the marked personal database, under the fixture lease.
Adapters return the existing tool read shapes and reuse application validators.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
from dataclasses import replace
import re

from sqlalchemy import text

from app.m3.order_tools import SqlAlchemyOrderReadRepository, ToolExecutionError
from app.m3.product_tools import ProductReadRepository, _validate_product_id
from app.m3.after_sales_tools import AfterSalesReadRepository
from app.m1.models import ReturnRequest, Refund, AfterSalesEvent

READ_TOOLS = frozenset({"get_order", "get_order_items", "track_shipment", "get_refund_status",
    "search_products", "get_product_detail", "recommend_products", "check_inventory",
    "search_knowledge", "get_coupon", "get_warranty"})


def product_id(alias):
    return "P-SYN-" + hashlib.sha256(alias.encode()).hexdigest()[:16].upper()


def create_projection_table(db):
    db.execute(text("CREATE TABLE IF NOT EXISTS d1_fixture_facts ("
        "lease_id TEXT NOT NULL, kind TEXT NOT NULL, source_id TEXT NOT NULL, "
        "resource_id TEXT NOT NULL, owner_id TEXT, payload TEXT NOT NULL, digest TEXT NOT NULL, "
        "PRIMARY KEY (lease_id,kind,source_id), UNIQUE(lease_id,kind,resource_id))"))


def scoped_facts(bundle, case):
    """Expand every declared reference, including FK dependencies and events."""
    fixtures = bundle["fixtures"]
    collections = ("orders_and_snapshots", "products", "inventory", "order_items", "policies", "returns", "refunds")
    index = {}
    for kind in collections:
        for row in fixtures.get(kind, []):
            key = row.get("id", row.get("evidence_id"))
            if not isinstance(key, str) or not key or key in index:
                raise ValueError("FIXTURE_SOURCE_ID_MISSING_OR_DUPLICATE")
            index[key] = (kind, row)
            for event in row.get("events", []):
                index[event["event_id"]] = (kind, row)
    refs = set(case["input"].get("fixture_refs", [])) | set(case["input"].get("acceptable_evidence_ids", []))
    if case["input"].get("order_snapshot_id"):
        refs.add(case["input"]["order_snapshot_id"])
    # Refusal cases intentionally perform no reads and need no target preload.
    if not case["input"].get("expected_tool_calls"):
        refs = set()
    selected = {}
    pending = list(refs)
    while pending:
        ref = pending.pop()
        if ref not in index:
            raise ValueError(f"FIXTURE_REFERENCE_UNRESOLVED: {ref}")
        kind, row = index[ref]
        source_id = row.get("id", row.get("evidence_id"))
        if source_id in selected:
            continue
        selected[source_id] = (kind, row)
        if kind == "products" and not isinstance(row.get("name"), str):
            raise ValueError("PRODUCT_NAME_REQUIRED")
        if kind == "policies":
            if not all(isinstance(row.get(k), str) and row[k] for k in ("text", "version", "effective")):
                raise ValueError("POLICY_TEXT_VERSION_INTERVAL_REQUIRED")
            start, end = row["effective"].split("/")
            if datetime.fromisoformat(start) > datetime.fromisoformat(end):
                raise ValueError("POLICY_INTERVAL_INVALID")
        if kind == "order_items" and (not isinstance(row.get("quantity"), int) or isinstance(row["quantity"], bool) or row["quantity"] < 1):
            raise ValueError("ORDER_ITEM_QUANTITY_INVALID")
        for key in ("order_id", "product_id", "return_id"):
            if row.get(key) in index:
                pending.append(row[key])
    return selected


def fact_rows(bundle, case, owners):
    selected = scoped_facts(bundle, case)
    orders = {r["id"]: r for r in bundle["fixtures"]["orders_and_snapshots"]}
    output = []
    for source_id, (kind, row) in sorted(selected.items()):
        resource = row.get("order_id") if kind == "orders_and_snapshots" else (
            product_id(source_id) if kind == "products" else source_id)
        owner_alias = row.get("owner_id")
        if kind in {"returns", "refunds", "order_items"}:
            owner_alias = orders[row["order_id"]]["owner_id"]
        payload = json.dumps(row, ensure_ascii=False, sort_keys=True)
        record = {"kind": kind, "source_id": source_id, "resource_id": resource,
            "owner_id": owners[owner_alias] if owner_alias else None, "payload": payload}
        record["digest"] = hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()
        output.append(record)
    return output


class ReadAllowance:
    def __init__(self, maximum=32):
        self.maximum, self.calls = maximum, 0
    def take(self):
        if self.calls >= self.maximum:
            raise ToolExecutionError("D1_READ_BUDGET_EXHAUSTED")
        self.calls += 1


class FactStore:
    def __init__(self, factory, target, lease_id, allowance=None):
        self.factory, self.target, self.lease_id = factory, target, lease_id
        self.allowance = allowance or ReadAllowance()
    def rows(self, kind, *, charge=True):
        if charge:
            self.allowance.take()
        with self.factory() as db:
            self.target.check_session(db)
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            if marker.lease_id != self.lease_id:
                raise ToolExecutionError("D1_FIXTURE_LEASE_EXPIRED")
            expected = {r["source_id"]: r for r in json.loads(marker.receipt)["fact_receipts"] if r["kind"] == kind}
            rows = db.execute(text("SELECT source_id,resource_id,owner_id,payload,digest FROM d1_fixture_facts "
                "WHERE lease_id=:lease AND kind=:kind ORDER BY source_id"), {"lease": self.lease_id, "kind": kind}).all()
        result = []
        if set(expected) != {row.source_id for row in rows}:
            raise ToolExecutionError("D1_FACT_RECEIPT_MISMATCH")
        for row in rows:
            body = {"kind": kind, "source_id": row.source_id, "resource_id": row.resource_id,
                "owner_id": row.owner_id, "payload": row.payload}
            if hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest() != row.digest:
                raise ToolExecutionError("D1_FACT_INTEGRITY_MISMATCH")
            if dict(body, digest=row.digest) != expected[row.source_id]:
                raise ToolExecutionError("D1_FACT_RECEIPT_MISMATCH")
            result.append({"source_id": row.source_id, "resource_id": row.resource_id,
                "owner_id": row.owner_id, "fact": json.loads(row.payload)})
        return result
    def owned_order(self, order_id, owner_id):
        row = next((r for r in self.rows("orders_and_snapshots", charge=False)
            if r["resource_id"] == order_id and r["owner_id"] == owner_id), None)
        if row is None:
            raise ToolExecutionError("ORDER_NOT_FOUND", status="NOT_FOUND")
        return row


class FixtureOrderRepository(SqlAlchemyOrderReadRepository):
    def __init__(self, store):
        super().__init__(store.factory)
        self.store = store
    def get_order(self, order_id, owner_id):
        self.store.allowance.take()
        record = self.store.owned_order(order_id, owner_id)
        row = record["fact"]
        return {"source": "business_database", "order_id": order_id,
            "status": row.get("status", row.get("order_status")), "paid_at": row.get("paid_at"),
            "delivered_at": row.get("delivered_at"), "paid_cents": row.get("paid_cents"),
            "version": row.get("version", 1), "fixture_provenance": record["source_id"],
            "additional_recorded_facts": {k: row[k] for k in ("delivered_days_ago", "delivery_days_ago") if k in row},
            "unknown_fields": [k for k in ("status", "paid_at", "delivered_at", "paid_cents")
                if (row.get("status", row.get("order_status")) if k == "status" else row.get(k)) is None]}
    def track_shipment(self, order_id, owner_id):
        self.store.allowance.take()
        self.store.owned_order(order_id, owner_id)
        # Sparse orders have no fabricated core Order row. The real Shipment
        # table is still queried, and positive shipments require a full FK row.
        from sqlalchemy import select
        from app.m1.models import Shipment
        with self.factory() as db:
            rows = db.execute(select(Shipment).where(Shipment.order_id == order_id)
                .order_by(Shipment.observed_at, Shipment.shipment_id)).scalars().all()
        if not rows:
            return {"source": "business_database", "order_id": order_id, "availability": "UNAVAILABLE",
                "records": [], "error_code": "NO_SHIPMENT_RECORD"}
        return {"source": "business_database", "order_id": order_id, "availability": "AVAILABLE",
            "records": [{"status": r.status, "observed_at": r.observed_at} for r in rows]}
    def get_order_items(self, order_id, owner_id):
        self.store.allowance.take()
        order = self.store.owned_order(order_id, owner_id)
        products = {r["source_id"]: r for r in self.store.rows("products", charge=False)}
        items = [r["fact"] for r in self.store.rows("order_items", charge=False)
            if r["owner_id"] == owner_id and r["fact"]["order_id"] == order["source_id"]]
        return {"source": "business_database", "order_id": order_id, "items": [
            {"product_id": product_id(r["product_id"]), "name": products[r["product_id"]]["fact"]["name"],
             "quantity": r["quantity"], "paid_cents": r.get("paid_cents"), "fixture_provenance": r["id"],
             "product_fixture_provenance": products[r["product_id"]]["source_id"]} for r in items]}


class FixtureProductRepository(ProductReadRepository):
    def __init__(self, store):
        super().__init__(store.factory)
        self.store = store
    @staticmethod
    def public(row):
        fact = row["fact"]
        return {"product_id": row["resource_id"], "name": fact["name"], "category": fact.get("category"),
            "price_cents": fact.get("price_cents"), "description": None, "description_status": "NOT_STORED",
            "recorded_attributes": {k: v for k, v in fact.items() if k not in {"id", "name", "category", "price_cents"}},
            "fixture_provenance": row["source_id"]}
    def resolve_product_reference(self, query):
        matches = self.resolve_product_references(query)
        return matches[0] if len(matches) == 1 else None
    def resolve_product_references(self, query):
        return [r["resource_id"] for r in self.store.rows("products", charge=False)
            if r["fact"]["name"].casefold() in query.casefold() or r["resource_id"] in query.upper()]
    def resolve_product_filters(self, query, filters):
        terms = set(re.findall(r"[a-z]{3,}", query.casefold())) - {"fictional", "synthetic", "product", "what", "which", "the"}
        names = " ".join(r["fact"]["name"].casefold() for r in self.store.rows("products", charge=False))
        matches = sorted((term for term in terms if term in names), key=lambda t: (-len(t), t))
        return replace(filters, keyword=matches[0]) if matches else filters
    def get_product_detail(self, product_id):
        _validate_product_id(product_id)
        row = next((r for r in self.store.rows("products") if r["resource_id"] == product_id), None)
        if row is None:
            return {"source": "shoprelay_product_catalog", "availability": "NOT_FOUND", "product_id": product_id, "error_code": "PRODUCT_NOT_FOUND"}
        return {"source": "shoprelay_product_catalog", "availability": "AVAILABLE", "product": self.public(row)}
    def search_products(self, filters):
        filters.validate()
        # Use the production repository's exact substring/category/price filter
        # semantics; missing values never become an affirmative match.
        rows = [r for r in self.store.rows("products") if
            (not filters.keyword or filters.keyword.casefold() in r["fact"]["name"].casefold() or
             filters.keyword.casefold() in str(r["fact"].get("category", "")).casefold()) and
            (not filters.category or r["fact"].get("category") == filters.category) and
            (filters.max_price_cents is None or isinstance(r["fact"].get("price_cents"), int) and r["fact"]["price_cents"] <= filters.max_price_cents)]
        return {"source": "shoprelay_product_catalog", "availability": "AVAILABLE",
            "products": [self.public(r) for r in rows[:filters.limit]], "count": len(rows),
            "truncated": len(rows) > filters.limit}
    def check_inventory(self, product, filters=None):
        self.store.allowance.take()
        products = self.store.rows("products", charge=False)
        if product is not None:
            _validate_product_id(product)
            products = [r for r in products if r["resource_id"] == product]
        inventory = {r["fact"]["product_id"]: r for r in self.store.rows("inventory", charge=False)}
        records = []
        for row in products:
            inv_record = inventory.get(row["source_id"])
            inv = inv_record["fact"] if inv_record else None
            if inv and (not all(isinstance(inv.get(k), int) and not isinstance(inv[k], bool) and inv[k] >= 0 for k in ("on_hand", "reserved_qty")) or inv["on_hand"] < inv["reserved_qty"]):
                raise ToolExecutionError("INVENTORY_DATA_INVALID")
            records.append({"product_id": row["resource_id"], "availability": "UNAVAILABLE",
                "error_code": "INVENTORY_FRESHNESS_UNVERIFIED" if inv else "INVENTORY_RECORD_MISSING",
                "observation_time": inv.get("observed_at") if inv else None,
                "available_qty": inv.get("on_hand") if inv else None, "reserved_qty": inv.get("reserved_qty") if inv else None,
                "snapshot_sellable_qty": inv["on_hand"] - inv["reserved_qty"] if inv and isinstance(inv.get("on_hand"), int) and isinstance(inv.get("reserved_qty"), int) else None,
                "freshness": "SYNTHETIC_SNAPSHOT_NOT_LIVE", "version": 1,
                "fixture_provenance": inv_record["source_id"] if inv_record else None,
                "product_fixture_provenance": row["source_id"]})
        return {"source": "shoprelay_inventory", "product_id": product, "availability": "UNAVAILABLE",
            "records": records, "count": len(records), "error_code": records[0]["error_code"] if records else "PRODUCT_NOT_FOUND"}
    def recommend_products(self, filters, *, require_inventory=False):
        result = self.search_products(filters)
        # No invented cents/credit exchange rate and no inferred shelf fit.
        return {"source": "shoprelay_product_catalog", "availability": "AVAILABLE",
            "recommendations": [{"product": p, "match_status": "UNCONFIRMED",
                "match_reasons": ["Only recorded catalog fields are available; fit/price units/stock may require clarification."],
                "inventory_version": None} for p in result["products"]], "count": result["count"]}


def _utc(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ToolExecutionError("FIXTURE_TIMEZONE_REQUIRED")
    return parsed


class FixtureAfterSalesRepository(AfterSalesReadRepository):
    def __init__(self, store):
        super().__init__(store.factory)
        self.store = store
    def get_refund_status(self, order_id, owner_id):
        self.store.allowance.take()
        order = self.store.owned_order(order_id, owner_id)
        returns, refunds = [], []
        for kind, model, output in (("returns", ReturnRequest, returns), ("refunds", Refund, refunds)):
            for r in self.store.rows(kind, charge=False):
                fact = r["fact"]
                if r["owner_id"] != owner_id or fact["order_id"] != order["source_id"]:
                    continue
                values = {c.name: None for c in model.__table__.columns}
                values.update({k: v for k, v in fact.items() if k in values})
                values.update(order_id=order_id, **{"return_id" if kind == "returns" else "refund_id": fact["id"]})
                for key in values:
                    if key.endswith("_at") and key != "created_at":
                        values[key] = _utc(values[key])
                record = model(**values)  # Detached model, never persisted with invented required fields.
                events = []
                for event in fact.get("events", []):
                    event = dict(event, order_id=order_id)
                    event["occurred_at"], event["recorded_at"] = _utc(event["occurred_at"]), _utc(event["recorded_at"])
                    events.append(AfterSalesEvent(**event))
                output.append(self._return(record, events) if kind == "returns" else self._refund(record, events))
        for row in refunds:
            linked = next((r for r in returns if r["request_id"] == row["return_id"]), None)
            row.update(linked_return_status=linked["status"] if linked else None, linked_return_verified=bool(linked and linked["verified"]))
            if row["return_id"] and not row["linked_return_verified"]:
                row.update(verified=False, verification_state="UNVERIFIED", status="UNKNOWN")
        returns.sort(key=lambda r: (r["created_at"] or "", r["request_id"]), reverse=True)
        refunds.sort(key=lambda r: (r["created_at"] or "", r["request_id"]), reverse=True)
        return {"source": "shoprelay_after_sales_database", "order_id": order_id, "availability": "AVAILABLE",
            "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "application_record_state": "NO_APPLICATION_RECORD" if not returns and not refunds else "PRESENT" if all(r["verified"] for r in returns+refunds) else "UNKNOWN",
            "returns": {"active_request": next((r for r in returns if r["verified"] and r["status"] in {"SUBMITTED", "UNDER_REVIEW", "APPROVED"}), None), "latest_request": next(iter(returns), None)},
            "refunds": {"active_request": next((r for r in refunds if r["verified"] and r["status"] in {"SUBMITTED", "UNDER_REVIEW", "APPROVED", "PROCESSING"}), None), "latest_request": next(iter(refunds), None)}}


class FixtureCouponRepository:
    def __init__(self, store):
        self.store = store
    def get_coupon(self, coupon_code):
        # Delegate the actual catalog lookup. Synthetic metadata lacks a code,
        # policy FK and threshold; it is not converted into a fabricated coupon.
        self.store.allowance.take()
        from app.m3.policy_tools import CouponReadRepository
        return CouponReadRepository(self.store.factory).get_coupon(coupon_code)


def fixture_retrieval(store, knowledge_version, evaluation_at):
    from app.m2.retrieval import RetrievalService, RetrievalConfig, RetrievalUnavailable, index_fingerprint
    class Registry:
        def current(self, _fallback=None):
            chunks = []
            for row in store.rows("policies", charge=False):
                fact = row["fact"]
                start, end = fact["effective"].split("/")
                chunks.append({"chunk_id": fact["evidence_id"], "policy_id": fact.get("policy_id",fact["evidence_id"]),
                    "clause_id": fact.get("clause_id","fixture"), "title": fact.get("title",fact["evidence_id"]), "version": fact["version"],
                    "effective_from": start+"T00:00:00Z",
                    "effective_to": (datetime.fromisoformat(end)+timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z"),
                    "content": fact["text"]})
            index = {"knowledge_version": knowledge_version, "chunks": chunks}
            return index, index_fingerprint(index)
    class SparseRetrieval(RetrievalService):
        def retrieve(self, query, **kwargs):
            if kwargs.get("mode", self.config.mode) != "bm25":
                raise RetrievalUnavailable("D1_BM25_ONLY", "External dense/reranker channels are not authorized")
            store.allowance.take()
            return super().retrieve(query, **kwargs)
        def pin_view(self, at=None):
            return super().pin_view(at or evaluation_at)
        def warmup(self):
            raise RetrievalUnavailable("D1_BM25_ONLY", "No model warmup")
    return SparseRetrieval(config=RetrievalConfig(mode="bm25", version="d1-fixture-bm25-r1"), registry=Registry())
