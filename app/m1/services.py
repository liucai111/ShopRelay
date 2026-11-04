from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.m1.models import (AuthSession, ConversationSession, DemoCredential, GraphExecutionRecord, Message,
                           Order, Run, RunEvent, Trace, User, safe_trace_json, utc_now)

TERMINAL_RUN_STATES = {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"}
RUN_TRANSITIONS = {
    "CREATED": {"RUNNING", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"},
    "RUNNING": {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"},
    "SUCCEEDED": set(),
    "FAILED": set(),
    "MODEL_UNAVAILABLE": set(),
    "UNKNOWN_RESULT": set(),
}


def new_id() -> str:
    return str(uuid.uuid4())


def password_hash(password: str) -> str:
    if len(password) < 12:
        raise ValueError("demo password must be at least 12 characters")
    salt = secrets.token_bytes(16)
    result = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt$16384$8$1${salt.hex()}${result.hex()}"


def verify_password(password: str, encoded: str | None) -> bool:
    # Always perform one scrypt operation for unknown users to reduce account enumeration timing.
    if not encoded:
        encoded = "scrypt$16384$8$1$" + ("00" * 16) + "$" + ("00" * 64)
    try:
        algorithm, n, r, p, salt_hex, expected_hex = encoded.split("$", 5)
        if algorithm != "scrypt":
            return False
        actual = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt_hex),
                                n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(expected_hex)))
        return secrets.compare_digest(actual.hex(), expected_hex)
    except (ValueError, MemoryError):
        return False


def set_demo_password(db: Session, user_id: str, password: str) -> None:
    if db.get(User, user_id) is None:
        raise ValueError("demo user does not exist; run the explicit M0 seed first")
    credential = db.get(DemoCredential, user_id)
    if credential is None:
        credential = DemoCredential(user_id=user_id, password_hash=password_hash(password), active=True, updated_at=utc_now())
        db.add(credential)
    else:
        credential.password_hash = password_hash(password)
        credential.active = True
        credential.updated_at = utc_now()
    db.commit()


def list_demo_users(db: Session) -> list[User]:
    return db.execute(select(User).order_by(User.user_id)).scalars().all()


def verify_demo_login(db: Session, user_id: str, password: str) -> User | None:
    user = db.get(User, user_id)
    credential = db.get(DemoCredential, user_id)
    valid = verify_password(password, credential.password_hash if credential and credential.active else None)
    return user if user is not None and credential is not None and credential.active and valid else None


def create_auth_session(db: Session, user_id: str, hours: int = 12) -> str:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    db.add(AuthSession(token_hash=token_hash, user_id=user_id, created_at=utc_now(),
                       expires_at=utc_now() + timedelta(hours=hours)))
    db.commit()
    return token


def authenticated_user(db: Session, token: str | None) -> str | None:
    if not token:
        return None
    digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
    auth = db.get(AuthSession, digest)
    if auth is None or auth.revoked_at is not None or auth.expires_at <= utc_now():
        return None
    return auth.user_id


def revoke_auth_session(db: Session, token: str | None) -> None:
    if token:
        digest = hashlib.sha256(token.encode("ascii", errors="ignore")).hexdigest()
        auth = db.get(AuthSession, digest)
        if auth and auth.revoked_at is None:
            auth.revoked_at = utc_now()
            db.commit()


def create_conversation(db: Session, user_id: str, title: str | None) -> ConversationSession:
    now = utc_now()
    record = ConversationSession(session_id=new_id(), user_id=user_id, created_at=now,
                                 updated_at=now, title=title)
    db.add(record)
    db.commit()
    db.refresh(record)
    return record


def list_conversations(db: Session, owner_id: str, *, limit: int = 100) -> list[ConversationSession]:
    """Return only the authenticated owner's recent conversations."""
    bounded_limit = max(1, min(int(limit), 200))
    return db.execute(
        select(ConversationSession)
        .where(ConversationSession.user_id == owner_id)
        .order_by(ConversationSession.updated_at.desc(), ConversationSession.created_at.desc(),
                  ConversationSession.session_id)
        .limit(bounded_limit)
    ).scalars().all()


def add_user_message(db: Session, owner_id: str, session_id: str, content: str) -> Message | None:
    conversation = db.execute(select(ConversationSession).where(
        ConversationSession.session_id == session_id,
        ConversationSession.user_id == owner_id,
    )).scalar_one_or_none()
    if conversation is None:
        return None
    sequence = db.execute(select(func.max(Message.sequence)).where(Message.session_id == session_id)).scalar_one()
    now = utc_now()
    message = Message(message_id=new_id(), session_id=session_id, role="user", content=content,
                      created_at=now, sequence=(sequence or 0) + 1, status="RECORDED")
    if not conversation.title:
        conversation.title = content.strip().replace("\r", " ").replace("\n", " ")[:60] or None
    conversation.updated_at = now
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


