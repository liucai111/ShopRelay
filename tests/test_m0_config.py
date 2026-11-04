from pathlib import Path
import secrets

import pytest

from app.m0.config import load_settings, parse_env_file


def test_process_environment_precedes_local_file_and_reports_source():
    settings = load_settings(environ={"DEEPSEEK_API_KEY": "process-secret", "DEEPSEEK_MODEL": "process-model"},
                             local_values={"DEEPSEEK_API_KEY": "file-secret", "DEEPSEEK_MODEL": "file-model"})
    assert settings.api_key == "process-secret"
    assert settings.api_key_source == "process_environment"
    assert settings.model == "process-model"
    assert settings.model_source == "process_environment"
    assert "process-secret" not in repr(settings)


def test_explicit_empty_process_secret_does_not_fall_back():
    with pytest.raises(RuntimeError, match="Git-ignored .env.m0.local"):
        load_settings(environ={"DEEPSEEK_API_KEY": ""}, local_values={"DEEPSEEK_API_KEY": "local-secret"})


def test_local_fallback_default_and_node_scope():
    settings = load_settings(environ={}, local_values={"DEEPSEEK_API_KEY": "local-secret"})
    assert settings.api_key_source == ".env.m0.local"
    assert settings.model == "deepseek-flash"
    assert settings.nodes["policy_answer"].scope == "implemented_m0_baseline"
    assert settings.nodes["policy_answer"].locked
    assert settings.nodes["route"].scope == "implemented_m2_1"
    assert settings.nodes["route"].thinking == "disabled"
    assert settings.nodes["route"].max_tokens == 384
    assert settings.nodes["route"].timeout_seconds == 10
    assert settings.nodes["route"].max_cost_usd == 0.01
    assert settings.nodes["route_diagnostic"].scope == "diagnostic_only"
    assert not settings.nodes["route_diagnostic"].locked


def test_env_parser_handles_comments_quotes_and_values_with_equals():
    assert parse_env_file('# comment\nA="x=y"\nB=plain') == {"A": "x=y", "B": "plain"}


def test_invalid_node_parameters_rejected():
    with pytest.raises(ValueError, match="invalid DeepSeek configuration"):
        load_settings(environ={"DEEPSEEK_API_KEY": "secret"},
                      local_values={"SHOPRELAY_DEEPSEEK_POLICY_ANSWER_TIMEOUT_SECONDS": "0"})


def test_api_runtime_settings_follow_process_then_local_priority():
    settings = load_settings(environ={"SHOPRELAY_ALLOWED_ORIGINS": "https://api.example", "SHOPRELAY_COOKIE_SECURE": "true"},
                             local_values={"SHOPRELAY_ALLOWED_ORIGINS": "http://localhost:8000", "SHOPRELAY_COOKIE_SECURE": "false"}, require_api_key=False)
    assert settings.allowed_origins == "https://api.example"
    assert settings.cookie_secure is True
    fallback = load_settings(environ={}, local_values={"SHOPRELAY_ALLOWED_ORIGINS": "http://localhost:8000", "SHOPRELAY_COOKIE_SECURE": "true"}, require_api_key=False)
    assert fallback.allowed_origins == "http://localhost:8000"
    assert fallback.cookie_secure is True


def test_invalid_cookie_secure_setting_rejected():
    with pytest.raises(ValueError, match="SHOPRELAY_COOKIE_SECURE"):
        load_settings(environ={}, local_values={"SHOPRELAY_COOKIE_SECURE": "sometimes"}, require_api_key=False)


def test_database_url_is_not_in_settings_repr():
    marker = secrets.token_urlsafe(24)
    synthetic_url = "postgresql://" + "shoprelay:" + marker + "@localhost/shoprelay"
    settings = load_settings(environ={"DATABASE_URL": synthetic_url}, require_api_key=False)
    assert marker not in repr(settings)
