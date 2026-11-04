"""Non-destructive ShopRelay runtime preflight; never prints credentials or DSNs."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from urllib.request import urlopen

from sqlalchemy import create_engine, text
from sqlalchemy.engine import URL, make_url

ROOT = Path(__file__).resolve().parents[1]


def safe_database_target(url: URL) -> dict[str, object]:
    """Return connection identity fields without query, password, or full DSN."""
    return {
        "driver": url.drivername,
        "host": url.host or ("local" if url.drivername.startswith("sqlite") else None),
        "port": url.port,
        "database": Path(url.database).name if url.drivername.startswith("sqlite") and url.database else url.database,
        "account": url.username,
    }


def validate_database_target(mode: str, url: URL) -> None:
    if mode == "integration":
        if url.get_backend_name() != "postgresql" or url.database != "shoprelay_m11_test":
            raise ValueError("integration mode is restricted to PostgreSQL database shoprelay_m11_test")
        if url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("integration mode requires a loopback PostgreSQL host")
        return
    if url.get_backend_name() == "postgresql":
        if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("demo mode refuses non-isolated PostgreSQL targets")
        return
    if url.get_backend_name() != "sqlite":
        raise ValueError("demo mode supports the local SQLite demo database or isolated test PostgreSQL only")
    db_path = Path(url.database or "").resolve()
    expected = (ROOT / "data" / "shoprelay.sqlite3").resolve()
    if db_path != expected:
        raise ValueError("demo mode SQLite target must be data/shoprelay.sqlite3")


def _qdrant_status(settings, *, require_hybrid: bool, runtime_ready: bool = False) -> tuple[str, str]:
    from app.m2.retrieval import IndexRegistry, RERANKER_DIR, SMALL_DIR, config_from_environment

    config = config_from_environment()
    if config.qdrant_mode == "local":
        qdrant_path = ROOT / "data" / "qdrant_retrieval_m2"
        qdrant_status = "ready" if qdrant_path.is_dir() else "missing_local_store"
        detail = "embedded_store_present" if qdrant_path.is_dir() else "embedded_store_missing"
    elif config.qdrant_mode == "service":
        if not config.qdrant_url:
            qdrant_status, detail = "unavailable", "service_url_unset"
        else:
            try:
                endpoint = config.qdrant_url.rstrip("/") + "/readyz"
                with urlopen(endpoint, timeout=3) as response:
                    qdrant_status = "ready" if response.status == 200 else "unavailable"
                    detail = "health_endpoint_ok" if response.status == 200 else "health_endpoint_non_200"
            except Exception as exc:  # avoid leaking URL or connection details
                qdrant_status, detail = "unavailable", type(exc).__name__
    else:
        qdrant_status, detail = "invalid_mode", "unsupported_qdrant_mode"

    if require_hybrid:
        for package in ("torch", "transformers", "sentence_transformers", "qdrant_client"):
            if importlib.util.find_spec(package) is None:
                raise RuntimeError(f"HYBRID dependency unavailable: {package}")
        missing = [name for name, path in (("bge_small", SMALL_DIR / "modules.json"),
                                           ("reranker", RERANKER_DIR / "model.safetensors"))
                   if not path.is_file()]
        if missing:
            raise RuntimeError("HYBRID cached model files missing: " + ",".join(missing))
        if qdrant_status != "ready":
            raise RuntimeError("HYBRID Qdrant dependency unavailable: " + detail)
        if runtime_ready and config.qdrant_mode == "local":
            # The running app owns the embedded Qdrant folder's exclusive lock.
            # Startup did the exact collection/count check before opening it.
            detail = "runtime_ready;embedded_store_opened_by_application;collection_check_not_repeated"
        else:
            from qdrant_client import QdrantClient
            index_path = ROOT / "data" / "index.json"
            index, fingerprint = IndexRegistry().current(index_path)
            client = (QdrantClient(url=config.qdrant_url, timeout=3) if config.qdrant_mode == "service"
                      else QdrantClient(path=str(ROOT / "data" / "qdrant_retrieval_m2"), timeout=3))
            try:
                collection = "sr_" + fingerprint[:24]
                if not client.collection_exists(collection):
                    raise RuntimeError("HYBRID active Qdrant collection is missing")
                count = client.count(collection, exact=True).count
                if count != len(index.get("chunks", [])):
                    raise RuntimeError("HYBRID Qdrant collection count does not match active index")
                detail = f"active_collection_ready;points={count}"
            finally:
                client.close()
    return qdrant_status, detail


def run_check(mode: str, *, require_hybrid: bool = False, runtime_ready: bool = False) -> dict[str, object]:
    from app.m0.config import ROOT as APP_ROOT, load_settings
    from app.m1.db import LATEST_SCHEMA_REVISION
    from scripts.g2_test_database import resolve_test_database_url

    settings = load_settings(require_api_key=False)
    if mode == "integration":
        # Reuse the repository's test-only resolver. It either accepts an explicit
        # shoprelay_m11_test URL or safely derives that database from the local
        # ShopRelay profile; it never connects to the default shoprelay database.
        url = resolve_test_database_url()
        config_source = "g2_test_database_resolver"
    else:
        database_url = settings.database_url or f"sqlite:///{(APP_ROOT / 'data' / 'shoprelay.sqlite3').as_posix()}"
        url = make_url(database_url)
        config_source = settings.database_url_source or "local_default"
    validate_database_target(mode, url)
    result: dict[str, object] = {
        "mode": mode,
        "database_config_source": config_source,
        "database_target": safe_database_target(url),
        "orchestrator": settings.orchestrator if mode == "demo" else "langgraph (startup override)",
        "supervisor_enabled": settings.m3_supervisor_enabled if mode == "demo" else "true (startup override)",
        "admin_allowlist_configured": bool(settings.observability_admin_user_ids) if mode == "demo" else "cleared unless explicitly supplied",
        "provider_key_configured": bool(settings.api_key),
        "deepseek_requests_made": 0,
    }
    engine = create_engine(url, pool_pre_ping=True)
    try:
        with engine.connect() as connection:
            if url.get_backend_name() == "postgresql":
                actual_db = connection.execute(text("select current_database()")).scalar_one()
                version = connection.execute(text("select current_setting('server_version')")).scalar_one()
                revision = connection.execute(text("select version_num from alembic_version")).scalar_one_or_none()
                if actual_db != "shoprelay_m11_test":
                    raise RuntimeError("connected PostgreSQL database identity mismatch")
                if revision != LATEST_SCHEMA_REVISION:
                    raise RuntimeError("PostgreSQL Alembic revision is not the application head")
                result["postgres"] = {"status": "ready", "version": version, "database": actual_db,
                                      "alembic_revision": revision}
                if mode == "integration":
                    schema = settings.checkpoint_schema
                    rows = connection.execute(text(
                        "select table_name from information_schema.tables where table_schema=:schema "
                        "and table_name in ('checkpoints','checkpoint_blobs','checkpoint_writes')"),
                        {"schema": schema}).scalars().all()
                    ready = set(rows) == {"checkpoints", "checkpoint_blobs", "checkpoint_writes"}
                    result["checkpointer"] = {"status": "ready" if ready else "not_ready", "schema": schema}
                    if not ready:
                        raise RuntimeError("LangGraph PostgreSQL Checkpointer tables are not ready")
            else:
                connection.execute(text("select 1"))
                result["database"] = {"status": "ready", "kind": "local SQLite demo"}
                revision = connection.execute(text("select version_num from alembic_version")).scalar_one_or_none()
                if revision != LATEST_SCHEMA_REVISION:
                    raise RuntimeError("SQLite demo database is not at the application Alembic revision")
                result["alembic_revision"] = revision
    finally:
        engine.dispose()

    index_path = APP_ROOT / "data" / "index.json"
    if not index_path.is_file():
        raise RuntimeError("policy index data/index.json is missing; preflight does not build or replace indexes")
    result["policy_index"] = {"status": "present", "path": "data/index.json"}
    qdrant_status, qdrant_detail = _qdrant_status(settings, require_hybrid=require_hybrid, runtime_ready=runtime_ready)
    result["qdrant"] = {"status": qdrant_status, "detail": qdrant_detail}
    if mode == "integration":
        # The distribution is named langgraph-checkpoint-postgres, but the
        # import package is nested under langgraph.checkpoint.postgres.
        for package in ("langgraph.checkpoint.postgres", "psycopg_pool"):
            if importlib.util.find_spec(package) is None:
                raise RuntimeError("LangGraph Checkpointer dependency unavailable: " + package)
    if require_hybrid:
        result["hybrid"] = {"status": "dependencies_and_cached_models_present", "model_downloads": 0}
    return result


def describe_config() -> dict[str, object]:
    """Safe diagnostic for configuration provenance; never contacts the database."""
    return _describe_mode_config("demo")


def _describe_mode_config(mode: str) -> dict[str, object]:
    from app.m0.config import ROOT as APP_ROOT, load_settings

    settings = load_settings(require_api_key=False)
    if mode == "integration":
        from scripts.g2_test_database import resolve_test_database_url
        url = resolve_test_database_url()
        source = "g2_test_database_resolver"
    else:
        database_url = settings.database_url or f"sqlite:///{(APP_ROOT / 'data' / 'shoprelay.sqlite3').as_posix()}"
        url = make_url(database_url)
        source = settings.database_url_source or "local_default"
    return {
        "mode": mode,
        "database_config_source": source,
        "database_target": safe_database_target(url),
        "provider_key_configured": bool(settings.api_key),
        "orchestrator_configured": settings.orchestrator,
        "admin_allowlist_configured": bool(settings.observability_admin_user_ids),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("demo", "integration"), default="demo")
    parser.add_argument("--require-hybrid", action="store_true")
    parser.add_argument("--runtime-ready", action="store_true",
                        help="avoid reopening an already-running embedded Qdrant store")
    parser.add_argument("--describe-config", action="store_true", help="print redacted config identity without connecting")
    args = parser.parse_args()
    if args.describe_config:
        try:
            print(json.dumps(_describe_mode_config(args.mode), ensure_ascii=False))
            return 0
        except Exception as exc:
            print(json.dumps({"status": "blocked", "error_type": type(exc).__name__,
                              "reason": "local configuration could not be parsed"}, ensure_ascii=False))
            return 1
    try:
        result = run_check(args.mode, require_hybrid=args.require_hybrid, runtime_ready=args.runtime_ready)
    except Exception as exc:
        # Do not print exception strings from database/network clients: they may contain DSNs.
        print(json.dumps({"status": "blocked", "error_type": type(exc).__name__,
                          "reason": str(exc) if isinstance(exc, (ValueError, RuntimeError)) else "dependency connection failed"},
                         ensure_ascii=False))
        return 1
    print(json.dumps({"status": "ready", **result}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
