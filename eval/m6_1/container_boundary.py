"""Gold-free application RPC adapter for the existing formal Runner/factory.

The controller keeps cases and graders. The application receives exactly one
signed runtime input plus limits; it delegates to FormalApplication evidence-only
execution and the same owner-scoped Repository/action/Checkpoint implementation.
"""
from __future__ import annotations
from dataclasses import asdict
from pathlib import Path
import base64,json,os,time,hashlib
from types import SimpleNamespace
from pydantic import BaseModel,ConfigDict,Field
from fastapi import FastAPI,HTTPException

from .contracts import EvalCase,EvalTrial,sha256_json,canonical_json
from .budget import BudgetLedger,BudgetLimits,BudgetStop
from .formal_assets import write_new,raw_hash
from .grants import FormalExecutionPlan,FormalExecutionGrant,FormalGrantAuthority,formal_case_payload,formal_case_digest,formal_runtime_digest
from .store import ResultStore

ROOT=Path(__file__).resolve().parents[2]
TRUST=ROOT/'data/eval/formal-trust'


def runtime_payload(case):
    value=formal_case_payload(case)
    return {k:value[k] for k in ('case_id','group_id','split','data_version','input')} | {
        'limits':value['expected_outcome']['task']['per_case_budget']}


def read_grant(value):
    p=dict(value['plan']);p['budget']=BudgetLimits(**p['budget'])
    for k in ('case_bindings','runtime_bindings','runtime_input_bindings'):
        if k in p:p[k]=tuple(tuple(row) for row in p[k])
    if 'pre_run_recoveries' in p:p['pre_run_recoveries']=tuple(tuple(row) for row in p['pre_run_recoveries'])
    return FormalExecutionGrant(**{**value,'plan':FormalExecutionPlan(**p)})


def runtime_case(payload):
    if set(payload)!={'case_id','group_id','split','data_version','input','limits'}:raise ValueError('runtime envelope fields invalid')
    inp=payload['input'];fields={'user_query','user_id','owner_map','allowed_capabilities','fixture_refs','fixture','evaluation_at'}
    if 'owner_interaction' in inp:fields.add('owner_interaction')
    from .fixture_read_models import READ_TOOLS
    if set(inp)!=fields or not set(inp['allowed_capabilities'])<=READ_TOOLS:raise ValueError('runtime input/scope invalid')
    # Only a budget is internal to this evidence adapter. No reference, expected
    # route, required evidence, hidden answer or grading result crosses the RPC.
    return EvalCase(payload['case_id'],payload['group_id'],payload['split'],inp,
        {'task':{'per_case_budget':BudgetLimits(**payload['limits']).to_dict()}},(),
        'signed-runtime-fixture','high',payload['data_version'])


class Envelope(BaseModel):
    model_config=ConfigDict(extra='forbid')
    grant:dict
    payload:dict
    trial_id:str
    remaining_seconds:float | None = Field(default=None,gt=0)


