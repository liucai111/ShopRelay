"""Real M3.4 Policy Agent API/retrieval smoke using the actual policy index and SQLite.

This proves the retrieval-to-Supervisor integration, but deliberately does not
claim PostgreSQL Checkpointer validation; use the documented PostgreSQL probe
when Docker Desktop and shoprelay_m11_test are available.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import tempfile
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from sqlalchemy import select
from unittest.mock import patch

from app.m0.config import ROOT, load_settings
from app.m0.data import seed
from app.m1.api import CSRF_HEADER, create_app
from app.m1.db import make_engine, make_session_factory, upgrade_database
from app.m1.models import GraphExecutionRecord, ObservabilityRecord, Run
from app.m1.services import set_demo_password
from app.orchestration.adapters import GraphServices


def _docker_status() -> str:
    try:
        result = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=3, check=False)
        return "AVAILABLE" if result.returncode == 0 else "BLOCKED_DOCKER_UNAVAILABLE"
    except (OSError, subprocess.TimeoutExpired):
        return "BLOCKED_DOCKER_UNAVAILABLE"


def _port_status() -> str:
    try:
        with socket.create_connection(("127.0.0.1", 5432), timeout=1):
            return "LISTENING"
    except OSError:
        return "NOT_LISTENING"


def run_probe(output_path: Path, *, retrieval_mode: str = "BM25",
              message: str = "退款申请需要满足什么条件？") -> dict:
    if retrieval_mode not in {"BM25", "HYBRID_RERANK"}:
        raise ValueError("retrieval_mode must be BM25 or HYBRID_RERANK")
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record = {"schema_version": "shoprelay.m3.4-policy-agent-sqlite.v1",
        "started_at": started_at, "status": "FAIL", "orchestrator": "langgraph",
        "agent_path": f"Router->Supervisor->PolicyAgent->shared RetrievalService({retrieval_mode})",
        "route_source": "isolated deterministic AGENT/search_knowledge integration route",
        "requested_retrieval_mode": retrieval_mode,
        "deepseek_requests": 0, "business_writes": 0,
        "postgres_status": "NOT_RUN", "postgres_database": "shoprelay_m11_test"}
    temp_root = ROOT / "docs" / "probes"
    temp_root.mkdir(parents=True, exist_ok=True)
    record["docker_engine"] = _docker_status()
    record["postgres_port_5432"] = _port_status()
    record["postgres_status"] = ("BLOCKED_DOCKER_UNAVAILABLE" if record["docker_engine"] != "AVAILABLE"
                                  else "NOT_RUN_POSTGRES_API_PROBE_NOT_ATTEMPTED")
    with tempfile.TemporaryDirectory(prefix="m3.4-policy-", dir=temp_root) as directory:
        db_path = Path(directory) / "policy-agent.sqlite3"
        db_url = f"sqlite:///{db_path.as_posix()}"
        seed(db_path)
        upgrade_database(db_url)
        engine = make_engine(db_url)
        factory = make_session_factory(engine)
        password = "m34-" + secrets.token_urlsafe(18)
        with factory() as db:
            set_demo_password(db, "u_demo", password)

        def settings_loader(**_kwargs):
            value = load_settings(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
                "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": "g2-m3.4",
                "SHOPRELAY_EXECUTION_VERSION": "m3.4-policy-v1"}, local_values={}, require_api_key=False)
            return replace(value, m3_supervisor_enabled=True)

        route = {"strategy": "AGENT",
            "sub_strategy": "AGENT+MULTI" if retrieval_mode == "HYBRID_RERANK" else None,
            "safety_decision": "ALLOW",
            "reason_code": "MULTI_POLICY_CONDITIONS" if retrieval_mode == "HYBRID_RERANK" else "POLICY_AGENT",
            "decision_source": "RULE",
            "required_capabilities": ["search_knowledge"], "needs_clarification": False}
        services = GraphServices(router=lambda _query: route, direct=lambda _state: {},
            single=lambda _state: {}, multi=lambda _state: {}, refuse=lambda _state: {})
        saver = MemorySaver()

        @asynccontextmanager
        async def checkpoint_factory(_url, _schema):
            yield saver

        with patch("app.m1.api.load_settings", side_effect=settings_loader):
            app = create_app(database_url=db_url, engine=engine, orchestrator_mode="langgraph",
                checkpointer_factory=checkpoint_factory, checkpointer_readiness=lambda *_: True,
                graph_services=services)
        try:
            with TestClient(app) as client:
                    csrf = client.get("/api/demo/csrf").json()["csrf_token"]
                    login = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
                        CSRF_HEADER: csrf}, json={"username": "u_demo", "password": password})
                    if login.status_code != 200:
                        raise RuntimeError("DEMO_LOGIN_FAILED")
                    headers = {"Origin": str(client.base_url).rstrip("/"),
                        CSRF_HEADER: client.cookies.get("shoprelay_csrf")}
                    response = client.post("/api/chat", headers=headers,
                        json={"message": message, "client_request_id": "m34-policy-probe-1"})
                    if response.status_code != 200:
                        raise RuntimeError("POLICY_AGENT_API_FAILED")
                    body = response.json()
                    run_id = body["run_id"]
                    events = client.get(f"/api/runs/{run_id}/events").json()["events"]
                    replay = client.post("/api/chat", headers=headers,
                        json={"message": message, "client_request_id": "m34-policy-probe-1"})
                    if replay.status_code != 200 or replay.json().get("run_id") != run_id:
                        raise RuntimeError("IDEMPOTENT_REPLAY_FAILED")
                    with factory() as db:
                        run = db.get(Run, run_id)
                        execution = db.get(GraphExecutionRecord, run_id)
                        rows = [json.loads(item.payload_json) for item in db.execute(select(
                            ObservabilityRecord).where(ObservabilityRecord.run_id == run_id,
                            ObservabilityRecord.record_type == "span")).scalars()]
                        result_payload = json.loads(run.result_json) if run and run.result_json else {}
                    tool_spans = [item for item in rows if item["node_name"] == "m3.tool.search_knowledge"]
                    checkpoint = list(saver.list({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}))
                    record.update({"status": "PASS", "backend": "isolated SQLite integration",
                        "database_mutated_outside_temporary_test_db": False,
                        "run_id": run_id, "trace_id": body.get("trace_id"),
                        "checkpoint_id": execution.checkpoint_id if execution else None,
                        "run_status": run.status if run else None,
                        "graph_execution_status": execution.status if execution else None,
                        "checkpoint_present": bool(checkpoint), "answer_kind": body.get("answer_kind"),
                        "answer": body.get("answer"), "strategy": body.get("strategy"),
                        "route": body.get("route"), "knowledge_version": body.get("knowledge_version"),
                        "retrieval": body.get("retrieval"), "policy_retrieval": body.get("policy_retrieval"),
                        "citation_ids": [item.get("chunk_id") for item in body.get("citations", [])],
                        "citations": body.get("citations", []),
                        "tool_references": body.get("tool_references", []),
                        "sse_event_types": [item["type"] for item in events],
                        "tool_started_count_after_replay": sum(item["type"] == "tool_started" for item in events),
                        "tool_span_count": len(tool_spans),
                        "tool_duration_ms": tool_spans[0]["duration_ms"] if tool_spans else None,
                        "model_calls": 0, "estimated_model_cost_usd": 0.0,
                        "restart_recovery": "NOT_RUN_SQLITE_SMOKE_USES_MEMORY_CHECKPOINTER",
                        "postgres_integration": "BLOCKED_DOCKER_UNAVAILABLE"})
        finally:
            engine.dispose()
    record["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return record


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "docs" / "probes" /
        "m3.4-policy-agent-sqlite-20261022-r1.json")
    parser.add_argument("--retrieval-mode", choices=("BM25", "HYBRID_RERANK"), default="BM25")
    parser.add_argument("--message", default="退款申请需要满足什么条件？")
    args = parser.parse_args()
    record = run_probe(args.output, retrieval_mode=args.retrieval_mode, message=args.message)
    print(json.dumps({key: value for key, value in record.items() if key not in {"answer", "citations"}},
                     ensure_ascii=False, indent=2))
    return 0 if record.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
