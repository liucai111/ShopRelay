"""Small real integration probes. Writes sanitized evidence only, never credentials."""
from __future__ import annotations

import importlib
import importlib.metadata
import json
import os
import platform
import shutil
import socket
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def package(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def error(exc: BaseException) -> str:
    # Class and HTTP status are enough for diagnostics; exception text may contain secrets.
    status = getattr(exc, "status_code", None)
    return f"{type(exc).__name__}" + (f" HTTP {status}" if status else "")


def deepseek_probe() -> dict:
    from app.m0.config import create_deepseek_client, load_settings
    try:
        settings = load_settings()
    except RuntimeError:
        return {"status": "blocked", "reason": "DEEPSEEK_API_KEY absent from process and ignored local configuration"}
    try:
        from openai import OpenAI
    except ImportError:
        return {"status": "blocked", "reason": "openai package absent"}
    node = settings.nodes["policy_answer"]
    client = create_deepseek_client(settings, node)
    model = settings.model
    results = {}
    for mode in ("disabled", "enabled"):
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(model=model, messages=[{"role": "system", "content": "Reply in json only, schema: {\"label\":\"policy\"}."}, {"role": "user", "content": "json: classify '退货政策' as policy."}], response_format={"type": "json_object"}, extra_body={"thinking": {"type": mode}}, max_tokens=256)
            content = response.choices[0].message.content or ""
            parsed = json.loads(content)
            results[f"json_{mode}"] = {"status": "ok", "valid_json": isinstance(parsed, dict), "model": response.model, "usage": response.usage.model_dump() if response.usage else None, "latency_ms": round((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            results[f"json_{mode}"] = {"status": "failed", "error": error(exc), "latency_ms": round((time.perf_counter() - started) * 1000)}
    tools = [{"type": "function", "function": {"name": "lookup_policy", "description": "Look up a named policy", "parameters": {"type": "object", "properties": {"policy": {"type": "string"}}, "required": ["policy"]}}}]
    for mode in ("disabled", "enabled"):
        started = time.perf_counter()
        try:
            messages = [{"role": "user", "content": "Use lookup_policy to check the return policy. Call the tool before answering."}]
            response = client.chat.completions.create(model=model, messages=messages, tools=tools, tool_choice="required" if mode == "disabled" else "auto", extra_body={"thinking": {"type": mode}}, max_tokens=512)
            message = response.choices[0].message
            calls = message.tool_calls or []
            results[f"tools_{mode}"] = {"status": "ok", "call_count": len(calls), "name": calls[0].function.name if calls else None, "arguments_valid_json": bool(calls and isinstance(json.loads(calls[0].function.arguments), dict)), "has_reasoning_content": bool(getattr(message, "reasoning_content", None)), "model": response.model, "usage": response.usage.model_dump() if response.usage else None, "latency_ms": round((time.perf_counter() - started) * 1000)}
            if calls:
                messages.extend([message, {"role": "tool", "tool_call_id": calls[0].id, "content": "Synthetic policy: quality returns require manual review."}])
                continuation = client.chat.completions.create(model=model, messages=messages, tools=tools, tool_choice="auto", extra_body={"thinking": {"type": mode}}, max_tokens=512)
                results[f"tools_{mode}"]["continuation_status"] = "ok"
                results[f"tools_{mode}"]["continuation_usage"] = continuation.usage.model_dump() if continuation.usage else None
        except Exception as exc:
            previous = results.get(f"tools_{mode}")
            if previous and previous["status"] == "ok":
                previous["continuation_status"] = "failed"
                previous["continuation_error"] = error(exc)
            else:
                results[f"tools_{mode}"] = {"status": "failed", "error": error(exc), "latency_ms": round((time.perf_counter() - started) * 1000)}
    try:
        stream = client.chat.completions.create(model=model, messages=[{"role": "user", "content": "Say OK."}], stream=True, stream_options={"include_usage": True}, extra_body={"thinking": {"type": "disabled"}}, max_tokens=32)
        events = 0
        usage = None
        for part in stream:
            events += 1
            if part.usage:
                usage = part.usage.model_dump()
        results["stream"] = {"status": "ok", "events": events, "usage": usage}
    except Exception as exc:
        results["stream"] = {"status": "failed", "error": error(exc)}
    return {"model_requested": model, "config_source": {"api_key": settings.api_key_source, "base_url": settings.base_url_source, "model": settings.model_source}, "checks": results}


def qdrant_probe() -> dict:
    try:
        from qdrant_client import QdrantClient, models
    except ImportError:
        return {"status": "blocked", "reason": "qdrant-client absent"}
    path = ROOT / "data/qdrant-probe"
    try:
        client = QdrantClient(path=str(path))
        if not client.collection_exists("m0_probe"):
            client.create_collection("m0_probe", vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
        client.upsert("m0_probe", points=[models.PointStruct(id=1, vector=[1.0, 0.0], payload={"policy": "test"})])
        client.close()
        reopened = QdrantClient(path=str(path))
        got = reopened.retrieve("m0_probe", ids=[1], with_payload=True)
        reopened.delete("m0_probe", points_selector=models.PointIdsList(points=[1]))
        deleted = reopened.retrieve("m0_probe", ids=[1])
        reopened.close()
        return {"status": "ok", "persisted_after_reopen": len(got) == 1, "deleted": len(deleted) == 0, "mode": "local_path", "service_mode": "not_tested"}
    except Exception as exc:
        return {"status": "failed", "error": error(exc)}


def main() -> None:
    packages = {name: package(name) for name in ("openai", "qdrant-client", "mcp", "sentence-transformers", "torch", "psycopg", "pytest")}
    with socket.socket() as sock:
        sock.settimeout(1)
        postgresql_local = sock.connect_ex(("127.0.0.1", 5432)) == 0
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version.split()[0], "platform": platform.platform(), "packages": packages, "docker_executable": bool(shutil.which("docker")), "postgresql_local_port_open": postgresql_local, "database": {"sqlite_version": sqlite3.sqlite_version}, "deepseek": deepseek_probe(), "qdrant": qdrant_probe(), "embedding": {"status": "not_tested" if packages["sentence-transformers"] else "blocked", "reason": "sentence-transformers absent" if not packages["sentence-transformers"] else "model load probe pending"}, "mcp": {"status": "not_tested" if packages["mcp"] else "blocked", "reason": "mcp package absent" if not packages["mcp"] else "transport probe pending"}, "postgresql": {"status": "not_tested" if postgresql_local and packages["psycopg"] else "blocked", "reason": "local service or psycopg unavailable" if not (postgresql_local and packages["psycopg"]) else "concurrency probe pending"}}
    output_dir = ROOT / "docs/probes"
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"runtime-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    with output.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(json.dumps({"path": str(output), "result": result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
