"""Deterministic, synthetic-only evaluation harness runner."""
from __future__ import annotations

from datetime import datetime, timezone
import time
import uuid
from typing import Any, Mapping

from .aggregate import aggregate
from .budget import BudgetLedger, BudgetStop
from .contracts import EvalOutcome, EvalTrial, LifecycleStatus, Verdict
from .grader import CodeGrader
from .judge import JudgeRequest, JudgeRubric, SyntheticJudgeStub
from .reviews import ReviewService
from .store import ResultStore
from dataclasses import dataclass
from .grader import GradeResult


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class SyntheticExecutor:
    """Deterministic fixture executor. It has no application, network, DB or model client."""
    _RESPONSES = {
        "syn-route-direct-01": {"route":"DIRECT","tools":[],"business":"ANSWERED_GENERAL","task":True,"evidence":[],"runs":1,"owner":"PASS"},
        "syn-route-single-02": {"route":"SINGLE","tools":[],"business":"EVIDENCE_RETURNED","task":True,"evidence":["syn-chunk-a"],"runs":1,"owner":"PASS"},
        "syn-route-multi-03": {"route":"MULTI","tools":[],"business":"TWO_HOPS","task":True,"evidence":["syn-chunk-b","syn-chunk-c"],"runs":1,"owner":"PASS"},
        "syn-route-agent-04": {"route":"AGENT","tools":["synthetic_order_lookup"],"business":"FACTS_VERIFIED","task":True,"evidence":["syn-order-ref"],"runs":2,"owner":"PASS"},
        "syn-route-refuse-05": {"route":"REFUSE","tools":[],"business":"SAFE_REFUSAL","task":True,"evidence":[],"runs":1,"owner":"PASS"},
        "syn-owner-fail-06": {"route":"AGENT","tools":[],"business":"ACCESS_DENIED","task":True,"evidence":[],"runs":1,"owner":"FAIL"},
        "syn-human-review-09": {"route":"DIRECT","tools":[],"business":"ANSWERED_GENERAL","task":False,"evidence":[],"runs":1,"owner":"PASS"},
        "syn-retry-idempotent-12": {"route":"AGENT","tools":["synthetic_order_lookup"],"business":"FACTS_VERIFIED","task":True,"evidence":["syn-item-ref"],"runs":2,"owner":"PASS"},
    }
    _TERMINAL = {
        "syn-run-unknown-07": ("UNKNOWN_RESULT", "response_lost"),
        "syn-env-error-08": ("ENV_ERROR", "fixture_dependency"),
        "syn-budget-stop-10": ("BUDGET_STOP", "preflight_budget"),
        "syn-model-unavailable-11": ("MODEL_UNAVAILABLE", "stub_unavailable"),
    }

    def planned_tools(self, case: Mapping[str, Any]) -> tuple[str, ...]:
        return tuple(self._RESPONSES.get(case.get("case_id"), {}).get("tools", ()))

    def planned_status(self, case: Mapping[str, Any]) -> str | None:
        return "BUDGET_STOP" if case.get("case_id") == "syn-budget-stop-10" else None

    def execute(self, case: Mapping[str, Any], trial_id: str) -> tuple[str, dict[str, Any], dict[str, Any]]:
        case_id = case.get("case_id")
        if case_id in self._TERMINAL:
            mode, stage = self._TERMINAL[case_id]
            return mode, {}, {"code": mode, "stage": stage}
        response = self._RESPONSES.get(case_id)
        if response is None:
            return "ENV_ERROR", {}, {"code":"UNKNOWN_SYNTHETIC_CASE"}
        result = {
            "route": response["route"], "tools": response["tools"], "schema_valid": True,
            "ownership_check": response["owner"], "unconfirmed_write": False,
            "evidence_ids": response["evidence"], "business_outcome": response["business"],
            "task_success_observable": response["task"], "safety_result": "PASS",
            "evidence_refs": response["evidence"],
        }
        run_ids = [f"synthetic-run.{trial_id}.{i}" for i in range(1, response["runs"] + 1)]
        return "SUCCEEDED", {"route": result["route"], "tools": result["tools"]}, {"result": result, "run_ids": run_ids}


