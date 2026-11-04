"""Isolated M3.1 PostgreSQL Checkpointer smoke; never migrates or initializes schemas.

Only targets the existing loopback ``shoprelay_m11_test`` database. A unique
synthetic run/user pair is removed through exact owner-scoped cleanup after the
checkpoint has been read from a newly opened connection.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select, text
from fastapi.testclient import TestClient

from scripts.g2_test_database import resolve_test_database_url
from scripts.probe_langgraph_g2_postgres import _cleanup_probe_users, _login
from app.m1.api import create_app
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.models import GraphExecutionRecord, ObservabilityRecord, Run, RunEvent, User
from app.m1.routing import RouteDecision
from app.m1.services import set_demo_password
from app.orchestration.adapters import GraphServices
from app.orchestration.checkpointer import open_checkpointer, run_checkpointer_async

ROOT = Path(__file__).resolve().parents[1]
EXPECTED_REVISION = "20261020_05"
DEFAULT_OUTPUT = ROOT / "docs" / "probes" / "m3.1-postgres-checkpoint-20261021-r1.json"


async def _read_checkpoint(database_url: str, schema: str, thread_id: str) -> tuple[dict, str | None]:
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    async with open_checkpointer(database_url, schema) as saver:
        checkpoint = await saver.aget_tuple(config)
    if checkpoint is None:
        raise AssertionError("M3.1 graph checkpoint could not be read after app restart")
    return checkpoint.checkpoint.get("channel_values", {}), checkpoint.config["configurable"].get("checkpoint_id")


async def _delete_checkpoint(database_url: str, schema: str, thread_id: str) -> None:
    async with open_checkpointer(database_url, schema) as saver:
        await saver.adelete_thread(thread_id)


def probe(schema: str) -> dict:
    started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    evidence: dict = {"schema_version": "shoprelay.m3.1-postgres-checkpoint-probe.v1",
        "started_at": started_at, "status": "NOT_RUN", "database": "shoprelay_m11_test",
        "application_revision_expected": EXPECTED_REVISION, "checkpointer_schema": schema,
        "deepseek_requests": 0, "business_tool_calls": 0, "write_executions": 0}
    phase = "configuration"
    database_url = None
    engine = None
    user_ids = ("g2probe_" + secrets.token_hex(10), "g2probe_" + secrets.token_hex(10))
    users_created = False
    run_id = None
    original_env = {key: os.environ.get(key) for key in
        ("SHOPRELAY_ORCHESTRATOR", "SHOPRELAY_M3_SUPERVISOR_ENABLED")}
    try:
        os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
        os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
        url = resolve_test_database_url()
        if url.database != "shoprelay_m11_test":
            raise RuntimeError("UNSAFE_DATABASE_TARGET")
        database_url = url.render_as_string(hide_password=False)
        from app.m0.config import load_settings
        configured_schema = load_settings(require_api_key=False).checkpoint_schema
        if configured_schema != schema:
            raise RuntimeError("CHECKPOINTER_SCHEMA_CONFIGURATION_MISMATCH")
        engine = make_engine(database_url)
        phase = "database_and_existing_schema_check"
        with engine.connect() as connection:
            evidence["postgres_server_version"] = connection.execute(
                text("SELECT current_setting('server_version')")).scalar_one()
            evidence["current_database"] = connection.execute(text("SELECT current_database()")).scalar_one()
        revision = current_revision(database_url)
        if revision != EXPECTED_REVISION:
            raise RuntimeError("APPLICATION_SCHEMA_REVISION_MISMATCH")
        evidence["application_revision"] = revision
        phase = "existing_checkpointer_readiness"
        from app.orchestration.checkpointer import checkpoint_schema_ready
        if not run_checkpointer_async(checkpoint_schema_ready(database_url, schema)):
            raise RuntimeError("CHECKPOINTER_SCHEMA_NOT_READY")

        factory = make_session_factory(engine)
        password = secrets.token_urlsafe(32)
        phase = "isolated_probe_user_setup"
        with factory() as db:
            db.add_all([User(user_id=user_ids[0], display_name="M3.1 Probe A"),
                        User(user_id=user_ids[1], display_name="M3.1 Probe B")])
            db.commit()
            set_demo_password(db, user_ids[0], password)
            set_demo_password(db, user_ids[1], password)
        users_created = True

        route_calls: list[str] = []
        def route(_query: str) -> dict:
            route_calls.append("router")
            return RouteDecision(strategy="AGENT", safety_decision="ALLOW",
                reason_code="LIVE_BUSINESS_FACT", decision_source="RULE",
                intent_labels=["live_business"], required_capabilities=["get_order"]).model_dump(mode="json")
        services = GraphServices(router=route, direct=lambda _state: {}, single=lambda _state: {},
            multi=lambda _state: {}, refuse=lambda _state: {})
        request_id = "m3.1-pg-" + uuid.uuid4().hex

        phase = "m3_api_run_and_graph_checkpoint"
        first_app = create_app(database_url=database_url, engine=engine, chat_provider=object(),
            orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer, graph_services=services)
        with TestClient(first_app) as client:
            headers = _login(client, user_ids[0], password)
            response = client.post("/api/chat", headers=headers,
                json={"message": "Check my current order status", "client_request_id": request_id})
            if response.status_code != 502 or response.json().get("code") != "AGENT_NOT_IMPLEMENTED":
                raise AssertionError("M3.1 default Agent did not fail closed with its stable status")
            run_id = response.json().get("details", {}).get("run_id")
            if not run_id:
                raise AssertionError("failed M3.1 response did not identify the persisted Run")
        evidence["run_id"] = run_id
        evidence["route_invocations_before_replay"] = len(route_calls)

        phase = "run_event_trace_and_checkpoint_metadata"
        with factory() as db:
            run = db.get(Run, run_id)
            graph_run = db.get(GraphExecutionRecord, run_id)
            events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)).scalars().all()
            records = db.execute(select(ObservabilityRecord).where(ObservabilityRecord.run_id == run_id)).scalars().all()
            if run is None or run.status != "FAILED" or run.error_code != "AGENT_NOT_IMPLEMENTED" or run.result_json is not None:
                raise AssertionError("failed M3.1 Run was not durably fail-closed")
            if graph_run is None or graph_run.status != "FAILED" or graph_run.graph_version != "g2-m3.1" or \
               graph_run.state_schema_version != "g2-m3.1-state-v1" or not graph_run.checkpoint_id:
                raise AssertionError("Run and M3.1 graph checkpoint metadata disagree")
            event_types = [item.event_type for item in events]
            if "agent_started" not in event_types or "tool_started" in event_types or "tool_finished" in event_types:
                raise AssertionError("persisted M3.1 events do not match stub-only execution")
            span_nodes = {json.loads(row.payload_json).get("node_name") for row in records
                if row.record_type == "span"}
            if not {"m3.supervisor.plan", "m3.agent.order_logistics"}.issubset(span_nodes):
                raise AssertionError("M3.1 child spans were not persisted")
            evidence.update({"trace_id": events[0].trace_id if events else None, "checkpoint_id": graph_run.checkpoint_id,
                "run_status": run.status, "run_error_code": run.error_code, "result_json_absent": True,
                "state_schema_version": graph_run.state_schema_version, "event_types": event_types,
                "span_nodes": sorted(span_nodes)})

        phase = "application_restart_read_and_owner_isolation"
        engine_after_restart = make_engine(database_url)
        try:
            restarted_app = create_app(database_url=database_url, engine=engine_after_restart,
                chat_provider=object(), orchestrator_mode="langgraph", checkpointer_factory=open_checkpointer,
                graph_services=services)
            with TestClient(restarted_app) as client:
                headers = _login(client, user_ids[0], password)
                run_response = client.get(f"/api/runs/{run_id}")
                replay = client.post("/api/chat", headers=headers,
                    json={"message": "Check my current order status", "client_request_id": request_id})
                if run_response.status_code != 200 or run_response.json()["run"]["status"] != "FAILED":
                    raise AssertionError("application restart did not preserve the failed M3.1 Run")
                if replay.status_code not in {502, 503} or len(route_calls) != 1:
                    raise AssertionError("duplicate request re-executed the M3.1 graph")
            with TestClient(restarted_app) as other_client:
                _login(other_client, user_ids[1], password)
                if other_client.get(f"/api/runs/{run_id}").status_code != 404:
                    raise AssertionError("second user could read the M3.1 Run")
                if other_client.get(f"/api/runs/{run_id}/events?after_seq=0").status_code != 404:
                    raise AssertionError("second user could read the M3.1 events")
        finally:
            engine_after_restart.dispose()
        evidence["application_restart_read"] = True
        evidence["owner_isolation"] = True
        evidence["duplicate_request_route_invocations"] = len(route_calls)

        phase = "postgres_checkpoint_reopen_and_state_validation"
        values, checkpoint_id = run_checkpointer_async(_read_checkpoint(database_url, schema, run_id))
        if values.get("strategy") != "AGENT" or values.get("terminal_status") != "FAILED" or \
           values.get("supervisor_status") != "BLOCKED" or not values.get("handoffs"):
            raise AssertionError("restored PostgreSQL checkpoint omitted M3.1 terminal state or Handoff")
        state_text = repr(values)
        if "Check my current order status" in state_text or "user_id" in state_text or user_ids[0] in state_text:
            raise AssertionError("M3.1 checkpoint contains raw query or user identity")
        if checkpoint_id != evidence["checkpoint_id"]:
            raise AssertionError("reopened checkpoint id does not match Run mapping")
        evidence["postgres_checkpoint_reopened"] = True
        evidence["restored_state_fields"] = sorted(key for key in (
            "business_intent", "current_task", "agent_tasks", "intermediate_results", "handoffs",
            "tool_execution_status", "agent_budget", "supervisor_status", "terminal_status") if key in values)
        evidence["checkpoint_excludes_query_and_identity"] = True
        evidence["status"] = "PASS"
        return evidence
    except Exception as exc:
        evidence["status"] = "FAIL"
        evidence["failure_phase"] = phase
        evidence["failure_type"] = type(exc).__name__
        evidence["failure_code"] = getattr(exc, "code", None) if isinstance(
            getattr(exc, "code", None), str) and str(getattr(exc, "code")).isupper() else "PROBE_ASSERTION_OR_ENVIRONMENT_FAILURE"
        return evidence
    finally:
        if database_url and run_id:
            try:
                run_checkpointer_async(_delete_checkpoint(database_url, schema, run_id))
                evidence["exact_checkpoint_cleanup"] = True
            except Exception:
                evidence["exact_checkpoint_cleanup"] = False
                if evidence.get("status") == "PASS":
                    evidence["status"] = "PASS_WITH_CLEANUP_WARNING"
        if engine is not None and users_created:
            try:
                evidence["probe_user_cleanup_counts"] = _cleanup_probe_users(engine, user_ids)
            except Exception:
                evidence["probe_user_cleanup"] = "FAILED"
                if evidence.get("status", "NOT_RUN").startswith("PASS"):
                    evidence["status"] = "PASS_WITH_CLEANUP_WARNING"
        if engine is not None:
            engine.dispose()
        for key, value in original_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        evidence["finished_at"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", default="shoprelay_langgraph")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = probe(args.schema)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    if args.output.exists():
        raise SystemExit("output path already exists; choose a new versioned path")
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if output["status"] not in {"PASS", "PASS_WITH_CLEANUP_WARNING"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
