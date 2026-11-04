"""Lazy real application backend. Importing this file performs no execution."""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict, replace
import json
import time
from types import SimpleNamespace
import uuid

from sqlalchemy import select, text
from .budget import BudgetStop
from .personal_dev import D1PreflightError, SUPPORTED_TOOLS


def validate_case_route_scope(plan: dict, route: dict) -> dict:
    """Keep runtime routing genuine while denying capabilities outside this case's read allowlist."""
    caps = route.get("required_capabilities", [])
    declared = plan["case"]["input"].get("expected_tool_calls", [])
    case_allowlist = {row.get("tool_id") for row in declared if row.get("mode") == "read_only"}
    if (not isinstance(caps, list) or not set(caps).issubset(SUPPORTED_TOOLS) or
            not set(caps).issubset(case_allowlist) or len(caps) > len(case_allowlist)):
        from app.m1.routing import RouteCapabilityScopeError
        raise RouteCapabilityScopeError("actual route exceeds case read-only scope", diagnostic={
            "schema_version":"route-capability-scope-diagnostic-v1", "stage":"RUNTIME_ROUTE_SCOPE",
            "error_code":"ROUTE_CAPABILITY_SCOPE_REJECTED", "decision_source":route.get("decision_source"),
            "allowed_capabilities":sorted(case_allowlist), "requested_capabilities":sorted(caps),
            "disallowed_capabilities":sorted(set(caps)-case_allowlist),
            "candidate_strategy":route["strategy"], "candidate_sub_strategy":route.get("sub_strategy"),
            "provider_response_received":False, "response_summary":None, "usage":None,
            "usage_status":"NOT_APPLICABLE_RULE_ROUTE"})
    return route


class BudgetClient:
    """Reserve each bounded SDK call at the official peak output rate before sending."""
    def __init__(self, client, ledger, trial_id, deadline, conservative_usd_per_million=1.20, request_timeout_cap=None):
        self.client, self.ledger, self.trial_id = client, ledger, trial_id
        self.deadline, self.conservative_usd_per_million = deadline, conservative_usd_per_million
        self.request_timeout_cap = request_timeout_cap
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def with_options(self, **options):
        options["max_retries"] = 0
        return BudgetClient(self.client.with_options(**options), self.ledger,
            self.trial_id, self.deadline, self.conservative_usd_per_million,
            min(options.get("timeout", self.request_timeout_cap or float("inf")), self.request_timeout_cap or float("inf")))

    def create(self, **kwargs):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BudgetStop("D1 duration budget exhausted")
        max_tokens = kwargs.get("max_tokens")
        if not isinstance(max_tokens, int) or max_tokens < 1:
            raise BudgetStop("Explicit bounded output tokens required")
        # Conservative prompt allowance; actual unknown usage remains visible.
        tokens = len(json.dumps(kwargs.get("messages", []), ensure_ascii=False).encode()) + max_tokens + 4096
        estimated_usd = tokens * self.conservative_usd_per_million / 1_000_000
        held = self.ledger.reserve(trial_id=self.trial_id, category="provider",
            tokens_reserved=tokens, estimated_usd_reserved=estimated_usd)
        try:
            # SDK create(timeout=...) overrides client.with_options(timeout=...).
            # Bound the actual per-request argument as well as client defaults.
            kwargs["timeout"] = min(remaining, self.request_timeout_cap or remaining, kwargs.get("timeout", remaining))
            result = self.client.with_options(timeout=kwargs["timeout"], max_retries=0).chat.completions.create(**kwargs)
        except Exception:
            self.ledger.settle(held, status="COMMIT", actual_calls=1, actual_tokens=None,
                estimated_usd=None, call_count_status="CONSERVATIVE_UPPER_BOUND")
            raise
        usage = getattr(result, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None)
        if isinstance(total_tokens, bool) or not isinstance(total_tokens, int) or total_tokens < 0:
            total_tokens = None
        estimated_usd = (total_tokens * self.conservative_usd_per_million / 1_000_000
            if total_tokens is not None else None)
        self.ledger.settle(held, status="COMMIT", actual_calls=1,
            actual_tokens=total_tokens, estimated_usd=estimated_usd,
            call_count_status="RECORDED")
        return result


