"""Configuration and evidence operations around the existing signed CLI/Runner.

No model loop,route override or alternate application execution lives here.
"""
from __future__ import annotations
from pathlib import Path
from datetime import datetime,timedelta,timezone
from dataclasses import asdict
import json,hashlib,sys

ROOT=Path(__file__).resolve().parents[2]
TRUST=ROOT/'data/eval/formal-trust'
BASE=ROOT/'data/eval/new-assets-r26/v7'
RESULTS=ROOT/'data/eval/formal-ledgers/r30'
AUTH='M62-PERSONAL-REAL-EVAL-BLIND-WAIVER-20261101'
METHOD='USER_REVIEWED_PERSONAL_SYNTHETIC_NO_BLIND_V1'

def read(p):return json.loads(p.read_text(encoding='utf8'))
def put(p,value):
    from eval.m6_1.formal_assets import write_new
    if p.exists():
        if read(p)!=value:raise ValueError('immutable personal preparation differs:'+p.name)
    else:write_new(p,value)

def prepare(phase,segment):
    import httpx
    from app.m0.config import load_settings
    from eval.m6_1.formal_application import dependency_versions
    from eval.m6_1.contracts import EvalCase,sha256_json
    from eval.m6_1.formal_assets import raw_hash
    from eval.m6_1.registry import DatasetAsset,DatasetRegistry
    from eval.m6_1.grants import FormalExecutionPlan,formal_case_digest,formal_runtime_digest
    from eval.m6_1.container_boundary import runtime_payload
    from eval.m6_1.budget import BudgetLimits
    from eval.m6_1.store import ResultStore
    if phase not in {'dev8','frozen100','m63'} or not segment.isdigit():raise ValueError('exact phase and numeric successor required')
    auth_path=TRUST/('m63-authorization.json' if phase=='m63' else 'personal-authorization.json')
    auth=read(auth_path)
    if auth['event_id']!=AUTH:raise PermissionError('wrong human authorization')
    health=httpx.get('http://application:8080/boundary/health',trust_env=False,timeout=15).json()
    if health['target_identity']!='ca3b0b3d027448a09cc58caa49139862' or health['active_runs'] or health['pending_actions'] or not health['lease_free']:
        raise PermissionError('dedicated target not clean or identity changed')
    if health['runtime']!=formal_runtime_digest():raise PermissionError('deployed runtime differs')
    all_cases={};assets=[]
    for name in ('dev8','frozen100'):
        path=BASE/'candidates'/f'{name}.json';payload=read(path);rel=path.relative_to(ROOT).as_posix();sha=raw_hash(path)
        if not any(b['path']==rel and b['sha256']==sha for b in auth['bound_inputs']):raise PermissionError('unapproved input source')
        all_cases[name]=[EvalCase.from_dict(c) for c in payload['cases']]
        assets.append(DatasetAsset('M62-R30-'+name,'user_reviewed_synthetic_benchmark',payload['version'],None,payload['split'],len(payload['cases']),rel,sha,
            'BATCH_USER_CONFIRMED','CHAT_USER_SELF_CONFIRMED','2026-10-31T15:46:02.487282+00:00',METHOD,'NOT_VERIFIED','NOT_VERIFIED','NOT_SCOREABLE',None,
            'EXACT_SIGNED_PERSONAL_REAL_EVAL_ONLY',('Disclosed self-built synthetic real application evaluation;not independent unseen Frozen','Blind assessment cancelled by owner'),
            historical_source_ref='data/eval/formal-trust/personal-authorization.json'))
    if phase=='m63':
        design=read(TRUST/'m63-design.json')
        path=RESULTS/'trial-cases.json';payload=read(path);rel=path.relative_to(ROOT).as_posix()
        if not any(b['path']==rel and b['sha256']==raw_hash(path) for b in auth['bound_inputs']):raise PermissionError('derived experiment not authorized')
        all_cases['m63']=[EvalCase.from_dict(c) for c in payload['cases']]
        if [c.case_id for c in all_cases['m63']] != [t['case_id'] for t in design['trials']]:raise PermissionError('experiment order drift')
        assets.append(DatasetAsset('M62-R30-m63','user_reviewed_synthetic_benchmark',payload['version'],None,payload['split'],250,rel,raw_hash(path),
            'BATCH_USER_CONFIRMED','CHAT_USER_SELF_CONFIRMED',auth['recorded_at_utc'],METHOD,'NOT_VERIFIED','NOT_VERIFIED','NOT_SCOREABLE',None,
            'EXACT_SIGNED_PERSONAL_REAL_EVAL_ONLY',('Preplanned 250 disclosed synthetic repeated trials;source references confirmed, new outputs not reviewed',),
            historical_source_ref=auth_path.relative_to(ROOT).as_posix()))
    RESULTS.mkdir(parents=True,exist_ok=True);RESULTS.chmod(0o2770)
    ledger_path=(RESULTS/'results.sqlite3').relative_to(ROOT).as_posix();store=ResultStore(ROOT/ledger_path);store.path.chmod(0o660)
    with store.transaction(immediate=False) as db:
        table=db.execute("SELECT 1 FROM sqlite_master WHERE name='personal_task_attempts'").fetchone()
        consumed={r[0] for r in db.execute('SELECT case_id FROM personal_task_attempts WHERE task_ref=?',(AUTH,))} if table else set()
        # Previous segments remain in the same database and count toward54USD.
        used=db.execute("SELECT COALESCE(SUM(estimated_usd),0) FROM budget_events WHERE event_type='COMMIT'").fetchone()[0]
        experiments=[r[0] for r in db.execute('SELECT experiment_id FROM experiments')]
    from eval.m6_1.budget import BudgetLedger
    for experiment in experiments:
        s=BudgetLedger(store,experiment,BudgetLimits(**store.budget_limits(experiment))).snapshot()
        if not s['complete'] or not s['settled'] or s['token_status']=='UNKNOWN_USAGE' or s['estimate_status']=='UNKNOWN_USAGE':
            raise PermissionError('shared task has unsettled or unknown accounting;reconcile first')
    if used>=(50 if phase=='m63' else 54):raise PermissionError('shared task budget exhausted')
    recovery_refs=[]
    proof_path=TRUST/'r30-pre-run-recovery.json'
    if proof_path.exists():
        proof=read(proof_path)
        with store.transaction(immediate=False) as db:
            present=db.execute("SELECT 1 FROM sqlite_master WHERE name='personal_task_recovery_claims'").fetchone()
            used_recovery=db.execute('SELECT 1 FROM personal_task_recovery_claims WHERE previous_attempt_id=?',(proof['previous_attempt_id'],)).fetchone() if present else None
        if not used_recovery and proof['case_id'] in {c.case_id for c in all_cases[phase]}:
            recovery_refs=[(proof['case_id'],proof_path.relative_to(ROOT).as_posix(),raw_hash(proof_path))]
    recoverable={r[0] for r in recovery_refs}
    cases=[c for c in all_cases[phase] if c.case_id not in consumed or c.case_id in recoverable]
    if not cases:raise ValueError('no unconsumed cases in phase;do not replay')
    phase_id=phase+'-s'+segment
    asset=next(a for a in assets if a.asset_id=='M62-R30-'+phase)
    settings=load_settings(require_api_key=False)
    if settings.model!='deepseek-flash' or settings.base_url!='https://api.deepseek.com':raise ValueError('approved provider changed')
    now=datetime.now(timezone.utc)
    pricepath='data/eval/formal-trust/r30-price.json'
    registry=DatasetRegistry(assets).to_dict()
    meta=read(BASE/'case-metadata.json')
    scopes=({k:[c.case_id for c in cases if c.case_id in meta['cohorts'][k]] for k in ('route','retrieval','outcome')} if phase=='frozen100' else
        {'route':[c.case_id for c in cases],'retrieval':[c.case_id for c in cases if c.expected_outcome['required_evidence']], 'outcome':[c.case_id for c in cases]})
    actions=sorted({c.input['owner_interaction']['tool_id'] for c in cases if 'owner_interaction' in c.input})
    env={'mode':'PERSONAL_REAL_EVAL','execution_factory':{'path':'eval/m6_1/formal_application.py','callable':'factory'},
        'application_postgres':read(TRUST/'target-binding.json'),'application_artifacts':(RESULTS/'application').relative_to(ROOT).as_posix(),
        'repository_read_cap_per_case':32,'python_dependencies':dependency_versions(),
        'provider_binding':{'model':settings.model,'endpoint':settings.base_url,'nodes':{k:asdict(v) for k,v in settings.nodes.items()}},
        'price_evidence':{'path':pricepath,'sha256':raw_hash(ROOT/pricepath)},'container_endpoint':'http://application:8080','container_proxy':'http://proxy:7892',
        'issuer_subject':'evalissuer','output_reviewers':['evalcontrol'],'simulated_action_tools':actions,
        'retrieval_evaluation_mode':'FORMAL_FULL_QUERY_BM25','personal_reference_method':METHOD,
        'classifier_semantic_failure_policy':'CASE_FAIL_CONTINUE_WHEN_FULLY_AUDITED'}
    if phase=='m63':
        env.update(trial_architectures={t['case_id']:'SINGLE' if t['variant']=='A' else 'MULTI' for t in design['trials']},
            budget_pool_policy='ACTUAL_STEP_RESERVATIONS_SHARED_COST_V1',retrieval_evaluation_mode='APPLICATION_TRACE_ONLY')
    limits=BudgetLimits(provider_calls=8*len(cases),tool_calls=12*len(cases),total_tokens=120000*len(cases),estimated_usd=50-used if phase=='m63' else .5*len(cases),
        max_calls_per_trial=20,max_duration_seconds=600*len(cases))
    exp={'fixture_version':asset.version,'fixture_sha256':asset.sha256,'grader_version':'formal-code-grading-v3','judge_version':'none',
        'retriever_config':{'mode':'bm25'},'knowledge_index_version':'case-bound-complete-corpus',
        'model_versions':{'application':settings.model},'prompt_versions':{'application':'runtime-bound'},
        'tool_permissions':sorted({t for c in cases for t in c.input['allowed_capabilities']}),'retry_policy':{'max_retries':0},'random_seed':1,
        'environment':env,'budget_limits':limits.to_dict(),'data_version':asset.version,'reporting_cohorts':scopes}
    gov='eval/registry/m6.2-personal-real-eval-governance-r6.json'
    bindings=tuple((p,raw_hash(ROOT/p)) for p in ('eval/m6_1/formal_application.py',auth_path.relative_to(ROOT).as_posix(),gov))
    bindings+=tuple((path,digest) for _,path,digest in recovery_refs)
    plan=FormalExecutionPlan(AUTH+'/'+phase_id,'PERSONAL_REAL_EVAL',asset.asset_id,asset.version,asset.sha256,
        tuple((c.case_id,formal_case_digest(c)) for c in cases),'evalcontrol',sha256_json(exp),'shoprelay-m62-eval-pg',health['target_profile'],health['target_identity'],
        ledger_path,(now-timedelta(minutes=1)).isoformat(),(now+timedelta(days=2)).isoformat(),limits,bindings,formal_runtime_digest(),'R30-'+phase_id,
        tuple((c.case_id,sha256_json(runtime_payload(c))) for c in cases),task_scope_ref=AUTH,pre_run_recoveries=tuple(recovery_refs))
    public=read(TRUST/'r30-public-key.json')['base64']
    cfg={'engineering_only':False,'issuer':'M62_R30_PERSONAL_ISSUER','trusted_issuers':['M62_R30_PERSONAL_ISSUER'],'subject':'evalcontrol',
        'signature_scheme':'ED25519_V1','verification_public_key_base64':public,'signing_key_env':'SHOPRELAY_R30_PERSONAL_SIGNING_KEY',
        'registry_metadata':'data/eval/formal-trust/registry.json','result_store':ledger_path,
        'grant_file':'data/eval/formal-trust/r30-'+phase_id+'-grant.json','actual_target_binding':[plan.target_id,plan.target_profile,plan.target_identity],
        'allowed_targets':[[plan.target_id,plan.target_profile,plan.target_identity]],'approved_plan_hashes':[sha256_json(asdict(plan))],
        'plan':json.loads(json.dumps(asdict(plan))),'experiment_id':plan.experiment_id,'experiment_config':exp,
        'source_revision_label':'LOCKED_PERSONAL_IMAGE_NOT_GIT_HEAD','scoring_version':'formal-code-grading-v3',
        'governance_binding':{'path':gov,'sha256':raw_hash(ROOT/gov)},
        'isolation_evidence_binding':{'path':'data/eval/formal-trust/r30-isolation.json','sha256':raw_hash(TRUST/'r30-isolation.json')}}
    request=RESULTS/('request-'+phase_id+'.json');put(request,{'config':cfg,'registry':registry,**({'authorization':auth} if phase=='m63' else {})})
    print(json.dumps({'phase':phase_id,'unconsumed':len(cases),'already_consumed':len(consumed),'request':request.relative_to(ROOT).as_posix(),
        'runtime':plan.execution_implementation_sha256,'model':settings.model,'usd_cap':limits.estimated_usd}))

