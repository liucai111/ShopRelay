"""Pure in-memory SQLite tests for the synthetic Shipment bundle/reset bridge."""
import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from app.m1.models import Base, Order, Shipment, User
from app.m3.order_tools import SqlAlchemyOrderReadRepository, ToolExecutionError
from eval.m6_1.shipment_fixture_loader import ShipmentFixtureError
from eval.m6_1.shipment_fixture_reset import cleanup_bundle_shipments, load_bundle_shipments


@pytest.fixture
def db():
    engine = create_engine("sqlite+pysqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine, tables=[User.__table__, Order.__table__, Shipment.__table__])
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add_all([
            User(user_id="db-user-a", display_name="A"),
            User(user_id="db-user-b", display_name="B"),
        ])
        session.flush()
        session.add_all([
            Order(order_id="DB-ORDER-A", user_id="db-user-a", status="PAID", paid_at=None,
                  delivered_at=None, paid_cents=100, version=1),
            Order(order_id="DB-ORDER-B", user_id="db-user-b", status="PAID", paid_at=None,
                  delivered_at=None, paid_cents=100, version=1),
        ])
        session.commit()
        yield session, factory
    engine.dispose()


def bundle(shipments=None):
    return {
        "fixtures": {
            "users": [{"user_id": "SYN-USER-A"}, {"user_id": "SYN-USER-B"}],
            "orders_and_snapshots": [
                {"id": "syn-order-a", "order_id": "DB-ORDER-A", "owner_id": "SYN-USER-A"},
                {"id": "syn-order-b", "order_id": "DB-ORDER-B", "owner_id": "SYN-USER-B"},
            ],
            "shipments": shipments or [],
        }
    }


def shipment(**changes):
    return {"shipment_id": "syn-ship-a", "order_id": "DB-ORDER-A",
            "status": "IN_TRANSIT", "observed_at": "2026-10-25T08:00:00+08:00", **changes}


def owner_map():
    return {"SYN-USER-A": "db-user-a", "SYN-USER-B": "db-user-b"}


def test_bundle_bridge_maps_owner_and_order_then_exactly_cleans_idempotently(db):
    session, factory = db
    receipt = load_bundle_shipments(session, bundle([shipment()]), owner_id_map=owner_map(), probe_id="case1")
    session.commit()
    assert len(receipt.shipment_receipts) == 1
    persisted_id = receipt.shipment_receipts[0].shipment_ids[0]
    assert persisted_id == "EV-case1-syn-ship-a"
    repo = SqlAlchemyOrderReadRepository(factory)
    assert repo.track_shipment("DB-ORDER-A", "db-user-a")["availability"] == "AVAILABLE"
    assert repo.track_shipment("DB-ORDER-A", "db-user-a")["records"][0]["status"] == "IN_TRANSIT"
    with pytest.raises(ToolExecutionError, match="ORDER_NOT_FOUND"):
        repo.track_shipment("DB-ORDER-A", "db-user-b")
    session.add(Shipment(shipment_id="unrelated", order_id="DB-ORDER-B", status="IN_TRANSIT",
                         observed_at="2026-10-25T00:00:00Z"))
    session.flush()
    assert cleanup_bundle_shipments(session, receipt) == 1
    session.commit()
    assert session.get(Shipment, persisted_id) is None
    assert session.get(Shipment, "unrelated") is not None
    assert cleanup_bundle_shipments(session, receipt) == 0


@pytest.mark.parametrize("shipment_row, expected", [
    (shipment(status="DELIVERED"), "SHIPMENT_STATUS_UNSUPPORTED"),
    (shipment(order_id="MISSING-ORDER"), "SHIPMENT_ORDER_FIXTURE_MISSING"),
])
def test_bundle_bridge_rejects_unsupported_state_and_missing_fixture_order(db, shipment_row, expected):
    session, _ = db
    with pytest.raises(ShipmentFixtureError, match=expected):
        load_bundle_shipments(session, bundle([shipment_row]), owner_id_map=owner_map(), probe_id="case2")
    assert session.execute(select(Shipment.shipment_id)).scalars().all() == []


def test_bundle_bridge_rejects_database_owner_mismatch_and_duplicate_rows(db):
    session, _ = db
    wrong_owner_mapping = {"SYN-USER-A": "db-user-b", "SYN-USER-B": "db-user-a"}
    with pytest.raises(ShipmentFixtureError, match="ORDER_OWNER_OR_ASSOCIATION_MISMATCH"):
        load_bundle_shipments(session, bundle([shipment()]), owner_id_map=wrong_owner_mapping, probe_id="case3")
    with pytest.raises(ShipmentFixtureError, match="DUPLICATE_FIXTURE_ID"):
        load_bundle_shipments(session, bundle([shipment(), shipment()]), owner_id_map=owner_map(), probe_id="case4")
    assert session.execute(select(Shipment.shipment_id)).scalars().all() == []


def test_bundle_bridge_requires_explicit_alias_map_and_empty_list_inserts_nothing(db):
    session, _ = db
    with pytest.raises(ShipmentFixtureError, match="OWNER_ID_MAP_REQUIRED"):
        load_bundle_shipments(session, bundle([shipment()]), owner_id_map={}, probe_id="case5")
    receipt = load_bundle_shipments(session, bundle(), owner_id_map=owner_map(), probe_id="case6")
    assert receipt.shipment_receipts == ()
    assert cleanup_bundle_shipments(session, receipt) == 0
    assert session.execute(select(Shipment.shipment_id)).scalars().all() == []


def test_bundle_bridge_rejects_colliding_owner_mapping_and_invalid_empty_probe_scope(db):
    session, _ = db
    with pytest.raises(ShipmentFixtureError, match="OWNER_ID_MAP_COLLISION"):
        load_bundle_shipments(session, bundle([shipment()]),
            owner_id_map={"SYN-USER-A": "db-user-a", "SYN-USER-B": "db-user-a"}, probe_id="case9")
    with pytest.raises(ShipmentFixtureError, match="INVALID_OWNER_OR_PROBE_SCOPE"):
        load_bundle_shipments(session, bundle(), owner_id_map=owner_map(), probe_id="../escape")


def test_bundle_bridge_refuses_replay_over_existing_persisted_shipment(db):
    session, _ = db
    first = load_bundle_shipments(session, bundle([shipment()]), owner_id_map=owner_map(), probe_id="case7")
    session.flush()
    with pytest.raises(ShipmentFixtureError, match="SHIPMENT_ID_ALREADY_EXISTS"):
        load_bundle_shipments(session, bundle([shipment()]), owner_id_map=owner_map(), probe_id="case7")
    assert session.get(Shipment, first.shipment_receipts[0].shipment_ids[0]) is not None


def test_loader_rejects_file_sqlite_as_unverified_persistent_database():
    # Creating the Engine does not open this deliberately nonexistent file; the
    # loader must reject its URL before any connection or file is created.
    engine = create_engine("sqlite+pysqlite:///m61c-prohibited-file-target.db")
    factory = sessionmaker(bind=engine)
    with factory() as session:
        with pytest.raises(ShipmentFixtureError, match="DATABASE_SCOPE_NOT_ALLOWED"):
            load_bundle_shipments(session, bundle([shipment()]), owner_id_map=owner_map(), probe_id="case8")
    engine.dispose()


def test_database_scope_guard_allows_only_local_named_test_postgres_without_connecting():
    from types import SimpleNamespace
    from eval.m6_1.shipment_fixture_loader import _assert_isolated_database

    class BindOnlySession:
        def __init__(self, dialect, database, host):
            self.bind = SimpleNamespace(dialect=SimpleNamespace(name=dialect),
                                        url=SimpleNamespace(database=database, host=host))

        def get_bind(self):
            return self.bind

    _assert_isolated_database(BindOnlySession("postgresql", "shoprelay_m11_test", "127.0.0.1"))
    for session in (
        BindOnlySession("postgresql", "shoprelay_m11_test", "remote.example"),
        BindOnlySession("postgresql", "production", "localhost"),
    ):
        with pytest.raises(ShipmentFixtureError, match="DATABASE_SCOPE_NOT_ALLOWED"):
            _assert_isolated_database(session)
