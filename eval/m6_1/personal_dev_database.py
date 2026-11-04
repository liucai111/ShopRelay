"""One explicitly marked local D1 SQLite target; no settings/DSN fallback."""
from __future__ import annotations

import hashlib
from pathlib import Path
import sqlite3
import uuid

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import sessionmaker

from app.m1.models import Base, RunEvent

PROFILE = "M6.1-D1-PERSONAL-DEV"
RELATIVE_DATABASE = Path("data/eval/m6.1-d1-personal-dev.sqlite3")
POLICY_EVENT_NAMES = ("policy_retrieval_started", "policy_retrieval_finished")
_OLD_EVENT_NAMES = ("started", "route_decided", "agent_started", "retrieval_hop", "tool_started",
    "tool_finished", "action_pending", "answer", "done", "error")


class PersonalDatabaseError(ValueError):
    pass


class PersonalDevDatabase:
    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.path.as_posix()}"

    def __init__(self, repository_root: Path, *, profile: str = PROFILE, relative_database: Path = RELATIVE_DATABASE):
        self.root = Path(repository_root).resolve(strict=True)
        self.profile = profile
        if profile not in {PROFILE, "M6.2-FORMAL-ISOLATED"}:
            raise PersonalDatabaseError("DATABASE_PROFILE_NOT_SUPPORTED")
        expected = self.root / relative_database
        if relative_database.is_absolute() or not expected.resolve().is_relative_to(self.root):
            raise PersonalDatabaseError("DATABASE_OUTSIDE_ROOT")
        if profile == PROFILE and relative_database != RELATIVE_DATABASE:
            raise PersonalDatabaseError("LEGACY_DATABASE_PATH_FIXED")
        if profile != PROFILE and not relative_database.as_posix().startswith("data/eval/formal-targets/"):
            raise PersonalDatabaseError("FORMAL_ISOLATED_DATABASE_PATH_REQUIRED")
        if expected.resolve() != expected.absolute():
            raise PersonalDatabaseError("DATABASE_PATH_ALIAS_REJECTED")
        self.path = expected
        self.identity: str | None = None

    def open(self, *, initialize: bool = False):
        if self.path.resolve() != self.path.absolute():
            raise PersonalDatabaseError("DATABASE_PATH_ALIAS_REJECTED")
        existed = self.path.exists()
        if not existed and not initialize:
            raise PersonalDatabaseError("PERSONAL_DATABASE_NOT_INITIALIZED")
        if initialize:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        if not existed:
            with self.path.open("xb"):
                pass
        engine = create_engine(f"sqlite+pysqlite:///{self.path.as_posix()}",
            connect_args={"check_same_thread": False, "timeout": 5})

        @event.listens_for(engine, "connect")
        def foreign_keys(connection, _record):
            connection.execute("PRAGMA foreign_keys=ON")

        factory = sessionmaker(bind=engine, expire_on_commit=False)
        try:
            if not existed:
                # Only a new dedicated file can be initialized. Never label an
                # existing application/shared database as a personal target.
                with engine.begin() as connection:
                    connection.execute(text("CREATE TABLE d1_personal_target ("
                        "profile TEXT PRIMARY KEY, identity TEXT NOT NULL, path TEXT NOT NULL, "
                        "lease_id TEXT, receipt TEXT)"))
                    connection.execute(text("INSERT INTO d1_personal_target "
                        "(profile,identity,path) VALUES (:profile,:identity,:path)"),
                        {"profile": self.profile, "identity": uuid.uuid4().hex, "path": str(self.path)})
                Base.metadata.create_all(engine)
            with factory() as db:
                self.check_session(db)
            self.check_policy_event_schema(engine)
            if not set(Base.metadata.tables).issubset(inspect(engine).get_table_names()):
                raise PersonalDatabaseError("PERSONAL_DATABASE_SCHEMA_INCOMPLETE")
            if initialize:
                from .fixture_read_models import create_projection_table
                with factory() as db, db.begin():
                    self.check_session(db)
                    create_projection_table(db)
            return engine, factory
        except Exception:
            engine.dispose()
            raise

    def check_session(self, db) -> None:
        bind = db.get_bind()
        if (bind.dialect.name != "sqlite" or not bind.url.database or
                Path(bind.url.database).resolve() != self.path or
                self.path.resolve() != self.path.absolute()):
            raise PersonalDatabaseError("PERSONAL_DATABASE_TARGET_MISMATCH")
        try:
            rows = db.execute(text("SELECT profile,identity,path FROM d1_personal_target")).all()
        except Exception as exc:
            raise PersonalDatabaseError("PERSONAL_DATABASE_MARKER_REQUIRED") from exc
        if len(rows) != 1 or rows[0].profile != self.profile or rows[0].path != str(self.path):
            raise PersonalDatabaseError("PERSONAL_DATABASE_MARKER_MISMATCH")
        if self.identity is not None and self.identity != rows[0].identity:
            raise PersonalDatabaseError("PERSONAL_DATABASE_IDENTITY_CHANGED")
        self.identity = rows[0].identity
        if db.execute(text("PRAGMA foreign_keys")).scalar_one() != 1:
            raise PersonalDatabaseError("FOREIGN_KEYS_REQUIRED")

    @staticmethod
    def check_policy_event_schema(engine) -> str:
        with engine.connect() as connection:
            row = connection.execute(text(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_events'"
            )).first()
        if row is None or not all(f"'{name}'" in row[0] for name in POLICY_EVENT_NAMES):
            raise PersonalDatabaseError("PERSONAL_DATABASE_POLICY_EVENT_SCHEMA_NOT_UPGRADED")
        return row[0]

    def acquire(self, factory, lease_id: str) -> None:
        with factory() as db, db.begin():
            self.check_session(db)
            result = db.execute(text("UPDATE d1_personal_target SET lease_id=:lease,receipt=NULL "
                "WHERE profile=:profile AND lease_id IS NULL"), {"lease": lease_id, "profile": self.profile})
            if result.rowcount != 1:
                raise PersonalDatabaseError("PERSONAL_DATABASE_LEASE_BUSY")

    def release(self, factory, lease_id: str) -> None:
        with factory() as db, db.begin():
            self.check_session(db)
            result = db.execute(text("UPDATE d1_personal_target SET lease_id=NULL,receipt=NULL "
                "WHERE profile=:profile AND lease_id=:lease"), {"profile": self.profile, "lease": lease_id})
            if result.rowcount != 1:
                raise PersonalDatabaseError("PERSONAL_DATABASE_LEASE_MISMATCH")


