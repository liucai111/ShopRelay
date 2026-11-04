from __future__ import annotations

import hmac
import json
import secrets
import time
import uuid
import asyncio
import os
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from typing import Generator
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from typing import Literal
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.m0.config import ROOT, load_settings
from app.m1.chat import ChatFailure
from app.m1.chat_execution import execute_claimed_chat
from app.m1.db import make_engine, make_session_factory, readiness_state
from app.m1.models import (ConversationSession, Message, ObservabilityRecord, PendingAction, Run,
                           SessionMemorySummary, SupportTicket, Trace, User, safe_trace_json, utc_now)
from app.m1.services import (add_user_message, authenticated_user, create_auth_session, create_conversation,
                             append_run_event, claim_run, create_run, event_cursor_seq, find_request_run,
                             finish_run, list_conversations, order_for_owner, read_conversation, read_run, read_run_events,
                             recover_expired_run, request_hash, new_id,
                             revoke_auth_session, serialize_datetime, verify_demo_login)

SESSION_COOKIE = "shoprelay_session"
CSRF_COOKIE = "shoprelay_csrf"
CSRF_HEADER = "x-csrf-token"
SESSION_MAX_AGE = 12 * 60 * 60


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


class LoginInput(ApiModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class CreateSessionInput(ApiModel):
    title: str | None = Field(default=None, max_length=200)


class UserMessageInput(ApiModel):
    content: str = Field(min_length=1, max_length=10000)


class CreateRunInput(ApiModel):
    client_request_id: str = Field(min_length=1, max_length=128)
    request_payload: dict = Field(default_factory=dict)

    @field_validator("request_payload")
    @classmethod
    def bounded_payload(cls, value: dict) -> dict:
        try:
            size = len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as exc:
            raise ValueError("request_payload must contain JSON values") from exc
        if size > 65536:
            raise ValueError("request_payload must be at most 64 KiB")
        return value


class ChatInput(ApiModel):
    session_id: str | None = Field(default=None, min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=10000)
    client_request_id: str = Field(min_length=1, max_length=128)


class StrictApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvalCleanupInput(StrictApiModel):
    session_id: str = Field(min_length=1, max_length=64)
    run_ids: list[str] = Field(default_factory=list, max_length=20)
    marker: str = Field(pattern=r"^[a-f0-9]{32}$")


class SimulatedActionArguments(StrictApiModel):
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(min_length=1, max_length=200)


class ReturnActionArguments(StrictApiModel):
    order_id: str = Field(min_length=1, max_length=64)
    reason_code: Literal["QUALITY_ISSUE", "WRONG_ITEM", "DAMAGED_IN_TRANSIT", "OTHER"]
    reason_summary: str = Field(min_length=1, max_length=300)


class RefundActionArguments(StrictApiModel):
    order_id: str = Field(min_length=1, max_length=64)
    reason_code: Literal["PRE_SHIPMENT", "APPROVED_RETURN", "OTHER"]
    reason_summary: str = Field(min_length=1, max_length=300)


class CancelOrderArguments(StrictApiModel):
    order_id: str = Field(min_length=1, max_length=64)
    reason_code: Literal["USER_REQUEST", "OTHER"]
    reason_summary: str = Field(min_length=1, max_length=300)


class EscalationActionArguments(StrictApiModel):
    issue_category: Literal["ACCOUNT_ACCESS", "ORDER_STATUS", "RETURN_REFUND", "PRODUCT_QUALITY", "POLICY_QUESTION", "PRIVACY_SECURITY", "OTHER"]
    order_id: str | None = Field(default=None, min_length=1, max_length=64)
    user_description: str = Field(min_length=1, max_length=1000)
    issue_summary: str = Field(min_length=1, max_length=600)
    reason_code: str = Field(pattern=r"^[A-Z0-9_]{1,64}$")
    related_agents: list[Literal["order_logistics", "policy", "after_sales", "product"]] = Field(default_factory=list, max_length=4)
    verified_fact_refs: list[str] = Field(default_factory=list, max_length=12)
    unverified_claim_codes: list[str] = Field(default_factory=list, max_length=12)
    evidence_references: list[str] = Field(default_factory=list, max_length=12)

    @field_validator("verified_fact_refs", "evidence_references")
    @classmethod
    def typed_references(cls, values: list[str]) -> list[str]:
        import re
        if any(not re.fullmatch(r"(?:evidence|fact|tool|policy):[A-Za-z0-9._:-]{1,128}", item) for item in values):
            raise ValueError("references must use a registered typed reference form")
        return list(dict.fromkeys(values))

    @field_validator("unverified_claim_codes")
    @classmethod
    def stable_claim_codes(cls, values: list[str]) -> list[str]:
        import re
        if any(not re.fullmatch(r"[A-Z0-9_]{1,64}", item) for item in values):
            raise ValueError("claim codes must be stable identifiers")
        return list(dict.fromkeys(values))


class CreatePendingActionInput(StrictApiModel):
    tool_id: Literal["simulated_test_action", "create_return_request", "create_refund_request", "cancel_order", "escalate_to_human"]
    session_id: str = Field(min_length=1, max_length=64)
    run_id: str = Field(min_length=1, max_length=64)
    target_resource_id: str = Field(min_length=1, max_length=128)
    arguments: SimulatedActionArguments | ReturnActionArguments | RefundActionArguments | CancelOrderArguments | EscalationActionArguments
    idempotency_key: str = Field(min_length=1, max_length=128)

    @model_validator(mode="before")
    @classmethod
    def bind_arguments_to_tool(cls, value):
        """Validate the strict argument schema selected by the trusted tool id.

        Return and refund currently share the ``OTHER`` reason value, so a plain
        union may otherwise coerce a refund proposal into the return schema.
        """
        if not isinstance(value, dict):
            return value
        tool_schemas = {
            "simulated_test_action": SimulatedActionArguments,
            "create_return_request": ReturnActionArguments,
            "create_refund_request": RefundActionArguments,
            "cancel_order": CancelOrderArguments,
            "escalate_to_human": EscalationActionArguments,
        }
        schema = tool_schemas.get(value.get("tool_id"))
        raw_arguments = value.get("arguments")
        if schema is None or isinstance(raw_arguments, BaseModel):
            return value
        updated = dict(value)
        updated["arguments"] = schema.model_validate(raw_arguments)
        return updated


class ConfirmPendingActionInput(StrictApiModel):
    confirm: Literal[True]
    arguments_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ClosePendingActionInput(StrictApiModel):
    acknowledge: Literal[True]


@contextmanager
def db_session(factory) -> Generator[Session, None, None]:
    session = factory()
    try:
        yield session
    finally:
        session.close()


def origin_of(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def origin_allowed(request: Request) -> bool:
    origin = origin_of(request.headers.get("origin", ""))
    if origin is None:
        return False
    allowed = {origin_of(str(request.base_url).rstrip("/"))}
    allowed.update(filter(None, (origin_of(item.strip()) for item in request.app.state.allowed_origins.split(","))))
    return origin in allowed


def require_csrf(request: Request) -> None:
    cookie = request.cookies.get(CSRF_COOKIE, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not origin_allowed(request) or not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Origin or CSRF validation failed"})


def create_app(*, database_url: str | None = None, engine=None, chat_provider=None, index_path=None,
               retrieval_evaluation_at: str | None = None, self_rag_config=None,
               orchestrator_mode: str | None = None, checkpointer_factory=None,
               checkpointer_readiness=None, graph_services=None,
               observability_admin_user_ids: tuple[str, ...] | list[str] | None = None,
               action_writes_enabled: bool = False,
               simulated_test_actions_enabled: bool = False,
               return_actions_enabled: bool = False,
               refund_actions_enabled: bool = False,
               cancel_actions_enabled: bool = False,
               ticket_actions_enabled: bool = False) -> FastAPI:
    run_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="shoprelay-run")

    @asynccontextmanager
    async def lifespan(_app):
        try:
            if app.state.orchestrator_mode == "langgraph":
                from app.orchestration.checkpointer import (checkpoint_schema_ready,
                    require_checkpointer_dependencies)
                require_checkpointer_dependencies()
                if checkpointer_readiness is None:
                    ready = await checkpoint_schema_ready(app.state.database_url, app.state.checkpoint_schema)
                else:
                    ready = checkpointer_readiness(app.state.database_url, app.state.checkpoint_schema)
                    if asyncio.iscoroutine(ready):
                        ready = await ready
                if ready is not True:
                    raise RuntimeError("LangGraph checkpoint schema is not initialized or unavailable")
                app.state.checkpoint_status = "ready"
            if app.state.retrieval_prewarm_enabled:
                app.state.retrieval_prewarm = {"status": "warming"}
                try:
                    result = await asyncio.to_thread(app.state.retrieval_service.warmup)
                    app.state.retrieval_prewarm = result
                except Exception as exc:
                    # Keep startup available for the existing explicit BM25 fallback;
                    # a failed warmup never makes a later Hybrid run appear successful.
                    app.state.retrieval_prewarm = {
                        "status": "unavailable",
                        "error_code": getattr(exc, "code", "RETRIEVAL_PREWARM_FAILED"),
                        "error_type": type(exc).__name__,
                    }
            yield
        finally:
            await asyncio.to_thread(run_executor.shutdown, wait=True, cancel_futures=False)

    app = FastAPI(title="ShopRelay API", version="2.3.0",
                  description="M2.3 safety-first routing, bounded Self-RAG policy execution, persisted runs and recoverable SSE", lifespan=lifespan)
    settings = load_settings(require_api_key=False)
    selected_mode = orchestrator_mode if orchestrator_mode is not None else settings.orchestrator
    if selected_mode not in {"legacy", "langgraph"}:
        raise ValueError("orchestrator mode must be legacy or langgraph")
    if settings.m3_supervisor_enabled and selected_mode != "langgraph":
        raise ValueError("M3.1 Supervisor requires explicit LangGraph mode")
    selected_url = database_url or settings.database_url or f"sqlite:///{(ROOT / 'data' / 'shoprelay.sqlite3').as_posix()}"
    db_engine = engine or make_engine(selected_url)
    factory = make_session_factory(db_engine)
    app.state.engine = db_engine
    app.state.session_factory = factory
    app.state.database_url = selected_url
    parsed_database_url = make_url(selected_url)
    # Evaluation endpoints are deliberately separate from ordinary Synthetic
    # execution and default closed. Even when enabled, only the isolated local
    # PostgreSQL database is accepted.
    eval_dev_requested = os.getenv("SHOPRELAY_EVAL_DEV_API_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    eval_loopback = parsed_database_url.host in {"127.0.0.1", "localhost", "::1"}
    app.state.eval_dev_api_enabled = bool(eval_dev_requested and
        parsed_database_url.get_backend_name() == "postgresql" and
        parsed_database_url.database == "shoprelay_m11_test" and eval_loopback)
    cleanup_requested = os.getenv("SHOPRELAY_EVAL_CLEANUP_ENABLED", "false").strip().lower() in {"1", "true", "yes", "on"}
    cleanup_users = frozenset(x.strip() for x in os.getenv("SHOPRELAY_EVAL_CLEANUP_USER_IDS", "").split(",") if x.strip())
    app.state.eval_cleanup_enabled = bool(app.state.eval_dev_api_enabled and cleanup_requested and cleanup_users)
    app.state.eval_cleanup_user_ids = cleanup_users
    isolated_m42_database = (parsed_database_url.get_backend_name() == "postgresql" and
                             parsed_database_url.database == "shoprelay_m11_test")
    # Both switches are explicit and default closed. The test action additionally
    # requires the project-owned isolated PostgreSQL database name.
    app.state.action_writes_enabled = bool(action_writes_enabled)
    app.state.simulated_test_actions_enabled = bool(
        action_writes_enabled and simulated_test_actions_enabled and isolated_m42_database)
    app.state.return_actions_enabled = bool(
        action_writes_enabled and return_actions_enabled and isolated_m42_database)
    app.state.refund_actions_enabled = bool(
        action_writes_enabled and refund_actions_enabled and isolated_m42_database)
    app.state.cancel_actions_enabled = bool(
        action_writes_enabled and cancel_actions_enabled and isolated_m42_database)
    app.state.ticket_actions_enabled = bool(
        action_writes_enabled and ticket_actions_enabled and isolated_m42_database)
    app.state.orchestrator_mode = selected_mode
    app.state.checkpoint_schema = settings.checkpoint_schema
    app.state.graph_version = settings.graph_version
    app.state.execution_version = settings.execution_version
    app.state.checkpointer_factory = checkpointer_factory
    app.state.graph_services = graph_services
    app.state.checkpoint_status = "not_required" if selected_mode == "legacy" else "unchecked"
    configured_admins = settings.observability_admin_user_ids if observability_admin_user_ids is None else observability_admin_user_ids
    app.state.observability_admin_user_ids = frozenset(configured_admins)
    app.state.settings = settings
    app.state.allowed_origins = settings.allowed_origins
    app.state.cookie_secure = settings.cookie_secure
    app.state.chat_provider = chat_provider
    app.state.policy_index_path = index_path or ROOT / "data" / "index.json"
    # Optional server-side evaluation clock. It is never taken from a chat request.
    app.state.retrieval_evaluation_at = retrieval_evaluation_at
    app.state.self_rag_config = self_rag_config
    from app.m2.retrieval import IndexRegistry, RetrievalService, config_from_environment
    app.state.retrieval_service = RetrievalService(app.state.policy_index_path, config_from_environment(), registry=IndexRegistry())
    app.state.retrieval_prewarm_enabled = os.getenv("SHOPRELAY_RETRIEVAL_PREWARM", "false").strip().lower() in {"1", "true", "yes", "on"}
    app.state.retrieval_prewarm = {"status": "pending" if app.state.retrieval_prewarm_enabled else "disabled"}
    app.state.execution_owner_id = str(uuid.uuid4())
    app.state.run_executor = run_executor

    @app.middleware("http")
    async def trace_middleware(request: Request, call_next):
        request.state.trace_id = str(uuid.uuid4())
        response = await call_next(request)
        response.headers["X-Trace-Id"] = request.state.trace_id
        return response

    def session_dependency():
        with db_session(factory) as db:
            yield db

    def current_user(request: Request, db: Session = Depends(session_dependency)) -> str:
        user_id = authenticated_user(db, request.cookies.get(SESSION_COOKIE))
        if not user_id:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Login required"})
        return user_id

    def response_body(request: Request, **payload):
        return {"trace_id": request.state.trace_id, **payload}

    def require_observability_admin(user_id: str) -> None:
        if user_id not in app.state.observability_admin_user_ids:
            raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Administrator access is not enabled"})

    def require_dev_eval_api() -> None:
        if not app.state.eval_dev_api_enabled:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Not found"})

    def secure_cookie(request: Request) -> bool:
        return request.url.scheme == "https" or request.app.state.cookie_secure

    def prepare_chat_run(db: Session, user_id: str, payload: ChatInput):
        request_payload = {"message": payload.message, "requested_session_id": payload.session_id}
        scope = f"chat:{payload.session_id}" if payload.session_id is not None else "chat:auto"
        run = find_request_run(db, user_id, payload.client_request_id, scope)
        if run is not None:
            same_payload = run.request_hash == request_hash(request_payload)
        else:
            session_id = payload.session_id
            if session_id is None:
                session_id = create_conversation(db, user_id, None).session_id
            run, same_payload = create_run(db, user_id, session_id, payload.client_request_id,
                                           request_payload, idempotency_scope=scope)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        if not same_payload:
            raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_CONFLICT",
                "message": "client_request_id was already used with different input"})
        return run

    def start_run_events(run: Run, trace_id: str) -> None:
        with db_session(factory) as event_db:
            event_db.add(Trace(trace_id=new_id(), user_id=run.user_id, run_id=run.run_id,
                event_type="orchestrator_selected", status="RUNNING",
                event_json=safe_trace_json({"orchestrator": app.state.orchestrator_mode,
                    "graph_version": app.state.graph_version if app.state.orchestrator_mode == "langgraph" else None,
                    "execution_version": app.state.execution_version if app.state.orchestrator_mode == "langgraph" else None}),
                created_at=utc_now()))
            append_run_event(event_db, run.run_id, trace_id, "started", {"status": "RUNNING"})

    def run_chat_worker(run_id: str, user_id: str, message: str, trace_id: str) -> None:
        with db_session(factory) as worker_db:
            run = worker_db.get(Run, run_id)
            if (run is None or run.status != "RUNNING"
                    or run.execution_owner_id != app.state.execution_owner_id):
                return
            if app.state.orchestrator_mode == "langgraph":
                from app.orchestration.runtime import execute_claimed_chat_langgraph
                execute_claimed_chat_langgraph(worker_db, run=run, user_id=user_id, message=message,
                    trace_id=trace_id, provider=app.state.chat_provider, index_path=app.state.policy_index_path,
                    retrieval_service=app.state.retrieval_service, evaluation_at=app.state.retrieval_evaluation_at,
                    self_rag_config=app.state.self_rag_config, settings=app.state.settings,
                    session_factory=factory, database_url=app.state.database_url,
                    checkpointer_factory=app.state.checkpointer_factory, graph_services=app.state.graph_services)
            else:
                execute_claimed_chat(worker_db, run=run, user_id=user_id, message=message, trace_id=trace_id,
                                     provider=app.state.chat_provider, index_path=app.state.policy_index_path,
                                     retrieval_service=app.state.retrieval_service,
                                     evaluation_at=app.state.retrieval_evaluation_at,
                                     self_rag_config=app.state.self_rag_config)

    def cursor_seq(db: Session, user_id: str, run_id: str, after_seq: int | None,
                   last_event_id: str | None) -> int:
        if after_seq is not None and last_event_id:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT",
                "message": "Use either after_seq or Last-Event-ID, not both"})
        if last_event_id:
            seq = event_cursor_seq(db, user_id, run_id, last_event_id)
            if seq is None:
                raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT",
                    "message": "Last-Event-ID is not a persisted event for this run"})
            return seq
        seq = after_seq or 0
        owned = read_run(db, user_id, run_id)
        if owned is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        if seq > owned[1]:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT",
                "message": "after_seq is beyond the latest persisted event"})
        return seq

    def public_event(event) -> dict:
        return {"event_id": event.event_id, "run_id": event.run_id, "session_id": event.session_id,
                "trace_id": event.trace_id, "seq": event.seq, "timestamp": serialize_datetime(event.timestamp),
                "type": event.event_type, "payload": json.loads(event.payload_json)}

    @app.exception_handler(HTTPException)
    async def http_error_handler(request: Request, exc: HTTPException):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "HTTP_ERROR", "message": str(exc.detail)}
        body = {"code": detail.get("code", "HTTP_ERROR"), "message": detail.get("message", "Request failed"),
                "trace_id": getattr(request.state, "trace_id", str(uuid.uuid4())),
                "retryable": bool(detail.get("retryable", False))}
        if detail.get("details") is not None:
            body["details"] = detail["details"]
        return JSONResponse(status_code=exc.status_code, content=body, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        errors = [{"field": ".".join(str(part) for part in item["loc"]), "type": item["type"]} for item in exc.errors()]
        return JSONResponse(status_code=422, content={"code": "INVALID_ARGUMENT", "message": "Request validation failed",
                             "trace_id": getattr(request.state, "trace_id", str(uuid.uuid4())),
                             "retryable": False, "details": errors})

    @app.exception_handler(SQLAlchemyError)
    async def database_error_handler(request: Request, exc: SQLAlchemyError):
        return JSONResponse(status_code=503, content={"code": "DATABASE_UNAVAILABLE", "message": "Database operation unavailable",
                             "trace_id": getattr(request.state, "trace_id", str(uuid.uuid4())), "retryable": True})

    @app.exception_handler(Exception)
    async def unexpected_error_handler(request: Request, exc: Exception):
        return JSONResponse(status_code=500, content={"code": "INTERNAL_ERROR", "message": "Request could not be completed",
                             "trace_id": getattr(request.state, "trace_id", str(uuid.uuid4())), "retryable": False})

    @app.get("/health/live")
    def health_live(request: Request):
        return response_body(request, status="live")

    @app.get("/health/ready")
    def health_ready(request: Request):
        try:
            from app.m1.db import LATEST_SCHEMA_REVISION
            # Session memory is shared by legacy and LangGraph execution modes.
            required_revision = LATEST_SCHEMA_REVISION
            state, _ = readiness_state(db_engine, required_revision=required_revision)
            if state != "ready":
                return JSONResponse(status_code=503, content={"status": "degraded", "code": "NOT_READY",
                                     "message": "Database schema is not upgraded to the API revision",
                                     "retryable": False, "dependencies": {"db": "ready", "schema": "degraded"},
                                     "trace_id": request.state.trace_id})
            if app.state.orchestrator_mode == "langgraph" and app.state.checkpoint_status != "ready":
                return JSONResponse(status_code=503, content={"status": "degraded", "code": "CHECKPOINTER_NOT_READY",
                    "message": "LangGraph checkpoint store is not ready", "retryable": False,
                    "dependencies": {"db": "ready", "schema": "ready", "checkpointer": app.state.checkpoint_status},
                    "trace_id": request.state.trace_id})
            dependencies = {"db": "ready", "schema": "ready"}
            if app.state.orchestrator_mode == "langgraph":
                dependencies["checkpointer"] = app.state.checkpoint_status
            return response_body(request, status="ready", dependencies=dependencies)
        except SQLAlchemyError:
            return JSONResponse(status_code=503, content={"status": "unavailable", "code": "DATABASE_UNAVAILABLE",
                                 "message": "Database is unavailable", "retryable": True,
                                 "dependencies": {"db": "unavailable"}, "trace_id": request.state.trace_id})

    @app.get("/api/demo/csrf")
    def csrf_token(request: Request, response: Response):
        token = secrets.token_urlsafe(32)
        response.set_cookie(CSRF_COOKIE, token, httponly=False, secure=secure_cookie(request),
                            samesite="lax", max_age=SESSION_MAX_AGE, path="/")
        return response_body(request, csrf_token=token)

    @app.get("/api/demo/me")
    def current_user_profile(request: Request, db: Session = Depends(session_dependency),
                             user_id: str = Depends(current_user)):
        user = db.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Login required"})
        return response_body(request, user={"display_name": user.display_name})

    @app.post("/api/demo/login")
    def login(payload: LoginInput, request: Request, response: Response, db: Session = Depends(session_dependency)):
        require_csrf(request)
        user = verify_demo_login(db, payload.username, payload.password)
        if user is None:
            raise HTTPException(status_code=401, detail={"code": "UNAUTHENTICATED", "message": "Invalid demo credentials"})
        token = create_auth_session(db, user.user_id)
        secure = secure_cookie(request)
        response.set_cookie(SESSION_COOKIE, token, httponly=True, secure=secure, samesite="lax",
                            max_age=SESSION_MAX_AGE, path="/")
        return response_body(request, user={"user_id": user.user_id, "display_name": user.display_name})

    @app.post("/api/demo/logout")
    def logout(request: Request, response: Response, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        revoke_auth_session(db, request.cookies.get(SESSION_COOKIE))
        response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, secure=secure_cookie(request), samesite="lax")
        return response_body(request, status="logged_out")

    @app.post("/api/sessions")
    def create_session(payload: CreateSessionInput, request: Request, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        item = create_conversation(db, user_id, payload.title)
        return response_body(request, session={"session_id": item.session_id, "title": item.title,
                                               "created_at": serialize_datetime(item.created_at)})

    @app.get("/api/eval/dev/sessions/lookup")
    def eval_dev_session_lookup(request: Request, marker: str = Query(pattern=r"^[a-f0-9]{32}$"),
                                db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_dev_eval_api()
        title = f"m6.1-eval:{marker}"
        items = db.execute(select(ConversationSession).where(
            ConversationSession.user_id == user_id,
            ConversationSession.title == title).limit(2)).scalars().all()
        if len(items) > 1:
            raise HTTPException(status_code=409, detail={"code": "DUPLICATE_EVAL_SESSION_MARKER"})
        if not items:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        item = items[0]
        return response_body(request, session={"session_id": item.session_id,
            "title": item.title, "created_at": serialize_datetime(item.created_at)})

    @app.get("/api/eval/dev/target")
    def eval_dev_target(request: Request):
        """Non-sensitive environment assertion; unavailable in ordinary app mode."""
        require_dev_eval_api()
        return response_body(request, environment="ISOLATED_DEV", database="shoprelay_m11_test")

    @app.post("/api/eval/dev/cleanup")
    def eval_dev_cleanup(payload: EvalCleanupInput, request: Request,
                         db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        """Delete one explicitly tagged eval session; disabled except dedicated local Dev processes."""
        if not app.state.eval_cleanup_enabled:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Not found"})
        require_csrf(request)
        if user_id not in app.state.eval_cleanup_user_ids:
            raise HTTPException(status_code=403, detail={"code": "FORBIDDEN", "message": "Evaluation cleanup operator required"})
        from eval.m6_1.cleanup import EvalCleanupBlocked, cleanup_exact_eval_session
        try:
            counts = cleanup_exact_eval_session(db, owner_id=user_id, session_id=payload.session_id,
                run_ids=tuple(payload.run_ids), marker=payload.marker,
                checkpoint_schema=app.state.checkpoint_schema)
        except EvalCleanupBlocked as exc:
            raise HTTPException(status_code=409, detail={"code": "EVAL_CLEANUP_BLOCKED", "reason": str(exc)}) from None
        return response_body(request, status="CLEANED_EXACT_EVAL_SCOPE", deleted_counts=counts)

    @app.get("/api/sessions")
    def list_sessions(request: Request, limit: int = Query(default=100, ge=1, le=200),
                      db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        items = list_conversations(db, user_id, limit=limit)
        return response_body(request, sessions=[{"session_id": item.session_id, "title": item.title,
            "created_at": serialize_datetime(item.created_at),
            "updated_at": serialize_datetime(item.updated_at)} for item in items])

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str, request: Request, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        result = read_conversation(db, user_id, session_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        session, messages, runs = result
        return response_body(request, session={"session_id": session.session_id, "title": session.title,
            "created_at": serialize_datetime(session.created_at), "updated_at": serialize_datetime(session.updated_at),
            "messages": [{"message_id": item.message_id, "role": item.role, "content": item.content,
                          "status": item.status, "created_at": serialize_datetime(item.created_at)} for item in messages],
            "runs": [{"run_id": run.run_id, "status": run.status, "created_at": serialize_datetime(run.created_at),
                      "updated_at": serialize_datetime(run.updated_at), "answer": json.loads(run.result_json) if run.result_json else None,
                      "error": ({"code": run.error_code, "details": json.loads(run.error_detail_json) if run.error_detail_json else None}
                                if run.error_code else None), "events_cursor": read_run(db, user_id, run.run_id)[1]}
                     for run in reversed(runs)]})

    @app.post("/api/sessions/{session_id}/messages")
    def post_message(session_id: str, payload: UserMessageInput, request: Request, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        item = add_user_message(db, user_id, session_id, payload.content)
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        return response_body(request, message={"message_id": item.message_id, "session_id": item.session_id,
            "role": item.role, "status": item.status, "created_at": serialize_datetime(item.created_at)})

    @app.delete("/api/sessions/{session_id}/memory")
    def clear_session_memory_endpoint(session_id: str, request: Request,
                                      db: Session = Depends(session_dependency),
                                      user_id: str = Depends(current_user)):
        """Clear this owner's compacted memory while retaining immutable conversation messages."""
        require_csrf(request)
        from app.m5.memory import clear_session_memory
        if not clear_session_memory(db, owner_id=user_id, session_id=session_id):
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        return response_body(request, status="memory_cleared", session_id=session_id)

    @app.get("/api/sessions/{session_id}/memory")
    def get_session_memory_endpoint(session_id: str, request: Request,
                                    db: Session = Depends(session_dependency),
                                    user_id: str = Depends(current_user)):
        """Return an owner-scoped memory status projection; never return summary text."""
        owned = db.execute(select(ConversationSession.session_id).where(
            ConversationSession.session_id == session_id,
            ConversationSession.user_id == user_id)).scalar_one_or_none()
        if owned is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        latest = db.execute(select(SessionMemorySummary).where(
            SessionMemorySummary.owner_id == user_id,
            SessionMemorySummary.session_id == session_id
        ).order_by(SessionMemorySummary.version.desc()).limit(1)).scalar_one_or_none()
        latest_run = db.execute(select(Run).where(
            Run.user_id == user_id, Run.session_id == session_id
        ).order_by(Run.created_at.desc()).limit(1)).scalar_one_or_none()
        telemetry = {}
        if latest_run and latest_run.result_json:
            try:
                telemetry = (json.loads(latest_run.result_json).get("context_memory") or {})
            except (TypeError, ValueError, AttributeError):
                telemetry = {}
        if latest_run:
            trace_row = db.execute(select(Trace).where(
                Trace.user_id == user_id, Trace.run_id == latest_run.run_id,
                Trace.event_type == "session_memory_context"
            ).order_by(Trace.created_at.desc()).limit(1)).scalar_one_or_none()
            if trace_row is not None:
                try:
                    telemetry = {**json.loads(trace_row.event_json), **telemetry}
                except (TypeError, ValueError):
                    pass
        if latest and latest.is_clear_marker:
            telemetry = {}
        try:
            from app.m5.memory import memory_budget_from_environment
            budget = memory_budget_from_environment()
            thresholds = {"soft": budget.soft_threshold, "hard": budget.hard_threshold,
                          "auto_compact": budget.auto_threshold}
            recent_message_count = budget.recent_message_count
        except (ValueError, TypeError):
            thresholds = {"soft": 0.5, "hard": 0.7, "auto_compact": 0.85}
            recent_message_count = 6
        try:
            source_ids = json.loads(latest.source_message_ids_json) if latest else []
            if not isinstance(source_ids, list):
                source_ids = []
        except (TypeError, ValueError):
            source_ids = []
        status = ("CLEARED" if latest and latest.is_clear_marker else
                  "PERSISTED" if latest and latest.summary_json else "NOT_CREATED")
        return response_body(request, memory={
            "session_id": session_id, "status": status,
            "summary_version": latest.version if latest else None,
            "summary_persisted": bool(latest and latest.summary_json and not latest.is_clear_marker),
            "summary_content_exposed": False,
            "summary_created_at": serialize_datetime(latest.created_at) if latest else None,
            "source_run_id": latest.source_run_id if latest else None,
            "source_message_count": len(source_ids),
            "source_through_sequence": latest.source_through_sequence if latest else 0,
            "estimated_tokens_before": (latest.estimated_tokens_before if latest and not latest.is_clear_marker
                                        else telemetry.get("estimated_tokens_before")),
            "estimated_tokens_after": (latest.estimated_tokens_after if latest and not latest.is_clear_marker
                                       else telemetry.get("estimated_tokens_after")),
            "compression_ratio": telemetry.get("compression_ratio"),
            "effective_context_tokens": telemetry.get("effective_context_tokens"),
            "load_ratio": (round(float(telemetry.get("estimated_tokens_before")) /
                                  float(telemetry.get("effective_context_tokens")), 4)
                           if telemetry.get("estimated_tokens_before") is not None and
                              telemetry.get("effective_context_tokens") else None),
            "level": telemetry.get("level", "NONE"),
            "thresholds": telemetry.get("thresholds") or thresholds,
            "recent_message_count": recent_message_count,
            "last_run_id": latest_run.run_id if latest_run else None,
            "last_run_status": latest_run.status if latest_run else None,
            "last_compaction_persisted": telemetry.get("summary_persisted"),
            "last_compaction_version": telemetry.get("summary_version"),
            "token_estimates_are_heuristic": True,
        })

    @app.post("/api/sessions/{session_id}/runs")
    def post_run(session_id: str, payload: CreateRunInput, request: Request, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        run, same_payload = create_run(db, user_id, session_id, payload.client_request_id, payload.request_payload)
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        if not same_payload:
            raise HTTPException(status_code=409, detail={"code": "IDEMPOTENCY_CONFLICT", "message": "client_request_id was already used with different input"})
        return response_body(request, run={"run_id": run.run_id, "session_id": run.session_id,
                                           "status": run.status, "created_at": serialize_datetime(run.created_at)})

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str, request: Request, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        result = read_run(db, user_id, run_id)
        if result is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        run, events_cursor = result
        if run.status == "RUNNING":
            run = recover_expired_run(db, run.run_id)
            run, events_cursor = read_run(db, user_id, run_id)
        result_payload = json.loads(run.result_json) if run.result_json else None
        return response_body(request, run={"run_id": run.run_id, "session_id": run.session_id,
            "status": run.status, "error": ({"code": run.error_code,
                "details": json.loads(run.error_detail_json) if run.error_detail_json else None} if run.error_code else None),
            "answer": result_payload,
            "model_id": run.model_id, "usage": json.loads(run.usage_json) if run.usage_json else None,
            "latency_ms": run.latency_ms, "started_at": serialize_datetime(run.started_at),
            "finished_at": serialize_datetime(run.finished_at),
            "events_cursor": events_cursor,
            "created_at": serialize_datetime(run.created_at), "updated_at": serialize_datetime(run.updated_at)})

    @app.get("/api/eval/dev/runs/lookup")
    def eval_dev_run_lookup(request: Request, session_id: str = Query(min_length=1, max_length=64),
                            client_request_id: str = Query(min_length=1, max_length=128),
                            db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        """Owner-scoped idempotency recovery; returns no prompt, answer or tool data."""
        require_dev_eval_api()
        owned_session = db.execute(select(ConversationSession.session_id).where(
            ConversationSession.session_id == session_id,
            ConversationSession.user_id == user_id)).scalar_one_or_none()
        if owned_session is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        run = find_request_run(db, user_id, client_request_id, f"chat:{session_id}")
        if run is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        return response_body(request, run={"run_id": run.run_id, "session_id": run.session_id,
            "status": run.status, "created_at": serialize_datetime(run.created_at)})

    @app.get("/api/runs/{run_id}/execution")
    def get_run_execution(run_id: str, request: Request,
                          db: Session = Depends(session_dependency),
                          user_id: str = Depends(current_user)):
        """Expose a small, owner-scoped projection, never raw internal traces or logs."""
        owned = read_run(db, user_id, run_id)
        if owned is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        run, _ = owned
        result = {}
        if run.result_json:
            try:
                decoded = json.loads(run.result_json)
                if isinstance(decoded, dict):
                    result = decoded
            except (TypeError, ValueError):
                result = {}
        route = result.get("route") if isinstance(result.get("route"), dict) else {}
        spans = []
        records = db.execute(select(ObservabilityRecord).where(
            ObservabilityRecord.run_id == run_id, ObservabilityRecord.user_id == user_id,
            ObservabilityRecord.record_type == "span"
        ).order_by(ObservabilityRecord.timestamp).limit(300)).scalars().all()
        span_keys = ("trace_id", "span_id", "parent_span_id", "node_name", "strategy", "start_time",
                     "end_time", "duration_ms", "status", "error_code", "graph_version")
        for row in records:
            try:
                span = json.loads(row.payload_json)
            except (TypeError, ValueError):
                continue
            if isinstance(span, dict):
                spans.append({key: span.get(key) for key in span_keys if key in span})
        trace_events = []
        trace_keys = {"policy_retrieval_aspect": ("version", "strategy_selected", "strategy_executed",
                          "strategy_trigger_reason", "degraded", "knowledge_version", "index_fingerprint",
                          "evidence_ids", "missing_aspect_ids", "request_mode"),
                      "policy_retrieval_completed": ("version", "strategy_selected", "strategy_executed",
                          "strategy_trigger_reason", "degraded", "knowledge_version", "index_fingerprint",
                          "evidence_ids", "missing_aspect_ids", "request_mode"),
                      "session_memory_context": ("level", "estimated_tokens_before", "estimated_tokens_after",
                          "compression_ratio", "effective_context_tokens", "thresholds", "summary_generation",
                          "summary_model_calls", "summary_cost_estimate_usd", "summary_latency_ms",
                          "provider_prompt_tokens", "estimate_to_provider_ratio", "summary_persisted",
                          "summary_version", "source_message_count")}
        for event_type, keys in trace_keys.items():
            rows = db.execute(select(Trace).where(Trace.user_id == user_id, Trace.run_id == run_id,
                Trace.event_type == event_type).order_by(Trace.created_at).limit(100)).scalars().all()
            for row in rows:
                try:
                    payload = json.loads(row.event_json)
                except (TypeError, ValueError):
                    continue
                if isinstance(payload, dict):
                    trace_events.append({"event_type": event_type, "status": row.status,
                        "created_at": serialize_datetime(row.created_at),
                        "data": {key: payload.get(key) for key in keys if key in payload}})
        evidence_rows = result.get("evidence") if isinstance(result.get("evidence"), list) else []
        citation_rows = result.get("citations") if isinstance(result.get("citations"), list) else []
        evidence_by_id = {row.get("chunk_id"): row for row in evidence_rows
                          if isinstance(row, dict) and isinstance(row.get("chunk_id"), str)}
        citations = []
        for citation in citation_rows[:100]:
            chunk_id = citation.get("chunk_id") if isinstance(citation, dict) else citation
            source = evidence_by_id.get(chunk_id)
            if not isinstance(source, dict) or not isinstance(chunk_id, str):
                continue
            display_source = {**source, **(citation if isinstance(citation, dict) else {})}
            citations.append({key: display_source.get(key) for key in
                ("chunk_id", "title", "content", "source", "source_type", "version", "policy_version",
                 "policy_document_version", "effective_from", "effective_to", "evaluation_at",
                 "knowledge_version", "index_fingerprint", "rank", "score", "result_refs",
                 "retrieval_call_ids") if key in display_source} |
                {"validity_status": "RUNTIME_CITATION_CHECKED" if run.status == "SUCCEEDED" and
                    result.get("strategy") in {"SINGLE", "MULTI"} else "SAVED_RUN_REFERENCE"})
        retrieved = []
        cited_ids = {item.get("chunk_id") for item in citations}
        for item in evidence_rows[:100]:
            if not isinstance(item, dict) or not isinstance(item.get("chunk_id"), str):
                continue
            retrieved.append({key: item.get(key) for key in
                ("chunk_id", "title", "source", "source_type", "version", "policy_version",
                 "policy_document_version", "effective_from", "effective_to", "evaluation_at",
                 "knowledge_version", "index_fingerprint", "rank", "score") if key in item} |
                {"citation_status": "CITED" if item.get("chunk_id") in cited_ids else "RETRIEVED_NOT_CITED"})
        retrieval = result.get("retrieval") if isinstance(result.get("retrieval"), dict) else {}
        policy_retrieval = result.get("policy_retrieval") if isinstance(result.get("policy_retrieval"), dict) else {}
        policy_retrieval_audit = []
        for item in result.get("policy_retrieval_audit", []) if isinstance(result.get("policy_retrieval_audit"), list) else []:
            if isinstance(item, dict):
                policy_retrieval_audit.append({key: item.get(key) for key in
                    ("audit_schema_version", "source_type", "capability", "call_id", "run_id", "trace_id", "result_ref",
                     "parameter_fingerprint", "aspect_id", "retrieval_mode", "strategy_selected",
                     "strategy_executed", "evaluation_at", "status", "executed", "duration_ms",
                     "error_code", "knowledge_version", "policy_document_version", "index_fingerprint", "evidence_ids",
                     "document_versions", "degraded") if key in item})
        handoffs = []
        for item in result.get("handoffs", []) if isinstance(result.get("handoffs"), list) else []:
            if isinstance(item, dict):
                handoffs.append({key: item.get(key) for key in
                    ("handoff_id", "source_agent", "target_agent", "required_capabilities", "context_refs",
                     "evidence_refs", "status", "error_code") if key in item})
        tool_calls = []
        for item in result.get("tool_execution_status", []) if isinstance(result.get("tool_execution_status"), list) else []:
            if isinstance(item, dict):
                tool_calls.append({key: item.get(key) for key in
                    ("tool_id", "capability", "status", "executed", "duration_ms", "error_code",
                     "retrieval_mode", "knowledge_version", "index_fingerprint", "evidence_ids",
                     "retrieval_count", "degraded") if key in item})
        tool_references = []
        for item in result.get("tool_references", []) if isinstance(result.get("tool_references"), list) else []:
            if isinstance(item, dict) and isinstance(item.get("result_ref"), str):
                tool_references.append({key: item.get(key) for key in
                    ("tool", "source", "result_ref") if key in item})
        business_evidence = []
        for item in result.get("business_evidence", []) if isinstance(result.get("business_evidence"), list) else []:
            if not isinstance(item, dict):
                continue
            projected = {key: item.get(key) for key in
                ("kind", "source", "tool", "result_ref", "provenance_ref", "scope_complete", "availability")
                if key in item}
            resource = item.get("resource")
            if isinstance(resource, dict):
                projected["resource"] = {key: resource.get(key) for key in ("type", "id", "name") if key in resource}
            facts = item.get("facts")
            if isinstance(facts, list):
                projected["facts"] = [{key: fact.get(key) for key in ("field", "value", "unit") if key in fact}
                    for fact in facts if isinstance(fact, dict)]
            business_evidence.append(projected)
        tool_call_audit = []
        for item in result.get("tool_call_audit", []) if isinstance(result.get("tool_call_audit"), list) else []:
            if isinstance(item, dict):
                tool_call_audit.append({key: item.get(key) for key in
                    ("tool_call_id", "tool", "capability", "resource_digest", "parameter_fingerprint", "result_ref",
                     "status", "executed", "error_code", "duration_ms", "run_id", "trace_id") if key in item})
        return response_body(request, execution={
            "run_id": run_id, "session_id": run.session_id, "status": run.status,
            "strategy": result.get("strategy") or route.get("strategy"),
            "sub_strategy": result.get("sub_strategy") or route.get("sub_strategy"),
            "route_source": route.get("decision_source"),
            "skill_id": route.get("skill_id"), "skill_version": route.get("skill_version"),
            "route_latency_ms": route.get("latency_ms"),
            "retrieval": {key: value for key, value in retrieval.items() if key in
                ("mode", "strategy_selected", "strategy_executed", "config_version", "config_hash",
                 "knowledge_version", "index_fingerprint", "evaluation_at", "retrieval_count", "degraded")},
            "policy_retrieval": {key: value for key, value in policy_retrieval.items() if key in
                ("version", "strategy_selected", "strategy_executed", "strategy_trigger_reason",
                 "degraded", "knowledge_version", "index_fingerprint", "evidence_ids",
                 "missing_aspect_ids", "request_mode", "evaluation_at")},
            "citations": citations, "retrieved_evidence": retrieved,
            "policy_retrieval_audit": policy_retrieval_audit,
            "spans": spans, "trace_events": trace_events, "handoffs": handoffs,
            "tool_calls": tool_calls, "tool_references": tool_references,
            "business_evidence": business_evidence, "tool_call_audit": tool_call_audit,
            "error_code": run.error_code,
            "finished_at": serialize_datetime(run.finished_at),
        })

    def ensure_action_enabled(request: Request, tool_id: str | None = None) -> None:
        simulated_ok = request.app.state.simulated_test_actions_enabled and tool_id in {None, "simulated_test_action"}
        return_ok = request.app.state.return_actions_enabled and tool_id in {None, "create_return_request"}
        refund_ok = request.app.state.refund_actions_enabled and tool_id in {None, "create_refund_request"}
        cancel_ok = request.app.state.cancel_actions_enabled and tool_id in {None, "cancel_order"}
        ticket_ok = request.app.state.ticket_actions_enabled and tool_id in {None, "escalate_to_human"}
        if not (simulated_ok or return_ok or refund_ok or cancel_ok or ticket_ok):
            raise HTTPException(status_code=503, detail={"code": "ACTION_EXECUTION_DISABLED",
                "message": "The requested isolated action adapter is disabled"})

    def action_http_error(exc):
        from app.m4.actions import ActionError
        if not isinstance(exc, ActionError):
            raise exc
        raise HTTPException(status_code=exc.status_code,
                            detail={"code": exc.code, "message": exc.message})

    @app.post("/api/actions")
    def post_pending_action(payload: CreatePendingActionInput, request: Request,
                            db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        ensure_action_enabled(request, payload.tool_id)
        from app.m4.actions import (ActionError, action_view, create_pending_action,
                                    prepare_return_action, replay_return_action_proposal,
                                    prepare_refund_action, replay_refund_action_proposal,
                                    prepare_cancel_action, replay_cancel_action_proposal)
        arguments = payload.arguments.model_dump()
        if payload.tool_id == "create_return_request":
            if not request.app.state.return_actions_enabled:
                raise HTTPException(status_code=503, detail={"code": "ACTION_EXECUTION_DISABLED",
                    "message": "The isolated return adapter is disabled"})
            if not isinstance(payload.arguments, ReturnActionArguments) or payload.arguments.order_id != payload.target_resource_id:
                raise HTTPException(status_code=422, detail={"code": "INVALID_ACTION_ARGUMENTS",
                    "message": "Return target must match the requested order"})
            try:
                prior = replay_return_action_proposal(db, owner_id=user_id,
                    session_id=payload.session_id, run_id=payload.run_id,
                    target_resource_id=payload.target_resource_id,
                    reason_code=payload.arguments.reason_code,
                    reason_summary=payload.arguments.reason_summary,
                    idempotency_key=payload.idempotency_key)
            except ActionError as exc:
                action_http_error(exc)
            if prior is not None:
                committed = prior.tool_id == "create_return_request" and prior.status == "SUCCEEDED"
                return response_body(request, action=action_view(prior), replayed=True,
                    business_effect="RETURN_REQUEST_SUBMITTED_PENDING_REVIEW" if committed else "NONE_PENDING_CONFIRMATION")
            try:
                arguments, _proposal = prepare_return_action(db, owner_id=user_id,
                    order_id=payload.arguments.order_id, reason_code=payload.arguments.reason_code,
                    reason_summary=payload.arguments.reason_summary,
                    retrieval_service=request.app.state.retrieval_service)
            except ActionError as exc:
                action_http_error(exc)
        elif payload.tool_id == "create_refund_request":
            if not request.app.state.refund_actions_enabled:
                raise HTTPException(status_code=503, detail={"code": "ACTION_EXECUTION_DISABLED",
                    "message": "The isolated refund adapter is disabled"})
            if not isinstance(payload.arguments, RefundActionArguments) or payload.arguments.order_id != payload.target_resource_id:
                raise HTTPException(status_code=422, detail={"code": "INVALID_ACTION_ARGUMENTS",
                    "message": "Refund target must match the requested order"})
            try:
                prior = replay_refund_action_proposal(db, owner_id=user_id,
                    session_id=payload.session_id, run_id=payload.run_id,
                    target_resource_id=payload.target_resource_id,
                    reason_code=payload.arguments.reason_code,
                    reason_summary=payload.arguments.reason_summary,
                    idempotency_key=payload.idempotency_key)
            except ActionError as exc:
                action_http_error(exc)
            if prior is not None:
                committed = prior.status == "SUCCEEDED"
                return response_body(request, action=action_view(prior), replayed=True,
                    business_effect="REFUND_REQUEST_SUBMITTED_PENDING_REVIEW" if committed else "NONE_PENDING_CONFIRMATION",
                    payment_effect="NONE")
            try:
                arguments, _proposal = prepare_refund_action(db, owner_id=user_id,
                    order_id=payload.arguments.order_id, reason_code=payload.arguments.reason_code,
                    reason_summary=payload.arguments.reason_summary,
                    retrieval_service=request.app.state.retrieval_service)
            except ActionError as exc:
                action_http_error(exc)
        elif payload.tool_id == "cancel_order":
            if not request.app.state.cancel_actions_enabled:
                raise HTTPException(status_code=503, detail={"code": "ACTION_EXECUTION_DISABLED",
                    "message": "The isolated order-cancellation adapter is disabled"})
            if not isinstance(payload.arguments, CancelOrderArguments) or payload.arguments.order_id != payload.target_resource_id:
                raise HTTPException(status_code=422, detail={"code": "INVALID_ACTION_ARGUMENTS",
                    "message": "Cancellation target must match the requested order"})
            try:
                prior = replay_cancel_action_proposal(db, owner_id=user_id,
                    session_id=payload.session_id, run_id=payload.run_id,
                    target_resource_id=payload.target_resource_id,
                    reason_code=payload.arguments.reason_code,
                    reason_summary=payload.arguments.reason_summary,
                    idempotency_key=payload.idempotency_key)
            except ActionError as exc:
                action_http_error(exc)
            if prior is not None:
                result = json.loads(prior.result_json) if prior.result_json else None
                return response_body(request, action=action_view(prior), replayed=True,
                    business_effect=("ORDER_CANCELLED" if prior.status == "SUCCEEDED" else
                        "NONE_PENDING_CONFIRMATION" if prior.status == "PENDING_CONFIRMATION" else "NONE"),
                    refund_effect=("SUBMITTED_PENDING_REVIEW" if result and result.get("refund_id") else "NONE"),
                    payment_effect="NOT_EXECUTED")
            try:
                arguments, _proposal = prepare_cancel_action(db, owner_id=user_id,
                    order_id=payload.arguments.order_id, reason_code=payload.arguments.reason_code,
                    reason_summary=payload.arguments.reason_summary,
                    retrieval_service=request.app.state.retrieval_service)
            except ActionError as exc:
                action_http_error(exc)
        elif payload.tool_id == "escalate_to_human":
            if not request.app.state.ticket_actions_enabled:
                raise HTTPException(status_code=503, detail={"code": "ACTION_EXECUTION_DISABLED",
                    "message": "The isolated ticket adapter is disabled"})
            if not isinstance(payload.arguments, EscalationActionArguments):
                raise HTTPException(status_code=422, detail={"code": "INVALID_ACTION_ARGUMENTS",
                    "message": "Ticket proposal does not match its strict schema"})
            expected_target = payload.arguments.order_id or "SUPPORT-GENERAL"
            if payload.target_resource_id != expected_target:
                raise HTTPException(status_code=422, detail={"code": "INVALID_ACTION_ARGUMENTS",
                    "message": "Ticket target must match its optional order reference"})
        try:
            item, replayed = create_pending_action(db, owner_id=user_id,
                session_id=payload.session_id, run_id=payload.run_id,
                trace_id=request.state.trace_id, tool_id=payload.tool_id,
                target_resource_id=payload.target_resource_id,
                arguments=arguments, idempotency_key=payload.idempotency_key,
                environment_name="shoprelay_m11_test",
                return_actions_enabled=request.app.state.return_actions_enabled,
                refund_actions_enabled=request.app.state.refund_actions_enabled,
                cancel_actions_enabled=request.app.state.cancel_actions_enabled,
                ticket_actions_enabled=request.app.state.ticket_actions_enabled)
        except ActionError as exc:
            action_http_error(exc)
        return response_body(request, action=action_view(item), replayed=replayed,
                             business_effect=("TICKET_SUBMITTED_PENDING_ACCEPTANCE" if item.tool_id == "escalate_to_human" and item.status == "SUCCEEDED" else "NONE_PENDING_CONFIRMATION"),
                             ticket=(json.loads(item.result_json) if item.tool_id == "escalate_to_human" and item.result_json else None),
                             confirmation_required=True,
                             execution_enabled=request.app.state.ticket_actions_enabled)

    @app.get("/api/actions")
    def list_pending_actions(request: Request, status: str | None = Query(default=None, max_length=32),
                             db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        query = select(PendingAction).where(PendingAction.owner_id == user_id)
        if status:
            query = query.where(PendingAction.status == status.upper())
        rows = db.execute(query.order_by(PendingAction.created_at.desc()).limit(100)).scalars().all()
        from app.m4.actions import action_view
        return response_body(request, actions=[action_view(item) for item in rows])

    @app.get("/api/sessions/{session_id}/actions")
    def list_session_action_proposals(session_id: str, request: Request,
                                      db: Session = Depends(session_dependency),
                                      user_id: str = Depends(current_user)):
        """Read-only, field-allowlisted Pending Action projection for this user's session."""
        session = db.execute(select(ConversationSession.session_id).where(
            ConversationSession.session_id == session_id,
            ConversationSession.user_id == user_id)).scalar_one_or_none()
        if session is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Session not found"})
        from app.m4.actions import action_workbench_view, server_permission_granted
        rows = db.execute(select(PendingAction).where(
            PendingAction.owner_id == user_id,
            PendingAction.session_id == session_id,
            PendingAction.tool_id.in_(("create_return_request", "create_refund_request",
                                       "cancel_order", "escalate_to_human")),
        ).order_by(PendingAction.created_at.desc()).limit(100)).scalars().all()
        tool_switches = {
            "create_return_request": request.app.state.return_actions_enabled,
            "create_refund_request": request.app.state.refund_actions_enabled,
            "cancel_order": request.app.state.cancel_actions_enabled,
            "escalate_to_human": request.app.state.ticket_actions_enabled,
        }
        isolated_action_database = (
            request.app.state.engine.dialect.name == "postgresql" and
            request.app.state.engine.url.database == "shoprelay_m11_test"
        )
        actions = []
        for item in rows:
            view = action_workbench_view(item)
            enabled = bool(isolated_action_database and tool_switches.get(item.tool_id, False) and
                           request.app.state.action_writes_enabled and
                           server_permission_granted(user_id, item.tool_id, "shoprelay_m11_test"))
            can_confirm = (enabled and item.status == "PENDING_CONFIRMATION" and
                           item.expires_at > utc_now())
            # The digest is only returned to its authenticated owner when this
            # exact isolated app instance is authorized to execute the action.
            view.update({
                "execution_available": can_confirm,
                "execution_scope": "ISOLATED_TEST_DATABASE" if enabled else "DISABLED",
                "confirmation_hash": item.arguments_hash if can_confirm else None,
            })
            actions.append(view)
        return response_body(request, actions=actions)

    @app.get("/api/actions/{action_id}")
    def get_pending_action(action_id: str, request: Request,
                           db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        from app.m4.actions import action_view
        item = db.execute(select(PendingAction).where(PendingAction.action_id == action_id,
            PendingAction.owner_id == user_id)).scalar_one_or_none()
        if item is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Action not found"})
        return response_body(request, action=action_view(item))

    @app.post("/api/actions/{action_id}/confirm")
    def confirm_action(action_id: str, payload: ConfirmPendingActionInput, request: Request,
                       db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        from app.m4.actions import ActionError, action_view, confirm_pending_action
        pending = db.execute(select(PendingAction).where(
            PendingAction.action_id == action_id,
            PendingAction.owner_id == user_id)).scalar_one_or_none()
        if pending is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Action not found"})
        ensure_action_enabled(request, pending.tool_id)
        try:
            item, replayed = confirm_pending_action(db, owner_id=user_id, action_id=action_id,
                simulated_enabled=request.app.state.simulated_test_actions_enabled,
                environment_name="shoprelay_m11_test", confirmed_arguments_hash=payload.arguments_hash,
                request_trace_id=request.state.trace_id,
                return_actions_enabled=request.app.state.return_actions_enabled,
                refund_actions_enabled=request.app.state.refund_actions_enabled,
                cancel_actions_enabled=request.app.state.cancel_actions_enabled,
                ticket_actions_enabled=request.app.state.ticket_actions_enabled,
                retrieval_service=request.app.state.retrieval_service)
        except ActionError as exc:
            action_http_error(exc)
        view=action_view(item)
        return response_body(request, action=view, answer=view['answer'], replayed=replayed,
                             business_effect=("TICKET_SUBMITTED_PENDING_ACCEPTANCE" if item.tool_id == "escalate_to_human" and item.status == "SUCCEEDED"
                                 else "RETURN_REQUEST_SUBMITTED_PENDING_REVIEW" if item.tool_id == "create_return_request" and item.status == "SUCCEEDED"
                                 else "REFUND_REQUEST_SUBMITTED_PENDING_REVIEW" if item.tool_id == "create_refund_request" and item.status == "SUCCEEDED"
                                 else "ORDER_CANCELLED" if item.tool_id == "cancel_order" and item.status == "SUCCEEDED"
                                 else "COMMITTED_SIMULATED_TEST_RECEIPT" if item.tool_id == "simulated_test_action" and item.status == "SUCCEEDED"
                                 else "NONE"),
                             refund_effect=("SUBMITTED_PENDING_REVIEW" if item.tool_id == "cancel_order" and item.status == "SUCCEEDED" and item.result_json and json.loads(item.result_json).get("refund_id") else "NONE" if item.tool_id == "cancel_order" else None),
                             payment_effect="NOT_EXECUTED" if item.tool_id in {"create_refund_request", "cancel_order"} else None,
            ticket=(json.loads(item.result_json) if item.tool_id == "escalate_to_human" and item.result_json else None),
                             action_status=item.status,
                             run_status=(db.execute(select(Run.status).where(Run.run_id == item.run_id,
                                 Run.user_id == user_id)).scalar_one_or_none()))

    def ticket_view(row: SupportTicket) -> dict:
        return {"ticket_id": row.ticket_id, "order_id": row.order_id,
            "session_id": row.session_id, "source_run_id": row.source_run_id,
            "issue_category": row.issue_category, "priority": row.priority,
            "user_description": row.user_description, "issue_summary": row.issue_summary,
            "related_agents": json.loads(row.related_agents_json),
            "verified_fact_refs": json.loads(row.verified_fact_refs_json),
            "unverified_claim_codes": json.loads(row.unverified_claim_codes_json),
            "evidence_references": json.loads(row.evidence_references_json),
            "status": row.status, "assigned": False, "notification_sent": False,
            "created_at": serialize_datetime(row.created_at),
            "updated_at": serialize_datetime(row.updated_at), "audit_reference": row.audit_reference}

    @app.get("/api/tickets")
    def list_tickets(request: Request, db: Session = Depends(session_dependency),
                     user_id: str = Depends(current_user)):
        rows = db.execute(select(SupportTicket).where(SupportTicket.owner_id == user_id)
            .order_by(SupportTicket.created_at.desc()).limit(100)).scalars().all()
        return response_body(request, tickets=[ticket_view(row) for row in rows])

    @app.get("/api/tickets/{ticket_id}")
    def get_ticket(ticket_id: str, request: Request,
                   db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        row = db.execute(select(SupportTicket).where(SupportTicket.ticket_id == ticket_id,
            SupportTicket.owner_id == user_id)).scalar_one_or_none()
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Ticket not found"})
        return response_body(request, ticket=ticket_view(row))

    @app.post("/api/actions/{action_id}/reject")
    def reject_action(action_id: str, payload: ClosePendingActionInput, request: Request,
                      db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        from app.m4.actions import ActionError, action_view, close_pending_action
        try:
            item = close_pending_action(db, owner_id=user_id, action_id=action_id,
                                        target_status="REJECTED", request_trace_id=request.state.trace_id)
        except ActionError as exc:
            action_http_error(exc)
        return response_body(request, action=action_view(item), business_effect="NONE")

    @app.post("/api/actions/{action_id}/cancel")
    def cancel_action(action_id: str, payload: ClosePendingActionInput, request: Request,
                      db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        from app.m4.actions import ActionError, action_view, close_pending_action
        try:
            item = close_pending_action(db, owner_id=user_id, action_id=action_id,
                                        target_status="CANCELLED", request_trace_id=request.state.trace_id)
        except ActionError as exc:
            action_http_error(exc)
        return response_body(request, action=action_view(item), business_effect="NONE")

    @app.post("/api/chat")
    def post_chat(payload: ChatInput, request: Request, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        run = prepare_chat_run(db, user_id, payload)
        if run.status == "CREATED":
            if claim_run(db, run, execution_owner_id=app.state.execution_owner_id):
                run = db.get(Run, run.run_id)
                start_run_events(run, request.state.trace_id)
                if app.state.orchestrator_mode == "langgraph":
                    from app.orchestration.runtime import execute_claimed_chat_langgraph
                    outcome = execute_claimed_chat_langgraph(db, run=run, user_id=user_id, message=payload.message,
                        trace_id=request.state.trace_id, provider=app.state.chat_provider,
                        index_path=app.state.policy_index_path, retrieval_service=app.state.retrieval_service,
                        evaluation_at=app.state.retrieval_evaluation_at, self_rag_config=app.state.self_rag_config,
                        settings=app.state.settings, session_factory=factory, database_url=app.state.database_url,
                        checkpointer_factory=app.state.checkpointer_factory, graph_services=app.state.graph_services)
                else:
                    outcome = execute_claimed_chat(db, run=run, user_id=user_id, message=payload.message,
                        trace_id=request.state.trace_id, provider=app.state.chat_provider,
                        index_path=app.state.policy_index_path, retrieval_service=app.state.retrieval_service,
                        evaluation_at=app.state.retrieval_evaluation_at, self_rag_config=app.state.self_rag_config)
                if outcome["failure"] is not None:
                    failure = outcome["failure"]
                    unavailable = failure.code in {"MODEL_UNAVAILABLE", "MODEL_AUTHENTICATION_FAILED", "MODEL_RATE_LIMITED"}
                    raise HTTPException(status_code=503 if failure.retryable or unavailable else 502,
                        detail={"code": failure.code, "message": failure.safe_message,
                                "retryable": failure.retryable,
                                "details": {"run_id": run.run_id, "status": run.status}})
            else:
                run = db.get(Run, run.run_id)
        if run.status == "RUNNING":
            run = recover_expired_run(db, run.run_id)
        if run.status == "SUCCEEDED" and run.result_json:
            result_payload = json.loads(run.result_json)
            return response_body(request, run_id=run.run_id, session_id=run.session_id,
                                 answer=result_payload["answer"], citations=result_payload["citations"],
                                 answer_kind=result_payload.get("answer_kind"),
                                 strategy=result_payload["strategy"], sub_strategy=result_payload["sub_strategy"],
                                 route=result_payload.get("route"),
                                 business_results=result_payload.get("business_results"),
                                 tool_references=result_payload.get("tool_references"),
                                  business_evidence=result_payload.get("business_evidence", []),
                                  tool_call_audit=result_payload.get("tool_call_audit", []),
                                 knowledge_version=result_payload.get("knowledge_version"),
                                 retrieval=result_payload.get("retrieval"),
                                 policy_retrieval=result_payload.get("policy_retrieval"),
                                 policy_retrieval_audit=result_payload.get("policy_retrieval_audit", []),
                                 status=run.status)
        if run.status == "RUNNING":
            return JSONResponse(status_code=202, content=response_body(request, run_id=run.run_id,
                session_id=run.session_id, status=run.status, answer=None, citations=[]))
        if run.status in {"FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"}:
            raise HTTPException(status_code=503, detail={"code": run.error_code or "RUN_FAILED",
                "message": "This request has a recorded failure and was not automatically retried",
                "retryable": False, "details": {"run_id": run.run_id, "status": run.status}})
        raise HTTPException(status_code=409, detail={"code": "RUN_NOT_EXECUTED", "message": "Run was not executed"})

    @app.post("/api/chat/stream")
    def post_chat_stream(payload: ChatInput, request: Request, after_seq: int | None = Query(default=None, ge=0),
                         last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                         db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        require_csrf(request)
        run = prepare_chat_run(db, user_id, payload)
        cursor = cursor_seq(db, user_id, run.run_id, after_seq, last_event_id)
        if run.status == "CREATED":
            if claim_run(db, run, execution_owner_id=app.state.execution_owner_id):
                run = db.get(Run, run.run_id)
                start_run_events(run, request.state.trace_id)
                try:
                    # Preserve request-scoped accounting/tracing when crossing
                    # the explicit worker boundary, including SSE execution.
                    app.state.run_executor.submit(copy_context().run, run_chat_worker, run.run_id, user_id,
                                                  payload.message, request.state.trace_id)
                except RuntimeError:
                    finish_run(db, run, status="FAILED", error_code="EXECUTOR_UNAVAILABLE",
                               trace_id=request.state.trace_id)
            else:
                run = db.get(Run, run.run_id)
        elif run.status == "RUNNING":
            run = recover_expired_run(db, run.run_id)

        run_id = run.run_id
        owner_id = user_id
        def stream_events():
            current_seq = cursor
            deadline = time.monotonic() + 300
            last_heartbeat = time.monotonic()
            while time.monotonic() < deadline:
                with db_session(factory) as poll_db:
                    current_run = poll_db.query(Run).filter_by(run_id=run_id, user_id=owner_id).one_or_none()
                    if current_run is None:
                        return
                    if current_run.status == "RUNNING":
                        current_run = recover_expired_run(poll_db, run_id)
                    events = read_run_events(poll_db, owner_id, run_id, after_seq=current_seq, limit=100)
                    status = current_run.status
                for event in events or []:
                    data = public_event(event)
                    yield f"id: {event.event_id}\nevent: {event.event_type}\ndata: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}\n\n"
                    current_seq = event.seq
                if status in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"} and not events:
                    return
                now = time.monotonic()
                if not events and now - last_heartbeat >= 15:
                    yield ": keep-alive\n\n"
                    last_heartbeat = now
                time.sleep(0.1)

        return StreamingResponse(stream_events(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
                     "X-Run-Id": run_id, "X-Run-Status": run.status})

    @app.get("/api/runs/{run_id}/events")
    def get_run_events(run_id: str, request: Request, after_seq: int | None = Query(default=None, ge=0),
                       last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
                       db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        owned_run = read_run(db, user_id, run_id)
        if owned_run is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        cursor = cursor_seq(db, user_id, run_id, after_seq, last_event_id)
        events = read_run_events(db, user_id, run_id, after_seq=cursor, limit=100)
        if events is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        result = read_run(db, user_id, run_id)
        run, _ = result
        run = recover_expired_run(db, run_id) if run.status == "RUNNING" else run
        events = read_run_events(db, user_id, run_id, after_seq=cursor, limit=100) or []
        _, max_seq = read_run(db, user_id, run_id)
        return response_body(request, run_id=run_id, status=run.status,
            events=[public_event(event) for event in events], next_cursor=events[-1].event_id if events else None,
            events_cursor=max_seq, has_more=len(events) == 100)

    @app.get("/api/orders")
    def list_owned_orders(request: Request, before: str = Query(default="", max_length=64),
            user_id: str = Depends(current_user)):
        from app.m3.order_tools import SqlAlchemyOrderReadRepository, ToolExecutionError
        try:
            page = SqlAlchemyOrderReadRepository(app.state.session_factory).list_orders(before, user_id)
        except ToolExecutionError as exc:
            raise HTTPException(status_code=400, detail={"code": exc.code, "message": "订单分页参数无效"}) from exc
        return response_body(request, **page)

    @app.get("/api/orders/{order_id}")
    def get_order(order_id: str, request: Request, db: Session = Depends(session_dependency), user_id: str = Depends(current_user)):
        order = order_for_owner(db, user_id, order_id)
        if order is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Order not found"})
        return response_body(request, order={"order_id": order.order_id, "status": order.status,
            "paid_at": order.paid_at, "delivered_at": order.delivered_at,
            "paid_cents": order.paid_cents, "version": order.version})

    @app.get("/api/admin/observability/metrics")
    def get_observability_metrics(request: Request,
            start: datetime = Query(alias="from"), end: datetime = Query(alias="to"),
            user_id: str = Depends(current_user)):
        require_observability_admin(user_id)
        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": "UTC offsets are required"})
        start = start.astimezone(timezone.utc)
        end = end.astimezone(timezone.utc)
        if end <= start or (end - start).total_seconds() > 31 * 24 * 60 * 60:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": "Metrics range must be ordered and at most 31 days"})
        from app.orchestration.persistence import aggregate_metrics
        from app.orchestration.admin_observability import aggregate_admin_overview
        try:
            metrics = aggregate_metrics(factory, start, end)
            overview = aggregate_admin_overview(factory, start, end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": str(exc)}) from exc
        return response_body(request, metrics=metrics, overview=overview)

    @app.get("/api/admin/observability/overview")
    def get_observability_overview(request: Request, start: datetime = Query(alias="from"),
            end: datetime = Query(alias="to"), user_id: str = Depends(current_user)):
        require_observability_admin(user_id)
        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": "UTC offsets are required"})
        from app.orchestration.admin_observability import aggregate_admin_overview
        try:
            overview = aggregate_admin_overview(factory, start, end)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": str(exc)}) from exc
        return response_body(request, overview=overview)

    @app.get("/api/admin/observability/runs")
    def get_observability_runs(request: Request, start: datetime = Query(alias="from"),
            end: datetime = Query(alias="to"), limit: int = Query(default=50, ge=1, le=100),
            offset: int = Query(default=0, ge=0, le=10000), status: str | None = Query(default=None, max_length=32),
            strategy: str | None = Query(default=None, pattern="^(DIRECT|SINGLE|MULTI|AGENT|REFUSE)$"),
            agent: str | None = Query(default=None, pattern="^(policy|order_logistics|after_sales|product)$"),
            tool_id: str | None = Query(default=None, min_length=1, max_length=96),
            orchestrator: str | None = Query(default=None, pattern="^(legacy|langgraph|unknown)$"),
            user_id: str = Depends(current_user)):
        require_observability_admin(user_id)
        if start.tzinfo is None or end.tzinfo is None:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": "UTC offsets are required"})
        from app.orchestration.admin_observability import list_admin_runs
        try:
            result = list_admin_runs(factory, start, end, limit=limit, offset=offset, status=status,
                                     strategy=strategy, agent=agent, tool_id=tool_id, orchestrator=orchestrator)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": str(exc)}) from exc
        return response_body(request, runs=result)

    @app.get("/api/admin/observability/runs/{run_id}")
    def get_observability_run_detail(request: Request, run_id: str,
            event_limit: int = Query(default=100, ge=1, le=200), span_limit: int = Query(default=200, ge=1, le=300),
            user_id: str = Depends(current_user)):
        require_observability_admin(user_id)
        from app.orchestration.admin_observability import admin_run_detail
        try:
            detail = admin_run_detail(factory, run_id, event_limit=event_limit, span_limit=span_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail={"code": "INVALID_ARGUMENT", "message": str(exc)}) from exc
        if detail is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "Run not found"})
        return response_body(request, detail=detail)

    return app


app = create_app()
