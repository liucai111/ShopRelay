"""Loopback-only deterministic M5.2-B-Q2 hybrid retrieval browser harness.

This harness is intentionally limited to the existing isolated ShopRelay test
database. It creates two temporary login identities and never calls a remote
model or enables business write adapters.
"""
from __future__ import annotations

import os
from uvicorn import run

from app.m0.config import ROOT, load_settings
from app.m1.api import create_app
from app.m1.db import make_engine, make_session_factory
from app.m1.models import User
from app.m1.services import set_demo_password
from app.m2.retrieval import IndexRegistry, RetrievalConfig, RetrievalService
from scripts.g2_test_database import resolve_test_database_url


class DeterministicPolicyProvider:
    def __init__(self) -> None:
        self.settings = load_settings(require_api_key=False)
        self.calls = {"plan": 0, "answer": 0}

    def plan_policy_aspects(self, question: str, *, context_notes=None) -> dict:
        self.calls["plan"] += 1
        anchors = [("return", "退货", "质量问题退货期限及人工审核"),
                   ("refund", "退款", "已签收订单申请退款的前置条件")]
        aspects = [{"aspect_id": key, "query_anchor": anchor, "text": text}
                   for key, anchor, text in anchors if anchor in question]
        if len(aspects) < 2:
            raise RuntimeError("Q2 probe expects the approved two-aspect dev fixture")
        return {"plan_result": {"request_mode": "POLICY_EXPLANATION",
                "requested_aspects": aspects, "user_provided_facts": []},
                "model_id": "q2-deterministic-policy-stub", "finish_reason": "stop",
                # Explicit zero external usage: this deterministic local stub
                # performs no provider request and must not poison run budget.
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "latency_ms": 0, "estimated_cost_usd": 0.0,
                "prompt_version": "m5.2-b-q2-stub-plan-v1", "raw_content": None}

    def answer_for_policy_aspects(self, question: str, evidence: list[dict], *,
                                  request_mode: str, requested_aspects: list[dict],
                                  user_provided_facts: list[dict], context_notes=None) -> dict:
        self.calls["answer"] += 1
        allowed = [row["chunk_id"] for row in evidence if isinstance(row.get("chunk_id"), str)]
        relevant = [item for item in allowed if item in {
            "policies/return-standard@2026-07#quality",
            "policies/refund@2026-01#before-shipping"}]
        citations = relevant or allowed
        return {"answer": ("政策层面，签收后30天内发生的质量问题可提交整单退货申请并进入人工审核，"
                           "但不保证获批；已签收订单须先有已批准的退货申请，之后才可申请退款。"
                           "题面未提供质量问题发生时长，不能确认该商品符合30天窗口；本次只解释政策，"
                           "没有核验具体订单或执行退款。"),
                "citations": citations, "finish_reason": "stop",
                "model_id": "q2-deterministic-policy-stub",
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
                "latency_ms": 0, "estimated_cost_usd": 0.0,
                "prompt_version": "m5.2-b-q2-stub-answer-v1", "raw_content": None}

    def answer(self, question: str, evidence: list[dict]) -> dict:
        return self.answer_for_policy_aspects(question, evidence,
            request_mode="POLICY_EXPLANATION", requested_aspects=[],
            user_provided_facts=[])


def main() -> None:
    marker = os.environ.get("M52BQ2_PROBE_ID", "").strip()
    password = os.environ.get("M52BQ2_TEST_PASSWORD", "")
    if not marker or not password or len(password) < 20:
        raise RuntimeError("Q2 probe marker and temporary password are required")
    if not marker.replace("_", "").isalnum() or len(marker) > 48:
        raise RuntimeError("invalid Q2 probe marker")

    url = resolve_test_database_url()
    if url.database != "shoprelay_m11_test" or url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("probe is restricted to loopback shoprelay_m11_test")
    database_url = url.render_as_string(hide_password=False)
    engine = make_engine(database_url)
    factory = make_session_factory(engine)
    user_ids = (f"{marker}_owner", f"{marker}_other")
    with factory() as db:
        if any(db.get(User, user_id) is not None for user_id in user_ids):
            raise RuntimeError("refusing to reuse a Q2 probe identity")
        for user_id in user_ids:
            db.add(User(user_id=user_id, display_name="M5.2-B-Q2隔离验收"))
        db.commit()
        for user_id in user_ids:
            set_demo_password(db, user_id, password)

    # Preserve the product's BM25 default. MULTI with two necessary aspects is
    # what selects HYBRID_RERANK in the existing policy execution contract.
    os.environ["SHOPRELAY_RETRIEVAL_MODE"] = "bm25"
    os.environ["SHOPRELAY_QDRANT_MODE"] = "local"
    os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
    os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "false"
    os.environ["SHOPRELAY_GRAPH_VERSION"] = "m5.2-b-q2"
    os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m5.2-b-q2-isolated"

    provider = DeterministicPolicyProvider()
    app = create_app(database_url=database_url, engine=engine,
        chat_provider=provider, index_path=ROOT / "data" / "index.json",
        orchestrator_mode="langgraph")
    app.state.retrieval_service = RetrievalService(ROOT / "data" / "index.json",
        RetrievalConfig(version="m2-r-v1", mode="bm25", qdrant_mode="local"),
        registry=IndexRegistry())
    print(f"Q2_PROBE_ID={marker}", flush=True)
    print(f"Q2_TEST_USERS={user_ids[0]},{user_ids[1]}", flush=True)
    print("Q2_PROVIDER=deterministic_stub; Q2_DEEPSEEK_HTTP_REQUESTS=0; Q2_BUSINESS_WRITES=0; Q2_MCP_WRITES=0", flush=True)
    try:
        run(app, host="127.0.0.1", port=8000, log_level="warning")
    finally:
        close = getattr(app.state.retrieval_service, "close", None)
        if callable(close):
            close()
        app.state.engine.dispose(close=True)


if __name__ == "__main__":
    main()
