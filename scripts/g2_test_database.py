"""Secret-safe URL resolver for G2 scripts; always constrained to loopback test PostgreSQL."""
from __future__ import annotations

import os

from sqlalchemy.engine import URL, make_url

from app.m0.config import LOCAL_CONFIG, load_settings, parse_env_file

_TEST_DB = "shoprelay_m11_test"
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}


def resolve_test_database_url(override: str | None = None) -> URL:
    local = parse_env_file(LOCAL_CONFIG.read_text(encoding="utf-8")) if LOCAL_CONFIG.exists() else {}
    explicit = override or os.environ.get("DATABASE_URL", "").strip() or local.get("DATABASE_URL", "").strip()
    if explicit:
        url = make_url(explicit)
        if url.database != _TEST_DB:
            raise ValueError("DATABASE_URL must target only shoprelay_m11_test")
    else:
        settings = load_settings(require_api_key=False)
        if not settings.database_url:
            raise ValueError("no local PostgreSQL credentials are configured")
        url = make_url(settings.database_url)
        if url.database != "shoprelay":
            raise ValueError("implicit test URL derivation only accepts the local ShopRelay database profile")
        url = url.set(database=_TEST_DB)
    if url.drivername not in {"postgresql", "postgresql+psycopg"} or url.host not in _ALLOWED_HOSTS:
        raise ValueError("G2 probe requires PostgreSQL on the local host")
    if "connect_timeout" not in url.query:
        url = url.update_query_dict({"connect_timeout": "3"})
    return url
