"""Capture secret-free owner-scope HTTP evidence for M5.2-B-Q2."""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select

from app.m1.db import make_engine, make_session_factory
from app.m1.models import Run
from scripts.g2_test_database import resolve_test_database_url


ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8000"


def login(username: str, password: str) -> httpx.Client:
    client = httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)
    csrf = client.get("/api/demo/csrf")
    if csrf.status_code != 200:
        raise RuntimeError("CSRF_BOOTSTRAP_FAILED")
    token = csrf.json().get("csrf_token")
    result = client.post("/api/demo/login", headers={"Origin": BASE, "X-CSRF-Token": token},
                         json={"username": username, "password": password})
    if result.status_code != 200:
        raise RuntimeError(f"TEST_LOGIN_FAILED_{result.status_code}")
    return client


def main() -> None:
    owner_name = os.environ.get("M52BQ2_OWNER_USERNAME", "")
    other_name = os.environ.get("M52BQ2_OTHER_USERNAME", "")
    owner_password = os.environ.get("M52BQ2_OWNER_PASSWORD", "")
    other_password = os.environ.get("M52BQ2_OTHER_PASSWORD", "")
    if not all((owner_name, other_name, owner_password, other_password)):
        raise RuntimeError("two isolated test identities are required")
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Q2 HTTP evidence is restricted to loopback shoprelay_m11_test")
    engine = make_engine(url.render_as_string(hide_password=False))
    factory = make_session_factory(engine)
    with factory() as db:
        run = db.execute(select(Run).where(Run.user_id == owner_name, Run.status == "SUCCEEDED")
                         .order_by(Run.created_at.desc())).scalars().first()
        if run is None:
            raise RuntimeError("SUCCESSFUL_OWNER_RUN_NOT_FOUND")
        run_id, session_id = run.run_id, run.session_id

    owner = login(owner_name, owner_password)
    other = login(other_name, other_password)
    anonymous = httpx.Client(base_url=BASE, timeout=15, follow_redirects=False)
    result: dict = {"schema_version": "shoprelay.m5.2-b-q2-owner-http.v1",
        "recorded_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "database": url.database, "http_origin": BASE, "run_id": run_id, "session_id": session_id,
        "credentials_recorded": False, "cookies_recorded": False,
        "resources": {}}
    try:
        paths = {
            "run_execution": f"/api/runs/{run_id}/execution",
            "session_memory": f"/api/sessions/{session_id}/memory",
        }
        clients = {"owner": owner, "other": other, "anonymous": anonymous}
        for resource, path in paths.items():
            row = {}
            for principal, client in clients.items():
                response = client.get(path)
                body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                row[principal] = {"http_status": response.status_code,
                    "trace_id_present": isinstance(body.get("trace_id"), str),
                    "body_keys": sorted(body.keys())}
                if principal == "owner" and response.status_code == 200:
                    projection = body.get("execution" if resource == "run_execution" else "memory", {})
                    if resource == "run_execution":
                        row[principal]["projection"] = {
                            "run_id": projection.get("run_id"), "session_id": projection.get("session_id"),
                            "status": projection.get("status"), "strategy": projection.get("strategy"),
                            "sub_strategy": projection.get("sub_strategy"),
                            "retrieval_strategy_selected": (projection.get("retrieval") or {}).get("strategy_selected"),
                            "retrieval_strategy_executed": (projection.get("retrieval") or {}).get("strategy_executed"),
                            "degraded": (projection.get("retrieval") or {}).get("degraded"),
                            "citation_ids": [item.get("chunk_id") for item in projection.get("citations", [])],
                            "evidence_ids": [item.get("chunk_id") for item in projection.get("retrieved_evidence", [])],
                            "span_count": len(projection.get("spans", [])),
                            "trace_event_types": [item.get("event_type") for item in projection.get("trace_events", [])],
                        }
                    else:
                        row[principal]["projection"] = {key: projection.get(key) for key in (
                            "session_id", "status", "summary_version", "summary_persisted",
                            "summary_content_exposed", "source_message_count", "source_through_sequence",
                            "estimated_tokens_before", "estimated_tokens_after", "effective_context_tokens",
                            "level", "thresholds", "last_run_id", "last_run_status",
                            "token_estimates_are_heuristic")}
            result["resources"][resource] = row

        expected = {"run_execution": {"owner": 200, "other": 404, "anonymous": 401},
                    "session_memory": {"owner": 200, "other": 404, "anonymous": 401}}
        result["all_expected_statuses"] = all(
            result["resources"][name][principal]["http_status"] == status
            for name, principals in expected.items() for principal, status in principals.items())
        memory_owner = result["resources"]["session_memory"]["owner"].get("projection", {})
        result["summary_body_exposed"] = memory_owner.get("summary_content_exposed", False) is not False
        out = ROOT / "docs" / "probes" / "m5.2-b-q2-owner-http-r1.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    finally:
        owner.close(); other.close(); anonymous.close(); engine.dispose()


if __name__ == "__main__":
    main()
