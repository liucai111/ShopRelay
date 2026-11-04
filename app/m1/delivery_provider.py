"""Local interactive deployment: real SDK, durable budget, no answer substitute.

Reuses the existing experiment budget store. Only safe request fingerprints and
usage are added here; visible answers stay in the normal owner-scoped Run store.
"""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
from contextvars import ContextVar
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from eval.m6_1.budget import BudgetLedger, BudgetLimits, BudgetStop
from eval.m6_1.contracts import EvalExperiment, sha256_json
from eval.m6_1.store import ResultStore


class AcceptanceAccount(BaseModel):
    """Trusted server-side receipt, never selected through a browser header."""
    model_config = ConfigDict(extra='forbid')
    batch_id: str = Field(pattern=r'^[a-z][a-z0-9-]{5,79}$')
    status: Literal['OPEN', 'CLOSED']
    approval_event: str
    session_ids: list[UUID] = Field(min_length=1, max_length=12)
    provider_calls: int = Field(gt=0)
    total_tokens: int = Field(gt=0)
    estimated_usd: float = Field(gt=0)


class ManualContinuation(BaseModel):
    """A trusted, append-only successor of the original manual protection cap."""
    model_config = ConfigDict(extra='forbid')
    scope: Literal['user-initiated-v2']
    approval_event: str
    predecessor_events_sha256: str = Field(pattern=r'^[0-9a-f]{64}$')
    cumulative_provider_calls: Literal[100]
    cumulative_total_tokens: Literal[2000000]
    cumulative_estimated_usd: Literal[5]


def continue_manual_account(client, approval_event: str):
    """Seal the consumed prefix; never rewrite immutable budget events or limits."""
    path = client.directory/'manual-continuation.json'
    if path.exists():
        raise BudgetStop('manual continuation already exists')
    scope, ledger = client.ledger()
    if scope != 'USER_INITIATED':
        raise BudgetStop('only the original manual account can be continued')
    snapshot = ledger.snapshot()
    if not snapshot['settled'] or snapshot['token_status'] == 'UNKNOWN_USAGE':
        raise BudgetStop('manual usage must be reconciled before continuation')
    record = ManualContinuation(scope='user-initiated-v2', approval_event=approval_event,
        predecessor_events_sha256=sha256_json(client.store.budget_snapshot(ledger.experiment_id)),
        cumulative_provider_calls=100, cumulative_total_tokens=2000000, cumulative_estimated_usd=5)
    with path.open('x', encoding='utf8') as stream:
        stream.write(record.model_dump_json())
        stream.flush()
        os.fsync(stream.fileno())
    return {'predecessor': snapshot, 'continuation': record.model_dump()}


acceptance_request: ContextVar[tuple[str, str, str] | None] = ContextVar('delivery_acceptance_request', default=None)


class DemoBudgetExhausted(BudgetStop):
    """Known local cap, distinct from network or unknown provider usage."""


def install_acceptance_accounting(app, directory: Path):
    """Bind only receipt-listed new synthetic sessions to their independent budget."""
    from fastapi.responses import JSONResponse

    @app.middleware('http')
    async def scoped_acceptance(request, call_next):
        path = directory/'acceptance-account.json'
        token = None
        if request.method == 'POST' and request.url.path in {'/api/chat', '/api/chat/stream'} and path.exists():
            account = AcceptanceAccount.model_validate_json(path.read_text())
            payload = await request.json()
            sid = payload.get('session_id') if isinstance(payload, dict) else None
            if sid in {str(value) for value in account.session_ids}:
                if account.status != 'OPEN':
                    return JSONResponse(status_code=409, content={'code':'ACCEPTANCE_CLOSED', 'message':'此合成验收已关闭，不会重放。'})
                token = acceptance_request.set((account.batch_id, sid, str(payload.get('client_request_id') or '')))
        try:
            return await call_next(request)
        finally:
            if token is not None:
                acceptance_request.reset(token)


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