def execute(phase_id):
    from eval.m6_1.execution_modes import formal_cli
    from eval.m6_1.formal_assets import write_new
    cfg_path='data/eval/formal-trust/deployment.json';cfg=read(ROOT/cfg_path)
    if cfg['experiment_id']!='R30-'+phase_id or cfg['plan']['mode']!='PERSONAL_REAL_EVAL':raise PermissionError('wrong active personal phase')
    result=formal_cli(['--formal-config',cfg_path,'--execute'],repository_root=ROOT)
    path=RESULTS/('result-'+phase_id+'.json');put(path,result)
    print(json.dumps({'phase':phase_id,'completed':len(result['completed_cases']),'consumed':len(result['consumed_cases']),
        'not_run':len(result['not_run']),'stop_reason':result['stop_reason'],'budget':result['budget']}))

def export():
    from eval.m6_1.store import ResultStore
    from eval.m6_1.budget import BudgetLedger,BudgetLimits
    from eval.m6_1.formal_scoring import recompute
    from eval.m6_1.contracts import EvalCase
    store=ResultStore(RESULTS/'results.sqlite3'); reports=[]
    with store.transaction(immediate=False) as db:
        experiments=[json.loads(r[0]) for r in db.execute('SELECT payload_json FROM experiments')]
    for e in experiments:
        cfg=e['config_snapshot'];trials=store.trials(e['experiment_id']);cases=[]
        for trial in trials:
            raw=store.raw_result(trial['trial_id']);cases.append(EvalCase.from_dict(read(ROOT/raw['case_path'])))
        target=RESULTS/('recomputed-'+e['experiment_id']+'.json')
        if target.exists():
            from eval.m6_1.contracts import sha256_json
            report=read(target)
            if any(row['raw_sha256']!=sha256_json(store.raw_result(row['trial_id'])) for row in report['rows']):raise ValueError('old version raw archive drift')
        else:
            report=recompute(cases,store,e['experiment_id'],scopes=cfg['reporting_cohorts'],repository_root=ROOT)
            put(target,report)
        reports.append({'experiment_id':e['experiment_id'],'trials':len(trials),'binding_sha256':report['binding_sha256'],'budget':report['budget']})
    print(json.dumps({'reports':reports,'formal_unseen_claim':False}))

if __name__=='__main__':
    if sys.argv[1]=='m63':
        AUTH='M63-PERSONAL-CORE-EXECUTION-APPROVED-20261101'
        RESULTS=ROOT/'data/eval/formal-ledgers/m63'
        if sys.argv[2]=='prepare':prepare('m63',sys.argv[3])
        elif sys.argv[2]=='execute':execute('m63-s'+sys.argv[3])
        elif sys.argv[2]=='export':export()
        else:raise ValueError('m63 prepare/execute/export only')
    elif sys.argv[1]=='prepare':prepare(sys.argv[2],sys.argv[3])
    elif sys.argv[1]=='execute':execute(sys.argv[2])
    elif sys.argv[1]=='export':export()
    else:raise ValueError('prepare/execute/export only;signing remains the existing issuer CLI')