def run_synthetic_experiment(*, cases: list[Any], experiment_id: str, store: ResultStore,
                             ledger: BudgetLedger, executor: SyntheticExecutor | None = None) -> dict[str, Any]:
    executor = executor or SyntheticExecutor()
    grader, reviews = CodeGrader(), ReviewService(store)
    experiment_started = time.monotonic()
    for index, case in enumerate(cases, 1):
        trial_id = f"trial.{experiment_id}.{case.case_id}.{index}"
        if store.trial(trial_id) is not None:
            continue  # Idempotent recovery: persisted trial is never executed again.
        started = _now()
        prior_events = store.trial_event_types(trial_id)
        if "RUNNING" in prior_events and not any(status in prior_events for status in LifecycleStatus.__members__):
            # A process died after start but before its immutable raw result commit. Never replay.
            trial = EvalTrial(trial_id, case.case_id, experiment_id, index, (), "UNKNOWN_RESULT", (), (),
                f"sqlite://trials/{trial_id}", {"code":"INTERRUPTED_BEFORE_RESULT_COMMIT"},
                {"provider_usage":"UNKNOWN","external_provider_calls":0,"latency_source":"recovery"}, {"total":None})
            now = _now()
            store.append_trial_bundle(trial=trial, raw_result={"lifecycle_status":"UNKNOWN_RESULT","recovery":"NO_REPLAY"},
                outcome=EvalOutcome(f"outcome.{uuid.uuid4().hex}", trial_id, "UNKNOWN", "UNKNOWN", "UNKNOWN", "UNKNOWN", "NOT_GRADED", grader.rubric_version, (), now),
                scores=[], at=now, terminal_event={"event_id":f"event.{uuid.uuid4().hex}","event_type":"UNKNOWN_RESULT","payload":{"reason":"INTERRUPTED_NO_REPLAY"},"created_at_utc":now})
            continue
        store.add_trial_event(f"event.{uuid.uuid4().hex}", trial_id, "RUNNING", {"case_id": case.case_id}, started)
        reservations: list[str] = []
        try:
            if time.monotonic() - experiment_started >= ledger.limits.max_duration_seconds:
                raise BudgetStop("experiment duration budget exhausted")
            # All external call categories are hard-zero for this synthetic experiment.
            case_payload = case.to_dict()
            for _tool in executor.planned_tools(case_payload):
                reservations.append(ledger.reserve(trial_id=trial_id, category="tool"))
            if executor.planned_status(case_payload) == "BUDGET_STOP":
                raise BudgetStop("fixture represents a pre-execution budget stop")
            lifecycle, calls, payload = executor.execute(case_payload, trial_id)
            for reservation in reservations:
                ledger.settle(reservation, status="COMMIT", actual_calls=1, actual_tokens=0, estimated_usd=0.0)
        except BudgetStop as exc:
            for reservation in reservations:
                ledger.settle(reservation, status="RELEASE", actual_calls=0, actual_tokens=None, estimated_usd=None)
            lifecycle, calls, payload = "BUDGET_STOP", {}, {"code":"BUDGET_STOP","reason":str(exc)}
        except Exception as exc:  # deterministic harness failure remains visible, not replayed silently
            for reservation in reservations:
                try:
                    ledger.settle(reservation, status="RELEASE", actual_calls=0, actual_tokens=None, estimated_usd=None)
                except Exception:
                    pass
            lifecycle, calls, payload = "ENV_ERROR", {}, {"error_type": type(exc).__name__}
        result = payload.get("result", {})
        error = payload if lifecycle != "SUCCEEDED" else None
        raw_result = {"lifecycle_status": lifecycle, "executor": "SYNTHETIC_DETERMINISTIC", "result": result, "error": error}
        if lifecycle == "SUCCEEDED" and case.case_id in {"syn-route-single-02", "syn-route-agent-04", "syn-human-review-09"}:
            stub_verdict = "PASS" if case.case_id == "syn-route-single-02" else "UNKNOWN"
            stub = SyntheticJudgeStub({trial_id: {"answer_support": stub_verdict}})
            response = stub.grade(JudgeRequest(
                request_id=f"judge-request.{trial_id}", trial_id=trial_id,
                rubric=JudgeRubric("synthetic-answer-review", "r1", ("answer_support",)),
                blinded_input={"synthetic_case_id": case.case_id}, allowed_evidence=tuple(result.get("evidence_refs", [])),
            ))
            raw_result["synthetic_judge"] = {"provider": response.provider, "verdicts": dict(response.verdicts), "usage": dict(response.usage), "external_request": False}
        run_ids = tuple(payload.get("run_ids", ()))
        elapsed = max(0.01, (datetime.now(timezone.utc) - datetime.fromisoformat(started.replace("Z", "+00:00"))).total_seconds() * 1000)
        trial = EvalTrial(
            trial_id=trial_id, case_id=case.case_id, experiment_id=experiment_id, trial_index=index,
            run_ids=run_ids, lifecycle_status=lifecycle, model_calls=(),
            tool_calls=tuple({"tool_id": tool, "provider": "SYNTHETIC_EXECUTOR", "status": "SUCCEEDED"} for tool in calls.get("tools", [])),
            raw_result_ref=f"sqlite://trials/{trial_id}", error=error,
            usage={"provider_usage": "NOT_APPLICABLE_SYNTHETIC", "prompt_tokens": None, "completion_tokens": None,
                   "external_provider_calls": 0, "judge_calls": 0, "embedding_calls": 0, "latency_source":"synthetic harness wall clock"},
            latency_ms={"total": elapsed, "provider": None},
        )
        score_records: list[dict[str, Any]] = []
        review_task = None
        if lifecycle == "SUCCEEDED":
            graded = grader.grade(trial_id=trial_id, expected=case.expected_outcome, result=result, lifecycle=lifecycle)
            score_records = list(graded.scores)
            outcome = graded.outcome
            if graded.needs_human_review:
                review_task = {"review_id":f"review.{uuid.uuid4().hex}","trial_id":trial_id,
                    "status":"PENDING_HUMAN_REVIEW","reason":"semantic result is not deterministically scoreable",
                    "evidence_refs":list(result.get("evidence_refs", [])),"created_at_utc":_now()}
        else:
            outcome = EvalOutcome(
                outcome_id=f"outcome.{uuid.uuid4().hex}", trial_id=trial_id,
                run_success="FAIL" if lifecycle in {"FAILED", "ENV_ERROR", "MODEL_UNAVAILABLE", "BUDGET_STOP"} else "UNKNOWN",
                answer_correctness="UNKNOWN", task_success="UNKNOWN", safety_result="UNKNOWN",
                grading_status="NOT_GRADED", rubric_version=grader.rubric_version,
                score_refs=(), created_at_utc=_now(),
            )
        now = _now()
        store.append_trial_bundle(trial=trial, raw_result=raw_result, outcome=outcome, scores=score_records, at=now,
            terminal_event={"event_id":f"event.{uuid.uuid4().hex}","event_type":lifecycle,
                "payload":{"run_ids":list(run_ids)},"created_at_utc":now}, review_task=review_task)
    trials = store.trials(experiment_id)
    scores = store.scores(experiment_id)
    return aggregate(trials, scores, ledger.snapshot(), store.outcomes_for_experiment(experiment_id), store.reviews_for_experiment(experiment_id))


