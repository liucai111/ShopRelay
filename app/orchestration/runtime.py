"""API adapter for one already-claimed LangGraph Run."""
from __future__ import annotations

import json
import re
from sqlalchemy import select
from dataclasses import replace as dataclass_replace

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.m1.chat import ChatFailure, DeepSeekProvider
from app.m1.models import ConversationSession, Run, Message
from app.m1.services import add_user_message, append_run_event, finish_run
from app.m5.memory import (persist_auto_summary, persist_memory_trace, prepare_memory_context,
                           usage_calibration)
from .adapters import GraphServices, make_legacy_services
from .checkpointer import open_checkpointer, run_checkpointer_async
from .graph import build_orchestration_graph
from .observability import MetricRecord
from .persistence import (GraphExecutionConflict, SQLAlchemyObservabilitySink,
                          begin_graph_execution, update_graph_execution)
from .state import GraphInput, GraphRuntimeContext, graph_input_state
from app.m3.order_tools import SqlAlchemyOrderReadRepository, requested_order_id, is_return_guidance_query

_SAFE_FAILURES = {
    "ROUTE_EXECUTOR_UNAVAILABLE": "已识别此请求需要实时业务能力；该执行器尚未开放，本次未调用工具或生成业务结论。",
    "MODEL_UNAVAILABLE": "模型服务暂不可用；本次没有保存政策答案。",
    "MODEL_AUTHENTICATION_FAILED": "模型服务认证失败；本次没有保存政策答案。",
    "MODEL_RATE_LIMITED": "模型服务繁忙；本次没有保存政策答案。",
    "RETRIEVAL_UNAVAILABLE": "当前政策检索服务不可用，未生成政策结论。",
    "EVIDENCE_GRADE_SCHEMA_INVALID": "证据评估未通过结构校验，本次没有保存政策答案。",
    "CONDITION_SCHEMA_INVALID": "条件拆解未通过结构校验，本次没有保存政策答案。",
    "MODEL_INVALID_RESPONSE": "模型响应或引用校验失败；本次没有保存政策答案。",
    "ANSWER_SEMANTIC_VALIDATION_FAILED": "模型答复未通过政策语义校验；本次没有保存政策答案。",
    "MODEL_BUDGET_EXCEEDED": "本轮模型预算已用尽；未生成未经校验的政策结论。",
    "CONTEXT_BUDGET_EXCEEDED": "历史上下文仍超出安全预算；本次没有调用模型或生成政策结论。",
    "POLICY_ASPECT_PLANNING_UNAVAILABLE": "无法安全拆分本次问题的有限政策检索目标；未生成政策结论。",
    "POLICY_ASPECT_PLAN_INVALID": "政策检索目标未通过结构或来源校验；未生成政策结论。",
    "POLICY_ASPECT_LIMIT_EXCEEDED": "本次请求包含过多政策方面，未继续检索或生成结论。",
    "RETRIEVAL_EVIDENCE_VERSION_INVALID": "检索证据不属于本轮锁定的政策快照；未生成政策结论。",
    "ANSWER_ASPECT_CITATION_MISSING": "答复未为每个有证据支持的政策方面提供本轮引用；未保存政策答案。",
    "RETRIEVAL_BUDGET_EXCEEDED": "本轮有限检索预算已用尽；未生成确定性政策结论。",
    "ACTION_EXECUTION_NOT_SUPPORTED": "本接口仅解释政策，不执行退款、退货或取消操作；未提交业务请求。",
    "ROUTE_CLASSIFICATION_FAILED": "请求分类未能安全完成；本次没有保存政策答案。",
    "AGENT_NOT_IMPLEMENTED": "已识别需要专业业务能力，但该 Agent 服务尚未实现；本次未执行工具或生成业务结论。",
    "AGENT_RESULT_NOT_USER_ANSWERABLE": "专业 Agent 骨架已完成委派校验，但业务执行与答案汇总尚未开放；本次未生成成功答案。",
    "ACTION_CONFIRMATION_REQUIRED": "此售后操作需要已实现的持久化确认流程；本次未执行任何写操作。",
    "ORDER_NOT_FOUND": "未找到可供当前账户访问的订单。",
    "ORDER_ID_REQUIRED": "请提供需要查询的订单号。",
    "ORDER_ID_AMBIGUOUS": "请一次只提供一个订单号，以便安全查询。",
    "INVALID_ORDER_ID": "订单号格式不正确，请检查后重试。",
    "TOOL_DATABASE_UNAVAILABLE": "订单服务暂时不可用，本次未返回订单信息。",
    "TOOL_TIMEOUT": "订单查询超时，本次未返回订单信息。",
    "TOOL_EVENT_PERSISTENCE_FAILED": "工具执行事件未能安全保存，本次未返回业务信息。",
    "TOOL_EXECUTION_FAILED": "订单查询未能完成，本次未返回业务信息。",
}
_AMBIGUOUS_PROVIDER_EXCEPTIONS = {"APIConnectionError", "APITimeoutError", "ConnectTimeout", "ReadTimeout", "TimeoutError"}


