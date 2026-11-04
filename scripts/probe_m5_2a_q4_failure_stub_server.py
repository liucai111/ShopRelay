"""Run a loopback-only FastAPI instance with deterministic provider failure injection.

This harness has no HTTP fault-injection route. It refuses databases other than
shoprelay_m11_test and is intended only for the M5.2-A-Q4 browser probe.
"""
from __future__ import annotations

import json
import os
import subprocess

from sqlalchemy.engine import URL, make_url
from uvicorn import run

from app.m1.api import create_app
from app.m1.chat import ChatFailure
from app.m1.db import make_engine, make_session_factory
from app.m1.models import User
from app.m1.services import set_demo_password

USER_ID = "m52aq4_browser_user"
USER_DISPLAY = "M5.2-A-Q4隔离浏览器验收"


def database_url() -> str:
    raw = subprocess.check_output(
        ["docker", "inspect", "--format", "{{json .Config.Env}}", "shoprelay-m0-postgres-1"],
        text=True,
    )
    env = dict(item.split("=", 1) for item in json.loads(raw) if "=" in item)
    if env.get("POSTGRES_USER") != "shoprelay" or not env.get("POSTGRES_PASSWORD"):
        raise RuntimeError("existing ShopRelay PostgreSQL credentials unavailable")
    return URL.create(
        "postgresql+psycopg",
        username="shoprelay",
        password=env["POSTGRES_PASSWORD"],
        host="127.0.0.1",
        port=5432,
        database="shoprelay_m11_test",
    ).render_as_string(hide_password=False)


class Q4FailureProvider:
    """Fail the first policy-answer invocation; never performs network I/O."""

    def __init__(self) -> None:
        self.calls = 0

    def answer(self, question, evidence):
        self.calls += 1
        raise ChatFailure(
            "MODEL_UNAVAILABLE",
            True,
            "模型服务暂不可用；本次没有生成答案。",
            {"provider_exception": "DeterministicQ4StubFailure"},
        )


def main() -> None:
    db_url = database_url()
    parsed = make_url(db_url)
    if parsed.get_backend_name() != "postgresql" or parsed.database != "shoprelay_m11_test":
        raise RuntimeError("Q4 harness is restricted to shoprelay_m11_test")
    password = os.environ.get("M52AQ4_TEST_PASSWORD")
    if not password or len(password) < 16:
        raise RuntimeError("temporary browser credential must be provided via M52AQ4_TEST_PASSWORD")

    engine = make_engine(db_url)
    factory = make_session_factory(engine)
    with factory() as db:
        if db.get(User, USER_ID) is not None:
            raise RuntimeError("refusing to reuse an existing Q4 test account")
        db.add(User(user_id=USER_ID, display_name=USER_DISPLAY))
        db.commit()
        set_demo_password(db, USER_ID, password)

    provider = Q4FailureProvider()
    app = create_app(
        database_url=db_url,
        engine=engine,
        orchestrator_mode="legacy",
        chat_provider=provider,
    )
    try:
        run(app, host="127.0.0.1", port=8000, log_level="warning")
    finally:
        app.state.engine.dispose(close=True)
        print(f"Q4_DETERMINISTIC_PROVIDER_FAILURE_CALLS={provider.calls}")


if __name__ == "__main__":
    main()
