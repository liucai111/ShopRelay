"""Explicit, non-destructive setup for the G2 test checkpointer schema."""
from __future__ import annotations

import argparse

from sqlalchemy.engine import make_url

from app.m0.config import load_settings
from app.orchestration.checkpointer import base_conninfo, open_checkpointer, run_checkpointer_async
from scripts.g2_test_database import resolve_test_database_url


async def setup(database_url: str, schema: str) -> None:
    url = make_url(database_url)
    if url.database != "shoprelay_m11_test":
        raise RuntimeError("setup is restricted to the ShopRelay test database shoprelay_m11_test")
    from psycopg import AsyncConnection, sql
    from psycopg.rows import dict_row

    async with await AsyncConnection.connect(base_conninfo(database_url), autocommit=True, row_factory=dict_row) as conn:
        await conn.execute(sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(sql.Identifier(schema)))
    async with open_checkpointer(database_url, schema) as saver:
        await saver.setup()


def main() -> None:
    parser = argparse.ArgumentParser(description="Explicitly initialize only the ShopRelay G2 test checkpoint schema")
    parser.add_argument("--database-url", help="Test DATABASE_URL; credentials are never printed")
    parser.add_argument("--schema", default=None, help="Defaults to SHOPRELAY_LANGGRAPH_CHECKPOINT_SCHEMA")
    args = parser.parse_args()
    settings = load_settings(require_api_key=False)
    try:
        database_url = resolve_test_database_url(args.database_url).render_as_string(hide_password=False)
    except Exception as exc:
        parser.error(f"isolated test database configuration is invalid ({type(exc).__name__})")
    schema = args.schema or settings.checkpoint_schema
    try:
        run_checkpointer_async(setup(database_url, schema))
    except Exception as exc:
        raise SystemExit(f"Checkpoint setup failed ({type(exc).__name__}); no schema was dropped or recreated") from None
    print(f"Checkpointer setup complete for shoprelay_m11_test schema {schema}.")


if __name__ == "__main__":
    main()