def _citation_payload(result: dict) -> list[dict]:
    evidence = result.get("evidence") or []
    known = {row.get("chunk_id"): row for row in evidence if isinstance(row, dict) and row.get("chunk_id")}
    citations = result.get("citations") or []
    output = []
    for citation in citations:
        chunk_id = citation.get("chunk_id") if isinstance(citation, dict) else citation
        if not isinstance(chunk_id, str) or chunk_id not in known:
            raise ChatFailure("MODEL_INVALID_RESPONSE", False,
                              "模型响应引用了本轮证据集之外的政策条款。")
        row = known[chunk_id]
        citation_refs = citation.get("result_refs", []) if isinstance(citation, dict) else []
        output.append({key: row.get(key) for key in
            ("chunk_id", "title", "content", "version", "policy_version", "policy_document_version",
             "effective_from", "effective_to", "knowledge_version", "index_fingerprint", "evaluation_at",
             "source_type") if key in row} | {
                 "result_refs": list(citation_refs) if isinstance(citation_refs, list) else [],
                 "retrieval_call_ids": list(citation.get("retrieval_call_ids", [])) if isinstance(citation, dict) else []})
    if result.get("answer_kind") == "POLICY_ANSWER" and evidence and not output:
        raise ChatFailure("MODEL_INVALID_RESPONSE", False,
                          "政策回答未包含可核验引用，本次未保存为成功答案。")
    return output


def _result_payload(result: dict, route: dict, trace_id: str) -> dict:
    answer = result.get("answer")
    if not isinstance(answer, str) or not answer.strip():
        raise ChatFailure("MODEL_INVALID_RESPONSE", False, "执行器没有返回可保存的答复。")
    strategy = route.get("strategy")
    if strategy not in {"DIRECT", "SINGLE", "MULTI", "REFUSE"}:
        raise ChatFailure("ROUTE_EXECUTOR_UNAVAILABLE", False, _SAFE_FAILURES["ROUTE_EXECUTOR_UNAVAILABLE"])
    citations = _citation_payload(result)
    payload = {
        "answer": answer,
        "answer_kind": result.get("answer_kind") or ("DIRECT" if strategy == "DIRECT" else "REFUSAL" if strategy == "REFUSE" else "POLICY_ANSWER"),
        "citations": citations,
        "strategy": strategy,
        "sub_strategy": route.get("sub_strategy"),
        "route": route,
        "trace_id": trace_id,
        "model_id": result.get("model_id"),
        "thinking": result.get("thinking"),
        "usage": result.get("budget") or result.get("usage"),
        "latency_ms": result.get("latency_ms"),
        "estimated_cost_usd": result.get("estimated_cost_usd"),
        # Explicit empty evidence/audit collections make deterministic zero-tool
        # DIRECT/REFUSE paths distinguishable from missing archive fields.
        "tool_references": result.get("tool_references", []),
        "business_evidence": result.get("business_evidence", []),
        "tool_call_audit": result.get("tool_call_audit", []),
    }
    for key in ("knowledge_version", "retrieval", "policy_retrieval", "policy_retrieval_audit", "grade_result", "stop_reason",
                "budget", "run_budget", "retrieval_hop_count"):
        if key in result:
            payload[key] = result[key]
    if isinstance(result.get("evidence"), list) and "evidence" not in payload:
        payload["evidence"] = [{key: row.get(key) for key in
            ("chunk_id", "title", "version", "policy_version", "policy_document_version",
             "effective_from", "effective_to", "knowledge_version", "index_fingerprint",
             "evaluation_at", "source_type") if key in row}
            for row in result["evidence"] if isinstance(row, dict)]
    if isinstance(result.get("self_rag"), dict):
        payload["self_rag"] = result["self_rag"]
        raw = result["self_rag"]
        payload.setdefault("evidence", [{key: row.get(key) for key in
            ("chunk_id", "title", "version", "policy_version", "policy_document_version",
             "effective_from", "effective_to", "knowledge_version", "index_fingerprint",
             "evaluation_at", "source_type") if key in row}
            for row in raw.get("evidence", []) if isinstance(row, dict)])
        if raw.get("budget"):
            payload["usage"] = raw["budget"]
            payload["latency_ms"] = raw["budget"].get("latency_ms")
            payload["estimated_cost_usd"] = raw["budget"].get("estimated_cost_usd")
    return payload


