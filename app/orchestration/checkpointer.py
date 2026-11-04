"""PostgreSQL checkpointer lifecycle and strict serializer policy."""
from __future__ import annotations

import asyncio
import re
import sys
from contextlib import asynccontextmanager
from importlib.metadata import version
from typing import AsyncIterator

from psycopg.conninfo import make_conninfo
from sqlalchemy.engine import make_url

CHECKPOINTER_VERSION = "3.2.0"
PSYCOPG_POOL_VERSION = "3.3.3"
_SCHEMA = re.compile(r"^[a-z][a-z0-9_]{0,62}$")


def require_checkpointer_dependencies() -> None:
    if version("langgraph-checkpoint-postgres") != CHECKPOINTER_VERSION:
        raise RuntimeError("langgraph-checkpoint-postgres version does not match the G2 lock")
    if version("psycopg-pool") != PSYCOPG_POOL_VERSION:
        raise RuntimeError("psycopg-pool version does not match the G2 lock")


def _normalized_url(database_url: str, *, schema: str | None = None) -> str:
    if not database_url:
        raise RuntimeError("PostgreSQL DATABASE_URL is required for LangGraph mode")
    url = make_url(database_url)
    if url.drivername not in {"postgresql", "postgresql+psycopg"}:
        raise RuntimeError("LangGraph mode requires a PostgreSQL DATABASE_URL")
    query = dict(url.query)
    if schema is not None:
        if not _SCHEMA.fullmatch(schema):
            raise ValueError("invalid checkpoint schema name")
        query["options"] = f"-c search_path={schema}"
    base = url.set(drivername="postgresql", query=query).render_as_string(hide_password=False)
    if schema is not None:
        # libpq URI parameters do not decode '+' as a space. Construct a
        # keyword conninfo so Psycopg quotes the search_path option correctly.
        return make_conninfo(base, options=f"-c search_path={schema}")
    return base


def checkpoint_conninfo(database_url: str, schema: str) -> str:
    """Build a private psycopg URI; callers must never log or return it."""
    return _normalized_url(database_url, schema=schema)


def base_conninfo(database_url: str) -> str:
    return _normalized_url(database_url)


def run_checkpointer_async(awaitable):
    """Run a Checkpointer coroutine on a Psycopg-compatible event loop.

    Psycopg's async sockets cannot run on Windows' default Proactor loop. The
    LangGraph API worker is synchronous at its boundary, so use a selector
    loop there while preserving the platform default everywhere else.
    """
    if sys.platform != "win32":
        return asyncio.run(awaitable)
    loop = asyncio.SelectorEventLoop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(awaitable)
    finally:
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
            if hasattr(loop, "shutdown_default_executor"):
                loop.run_until_complete(loop.shutdown_default_executor())
        finally:
            asyncio.set_event_loop(None)
            loop.close()


def strict_serializer():
    from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
    return JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)


@asynccontextmanager
async def open_checkpointer(database_url: str, schema: str) -> AsyncIterator:
    require_checkpointer_dependencies()
    from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

    async with AsyncPostgresSaver.from_conn_string(
        checkpoint_conninfo(database_url, schema), pipeline=False, serde=strict_serializer()
    ) as saver:
        yield saver


async def checkpoint_schema_ready(database_url: str, schema: str) -> bool:
    """Read-only readiness check. This function never creates a schema or saver tables."""
    require_checkpointer_dependencies()
    if not _SCHEMA.fullmatch(schema):
        raise ValueError("invalid checkpoint schema name")
    from psycopg import connect
    from psycopg.rows import dict_row

    conninfo = base_conninfo(database_url)

    def check_tables() -> bool:
        with connect(conninfo, autocommit=True, row_factory=dict_row) as conn:
            row = conn.execute(
                "SELECT to_regclass(%s) AS checkpoints, to_regclass(%s) AS checkpoint_blobs, "
                "to_regclass(%s) AS checkpoint_writes",
                (f"{schema}.checkpoints", f"{schema}.checkpoint_blobs", f"{schema}.checkpoint_writes"),
            ).fetchone()
            return bool(row and all(row.values()))

    try:
        return await asyncio.to_thread(check_tables)
    except Exception as exc:
        raise RuntimeError(f"LangGraph checkpoint database readiness failed ({type(exc).__name__})") from exc