def worker_setup(body,*,archive_access=False):
    from .governance import verify_container_process,load_governance
    from .registry import DatasetRegistry
    from .formal_application import FormalApplication
    verify_container_process(10002)
    config=json.loads((TRUST/'deployment.json').read_text())
    load_governance(ROOT,config['governance_binding'])
    if not config['engineering_only']:
        mode=json.loads((TRUST/'mode.json').read_text())
        if not archive_access and mode!={'engineering_only':False,'real_provider_allowed':True}:
            raise PermissionError('formal container is prepared but execution is not activated')
        from .governance import verify_isolation_evidence
        verify_isolation_evidence(ROOT,config['isolation_evidence_binding'],formal_runtime_digest())
    grant=read_grant(body.grant)
    if config['engineering_only'] and (grant.plan.asset_id not in config['engineering_asset_ids'] or grant.plan.mode!='ENGINEERING_EVAL'):
        raise PermissionError('engineering deployment does not authorize real candidate or Frozen execution')
    if grant.plan.config_sha256!=sha256_json(config['experiment_config']) or config['plan']!=asdict_json(grant.plan):
        raise PermissionError('worker deployment/config differs from signed plan')
    store=ResultStore(ROOT/grant.plan.ledger_path)
    authority=FormalGrantAuthority(DatasetRegistry.load_metadata(TRUST/'registry.json'),repository_root=ROOT,store=store,
        issuer=config['issuer'],trusted_issuers=set(config['trusted_issuers']),signing_key=None,
        verification_key=base64.b64decode(config['verification_public_key_base64']),
        approved_plan_hashes=set(config['approved_plan_hashes']),allowed_targets={tuple(r) for r in config['allowed_targets']})
    binding={'subject':config['subject'],'mode':grant.plan.mode,'config_sha256':grant.plan.config_sha256,
        'target':tuple(config['actual_target_binding'])}
    authority._verify_bindings(grant,**binding)
    if dict(grant.plan.runtime_input_bindings).get(body.payload['case_id'])!=sha256_json(body.payload):
        raise PermissionError('runtime scope/owner/arguments/budget changed')
    if body.trial_id!=f'formal.{grant.grant_id}.{body.payload["case_id"]}':raise PermissionError('attempt identifier mismatch')
    # Public permit is mirrored in the application ledger; no Gold is mounted.
    with store.transaction() as db:
        row=db.execute('SELECT payload_json FROM formal_grants WHERE grant_id=?',(grant.grant_id,)).fetchone()
        if row is None:db.execute('INSERT INTO formal_grants VALUES(?,?,?)',(grant.grant_id,grant.plan.approval_ref,canonical_json(asdict(grant))))
        elif row[0]!=canonical_json(asdict(grant)):raise PermissionError('persisted worker permit changed')
        db.execute('CREATE TABLE IF NOT EXISTS container_attempts(trial_id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,stage TEXT NOT NULL,receipt_json TEXT,result_json TEXT)')
    if archive_access:authority.verify_archive_access(grant,subject=config['subject'])
    else:authority.verify(grant,**binding)
    if store.budget_limits(grant.plan.experiment_id) is None:
        from .experiment import preregister_experiment
        from .execution_modes import ExecutionModeGate
        gate=ExecutionModeGate(None,subject=config['subject'],formal_authority=authority,config_sha256=grant.plan.config_sha256,target=binding['target'])
        preregister_experiment(store=store,experiment_id=grant.plan.experiment_id,config=config['experiment_config'],git_commit='ENGINEERING_IMAGE_BINDING_NOT_GIT_HEAD',
            strategy=grant.plan.mode,scoring_version='formal-code-grading-v1',formal_gate=gate,formal_grant=grant)
    ledger=BudgetLedger(store,grant.plan.experiment_id,grant.plan.budget)
    class RuntimeGate:
        formal_authority=authority
        def authorize(self,**kwargs):
            authority.verify(grant,**binding)
            if dict(grant.plan.runtime_input_bindings).get(body.payload['case_id'])!=sha256_json(body.payload):raise PermissionError('runtime changed')
    worker=FormalApplication(config=config,store=store,ledger=ledger,mode_gate=RuntimeGate(),grant=grant)
    with worker.session_factory() as db:worker.target.check_session(db)
    if (worker.target.identity!=grant.plan.target_identity or worker.target.profile!=grant.plan.target_profile):raise PermissionError('actual target marker mismatch')
    return worker,store,authority,grant,binding,ledger,runtime_case(body.payload)


def asdict_json(value):return json.loads(json.dumps(asdict(value)))


app=FastAPI(docs_url=None,redoc_url=None)

@app.get('/boundary/health')
def health():
    from .governance import verify_container_process
    from .personal_dev_database import FormalPostgresDatabase
    identity=verify_container_process(10002)
    binding=json.loads((TRUST/'target-binding.json').read_text())
    target=FormalPostgresDatabase(ROOT,binding);engine,factory=target.open()
    from sqlalchemy import text
    with factory() as db:
        counts={t:db.execute(text('SELECT count(*) FROM '+t)).scalar_one() for t in ['runs','controlled_pending_actions']}
        active=db.execute(text("SELECT count(*) FROM runs WHERE status NOT IN ('SUCCEEDED','FAILED','CANCELLED','MODEL_UNAVAILABLE')")).scalar_one()
        marker=db.execute(text('SELECT lease_id FROM d1_personal_target')).scalar_one()
    engine.dispose()
    mode=json.loads((TRUST/'mode.json').read_text())
    return {**identity,'target_identity':target.identity,'target_profile':target.profile,'active_runs':active,
        'engineering_only':mode['engineering_only'],'real_provider_activated':mode['real_provider_allowed'],
        'pending_actions':counts['controlled_pending_actions'],'lease_free':marker is None,'runtime':formal_runtime_digest()}