class MeteredChatClient:
    """Serial, no-retry wrapper around the actual SDK create boundary."""
    def __init__(self, client, directory: Path, binding: dict):
        self.client, self.directory, self.binding = client, directory, binding
        directory.mkdir(parents=True, exist_ok=True)
        self.store = ResultStore(directory/'requests.sqlite')
        self.lock = threading.Lock()
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def manual_budget_status(self):
        """Public aggregate protection status, without request or session details."""
        scope, ledger = self.ledger()
        if scope not in {'USER_INITIATED', 'user-initiated-v2'}:
            return None
        snapshot = ledger.snapshot()
        if not snapshot['settled'] or snapshot['token_status'] == 'UNKNOWN_USAGE':
            return {'status': 'RECONCILIATION_REQUIRED'}
        return {'status': 'AVAILABLE' if snapshot['provider_requests'] < ledger.limits.provider_calls else 'EXHAUSTED',
            'remaining_provider_requests': ledger.limits.provider_calls-snapshot['provider_requests'],
            'remaining_estimated_usd': ledger.limits.estimated_usd-snapshot['estimated_usd'],
            'local_protection_not_supplier_invoice': True}

    def ledger(self):
        # Trusted server-side switch. Browser headers cannot select accounting.
        acceptance = acceptance_request.get()
        if acceptance:
            account = AcceptanceAccount.model_validate_json((self.directory/'acceptance-account.json').read_text())
            if account.status != 'OPEN' or account.batch_id != acceptance[0] or acceptance[1] not in {str(s) for s in account.session_ids}:
                raise BudgetStop('acceptance account is closed or mismatched')
            scope = account.batch_id
            limits = BudgetLimits(provider_calls=account.provider_calls, total_tokens=account.total_tokens,
                estimated_usd=account.estimated_usd, max_concurrent_trials=1, max_duration_seconds=120, max_retries=0)
        else:
            scope = (self.directory/'scope').read_text().strip()
            if scope not in {'AUTOMATED_SYNTHETIC', 'USER_INITIATED'}:
                raise BudgetStop('interactive accounting scope is not enabled')
            limits = BudgetLimits(provider_calls=10, total_tokens=200000, estimated_usd=1,
                max_concurrent_trials=1, max_duration_seconds=120, max_retries=0)
            continuation = self.directory/'manual-continuation.json'
            if scope == 'USER_INITIATED' and continuation.exists():
                record = ManualContinuation.model_validate_json(continuation.read_text())
                previous_id = 'delivery-user_initiated'
                events = self.store.budget_snapshot(previous_id)
                if sha256_json(events) != record.predecessor_events_sha256:
                    raise BudgetStop('sealed manual predecessor changed')
                previous_limits = self.store.budget_limits(previous_id)
                if previous_limits is None:
                    raise BudgetStop('manual predecessor budget missing')
                previous = BudgetLedger.snapshot_from_events(events, BudgetLimits(**previous_limits), previous_limits)
                if not previous['settled'] or previous['token_status'] == 'UNKNOWN_USAGE':
                    raise BudgetStop('manual predecessor usage unknown')
                scope = record.scope
                limits = BudgetLimits(provider_calls=record.cumulative_provider_calls-previous['provider_requests'],
                    total_tokens=record.cumulative_total_tokens-previous['tokens'],
                    estimated_usd=record.cumulative_estimated_usd-previous['estimated_usd'],
                    max_concurrent_trials=1, max_duration_seconds=120, max_retries=0)
        config = {**self.binding, 'scope': scope, 'limits': limits.to_dict()}
        experiment = EvalExperiment(experiment_id='delivery-'+scope.lower(), config_snapshot=config,
            config_sha256=sha256_json(config), git_commit='working-tree-source-manifest',
            knowledge_index_version=self.binding['knowledge_version'],
            model_versions={'provider': self.binding['model']}, prompt_versions={'policy': 'customer-care-v1'},
            strategy='REAL_APPLICATION', tool_permissions=('READ_ONLY',), budget=limits.to_dict(),
            data_version='public-synthetic-policies', scoring_version='engineering-not-task-grading')
        self.store.lock_experiment(experiment, utc_now())
        return scope, BudgetLedger(self.store, experiment.experiment_id, limits)

    def create(self, **kwargs):
        with self.lock:
            scope, ledger = self.ledger()
            prior = ledger.snapshot()
            if prior['active_reservations'] or prior['token_status'] == 'UNKNOWN_USAGE':
                raise BudgetStop('previous request usage requires reconciliation')
            if kwargs['model'] != self.binding['model']:
                raise BudgetStop('provider model differs from deployment binding')
            raw = json.dumps(kwargs, ensure_ascii=False, sort_keys=True, default=str).encode()
            # UTF-8 bytes bound input tokens conservatively; output is SDK-bounded.
            input_bound = len(raw) + 1024
            output_bound = kwargs['max_tokens']
            reserve_cost = (input_bound*.30 + output_bound*1.20)/1000000
            if (prior['provider_requests'] + 1 > ledger.limits.provider_calls or
                    prior['tokens'] + input_bound + output_bound > ledger.limits.total_tokens or
                    prior['estimated_usd'] + reserve_cost > ledger.limits.estimated_usd):
                raise DemoBudgetExhausted('local demonstration budget exhausted before sending')
            reservation = ledger.reserve(trial_id='visible-chat', category='provider',
                tokens_reserved=input_bound+output_bound, estimated_usd_reserved=reserve_cost)
            event = {'scope': scope, 'reservation': reservation, 'at': utc_now(),
                'model': kwargs['model'], 'endpoint_host': self.binding['endpoint_host'],
                'request_sha256': hashlib.sha256(raw).hexdigest(), 'max_tokens': output_bound,
                'thinking': kwargs.get('extra_body', {}).get('thinking'), 'automatic_retries': 0,
                'price_source': self.binding['price_source'], 'invoice_status': 'UNKNOWN'}
            accepted = acceptance_request.get()
            if accepted:
                event.update(session_id=accepted[1], client_request_id=accepted[2])
            try:
                response = self.client.chat.completions.create(**kwargs)
            except Exception as exc:
                ledger.settle(reservation, status='COMMIT', actual_calls=1,
                    actual_tokens=None, estimated_usd=None, call_count_status='CONSERVATIVE_UPPER_BOUND')
                self.record({**event, 'status': 'REQUEST_FAILED', 'exception_type': type(exc).__name__,
                    'usage_status': 'UNKNOWN'})
                raise
            usage = response.usage
            if usage is None:
                ledger.settle(reservation, status='COMMIT', actual_calls=1, actual_tokens=None, estimated_usd=None)
                self.record({**event, 'status': 'RESPONSE_RECEIVED', 'usage_status': 'UNKNOWN'})
                raise BudgetStop('provider response usage missing')
            prompt, completion = usage.prompt_tokens, usage.completion_tokens
            if type(prompt) is not int or type(completion) is not int or min(prompt, completion) < 0:
                ledger.settle(reservation, status='COMMIT', actual_calls=1, actual_tokens=None, estimated_usd=None)
                self.record({**event, 'status': 'RESPONSE_RECEIVED', 'usage_status': 'UNKNOWN'})
                raise BudgetStop('provider response usage invalid')
            cost = (prompt*.30 + completion*1.20)/1000000
            ledger.settle(reservation, status='COMMIT', actual_calls=1,
                actual_tokens=prompt+completion, estimated_usd=cost)
            choice = response.choices[0] if response.choices else None
            self.record({**event, 'status': 'RESPONSE_RECEIVED', 'usage_status': 'RECORDED',
                'response_id': response.id, 'response_model': response.model,
                'prompt_tokens': prompt, 'completion_tokens': completion,
                'local_peak_estimate_usd': cost, 'finish_reason': getattr(choice, 'finish_reason', None)})
            return response

    def record(self, event):
        with (self.directory/'provider-events.jsonl').open('a', encoding='utf8') as stream:
            stream.write(json.dumps(event, ensure_ascii=False)+'\n')
            stream.flush()
            os.fsync(stream.fileno())