def read_conversation(db: Session, owner_id: str, session_id: str):
    conversation = db.execute(select(ConversationSession).where(
        ConversationSession.session_id == session_id,
        ConversationSession.user_id == owner_id,
    )).scalar_one_or_none()
    if conversation is None:
        return None
    messages = db.execute(select(Message).where(Message.session_id == session_id).order_by(
        Message.sequence.is_(None), Message.sequence, Message.created_at, Message.message_id
    )).scalars().all()
    runs = db.execute(select(Run).where(Run.session_id == session_id, Run.user_id == owner_id)
                      .order_by(Run.created_at.desc()).limit(10)).scalars().all()
    return conversation, messages, runs


def read_run(db: Session, owner_id: str, run_id: str):
    run = db.execute(select(Run).where(Run.run_id == run_id, Run.user_id == owner_id)).scalar_one_or_none()
    if run is None:
        return None
    events_cursor = db.execute(select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.run_id == run_id)).scalar_one()
    return run, events_cursor


RUN_EVENT_TYPES = {"started", "route_decided", "agent_started", "retrieval_hop", "tool_started",
                   "tool_finished", "policy_retrieval_started", "policy_retrieval_finished",
                   "action_pending", "answer", "done", "error"}


def _run_event_entity(db: Session, run: Run, trace_id: str, event_type: str, payload: dict,
                     event_key: str | None = None) -> RunEvent:
    if event_type not in RUN_EVENT_TYPES:
        raise ValueError(f"unsupported run event type: {event_type}")
    if event_key is not None:
        prior = db.execute(select(RunEvent).where(RunEvent.run_id == run.run_id,
                                                  RunEvent.event_key == event_key)).scalar_one_or_none()
        if prior is not None:
            if prior.event_type != event_type or prior.payload_json != safe_trace_json(payload):
                raise ValueError("idempotent run event key reused with different content")
            return prior
    seq = db.execute(select(func.coalesce(func.max(RunEvent.seq), 0)).where(RunEvent.run_id == run.run_id)).scalar_one() + 1
    event = RunEvent(event_id=new_id(), run_id=run.run_id, session_id=run.session_id,
                     trace_id=trace_id, seq=seq, timestamp=utc_now(), event_type=event_type,
                     payload_json=safe_trace_json(payload), event_key=event_key)
    db.add(event)
    return event


def append_run_event(db: Session, run_id: str, trace_id: str, event_type: str, payload: dict,
                     *, event_key: str | None = None) -> RunEvent:
    run = db.execute(select(Run).where(Run.run_id == run_id).with_for_update()).scalar_one()
    event = _run_event_entity(db, run, trace_id, event_type, payload, event_key)
    db.commit()
    db.refresh(event)
    return event


def read_run_events(db: Session, owner_id: str, run_id: str, *, after_seq: int = 0, limit: int = 100):
    owned = db.execute(select(Run.run_id).where(Run.run_id == run_id, Run.user_id == owner_id)).scalar_one_or_none()
    if owned is None:
        return None
    return db.execute(select(RunEvent).where(RunEvent.run_id == run_id, RunEvent.seq > after_seq)
                      .order_by(RunEvent.seq).limit(limit)).scalars().all()


def event_cursor_seq(db: Session, owner_id: str, run_id: str, event_id: str) -> int | None:
    return db.execute(select(RunEvent.seq).join(Run, Run.run_id == RunEvent.run_id).where(
        Run.user_id == owner_id, RunEvent.run_id == run_id, RunEvent.event_id == event_id
    )).scalar_one_or_none()


