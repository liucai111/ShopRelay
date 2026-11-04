"""ASGI entry point for the guarded local demo/integration launcher."""
from __future__ import annotations

import os

from sqlalchemy.engine import make_url

from app.m0.config import ROOT, load_settings
from scripts.check_shoprelay_runtime import validate_database_target

_settings = load_settings(require_api_key=False)
_mode = os.getenv("SHOPRELAY_RUNTIME_MODE", "demo").strip().lower()
if _mode not in {"demo", "integration"}:
    raise RuntimeError("SHOPRELAY_RUNTIME_MODE must be demo or integration")
if _mode == "integration":
    from scripts.g2_test_database import resolve_test_database_url
    _database_url = resolve_test_database_url().render_as_string(hide_password=False)
else:
    _database_url = _settings.database_url or f"sqlite:///{(ROOT / 'data' / 'shoprelay.sqlite3').as_posix()}"
_url = make_url(_database_url)
validate_database_target(_mode, _url)

_admin_ids = _settings.observability_admin_user_ids if _mode == "integration" else ()
_enable_test_actions = os.getenv("SHOPRELAY_ENABLE_ISOLATED_TEST_ACTIONS", "false").strip().lower() == "true"
if _enable_test_actions and (_mode != "integration" or _url.database != "shoprelay_m11_test"):
    raise RuntimeError("isolated test actions require integration mode and shoprelay_m11_test")
if _mode == "demo" and (_settings.orchestrator != "legacy" or _settings.m3_supervisor_enabled or _admin_ids):
    raise RuntimeError("demo mode must keep legacy defaults and an empty admin allowlist")

_provider_mode = os.getenv("SHOPRELAY_PROVIDER_MODE", "stub").strip().lower()
if _provider_mode not in {"stub", "configured"}:
    raise RuntimeError("SHOPRELAY_PROVIDER_MODE must be stub or configured")
if _provider_mode == "configured" and not _settings.api_key.strip():
    raise RuntimeError("configured provider mode requires a locally configured provider key")

from app.m1.api import app as _default_app, create_app  # noqa: E402

# api.py exports a default app for ordinary Uvicorn use. This guarded launcher
# creates the selected profile app and releases the unused default engine.
_default_app.state.engine.dispose()

_provider = None
if _provider_mode == "stub":
    from scripts.probe_m5_4_a_browser_server import DeterministicEvidenceStub
    _provider = DeterministicEvidenceStub()

_enable_graph = _mode == "integration"
app = create_app(
    database_url=_database_url,
    orchestrator_mode="langgraph" if _enable_graph else "legacy",
    chat_provider=_provider,
    observability_admin_user_ids=_admin_ids,
    action_writes_enabled=_enable_test_actions,
    simulated_test_actions_enabled=_enable_test_actions,
    return_actions_enabled=_enable_test_actions,
    refund_actions_enabled=_enable_test_actions,
    cancel_actions_enabled=_enable_test_actions,
    ticket_actions_enabled=_enable_test_actions,
)

if _admin_ids:
    from app.m1.models import DemoCredential, User
    with app.state.session_factory() as _db:
        for _user_id in _admin_ids:
            _user = _db.get(User, _user_id)
            _credential = _db.get(DemoCredential, _user_id)
            if _user is None or _credential is None or not _credential.active:
                app.state.engine.dispose()
                raise RuntimeError("configured isolated administrator is absent or disabled in the test database")

print({
    "runtime_profile": _mode,
    "orchestrator": app.state.orchestrator_mode,
    "provider": "deterministic_local_stub" if _provider_mode == "stub" else "configured_remote_provider",
    "database": _url.database,
    "admin_allowlist_count": len(_admin_ids),
    "isolated_simulated_actions_enabled": _enable_test_actions,
    "production_write_enabled": False,
    "mcp_write_enabled": False,
}, flush=True)