def _agent_business_payload(result: dict, route: dict, trace_id: str) -> dict:
    answer = result.get("answer")
    if route.get("strategy") != "AGENT" or result.get("answer_kind") not in {
            "BUSINESS_FACTS", "POLICY_ANSWER", "CLARIFICATION_REQUIRED"}:
        raise ChatFailure("AGENT_RESULT_NOT_USER_ANSWERABLE", False, _SAFE_FAILURES["AGENT_RESULT_NOT_USER_ANSWERABLE"])
    if not isinstance(answer, str) or not answer.strip():
        raise ChatFailure("AGENT_RESULT_NOT_USER_ANSWERABLE", False, _SAFE_FAILURES["AGENT_RESULT_NOT_USER_ANSWERABLE"])
    handoffs = []
    for item in result.get("handoffs", []) if isinstance(result.get("handoffs"), list) else []:
        if not isinstance(item, dict):
            continue
        handoffs.append({key: item.get(key) for key in
            ("handoff_id", "source_agent", "target_agent", "required_capabilities", "context_refs",
             "evidence_refs", "status", "error_code") if key in item})
    return {"answer": answer, "answer_kind": result["answer_kind"],
        "citations": result.get("citations", []), "evidence": result.get("evidence", []),
        "knowledge_version": result.get("knowledge_version"),
        "retrieval": result.get("retrieval"), "policy_retrieval": result.get("policy_retrieval"),
        "policy_retrieval_audit": result.get("policy_retrieval_audit", []),
        "strategy": "AGENT", "sub_strategy": None, "route": route, "trace_id": trace_id,
        "business_results": result.get("business_results", {}),
        "business_evidence": result.get("business_evidence", []),
        "tool_references": result.get("tool_references", []),
        "tool_execution_status": result.get("tool_execution_status", []),
        "tool_call_audit": result.get("tool_call_audit", []),
        "handoffs": handoffs,
        "model_id": result.get('model_id'), "usage": result.get('usage'), "latency_ms": result.get("latency_ms", 0),
        "estimated_cost_usd": result.get('estimated_cost_usd',0.0)}


def delivered_order_results(db: Session, *, owner_id: str, session_id: str, current_run_id: str):
    """Read the bounded persisted delivery boundary once; rejected output is excluded."""
    owned = db.get(ConversationSession, session_id)
    if owned is None or owned.user_id != owner_id:
        return
    messages = set(db.scalars(select(Message.content).where(Message.session_id == session_id,
        Message.role == "assistant").order_by(Message.created_at.desc()).limit(40)))
    previous = db.scalars(select(Run).where(Run.user_id == owner_id, Run.session_id == session_id,
        Run.run_id != current_run_id, Run.status == "SUCCEEDED").order_by(Run.created_at.desc()).limit(20))
    for old in previous:
        if old.result_json is None:
            continue
        value = json.loads(old.result_json)
        if value["answer"] in messages and value.get("business_results"):
            yield value["business_results"]