class FormalPostgresDatabase(PersonalDevDatabase):
    """One explicit loopback simulated-business database, with an immutable marker.

    Credentials stay in a process environment variable. Provisioning refuses a
    populated database; it never relabels or migrates an existing application DB.
    """
    profile = "M6.2-FORMAL-POSTGRES"

    def __init__(self, repository_root: Path, binding: dict):
        import os
        from sqlalchemy.engine import make_url
        self.root = repository_root.resolve(strict=True)
        self.binding = binding
        self.path = self.root / binding["identity_manifest"]
        if (Path(binding["identity_manifest"]).is_absolute() or
                not self.path.resolve().is_relative_to(self.root / "data/eval/formal-targets") or
                self.path.resolve() != self.path.absolute()):
            raise PersonalDatabaseError("FORMAL_POSTGRES_MANIFEST_PATH_INVALID")
        self._url = os.environ[binding["url_env"]]
        url = make_url(self._url)
        allowed_hosts={"127.0.0.1", "localhost", "::1"}
        if binding.get('container_target'):
            from .governance import verify_container_process
            verify_container_process(10002)
            if binding['container_target']!={'service':'m62-eval-postgres','uid':10002}:
                raise PersonalDatabaseError('FORMAL_CONTAINER_TARGET_BINDING_INVALID')
            allowed_hosts.add('m62-eval-postgres')
        if (url.get_backend_name() != "postgresql" or url.host not in allowed_hosts or
                url.database != "shoprelay_m11_test" or
                (url.host, url.port or 5432, url.database) != (binding["host"], binding["port"], binding["database"])):
            raise PersonalDatabaseError("FORMAL_POSTGRES_EXACT_TARGET_REQUIRED")
        self.identity = None

    @property
    def database_url(self):
        return self._url

    def open(self, *, initialize=False):
        import json
        from app.m1.db import make_engine, make_session_factory
        from .formal_assets import write_new
        engine = make_engine(self.database_url)
        factory = make_session_factory(engine)
        if initialize:
            if self.path.exists(): raise PersonalDatabaseError("FORMAL_TARGET_ALREADY_EXISTS")
            with engine.begin() as db:
                if inspect(db).get_table_names(schema="public"):
                    raise PersonalDatabaseError("FORMAL_POSTGRES_MUST_BE_NEW_EMPTY_DATABASE")
                Base.metadata.create_all(db)
                db.execute(text("CREATE TABLE d1_personal_target (profile TEXT PRIMARY KEY,identity TEXT NOT NULL,path TEXT NOT NULL,lease_id TEXT,receipt TEXT)"))
                db.execute(text("INSERT INTO d1_personal_target(profile,identity,path) VALUES(:p,:i,:path)"),
                    {"p":self.profile,"i":uuid.uuid4().hex,"path":str(self.path)})
                from .fixture_read_models import create_projection_table
                create_projection_table(db)
            with factory() as db: self.check_session(db)
            write_new(self.path,{"profile":self.profile,"identity":self.identity,
                "host":self.binding["host"],"port":self.binding["port"],"database":self.binding["database"]})
        else:
            value = json.loads(self.path.read_text(encoding="utf8"))
            if value["profile"] != self.profile: raise PersonalDatabaseError("FORMAL_POSTGRES_PROFILE_CHANGED")
            self.identity = value["identity"]
            with factory() as db: self.check_session(db)
        return engine,factory

    def check_session(self, db):
        from sqlalchemy.engine import make_url
        bind = db.get_bind()
        expected,actual = make_url(self.database_url),bind.url
        if actual != expected or bind.dialect.name != "postgresql":
            raise PersonalDatabaseError("FORMAL_POSTGRES_CONNECTION_CHANGED")
        if db.execute(text("SELECT current_database()")).scalar_one() != self.binding["database"]:
            raise PersonalDatabaseError("FORMAL_POSTGRES_SERVER_TARGET_CHANGED")
        rows = db.execute(text("SELECT profile,identity,path FROM d1_personal_target")).all()
        if len(rows)!=1 or rows[0].profile!=self.profile or rows[0].path!=str(self.path) or (self.identity and rows[0].identity!=self.identity):
            raise PersonalDatabaseError("FORMAL_POSTGRES_IDENTITY_CHANGED")
        self.identity=rows[0].identity