@app.post('/boundary/{operation}')
def operation(operation:str,body:Envelope):
    worker=None
    try:
        worker,store,authority,grant,binding,ledger,case=worker_setup(body,archive_access=operation in {'cleanup','inspect','close'})
        with store.transaction(immediate=False) as db:row=db.execute('SELECT * FROM container_attempts WHERE trial_id=?',(body.trial_id,)).fetchone()
        if operation=='prepare':
            if row is not None:raise PermissionError('attempt consumed;prepare cannot be replayed')
            authority.claim_runtime(grant,runtime_payload=body.payload,attempt_id=body.trial_id,**binding)
            with store.transaction() as db:db.execute('INSERT INTO container_attempts VALUES(?,?,?,?,?)',(body.trial_id,canonical_json(body.payload),'CLAIMED',None,None))
            receipt=worker.prepare(case,body.trial_id)
            with store.transaction() as db:db.execute('UPDATE container_attempts SET stage=?,receipt_json=? WHERE trial_id=?',('PREPARED',canonical_json(receipt),body.trial_id))
            return receipt
        if row is None or row['payload_json']!=canonical_json(body.payload):raise PermissionError('no matching persisted attempt')
        if operation=='close':
            authority.close(grant);return {'closed':True}
        receipt=json.loads(row['receipt_json'])
        if operation=='inspect':
            inspection=worker.inspect_failure(receipt) if row['stage'] in {'PREPARED','EXECUTING'} else {'stage':row['stage'],'receipt':receipt,'run_created':'UNKNOWN'}
            return inspection
        if operation=='execute':
            if row['stage']!='PREPARED':raise PermissionError('Run attempt already consumed;inspect,never replay')
            if not worker.config['engineering_only']:
                # Activation may change after a prepared container starts.
                # Reuse the signed startup activation boundary before any Run,
                # rather than treating a changed flag as a loaded credential.
                from .container_startup import configure_execution
                configure_execution(ROOT,json.loads((TRUST/'mode.json').read_text()))
            with store.transaction() as db:db.execute('UPDATE container_attempts SET stage=? WHERE trial_id=?',('EXECUTING',body.trial_id))
            from datetime import datetime,timezone
            expiry=(datetime.fromisoformat(grant.plan.expires_at_utc.replace('Z','+00:00'))-datetime.now(timezone.utc)).total_seconds()
            remaining=min(case.expected_outcome['task']['per_case_budget']['max_duration_seconds'],expiry,
                body.remaining_seconds if body.remaining_seconds is not None else expiry)
            if remaining<=0:raise PermissionError('remaining execution deadline exhausted')
            result=worker.execute(case,body.trial_id,receipt,ledger,remaining_seconds=remaining,evidence_only=True)
            with store.transaction() as db:db.execute('UPDATE container_attempts SET stage=?,receipt_json=?,result_json=? WHERE trial_id=?',('ARCHIVED',canonical_json(receipt),canonical_json(result),body.trial_id))
            return result
        if operation=='verify-repeat':
            if worker.config['engineering_only'] is not True or row['stage']!='ARCHIVED':raise PermissionError('repeat proof is engineering-only on terminal archived work')
            result=json.loads(row['result_json']);ev=result['archive'].get('owner_interaction')
            if ev is None or ev['required_action_state']!='SUCCEEDED' or ev['state_check']!='PASS':raise PermissionError('only already successful actions may be verified')
            from app.m4.actions import confirm_pending_action,ActionError
            from .formal_actions import action_snapshot
            from .formal_application import restore_receipt
            held_receipt=restore_receipt(receipt['fixture']);before=action_snapshot(worker,held_receipt,ev['products']);action=ev['snapshot']['controlled_pending_actions'][0]
            reservation=ledger.reserve(trial_id=body.trial_id+':engineering-repeat-proof',category='tool',calls=2)
            with worker.session_factory() as db:
                actual,replayed=confirm_pending_action(db,owner_id=action['owner_id'],action_id=action['action_id'],simulated_enabled=False,
                    environment_name='shoprelay_m11_test',confirmed_arguments_hash=action['arguments_hash'])
                if not replayed or actual.status!='SUCCEEDED':raise ValueError('repeat confirmation unexpectedly executed')
            wrong_owner_denied=False
            try:
                with worker.session_factory() as db:confirm_pending_action(db,owner_id='different-engineering-owner',action_id=action['action_id'],
                    simulated_enabled=False,environment_name='shoprelay_m11_test',confirmed_arguments_hash=action['arguments_hash'])
            except ActionError:wrong_owner_denied=True
            after=action_snapshot(worker,held_receipt,ev['products'])
            ledger.settle(reservation,status='COMMIT',actual_calls=2,actual_tokens=0,estimated_usd=0)
            if before!=after or not wrong_owner_denied:raise ValueError('idempotency/owner verification failed')
            return {'repeat_confirm_replayed_without_effect':True,'wrong_owner_denied':True,'verification_calls':2,'accounting_trial':body.trial_id+':engineering-repeat-proof'}
        if operation=='cleanup':
            if row['stage'] not in {'ARCHIVED','CLEANED'}:raise PermissionError('unknown execution;preserve scene')
            result=json.loads(row['result_json'])
            cleaned=worker.cleanup(receipt,{'trial_id':body.trial_id},{'application_archive':result['archive'],'archive_sha256':result['archive_sha256']})
            if cleaned and row['stage']!='CLEANED':
                known=ledger.trial_snapshot(body.trial_id)
                authority.settle(grant,case_id=case.case_id,attempt_id=body.trial_id,continue_allowed=not known['unknown_tokens'] and not known['unknown_cost'],cleanup_status='CLEANED')
                with store.transaction() as db:db.execute('UPDATE container_attempts SET stage=? WHERE trial_id=?',('CLEANED',body.trial_id))
            return {'cleaned':cleaned,'proof':json.loads(worker.artifact_path(body.trial_id,'.cleanup.json').read_text())}
        raise ValueError('unknown operation')
    except (ValueError,PermissionError,KeyError) as exc:
        raise HTTPException(403,detail={'stage':'BOUNDARY_REJECTED','error_type':type(exc).__name__}) from exc
    finally:
        if worker is not None:worker.engine.dispose()