def delivered_order_query(db: Session, *, owner_id: str, session_id: str,
                          current_run_id: str, query: str) -> str:
    """Resolve a resource hint from delivered, same-owner session facts only.

    This never grants access or carries eligibility forward. Every new tool call
    refreshes the selected resource under the authenticated owner predicate.
    """
    reference = re.search(r"第[一二三四五1-5]个|这笔|这个订单|这件|这个商品|该商品|该订单|这单|它", query)
    general_policy = re.search(r"一般|通常|通用|所有|普通商品|政策|条款|规则", query)
    contextual_return = is_return_guidance_query(query) and not general_policy
    if requested_order_id(query) != (None, None) or not (reference or contextual_return):
        return query
    previous = delivered_order_results(db, owner_id=owner_id, session_id=session_id, current_run_id=current_run_id)
    ordinal = re.search(r"第([一二三四五1-5])个", query)
    for facts in previous:
        target = None
        if ordinal:
            listing = facts.get("list_orders")
            if listing is None:
                continue
            index = "一二三四五".find(ordinal[1]) if not ordinal[1].isdigit() else int(ordinal[1]) - 1
            rows = listing["orders"]
            if index >= len(rows):
                return query  # Never resolve against an older, longer list.
            target = rows[index]["order_id"]
        elif "get_order" in facts:
            target = facts["get_order"]["order_id"]
        elif "order_logistics" in facts and "get_order" in facts["order_logistics"]:
            target = facts["order_logistics"]["get_order"]["order_id"]
        elif "list_orders" in facts:
            rows = facts["list_orders"]["orders"]
            if len(rows) != 1:
                return query  # A list of several orders is not a selection.
            target = rows[0]["order_id"]
        if target:
            if re.fullmatch(r"(?:第[一二三四五1-5]个|这笔|这个订单|这单)[。！! ]*", query.strip()):
                return f"请查询订单 {target} 的状态。"
            return query + f"\n当前问题关联的本人订单：{target}。"
    return query