class PerCaseBudgetLedger:
    """Enforce per-case caps before every durable reservation in a batch ledger."""
    def __init__(self, ledger, case_id: str, limits):
        self.ledger, self.case_id, self.limits = ledger, case_id, limits

    def reserve(self, *, trial_id: str, category: str, calls: int = 1, tokens_reserved: int = 0,
                estimated_usd_reserved: float = 0.0, retry_index: int | None = None) -> str:
        if trial_id != self.case_id:
            raise BudgetStop("per-case ledger trial identity mismatch")
        usage = self.ledger.trial_snapshot(self.case_id)
        used_calls = usage["conservative_calls_by_category"].get(category, 0)
        cap = {"provider": self.limits.provider_calls, "tool": self.limits.tool_calls,
               "embedding": self.limits.embedding_calls, "judge": self.limits.judge_calls}.get(category)
        if cap is None or used_calls + calls > cap:
            raise BudgetStop(f"per-case {category} request cap exhausted")
        # Unknown provider usage stays explicitly UNKNOWN, while the durable
        # reserved token/cost ceilings are charged in usage["tokens"] and
        # usage["estimated_usd"]. Continue only if this next conservative
        # reservation still fits every bound.
        if usage["tokens"] + tokens_reserved > self.limits.total_tokens:
            raise BudgetStop("per-case token budget exhausted")
        if usage["estimated_usd"] + estimated_usd_reserved > self.limits.estimated_usd:
            raise BudgetStop("per-case local USD estimate budget exhausted")
        return self.ledger.reserve(trial_id=trial_id, category=category, calls=calls,
            tokens_reserved=tokens_reserved, estimated_usd_reserved=estimated_usd_reserved,
            retry_index=retry_index)

    def settle(self, reservation_id: str, **kwargs):
        return self.ledger.settle(reservation_id, **kwargs)

    def snapshot(self):
        return self.ledger.snapshot()


def execute_application_case(coordinator, plan, receipt, ledger, settings, *, trial_id=None, remaining_seconds=None, repositories=None, conservative_usd_per_million=1.20, checkpointer_factory=None, agent_architecture=None):
    # All runtime/SDK imports are behind coordinator Registry+permit preflight.
    from langgraph.checkpoint.memory import MemorySaver
    from app.m1.chat import DeepSeekProvider
    from app.m0.config import create_deepseek_client
    from app.m1.services import create_conversation, create_run, claim_run
    from app.m1.models import Run, RunEvent, Trace, ObservabilityRecord, GraphExecutionRecord, Message
    from app.orchestration.runtime import execute_claimed_chat_langgraph
    from app.orchestration.adapters import make_legacy_services
    from app.orchestration.checkpointer import strict_serializer

    if not settings.api_key.strip() or ledger.limits.estimated_usd <= 0 or ledger.limits.provider_calls < 1:
        raise D1PreflightError("CONFIGURED_PROVIDER_AND_POSITIVE_APPROVED_BUDGET_REQUIRED")
    selected = replace(settings, database_url=coordinator.target.database_url,
        orchestrator="langgraph", m3_supervisor_enabled=True)
    client = BudgetClient(create_deepseek_client(selected, selected.nodes["route"]), ledger,
        trial_id or plan["case_id"], time.monotonic() + min(ledger.limits.max_duration_seconds, remaining_seconds if remaining_seconds is not None else ledger.limits.max_duration_seconds),
        conservative_usd_per_million)  # approved peak rate, not supplier billing
    provider = DeepSeekProvider(selected, client=client)

    reads = repositories if repositories is not None else coordinator.repositories(plan, receipt, maximum=ledger.limits.tool_calls)
    retrieval = reads["retrieval"]
    case_capabilities = sorted({row.get("tool_id") for row in
        plan["case"]["input"].get("expected_tool_calls", []) if row.get("mode") == "read_only"})
    services = make_legacy_services(provider=provider, classifier=provider, retrieval_service=retrieval,
        available_capabilities=case_capabilities)
    real_router = services.router

    def guarded_router(query, **kwargs):
        route = real_router(query, **kwargs)
        return validate_case_route_scope(plan, route)  # Do not force route or inject expected results.

    services.router = guarded_router
    if agent_architecture is not None:
        from app.m3.single_agent import bind_agent_controls
        bind_agent_controls(services,provider,reads,agent_architecture,case_capabilities)
    saver = MemorySaver(serde=strict_serializer())

    @asynccontextmanager
    async def memory_checkpoint_factory(_url, _schema):
        yield saver

    return execute_claimed_lifecycle(coordinator, plan, receipt, application_call=execute_claimed_chat_langgraph,
        provider=provider, selected=selected, services=services, reads=reads,
        checkpoint_factory=checkpointer_factory or memory_checkpoint_factory)


