"""Thin adapters for existing routing, retrieval and Self-RAG services."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from app.m1.routing import decide_route
from app.m1.chat_responses import direct_response, refusal_response


@dataclass
class GraphServices:
    """Pure execution callbacks; callbacks must not claim Runs or persist SSE."""
    router: Callable[[str], Any]
    direct: Callable[[dict], dict]
    single: Callable[[dict], dict]
    multi: Callable[[dict], dict]
    refuse: Callable[[dict], dict]
    agent_supervisor: Callable[[dict, Any], dict] | None = None
    agent_single: Callable[[dict, Any], dict] | None = None
    business_composer: Callable[[dict, Any], dict] | None = None


def default_route(query: str, classifier=None, *, available_capabilities=None) -> dict:
    return decide_route(query, classifier, available_capabilities=available_capabilities).model_dump(mode="json")


def _citations(evidence: list[dict], ids: list[str]) -> list[dict]:
    by_id = {row.get("chunk_id"): row for row in evidence}
    if not ids or any(item not in by_id for item in ids):
        if ids:
            raise ValueError("citation is outside retrieved evidence")
    return [by_id[item] for item in ids]


def make_legacy_services(*, provider=None, retrieval_service=None, index_path=None,
                         evaluation_at: str | None = None, classifier=None,
                         available_capabilities=None,
                         self_rag_config=None,
                         require_model_route_success: bool = False,
                         direct_fn: Callable[[dict], dict] | None = None,
                         refuse_fn: Callable[[dict], dict] | None = None) -> GraphServices:
    """Build service adapters from current M1/M2 functions; no DB or event writes."""
    def router(query: str, *, context_notes: str = ""):
        active_classifier = classifier
        if classifier is not None and context_notes:
            from app.m5.memory import ContextualRouteClassifier
            active_classifier = ContextualRouteClassifier(classifier, context_notes)
        result = default_route(query, active_classifier, available_capabilities=available_capabilities)
        if require_model_route_success and result.get('fallback_error'):
            from app.m1.chat import ChatFailure
            if 'DemoBudgetExhausted' in result['fallback_error']:
                raise ChatFailure('DEMO_BUDGET_EXHAUSTED', False,
                    '个人演示模型额度不足，并非模型连接故障。请联系演示维护者核对额度。',
                    {'route': result, 'usage': result.get('usage')})
            raise ChatFailure('ROUTE_CLASSIFICATION_FAILED', False,
                '模型未能可靠理解这次请求，未自动重试。请稍后再试或查看执行详情。',
                {'route': result, 'usage': result.get('usage')})
        return result

    def direct(state: dict) -> dict:
        route = state.get("route") or {}
        return (direct_fn(state) if direct_fn else direct_response(route))

    def refuse(state: dict) -> dict:
        route = state.get("route") or {}
        return (refuse_fn(state) if refuse_fn else refusal_response(reason_code=route.get("reason_code")))

    def single(state: dict) -> dict:
        from app.m1.chat import DeepSeekProvider
        from app.m2.policy_execution import execute_policy_request
        from app.m2.retrieval import RetrievalService, config_from_environment
        active_provider = provider or DeepSeekProvider()
        active_retrieval = retrieval_service or RetrievalService(index_path, config_from_environment())
        return execute_policy_request(state["query"], route=state.get("route") or {"strategy": "SINGLE"},
            provider=active_provider, retrieval=active_retrieval, index_path=index_path,
            evaluation_at=evaluation_at, on_retrieval=state.get("_emit_retrieval_hop"),
            on_retrieval_audit=state.get("_emit_policy_retrieval_audit"),
            run_id=state.get("run_id"), trace_id=state.get("trace_id"),
            memory_context=state.get("_memory_context"))

    def multi(state: dict) -> dict:
        from app.m1.chat import DeepSeekProvider
        from app.m2.policy_execution import execute_policy_request
        from app.m2.retrieval import RetrievalService, config_from_environment
        active_provider = provider or DeepSeekProvider()
        active_retrieval = retrieval_service or RetrievalService(index_path, config_from_environment())
        route = state.get("route") or {"strategy": "MULTI"}
        return execute_policy_request(state["query"], route=route, provider=active_provider,
            retrieval=active_retrieval, index_path=index_path, evaluation_at=evaluation_at,
            on_retrieval=state.get("_emit_retrieval_hop"),
            on_retrieval_audit=state.get("_emit_policy_retrieval_audit"),
            run_id=state.get("run_id"), trace_id=state.get("trace_id"),
            memory_context=state.get("_memory_context"))
    return GraphServices(router=router, direct=direct, single=single, multi=multi, refuse=refuse)
