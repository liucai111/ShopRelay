"""Real isolated stdio/Streamable HTTP MCP probe plus ProductAgent adapter path."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import replace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.m1.models import Base, Product, ObservabilityRecord, Run
from app.m3.agents import (AfterSalesAgent, AgentRegistry, OrderLogisticsAgent,
    PolicyAgent, ProductAgent)
from app.m3.graph import build_supervisor_graph, invoke_supervisor
from app.m3.product_tools import ProductReadRepository
from app.orchestration.observability import InMemoryObservabilitySink
from app.orchestration.adapters import GraphServices
from app.mcp import McpClientError, McpServerTrust, McpToolGrant, TrustedMcpClient
from app.mcp.adapters import McpProductReadRepository

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "scripts/mcp_m4_1_test_server.py"
SDK_VERSION = "2.2.0"


def _env(db_url: str) -> dict[str, str]:
    result = {key: os.environ[key] for key in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP")
              if key in os.environ}
    result.update({"PYTHONPATH": os.pathsep.join([str(ROOT / ".vendor" / "g2"),
        str(ROOT / ".vendor"), str(ROOT)]), "PYTHONIOENCODING": "utf-8",
        "SHOPRELAY_MCP_PRODUCT_DB_URL": db_url})
    return result


def _trust(server_id: str, *, transport: str, db_url: str, port: int | None = None) -> McpServerTrust:
    grants = (
        McpToolGrant("get_product_detail", "product_detail", read_only=True),
        McpToolGrant("public_catalog_tag", "public_tag", read_only=True),
        McpToolGrant("page_probe_59", "page_probe_59", read_only=True),
    )
    if transport == "stdio":
        return McpServerTrust(server_id=server_id, transport="stdio", tools=grants,
            command=sys.executable, args=(str(SERVER), "stdio"), cwd=str(ROOT), env=_env(db_url),
            allowed_resources=("shoprelay://m4.1/public-info",), allowed_prompts=("catalog_question",))
    return McpServerTrust(server_id=server_id, transport="streamable_http", tools=grants,
        url=f"http://127.0.0.1:{port}/mcp", allowed_hosts=("127.0.0.1",),
        allowed_resources=("shoprelay://m4.1/public-info",), allowed_prompts=("catalog_question",))


async def _transport_probe(trust: McpServerTrust) -> dict:
    client = TrustedMcpClient(trust)
    tools = await client.discover()
    assert len(client._catalog) == 63, f"expected 63 remotely discovered tools, got {len(client._catalog)}"
    assert len(tools) == 3, f"expected only 3 locally allowlisted tools, got {len(tools)}"
    assert {"product_detail", "public_tag", "page_probe_59"}.issubset({item["local_name"] for item in tools})
    durations = []
    detail = await client.call("product_detail", {"product_id": "P-MCP-TEST"})
    durations.append(client.last_duration_ms)
    assert detail["data"]["availability"] == "AVAILABLE"
    assert detail["data"]["product"]["product_id"] == "P-MCP-TEST"
    page = await client.call("page_probe_59", {"value": "pagination"})
    durations.append(client.last_duration_ms)
    assert page["data"]["page_item"] == 59
    tag = await client.call("public_tag", {"query": "public"})
    durations.append(client.last_duration_ms)
    assert tag["data"]["source"] == "isolated_probe"
    assert tag["untrusted"] is True and "Ignore previous instructions" in tag["data"]["message"]
    resources = await client.list_resources()
    resource_content = await client.read_resource("shoprelay://m4.1/public-info")
    prompts = await client.list_prompts()
    prompt_messages = await client.get_prompt("catalog_question", {"topic": "电水壶"})
    assert "catalog_question" in prompts and resource_content
    assert "电水壶" in "".join(prompt_messages)
    try:
        await client.call("delete_customer_records", {"confirmation": "yes"})
        raise AssertionError("ungranted server tool was callable")
    except McpClientError as exc:
        assert exc.code == "MCP_TOOL_NOT_AUTHORIZED"
    try:
        await client.call("product_detail", {"product_id": 123})
        raise AssertionError("invalid schema argument was accepted")
    except McpClientError as exc:
        assert exc.code == "MCP_ARGUMENT_SCHEMA_INVALID"
    return {"status": "PASS", "transport": client.trust.transport,
        "remotely_discovered_tool_count": len(client._catalog), "locally_allowlisted_tool_count": len(tools),
        "negotiated_protocol_version": client.last_protocol_version,
        "allowlisted_tool_ids": [row["tool_id"] for row in tools],
        "product_detail": detail["data"], "page_59_call": page["data"],
        "resource_count": len(resources), "resource_read": bool(resource_content),
        "prompts": prompts, "prompt_read": bool(prompt_messages),
        "untrusted_malicious_text_preserved_as_data": True,
        "local_write_denial": "MCP_TOOL_NOT_AUTHORIZED",
        "schema_denial": "MCP_ARGUMENT_SCHEMA_INVALID", "client_call_count": client.call_count,
        "call_duration_ms": durations,
        "latency_ms": {"p50": statistics.median(durations),
            "p95_nearest_rank": sorted(durations)[max(0, int(0.95 * len(durations) + 0.999999) - 1)]}}


def main() -> int:
    out = ROOT / "docs/probes/m4.1-mcp-dual-transport-20261022-r4.json"
    if out.exists():
        raise SystemExit(f"refusing to overwrite {out}")
    work = ROOT / f"mcp-m4.1-probe-{uuid.uuid4().hex}"
    work.mkdir()
    db_path = work / "mcp-products.sqlite3"
    # Preserve Windows drive separators; sqlite:///D:/... is interpreted as a
    # rooted /D:/ path by this runtime, while sqlite:///D:\... is correct.
    db_url = f"sqlite:///{db_path}"
    engine = create_engine(db_url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        if db.get(Product, "P-MCP-TEST") is None:
            db.add(Product(product_id="P-MCP-TEST", name="隔离 MCP 测试水壶", category="家电", price_cents=12345))
    result: dict = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "phase": "M4.1",
        "sdk": f"mcp=={SDK_VERSION}", "protocol_target": "2026-07-28",
        "database_scope": "ephemeral SQLite; ProductReadRepository reused",
        "business_write_calls": 0, "deepseek_requests": 0,
        "fixture_source": "SIMULATED_TEST_DATA", "fixture_cleanup": "temporary directory removed at end"}
    processes = []
    try:
        result["stdio"] = asyncio.run(_transport_probe(_trust("m4.1-stdio", transport="stdio", db_url=db_url)))
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        env = _env(db_url)
        process = subprocess.Popen([sys.executable, str(SERVER), "http", str(port)], cwd=str(ROOT), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        processes.append(process)
        ready = False
        for _ in range(80):
            if process.poll() is not None:
                raise RuntimeError("HTTP MCP server exited before readiness")
            with socket.socket() as sock:
                sock.settimeout(0.2)
                if sock.connect_ex(("127.0.0.1", port)) == 0:
                    ready = True
                    break
            time.sleep(0.2)
        if not ready:
            raise TimeoutError("HTTP MCP server readiness timeout")
        http_trust = _trust("m4.1-http", transport="streamable_http", db_url=db_url, port=port)
        result["streamable_http"] = asyncio.run(_transport_probe(http_trust))

        # Existing ProductAgent and Supervisor execute through the MCP-backed
        # repository, invoked through the existing authenticated FastAPI Run/SSE
        # path. The remote service delegates to ProductReadRepository.
        http_client = TrustedMcpClient(http_trust)
        mcp_repo = McpProductReadRepository(http_client)
        registry = AgentRegistry(policy=PolicyAgent(), order_logistics=OrderLogisticsAgent(),
            after_sales=AfterSalesAgent(), product=ProductAgent(mcp_repo))
        # The API database is separate from the MCP service fixture. Only the
        # existing product read repository serves product data to the MCP server.
        from app.m0.data import seed
        from app.m1.api import CSRF_HEADER, create_app
        from app.m1.db import make_engine, make_session_factory, upgrade_database
        from app.m1.services import set_demo_password
        from langgraph.checkpoint.memory import MemorySaver
        from fastapi.testclient import TestClient
        import app.m1.api as api_module

        api_db_path = work / "api.sqlite3"
        seed(api_db_path)
        api_url = f"sqlite:///{api_db_path}"
        upgrade_database(api_url)
        api_engine = make_engine(api_url)
        api_factory = make_session_factory(api_engine)
        demo_password = "mcp-m4-1-" + uuid.uuid4().hex
        with api_factory() as db:
            set_demo_password(db, "u_demo", demo_password)
        loader = api_module.load_settings
        def m4_settings(**_kwargs):
            return replace(loader(environ={"SHOPRELAY_ORCHESTRATOR": "langgraph",
                "SHOPRELAY_M3_SUPERVISOR_ENABLED": "true", "SHOPRELAY_GRAPH_VERSION": "m4.1",
                "SHOPRELAY_EXECUTION_VERSION": "m4.1-mcp-readonly"}, local_values={}, require_api_key=False),
                m3_supervisor_enabled=True)
        api_module.load_settings = m4_settings
        memory_saver = MemorySaver()
        @asynccontextmanager
        async def checkpointer_factory(_url, _schema):
            yield memory_saver
        def router(_query):
            return {"strategy": "AGENT", "reason_code": "LIVE_PRODUCT_FACT",
                "intent_labels": ["live_business"], "required_capabilities": ["get_product_detail"],
                "decision_source": "M4.1_INTEGRATION_TEST"}
        def run_supervisor(state, context):
            trusted = context.trusted_agent_context
            from app.orchestration.persistence import SQLAlchemyObservabilitySink
            run_sink = SQLAlchemyObservabilitySink(api_factory, user_id=trusted.user_id,
                run_id=trusted.run_id, trace_id=trusted.trace_id,
                graph_version=state.get("graph_version", "m4.1"))
            graph = build_supervisor_graph(registry, sink=run_sink)
            return invoke_supervisor(graph, state, context)
        services = GraphServices(router=router, direct=lambda _s: {}, single=lambda _s: {},
            multi=lambda _s: {}, refuse=lambda _s: {},
            agent_supervisor=run_supervisor)
        api_app = create_app(database_url=api_url, engine=api_engine, orchestrator_mode="langgraph",
            checkpointer_factory=checkpointer_factory, checkpointer_readiness=lambda *_: True,
            graph_services=services)
        request_body = {"message": "查看商品 P-MCP-TEST 的详情", "client_request_id": "mcp-m4-1-product-1"}
        saved_cookies = None
        run_id = None
        try:
            with TestClient(api_app) as client:
                csrf = client.get("/api/demo/csrf").json()["csrf_token"]
                login = client.post("/api/demo/login", headers={"Origin": str(client.base_url).rstrip("/"),
                    CSRF_HEADER: csrf}, json={"username": "u_demo", "password": demo_password})
                assert login.status_code == 200
                headers = {"Origin": str(client.base_url).rstrip("/"),
                    CSRF_HEADER: client.cookies.get("shoprelay_csrf")}
                started = time.perf_counter()
                response = client.post("/api/chat/stream", headers=headers, json=request_body)
                assert response.status_code == 200
                parsed_events = []
                for block in response.text.replace("\r\n", "\n").split("\n\n"):
                    event_name = next((line[7:] for line in block.splitlines() if line.startswith("event: ")), None)
                    data_line = next((line[6:] for line in block.splitlines() if line.startswith("data: ")), None)
                    if event_name and data_line:
                        parsed_events.append({"type": event_name, "data": json.loads(data_line)})
                run_id = next((event["data"].get("run_id") for event in parsed_events
                    if event["type"] == "started" and event["data"].get("run_id")), None)
                if run_id is None:
                    # run_id is also available from the single newly-created DB record.
                    with api_factory() as db:
                        run_id = db.execute(select(Run.run_id).order_by(Run.created_at.desc())).scalars().first()
                assert run_id
                event_types = [event["type"] for event in parsed_events]
                assert "answer" in event_types and event_types[-1] == "done", event_types
                run_response = client.get(f"/api/runs/{run_id}")
                assert run_response.status_code == 200
                api_result = run_response.json()["run"]
                assert api_result["status"] == "SUCCEEDED", api_result
                assert api_result["answer"]["business_results"]["get_product_detail"]["product"]["product_id"] == "P-MCP-TEST"
                status_before_replay = http_client.call_count
                replay = client.post("/api/chat", headers=headers, json=request_body)
                assert replay.status_code == 200 and replay.json()["run_id"] == run_id
                assert http_client.call_count == status_before_replay == 1
                saved_cookies = dict(client.cookies)
                api_spans = []
                with api_factory() as db:
                    run = db.get(Run, run_id)
                    graph_run = db.get(__import__("app.m1.models", fromlist=["GraphExecutionRecord"]).GraphExecutionRecord, run_id)
                    rows = db.execute(select(ObservabilityRecord).where(ObservabilityRecord.run_id == run_id,
                        ObservabilityRecord.record_type == "span")).scalars().all()
                api_spans = [json.loads(row.payload_json) for row in rows]
                assert run.status == graph_run.status == "SUCCEEDED" and graph_run.checkpoint_id
                assert any(span["node_name"] == "mcp.client.m4.1-http.streamable_http.get_product_detail" for span in api_spans)
                assert any(span["node_name"] == "m3.tool.get_product_detail" for span in api_spans)
                snapshots = list(memory_saver.list({"configurable": {"thread_id": run_id, "checkpoint_ns": ""}}))
                checkpoint_text = repr([item.checkpoint.get("channel_values", {}) for item in snapshots])
                assert "P-MCP-TEST" not in checkpoint_text and "隔离 MCP 测试水壶" not in checkpoint_text
                assert http_client.call_count == 1
            # Recreate the FastAPI app against the same business DB/checkpointer.
            api_app_rebuilt = create_app(database_url=api_url, engine=api_engine, orchestrator_mode="langgraph",
                checkpointer_factory=checkpointer_factory, checkpointer_readiness=lambda *_: True,
                graph_services=services)
            with TestClient(api_app_rebuilt) as rebuilt:
                for name, value in saved_cookies.items():
                    rebuilt.cookies.set(name, value)
                restored = rebuilt.get(f"/api/runs/{run_id}", headers={"Origin": str(rebuilt.base_url).rstrip("/")})
                assert restored.status_code == 200 and restored.json()["run"]["status"] == "SUCCEEDED"
                restored_events = rebuilt.get(f"/api/runs/{run_id}/events",
                    headers={"Origin": str(rebuilt.base_url).rstrip("/")})
                assert restored_events.status_code == 200 and len(restored_events.json()["events"]) == len(parsed_events)
            result["agent_integration"] = {"status": "PASS", "path": ["authenticated FastAPI",
                "Run/SSE", "legacy-compatible LangGraph orchestration", "Supervisor", "ProductAgent",
                "AgentExecutionContext", "McpProductReadRepository", "MCP Client",
                "Streamable HTTP MCP Server", "existing ProductReadRepository"],
                "router_mode": "deterministic integration injection; not Router quality evidence",
                "run_id": run_id, "run_status": api_result["status"], "run_idempotent_replay": True,
                "replay_additional_mcp_calls": 0, "sse_events": event_types,
                "answer": api_result["answer"]["answer"],
                "business_result": api_result["answer"]["business_results"]["get_product_detail"],
                "mcp_call_count": http_client.call_count,
                "mcp_last_call_duration_ms": http_client.last_duration_ms,
                "mcp_server_id": http_trust.server_id, "mcp_transport": http_trust.transport,
                "mcp_tool_id": "m4.1-http/get_product_detail",
                "request_latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "trace_span_count": len(api_spans), "mcp_trace_span_persisted": True,
                "checkpoint_excludes_product_payload": True,
                "product_tool_trace_span_persisted": True,
                "checkpoint_id_present": True, "app_rebuild_run_and_events_restored": True}
        finally:
            api_module.load_settings = loader
            api_engine.dispose()
        with factory() as db:
            result["fixture_row_count_after_calls"] = len(db.execute(select(Product).where(
                Product.product_id == "P-MCP-TEST")).scalars().all())
        result["transport_call_count"] = result["stdio"]["client_call_count"] + \
            result["streamable_http"]["client_call_count"] + http_client.call_count
        result["write_execution_count"] = 0
        result["status"] = "PASS"
    except Exception as exc:
        result["status"] = "FAIL"
        result["error_type"] = type(exc).__name__
        result["error_code"] = getattr(exc, "code", None)
        raise
    finally:
        for proc in processes:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        engine.dispose()
        import shutil
        shutil.rmtree(work)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps({"record": str(out), "status": result["status"],
        "transports": {key: value["status"] for key, value in result.items()
                       if key in {"stdio", "streamable_http"}},
        "agent_integration": result["agent_integration"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
