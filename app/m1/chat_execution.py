"""Shared claimed-run executor used by ordinary and SSE chat APIs."""
from __future__ import annotations

import uuid

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.m1.chat import ChatFailure, DeepSeekProvider, retrieve
from app.m1.chat_responses import direct_response, refusal_response
from app.m1.routing import decide_route
from app.m1.models import Trace, safe_trace_json, utc_now
from app.m1.services import add_user_message, append_run_event, finish_run
from app.m5.memory import (ContextualRouteClassifier, persist_auto_summary,
                           persist_memory_trace, prepare_memory_context, usage_calibration)


def _memory_telemetry(plan: dict, *, persisted: bool | None = None, version: int | None = None) -> dict:
    keys = ("level", "estimated_tokens_before", "estimated_tokens_after", "compression_ratio",
            "effective_context_tokens", "thresholds", "summary_generation", "summary_model_calls",
            "summary_cost_estimate_usd", "summary_latency_ms", "provider_prompt_tokens",
            "estimate_to_provider_ratio", "context_budget_exceeded", "context_budget_overage_tokens",
            "context_budget_handling")
    result = {key: plan.get(key) for key in keys}
    result.update({"summary_persisted": persisted, "summary_version": version,
                   "source_message_count": len(plan.get("source_message_ids") or [])})
    return result


def _finish_memory(db: Session, *, run, user_id: str, trace_id: str, plan: dict, succeeded: bool) -> None:
    persisted, version = (persist_auto_summary(db, owner_id=user_id, session_id=run.session_id,
        run_id=run.run_id, plan=plan) if succeeded else (False, None))
    try:
        persist_memory_trace(db, user_id=user_id, run_id=run.run_id, trace_id=trace_id,
                             plan=plan, persist_ok=persisted, summary_version=version)
    except Exception:
        db.rollback()