def execute_claimed_lifecycle(coordinator, plan, receipt, *, application_call, provider,
                              selected, services, reads, checkpoint_factory):
    """Actual Run/receipt/observation lifecycle; injectable call for OFFLINE tests.

    The real backend passes execute_claimed_chat_langgraph. Tests pass a plain
    function; no graph/Agent/Provider is started by this helper itself.
    """
    from app.m1.services import create_conversation, create_run, claim_run
    from app.m1.models import Run, RunEvent, Trace, ObservabilityRecord, GraphExecutionRecord, Message
    retrieval = reads["retrieval"]
    with coordinator.factory() as db:
        coordinator._check_receipt(db, receipt)
        session = create_conversation(db, plan["owner_id"], f"m6.1-eval:{receipt.lease_id}")
        request_payload={"message":plan["message"]}
        scope={}
        if coordinator.target.profile=="M6.2-FORMAL-POSTGRES":
            request_payload["requested_session_id"]=session.session_id
            scope={"idempotency_scope":"chat:"+session.session_id}
        run, matches = create_run(db, plan["owner_id"], session.session_id,
            f"m61c:{receipt.lease_id}:{plan['case_id']}", request_payload,**scope)
        if not matches or not claim_run(db, run, execution_owner_id=receipt.lease_id):
            raise D1PreflightError("RUN_CLAIM_FAILED_NO_REPLAY")
        receipt.session_id, receipt.run_id = session.session_id, run.run_id
        db.execute(text("UPDATE d1_personal_target SET receipt=:receipt WHERE lease_id=:lease"),
            {"receipt": json.dumps(asdict(receipt)), "lease": receipt.lease_id})
        db.commit()
        result = application_call(db, run=run, user_id=plan["owner_id"],
            message=plan["message"], trace_id=uuid.uuid4().hex, provider=provider, index_path=None,
            retrieval_service=retrieval, evaluation_at=plan["evaluation_at"], self_rag_config=None, settings=selected,
            session_factory=coordinator.factory, database_url=selected.database_url,
            checkpointer_factory=checkpoint_factory, graph_services=services, read_repositories=reads)
        db.expire_all()
        observed = db.get(Run, receipt.run_id)
        snapshots = {}
        for model in (Run, Message, RunEvent, Trace, ObservabilityRecord, GraphExecutionRecord):
            if model is Message:
                rows = db.execute(select(model).where(model.session_id == receipt.session_id)).scalars().all()
            else:
                rows = db.execute(select(model).where(model.run_id == receipt.run_id)).scalars().all()
            snapshots[model.__tablename__] = [{c.name: getattr(row, c.name) for c in model.__table__.columns} for row in rows]
        return {"status": observed.status, "run_id": observed.run_id,
            "result": result.get("result"), "failure_code": getattr(result.get("failure"), "code", None),
            "observations": snapshots, "checkpoint_mode": "POSTGRES_DURABLE_NO_AUTOMATIC_REPLAY" if coordinator.target.profile == "M6.2-FORMAL-POSTGRES" else "MEMORY_NO_CRASH_RESUME",
            "agent_quality_verdict": "NOT_GRADED", "read_calls": reads["store"].allowance.calls}
