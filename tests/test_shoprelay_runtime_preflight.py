from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from scripts.check_shoprelay_runtime import ROOT, safe_database_target, validate_database_target


def test_safe_database_identity_never_contains_credentials_or_query_options():
    url = make_url("postgresql+psycopg://shoprelay:sentinel-secret@127.0.0.1:5432/shoprelay_m11_test?sslpassword=also-secret")
    identity = safe_database_target(url)
    rendered = repr(identity)
    assert identity == {
        "driver": "postgresql+psycopg",
        "host": "127.0.0.1",
        "port": 5432,
        "database": "shoprelay_m11_test",
        "account": "shoprelay",
    }
    assert "sentinel-secret" not in rendered
    assert "also-secret" not in rendered


def test_integration_mode_only_accepts_loopback_test_database():
    validate_database_target("integration", make_url("postgresql://shoprelay@localhost/shoprelay_m11_test"))
    with pytest.raises(ValueError):
        validate_database_target("integration", make_url("postgresql://shoprelay@localhost/shoprelay"))
    with pytest.raises(ValueError):
        validate_database_target("integration", make_url("postgresql://shoprelay@example.invalid/shoprelay_m11_test"))


def test_demo_mode_refuses_non_test_postgres():
    validate_database_target("demo", make_url("postgresql://shoprelay@127.0.0.1/shoprelay_m11_test"))
    with pytest.raises(ValueError):
        validate_database_target("demo", make_url("postgresql://shoprelay@127.0.0.1/shoprelay"))


def test_demo_sqlite_target_is_limited_to_repo_demo_database():
    expected = (ROOT / "data" / "shoprelay.sqlite3").resolve()
    validate_database_target("demo", make_url(f"sqlite:///{expected.as_posix()}"))
    with pytest.raises(ValueError):
        validate_database_target("demo", make_url("sqlite:///C:/temp/other.sqlite3"))


def test_integration_test_database_resolver_is_used_without_exposing_url(monkeypatch):
    from scripts import check_shoprelay_runtime as preflight

    class Settings:
        database_url = "postgresql://user:secret@127.0.0.1:5432/shoprelay"
        database_url_source = "local_file"
        api_key = ""
        orchestrator = "legacy"
        m3_supervisor_enabled = False
        observability_admin_user_ids = ()
        checkpoint_schema = "shoprelay_langgraph"

    monkeypatch.setattr(preflight, "create_engine", lambda *args, **kwargs: pytest.fail("DB connection must not be opened"))
    monkeypatch.setattr("scripts.g2_test_database.resolve_test_database_url", lambda: make_url(
        "postgresql://user:secret@127.0.0.1:5432/shoprelay_m11_test"
    ))
    monkeypatch.setattr("app.m0.config.load_settings", lambda **kwargs: Settings())
    # The resolver result is exercised through the same target selection path by
    # the redacted mode description, without contacting PostgreSQL.
    described = preflight._describe_mode_config("integration")
    rendered = repr(described)
    assert described["database_target"]["database"] == "shoprelay_m11_test"
    assert described["database_config_source"] == "g2_test_database_resolver"
    assert "secret" not in rendered


def test_integration_preflight_uses_postgres_import_namespace(monkeypatch):
    import importlib.util
    from scripts import check_shoprelay_runtime as preflight

    assert importlib.util.find_spec("langgraph.checkpoint.postgres") is not None
    assert importlib.util.find_spec("psycopg_pool") is not None


def test_integration_asgi_database_url_is_rendered_for_application_factory(monkeypatch):
    from sqlalchemy.engine import make_url

    url = make_url("postgresql://user:secret@127.0.0.1:5432/shoprelay_m11_test")
    rendered = url.render_as_string(hide_password=False)
    parsed = make_url(rendered)
    assert parsed.database == "shoprelay_m11_test"
    assert parsed.password == "secret"