def execute_claimed_chat(db: Session, *, run, user_id: str, message: str, trace_id: str,
                         provider, index_path, retrieval_service=None, evaluation_at: str | None = None,
                         self_rag_config=None) -> dict:
    """Run one already database-claimed chat; failures are persisted before returning."""
    memory_plan = None
    try:
        add_user_message(db, user_id, run.session_id, message)
        memory_plan = prepare_memory_context(db, owner_id=user_id, session_id=run.session_id,
                                             current_query=message, run_id=run.run_id)
        route = decide_route(message)
        if route.decision_source == "FALLBACK":
            if memory_plan.get("context_budget_exceeded"):
                raise ChatFailure("CONTEXT_BUDGET_EXCEEDED", False,
                    "历史上下文仍超出安全预算，未调用路由模型或政策模型。",
                    {"estimated_tokens_after": memory_plan.get("estimated_tokens_after"),
                     "effective_context_tokens": memory_plan.get("effective_context_tokens")})
            try:
                active_classifier = provider or DeepSeekProvider()
                route = decide_route(message, ContextualRouteClassifier(active_classifier,
                    memory_plan.get("memory_context", "")))
            except ChatFailure:
                pass
        route_payload = route.model_dump(mode="json")
        append_run_event(db, run.run_id, trace_id, "route_decided", route_payload)
        if route.strategy in {"SINGLE", "MULTI"} and memory_plan.get("context_budget_exceeded"):
            raise ChatFailure("CONTEXT_BUDGET_EXCEEDED", False,
                "历史上下文仍超出安全预算，未调用政策模型。",
                {"estimated_tokens_after": memory_plan.get("estimated_tokens_after"),
                 "effective_context_tokens": memory_plan.get("effective_context_tokens")})
        if route.strategy == "AGENT":
            failure = ChatFailure("ROUTE_EXECUTOR_UNAVAILABLE", False,
                "已识别此请求需要实时业务能力；该执行器尚未开放，本次未调用工具或生成业务结论。",
                {"route": route_payload})
            finish_run(db, run, status="FAILED", error_code=failure.code,
                       error_detail=failure.diagnostic, trace_id=trace_id)
            return {"status": "FAILED", "result": None, "failure": failure}
        if route.strategy in {"DIRECT", "REFUSE"}:
            response = (refusal_response(reason_code=getattr(route, "reason_code", None), latency_ms=route.latency_ms) if route.strategy == "REFUSE" else
                        direct_response(route_payload, latency_ms=route.latency_ms))
            answer = response["answer"]
            result_payload = {"answer": answer, "citations": [], "strategy": route.strategy,
                "answer_kind": response["answer_kind"],
                "sub_strategy": route.sub_strategy, "route": route_payload, "trace_id": trace_id,
                "model_id": None, "thinking": None, "usage": None, "latency_ms": route.latency_ms,
                "estimated_cost_usd": 0, "context_memory": _memory_telemetry(memory_plan)}
            finish_run(db, run, status="SUCCEEDED", result=result_payload,
                assistant_message=answer, trace_id=trace_id)
            _finish_memory(db, run=run, user_id=user_id, trace_id=trace_id,
                           plan=memory_plan, succeeded=True)
            return {"status": "SUCCEEDED", "result": result_payload, "failure": None}
        append_run_event(db, run.run_id, trace_id, "agent_started", {"agent": "policy"})
        from app.m2.policy_execution import execute_policy_request
        from app.m2.retrieval import RetrievalService, config_from_environment
        active_provider = provider or DeepSeekProvider()
        selected_retrieval = retrieval_service or RetrievalService(index_path, config_from_environment())
        selected_retrieval.index_path = index_path

        def persist_policy_retrieval(payload):
            append_run_event(db, run.run_id, trace_id, "retrieval_hop", payload)
            db.add(Trace(trace_id=str(uuid.uuid4()), user_id=user_id, run_id=run.run_id,
                         event_type="policy_retrieval_aspect",
                         status="DEGRADED" if payload.get("degraded") else "RECORDED",
                         event_json=safe_trace_json(payload), created_at=utc_now()))
            db.commit()

        policy_result = execute_policy_request(message, route=route_payload, provider=active_provider,
            retrieval=selected_retrieval, index_path=index_path, evaluation_at=evaluation_at,
            on_retrieval=persist_policy_retrieval,
            memory_context=memory_plan.get("memory_context"))
        memory_plan["usage_calibration"] = usage_calibration(memory_plan, policy_result.get("usage") or policy_result.get("budget"))
        if policy_result.get("terminal_status") != "SUCCEEDED":
            raise ChatFailure(policy_result.get("error_code") or "POLICY_EXECUTION_FAILED", False,
                "政策问答未能完成；没有保存未经校验的答案。",
                policy_result.get("error_diagnostic"))
        policy_meta = policy_result.get("policy_retrieval") or {}
        db.add(Trace(trace_id=str(uuid.uuid4()), user_id=user_id, run_id=run.run_id,
                     event_type="policy_retrieval_completed", status=policy_result.get("stop_reason", "COMPLETED"),
                     event_json=safe_trace_json({key: policy_meta.get(key) for key in
                         ("version", "strategy_selected", "strategy_executed", "strategy_trigger_reason",
                          "degraded", "knowledge_version", "index_fingerprint", "evidence_ids",
                          "missing_aspect_ids", "request_mode")}), created_at=utc_now()))
        db.commit()
        result_payload = {key: policy_result.get(key) for key in
            ("answer", "answer_kind", "citations", "evidence", "knowledge_version", "retrieval",
             "policy_retrieval", "usage", "run_budget", "estimated_cost_usd", "latency_ms", "model_id", "thinking",
             "stop_reason") if key in policy_result}
        result_payload.update({"strategy": route.strategy, "sub_strategy": route.sub_strategy,
                               "route": route_payload, "trace_id": trace_id,
                               "context_memory": _memory_telemetry(memory_plan)})
        finish_run(db, run, status="SUCCEEDED", result=result_payload,
            model_id=policy_result.get("model_id"), usage=policy_result.get("usage"),
            latency_ms=policy_result.get("latency_ms"), assistant_message=policy_result["answer"],
            trace_id=trace_id)
        _finish_memory(db, run=run, user_id=user_id, trace_id=trace_id,
                       plan=memory_plan, succeeded=True)
        return {"status": "SUCCEEDED", "result": result_payload, "failure": None}
    except ChatFailure as exc:
        unavailable = exc.code in {"MODEL_UNAVAILABLE", "MODEL_AUTHENTICATION_FAILED", "MODEL_RATE_LIMITED"}
        finish_run(db, run, status="MODEL_UNAVAILABLE" if unavailable else "FAILED",
                   error_code=exc.code, error_detail=exc.diagnostic, trace_id=trace_id)
        if memory_plan is not None:
            _finish_memory(db, run=run, user_id=user_id, trace_id=trace_id,
                           plan=memory_plan, succeeded=False)
        return {"status": run.status, "result": None, "failure": exc}
    except SQLAlchemyError:
        db.rollback()
        try:
            finish_run(db, run, status="FAILED", error_code="PERSISTENCE_FAILED", trace_id=trace_id)
        except SQLAlchemyError:
            db.rollback()
        if memory_plan is not None:
            _finish_memory(db, run=run, user_id=user_id, trace_id=trace_id,
                           plan=memory_plan, succeeded=False)
        return {"status": "FAILED", "result": None,
                "failure": ChatFailure("PERSISTENCE_FAILED", True,
                                       "聊天结果暂未能安全保存，请查询运行状态。")}
    except Exception:
        db.rollback()
        try:
            finish_run(db, run, status="FAILED", error_code="EXECUTION_FAILED", trace_id=trace_id)
        except SQLAlchemyError:
            db.rollback()
        if memory_plan is not None:
            _finish_memory(db, run=run, user_id=user_id, trace_id=trace_id,
                           plan=memory_plan, succeeded=False)
        return {"status": "FAILED", "result": None,
                "failure": ChatFailure("EXECUTION_FAILED", False,
                                       "聊天执行未能完成；没有保存成功答案。")}
