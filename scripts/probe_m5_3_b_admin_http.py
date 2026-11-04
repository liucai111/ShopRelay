"""Live HTTP authorization and projection probe for M5.3-B.

Requires an already-created synthetic dashboard fixture and one-time password
in M53B_FIXTURE_PASSWORD. Never prints credentials, cookies, or DSNs.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / ".vendor"))
sys.path.insert(0, str(ROOT))
from scripts.g2_test_database import resolve_test_database_url


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: python scripts/probe_m5_3_b_admin_http.py <probe_marker>")
    marker = sys.argv[1]
    if not marker.startswith("m53b-") or len(marker) > 48:
        raise SystemExit("invalid probe marker")
    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test":
        raise SystemExit("refusing non-test database")
    password = os.environ.get("M53B_FIXTURE_PASSWORD", "")
    if len(password) < 14:
        raise SystemExit("M53B_FIXTURE_PASSWORD must be provided through process environment")
    base = os.environ.get("M53B_API_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    # The backend explicitly trusts its own origin; this exercises the same
    # CSRF cookie/header check without assuming the Vite proxy origin list.
    origin = os.environ.get("M53B_FRONTEND_ORIGIN", base)
    now = datetime.now(timezone.utc)
    start = (now - timedelta(days=30)).isoformat()
    end = (now + timedelta(minutes=1)).isoformat()
    records: dict[str, object] = {"probe_id": marker, "database": url.database, "base_url": base,
        "window_days": 30, "requests": [], "sensitive_values_logged": False}

    def contains_forbidden_key(value: object) -> bool:
        forbidden = {"prompt", "raw_prompt", "result_json", "checkpoint", "password", "cookie", "token"}
        if isinstance(value, dict):
            return any(str(key).casefold() in forbidden or contains_forbidden_key(child)
                       for key, child in value.items())
        if isinstance(value, list):
            return any(contains_forbidden_key(child) for child in value)
        return False

    def record(name: str, response: httpx.Response, detail: dict | None = None) -> dict:
        entry = {"name": name, "status": response.status_code}
        if detail:
            entry.update(detail)
        records["requests"].append(entry)  # type: ignore[union-attr]
        return response.json() if response.content else {}

    with httpx.Client(base_url=base, timeout=20, follow_redirects=False) as anonymous:
        response = anonymous.get("/api/admin/observability/overview", params={"from": start, "to": end})
        record("anonymous_overview", response)

    def authenticated(username: str):
        client = httpx.Client(base_url=base, timeout=20, follow_redirects=False)
        csrf = client.get("/api/demo/csrf")
        csrf_body = csrf.json()
        token = csrf_body.get("csrf_token")
        if csrf.status_code != 200 or not isinstance(token, str):
            raise RuntimeError("CSRF bootstrap failed")
        login = client.post("/api/demo/login", json={"username": username, "password": password},
                            headers={"Origin": origin, "X-CSRF-Token": token})
        payload = login.json()
        expected_id = f"{marker}-admin" if username == f"{marker}-admin" else f"{marker}-user"
        if login.status_code != 200 or (payload.get("user") or {}).get("user_id") != expected_id:
            code = ((payload.get("detail") or {}).get("code") if isinstance(payload.get("detail"), dict) else None) or payload.get("code")
            client.close()
            raise RuntimeError(f"synthetic account login failed: HTTP {login.status_code}, code={code or 'UNSPECIFIED'}")
        record(f"{username}_login", login, {"identity_matches_fixture": True})
        return client

    admin = authenticated(f"{marker}-admin")
    normal = authenticated(f"{marker}-user")
    try:
        overview_response = admin.get("/api/admin/observability/overview", params={"from": start, "to": end})
        overview = record("admin_overview", overview_response)
        agg = overview.get("overview", {})
        counts = agg.get("run_counts", {})
        record("overview_summary", overview_response, {"run_total": counts.get("total"),
            "status_counts": counts.get("by_status"), "unknown_result_count": counts.get("unknown_result_count"),
            "success_rate": counts.get("success_rate"), "latency_sample_count": (agg.get("latency_ms") or {}).get("sample_count"),
            "latency_p50_ms": (agg.get("latency_ms") or {}).get("p50"),
            "latency_p95_ms": (agg.get("latency_ms") or {}).get("p95"),
            "stub_run_count": (agg.get("model") or {}).get("stub_run_count"),
            "provider_usage_run_count": (agg.get("model") or {}).get("provider_usage_run_count"),
            "prompt_tokens": (agg.get("model") or {}).get("prompt_tokens"),
            "completion_tokens": (agg.get("model") or {}).get("completion_tokens"),
            "estimated_cost_usd": (agg.get("model") or {}).get("estimated_cost_usd"),
            "estimated_cost_sample_count": (agg.get("model") or {}).get("estimated_cost_sample_count"),
            "bm25_executed_count": (agg.get("retrieval") or {}).get("bm25_count"),
            "hybrid_executed_count": (agg.get("retrieval") or {}).get("hybrid_rerank_count"),
            "retrieval_degraded_count": (agg.get("retrieval") or {}).get("degraded_run_count"),
            "degraded_reasons": (agg.get("retrieval") or {}).get("degraded_reason_counts")})

        def list_runs(name: str, **params: str):
            query = {"from": start, "to": end, "limit": "25", **params}
            response = admin.get("/api/admin/observability/runs", params=query)
            body = record(name, response)
            result = body.get("runs", {})
            items = result.get("items", [])
            records["requests"][-1].update({"returned": len(items), "has_more": result.get("has_more"),
                "fixture_run_ids_present": [item.get("run_id") for item in items if str(item.get("run_id", "")).startswith(marker)]})  # type: ignore[index]
            return response, items

        page1, items1 = list_runs("admin_runs_page_1")
        page2, items2 = list_runs("admin_runs_page_2", offset="25")
        list_runs("filter_unknown", status="UNKNOWN_RESULT")
        list_runs("filter_agent", strategy="AGENT", agent="policy")
        list_runs("filter_tool", tool_id="search_knowledge")
        list_runs("filter_langgraph", orchestrator="langgraph")
        list_runs("filter_legacy", orchestrator="legacy")
        invalid = admin.get("/api/admin/observability/runs", params={"from": start, "to": end, "orchestrator": "other"})
        record("invalid_orchestrator_rejected", invalid)
        detail_response = admin.get(f"/api/admin/observability/runs/{marker}-agent-ok")
        detail_body = detail_response.json()
        detail = detail_body.get("detail", {})
        serialized = json.dumps(detail, ensure_ascii=False)
        record("admin_run_detail", detail_response, {"event_count": len((detail.get("events") or {}).get("items", [])),
            "span_count": len((detail.get("spans") or {}).get("items", [])),
            "graph_execution_present": detail.get("graph_execution") is not None,
            "trace_metadata_count": len(detail.get("trace_metadata", [])),
            "audit_metadata_count": len(detail.get("audit_metadata", [])),
            "sensitive_prompt_sentinel_absent": "REDACTION_SENTINEL" not in serialized,
            "forbidden_raw_fields_absent": not contains_forbidden_key(detail)})
        normal_overview = normal.get("/api/admin/observability/overview", params={"from": start, "to": end})
        record("ordinary_user_overview", normal_overview)
        future_start = datetime(2050, 1, 1, tzinfo=timezone.utc).isoformat()
        future_end = datetime(2050, 1, 2, tzinfo=timezone.utc).isoformat()
        empty_response = admin.get("/api/admin/observability/overview", params={"from": future_start, "to": future_end})
        empty_body = empty_response.json().get("overview", {})
        record("empty_window_overview", empty_response, {"run_total": (empty_body.get("run_counts") or {}).get("total"),
            "success_rate": (empty_body.get("run_counts") or {}).get("success_rate"),
            "latency_p50_ms": (empty_body.get("latency_ms") or {}).get("p50"),
            "latency_sample_count": (empty_body.get("latency_ms") or {}).get("sample_count")})
        records["pagination_verified"] = bool(page1.status_code == 200 and page2.status_code == 200 and
            (page1.json().get("runs") or {}).get("has_more") and len(items1) == 25 and items2 and
            items1[0].get("run_id") != items2[0].get("run_id"))
    finally:
        normal.close()
        admin.close()
    rendered = json.dumps(records, ensure_ascii=False, indent=2)
    output_path = os.environ.get("M53B_PROBE_OUTPUT")
    if output_path:
        target = Path(output_path).resolve()
        if ROOT not in target.parents:
            raise SystemExit("evidence output must remain inside the repository")
        target.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    statuses = {item["name"]: item["status"] for item in records["requests"]}  # type: ignore[index]
    required = {"anonymous_overview": 401, "admin_overview": 200, "admin_run_detail": 200,
                "ordinary_user_overview": 403, "invalid_orchestrator_rejected": 422}
    if any(statuses.get(key) != value for key, value in required.items()) or not records.get("pagination_verified"):
        raise SystemExit("HTTP acceptance failed")


if __name__ == "__main__":
    main()