def execute_claimed_chat_langgraph(db: Session, *, run: Run, user_id: str, message: str, trace_id: str,
                                   provider, index_path, retrieval_service, evaluation_at,
                                   self_rag_config, settings, session_factory: sessionmaker,
                                   database_url: str | None = None, checkpointer_factory=None,
                                   graph_services: GraphServices | None = None,
                                   read_repositories=None) -> dict:
    """Execute once behind the existing atomic Run claim; no graph replay is performed here."""
    execution_query = delivered_order_query(db, owner_id=user_id, session_id=run.session_id,
        current_run_id=run.run_id, query=message)
    context = GraphRuntimeContext(query=execution_query, m3_supervisor_enabled=bool(settings.m3_supervisor_enabled),
        tool_result_store={}, tool_execution_records=[], retrieval_service=retrieval_service,
        retrieval_evaluation_at=evaluation_at)
    memory_plan = None
    if settings.m3_supervisor_enabled:
        from app.m3.contracts import TrustedAgentContext
        from app.m4.actions import server_draft_permission_granted
        context.escalation_permission_checker = server_draft_permission_granted
        owned_session = db.get(ConversationSession, run.session_id)
        owner_context_valid = (run.user_id == user_id and owned_session is not None and owned_session.user_id == user_id)
        order_id, order_id_error = requested_order_id(execution_query)
        list_before = None
        if re.search(r"订单.{0,8}下一页", message):
            for facts in delivered_order_results(db, owner_id=user_id, session_id=run.session_id, current_run_id=run.run_id):
                if "list_orders" in facts:
                    list_before = facts["list_orders"]["next_before"]
                    break
        context.trusted_agent_context = TrustedAgentContext(user_id=user_id, session_id=run.session_id,
            run_id=run.run_id, trace_id=trace_id, authenticated=True,
            run_owner_verified=owner_context_valid, session_owner_verified=owner_context_valid,
            requested_order_id=order_id, order_id_error=order_id_error, order_list_before=list_before)
    graph_record_started = False
    checkpoint_id = None
    sink = SQLAlchemyObservabilitySink(session_factory, user_id=user_id, run_id=run.run_id,
        trace_id=trace_id, graph_version=settings.graph_version)
    context.observability_sink = sink

    def emit_event(event_type: str, payload: dict, event_key: str) -> None:
        with session_factory() as event_db:
            append_run_event(event_db, run.run_id, trace_id, event_type, payload, event_key=event_key)

    context.emit_event = emit_event
    try:
        add_user_message(db, user_id, run.session_id, message)
        memory_plan = prepare_memory_context(db, owner_id=user_id, session_id=run.session_id,
                                             current_query=message, run_id=run.run_id)
        if memory_plan.get("context_budget_exceeded"):
            # Inspect only the deterministic route. Ambiguous classification and
            # policy routes would call a model, so stop those before invoking the
            # graph; DIRECT/REFUSE and read-only AGENT paths do not need the
            # oversized prose projection to authorize or execute their contract.
            from app.m1.routing import decide_route
            deterministic_route = decide_route(message)
            if (deterministic_route.decision_source == "FALLBACK" or
                    deterministic_route.strategy in {"SINGLE", "MULTI"}):
                raise ChatFailure("CONTEXT_BUDGET_EXCEEDED", False,
                    _SAFE_FAILURES["CONTEXT_BUDGET_EXCEEDED"],
                    {"estimated_tokens_after": memory_plan.get("estimated_tokens_after"),
                     "effective_context_tokens": memory_plan.get("effective_context_tokens")})
        context.memory_context = memory_plan.get("memory_context", "")
        state_schema_version = "g2-m3.3-state-v1" if settings.m3_supervisor_enabled else "g2-state-v1"
        begin_graph_execution(db, run, graph_version=settings.graph_version,
                              execution_version=settings.execution_version,
                              state_schema_version=state_schema_version)
        graph_record_started = True
        active_provider = provider or DeepSeekProvider()
        services = graph_services or make_legacy_services(provider=active_provider,
            retrieval_service=retrieval_service, index_path=index_path, evaluation_at=evaluation_at,
            classifier=active_provider, self_rag_config=self_rag_config)
        if graph_services is not None:
            # The supervisor closure captures this Run's sink, tool result store and
            # event callback; never mutate the app-wide injected service registry.
            services = dataclass_replace(graph_services)
        if settings.m3_supervisor_enabled and services.agent_supervisor is None:
            from app.m3.agents import default_agent_registry
            from app.m3.graph import build_supervisor_graph, invoke_supervisor
            supplied_reads = read_repositories or {}
            repository = supplied_reads.get("order") or SqlAlchemyOrderReadRepository(session_factory)
            from app.m3.after_sales_tools import AfterSalesReadRepository
            after_sales_repository = supplied_reads.get("after_sales") or AfterSalesReadRepository(session_factory)
            from app.m3.product_tools import ProductReadRepository
            product_repository = supplied_reads.get("product") or ProductReadRepository(session_factory)
            from app.m3.policy_tools import PolicyReadService
            from app.m3.policy_tools import CouponReadRepository
            policy_service = supplied_reads.get("policy") or PolicyReadService(retrieval_service, evaluation_at=evaluation_at)
            supervisor_graph = build_supervisor_graph(default_agent_registry(
                order_repository=repository, product_repository=product_repository,
                policy_service=policy_service, after_sales_repository=after_sales_repository,
                coupon_repository=supplied_reads.get("coupon") or CouponReadRepository(session_factory)), sink=sink)
            services.agent_supervisor = lambda state, graph_context: invoke_supervisor(
                supervisor_graph, state, graph_context)
        input_value = GraphInput(run_id=run.run_id, session_id=run.session_id,
                                 trace_id=trace_id, query=execution_query)
        saver_factory = checkpointer_factory or open_checkpointer

        async def invoke() -> tuple[dict, str | None]:
            async with saver_factory(database_url or settings.database_url or "", settings.checkpoint_schema) as saver:
                graph = build_orchestration_graph(services, sink=sink, checkpointer=saver)
                config = {"configurable": {"thread_id": run.run_id, "checkpoint_ns": ""},
                          "metadata": {"run_id": run.run_id, "graph_version": settings.graph_version,
                                       "execution_version": settings.execution_version}}
                result = await graph.ainvoke(graph_input_state(input_value, graph_version=settings.graph_version), context=context,
                                             config=config, durability="sync")
                snapshot = await graph.aget_state(config)
                snapshot_config = getattr(snapshot, "config", None) or {}
                saved_id = (snapshot_config.get("configurable") or {}).get("checkpoint_id")
                return dict(result), saved_id

        graph_state, checkpoint_id = run_checkpointer_async(invoke())
        update_graph_execution(db, run.run_id, status="RUNNING", checkpoint_id=checkpoint_id)
        route = context.route_payload or {}
        result = context.execution_result or {}
        failure_code = context.failure_code or graph_state.get("error_code") or result.get("error_code")
        terminal = result.get("terminal_status") or graph_state.get("terminal_status")
        agent_payload = None
        if route.get("strategy") == "AGENT" and settings.m3_supervisor_enabled and terminal == "SUCCEEDED":
            agent_payload = _agent_business_payload(result, route, trace_id)
        elif route.get("strategy") == "AGENT" or failure_code == "ROUTE_EXECUTOR_UNAVAILABLE":
            code = failure_code or result.get("error_code") or "ROUTE_EXECUTOR_UNAVAILABLE"
            if not settings.m3_supervisor_enabled:
                code = "ROUTE_EXECUTOR_UNAVAILABLE"
            raise ChatFailure(code, False, _SAFE_FAILURES.get(code, _SAFE_FAILURES["ROUTE_EXECUTOR_UNAVAILABLE"]),
                              context.failure_diagnostic or
                              {"route": route, "supervisor_status": result.get("supervisor_status")})
        if terminal == "FAILED" or failure_code:
            code = failure_code or "GRAPH_EXECUTION_FAILED"
            raise ChatFailure(code, context.failure_retryable, _SAFE_FAILURES.get(code, "本次执行未通过安全校验，没有保存成功答案。"),
                              context.failure_diagnostic or result.get("error_diagnostic"))

        result_payload = agent_payload if agent_payload is not None else _result_payload(result, route, trace_id)
        result_payload["context_memory"] = {key: memory_plan.get(key) for key in
            ("level", "estimated_tokens_before", "estimated_tokens_after", "compression_ratio",
             "effective_context_tokens", "thresholds", "summary_generation", "summary_model_calls",
             "summary_cost_estimate_usd", "summary_latency_ms", "context_budget_exceeded",
             "context_budget_overage_tokens", "context_budget_handling")}
        result_payload["context_memory"].update({"summary_persisted": None,
            "summary_version": memory_plan.get("summary_version"),
            "source_message_count": len(memory_plan.get("source_message_ids") or [])})
        memory_plan["usage_calibration"] = usage_calibration(memory_plan, result_payload.get("usage"))
        result_payload["context_memory"].update(memory_plan["usage_calibration"])
        model_id = result_payload.get("model_id") or (route.get("model_id") if route.get("decision_source") == "LLM" else None)
        usage = result_payload.get("usage")
        latency = result_payload.get("latency_ms")
        sink.record_metrics(MetricRecord(run_status="SUCCEEDED", graph_version=settings.graph_version))
        finish_run(db, run, status="SUCCEEDED", result=result_payload, model_id=model_id,
                   usage=usage, latency_ms=latency, assistant_message=result_payload["answer"], trace_id=trace_id,
                   graph_execution_status="SUCCEEDED", graph_checkpoint_id=checkpoint_id)
        persisted, version = persist_auto_summary(db, owner_id=user_id, session_id=run.session_id,
            run_id=run.run_id, plan=memory_plan)
        result_payload["context_memory"].update({"summary_persisted": persisted, "summary_version": version})
        try:
            persist_memory_trace(db, user_id=user_id, run_id=run.run_id, trace_id=trace_id,
                                 plan=memory_plan, persist_ok=persisted, summary_version=version)
        except Exception:
            db.rollback()
        run.result_json = json.dumps(result_payload, ensure_ascii=False, sort_keys=True)
        db.commit()
        return {"status": "SUCCEEDED", "result": result_payload, "failure": None}
    except ChatFailure as exc:
        provider_exception = (exc.diagnostic or {}).get("provider_exception") if isinstance(exc.diagnostic, dict) else None
        ambiguous = provider_exception in _AMBIGUOUS_PROVIDER_EXCEPTIONS or exc.code == "CHECKPOINT_PERSISTENCE_FAILED"
        unavailable = exc.code in {"MODEL_UNAVAILABLE", "MODEL_AUTHENTICATION_FAILED", "MODEL_RATE_LIMITED"}
        status = "UNKNOWN_RESULT" if ambiguous else "MODEL_UNAVAILABLE" if unavailable else "FAILED"
        graph_status = "UNKNOWN" if ambiguous else "FAILED"
        if graph_record_started:
            sink.record_metrics(MetricRecord(run_status=graph_status, graph_version=settings.graph_version))
        error_detail = dict(exc.diagnostic) if isinstance(exc.diagnostic, dict) else {}
        scope = error_detail.get("route_capability_scope")
        if isinstance(scope, dict) and scope.get("schema_version") == "route-capability-scope-diagnostic-v1":
            error_detail["route_capability_scope"] = {**scope, "run_id": run.run_id, "trace_id": trace_id}
        finish_run(db, run, status=status, error_code=exc.code,
            error_detail=error_detail or None, trace_id=trace_id,
            graph_execution_status=graph_status if graph_record_started else None,
            graph_checkpoint_id=checkpoint_id)
        if memory_plan is not None:
            try:
                persist_memory_trace(db, user_id=user_id, run_id=run.run_id, trace_id=trace_id,
                                     plan=memory_plan, persist_ok=False, summary_version=None)
            except Exception:
                db.rollback()
        if error_detail:
            exc.diagnostic = error_detail
        return {"status": status, "result": None, "failure": exc}
    except SQLAlchemyError:
        db.rollback()
        code = "PERSISTENCE_FAILED"
        if graph_record_started:
            try:
                sink.record_metrics(MetricRecord(run_status="UNKNOWN", graph_version=settings.graph_version))
                finish_run(db, run, status="UNKNOWN_RESULT", error_code=code,
                error_detail={"reason": "database persistence failed during graph execution"}, trace_id=trace_id,
                    graph_execution_status="UNKNOWN", graph_checkpoint_id=checkpoint_id)
            except SQLAlchemyError:
                db.rollback()
        if memory_plan is not None:
            try:
                persist_memory_trace(db, user_id=user_id, run_id=run.run_id, trace_id=trace_id,
                                     plan=memory_plan, persist_ok=False, summary_version=None)
            except Exception:
                db.rollback()
        return {"status": "UNKNOWN_RESULT" if graph_record_started else "FAILED", "result": None,
                "failure": ChatFailure(code, True, "运行状态暂未能安全保存，请查询运行状态。")}
    except Exception as exc:
        db.rollback()
        error_code = context.failure_code or getattr(exc, "code", None) or "GRAPH_EXECUTION_FAILED"
        ambiguous = bool(context.model_request_count) or error_code in {"MODEL_UNAVAILABLE", "CHECKPOINT_PERSISTENCE_FAILED"}
        status = "UNKNOWN_RESULT" if ambiguous else "FAILED"
        graph_status = "UNKNOWN" if ambiguous else "FAILED"
        error_detail = {"exception_type": type(exc).__name__}
        if graph_record_started:
            try:
                failure_diagnostic = context.failure_diagnostic
                if isinstance(failure_diagnostic, dict):
                    scope = failure_diagnostic.get("route_capability_scope")
                    if isinstance(scope, dict) and scope.get("schema_version") == "route-capability-scope-diagnostic-v1":
                        error_detail["route_capability_scope"] = {**scope,
                            "run_id": run.run_id, "trace_id": trace_id}
                if isinstance(exc, ModuleNotFoundError) and isinstance(getattr(exc, "name", None), str):
                    # Module names are deployment diagnostics, not secrets. Preserve
                    # the failing import without exposing raw exception text.
                    error_detail["missing_module"] = exc.name[:128]
                sink.record_metrics(MetricRecord(run_status=graph_status, graph_version=settings.graph_version))
                finish_run(db, run, status=status, error_code=error_code,
                    error_detail=error_detail, trace_id=trace_id,
                    graph_execution_status=graph_status, graph_checkpoint_id=checkpoint_id)
            except SQLAlchemyError:
                db.rollback()
        if memory_plan is not None:
            try:
                persist_memory_trace(db, user_id=user_id, run_id=run.run_id, trace_id=trace_id,
                                     plan=memory_plan, persist_ok=False, summary_version=None)
            except Exception:
                db.rollback()
        failure_detail = {"exception_type": type(exc).__name__}
        if isinstance(error_detail, dict) and "route_capability_scope" in error_detail:
            failure_detail["route_capability_scope"] = error_detail["route_capability_scope"]
        return {"status": status, "result": None,
                "failure": ChatFailure(error_code, False, "图执行未能完成；没有保存成功答案。",
                                        failure_detail)}