class ContainerApplication:
    """Controller adapter: same Runner/Grader; no application or model in this process."""
    def __init__(self,*,config,store,ledger,mode_gate,grant):
        self.config,self.store,self.ledger,self.gate,self.grant=config,store,ledger,mode_gate,grant
        self.root=mode_gate.formal_authority.root;self.env=config['experiment_config']['environment']
        self.artifacts=self.store.path.parent/'application'
    def artifact_path(self,trial_id,suffix):return self.artifacts/(hashlib.sha256(trial_id.encode()).hexdigest()+suffix)
    def verify_signing_subject(self):
        from .formal_assets import authenticated_subject
        return authenticated_subject()==self.env['issuer_subject']
    def verify_execution_subject(self,expected_subject):
        from .formal_assets import authenticated_subject
        return os.geteuid()==10001 and authenticated_subject()==expected_subject==self.grant.plan.subject
    def rpc(self,operation,case,trial_id,*,remaining_seconds=None):
        import httpx
        if self.env['container_endpoint']!='http://application:8080':raise PermissionError('unexpected internal application endpoint')
        with httpx.Client(trust_env=False,timeout=610) as client:
            response=client.post(self.env['container_endpoint']+'/boundary/'+operation,
                json={'grant':asdict_json(self.grant),'payload':runtime_payload(case),'trial_id':trial_id,'remaining_seconds':remaining_seconds})
            response.raise_for_status();return response.json()
    def preflight(self,*,require_provider_key=True):
        from .governance import load_governance
        load_governance(self.root,self.config['governance_binding'])
        if not self.config['engineering_only']:
            from .governance import verify_isolation_evidence
            verify_isolation_evidence(self.root,self.config['isolation_evidence_binding'],formal_runtime_digest())
        import httpx
        with httpx.Client(trust_env=False,timeout=15) as client:
            result=client.get(self.env['container_endpoint']+'/boundary/health');result.raise_for_status();observed=result.json()
        if observed['runtime']!=self.grant.plan.execution_implementation_sha256 or observed['target_identity']!=self.grant.plan.target_identity:
            raise PermissionError('container image/runtime or DB marker changed')
        if not self.config['engineering_only'] and require_provider_key and not observed['real_provider_activated']:
            raise PermissionError('formal provider activation is not complete')
        return observed
    def verify_execution_target(self,target):
        seen=self.preflight(require_provider_key=False)
        return target==(self.grant.plan.target_id,seen['target_profile'],seen['target_identity']) and not seen['active_runs']
    def admit(self,cases):
        if [c.case_id for c in cases]!=[c for c,_ in self.grant.plan.case_bindings]:raise PermissionError('container exact scope mismatch')
        for c in cases:
            if dict(self.grant.plan.runtime_input_bindings).get(c.case_id)!=sha256_json(runtime_payload(c)):raise PermissionError('runtime payload differs from issuer binding')
            runtime_case(runtime_payload(c))
        return {'cases':len(cases),'status':'EXACT_RUNTIME_INPUTS_BOUND;actual fixture prepare remains worker-owned'}
    def prepare(self,case,trial_id):
        self.gate.authorize(mode=__import__('eval.m6_1.execution_modes',fromlist=['ExecutionMode']).ExecutionMode(self.grant.plan.mode),grant=self.grant,case=case,attempt_id=trial_id)
        self.artifacts.mkdir(parents=True,exist_ok=True)
        write_new(self.artifact_path(trial_id,'.case.json'),formal_case_payload(case))
        return self.rpc('prepare',case,trial_id)
    def execute(self,case,trial_id,receipt,ledger,*,remaining_seconds):
        from .formal_scoring import evaluate,grade_result
        from .runner import FormalTrialResult
        limit=BudgetLimits(**case.expected_outcome['task']['per_case_budget'])
        price_path=self.root/self.env['price_evidence']['path']
        if raw_hash(price_path)!=self.env['price_evidence']['sha256']:raise PermissionError('price evidence binding changed')
        rate=json.loads(price_path.read_text())['conservative_usd_per_million']
        # Reserve a conservative token-priced upper bound, not the entire
        # monetary cap for every case. The worker retains all per-case caps.
        cost=min(limit.estimated_usd,limit.total_tokens*rate/1_000_000)
        held={}
        try:
            for category,calls in [('provider',limit.provider_calls),('tool',limit.tool_calls)]:
                if calls:held[category]=ledger.reserve(trial_id=trial_id,category=category,calls=calls,
                    tokens_reserved=limit.total_tokens if category=='provider' else 0,
                    estimated_usd_reserved=cost if category=='provider' else 0)
        except BudgetStop:
            # No RPC was sent: release only reservations made in this block.
            for rid in held.values():ledger.settle(rid,status='RELEASE',actual_calls=0,actual_tokens=0,estimated_usd=0)
            raise
        response=self.rpc('execute',case,trial_id,remaining_seconds=remaining_seconds);receipt.update(response['receipt']);archive=response['archive'];usage=response['usage']
        for category,rid in held.items():
            calls=usage['calls_by_category'][category]
            ledger.settle(rid,status='COMMIT' if calls else 'RELEASE',actual_calls=calls,
                actual_tokens=None if category=='provider' and usage['unknown_tokens'] else usage['tokens'] if category=='provider' else 0,
                estimated_usd=None if category=='provider' and usage['unknown_cost'] else usage['estimated_usd'] if category=='provider' else 0)
        actual_usage=ledger.trial_snapshot(trial_id)
        path=self.artifact_path(trial_id,'.json');write_new(path,archive)
        if raw_hash(path)!=response['archive_sha256']:raise ValueError('returned application archive serialization changed')
        assessment=evaluate(case,archive,actual_usage,allow_known_classifier_rejection=self.env.get('classifier_semantic_failure_policy')=='CASE_FAIL_CONTINUE_WHEN_FULLY_AUDITED');outer=archive['result']
        trial=EvalTrial(trial_id,case.case_id,self.grant.plan.experiment_id,1,(outer['run_id'],),outer['status'],(),tuple(archive['read_invocations']),
            path.relative_to(self.root).as_posix(),{'code':outer['failure_code']} if outer['failure_code'] else None,actual_usage,response['latency_ms'])
        raw={'application_archive':archive,'code_assessment':assessment,'case_sha256':formal_case_digest(case),
            'case_path':self.artifact_path(trial_id,'.case.json').relative_to(self.root).as_posix(),'config_sha256':self.grant.plan.config_sha256,
            'archive_sha256':raw_hash(path),'scoring_runtime_sha256':formal_runtime_digest()}
        return FormalTrialResult(trial,raw,grade_result(trial_id,assessment),assessment['integrity'],tuple(assessment['blocking_items']))
    def cleanup(self,receipt,trial,raw):
        case=EvalCase.from_dict(json.loads((self.root/raw['case_path']).read_text()))
        result=self.rpc('cleanup',case,trial['trial_id'])
        if result['cleaned']:
            path=self.artifact_path(trial['trial_id'],'.cleanup.json')
            if not path.exists():write_new(path,result['proof'])
        return result['cleaned']
    def inspect_failure(self,receipt):
        case=EvalCase.from_dict(json.loads(self.artifact_path(receipt['trial_id'],'.case.json').read_text()))
        return self.rpc('inspect',case,receipt['trial_id'])
    def finish(self,cases,result):
        consumed=set(result['consumed_cases'])
        try:
            if consumed:self.rpc('close',next(c for c in reversed(cases) if c.case_id in consumed),
                f'formal.{self.grant.grant_id}.{next(c.case_id for c in reversed(cases) if c.case_id in consumed)}')
        finally:
            if result['stop_reason'] is not None:self.gate.formal_authority.close(self.grant)
