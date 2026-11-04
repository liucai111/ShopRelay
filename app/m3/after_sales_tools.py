"""Owner-scoped, read-only after-sales status and evidence evaluation."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.m1.models import AfterSalesEvent, Order, Refund, ReturnRequest
from .policy_tools import evidence_is_valid
from .order_tools import ToolExecutionError


class AfterSalesReadRepository:
    """Read actual return/refund rows only after verifying order ownership."""

    def __init__(self, factory: sessionmaker) -> None:
        self.factory = factory

    def get_refund_status(self, order_id: str, owner_id: str) -> dict[str, Any]:
        try:
            with self.factory() as db:
                owned = db.execute(select(Order.order_id).where(
                    Order.order_id == order_id, Order.user_id == owner_id)).scalar_one_or_none()
                if owned is None:
                    raise ToolExecutionError("ORDER_NOT_FOUND", status="NOT_FOUND")
                available_tables = set(inspect(db.get_bind()).get_table_names())
                required_tables = {Refund.__tablename__, ReturnRequest.__tablename__}
                if not required_tables.issubset(available_tables):
                    return {
                        "source": "shoprelay_after_sales_database",
                        "order_id": order_id,
                        "availability": "NOT_AVAILABLE",
                        "error_code": "AFTER_SALES_STATUS_SOURCE_UNAVAILABLE",
                        "missing_sources": sorted(required_tables - available_tables),
                        "refunds": None,
                        "returns": None,
                    }
                refunds = db.execute(select(Refund).where(Refund.order_id == order_id).order_by(
                    Refund.created_at.desc(), Refund.refund_id.desc())).scalars().all()
                returns = db.execute(select(ReturnRequest).where(ReturnRequest.order_id == order_id).order_by(
                    ReturnRequest.created_at.desc(), ReturnRequest.return_id.desc())).scalars().all()
                events = db.execute(select(AfterSalesEvent).where(
                    AfterSalesEvent.order_id == order_id).order_by(
                    AfterSalesEvent.occurred_at, AfterSalesEvent.event_id)).scalars().all()
                events_by_aggregate: dict[tuple[str, str], list[AfterSalesEvent]] = {}
                for event in events:
                    key = ("return", event.return_id) if event.return_id else ("refund", event.refund_id)
                    events_by_aggregate.setdefault(key, []).append(event)
                return_rows = [self._return(row, events_by_aggregate.get(("return", row.return_id), []))
                               for row in returns]
                return_by_id = {row["request_id"]: row for row in return_rows}
                refund_rows = [self._refund(row, events_by_aggregate.get(("refund", row.refund_id), []))
                               for row in refunds]
                for row in refund_rows:
                    linked = return_by_id.get(row["return_id"]) if row.get("return_id") else None
                    row["linked_return_status"] = linked["status"] if linked else None
                    row["linked_return_verified"] = bool(linked and linked["verified"])
                    # A refund linked to a return must point to an audited return
                    # on this same owner-checked order. Missing/mismatched links
                    # make the refund record unusable as a verified business fact.
                    if row["return_id"] and not row["linked_return_verified"]:
                        row["verified"] = False
                        row["verification_state"] = "UNVERIFIED"
                        row["status"] = "UNKNOWN"
                open_refunds = [row for row in refund_rows if row["verified"] and row["status"] in
                                {"SUBMITTED", "UNDER_REVIEW", "APPROVED", "PROCESSING"}]
                open_returns = [row for row in return_rows if row["verified"] and row["status"] in
                                {"SUBMITTED", "UNDER_REVIEW", "APPROVED"}]
                return {
                    "source": "shoprelay_after_sales_database",
                    "order_id": order_id,
                    "observed_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "availability": "AVAILABLE",
                    "application_record_state": ("NO_APPLICATION_RECORD" if not refunds and not returns
                        else "PRESENT" if all(item["verified"] for item in return_rows + refund_rows)
                        else "UNKNOWN"),
                    "refunds": {
                        "active_request": open_refunds[0] if open_refunds else None,
                        "latest_request": refund_rows[0] if refund_rows else None,
                    },
                    "returns": {
                        "active_request": open_returns[0] if open_returns else None,
                        "latest_request": return_rows[0] if return_rows else None,
                    },
                }
        except ToolExecutionError:
            raise
        except SQLAlchemyError as exc:
            raise ToolExecutionError("AFTER_SALES_DATABASE_UNAVAILABLE") from exc

    @staticmethod
    def _verified_history(kind: str, row: Any, events: list[AfterSalesEvent]) -> tuple[bool, str | None, list[str]]:
        """Verify state from matching, ordered audit events; a row status alone is never proof."""
        if row.source_type not in {"BUSINESS_SYSTEM", "SIMULATED_TEST_DATA"} or not row.source_ref:
            return False, None, []
        transitions = {
            "return": {
                "SUBMITTED": {"UNDER_REVIEW", "CANCELLED"},
                "UNDER_REVIEW": {"APPROVED", "REJECTED", "CANCELLED"},
                "APPROVED": {"CLOSED"},
                "REJECTED": {"CLOSED"},
            },
            "refund": {
                "SUBMITTED": {"UNDER_REVIEW", "CANCELLED"},
                "UNDER_REVIEW": {"APPROVED", "REJECTED", "CANCELLED"},
                "APPROVED": {"PROCESSING"},
                "PROCESSING": {"COMPLETED", "FAILED"},
            },
        }[kind]
        expected_prefix = "RETURN_" if kind == "return" else "REFUND_"
        state = None
        used: list[str] = []
        latest_at = None
        for event in sorted(events, key=lambda item: (item.occurred_at, item.event_id)):
            expected_aggregate_id = row.return_id if kind == "return" else row.refund_id
            actual_aggregate_id = event.return_id if kind == "return" else event.refund_id
            is_payment_confirmation = (kind == "refund" and event.event_type == "REFUND_COMPLETED" and
                event.source_type == "PAYMENT_PROVIDER" and event.actor_type == "PAYMENT_PROVIDER")
            if (event.order_id != row.order_id or actual_aggregate_id != expected_aggregate_id or
                    (event.source_type != row.source_type and not is_payment_confirmation)):
                return False, None, []
            if ((event.source_ref != row.source_ref and not is_payment_confirmation) or
                    not event.event_type.startswith(expected_prefix)):
                return False, None, []
            if event.occurred_at.tzinfo is None or event.recorded_at.tzinfo is None:
                return False, None, []
            suffix = event.event_type.removeprefix(expected_prefix)
            next_state = {"REVIEW_STARTED": "UNDER_REVIEW"}.get(suffix, suffix)
            if next_state in {"APPROVED", "REJECTED"} and event.actor_type not in {
                    "HUMAN_REVIEWER", "SIMULATED_TEST_DATA"}:
                return False, None, []
            if state is None:
                if next_state != "SUBMITTED":
                    return False, None, []
            elif next_state not in transitions.get(state, set()):
                return False, None, []
            if next_state == "COMPLETED" and not (
                    kind == "refund" and event.actor_type == "PAYMENT_PROVIDER" and
                    event.source_type == "PAYMENT_PROVIDER" and
                    row.payment_confirmation_source == "PAYMENT_PROVIDER" and
                    row.payment_confirmed_at is not None and
                    row.payment_confirmed_at.astimezone(timezone.utc) ==
                    event.occurred_at.astimezone(timezone.utc)):
                return False, None, []
            state = next_state
            latest_at = event.occurred_at
            used.append(event.event_id)
        if state != row.status or latest_at is None:
            return False, None, []
        time_field = {
            "SUBMITTED": "submitted_at", "UNDER_REVIEW": "review_started_at",
            "APPROVED": "approved_at", "REJECTED": "rejected_at", "CANCELLED": "cancelled_at",
            "CLOSED": "closed_at",
            "PROCESSING": "processed_at", "COMPLETED": "processed_at",
            "FAILED": "failed_at",
        }.get(state)
        state_time = getattr(row, time_field, None) if time_field else latest_at
        if state_time is None or state_time.astimezone(timezone.utc) != latest_at.astimezone(timezone.utc):
            return False, None, []
        return True, state, used

    @staticmethod
    def _refund(row: Refund, events: list[AfterSalesEvent]) -> dict[str, Any]:
        verified, state, event_ids = AfterSalesReadRepository._verified_history("refund", row, events)
        return {"request_id": row.refund_id, "status": state or "UNKNOWN", "verified": verified,
                "verification_state": "VERIFIED" if verified else "UNVERIFIED",
                "amount_cents": row.amount_cents, "created_at": row.created_at,
                "submitted_at": row.submitted_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    if row.submitted_at else None,
                "return_id": row.return_id, "source_type": row.source_type,
                "source_ref": row.source_ref, "event_ids": event_ids,
                "payment_settlement_verified": bool(verified and state == "COMPLETED" and
                    row.payment_confirmation_source == "PAYMENT_PROVIDER" and row.payment_confirmed_at)}

    @staticmethod
    def _return(row: ReturnRequest, events: list[AfterSalesEvent]) -> dict[str, Any]:
        verified, state, event_ids = AfterSalesReadRepository._verified_history("return", row, events)
        return {"request_id": row.return_id, "status": state or "UNKNOWN", "verified": verified,
                "verification_state": "VERIFIED" if verified else "UNVERIFIED",
                "created_at": row.created_at,
                "submitted_at": row.submitted_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    if row.submitted_at else None,
                "approved_at": row.approved_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
                    if row.approved_at else None,
                "source_type": row.source_type, "source_ref": row.source_ref,
                "evidence_ref": row.evidence_ref, "event_ids": event_ids}


def evaluate_policy_conditions(order: dict[str, Any], after_sales: dict[str, Any],
                               policy: dict[str, Any], *, query: str) -> dict[str, Any]:
    """Conservative deterministic evaluation; no user claim becomes a verified fact."""
    evidence = [row for row in policy.get("evidence", []) if isinstance(row, dict)]
    citations = [{key: row[key] for key in (
        "chunk_id", "policy_id", "clause_id", "title", "content", "version", "policy_version",
        "effective_from", "effective_to", "knowledge_version", "index_fingerprint", "rank", "score")
        if key in row} for row in evidence]
    asked_quality = any(token in query for token in ("质量", "瑕疵", "故障", "缺陷"))
    asked_refund = "退款" in query
    conditions: list[dict[str, Any]] = []

    def source_row(markers: tuple[str, ...]) -> dict[str, Any] | None:
        for row in evidence:
            content = str(row.get("content", ""))
            if any(marker in content for marker in markers):
                return row
        return None

    order_id = order.get("order_id")
    delivered_at = order.get("delivered_at")
    policy_matches_paid_at = False
    paid_at = order.get("paid_at")
    if paid_at:
        try:
            paid_dt = datetime.fromisoformat(str(paid_at).replace("Z", "+00:00"))
            policy_matches_paid_at = any(
                (not row.get("effective_from") or datetime.fromisoformat(
                    str(row["effective_from"]).replace("Z", "+00:00")) <= paid_dt) and
                (not row.get("effective_to") or paid_dt < datetime.fromisoformat(
                    str(row["effective_to"]).replace("Z", "+00:00"))) for row in evidence)
        except (TypeError, ValueError):
            policy_matches_paid_at = False

    def source_matches_payment(row: dict[str, Any] | None) -> bool:
        if not row or not paid_at:
            return False
        try:
            paid_dt = datetime.fromisoformat(str(paid_at).replace("Z", "+00:00"))
            start = datetime.fromisoformat(str(row["effective_from"]).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(row["effective_to"]).replace("Z", "+00:00")) if row.get("effective_to") else None
            return start <= paid_dt and (end is None or paid_dt < end)
        except (KeyError, TypeError, ValueError):
            return False

    if asked_quality:
        clause_row = source_row(("质量问题", "质量", "30天", "30 天"))
        clause = str(clause_row.get("chunk_id")) if clause_row else None
        conditions.append({
            "condition_id": "quality_return_window",
            "policy_source_id": clause,
            "required_fact": "verified_quality_issue_time_and_delivery_time",
            "fact_source": "BUSINESS_TOOL",
            "evaluation_result": "UNKNOWN",
            "missing_information": ["QUALITY_ISSUE_TIME_NOT_VERIFIED"] if delivered_at else
                ["DELIVERY_TIME_NOT_VERIFIED", "QUALITY_ISSUE_TIME_NOT_VERIFIED"],
            "explanation": "订单工具没有质量问题发生时间/证明字段；用户陈述不能替代业务核验。对应政策证据还必须覆盖付款时点。",
        })
        conditions.append({
            "condition_id": "return_manual_review",
            "policy_source_id": clause,
            "required_fact": "manual_return_review_outcome",
            "fact_source": "POLICY",
            "evaluation_result": "POLICY_REQUIRES_MANUAL_REVIEW" if clause and source_matches_payment(clause_row) else "UNKNOWN",
            "missing_information": [] if clause and source_matches_payment(clause_row) else ["VALID_RETURN_POLICY_AT_PAYMENT_MISSING"],
            "explanation": "政策证据支持提交申请后由人工审核，不代表批准。",
        })
    if asked_refund:
        clause_row = source_row(("已批准的退货", "已批准退货", "已签收订单", "已付款且未发货"))
        delivered = order.get("status") == "DELIVERED" or bool(delivered_at)
        if not delivered and order.get("status") == "PAID":
            clause_row = source_row(("已付款且未发货", "未发货订单可申请退款", "付款后且发货前"))
        clause = str(clause_row.get("chunk_id")) if clause_row else None
        return_data = after_sales.get("returns") or {}
        latest_return = return_data.get("latest_request") or {}
        approved_return = latest_return.get("verified") is True and latest_return.get("status") == "APPROVED"
        if delivered:
            if after_sales.get("availability") == "NOT_AVAILABLE":
                result, missing = "UNKNOWN", ["AFTER_SALES_STATUS_SOURCE_UNAVAILABLE"]
            elif after_sales.get("application_record_state") == "UNKNOWN":
                result, missing = "UNKNOWN", ["AFTER_SALES_RECORD_PROVENANCE_UNVERIFIED"]
            else:
                result = "SATISFIED" if approved_return else "NOT_SATISFIED"
                missing = [] if approved_return else ["APPROVED_RETURN_NOT_VERIFIED"]
            condition_id = "refund_requires_approved_return_when_delivered"
            required_fact = "verified_approved_return_request_for_delivered_order"
            explanation = "已签收订单申请退款须有已审计确认的已批准退货申请；这不等于退款获批或到账。"
        elif order.get("status") == "PAID":
            result = "SATISFIED" if order.get("paid_at") else "UNKNOWN"
            missing = [] if order.get("paid_at") else ["PAYMENT_TIME_NOT_VERIFIED"]
            condition_id = "refund_request_for_paid_unshipped_order"
            required_fact = "verified_paid_and_not_shipped_order"
            explanation = "已核实订单仍处于已付款未发货阶段及有效政策时，表示满足该政策所述申请前置条件，不代表退款已批准或到账。"
        else:
            result, missing = "UNKNOWN", ["CURRENT_ORDER_STAGE_NOT_CONFIRMED_FOR_REFUND_RULE"]
            condition_id = "refund_application_stage"
            required_fact = "verified_order_stage"
            explanation = "订单阶段无法映射到已核实的退款前置条件。"
        if not clause or not source_matches_payment(clause_row):
            result, missing = "UNKNOWN", ["POLICY_VERSION_AT_PAYMENT_NOT_VERIFIED"]
        conditions.append({
            "condition_id": condition_id,
            "policy_source_id": clause,
            "required_fact": required_fact,
            "fact_source": "BUSINESS_TOOL",
            "evaluation_result": result,
            "missing_information": missing,
            "explanation": explanation,
        })
    return {
        "source": "shoprelay_after_sales_evaluator",
        "order_id": order_id,
        "order_status": order.get("status"),
        "delivered_at": delivered_at,
        "policy_version_at_payment_verified": policy_matches_paid_at,
        "conditions": conditions,
        "citations": citations,
        "after_sales_status": after_sales,
        "user_claims_used_as_verified_facts": False,
    }


def build_after_sales_business_result(supervisor_result: dict[str, Any], *, route: dict[str, Any],
                                      result_store: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Render a three-specialist answer only from this run's validated tool payloads."""
    caps = set(route.get("required_capabilities") or [])
    if caps == {"escalate_to_human"}:
        handoffs = supervisor_result.get("handoffs") or []
        if supervisor_result.get("supervisor_status") != "COMPLETED" or len(handoffs) != 1:
            return None
        handoff = handoffs[0]
        agent_result = handoff.get("result") or {}
        refs = agent_result.get("tool_result_refs") or []
        proposal_ref = next((ref for ref in refs if isinstance(ref, str) and
            isinstance(result_store.get(ref), dict) and
            result_store[ref].get("source") == "shoprelay_escalation_draft"), None)
        proposal_payload = result_store.get(proposal_ref) if proposal_ref else None
        if (handoff.get("target_agent") != "after_sales" or handoff.get("status") != "COMPLETED" or
                agent_result.get("source_agent") != "after_sales" or not isinstance(proposal_payload, dict)):
            return None
        draft = proposal_payload.get("draft")
        arguments = proposal_payload.get("pending_action_arguments")
        if (not isinstance(draft, dict) or not isinstance(arguments, dict) or
                draft.get("status") != "DRAFT_ONLY" or draft.get("execution_status") != "NOT_EXECUTED" or
                draft.get("ticket_created") is not False or draft.get("notification_sent") is not False):
            return None
        return {
            "answer": "已生成一份人工协助草案供后续确认；当前尚未创建待确认操作、客服工单或发送通知。",
            "answer_kind": "CLARIFICATION_REQUIRED",
            "business_results": {"escalation_proposal": {
                "draft_ref": proposal_ref,
                "status": "DRAFT_ONLY",
                "execution_status": "NOT_EXECUTED",
                "ticket_created": False,
                "notification_sent": False,
                "pending_action_arguments": arguments,
            }},
            "tool_references": [{"tool": "escalate_to_human", "source": "shoprelay_escalation_draft",
                                 "result_ref": proposal_ref}],
            "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "handoffs": handoffs,
            "citations": [], "evidence": [], "latency_ms": 0,
        }
    # A status-only question has a supported read contract of exactly one
    # owner-checked get_refund_status call. It does not require order facts or
    # policy retrieval, and must still produce a user-facing answer from the
    # actual result reference.
    if caps == {"get_refund_status"}:
        if supervisor_result.get("supervisor_status") != "COMPLETED":
            return None
        handoffs = supervisor_result.get("handoffs") or []
        if (len(handoffs) != 1 or handoffs[0].get("target_agent") != "after_sales" or
                handoffs[0].get("status") != "COMPLETED"):
            return None
        agent_result = handoffs[0].get("result") or {}
        if agent_result.get("source_agent") != "after_sales":
            return None
        refs = agent_result.get("tool_result_refs") or []
        status_ref = next((ref for ref in refs if isinstance(ref, str) and
            isinstance(result_store.get(ref), dict) and
            result_store[ref].get("source") == "shoprelay_after_sales_database"), None)
        status = result_store.get(status_ref) if status_ref else None
        if not isinstance(status, dict) or not isinstance(status.get("order_id"), str):
            return None
        if status.get("availability") == "NOT_AVAILABLE":
            answer = "当前售后记录来源不可用，因此无法核实该退款申请状态。"
        elif status.get("application_record_state") == "NO_APPLICATION_RECORD":
            answer = "售后记录中没有找到该订单的退款或退货申请记录；这不表示款项已退款或已到账。"
        elif status.get("application_record_state") == "UNKNOWN":
            answer = "找到了售后记录，但其来源未能通过核验，因此我无法确认申请状态或退款是否到账。"
        else:
            parts=[]
            for category,label in (('returns','退货'),('refunds','退款')):
                collection=status.get(category)
                if not isinstance(collection,dict):
                    parts.append(f"本次来源未提供可核实的{label}申请状态。")
                    continue
                request=collection.get('latest_request')
                if request is None:
                    parts.append(f"本次查询未找到{label}申请记录。")
                elif request['verified'] is True and isinstance(request['status'],str):
                    parts.append(f"售后记录显示最近{label}申请的记录状态为 {request['status']}。")
                else:
                    parts.append(f"存在{label}申请记录，但其状态来源未通过核验；状态保持未知。")
            answer=''.join(parts)+"退货申请、审核、批准与退款完成或款项到账是不同阶段，不能相互替代。"
        tool_ref = {"tool": "get_refund_status", "source": "shoprelay_after_sales_database",
            "result_ref": status_ref}
        return {"answer": answer, "answer_kind": "BUSINESS_FACTS",
            "business_results": {"after_sales": status}, "tool_references": [tool_ref],
            "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "handoffs": handoffs, "citations": [], "evidence": [], "latency_ms": 0}
    if not {"get_order", "search_knowledge", "get_refund_status"}.issubset(caps):
        return None
    if supervisor_result.get("supervisor_status") != "COMPLETED" or \
            supervisor_result.get("terminal_status") != "RUNNING":
        return None
    handoffs = supervisor_result.get("handoffs") or []
    targets = [row.get("target_agent") for row in handoffs]
    if targets != ["order_logistics", "policy", "after_sales"] or any(
            row.get("status") != "COMPLETED" for row in handoffs):
        return None
    order_result = handoffs[0].get("result") or {}
    policy_result = handoffs[1].get("result") or {}
    after_result = handoffs[2].get("result") or {}
    if (order_result.get("source_agent") != "order_logistics" or
            policy_result.get("source_agent") != "policy" or
            after_result.get("source_agent") != "after_sales"):
        return None
    order_refs = order_result.get("tool_result_refs") or []
    policy_refs = policy_result.get("tool_result_refs") or []
    after_refs = after_result.get("tool_result_refs") or []
    order_payload = next((result_store.get(ref) for ref in order_refs
        if isinstance(result_store.get(ref), dict) and result_store[ref].get("source") == "business_database"), None)
    policy_payload = next((result_store.get(ref) for ref in policy_refs
        if isinstance(result_store.get(ref), dict) and result_store[ref].get("source") == "shoprelay_policy_retrieval"), None)
    status_payload = next((result_store.get(ref) for ref in after_refs
        if isinstance(result_store.get(ref), dict) and result_store[ref].get("source") == "shoprelay_after_sales_database"), None)
    evaluation = next((result_store.get(ref) for ref in after_refs
        if isinstance(result_store.get(ref), dict) and result_store[ref].get("source") == "shoprelay_after_sales_evaluator"), None)
    if not all(isinstance(item, dict) for item in (order_payload, policy_payload, status_payload, evaluation)):
        return None
    order_id = order_payload.get("order_id")
    if not order_id or status_payload.get("order_id") != order_id or evaluation.get("order_id") != order_id:
        return None
    evidence = policy_payload.get("evidence") or []
    citations = []
    if evidence:
        fingerprint = policy_payload.get("index_fingerprint")
        version = policy_payload.get("knowledge_version")
        at = policy_payload.get("evaluation_at")
        if not all(isinstance(value, str) and value for value in (fingerprint, version, at)):
            return None
        if any(not evidence_is_valid(row, fingerprint=fingerprint, knowledge_version=version,
                                     evaluation_at=at) for row in evidence):
            return None
        citations = [{key: row[key] for key in (
            "chunk_id", "policy_id", "clause_id", "title", "content", "version", "policy_version",
            "effective_from", "effective_to", "knowledge_version", "index_fingerprint", "rank", "score")
            if key in row} for row in evidence[:3]]
    status_text = "" if status_payload.get("application_record_state") != "NO_APPLICATION_RECORD" else "售后数据源可用，但没有申请记录。"
    refund_data = status_payload.get("refunds") or {}
    return_data = status_payload.get("returns") or {}
    if status_payload.get("availability") == "NOT_AVAILABLE":
        missing = "、".join(status_payload.get("missing_sources", [])) or "售后状态表"
        status_text = f"当前数据库没有可用的售后状态数据源（缺少 {missing}），无法核实退款或退货申请状态。"
    latest_refund = refund_data.get("latest_request")
    latest_return = return_data.get("latest_request")
    if latest_refund or latest_return:
        parts = []
        if latest_return and latest_return.get("verified"):
            parts.append(f"最近退货申请状态为 {latest_return.get('status')}")
        elif latest_return:
            parts.append("存在退货申请记录，但状态来源未通过审计验证")
        if latest_refund and latest_refund.get("verified"):
            parts.append(f"最近退款申请状态为 {latest_refund.get('status')}")
        elif latest_refund:
            parts.append("存在退款申请记录，但状态来源未通过审计验证")
        if parts:
            status_text = "售后数据库" + "，".join(parts) + "。"
    condition_text = "；".join(
        f"{item['condition_id']}：{item['evaluation_result']}（{item['explanation']}" +
        (f"；待补充：{'、'.join(item['missing_information'])}" if item.get("missing_information") else "") + ")"
        for item in evaluation.get("conditions", []))
    policy_titles = "、".join(f"{item.get('title')}（{item.get('version')}）" for item in citations)
    if citations:
        answer = (f"已通过订单数据核实订单 {order_id} 当前状态为 {order_payload.get('status')}。"
                  f"{status_text}检索到的政策证据：{policy_titles}。")
        if condition_text:
            answer += f"条件核对：{condition_text}。"
        answer += "用户在对话中的描述不作为已核实的订单或质量事实；本次只查询并解释规则，不会创建申请、批准退货或发起退款。"
        kind = "POLICY_ANSWER"
    else:
        answer = (f"已核实订单 {order_id} 状态为 {order_payload.get('status')}；{status_text}"
                  "本次没有取得适用于该判断的有效政策证据，因此无法确认申请条件。不会执行任何售后写操作。")
        kind = "CLARIFICATION_REQUIRED"
    refs = [{"tool": "get_order", "source": "business_database", "result_ref": order_refs[0]}]
    refs.extend([{"tool": "search_knowledge", "source": "shoprelay_policy_retrieval", "result_ref": ref}
                 for ref in policy_refs])
    refs.extend([{"tool": "get_refund_status", "source": "shoprelay_after_sales_database", "result_ref": ref}
                 for ref in after_refs if result_store.get(ref, {}).get("source") == "shoprelay_after_sales_database"])
    return {"answer": answer, "answer_kind": kind,
        "business_results": {"order": {"order_id": order_id, "status": order_payload.get("status"),
            "paid_at": order_payload.get("paid_at"), "delivered_at": order_payload.get("delivered_at")},
            "after_sales": status_payload, "conditions": evaluation.get("conditions", []),
            "policy_version": policy_payload.get("knowledge_version"),
            "policy_evidence_ids": [row.get("chunk_id") for row in citations],
            "policy_evaluation_at": policy_payload.get("evaluation_at"),
            "user_claims_used_as_verified_facts": False},
        "tool_references": refs, "tool_execution_status": supervisor_result.get("tool_execution_status", []),
        "citations": citations, "evidence": citations,
        "knowledge_version": policy_payload.get("knowledge_version"),
        "policy_retrieval": {key: policy_payload.get(key) for key in (
            "retrieval_mode", "knowledge_version", "index_fingerprint", "evaluation_at",
            "config_version", "config_hash", "degraded", "failures", "latency_ms",
            "retrieval_channels", "models", "diagnostics")},
        "latency_ms": round(sum(float(row.get("duration_ms") or 0)
            for row in supervisor_result.get("tool_execution_status", []) if row.get("executed")), 3)}
