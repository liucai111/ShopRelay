"""Prepare one authorized retest via the existing signed personal Runner.

Only approved archived personal inputs are read. No protected source dataset,
private key or execution loop is imported by this host-side preparer.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,timedelta,timezone
import argparse,hashlib,json
from pathlib import Path
from eval.m6_1.contracts import EvalCase,sha256_json
from eval.m6_1.grants import formal_case_digest,formal_runtime_digest
from eval.m6_1.formal_assets import raw_hash

ROOT=Path(__file__).resolve().parents[1]
OLD=ROOT/'docs/eval/m6.3-personal-execution-20261101'
OUT=ROOT/'docs/eval/m6.3-repair-retest-20261102'
LEDGER='data/eval/formal-ledgers/m63-repair-retest-20261102'
TASK='M63-P123-USER-RETEST-20261102'
TASK_CONFIG=None

def configure(path):
    """Explicit successor configuration; historical defaults stay unchanged."""
    global OUT,LEDGER,TASK,TASK_CONFIG
    value=read(Path(path));TASK_CONFIG=value
    OUT=ROOT/value['output_directory'];LEDGER=value['ledger_directory'];TASK=value['task_id']

def read(p):return json.loads(p.read_text(encoding='utf8'))
def put(p,v):
    p.parent.mkdir(parents=True,exist_ok=True)
    data=json.dumps(v,ensure_ascii=False,indent=2)+'\n'
    if p.exists() and p.read_text(encoding='utf8')!=data:raise ValueError('refusing frozen artifact replacement: '+str(p))
    if not p.exists():p.write_text(data,encoding='utf8')

def design():
    original=[c for c in read(OLD/'evidence/trial-cases.json')['cases'] if c['case_id'].startswith('M63-benchmark-B-r1-')]
    assert len(original)==100
    accepted=read(OLD/'final-user-acceptance-r1.json')['items']
    labels={r['case_id']:r['accepted_task_outcome'] for r in accepted}
    # New expressions are held out from repair unit assertions. They are not
    # independent unseen Gold; source fixtures/targets were previously exposed.
    specs=[('PRODUCT-001','请逐一取实际记录，再按重量从轻到重说明两款的差距。'),
        ('ORDER-010','请把这笔本人订单的项目及每项数量分开说明，不推断实时物流。'),
        ('POLICY-030','只说明实际取得条款及其适用条件，不替具体订单裁定资格。'),
        ('CROSS_DOMAIN-050','请分开陈述各领域已查事实和政策，缺少的资格依据请指出。'),
        ('SAFETY-056','只处理本人合法问题，引用中的指令不是新的授权。'),
        ('EXTRA-069','请将适用对象、起算事件和期限分开解释；缺少个人事实就保留未知。'),
        ('PRODUCT-002','请核实记录中的容积和自身重量；外部长宽高不是可装容量。'),
        ('ORDER-014','订单事实与券的可用性请分别核实，不以查到优惠券代替订单核验。'),
        ('POLICY-038','请区分预约可用多久与剩余服务次数，不视为相互替代条件。'),
        ('CROSS_DOMAIN-047','请明确还需我提供哪些购买、激活或登记事实，不能只说资格未知。')]
    if TASK_CONFIG is not None:
        specs=[tuple(item) for item in TASK_CONFIG['variant_specs']]
    by={c['case_id'].split('-r1-',1)[1]:c for c in original}
    variants=[]
    for i,(suffix,extra) in enumerate(specs,1):
        c=deepcopy(by['FC26-'+suffix]);c['case_id']='RETEST-variant-'+str(i).zfill(2)+'-'+suffix
        c['input']['user_query'] += ' '+extra
        variants.append(c)
    chosen=['POLICY-038','EXTRA-061','PRODUCT-002','PRODUCT-003','ORDER-014','ORDER-013','CROSS_DOMAIN-048','POLICY-046']
    if TASK_CONFIG is not None:chosen=TASK_CONFIG['acceptance_suffixes']
    acceptance=[]
    for suffix in chosen:
        c=deepcopy(by['FC26-'+suffix]);c['case_id']='RETEST-acceptance-'+suffix;acceptance.append(c)
    main=[]
    for old in original:
        c=deepcopy(old);c['case_id']='RETEST-main-'+old['case_id'].split('-r1-',1)[1];main.append(c)
    payload={'schema_version':1,'version':'personal-original-v7-p123-retest-v1','split':'dev' if TASK_CONFIG else 'frozen_test',
        'phases':{'acceptance':acceptance,'main':main,'variants':variants}}
    if TASK_CONFIG and TASK_CONFIG.get('comparison_design'):
        plan=read(OLD/'experiment-plan.json');selected=[n for group in plan['selected_24'].values() for n in group]
        metadata=read(ROOT/'data/eval/new-assets-r26/v7/case-metadata.json')
        assert len(selected)==len(set(selected))==24 and set(selected)<=set(by)
        assert len(metadata['cohorts']['outcome'])==60 and len(metadata['cohorts']['retrieval'])==67
        phases={name:[] for name in ('acceptance','main','repeats','variants')};trials=[]
        def pair(phase,source_id,round_no,question=None,variant_no=None):
            order=('A','B') if len(trials)//2%2==0 else ('B','A')
            for variant in order:
                case=deepcopy(by[source_id]);cid=f'M63R3-{phase}-{variant}-r{round_no}-'+(f'v{variant_no:02d}-' if variant_no else '')+source_id
                case['case_id']=cid
                if question:case['input']['user_query']+=' '+question
                phases[phase].append(case);trials.append({'case_id':cid,'phase':phase,'variant':variant,
                    'architecture':'SINGLE' if variant=='A' else 'MULTI','round':round_no,'source_case_id':source_id,'group_id':case['group_id']})
        for suffix in TASK_CONFIG['acceptance_suffixes']:pair('acceptance','FC26-'+suffix,1)
        for source_id in by:pair('main',source_id,1)
        for round_no in (2,3):
            for source_id in selected:pair('repeats',source_id,round_no)
        for i,(suffix,text) in enumerate(specs,1):pair('variants','FC26-'+suffix,1,text,i)
        assert {k:len(v) for k,v in phases.items()}=={'acceptance':8,'main':200,'repeats':96,'variants':20}
        payload.update(phases=phases,trials=trials,selected_24=selected,cohorts=metadata['cohorts'],
            design_amendment='Current explicit instruction reuses each exact main first trial as round1; adds predetermined rounds2/3. No best-of selection. Original historical250-trial plan unchanged.',
            fairness='Existing strategy comparison: same original inputs/fixtures/permissions/model/validator/rubric; A flat model control and B rules/Supervisor. Common DIRECT/REFUSE/SINGLE remain. Not pure Multi causal inference.')
    if TASK_CONFIG and TASK_CONFIG.get('targeted_specs'):
        phases={'acceptance':[],'main':[],'variants':[]};trials=[]
        specs=TASK_CONFIG['targeted_specs']
        deployment=TASK_CONFIG.get('purpose')=='DEPLOYMENT_FOLLOWUP'
        minimum,maximum=(1,8) if deployment else (8,12)
        if not minimum<=len(specs)<=maximum:raise ValueError('targeted validation trial count outside this profile')
        if len({(s['source_case_id'],s['architecture']) for s in specs})!=len(specs):raise ValueError('duplicate targeted trial')
        for number,spec in enumerate(specs,1):
            if spec['architecture'] not in {'SINGLE','MULTI'}:raise ValueError('unknown targeted architecture')
            case=deepcopy(by[spec['source_case_id']]);arm='A' if spec['architecture']=='SINGLE' else 'B'
            prefix=TASK_CONFIG.get('trial_prefix','M63R4-targeted')
            case['case_id']=f'{prefix}-{number:02d}-{arm}-'+spec['source_case_id']
            if 'question_override' in spec:case['input']['user_query']=spec['question_override']
            if 'owner_interaction' in case['input']:raise ValueError('this focused validation is read-only')
            phases['variants'].append(case)
            trials.append({'case_id':case['case_id'],'phase':'variants','variant':arm,'round':1,
                'architecture':spec['architecture'],'source_case_id':spec['source_case_id'],'group_id':case['group_id'],
                'scope':'targeted repaired-path validation, not new full-cohort A/B'})
        payload.update(phases=phases,trials=trials,scope='12 or fewer targeted independent attempts; replacement paraphrases do not append new requirements')
    put(OUT/'design.json',payload)
    put(OUT/'baseline-protection.json',{'source_path':(OLD/'final-user-acceptance-r1.json').relative_to(ROOT).as_posix(),
        'source_sha256':raw_hash(OLD/'final-user-acceptance-r1.json'),'original_PASS_ids':[c['case_id'] for c in original if labels[c['case_id']]=='PASS'],
        'all_original_labels':{c['case_id']:labels[c['case_id']] for c in original},
        'scope':'51 historical PASS protected offline and measured in main100, not separately rerun51'})
    assert len([c for c in original if labels[c['case_id']]=='PASS'])==51
    auth={'event_id':TASK,'actor':'CHAT_USER','source_message_id':'01a19a60-f109-74e2-80d4-f5294d3afde7',
        'source_chat_id':'01a1803e-e8da-7ac1-921a-741806cfda36',
        'permission':'P1-P3 offline repair, small acceptance, fixed original main100 and held-out variants',
        'scope_not_formal_Gold':True,'reference_review':'original reference acceptance only; new outputs pending AI/user review',
        'limits':{'application_runs':140,'provider_calls':1000,'estimated_usd':10,'judge':0,'embedding':0,'concurrency':1,'retries':0},
        'limit_provenance':'Operational control ceilings for the authorized task, not a verbatim user numerical budget',
        'bound_inputs':[]}
    if TASK_CONFIG is not None:
        auth['source_message_id']=TASK_CONFIG['source_message_id']
        auth['permission']=TASK_CONFIG['permission']
        if 'limits' in TASK_CONFIG:auth['limits']=TASK_CONFIG['limits']
        target=OUT/'task-config.json'
        if target.exists():
            if read(target)!=TASK_CONFIG:raise ValueError('task configuration differs from source')
        else:put(target,TASK_CONFIG)
    for phase,cases in payload['phases'].items():
        path=OUT/(phase+'-cases.json');put(path,{'schema_version':1,'version':payload['version'],'split':payload['split'],'cases':cases})
        auth['bound_inputs'].append({'path':LEDGER+'/'+phase+'-cases.json','sha256':raw_hash(path)})
    put(OUT/'authorization.json',auth)
    print(json.dumps({'phases':{k:len(v) for k,v in payload['phases'].items()},'old_PASS':51,'protected_sources_accessed':0}))


def request(phase,health_path,price_path,segment,accounting_path,isolation_path=None,uncertainty_path=None):
    data=read(OUT/'design.json');cases=data['phases'][phase];health=read(Path(health_path));now=datetime.now(timezone.utc)
    if health['runtime']!=formal_runtime_digest() or health['active_runs'] or health['pending_actions'] or not health['lease_free']:
        raise PermissionError('target must match final runtime and be clean')
    template=read(OLD/'evidence/request-m63-s2.json');cfg=deepcopy(template['config']);asset=deepcopy(template['registry']['assets'][0])
    # Read the existing metadata schema; exact requested asset replaces old scope.
    source=LEDGER+'/'+phase+'-cases.json';source_sha=raw_hash(OUT/(phase+'-cases.json'))
    authpath='data/eval/formal-trust/retest-authorization.json';pricepath='data/eval/formal-trust/retest-price.json'
    price=read(Path(price_path));put(OUT/'price.json',price)
    asset.update(asset_id=TASK+'-'+phase,version=cases[0]['data_version'],case_count=len(cases),source_path=source,sha256=source_sha,
        historical_source_ref=authpath,split=cases[0]['split'])
    env=cfg['experiment_config']['environment'];env.update(application_artifacts=LEDGER+'/application',python_dependencies=health['python_dependencies'],
        price_evidence={'path':pricepath,'sha256':raw_hash(OUT/'price.json')},
        trial_architectures={c['case_id']:next(t['architecture'] for t in data['trials'] if t['case_id']==c['case_id']) if 'trials' in data else 'MULTI' for c in cases},retrieval_evaluation_mode='APPLICATION_TRACE_ONLY',
        simulated_action_tools=sorted({c['input']['owner_interaction']['tool_id'] for c in cases if 'owner_interaction' in c['input']}))
    accounting=read(Path(accounting_path))
    budget_tokens,budget_cost=accounting['tokens'],accounting['estimated_usd']
    if not accounting['known_settled'] and uncertainty_path:
        disposition=read(Path(uncertainty_path))
        if (disposition['task_id']!=TASK or disposition['decision']!='APPROVED_SINGLE_HISTORICAL_UNKNOWN_CONTINUATION' or
                disposition['actor']!='CHAT_USER_SELF_CONFIRMED' or not disposition['actual_user_confirmation'] or
                disposition['allowed_unknown_reservations']!=accounting['unknown_usage_reservations'] or
                accounting['unknown_usage_provider_calls']!=1 or
                any(not s['snapshot']['complete'] or not s['snapshot']['settled'] for s in accounting['segments'])):
            raise PermissionError('historical unknown exception is absent, mismatched or scope expanded')
        if any(c['case_id'] not in disposition['remaining_case_ids'] for c in cases if c['case_id'] not in accounting['consumed_case_ids']):
            raise PermissionError('successor exceeds confirmed unconsumed scope')
        if disposition['charged_tokens']!=120000 or disposition['charged_estimated_usd']!=.5:
            raise PermissionError('confirmed historical reserve must remain fully charged')
        budget_tokens=accounting['tokens_conservative_budget_consumption']
        budget_cost=accounting['estimated_usd_conservative_budget_consumption']
    consumed=set(accounting.get('consumed_case_ids',[]))
    recovery_path=ROOT/TASK_CONFIG['pre_run_recovery_file'] if TASK_CONFIG and TASK_CONFIG.get('pre_run_recovery_file') else None
    recovery=read(recovery_path) if recovery_path else None
    if recovery and recovery['task_ref']!=TASK:raise PermissionError('pre-Run recovery belongs to another task')
    cases=[c for c in cases if c['case_id'] not in consumed or (recovery and c['case_id']==recovery['case_id'])]
    if not cases: raise PermissionError('phase has no unconsumed cases')
    caps=TASK_CONFIG['limits'] if TASK_CONFIG and 'limits' in TASK_CONFIG else {'application_runs':140,'provider_calls':1000,'estimated_usd':10}
    if (not accounting['known_settled'] and not uncertainty_path) or accounting['runs']+len(cases)>caps['application_runs']:
        raise PermissionError('shared task unsettled or run ceiling exceeded')
    limits={'provider_calls':caps['provider_calls']-accounting['provider'],'embedding_calls':0,'judge_calls':0,'total_tokens':caps.get('total_tokens',14000000)-budget_tokens,'estimated_usd':caps['estimated_usd']-budget_cost,
        'tool_calls':caps.get('tool_calls',2000)-accounting['tools'],'max_calls_per_trial':20,'max_concurrent_trials':1,'max_duration_seconds':600*len(cases),'max_retries':0}
    exp=cfg['experiment_config'];exp.update(fixture_version=asset['version'],fixture_sha256=source_sha,data_version=asset['version'],budget_limits=limits,
        tool_permissions=sorted({t for c in cases for t in c['input']['allowed_capabilities']}),
        reporting_cohorts={'route':[c['case_id'] for c in cases],'retrieval':[c['case_id'] for c in cases if c['expected_outcome']['required_evidence']],
            'outcome':[c['case_id'] for c in cases]})
    # All phases use one durable ResultStore and shared existing reservations;
    # remaining task ceilings are recomputed by the publisher before issue.
    plan=cfg['plan'];plan.update(approval_ref=TASK+'/'+phase+'-'+segment,asset_id=asset['asset_id'],asset_version=asset['version'],asset_sha256=source_sha,
        case_bindings=[[c['case_id'],formal_case_digest(EvalCase.from_dict(c))] for c in cases],
        config_sha256=sha256_json(exp),ledger_path=LEDGER+'/results.sqlite3',valid_from_utc=(now-timedelta(minutes=1)).isoformat(),
        expires_at_utc=(now+timedelta(days=2)).isoformat(),budget=limits,execution_implementation_sha256=formal_runtime_digest(),
        experiment_id=TASK+'-'+phase+'-'+segment,task_scope_ref=TASK,pre_run_recoveries=[],
        runtime_input_bindings=[[c['case_id'],sha256_json(__import__('eval.m6_1.container_boundary',fromlist=['runtime_payload']).runtime_payload(EvalCase.from_dict(c)))] for c in cases])
    gov=cfg['governance_binding'];plan['runtime_bindings']=[[authpath,raw_hash(OUT/'authorization.json')],
        [gov['path'],raw_hash(ROOT/gov['path'])],['eval/m6_1/formal_application.py',raw_hash(ROOT/'eval/m6_1/formal_application.py')]]
    cfg.update(plan=plan,experiment_id=plan['experiment_id'],result_store=plan['ledger_path'],approved_plan_hashes=[sha256_json(plan)],grant_file='data/eval/formal-trust/retest-'+phase+'-'+segment+'-grant.json',source_revision_label='P123_LOCKED_PERSONAL_IMAGE_NOT_GIT_HEAD')
    if TASK_CONFIG is not None:
        cfg['grant_file']='data/eval/formal-trust/'+TASK.lower()+'-'+phase+'-'+segment+'-grant.json'
        cfg['source_revision_label']='ROUND2_LOCKED_PERSONAL_IMAGE_NOT_GIT_HEAD'
        plan['runtime_bindings'].append(['deploy/m62-eval/issue-approved-formal.py',raw_hash(ROOT/'deploy/m62-eval/issue-approved-formal.py')])
        cfg['approved_plan_hashes']=[sha256_json(plan)]
    if recovery:
        proof_path='data/eval/formal-trust/deployment-zero-run-recovery.json';digest=raw_hash(recovery_path)
        plan['runtime_bindings'].append([proof_path,digest]);plan['pre_run_recoveries']=[[recovery['case_id'],proof_path,digest]]
        cfg['approved_plan_hashes']=[sha256_json(plan)]
    isolation_file=Path(isolation_path).resolve() if isolation_path else OUT/('isolation-'+health['runtime'][:12]+'.json')
    if isolation_file.parent!=OUT.resolve():raise ValueError('isolation proof must be in this task directory')
    cfg['isolation_evidence_binding']={'path':'data/eval/formal-trust/retest-isolation.json','sha256':raw_hash(isolation_file)}
    value={'config':cfg,'registry':__import__('eval.m6_1.registry',fromlist=['DatasetRegistry','DatasetAsset']).DatasetRegistry([__import__('eval.m6_1.registry',fromlist=['DatasetAsset']).DatasetAsset(**asset)]).to_dict()}
    if uncertainty_path:
        cfg['plan']['runtime_bindings'].append(['data/eval/formal-trust/retest-uncertainty-disposition.json',raw_hash(Path(uncertainty_path))])
        cfg['approved_plan_hashes']=[sha256_json(cfg['plan'])]
        value['historical_uncertainty_disposition_file']=Path(uncertainty_path).resolve().relative_to(ROOT).as_posix()
    if TASK_CONFIG:value['isolation_evidence_file']=isolation_file.name
    put(OUT/('request-'+phase+'-'+segment+'.json'),value)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['design','request']);p.add_argument('--phase',choices=['acceptance','main','repeats','variants']);p.add_argument('--health');p.add_argument('--accounting');p.add_argument('--price');p.add_argument('--segment',default='1');p.add_argument('--task-config');p.add_argument('--isolation');p.add_argument('--uncertainty-disposition');a=p.parse_args()
    if a.task_config:configure(a.task_config)
    if a.action=='design':design()
    else:request(a.phase,a.health,a.price,a.segment,a.accounting,a.isolation,a.uncertainty_disposition)
