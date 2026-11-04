"""Owner-scoped, read-only adapters over the existing M0 business tables."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass, field
from contextvars import ContextVar
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.m1.models import Order, OrderItem, Product, Shipment
from app.orchestration.observability import (
    MetricRecord, ObservabilitySink, StructuredLog, TraceSpan, elapsed_ms, start_span_id, utc_now,
)

_ORDER_ID = re.compile(r"(?<![A-Za-z0-9])(?:SR-)?ORD[-_][A-Za-z0-9_-]{1,48}(?![A-Za-z0-9])", re.IGNORECASE)
_ORDER_MARKER = re.compile(r"(?<![A-Za-z0-9])(?:SR-)?ORD[-_][^\s,;!?，；。！？]+", re.IGNORECASE)
_CANONICAL_ORDER_ID = re.compile(r"^ORD[-_][A-Z0-9_-]{1,48}$")
_ACTIVE_READ_AUDIT=ContextVar('shoprelay_active_read_audit',default=None)

def active_read_audit():
    """Same-call association for an injected metered Repository,not authorization."""
    return _ACTIVE_READ_AUDIT.get()

def _invoke_audited(audit,operation,*args):
    token=_ACTIVE_READ_AUDIT.set(audit)
    try:return operation(*args)
    finally:_ACTIVE_READ_AUDIT.reset(token)


def _canonical_fingerprint(value: Any, key: bytes) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(key, encoded.encode("utf-8"), hashlib.sha256).hexdigest()


def _new_tool_audit(capability: str, arguments: dict[str, Any], *, run_id: str, trace_id: str,
                    fingerprint_key: bytes, resource_keys: tuple[str, ...] = ()) -> dict[str, Any]:
    resources = {key: arguments[key] for key in resource_keys if key in arguments}
    return {"tool_call_id": uuid.uuid4().hex, "tool": capability, "capability": capability,
        "resource_digest": _canonical_fingerprint(resources, fingerprint_key) if resources else None,
        "parameter_fingerprint": _canonical_fingerprint(arguments, fingerprint_key), "result_ref": None,
        "status": "STARTED", "executed": False, "run_id": run_id, "trace_id": trace_id}


def _tool_event_payload(audit: dict[str, Any], *, status: str, error_code: str | None = None) -> dict[str, Any]:
    payload = {key: audit.get(key) for key in
        ("tool_call_id", "tool", "resource_digest", "parameter_fingerprint", "result_ref", "run_id", "trace_id")}
    if audit.get("owner_scope"):
        payload["owner_scope"] = audit["owner_scope"]
    payload["status"] = status
    if error_code:
        payload["error_code"] = error_code
    return payload


def not_executed_tool_audit(capability: str, *, run_id: str, trace_id: str,
                            error_code: str | None = None) -> dict[str, Any]:
    return {"tool_call_id": uuid.uuid4().hex, "tool": capability, "capability": capability,
        "resource_digest": None, "parameter_fingerprint": None, "result_ref": None,
        "status": "NOT_EXECUTED", "executed": False, "error_code": error_code,
        "run_id": run_id, "trace_id": trace_id}


def is_return_guidance_query(query: str) -> bool:
    """Recognize return guidance, not a completed action or progress lookup."""
    if re.search(r"进度|处理状态|审核状态|退款状态|售后状态", query):
        return False
    procedural = re.search(r"怎么|如何|怎样|流程|步骤|\bhow\b", query, re.I)
    if not procedural and re.search(r"(?:提交|创建|发起|执行|批准|撤销|取消|确认).{0,8}(?:退货|退换)", query):
        return False  # Keep explicit action requests on the confirmation path.
    return bool(re.search(r"(?:怎么|如何|怎样|想|需要|能不能|能否|可以|能).{0,12}(?:退货|退回|退换)|"
        r"(?:退货|退回|退换).{0,12}(?:怎么|如何|怎样|流程|步骤|申请|办理)|"
        r"\bhow\s+(?:(?:can|do)\s+i\s+|to\s+)?return\b", query, re.I))


def requested_order_id(query: str) -> tuple[str | None, str | None]:
    """Extract one target identifier. It is untrusted input, never an authorization token."""
    # Quoted instructions are not the active target when an explicit, unique
    # order is requested outside the quote. Owner predicates still authorize it.
    unquoted = re.sub(r'"[^"\n]*"|“[^”\n]*”', " ", query)
    outside = list(dict.fromkeys(match.group(0).upper().removeprefix("SR-") for match in _ORDER_ID.finditer(unquoted)))
    instruction_quote = any(re.search(r"ignore|reveal|disclose|system prompt|api.?key|忽略|密钥|系统提示", fragment, re.I)
        for pair in re.findall(r'"([^"\n]*)"|“([^”\n]*)”', query) for fragment in pair if fragment)
    if len(outside) == 1 and instruction_quote:
        query = unquoted
    matches = list(dict.fromkeys(match.group(0).upper().removeprefix("SR-") for match in _ORDER_ID.finditer(query)))
    if len(matches) > 1:
        return None, "ORDER_ID_AMBIGUOUS"
    if not matches:
        if _ORDER_MARKER.search(query) or re.search(
                r"(?<![A-Za-z0-9])(?:SR-)?ORD[-_](?![A-Za-z0-9_-])", query, re.IGNORECASE):
            return None, "INVALID_ORDER_ID"
        return None, None
    if not _CANONICAL_ORDER_ID.fullmatch(matches[0]):
        return None, "INVALID_ORDER_ID"
    return matches[0], None


class ToolExecutionError(RuntimeError):
    def __init__(self, code: str, *, status: str = "FAILED") -> None:
        super().__init__(code)
        self.code = code
        self.status = status


class OrderReadRepository:
    """Repository contract. Every method receives the authenticated owner explicitly."""
    def list_orders(self, before: str, owner_id: str) -> dict[str, Any]:
        raise NotImplementedError

    def get_order(self, order_id: str, owner_id: str) -> dict[str, Any]:  # pragma: no cover - protocol-like base
        raise NotImplementedError

    def get_order_items(self, order_id: str, owner_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError

    def track_shipment(self, order_id: str, owner_id: str) -> dict[str, Any]:  # pragma: no cover
        raise NotImplementedError


class SqlAlchemyOrderReadRepository(OrderReadRepository):
    """Queries only existing tables; no schema creation or business writes."""
    def __init__(self, factory: sessionmaker) -> None:
        self.factory = factory

    def _owned_order(self, db: Session, order_id: str, owner_id: str) -> Order | None:
        return db.execute(select(Order).where(Order.order_id == order_id, Order.user_id == owner_id)).scalar_one_or_none()

    def list_orders(self, before: str, owner_id: str) -> dict[str, Any]:
        # The existing schema has no creation timestamp. Never call this a
        # chronological list: a keyset on the immutable ID gives a stable page.
        if before and not re.fullmatch(r"ORD[-_][A-Z0-9_-]{1,48}", before):
            raise ToolExecutionError("INVALID_ORDER_CURSOR")
        with self.factory() as db:
            query = select(Order).where(Order.user_id == owner_id)
            if before:
                query = query.where(Order.order_id < before)
            rows = db.execute(query.order_by(Order.order_id.desc()).limit(6)).scalars().all()
            page = rows[:5]
            return {"source": "business_database", "sort": "order_id_desc", "page_size": 5,
                "orders": [{"order_id": row.order_id, "status": row.status,
                    "paid_at": row.paid_at, "delivered_at": row.delivered_at,
                    "paid_cents": row.paid_cents, "version": row.version} for row in page],
                "has_more": len(rows) > 5,
                "next_before": page[-1].order_id if len(rows) > 5 else None}

    def get_order(self, order_id: str, owner_id: str) -> dict[str, Any]:
        with self.factory() as db:
            row = self._owned_order(db, order_id, owner_id)
            if row is None:
                raise ToolExecutionError("ORDER_NOT_FOUND", status="NOT_FOUND")
            return {"source": "business_database", "order_id": row.order_id, "status": row.status, "paid_at": row.paid_at,
                    "delivered_at": row.delivered_at, "paid_cents": row.paid_cents, "version": row.version}

    def get_order_items(self, order_id: str, owner_id: str) -> dict[str, Any]:
        with self.factory() as db:
            if self._owned_order(db, order_id, owner_id) is None:
                raise ToolExecutionError("ORDER_NOT_FOUND", status="NOT_FOUND")
            rows = db.execute(select(OrderItem.product_id, Product.name, OrderItem.quantity,
                                     OrderItem.paid_cents)
                .join(Order, Order.order_id == OrderItem.order_id)
                .join(Product, Product.product_id == OrderItem.product_id)
                .where(OrderItem.order_id == order_id, Order.user_id == owner_id)
                .order_by(OrderItem.product_id)).all()
            return {"source": "business_database", "order_id": order_id, "items": [
                {"product_id": row.product_id, "name": row.name, "quantity": row.quantity,
                 "paid_cents": row.paid_cents} for row in rows]}

    def track_shipment(self, order_id: str, owner_id: str) -> dict[str, Any]:
        with self.factory() as db:
            if self._owned_order(db, order_id, owner_id) is None:
                raise ToolExecutionError("ORDER_NOT_FOUND", status="NOT_FOUND")
            rows = db.execute(select(Shipment.status, Shipment.observed_at)
                .join(Order, Order.order_id == Shipment.order_id)
                .where(Shipment.order_id == order_id, Order.user_id == owner_id)
                .order_by(Shipment.observed_at, Shipment.shipment_id)).all()
            if not rows:
                return {"source": "business_database", "order_id": order_id, "availability": "UNAVAILABLE", "records": [],
                        "error_code": "NO_SHIPMENT_RECORD"}
            return {"source": "business_database", "order_id": order_id, "availability": "AVAILABLE", "records": [
                {"status": row.status, "observed_at": row.observed_at} for row in rows]}


@dataclass
class AgentExecutionContext:
    """Ephemeral per-run tool dependencies; business payloads never enter graph state."""
    result_store: dict[str, dict[str, Any]]
    sink: ObservabilitySink
    run_id: str
    trace_id: str
    parent_span_id: str | None
    user_query: str = ""
    memory_context: str = ""
    graph_version: str = "g2-m3.3"
    retrieval_service: Any = None
    retrieval_evaluation_at: str | None = None
    route_payload: dict[str, Any] = field(default_factory=dict)
    emit_event: Callable[[str, dict, str], None] | None = None
    escalation_permission_checker: Callable[[str, str], bool] | None = None
    call_records: list[dict[str, Any]] = field(default_factory=list)
    # Per-runtime secret salt. Only keyed fingerprints leave this context; the
    # key itself is never persisted, logged, checkpointed, or returned.
    audit_fingerprint_key: bytes = field(default_factory=lambda: secrets.token_bytes(32), repr=False)

    def execute_read(self, capability: str, order_id: str, owner_id: str,
                     operation: Callable[[str, str], dict[str, Any]]) -> str:
        if capability not in {"list_orders", "get_order", "get_order_items", "track_shipment", "get_refund_status"}:
            raise ToolExecutionError("TOOL_NOT_ALLOWED")
        span_id = start_span_id()
        started_at = utc_now()
        started = time.perf_counter()
        arguments = ({"before": order_id, "limit": 5, "owner_id": owner_id} if capability == "list_orders"
                     else {"order_id": order_id, "owner_id": owner_id})
        audit = _new_tool_audit(capability, arguments,
            run_id=self.run_id, trace_id=self.trace_id, fingerprint_key=self.audit_fingerprint_key,
            resource_keys=("owner_id",) if capability == "list_orders" else ("order_id", "owner_id"))
        audit["owner_scope"] = "AUTHENTICATED_OWNER_FILTER"
        event_base = f"m3:tool:{self.run_id}:{audit['tool_call_id']}"
        if self.emit_event:
            try:
                self.emit_event("tool_started", _tool_event_payload(audit, status="STARTED"), f"{event_base}:started")
            except Exception as exc:
                self.call_records.append({**audit, "status": "FAILED", "executed": False,
                                          "error_code": "TOOL_EVENT_PERSISTENCE_FAILED"})
                raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc
        status = "COMPLETED"
        error_code = None
        ref = "tool_result:" + hashlib.sha256(f"{self.run_id}|{capability}|{order_id}".encode()).hexdigest()[:48]
        audit["result_ref"] = ref
        try:
            payload = _invoke_audited(audit,operation,order_id,owner_id)
            self.result_store[ref] = payload
            if payload.get("availability") in {"UNAVAILABLE", "NOT_AVAILABLE"}:
                status = str(payload["availability"])
                error_code = str(payload.get("error_code") or "BUSINESS_DATA_UNAVAILABLE")
            return ref
        except ToolExecutionError as exc:
            status, error_code = exc.status, exc.code
            raise
        except SQLAlchemyError as exc:
            status, error_code = "FAILED", "TOOL_DATABASE_UNAVAILABLE"
            raise ToolExecutionError(error_code) from exc
        except TimeoutError as exc:
            status, error_code = "FAILED", "TOOL_TIMEOUT"
            raise ToolExecutionError(error_code) from exc
        except Exception as exc:
            status, error_code = "FAILED", "TOOL_EXECUTION_FAILED"
            raise ToolExecutionError(error_code) from exc
        finally:
            elapsed = elapsed_ms(started)
            span = TraceSpan(trace_id=self.trace_id, span_id=span_id, parent_span_id=self.parent_span_id,
                run_id=self.run_id, node_name=f"m3.tool.{capability}", strategy="AGENT",
                start_time=started_at, end_time=utc_now(), duration_ms=elapsed,
                # NOT_AVAILABLE is a valid, successfully obtained domain result (for
                # example, this database has no refunds/returns tables).  The tool
                # execution itself succeeded; preserve the unavailable data status
                # in call_records and the returned payload, not as an operational
                # span failure.
                status="OK" if status in {"COMPLETED", "UNAVAILABLE", "NOT_AVAILABLE"} else "ERROR",
                error_code=error_code, graph_version=self.graph_version)
            self.sink.record_span(span)
            self.sink.record_metrics(MetricRecord(node_execution_count=1,
                node_error_count=int(status == "FAILED"), node_duration_ms=[elapsed], span_id=span_id,
                node_name=f"m3.tool.{capability}", graph_version=self.graph_version))
            self.sink.record_log(StructuredLog(timestamp=utc_now(),
                level="ERROR" if status == "FAILED" else "INFO", trace_id=self.trace_id,
                run_id=self.run_id, node=f"m3.tool.{capability}",
                event_type="tool_failed" if status == "FAILED" else "tool_completed",
                error_code=error_code,
                sanitized_message=f"read tool {status.lower()}" if status != "FAILED" else f"read tool failed: {error_code}",
                span_id=span_id))
            audit.update({"status": status, "executed": True, "error_code": error_code,
                          "duration_ms": elapsed})
            self.call_records.append(dict(audit))
            if self.emit_event:
                try:
                    self.emit_event("tool_finished", _tool_event_payload(audit, status=status,
                        error_code=error_code), f"{event_base}:finished")
                except Exception as exc:
                    raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc

    def create_escalation_proposal(self, *, trusted, reason_code: str,
                                   order_ref: str | None, evidence_refs: list[str],
                                   related_agents: list[str]) -> str:
        """Build a per-run proposal reference only; never creates an Action/ticket."""
        if self.escalation_permission_checker is None:
            raise ToolExecutionError("PROPOSAL_PERMISSION_UNAVAILABLE")
        span_id = start_span_id()
        started_at = utc_now()
        started = time.perf_counter()
        event_base = f"m3:proposal:{self.run_id}:escalate_to_human"
        if self.emit_event:
            try:
                self.emit_event("tool_started", {"tool": "escalate_to_human", "status": "STARTED"},
                                f"{event_base}:started")
            except Exception as exc:
                self.call_records.append({"capability": "escalate_to_human", "status": "FAILED",
                    "executed": False, "error_code": "TOOL_EVENT_PERSISTENCE_FAILED"})
                raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc
        status, error_code = "DRAFT_ONLY", None
        ref = "tool_result:" + hashlib.sha256(
            f"{self.run_id}|escalate_to_human|{trusted.user_id}".encode()).hexdigest()[:48]
        try:
            if not trusted.authenticated or not trusted.run_owner_verified or not trusted.session_owner_verified:
                raise ToolExecutionError("AGENT_CONTEXT_INVALID")
            try:
                permission_ok = bool(self.escalation_permission_checker(
                    trusted.user_id, "support.ticket.draft"))
            except Exception as exc:
                status, error_code = "FAILED", "PROPOSAL_PERMISSION_CHECK_FAILED"
                raise ToolExecutionError(error_code) from exc
            if not permission_ok:
                raise PermissionError("TOOL_NOT_AUTHORIZED")
            if order_ref is not None:
                if order_ref != trusted.requested_order_id or not any(
                    isinstance(value, dict) and value.get("source") == "business_database" and
                    value.get("order_id") == order_ref for value in self.result_store.values()
                ):
                    raise ToolExecutionError("ORDER_REFERENCE_UNVERIFIED")
            from app.m3.escalation import create_escalation_draft
            def checked_permission(owner_id: str, permission: str) -> bool:
                return owner_id == trusted.user_id and permission == "support.ticket.draft" and permission_ok
            draft = create_escalation_draft(
                permission_checker=checked_permission,
                trusted_user_id=trusted.user_id, authenticated=trusted.authenticated,
                owner_verified=trusted.run_owner_verified and trusted.session_owner_verified,
                reason_code=reason_code, order_ref=order_ref, summary_code="HUMAN_REVIEW_REQUESTED",
                issue_category="OTHER",
                user_description="用户请求人工客服协助；详细内容保留在当前会话，待用户确认。",
                issue_summary="人工客服协助请求，等待用户确认后再提交。",
                related_agents=related_agents, verified_fact_refs=[],
                unverified_claim_codes=["USER_REQUESTED_ESCALATION"], evidence_refs=evidence_refs)
            payload = {"source": "shoprelay_escalation_draft", "draft": draft.model_dump(mode="json"),
                       "pending_action_arguments": draft.pending_action_arguments()}
            import json
            payload = json.loads(json.dumps(payload, ensure_ascii=False, allow_nan=False))
            self.result_store[ref] = payload
        except PermissionError as exc:
            status, error_code = "FAILED", "TOOL_NOT_AUTHORIZED"
            raise ToolExecutionError(error_code) from exc
        except ToolExecutionError as exc:
            status, error_code = "FAILED", exc.code
            raise
        except Exception as exc:
            status, error_code = "FAILED", "PROPOSAL_SCHEMA_INVALID"
            raise ToolExecutionError(error_code) from exc
        finally:
            duration = elapsed_ms(started)
            span = TraceSpan(trace_id=self.trace_id, span_id=span_id, parent_span_id=self.parent_span_id,
                run_id=self.run_id, node_name="m3.proposal.escalate_to_human", strategy="AGENT",
                start_time=started_at, end_time=utc_now(), duration_ms=duration,
                status="OK" if status == "DRAFT_ONLY" else "ERROR", error_code=error_code,
                graph_version=self.graph_version)
            self.sink.record_span(span)
            self.sink.record_metrics(MetricRecord(node_execution_count=1,
                node_error_count=int(status == "FAILED"), node_duration_ms=[duration], span_id=span_id,
                node_name="m3.proposal.escalate_to_human", graph_version=self.graph_version))
            self.sink.record_log(StructuredLog(timestamp=utc_now(), level="ERROR" if status == "FAILED" else "INFO",
                trace_id=self.trace_id, run_id=self.run_id, node="m3.proposal.escalate_to_human",
                event_type="proposal_failed" if status == "FAILED" else "proposal_created",
                error_code=error_code, sanitized_message="escalation proposal was not executed", span_id=span_id))
            self.call_records.append({"capability": "escalate_to_human", "status": status,
                "executed": False, "error_code": error_code, "duration_ms": duration})
            if self.emit_event:
                try:
                    self.emit_event("tool_finished", {"tool": "escalate_to_human", "status": status,
                        "executed": False, "error_code": error_code}, f"{event_base}:finished")
                except Exception as exc:
                    raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc
        return ref

    def execute_product_read(self, capability: str, arguments: dict[str, Any],
                             operation: Callable[[], dict[str, Any]]) -> str:
        """Execute one registered public catalog read; arguments never enter telemetry."""
        allowed = {"search_products", "get_product_detail", "recommend_products", "check_inventory"}
        if capability not in allowed or not isinstance(arguments, dict):
            raise ToolExecutionError("TOOL_NOT_ALLOWED")
        span_id = start_span_id()
        started_at = utc_now()
        started = time.perf_counter()
        audit = _new_tool_audit(capability, arguments, run_id=self.run_id, trace_id=self.trace_id,
            fingerprint_key=self.audit_fingerprint_key, resource_keys=("product_id", "product_ids", "ids"))
        event_base = f"m3:tool:{self.run_id}:{audit['tool_call_id']}"
        if self.emit_event:
            try:
                self.emit_event("tool_started", _tool_event_payload(audit, status="STARTED"), f"{event_base}:started")
            except Exception as exc:
                self.call_records.append({**audit, "status": "FAILED", "executed": False,
                                          "error_code": "TOOL_EVENT_PERSISTENCE_FAILED"})
                raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc
        status, error_code = "COMPLETED", None
        argument_fingerprint = audit["parameter_fingerprint"]
        ref = "tool_result:" + hashlib.sha256(
            f"{self.run_id}|{capability}|{argument_fingerprint}".encode()).hexdigest()[:48]
        audit["result_ref"] = ref
        try:
            payload = _invoke_audited(audit, operation)
            if not isinstance(payload, dict) or payload.get("source") not in {
                    "shoprelay_product_catalog", "shoprelay_inventory"}:
                raise ToolExecutionError("TOOL_RESULT_SCHEMA_INVALID")
            self.result_store[ref] = payload
            if payload.get("availability") in {"UNAVAILABLE", "NOT_FOUND"}:
                status = str(payload["availability"])
                error_code = str(payload.get("error_code") or "BUSINESS_DATA_UNAVAILABLE")
            return ref
        except ToolExecutionError as exc:
            status, error_code = exc.status, exc.code
            raise
        except SQLAlchemyError as exc:
            status, error_code = "FAILED", "TOOL_DATABASE_UNAVAILABLE"
            raise ToolExecutionError(error_code) from exc
        except TimeoutError as exc:
            status, error_code = "FAILED", "TOOL_TIMEOUT"
            raise ToolExecutionError(error_code) from exc
        except Exception as exc:
            status, error_code = "FAILED", "TOOL_EXECUTION_FAILED"
            raise ToolExecutionError(error_code) from exc
        finally:
            elapsed = elapsed_ms(started)
            span = TraceSpan(trace_id=self.trace_id, span_id=span_id, parent_span_id=self.parent_span_id,
                run_id=self.run_id, node_name=f"m3.tool.{capability}", strategy="AGENT",
                start_time=started_at, end_time=utc_now(), duration_ms=elapsed,
                status="OK" if status in {"COMPLETED", "UNAVAILABLE", "NOT_FOUND"} else "ERROR",
                error_code=error_code, graph_version=self.graph_version)
            self.sink.record_span(span)
            self.sink.record_metrics(MetricRecord(node_execution_count=1,
                node_error_count=int(status == "FAILED"), node_duration_ms=[elapsed], span_id=span_id,
                node_name=f"m3.tool.{capability}", graph_version=self.graph_version))
            self.sink.record_log(StructuredLog(timestamp=utc_now(),
                level="ERROR" if status == "FAILED" else "INFO", trace_id=self.trace_id,
                run_id=self.run_id, node=f"m3.tool.{capability}",
                event_type="tool_failed" if status == "FAILED" else "tool_completed",
                error_code=error_code,
                sanitized_message=f"read tool {status.lower()}" if status != "FAILED" else f"read tool failed: {error_code}",
                span_id=span_id))
            audit.update({"status": status, "executed": True, "error_code": error_code,
                          "duration_ms": elapsed})
            self.call_records.append(dict(audit))
            if self.emit_event:
                try:
                    self.emit_event("tool_finished", _tool_event_payload(audit, status=status,
                        error_code=error_code), f"{event_base}:finished")
                except Exception as exc:
                    raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc

    def execute_policy_read(self, capability: str, *, query: str, mode: str,
                            result_ref: str | None = None,
                            resource_ids: dict[str, Any] | None = None,
                            operation: Callable[[], dict[str, Any]]) -> str:
        """Run the registered, bounded policy search; payload remains per-run ephemeral."""
        if capability not in {"search_knowledge", "get_warranty"} or mode not in {"BM25", "HYBRID_RERANK"}:
            raise ToolExecutionError("TOOL_NOT_ALLOWED")
        if not isinstance(query, str) or not query.strip() or len(query) > 1000:
            raise ToolExecutionError("POLICY_QUERY_INVALID")
        span_id = start_span_id()
        started_at = utc_now()
        started = time.perf_counter()
        audit = _new_tool_audit(capability, {"query": query, "mode": mode},
            run_id=self.run_id, trace_id=self.trace_id, fingerprint_key=self.audit_fingerprint_key)
        audit["result_ref"] = result_ref
        if resource_ids:
            audit["resource_digest"] = _canonical_fingerprint(resource_ids, self.audit_fingerprint_key)
        event_base = f"m3:tool:{self.run_id}:{audit['tool_call_id']}"
        if self.emit_event:
            try:
                self.emit_event("tool_started", _tool_event_payload(audit, status="STARTED"),
                                f"{event_base}:started")
                if capability == "search_knowledge" and isinstance(result_ref, str) and result_ref:
                    self.emit_event("policy_retrieval_started", {
                        "audit_schema_version": "policy-retrieval-audit-v1",
                        "source_type": "POLICY_SEARCH_TOOL",
                        "capability": capability,
                        "call_id": audit["tool_call_id"],
                        "result_ref": result_ref,
                        "parameter_fingerprint": audit["parameter_fingerprint"],
                        "retrieval_mode": mode,
                        "status": "STARTED",
                        "executed": False,
                        "run_id": self.run_id,
                        "trace_id": self.trace_id,
                    }, f"{event_base}:policy-started")
            except Exception as exc:
                self.call_records.append({**audit, "status": "FAILED", "executed": False,
                                          "error_code": "TOOL_EVENT_PERSISTENCE_FAILED"})
                raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc
        status, error_code = "COMPLETED", None
        payload: dict[str, Any] = {}
        try:
            payload = _invoke_audited(audit, operation)
            if not isinstance(payload, dict) or payload.get("source") != "shoprelay_policy_retrieval":
                raise ToolExecutionError("POLICY_RESULT_SCHEMA_INVALID")
            self.result_store[payload["result_ref"]] = payload
            audit["result_ref"] = payload["result_ref"]
            if not payload.get("evidence"):
                status, error_code = "NO_EVIDENCE", "POLICY_EVIDENCE_NOT_FOUND"
            elif payload.get("degraded"):
                status, error_code = "DEGRADED", "RETRIEVAL_DEGRADED"
            if self.emit_event:
                try:
                    self.emit_event("retrieval_hop", {"retrieval_index": 1,
                        "call_id": audit["tool_call_id"],
                        "result_ref": payload.get("result_ref"),
                        "capability": capability,
                        "strategy_executed": payload.get("retrieval_mode"),
                        "knowledge_version": payload.get("knowledge_version"),
                        "index_fingerprint": payload.get("index_fingerprint"),
                        "evidence_ids": [row.get("chunk_id") for row in payload.get("evidence", [])],
                        "evidence_count": len(payload.get("evidence", [])),
                        "degraded": bool(payload.get("degraded"))},
                         f"m3:retrieval:{self.run_id}:{audit['tool_call_id']}:1")
                except Exception as exc:
                    status, error_code = "FAILED", "TOOL_EVENT_PERSISTENCE_FAILED"
                    raise ToolExecutionError(error_code) from exc
            return payload["result_ref"]
        except ToolExecutionError as exc:
            status, error_code = exc.status, exc.code
            raise
        except TimeoutError as exc:
            status, error_code = "FAILED", "RETRIEVAL_TIMEOUT"
            raise ToolExecutionError(error_code) from exc
        except Exception as exc:
            status, error_code = "FAILED", getattr(exc, "code", None) or "RETRIEVAL_UNAVAILABLE"
            raise ToolExecutionError(error_code) from exc
        finally:
            elapsed = elapsed_ms(started)
            # Result metadata is safe to log; policy text and the query are not.
            span = TraceSpan(trace_id=self.trace_id, span_id=span_id, parent_span_id=self.parent_span_id,
                run_id=self.run_id, node_name=f"m3.tool.{capability}", strategy="AGENT",
                start_time=started_at, end_time=utc_now(), duration_ms=elapsed,
                status="ERROR" if status == "FAILED" else "OK", error_code=error_code,
                graph_version=self.graph_version)
            self.sink.record_span(span)
            self.sink.record_metrics(MetricRecord(node_execution_count=1,
                node_error_count=int(status == "FAILED"), node_duration_ms=[elapsed],
                retrieval_hop_count=int(payload.get("retrieval_count", 0)), span_id=span_id,
                node_name=f"m3.tool.{capability}", graph_version=self.graph_version))
            self.sink.record_log(StructuredLog(timestamp=utc_now(),
                level="ERROR" if status == "FAILED" else "INFO", trace_id=self.trace_id,
                run_id=self.run_id, node=f"m3.tool.{capability}",
                event_type="tool_failed" if status == "FAILED" else "tool_completed",
                error_code=error_code,
                sanitized_message="policy retrieval failed" if status == "FAILED" else f"policy retrieval {status.lower()}",
                span_id=span_id))
            audit.update({"status": status, "executed": True, "error_code": error_code,
                "duration_ms": elapsed})
            self.call_records.append({**audit,
                "retrieval_mode": payload.get("retrieval_mode"),
                "knowledge_version": payload.get("knowledge_version"),
                "index_fingerprint": payload.get("index_fingerprint"),
                "evidence_ids": [row.get("chunk_id") for row in payload.get("evidence", [])],
                "retrieval_count": payload.get("retrieval_count", 0),
                "degraded": payload.get("degraded", False)})
            if self.emit_event:
                try:
                    if capability == "search_knowledge" and isinstance(audit.get("result_ref"), str) and audit["result_ref"]:
                        self.emit_event("policy_retrieval_finished", {
                            "audit_schema_version": "policy-retrieval-audit-v1",
                            "source_type": "POLICY_SEARCH_TOOL",
                            "capability": capability,
                            "call_id": audit["tool_call_id"],
                            "result_ref": audit["result_ref"],
                            "parameter_fingerprint": audit["parameter_fingerprint"],
                            "retrieval_mode": mode,
                            "status": "SUCCEEDED" if status in {"COMPLETED", "NO_EVIDENCE", "DEGRADED"} else "FAILED",
                            "executed": True,
                            "error_code": error_code,
                            "knowledge_version": payload.get("knowledge_version"),
                            "index_fingerprint": payload.get("index_fingerprint"),
                            "evidence_ids": [row.get("chunk_id") for row in payload.get("evidence", [])
                                             if isinstance(row, dict) and isinstance(row.get("chunk_id"), str)],
                            "degraded": bool(payload.get("degraded")),
                            "run_id": self.run_id,
                            "trace_id": self.trace_id,
                        }, f"{event_base}:policy-finished")
                    self.emit_event("tool_finished", _tool_event_payload(audit, status=status,
                        error_code=error_code), f"{event_base}:finished")
                except Exception as exc:
                    raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc

    def execute_coupon_read(self, capability: str, arguments: dict[str, Any],
                            operation: Callable[[], dict[str, Any]]) -> str:
        """Bounded coupon catalog read; the returned catalog row is not eligibility proof."""
        if capability != "get_coupon" or not isinstance(arguments, dict) or \
                set(arguments) != {"coupon_code"} or not re.fullmatch(
                    r"[A-Z0-9][A-Z0-9_-]{1,63}", str(arguments.get("coupon_code", ""))):
            raise ToolExecutionError("COUPON_ARGUMENT_INVALID")
        started_at, started, span_id = utc_now(), time.perf_counter(), start_span_id()
        audit = _new_tool_audit(capability, arguments, run_id=self.run_id, trace_id=self.trace_id,
            fingerprint_key=self.audit_fingerprint_key, resource_keys=("coupon_code",))
        event_base = f"m3:tool:{self.run_id}:{audit['tool_call_id']}"
        if self.emit_event:
            try:
                self.emit_event("tool_started", _tool_event_payload(audit, status="STARTED"), f"{event_base}:started")
            except Exception as exc:
                self.call_records.append({**audit, "status": "FAILED", "executed": False,
                    "error_code": "TOOL_EVENT_PERSISTENCE_FAILED"})
                raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc
        status, error_code = "COMPLETED", None
        ref = "tool_result:" + hashlib.sha256(
            f"{self.run_id}|{capability}|{arguments['coupon_code']}".encode()).hexdigest()[:48]
        audit["result_ref"] = ref
        try:
            payload = _invoke_audited(audit, operation)
            if not isinstance(payload, dict) or payload.get("source") != "shoprelay_coupon_catalog" or \
                    payload.get("availability") not in {"AVAILABLE", "NOT_FOUND", "NOT_AVAILABLE"}:
                raise ToolExecutionError("COUPON_RESULT_SCHEMA_INVALID")
            self.result_store[ref] = payload
            if payload["availability"] != "AVAILABLE":
                status, error_code = payload["availability"], payload.get("error_code")
            return ref
        except ToolExecutionError as exc:
            status, error_code = exc.status, exc.code
            raise
        except SQLAlchemyError as exc:
            status, error_code = "FAILED", "COUPON_DATABASE_UNAVAILABLE"
            raise ToolExecutionError(error_code) from exc
        except Exception as exc:
            status, error_code = "FAILED", "COUPON_TOOL_FAILED"
            raise ToolExecutionError(error_code) from exc
        finally:
            elapsed = elapsed_ms(started)
            self.sink.record_span(TraceSpan(trace_id=self.trace_id, span_id=span_id,
                parent_span_id=self.parent_span_id, run_id=self.run_id, node_name="m3.tool.get_coupon",
                strategy="AGENT", start_time=started_at, end_time=utc_now(), duration_ms=elapsed,
                status="ERROR" if status == "FAILED" else "OK", error_code=error_code,
                graph_version=self.graph_version))
            self.sink.record_metrics(MetricRecord(node_execution_count=1,
                node_error_count=int(status == "FAILED"), node_duration_ms=[elapsed], span_id=span_id,
                node_name="m3.tool.get_coupon", graph_version=self.graph_version))
            self.sink.record_log(StructuredLog(timestamp=utc_now(),
                level="ERROR" if status == "FAILED" else "INFO", trace_id=self.trace_id,
                run_id=self.run_id, node="m3.tool.get_coupon",
                event_type="tool_failed" if status == "FAILED" else "tool_completed",
                error_code=error_code, sanitized_message="coupon catalog read failed" if status == "FAILED"
                    else f"coupon catalog read {status.lower()}", span_id=span_id))
            audit.update({"status": status, "executed": True, "error_code": error_code,
                          "duration_ms": elapsed})
            self.call_records.append(dict(audit))
            if self.emit_event:
                try:
                    self.emit_event("tool_finished", _tool_event_payload(audit, status=status,
                        error_code=error_code), f"{event_base}:finished")
                except Exception as exc:
                    raise ToolExecutionError("TOOL_EVENT_PERSISTENCE_FAILED") from exc


def build_order_business_result(supervisor_result: dict[str, Any], *, route: dict[str, Any],
                                trusted: Any, result_store: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    """Validate and render a completed read-only Order Agent handoff; never trusts model text."""
    allowed = {"list_orders", "get_order", "get_order_items", "track_shipment"}
    capabilities = route.get("required_capabilities")
    if not isinstance(capabilities, list) or not capabilities or not set(capabilities).issubset(allowed):
        return None
    if supervisor_result.get("supervisor_status") != "COMPLETED" or \
       supervisor_result.get("terminal_status") != "RUNNING":
        return None
    handoffs = supervisor_result.get("handoffs") or []
    if len(handoffs) != 1:
        return None
    handoff = handoffs[0]
    if handoff.get("target_agent") != "order_logistics" or handoff.get("status") != "COMPLETED":
        return None
    task_caps = handoff.get("required_capabilities")
    if not isinstance(task_caps, list) or sorted(task_caps) != sorted(capabilities):
        return None
    agent_result = handoff.get("result") or {}
    if agent_result.get("source_agent") != "order_logistics" or agent_result.get("status") != "COMPLETED":
        return None
    missing = agent_result.get("missing_information") or []
    if missing == ["ORDER_ID_REQUIRED"] and not agent_result.get("tool_result_refs"):
        return {"answer": "请提供需要查询的订单号。", "answer_kind": "CLARIFICATION_REQUIRED",
                "business_results": {}, "tool_references": [], "tool_execution_status":
                    supervisor_result.get("tool_execution_status", []), "latency_ms": 0}
    if not (trusted.authenticated and trusted.run_owner_verified and trusted.session_owner_verified):
        return None
    if capabilities == ["list_orders"]:
        refs = agent_result.get("tool_result_refs", [])
        statuses = supervisor_result.get("tool_execution_status", [])
        if (len(refs) != 1 or not any(row.get("capability") == "list_orders" and
                row.get("executed") and row.get("status") == "COMPLETED" for row in statuses)):
            return None
        value = result_store.get(refs[0])
        if value is None or value.get("source") != "business_database":
            return None
        rows = value["orders"]
        text = "您本人账户下暂无订单。" if not rows else "您账户下的订单（按订单号排序）：\n" + "\n".join(
            f"{index + 1}. {row['order_id']}" for index, row in enumerate(rows)) + "\n请选择要查询的订单。"
        return {"answer": text, "answer_kind": "BUSINESS_FACTS", "business_results": {"list_orders": value},
            "tool_references": [{"tool": "list_orders", "source": "business_database", "result_ref": refs[0]}],
            "tool_execution_status": statuses, "latency_ms": sum(row.get("duration_ms", 0) for row in statuses)}
    if not getattr(trusted, "requested_order_id", None) or not getattr(trusted, "authenticated", False) or \
       not getattr(trusted, "run_owner_verified", False) or not getattr(trusted, "session_owner_verified", False):
        return None
    statuses = {row.get("capability"): row for row in supervisor_result.get("tool_execution_status", [])}
    refs = agent_result.get("tool_result_refs") or []
    if len(refs) != len(task_caps):
        return None
    facts: dict[str, Any] = {}
    references: list[dict[str, str]] = []
    for capability, ref in zip(task_caps, refs, strict=True):
        status = statuses.get(capability)
        if not status or not status.get("executed") or status.get("status") not in {"COMPLETED", "UNAVAILABLE"}:
            return None
        value = result_store.get(ref)
        if not isinstance(value, dict) or value.get("order_id") != trusted.requested_order_id:
            return None
        facts[capability] = value
        references.append({"tool": capability, "source": "business_database", "result_ref": ref})

    phrases: list[str] = []
    if "get_order" in facts:
        row = facts["get_order"]
        state_names = {"CREATED": "待付款", "PENDING": "待付款", "PAID": "已付款", "SHIPPED": "已发货",
                       "DELIVERED": "已送达", "CANCELLED": "已取消", "CANCELED": "已取消", "REFUNDED": "已退款"}
        display_state = state_names.get(row['status'], '暂无法解释，原始值已保留在详情')
        phrases.append(f"订单 {row['order_id']} 已记录为{display_state}。")
        if row.get("paid_at"):
            phrases.append(f"付款时间：{row['paid_at']}。")
        if row.get("delivered_at"):
            phrases.append(f"签收时间：{row['delivered_at']}。")
        amount = row.get("paid_cents")
        if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
            phrases.append(f"订单记录金额：{amount}分（原始最小金额单位，币种未提供）。")
    if "get_order_items" in facts:
        items = facts["get_order_items"].get("items", [])
        if items:
            rendered_items = []
            for item in items:
                description = (f"{item['name']}（商品编号 {item['product_id']}）× {item['quantity']}")
                amount = item.get("paid_cents")
                if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                    description += f"，记录实付 {amount}分（币种未提供）"
                rendered_items.append(description)
            phrases.append("订单商品：" + "；".join(rendered_items) + "。")
        else:
            phrases.append("该订单当前没有可用的商品明细记录。")
    if "track_shipment" in facts:
        shipment = facts["track_shipment"]
        phrases.append("物流记录来源：业务数据库（business_database）。")
        if shipment.get("availability") == "AVAILABLE":
            phrases.append("物流记录：" + "；".join(
                f"{item['observed_at']} 状态 {item['status']}" for item in shipment["records"]) + "。")
        else:
            phrases.append("本次查询未找到该订单的物流记录，观测时刻和当前位置未知，无法确认实时物流状态；无记录不表示未发货或延误。")
    if not phrases:
        return None
    durations = [float(row.get("duration_ms", 0)) for row in (supervisor_result.get("tool_execution_status") or [])
                 if row.get("executed")]
    return {"answer": "".join(phrases), "answer_kind": "BUSINESS_FACTS", "business_results": facts,
            "tool_references": references, "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "latency_ms": round(sum(durations), 3)}
