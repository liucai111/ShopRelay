from __future__ import annotations

from typing import Any

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.m0.config import ROOT, load_settings

LEGACY_SCHEMA_REVISION = "20261020_05"
M3_2_SCHEMA_REVISION = "20261021_06"
M3_3_SCHEMA_REVISION = "20261021_07"
M3_5_SCHEMA_REVISION = "20261022_08"
M4_2_SCHEMA_REVISION = "20261022_10"
M4_3_SCHEMA_REVISION = "20261022_11"
M4_3B1_SCHEMA_REVISION = "20261022_12"
M4_3B2_SCHEMA_REVISION = "20261022_13"
M4_3B3_SCHEMA_REVISION = "20261022_14"
M4_3B4_SCHEMA_REVISION = "20261023_15"
M5_1_SCHEMA_REVISION = "20261023_16"
D1_POLICY_AUDIT_SCHEMA_REVISION = "20261027_17"
LATEST_SCHEMA_REVISION = D1_POLICY_AUDIT_SCHEMA_REVISION
_SCHEMA_REVISION_ORDER = (LEGACY_SCHEMA_REVISION, M3_2_SCHEMA_REVISION, M3_3_SCHEMA_REVISION,
                          M3_5_SCHEMA_REVISION, M4_2_SCHEMA_REVISION, M4_3_SCHEMA_REVISION,
                          M4_3B1_SCHEMA_REVISION, M4_3B2_SCHEMA_REVISION,
                          M4_3B3_SCHEMA_REVISION, M4_3B4_SCHEMA_REVISION, M5_1_SCHEMA_REVISION,
                          D1_POLICY_AUDIT_SCHEMA_REVISION)


def default_database_url() -> str:
    configured = load_settings(require_api_key=False).database_url
    if configured:
        return configured
    return f"sqlite:///{(ROOT / 'data' / 'shoprelay.sqlite3').as_posix()}"


def make_engine(database_url: str | None = None, **kwargs: Any) -> Engine:
    url = database_url or default_database_url()
    options = dict(kwargs)
    if url.startswith("sqlite:"):
        options.setdefault("connect_args", {"check_same_thread": False, "timeout": 10})
    engine = create_engine(url, pool_pre_ping=True, future=True, **options)
    if url.startswith("sqlite:"):
        @event.listens_for(engine, "connect")
        def enable_foreign_keys(dbapi_connection, _connection_record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def check_database(engine: Engine) -> bool:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    return True


def readiness_state(engine: Engine, *, required_revision: str = LEGACY_SCHEMA_REVISION) -> tuple[str, str | None]:
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
        has_revision_table = connection.dialect.has_table(connection, "alembic_version")
        revision = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one_or_none() if has_revision_table else None
    try:
        schema_ready = (_SCHEMA_REVISION_ORDER.index(revision) >=
                        _SCHEMA_REVISION_ORDER.index(required_revision))
    except ValueError:
        schema_ready = False
    return ("ready", revision) if schema_ready else ("degraded", revision)


def alembic_config(database_url: str) -> Config:
    path = ROOT / "alembic.ini"
    config = Config(str(path))
    config.set_main_option("script_location", str(ROOT / "migrations" / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    return config


def upgrade_database(database_url: str | None = None) -> None:
    command.upgrade(alembic_config(database_url or default_database_url()), "head")


def current_revision(database_url: str | None = None) -> str | None:
    cfg = alembic_config(database_url or default_database_url())
    from alembic.runtime.migration import MigrationContext

    engine = make_engine(database_url or default_database_url())
    try:
        with engine.connect() as connection:
            if not connection.dialect.has_table(connection, "alembic_version"):
                return None
            return MigrationContext.configure(connection).get_current_revision()
    finally:
        engine.dispose()
