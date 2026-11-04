"""Probe and explicitly upgrade only the dedicated local M6.1-D1 SQLite target."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import uuid

from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import sessionmaker

from app.m1.models import ConversationSession, Run, RunEvent, User, utc_now
from eval.m6_1.personal_dev_authorization import DIAGNOSTIC_ROUND_ROOT
from eval.m6_1.personal_dev_database import (
    PROFILE, RELATIVE_DATABASE, migrate_personal_policy_event_schema,
)

ROOT = Path(__file__).resolve().parents[1]
ROUND_ROOT = ROOT / DIAGNOSTIC_ROUND_ROOT
DATABASE_PATH = ROOT / RELATIVE_DATABASE


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def open_readonly(path: Path):
    return sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)


def marker_summary(connection) -> dict:
    rows = connection.execute(
        "SELECT profile,identity,path,lease_id,receipt FROM d1_personal_target"
    ).fetchall()
    if len(rows) != 1:
        raise RuntimeError("PERSONAL_DB_MARKER_CARDINALITY_INVALID")
    row = rows[0]
    return {"profile": row[0], "identity": row[1], "path": row[2],
        "lease_active": row[3] is not None, "receipt_present": row[4] is not None}


def table_counts(path: Path) -> dict[str, int]:
    connection = open_readonly(path)
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names}
    finally:
        connection.close()


def backup_sqlite(source: Path, destination: Path) -> str:
    if (not source.is_file() or source.is_symlink() or source.resolve() != source.absolute() or
            destination.exists() or destination.is_symlink()):
        raise RuntimeError("BACKUP_SOURCE_OR_DESTINATION_INVALID")
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = open_readonly(source)
    dst = sqlite3.connect(str(destination), timeout=10)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    check = open_readonly(destination)
    source_check = open_readonly(source)
    try:
        if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise RuntimeError("BACKUP_INTEGRITY_CHECK_FAILED")
        if marker_summary(check) != marker_summary(source_check):
            raise RuntimeError("BACKUP_TARGET_IDENTITY_MISMATCH")
    finally:
        check.close()
        source_check.close()
    return file_sha256(destination)


def insert_probe_event(path: Path) -> dict:
    engine = create_engine(f"sqlite+pysqlite:///{path.as_posix()}", connect_args={"timeout": 5})

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    factory = sessionmaker(bind=engine, expire_on_commit=False)
    now = utc_now()
    run_id, user_id, session_id, trace_id = ("schema-probe-run", "schema-probe-user",
        "schema-probe-session", "schema-probe-trace")
    try:
        with factory() as db, db.begin():
            db.add(User(user_id=user_id, display_name="Schema Probe"))
            db.flush()
            db.add(ConversationSession(session_id=session_id, user_id=user_id,
                created_at=now, title="schema probe", updated_at=now))
            db.flush()
            db.add(Run(run_id=run_id, user_id=user_id, session_id=session_id,
                client_request_id="schema-probe-request", idempotency_scope="schema-probe",
                request_hash="0" * 64, status="SUCCEEDED", created_at=now, updated_at=now))
            db.flush()
            db.add(RunEvent(event_id="schema-probe-policy-event", run_id=run_id,
                session_id=session_id, trace_id=trace_id, seq=1, timestamp=now,
                event_type="policy_retrieval_started", payload_json="{}"))
        with factory() as db:
            saved = db.execute(select(RunEvent).where(
                RunEvent.event_id == "schema-probe-policy-event")).scalar_one()
            if saved.event_type != "policy_retrieval_started" or saved.run_id != run_id:
                raise RuntimeError("ISOLATED_NEW_EVENT_READBACK_MISMATCH")
            count = db.execute(text("SELECT count(*) FROM run_events WHERE run_id=:id"),
                {"id": run_id}).scalar_one()
            fk = db.execute(text("PRAGMA foreign_key_check")).all()
            integrity = db.execute(text("PRAGMA integrity_check")).scalar_one()
        if count != 1 or fk or integrity != "ok":
            raise RuntimeError("ISOLATED_NEW_EVENT_INTEGRITY_FAILED")
        return {"event_type": saved.event_type, "run_id": saved.run_id,
            "readback_count": count, "foreign_key_check": "PASS", "integrity_check": integrity}
    finally:
        engine.dispose()


def run_probe() -> dict:
    probe_tag = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    probe_dir = ROUND_ROOT / "database" / f"schema-probe-{probe_tag}"
    probe_path = probe_dir / "legacy-copy.sqlite3"
    backup_path = probe_dir / "legacy-copy-backup.sqlite3"
    output = probe_dir / "schema-upgrade-probe-r3.json"
    if output.exists() or probe_path.exists() or backup_path.exists():
        raise RuntimeError("SCHEMA_PROBE_OUTPUT_ALREADY_EXISTS")
    probe_dir.mkdir(parents=True, exist_ok=False)
    actual = open_readonly(DATABASE_PATH)
    try:
        source_marker = marker_summary(actual)
        if source_marker["profile"] != PROFILE or source_marker["lease_active"] or source_marker["receipt_present"]:
            raise RuntimeError("ACTUAL_PERSONAL_TARGET_NOT_IDLE")
        actual_schema = actual.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_events'").fetchone()[0]
    finally:
        actual.close()
    backup_sqlite(DATABASE_PATH, probe_path)
    probe_identity = "schema-probe-" + uuid.uuid4().hex
    writable = sqlite3.connect(str(probe_path), timeout=10)
    try:
        writable.execute("BEGIN IMMEDIATE")
        writable.execute("UPDATE d1_personal_target SET identity=?,path=?,lease_id=NULL,receipt=NULL WHERE profile=?",
            (probe_identity, str(probe_path.absolute()), PROFILE))
        writable.commit()
    finally:
        writable.close()
    backup_sha = backup_sqlite(probe_path, backup_path)
    upgrade = migrate_personal_policy_event_schema(probe_path, expected_identity=probe_identity,
        expected_marker_path=probe_path.absolute(), backup_path=backup_path)
    event_result = insert_probe_event(probe_path)
    result = {"mode": "ISOLATED_OLD_SCHEMA_COPY", "actual_database_modified": False,
        "actual_database_identity": source_marker["identity"],
        "actual_old_schema_sha256": hashlib.sha256(actual_schema.encode()).hexdigest(),
        "probe_identity": probe_identity, "probe_backup_sha256": backup_sha,
        "upgrade": upgrade, "event_write_readback": event_result,
        "probe_database_removed_after_verification": False}
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


def run_upgrade() -> dict:
    db_dir = ROUND_ROOT / "database"
    backup_path = db_dir / "personal-dev-before-upgrade.sqlite3"
    output = db_dir / "personal-dev-schema-upgrade-r3.json"
    if output.exists() or backup_path.exists():
        raise RuntimeError("DATABASE_UPGRADE_OUTPUT_ALREADY_EXISTS")
    if not DATABASE_PATH.is_file() or DATABASE_PATH.is_symlink() or DATABASE_PATH.resolve() != DATABASE_PATH.absolute():
        raise RuntimeError("PERSONAL_DATABASE_PATH_NOT_EXACT")
    before_counts = table_counts(DATABASE_PATH)
    before_sha = file_sha256(DATABASE_PATH)
    before = open_readonly(DATABASE_PATH)
    try:
        marker = marker_summary(before)
        schema_row = before.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_events'").fetchone()
        before_sql = schema_row[0] if schema_row else None
        integrity_before = before.execute("PRAGMA integrity_check").fetchone()[0]
        foreign_keys_before = before.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        before.close()
    if (marker["profile"] != PROFILE or marker["path"] != str(DATABASE_PATH.absolute()) or
            marker["lease_active"] or marker["receipt_present"] or integrity_before != "ok" or
            foreign_keys_before):
        raise RuntimeError("PERSONAL_DATABASE_UPGRADE_PRECONDITION_FAILED")
    backup_sha = backup_sqlite(DATABASE_PATH, backup_path)
    try:
        upgrade = migrate_personal_policy_event_schema(DATABASE_PATH,
            expected_identity=marker["identity"], expected_marker_path=DATABASE_PATH.absolute(),
            backup_path=backup_path)
        after_counts = table_counts(DATABASE_PATH)
        after_sha = file_sha256(DATABASE_PATH)
        after = open_readonly(DATABASE_PATH)
        try:
            after_marker = marker_summary(after)
            after_sql = after.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='run_events'").fetchone()[0]
            integrity_after = after.execute("PRAGMA integrity_check").fetchone()[0]
            foreign_keys_after = after.execute("PRAGMA foreign_key_check").fetchall()
            new_events_count = sum(after_sql.find("'" + event_name + "'") >= 0
                for event_name in ("policy_retrieval_started", "policy_retrieval_finished"))
        finally:
            after.close()
        if (after_counts != before_counts or after_marker != marker or integrity_after != "ok" or
                foreign_keys_after or new_events_count != 2):
            raise RuntimeError("PERSONAL_DATABASE_POST_UPGRADE_VERIFICATION_FAILED")
        result = {"mode": "ACTUAL_PERSONAL_DATABASE_UPGRADE", "status": "UPGRADED",
            "database_path": str(DATABASE_PATH.absolute()), "profile": marker["profile"],
            "database_identity": marker["identity"], "backup_path": str(backup_path.relative_to(ROOT)),
            "backup_sha256": backup_sha, "database_sha256_before": before_sha,
            "database_sha256_after": after_sha, "table_row_counts_before": before_counts,
            "table_row_counts_after": after_counts, "marker_unchanged": True,
            "before_schema_sql": before_sql, "after_schema_sql": after_sql,
            "integrity_before": integrity_before, "integrity_after": integrity_after,
            "foreign_key_check_before": "PASS", "foreign_key_check_after": "PASS",
            "policy_event_values_accepted_by_check": new_events_count == 2,
            "no_probe_event_written_to_actual_database": True, "upgrade": upgrade,
            "verified_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    except Exception as exc:
        result = {"mode": "ACTUAL_PERSONAL_DATABASE_UPGRADE", "status": "FAILED",
            "database_path": str(DATABASE_PATH.absolute()), "database_identity": marker["identity"],
            "backup_path": str(backup_path.relative_to(ROOT)), "backup_sha256": backup_sha,
            "database_sha256_before": before_sha, "database_sha256_after": file_sha256(DATABASE_PATH),
            "error_type": type(exc).__name__, "error": str(exc)}
        with output.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        raise
    with output.open("x", encoding="utf-8", newline="\n") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--probe-only", action="store_true")
    group.add_argument("--apply-personal-upgrade", action="store_true")
    args = parser.parse_args()
    print(json.dumps(run_probe() if args.probe_only else run_upgrade(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
