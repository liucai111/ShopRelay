"""Isolated PostgreSQL concurrency, uniqueness and rollback probe."""
from __future__ import annotations

import concurrent.futures
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run() -> dict:
    from app.m0.config import load_settings
    try:
        url = load_settings(require_api_key=False).database_url
    except Exception as exc:
        return {"status": "blocked", "reason": f"configuration unavailable: {type(exc).__name__}"}
    if not url:
        return {"status": "blocked", "reason": "DATABASE_URL and local PostgreSQL password absent; no connection attempted"}
    try:
        import psycopg
        from psycopg import sql
    except ImportError:
        return {"status": "blocked", "reason": "psycopg absent"}
    table = "shoprelay_m0_probe_" + uuid.uuid4().hex[:12]
    identifier = sql.Identifier(table)
    try:
        with psycopg.connect(url, connect_timeout=4) as conn:
            server_version = conn.execute("SHOW server_version").fetchone()[0]
            conn.execute(sql.SQL("CREATE TABLE {} (request_id text PRIMARY KEY, order_id text NOT NULL, reason text NOT NULL, status text NOT NULL)").format(identifier))
            conn.execute(sql.SQL("CREATE UNIQUE INDEX {} ON {} (order_id) WHERE status IN ('SUBMITTED','UNDER_REVIEW','APPROVED')").format(sql.Identifier(table + "_open"), identifier))
        def insert(request_id: str, reason: str) -> str:
            try:
                with psycopg.connect(url, connect_timeout=4) as connection:
                    connection.execute(sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s)").format(identifier), (request_id, "ORDER-ONE", reason, "SUBMITTED"))
                return "committed"
            except psycopg.errors.UniqueViolation:
                return "unique_violation"
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(lambda pair: insert(*pair), [("R1", "reason-a"), ("R2", "reason-b")]))
        committed_request_id = ("R1", "R2")[outcomes.index("committed")]
        try:
            with psycopg.connect(url, connect_timeout=4) as conn:
                conn.execute(
                    sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s)").format(identifier),
                    (committed_request_id, "ORDER-IDEMPOTENCY", "duplicate-key-test", "SUBMITTED"),
                )
            idempotency_key_conflict = False
        except psycopg.errors.UniqueViolation:
            idempotency_key_conflict = True
        try:
            with psycopg.connect(url, connect_timeout=4) as conn:
                conn.execute(sql.SQL("INSERT INTO {} VALUES (%s,%s,%s,%s)").format(identifier), ("ROLLBACK", "ORDER-TWO", "rollback-test", "SUBMITTED"))
                raise RuntimeError("force rollback")
        except RuntimeError:
            pass
        with psycopg.connect(url, connect_timeout=4) as conn:
            count = conn.execute(sql.SQL("SELECT count(*) FROM {}").format(identifier)).fetchone()[0]
        passed = sorted(outcomes) == ["committed", "unique_violation"] and idempotency_key_conflict and count == 1
        return {"status": "ok" if passed else "failed", "concurrent_outcomes": outcomes, "idempotency_key_conflict": idempotency_key_conflict, "rows_after_rollback": count, "postgres_server_version": server_version, "psycopg_version": psycopg.__version__}
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__}
    finally:
        try:
            with psycopg.connect(url, connect_timeout=4) as conn:
                conn.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(identifier))
        except Exception:
            pass


def main() -> None:
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "probe": "postgres_m0", "result": run()}
    path = ROOT / "docs/probes" / f"postgres-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(json.dumps({"path": str(path), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