@dataclass(frozen=True)
class FormalTrialResult:
    trial: EvalTrial
    raw_result: dict[str, Any]
    grade: GradeResult
    integrity_checks: Mapping[str,str]
    blocking_items: tuple[str, ...] = ()


def run_authorized_experiment(*,cases,mode,grant,mode_gate,experiment_id: str,store: ResultStore,
        ledger: BudgetLedger,prepare,execute,cleanup,inspect_failure=None) -> dict[str, Any]:
    """Formal entry sharing existing trial/grader/store/budget contracts.

    The reviewed target factory supplies receipt prepare, budget-aware actual
    application execution and exact cleanup. It is bound in the signed runtime
    artifacts/config, not inferred from a mode or an injected synthetic label.
    No Agent/model implementation or Provider loop is duplicated here.
    """
    from .execution_modes import ExecutionMode,ModeAuthorizationError
    from .grants import FormalExecutionGrant,GrantDenied
    if mode not in {ExecutionMode.DEV_SMOKE,ExecutionMode.FROZEN_EVAL,ExecutionMode.ENGINEERING_EVAL,ExecutionMode.PERSONAL_REAL_EVAL} or not isinstance(grant,FormalExecutionGrant):
        raise ModeAuthorizationError("formal mode requires its signed grant")
    authority=mode_gate.formal_authority;binding=mode_gate.formal_binding(mode)
    authority.verify(grant,**binding)
    if [c.case_id for c in cases]!=[cid for cid,_ in grant.plan.case_bindings]: raise GrantDenied("formal exact order/scope mismatch")
    if experiment_id!=grant.plan.experiment_id or ledger.experiment_id!=experiment_id or ledger.store.path!=store.path or ledger.limits!=grant.plan.budget:
        raise GrantDenied("formal budget/store binding mismatch")
    with store.transaction(immediate=False) as db:
        experiment=db.execute("SELECT config_sha256 FROM experiments WHERE experiment_id=?",(experiment_id,)).fetchone()
    if experiment is None or experiment[0]!=grant.plan.config_sha256: raise GrantDenied("formal experiment lock missing/mismatched")
    # Recover only durable terminal work. An interrupted unknown execution is
    # never replayed; its receipt remains for reconciliation, not blind cleanup.
    stop=None;completed=[]
    for index,case in enumerate(cases,1):
        boundary_started=authority.clock()
        with store.transaction(immediate=False) as db:
            first=db.execute("SELECT created_at_utc FROM formal_grant_events WHERE grant_id=? AND event_type='CLAIMED' ORDER BY rowid LIMIT 1",(grant.grant_id,)).fetchone()
        elapsed=(authority.clock()-datetime.fromisoformat(first[0])).total_seconds() if first else 0
        if elapsed>=ledger.limits.max_duration_seconds:
            stop="FORMAL_EXECUTION_DEADLINE";break
        trial_id=f"formal.{grant.grant_id}.{case.case_id}"
        with store.transaction(immediate=False) as db:
            events=db.execute("SELECT event_type FROM formal_grant_events WHERE grant_id=? AND attempt_id=?",(grant.grant_id,trial_id)).fetchall()
        existing=store.trial(trial_id)
        if events and any(r[0]=="SETTLED" for r in events):
            completed.append(case.case_id);continue
        if events and existing is None:
            stop="INTERRUPTED_UNKNOWN_NO_REPLAY";break
        snapshot=ledger.snapshot()
        if (not snapshot["complete"] or not snapshot["settled"] or
                snapshot["token_status"]=="UNKNOWN_USAGE" or snapshot["estimate_status"]=="UNKNOWN_USAGE" or
                snapshot['calls_status'] in {'UNKNOWN','CONSERVATIVE_UPPER_BOUND'}):
            stop="BUDGET_UNSETTLED_OR_UNKNOWN";break
        if existing is None:
            mode_gate.claim(mode=mode,grant=grant,case=case,attempt_id=trial_id)
            receipt=None;result=None
            try:
                receipt=prepare(case,trial_id)
                store.add_trial_event(uuid.uuid4().hex,trial_id,"PREPARED",{"receipt":receipt},_now())
                remaining=min(ledger.limits.max_duration_seconds-elapsed-(authority.clock()-boundary_started).total_seconds(),
                    (datetime.fromisoformat(grant.plan.expires_at_utc.replace('Z','+00:00'))-authority.clock()).total_seconds())
                if remaining<=0: raise BudgetStop("formal execution deadline")
                result=execute(case,trial_id,receipt,ledger,remaining_seconds=remaining)
                if not isinstance(result,FormalTrialResult): raise ValueError("formal execution result contract invalid")
                trial=result.trial
                if trial.case_id!=case.case_id or trial.trial_id!=trial_id or trial.experiment_id!=experiment_id:
                    raise ValueError("formal result identifiers mismatch")
                if result.grade.outcome.trial_id!=trial_id or any(score['trial_id']!=trial_id for score in result.grade.scores):
                    raise ValueError("formal grader trial binding mismatch")
                if set(result.integrity_checks)!={"permission","audit","usage","terminal"}:
                    raise ValueError("formal integrity dimensions missing")
                raw={**result.raw_result,"receipt":receipt,"execution_integrity":dict(result.integrity_checks),
                    "formal_result_schema_version":"formal-trial-result-v2", "execution_blocking_items":list(result.blocking_items)}
                review_task=({"review_id":"review."+uuid.uuid4().hex,"trial_id":trial_id,"status":"PENDING_HUMAN_REVIEW",
                    "reason":"Formal semantic dimensions require genuine review;code outcome is separate",
                    "evidence_refs":sorted({ref for score in result.grade.scores for ref in score['evidence_refs']}),
                    "created_at_utc":_now()} if result.grade.needs_human_review else None)
                store.append_trial_bundle(trial=trial,raw_result=raw,outcome=result.grade.outcome,
                    scores=list(result.grade.scores),at=_now(),terminal_event={"event_id":uuid.uuid4().hex,
                        "event_type":trial.lifecycle_status,"payload":{"run_ids":list(trial.run_ids)},"created_at_utc":_now()},review_task=review_task)
                existing=store.trial(trial_id)
            except Exception as exc:
                inspection = {"run_created":"UNKNOWN", "source":"NO_VERIFIED_TARGET_INSPECTION"}
                if receipt is not None and inspect_failure is not None:
                    try: inspection = {**inspection, **inspect_failure(receipt)}
                    except Exception as inspection_error:
                        inspection["inspection_error_type"] = type(inspection_error).__name__
                store.add_trial_event(uuid.uuid4().hex,trial_id,"EXECUTION_UNKNOWN",{
                    "error_type":type(exc).__name__,"receipt_saved":receipt is not None,
                    "reported_run_ids":list(result.trial.run_ids) if isinstance(result,FormalTrialResult) else inspection.get("run_ids"),
                    "run_created":inspection["run_created"], "target_inspection":inspection},_now())
                stop="EXECUTION_OR_EVALUATOR_UNKNOWN_NO_REPLAY";break
        raw=store.raw_result(trial_id)
        snapshot=ledger.trial_snapshot(trial_id)
        checks=raw["execution_integrity"]
        # R23 v1 had only the four critical dimensions; its frozen source is
        # preserved. New v2 producers must always supply explicit stop reasons.
        blockers = raw["execution_blocking_items"] if raw.get("formal_result_schema_version") == "formal-trial-result-v2" else []
        terminal=existing["lifecycle_status"] in {"SUCCEEDED","FAILED","MODEL_UNAVAILABLE","ENV_ERROR"}
        safe_cleanup=terminal and checks["permission"]=="PASS" and checks["audit"]=="PASS"
        cleanup_status="PRESERVED_UNSAFE_OR_UNKNOWN"
        if safe_cleanup:
            try: cleanup_status="CLEANED" if cleanup(raw["receipt"],existing,raw) is True else "FAILED"
            except Exception as exc:
                store.add_trial_event(uuid.uuid4().hex,trial_id,"CLEANUP_ERROR",{"error_type":type(exc).__name__},_now())
                cleanup_status="FAILED"
        # Quality/task FAIL does not block. Critical unknown/failed dimensions,
        # reservations or cleanup do; no all([]) pass and no unknown-as-zero.
        final_blockers = [*blockers, *(f"CRITICAL_{key.upper()}_{value}" for key,value in checks.items() if value != "PASS")]
        if not terminal: final_blockers.append("RUN_NONTERMINAL")
        if not snapshot["complete"] or not snapshot["settled"] or snapshot["unknown_tokens"] or snapshot["unknown_cost"]:
            final_blockers.append("BUDGET_UNKNOWN_OR_UNSETTLED")
        if ledger.snapshot()['calls_status'] in {'UNKNOWN','CONSERVATIVE_UPPER_BOUND'}:
            final_blockers.append("CALL_COUNT_UNKNOWN_OR_CONSERVATIVE")
        if cleanup_status != "CLEANED": final_blockers.append("CLEANUP_" + cleanup_status)
        allowed = not final_blockers
        store.add_trial_event(uuid.uuid4().hex,trial_id,"CLEANUP",{"status":cleanup_status,
            "decision":"CONTINUE" if allowed else "STOP", "blocking_items":final_blockers},_now())
        authority.settle(grant,case_id=case.case_id,attempt_id=trial_id,continue_allowed=allowed,cleanup_status=cleanup_status)
        completed.append(case.case_id)
        if not allowed: stop=blockers[0] if blockers else "CRITICAL_EXECUTION_USAGE_AUDIT_OR_CLEANUP";break
    if stop is None: authority.close(grant)
    with store.transaction(immediate=False) as db:
        consumed={r[0] for r in db.execute("SELECT case_id FROM formal_grant_events WHERE grant_id=? AND event_type='CLAIMED'",(grant.grant_id,)).fetchall()}
    return {"completed_cases":completed,"consumed_cases":[c.case_id for c in cases if c.case_id in consumed],
        "unresolved_consumed_cases":[c.case_id for c in cases if c.case_id in consumed and c.case_id not in completed],
        "stop_reason":stop,"not_run":[c.case_id for c in cases if c.case_id not in consumed],
        "budget":ledger.snapshot(),"trials":store.trials(experiment_id),"outcomes":store.outcomes_for_experiment(experiment_id)}
