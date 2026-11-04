"""Fail-closed execution modes for evaluation adapters.

Synthetic and formal signatures remain separate. Formal execution requires an
explicitly configured issuer, durable attempt store and exact target/config.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from .contracts import EvalCase
from .grants import (GrantDenied, SyntheticGrant, SyntheticGrantAuthority,
    FormalExecutionGrant,FormalGrantAuthority)


class ExecutionMode(str, Enum):
    SYNTHETIC = "SYNTHETIC"
    ENGINEERING_EVAL = "ENGINEERING_EVAL"
    PERSONAL_REAL_EVAL = "PERSONAL_REAL_EVAL"
    DEV_SMOKE = "DEV_SMOKE"
    FROZEN_EVAL = "FROZEN_EVAL"


class ModeAuthorizationError(PermissionError):
    pass


class ExecutionModeGate:
    """Authorize an exact signed case under its separately configured execution mode."""

    def __init__(self, synthetic_authority: SyntheticGrantAuthority | None, *, subject: str,
                 formal_authority: FormalGrantAuthority | None = None,
                 config_sha256: str | None = None, target: tuple[str,str,str] | None = None) -> None:
        if not subject:
            raise ValueError("runner subject is required")
        self._authority = synthetic_authority
        self._subject = subject
        self.formal_authority=formal_authority
        self.config_sha256,self.target=config_sha256,target

    @property
    def subject(self) -> str: return self._subject

    def formal_binding(self, mode: ExecutionMode) -> dict:
        if not isinstance(mode,ExecutionMode) or mode is ExecutionMode.SYNTHETIC:
            raise ModeAuthorizationError("typed formal mode required")
        if self.formal_authority is None or not self.config_sha256 or self.target is None:
            raise ModeAuthorizationError(f"{mode.value} has no authorization issuer configured")
        return {"subject":self._subject,"mode":mode.value,"config_sha256":self.config_sha256,"target":self.target}

    def claim(self,*,mode: ExecutionMode,grant: FormalExecutionGrant,case: EvalCase,attempt_id: str):
        binding=self.formal_binding(mode)
        try: self.formal_authority.claim(grant,case=case,attempt_id=attempt_id,**binding)
        except GrantDenied as exc: raise ModeAuthorizationError(str(exc)) from exc

    def authorize(self, *, mode: ExecutionMode, grant: Any, case: EvalCase,attempt_id: str | None = None) -> None:
        if not isinstance(mode, ExecutionMode):
            raise ModeAuthorizationError("mode must be a typed ExecutionMode")
        if mode is not ExecutionMode.SYNTHETIC:
            binding=self.formal_binding(mode)
            try: self.formal_authority.verify(grant,case=case,attempt_id=attempt_id,**binding)
            except GrantDenied as exc: raise ModeAuthorizationError(str(exc)) from exc
            return
        if not isinstance(grant, SyntheticGrant):
            raise ModeAuthorizationError("synthetic mode requires a signed synthetic asset grant")
        if self._authority is None: raise ModeAuthorizationError("synthetic issuer not configured")
        try:
            granted_cases = self._authority.read_cases(grant, subject=self._subject)
        except GrantDenied as exc:
            raise ModeAuthorizationError("synthetic asset grant is invalid") from exc
        if case.split != "synthetic" or not any(
            item.case_id == case.case_id and item.to_dict() == case.to_dict()
            for item in granted_cases
        ):
            raise ModeAuthorizationError("case is not covered by the signed synthetic grant")


def formal_cli(argv,*,repository_root):
    """Owner-configured issue/preflight/execute path; no built-in real identity/key.

    Only metadata is loaded before signature verification. Tests inject fresh
    local fixtures and a test signing key; protected project assets stay closed.
    """
    import argparse,base64,importlib.util,json,os
    from dataclasses import asdict
    from pathlib import Path
    from .budget import BudgetLimits,BudgetLedger
    from .contracts import sha256_json
    from .store import ResultStore
    from .registry import DatasetRegistry
    from .grants import FormalExecutionPlan
    parser=argparse.ArgumentParser()
    parser.add_argument("--formal-config",type=Path,required=True)
    action=parser.add_mutually_exclusive_group(required=True)
    for name in ("issue","preflight","execute","report","receive-review","adjudicate-review","admission","recover-grant"): action.add_argument("--"+name,action="store_true")
    parser.add_argument("--submission", type=Path)
    parser.add_argument("--review-phase", choices=("day1", "blind"))
    args=parser.parse_args(argv)
    root=Path(repository_root).resolve(strict=True)
    config_path = args.formal_config if args.formal_config.is_absolute() else root / args.formal_config
    if not config_path.resolve().is_relative_to(root):
        raise ModeAuthorizationError("formal configuration outside configured repository")
    config=json.loads(config_path.read_text(encoding="utf8"))
    def local(relative):
        path=(root/relative).resolve()
        if not path.is_relative_to(root): raise ModeAuthorizationError("formal path outside configured repository")
        return path
    def plan_from(value):
        value=dict(value);value["budget"]=BudgetLimits(**value["budget"])
        for key in ("case_bindings","runtime_bindings"): value[key]=tuple(tuple(r) for r in value[key])
        if 'pre_run_recoveries' in value:value['pre_run_recoveries']=tuple(tuple(r) for r in value['pre_run_recoveries'])
        if 'runtime_input_bindings' in value:value['runtime_input_bindings']=tuple(tuple(r) for r in value['runtime_input_bindings'])
        return FormalExecutionPlan(**value)
    plan=plan_from(config["plan"])
    if config["experiment_id"]!=plan.experiment_id:
        raise ModeAuthorizationError("formal budget experiment differs from approved plan")
    if config.get("signature_scheme") == "ED25519_V1":
        verification_key = base64.b64decode(config["verification_public_key_base64"], validate=True)
        key = base64.b64decode(os.environ[config["signing_key_env"]], validate=True) if args.issue else None
    else:
        verification_key = None
        key=base64.b64decode(os.environ[config["signing_key_env"]],validate=True)
    if local(config["result_store"])!=local(plan.ledger_path):
        raise ModeAuthorizationError("formal ledger path is outside the approved plan")
    if not local(plan.ledger_path).is_file():
        raise ModeAuthorizationError("registered formal ledger missing; deployment initialization/reconciliation required")
    store=ResultStore(local(config["result_store"]))
    authority=FormalGrantAuthority(DatasetRegistry.load_metadata(local(config["registry_metadata"])),
        repository_root=root,store=store,issuer=config["issuer"],trusted_issuers=set(config["trusted_issuers"]),
        signing_key=key,verification_key=verification_key,approved_plan_hashes=set(config["approved_plan_hashes"]),
        allowed_targets={tuple(t) for t in config["allowed_targets"]})
    def worker_for(grant, *, before_registration=False, for_issuance=False):
        from .contracts import sha256_json
        from .grants import GrantDenied, formal_runtime_digest
        import hashlib
        if sha256_json(asdict(plan)) not in authority.approved_plans or plan.config_sha256 != sha256_json(config["experiment_config"]) or plan.execution_implementation_sha256 != formal_runtime_digest():
            raise GrantDenied("deployment is not the exact approved configuration/runtime")
        factory = config["experiment_config"]["environment"]["execution_factory"]
        expected = dict(plan.runtime_bindings).get(factory["path"])
        if expected is None or hashlib.sha256(local(factory["path"]).read_bytes()).hexdigest() != expected:
            raise ModeAuthorizationError("execution factory source not bound or changed")
        spec = importlib.util.spec_from_file_location("eval.m6_1._approved_formal_target_factory", local(factory["path"]))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ledger = (None if before_registration and store.budget_limits(config["experiment_id"]) is None
                  else BudgetLedger(store, config["experiment_id"], plan.budget))
        gate = ExecutionModeGate(None, subject=config["subject"], formal_authority=authority,
            config_sha256=sha256_json(config["experiment_config"]), target=tuple(config["actual_target_binding"]))
        worker = getattr(module, factory["callable"])(config=config, store=store, ledger=ledger, mode_gate=gate, grant=grant)
        if for_issuance and hasattr(worker, "verify_signing_subject"):
            if worker.verify_signing_subject() is not True:
                raise ModeAuthorizationError("authenticated issuer principal mismatch")
        elif worker.verify_execution_subject(gate.subject) is not True:
            raise ModeAuthorizationError("authenticated execution principal mismatch")
        if worker.verify_execution_target(gate.target) is not True:
            raise ModeAuthorizationError("actual formal target identity mismatch")
        if hasattr(worker, "preflight"):
            if for_issuance: worker.preflight(require_provider_key=False)
            else: worker.preflight()
        return worker, ledger
    grant_path=local(config["grant_file"])
    if args.issue or args.recover_grant:
        # Refuse overwriting a closed/revoked grant even before consuming its
        # approval. An approval_ref is also UNIQUE across restarts in the store.
        if grant_path.exists(): raise ModeAuthorizationError("formal grant file exists; immutable")
        preview = FormalExecutionGrant("UNISSUED_PREFLIGHT", authority.issuer, plan, "0" * 64, "")
        worker_for(preview, before_registration=True, for_issuance=True)
        if args.recover_grant:
            with store.transaction(immediate=False) as db:
                orphan = db.execute("SELECT payload_json FROM formal_grants WHERE approval_ref=?", (plan.approval_ref,)).fetchone()
            if orphan is None: raise ModeAuthorizationError("no persisted grant exists for recovery")
            value = json.loads(orphan[0]); value["plan"] = plan_from(value["plan"])
            grant = FormalExecutionGrant(**value)
            if grant.plan != plan: raise ModeAuthorizationError("recovery plan differs from existing grant")
            authority.verify(grant, subject=plan.subject, mode=plan.mode, config_sha256=plan.config_sha256,
                target=(plan.target_id,plan.target_profile,plan.target_identity))
        else:
            grant=authority.issue(plan)
        from .formal_assets import write_new
        write_new(grant_path, asdict(grant))
        return {"status":"SIGNED_SCOPED_GRANT_CREATED","grant_id":grant.grant_id,"mode":plan.mode}
    payload=json.loads(grant_path.read_text(encoding="utf8"));payload["plan"]=plan_from(payload["plan"])
    grant=FormalExecutionGrant(**payload)
    if grant.plan!=plan: raise ModeAuthorizationError("deployment plan differs from issued plan")
    if plan.config_sha256 != sha256_json(config["experiment_config"]) or config["subject"] != plan.subject:
        raise ModeAuthorizationError("deployment configuration/subject differs from signed plan")
    target=(plan.target_id,plan.target_profile,plan.target_identity)
    gate=ExecutionModeGate(None,subject=config["subject"],formal_authority=authority,
        config_sha256=sha256_json(config["experiment_config"]),target=tuple(config["actual_target_binding"]))
    mode=ExecutionMode(plan.mode)
    if args.report or args.receive_review or args.adjudicate_review:
        from .formal_assets import authenticated_subject, confined, write_new
        from .formal_scoring import recompute, output_review_form
        from .contracts import EvalCase
        from .grants import formal_case_digest
        authority.verify_archive_access(grant, subject=authenticated_subject())
        cases = []
        for trial in store.trials(config["experiment_id"]):
            raw = store.raw_result(trial["trial_id"])
            path = confined(root, raw["case_path"])
            case = EvalCase.from_dict(json.loads(path.read_text(encoding="utf8")))
            if formal_case_digest(case) != dict(plan.case_bindings)[case.case_id]:
                raise ModeAuthorizationError("archived case no longer matches signed scope")
            cases.append(case)
        if args.receive_review or args.adjudicate_review:
            from .formal_scoring import receive_output_review, adjudicate_outputs
            if args.submission is None: raise ModeAuthorizationError("bound review submission required")
            submission = json.loads(local(str(args.submission)).read_text(encoding="utf8"))
            trial = store.trial(submission["trial_id"])
            if trial is None or trial["experiment_id"] != config["experiment_id"]:
                raise ModeAuthorizationError("review trial outside approved experiment")
            case = next(c for c in cases if c.case_id == trial["case_id"])
            raw = store.raw_result(trial["trial_id"])
            reviewers = config["experiment_config"]["environment"]["output_reviewers"]
            if args.receive_review:
                return receive_output_review(case, trial, raw, store, submission, phase=args.review_phase, trusted_reviewers=reviewers)
            return adjudicate_outputs(case, trial, raw, store, submission, trusted_reviewers=reviewers)
        scopes = config["experiment_config"].get("reporting_cohorts")
        if scopes is not None:
            if set(scopes) != {"route", "retrieval", "outcome"} or any(len(set(ids)) != len(ids) or not set(ids) <= dict(plan.case_bindings).keys() for ids in scopes.values()):
                raise ModeAuthorizationError("reporting cohort outside frozen case scope")
        report = recompute(cases, store, config["experiment_id"], scopes=scopes, repository_root=root)
        forms = [output_review_form(c, t, store.raw_result(t["trial_id"]))
            for c, t in zip(cases, store.trials(config["experiment_id"]))]
        report["output_review_forms"] = forms
        report["planned_case_ids"] = [cid for cid, _ in plan.case_bindings]
        report["not_run_case_ids"] = [cid for cid, _ in plan.case_bindings if cid not in {c.case_id for c in cases}]
        with store.transaction(immediate=False) as db:
            report["authorization_events"] = [dict(r) for r in db.execute("SELECT event_type,case_id,attempt_id,payload_json,created_at_utc FROM formal_grant_events WHERE grant_id=? ORDER BY rowid", (grant.grant_id,)).fetchall()]
        claims = {e["case_id"]: e["attempt_id"] for e in report["authorization_events"] if e["event_type"] == "CLAIMED"}
        report["not_run_case_ids"] = [cid for cid, _ in plan.case_bindings if cid not in claims]
        report["unresolved_consumed_attempts"] = [{"case_id":cid,"attempt_id":attempt,
            "inspection_events":[event for event in store.trial_events(attempt) if event["event_type"] == "EXECUTION_UNKNOWN"],
            "scoring_status":"UNRESOLVED_EXECUTION_OR_EVALUATOR_FAILURE_NOT_NOT_RUN"}
            for cid,attempt in claims.items() if store.trial(attempt) is None]
        report["coverage"] = {"planned_cases":len(plan.case_bindings), "consumed_attempts":len(claims),
            "archived_trials":len(cases), "unresolved_consumed_attempts":len(report["unresolved_consumed_attempts"]),
            "never_consumed_cases":len(report["not_run_case_ids"])}
        if report["unresolved_consumed_attempts"]:
            report["metrics"]["scope_note"] = "ARCHIVED_TRIAL_SUBSET_ONLY; unresolved consumed cases and evaluator failures are separately retained,not counted as NOT_RUN"
            report["metrics"]["quality_targets"] = {k:None for k in report["metrics"]["quality_targets"]}
            report["metrics"]["task_success_strict_PASS"]["value"] = None
            report["metrics"]["task_success_strict_PASS"]["status"] = "INCOMPLETE_EXECUTION_OR_EVALUATOR_COVERAGE"
        report["status"] = 'RECOMPUTED_PERSONAL_REAL_NOT_INDEPENDENT_FROZEN' if plan.mode=='PERSONAL_REAL_EVAL' else "RECOMPUTED_ENGINEERING_NOT_QUALITY_APPROVAL"
        report_path = store.path.parent / ("formal-report-" + report["binding_sha256"] + ".json")
        if not report_path.exists(): write_new(report_path, report)
        elif json.loads(report_path.read_text(encoding="utf8")) != report:
            raise ModeAuthorizationError("existing report conflicts with recomputation")
        return {"status": report["status"], "report_path": report_path.relative_to(root).as_posix(),
            "trials": len(cases), "binding_sha256": report["binding_sha256"]}
    authority.verify(grant,**gate.formal_binding(mode))
    if args.preflight:
        worker_for(grant, before_registration=True)
        return {"status":"FORMAL_METADATA_SIGNATURE_PREFLIGHT_PASSED","content_read":False,
            "mode":mode.value,"grant_id":grant.grant_id,"runtime_target_verification":"BOUND_FACTORY_ACTUAL_IDENTITY_AND_TARGET_VERIFIED"}
    from .experiment import preregister_experiment
    from .runner import run_authorized_experiment
    locked=preregister_experiment(store=store,experiment_id=config["experiment_id"],config=config["experiment_config"],
        git_commit=config["source_revision_label"],strategy=mode.value,scoring_version=config["scoring_version"],
        formal_gate=gate,formal_grant=grant)
    worker, ledger = worker_for(grant)
    cases=authority.read_cases(grant,subject=config["subject"],config_sha256=gate.config_sha256,target=target)
    if hasattr(worker, "admit"):
        admission = worker.admit(cases)
        if args.admission: return admission
    elif args.admission:
        raise ModeAuthorizationError("bound factory has no actual Repository admission method")
    result = run_authorized_experiment(cases=cases,mode=mode,grant=grant,mode_gate=gate,
        experiment_id=config["experiment_id"],store=store,ledger=ledger,
        prepare=worker.prepare,execute=worker.execute,cleanup=worker.cleanup,inspect_failure=getattr(worker,"inspect_failure",None))
    if hasattr(worker,'finish'):
        try:worker.finish(cases,result)
        except Exception as exc:
            result['remote_close_error_type']=type(exc).__name__
            result['stop_reason']=result['stop_reason'] or 'REMOTE_PERMIT_CLOSE_UNVERIFIED'
    from .formal_assets import write_new
    from .contracts import sha256_json
    path = store.path.parent / ("execution-closeout-" + sha256_json(result) + ".json")
    if not path.exists(): write_new(path, result)
    return result