def recover_expired_run(db: Session, run_id: str) -> Run | None:
    run = db.execute(select(Run).where(Run.run_id == run_id)).scalar_one_or_none()
    if run is None or run.status != "RUNNING":
        return run
    now = utc_now()
    if run.lease_expires_at is not None and run.lease_expires_at > now:
        return run
    # Lock only a candidate that appeared stale, then recheck after waiting for any worker.
    run = db.execute(select(Run).where(Run.run_id == run_id).with_for_update()).scalar_one_or_none()
    if run is None or run.status != "RUNNING":
        return run
    now = utc_now()
    if run.lease_expires_at is not None and run.lease_expires_at > now:
        return run
    run.status = "UNKNOWN_RESULT"
    run.error_code = "UNKNOWN_RESULT"
    run.error_detail_json = safe_trace_json({"reason": "execution lease expired; provider outcome is not known and will not be replayed"})
    run.finished_at = now
    run.updated_at = now
    run.execution_owner_id = None
    run.lease_expires_at = None
    graph_execution = db.get(GraphExecutionRecord, run.run_id)
    if graph_execution is not None and graph_execution.status == "RUNNING":
        graph_execution.status = "UNKNOWN"
        graph_execution.error_code = "UNKNOWN_RESULT"
        graph_execution.updated_at = now
        graph_execution.finished_at = now
    trace_id = new_id()
    db.add(Trace(trace_id=trace_id, user_id=run.user_id, run_id=run.run_id,
                 event_type="run_status_changed", status="UNKNOWN_RESULT",
                 event_json=safe_trace_json({"status": "UNKNOWN_RESULT", "error_code": "UNKNOWN_RESULT"}),
                 created_at=now))
    _run_event_entity(db, run, trace_id, "error", {"code": "UNKNOWN_RESULT",
        "message": "执行租约已过期，外部模型调用结果未知；系统不会自动重放。"})
    db.commit()
    db.refresh(run)
    return run