def migrate_personal_policy_event_schema(db_path: Path, *, expected_identity: str,
        expected_marker_path: Path, backup_path: Path) -> dict:
    """Explicit, data-preserving SQLite upgrade for the dedicated create_all D1 database.

    This intentionally does not create/stamp an Alembic version table: the personal
    database was bootstrapped from ORM metadata and has no trustworthy Alembic head.
    The caller must first create a consistent SQLite backup with the backup API.
    """
    path = Path(db_path)
    backup = Path(backup_path)
    marker_path = Path(expected_marker_path)
    if (not path.is_file() or path.is_symlink() or path.resolve() != path.absolute() or
            marker_path.resolve() != path.resolve() or not backup.is_file() or
            backup.is_symlink() or backup.resolve() == path.resolve()):
        raise PersonalDatabaseError("PERSONAL_DATABASE_MIGRATION_PATH_OR_BACKUP_INVALID")
    if not expected_identity:
        raise PersonalDatabaseError("PERSONAL_DATABASE_EXPECTED_IDENTITY_REQUIRED")

    def sql_for(connection, table):
        row = connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row[0] if row else None

    db = sqlite3.connect(str(path), timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")
        integrity_before = db.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity_before != "ok" or db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise PersonalDatabaseError("PERSONAL_DATABASE_INTEGRITY_CHECK_FAILED_BEFORE_MIGRATION")
        marker = db.execute("SELECT profile,identity,path,lease_id,receipt FROM d1_personal_target").fetchall()
        if (len(marker) != 1 or marker[0]["profile"] != PROFILE or
                marker[0]["identity"] != expected_identity or
                Path(marker[0]["path"]).resolve() != path.resolve() or
                marker[0]["lease_id"] is not None or marker[0]["receipt"] is not None):
            raise PersonalDatabaseError("PERSONAL_DATABASE_MARKER_OR_LEASE_MISMATCH")
        nonterminal = db.execute(
            "SELECT count(*) FROM runs WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','MODEL_UNAVAILABLE')"
        ).fetchone()[0]
        pending = db.execute("SELECT count(*) FROM controlled_pending_actions").fetchone()[0]
        if nonterminal or pending:
            raise PersonalDatabaseError("PERSONAL_DATABASE_ACTIVE_RUN_OR_PENDING_ACTION_BLOCKS_MIGRATION")
        before_sql = sql_for(db, "run_events")
        if not before_sql:
            raise PersonalDatabaseError("PERSONAL_DATABASE_RUN_EVENTS_TABLE_MISSING")
        if all(f"'{name}'" in before_sql for name in POLICY_EVENT_NAMES):
            return {"status": "ALREADY_UPGRADED", "identity": expected_identity,
                "path": str(path), "schema_sql": before_sql,
                "row_count": db.execute("SELECT count(*) FROM run_events").fetchone()[0]}
        if (not all(f"'{name}'" in before_sql for name in _OLD_EVENT_NAMES) or
                any(f"'{name}'" in before_sql for name in POLICY_EVENT_NAMES)):
            raise PersonalDatabaseError("PERSONAL_DATABASE_RUN_EVENTS_SCHEMA_UNEXPECTED")
        columns_before = [row[1] for row in db.execute("PRAGMA table_info(run_events)")]
        expected_columns = [column.name for column in RunEvent.__table__.columns]
        if columns_before != expected_columns:
            raise PersonalDatabaseError("PERSONAL_DATABASE_RUN_EVENTS_COLUMNS_UNEXPECTED")
        fk_before = {(row["from"], row["table"], row["to"])
            for row in db.execute("PRAGMA foreign_key_list(run_events)")}
        if fk_before != {("run_id", "runs", "run_id"), ("session_id", "sessions", "session_id")}:
            raise PersonalDatabaseError("PERSONAL_DATABASE_RUN_EVENTS_FOREIGN_KEYS_UNEXPECTED")
        custom_indexes = db.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='index' AND tbl_name='run_events' "
            "AND name NOT LIKE 'sqlite_autoindex%' ORDER BY name").fetchall()
        expected_indexes = {index.name: (tuple(column.name for column in index.columns), bool(index.unique))
            for index in RunEvent.__table__.indexes}
        actual_indexes = {}
        for index in db.execute("PRAGMA index_list(run_events)"):
            if index[1].startswith("sqlite_autoindex"):
                continue
            actual_indexes[index[1]] = (tuple(row[2] for row in db.execute(
                f'PRAGMA index_info("{index[1]}")')), bool(index[2]))
        custom_triggers = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='run_events'")]
        if actual_indexes != expected_indexes or custom_triggers:
            raise PersonalDatabaseError("PERSONAL_DATABASE_RUN_EVENTS_CUSTOM_SCHEMA_UNEXPECTED")
        index_sql = [row["sql"] for row in custom_indexes]
        backup_db = sqlite3.connect(f"file:{backup.resolve().as_posix()}?mode=ro", uri=True)
        try:
            backup_marker = backup_db.execute(
                "SELECT profile,identity,path,lease_id,receipt FROM d1_personal_target").fetchall()
            backup_integrity = backup_db.execute("PRAGMA integrity_check").fetchone()[0]
            backup_schema = sql_for(backup_db, "run_events")
            backup_events = backup_db.execute("SELECT count(*) FROM run_events").fetchone()[0]
            if (backup_integrity != "ok" or len(backup_marker) != 1 or
                    backup_marker[0][0] != PROFILE or backup_marker[0][1] != expected_identity or
                    Path(backup_marker[0][2]).resolve() != path.resolve() or
                    backup_marker[0][3] is not None or backup_marker[0][4] is not None or
                    backup_schema != before_sql or backup_events != db.execute(
                        "SELECT count(*) FROM run_events").fetchone()[0]):
                raise PersonalDatabaseError("PERSONAL_DATABASE_BACKUP_NOT_CONSISTENT")
        finally:
            backup_db.close()
        invalid_events = db.execute(
            "SELECT count(*) FROM run_events WHERE event_type NOT IN (" +
            ",".join("?" for _ in _OLD_EVENT_NAMES) + ")", _OLD_EVENT_NAMES).fetchone()[0]
        if invalid_events:
            raise PersonalDatabaseError("PERSONAL_DATABASE_RUN_EVENTS_DATA_UNEXPECTED")
        row_count_before = db.execute("SELECT count(*) FROM run_events").fetchone()[0]
        try:
            db.execute("PRAGMA foreign_keys=OFF")
            db.execute("BEGIN IMMEDIATE")
            marker = db.execute("SELECT profile,identity,path,lease_id,receipt FROM d1_personal_target").fetchall()
            if (len(marker) != 1 or marker[0]["profile"] != PROFILE or
                    marker[0]["identity"] != expected_identity or
                    Path(marker[0]["path"]).resolve() != path.resolve() or
                    marker[0]["lease_id"] is not None or marker[0]["receipt"] is not None):
                raise PersonalDatabaseError("PERSONAL_DATABASE_MARKER_CHANGED_DURING_MIGRATION")
            if db.execute("SELECT count(*) FROM runs WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','MODEL_UNAVAILABLE')").fetchone()[0] or db.execute("SELECT count(*) FROM controlled_pending_actions").fetchone()[0]:
                raise PersonalDatabaseError("PERSONAL_DATABASE_ACTIVE_STATE_CHANGED_DURING_MIGRATION")
            old_table = "run_events__policy_event_schema_old"
            if sql_for(db, old_table):
                raise PersonalDatabaseError("PERSONAL_DATABASE_MIGRATION_TEMP_TABLE_EXISTS")
            db.execute(f'ALTER TABLE run_events RENAME TO "{old_table}"')
            ddl = str(CreateTable(RunEvent.__table__).compile(dialect=sqlite.dialect()))
            db.execute(ddl)
            quoted = ",".join('"' + name.replace('"', '""') + '"' for name in columns_before)
            db.execute(f'INSERT INTO run_events ({quoted}) SELECT {quoted} FROM "{old_table}"')
            row_count_after_copy = db.execute("SELECT count(*) FROM run_events").fetchone()[0]
            if row_count_after_copy != row_count_before:
                raise PersonalDatabaseError("PERSONAL_DATABASE_RUN_EVENTS_ROW_COUNT_CHANGED")
            db.execute(f'DROP TABLE "{old_table}"')
            # Renaming the legacy table keeps its named indexes until it is
            # dropped. Recreate those indexes only after that drop.
            for statement in index_sql:
                db.execute(statement)
            after_sql = sql_for(db, "run_events")
            if not after_sql or not all(f"'{name}'" in after_sql for name in POLICY_EVENT_NAMES):
                raise PersonalDatabaseError("PERSONAL_DATABASE_POLICY_EVENT_SCHEMA_UPGRADE_FAILED")
            if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise PersonalDatabaseError("PERSONAL_DATABASE_FOREIGN_KEY_CHECK_FAILED_AFTER_MIGRATION")
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.execute("PRAGMA foreign_keys=ON")
        integrity_after = db.execute("PRAGMA integrity_check").fetchone()[0]
        fk_after = db.execute("PRAGMA foreign_key_check").fetchone()
        marker_after = db.execute("SELECT profile,identity,path,lease_id,receipt FROM d1_personal_target").fetchall()
        if (integrity_after != "ok" or fk_after is not None or len(marker_after) != 1 or
                marker_after[0]["identity"] != expected_identity or
                Path(marker_after[0]["path"]).resolve() != path.resolve() or
                marker_after[0]["lease_id"] is not None or marker_after[0]["receipt"] is not None):
            raise PersonalDatabaseError("PERSONAL_DATABASE_POST_MIGRATION_VALIDATION_FAILED")
        return {"status": "UPGRADED", "identity": expected_identity, "path": str(path),
            "before_schema_sql": before_sql, "after_schema_sql": sql_for(db, "run_events"),
            "run_events_rows_preserved": row_count_before,
            "integrity_before": integrity_before, "integrity_after": integrity_after,
            "foreign_key_check_after": "PASS", "backup_sha256": hashlib.sha256(backup.read_bytes()).hexdigest()}
    finally:
        db.close()