def create_run(db: Session, owner_id: str, session_id: str, client_request_id: str, request_payload: dict,
               *, idempotency_scope: str | None = None):
    conversation = db.execute(select(ConversationSession).where(
        ConversationSession.session_id == session_id,
        ConversationSession.user_id == owner_id,
    )).scalar_one_or_none()
    if conversation is None:
        return None, False
    scope = idempotency_scope or f"api_run:{session_id}"
    request_hash_value = request_hash(request_payload, scope_session_id=session_id)
    legacy = db.execute(select(Run).where(
        Run.user_id == owner_id, Run.session_id == session_id,
        Run.client_request_id == client_request_id, Run.idempotency_scope.like("legacy:%"),
    )).scalar_one_or_none()
    if legacy is not None:
        legacy_body = json.dumps(request_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        legacy_hash = hashlib.sha256(legacy_body.encode("utf-8")).hexdigest()
        return legacy, legacy.request_hash == legacy_hash
    existing = db.execute(select(Run).where(
        Run.user_id == owner_id,
        Run.idempotency_scope == scope,
        Run.client_request_id == client_request_id,
    )).scalar_one_or_none()
    if existing:
        if existing.request_hash != request_hash_value:
            return existing, False
        return existing, True
    now = utc_now()
    run = Run(run_id=new_id(), user_id=owner_id, session_id=session_id,
              client_request_id=client_request_id, idempotency_scope=scope, request_hash=request_hash_value,
              status="CREATED", created_at=now, updated_at=now)
    db.add(run)
    db.add(Trace(trace_id=new_id(), user_id=owner_id, run_id=run.run_id,
                 event_type="run_created", status="CREATED",
                 event_json=safe_trace_json({"status": "CREATED"}), created_at=now))
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        existing = db.execute(select(Run).where(
            Run.user_id == owner_id,
            Run.idempotency_scope == scope,
            Run.client_request_id == client_request_id,
        )).scalar_one_or_none()
        if existing is None or existing.request_hash != request_hash_value:
            return existing, False
        return existing, True
    db.refresh(run)
    return run, True


def find_request_run(db: Session, owner_id: str, client_request_id: str, idempotency_scope: str):
    return db.execute(select(Run).where(Run.user_id == owner_id,
                                        Run.idempotency_scope == idempotency_scope,
                                        Run.client_request_id == client_request_id)).scalar_one_or_none()


def request_hash(request_payload: dict, *, scope_session_id: str | None = None) -> str:
    material = dict(request_payload)
    if "requested_session_id" not in material and scope_session_id is not None:
        material["__session_id"] = scope_session_id
    body = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def claim_run(db: Session, run: Run, *, execution_owner_id: str | None = None, lease_seconds: int = 120) -> bool:
    """Atomically claim CREATED work; the database CAS prevents duplicate paid calls."""
    now = utc_now()
    changed = db.execute(update(Run).where(Run.run_id == run.run_id, Run.status == "CREATED")
                         .values(status="RUNNING", started_at=now, updated_at=now,
                                 execution_owner_id=execution_owner_id,
                                 lease_expires_at=now + timedelta(seconds=lease_seconds) if execution_owner_id else None))
    if changed.rowcount != 1:
        db.rollback()
        db.refresh(run)
        return False
    db.add(Trace(trace_id=new_id(), user_id=run.user_id, run_id=run.run_id,
                 event_type="run_status_changed", status="RUNNING",
                 event_json=safe_trace_json({"status": "RUNNING"}), created_at=now))
    db.commit()
    db.refresh(run)
    return True


def finish_run(db: Session, run: Run, *, status: str, error_code: str | None = None,
               error_detail: dict | None = None,
               result: dict | None = None, model_id: str | None = None,
               usage: dict | None = None, latency_ms: int | None = None,
               assistant_message: str | None = None, trace_id: str | None = None,
               graph_execution_status: str | None = None,
               graph_checkpoint_id: str | None = None) -> None:
    """Commit terminal state and answer atomically; failures never create assistant answers."""
    prior = db.execute(select(Run).where(Run.run_id == run.run_id).with_for_update()).scalar_one()
    if prior.status != "RUNNING" or status not in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE", "UNKNOWN_RESULT"}:
        raise ValueError(f"illegal run completion: {prior.status} -> {status}")
    now = utc_now()
    prior.status = status
    prior.error_code = error_code
    prior.error_detail_json = safe_trace_json(error_detail) if error_detail is not None else None
    prior.result_json = json.dumps(result, ensure_ascii=False, sort_keys=True) if result is not None else None
    prior.model_id = model_id
    prior.usage_json = json.dumps(usage, ensure_ascii=False, sort_keys=True) if usage is not None else None
    prior.latency_ms = latency_ms
    prior.finished_at = now
    prior.updated_at = now
    prior.execution_owner_id = None
    prior.lease_expires_at = None
    if graph_execution_status is not None:
        if graph_execution_status not in {"SUCCEEDED", "FAILED", "UNKNOWN"}:
            raise ValueError("invalid terminal graph execution status")
        graph_execution = db.get(GraphExecutionRecord, run.run_id)
        if graph_execution is None or graph_execution.status != "RUNNING":
            raise ValueError("graph execution is not in a finalizable state")
        graph_execution.status = graph_execution_status
        graph_execution.error_code = error_code
        if graph_checkpoint_id is not None:
            graph_execution.checkpoint_id = graph_checkpoint_id
        graph_execution.updated_at = now
        graph_execution.finished_at = now
    if assistant_message is not None:
        sequence = db.execute(select(func.max(Message.sequence)).where(Message.session_id == run.session_id)).scalar_one()
        db.add(Message(message_id=new_id(), session_id=run.session_id, role="assistant",
                       content=assistant_message, created_at=now, sequence=(sequence or 0) + 1,
                       status="RECORDED"))
    event = {"status": status, "error_code": error_code, "error_detail": error_detail, "model_id": model_id,
             "latency_ms": latency_ms, "trace_id": trace_id}
    db.add(Trace(trace_id=new_id(), user_id=run.user_id, run_id=run.run_id,
                 event_type="run_status_changed", status=status,
                 event_json=safe_trace_json(event), created_at=now))
    if status == "SUCCEEDED":
        _run_event_entity(db, prior, trace_id or new_id(), "answer", result or {})
        _run_event_entity(db, prior, trace_id or new_id(), "done", {"status": "SUCCEEDED"})
    else:
        _run_event_entity(db, prior, trace_id or new_id(), "error",
                          {"code": error_code or status, "message": "请求未能完成，结果已安全记录。"})
    db.commit()
    db.refresh(run)


def transition_run(db: Session, run: Run, new_status: str, error_code: str | None = None) -> None:
    prior_status = run.status
    if new_status not in RUN_TRANSITIONS.get(prior_status, set()):
        raise ValueError(f"illegal run transition: {prior_status} -> {new_status}")
    if new_status == "MODEL_UNAVAILABLE" and error_code != "MODEL_UNAVAILABLE":
        raise ValueError("MODEL_UNAVAILABLE terminal state requires matching error_code")
    if new_status != "MODEL_UNAVAILABLE" and error_code == "MODEL_UNAVAILABLE":
        raise ValueError("MODEL_UNAVAILABLE error cannot be attached to another run state")
    now = utc_now()
    changed = db.execute(update(Run).where(Run.run_id == run.run_id, Run.status == prior_status)
                         .values(status=new_status, error_code=error_code, updated_at=now))
    if changed.rowcount != 1:
        db.rollback()
        raise ValueError("run state changed concurrently; reload before transition")
    db.add(Trace(trace_id=new_id(), user_id=run.user_id, run_id=run.run_id,
                 event_type="run_status_changed", status=new_status,
                 event_json=safe_trace_json({"status": new_status, "error_code": error_code}),
                 created_at=now))
    db.commit()
    db.refresh(run)


def order_for_owner(db: Session, owner_id: str, order_id: str):
    return db.execute(select(Order).where(Order.order_id == order_id, Order.user_id == owner_id)).scalar_one_or_none()


def serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
