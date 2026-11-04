"""Trusted deployment/export operations for the existing signed personal CLI."""
import argparse,json,sys
from pathlib import Path
from datetime import datetime,timedelta,timezone
from scripts import manage_m6_2_personal_real as ops
from scripts.prepare_m63_retest import OUT,LEDGER,ROOT,read,put,TASK

COMPOSE=['compose','-f','deploy/m62-eval/compose.yaml','-f','deploy/m62-eval/personal-real-r30.yaml','-f','deploy/m62-eval/personal-retest.yaml']

def configure(path):
    global OUT,LEDGER,TASK,COMPOSE
    from scripts import prepare_m63_retest as prep
    prep.configure(path);OUT,LEDGER,TASK=prep.OUT,prep.LEDGER,prep.TASK
    COMPOSE=['compose','-f','deploy/m62-eval/compose.yaml','-f','deploy/m62-eval/personal-real-r30.yaml','-f',prep.TASK_CONFIG['compose_overlay']]
def docker(*args,**kwargs):return ops.docker(*args,**kwargs)
def control(code):return json.loads(docker(*COMPOSE,'exec','-T','control','python','-c',code))


def classify_pretool_model_failure(raw:dict,trial:dict,cleanup:dict|None=None)->dict|None:
    """Resolve a known pre-tool response rejection without altering frozen scores.

    The frozen grader leaves unlisted failure stages UNKNOWN. A complete
    positive lifecycle plus the responsible parser's code can establish that
    this specific attempt safely failed before any tool. Missing or conflicting
    audit/usage never receives this operational continuation classification.
    """
    archive=raw['application_archive'];outer=archive['result'];usage=trial['usage']
    events=outer['observations']['run_events'];runs=outer['observations']['runs']
    if (outer['failure_code']!='STRUCTURED_NODE_INVALID_RESPONSE' or outer['status']!='FAILED' or
            raw['execution_blocking_items']!=['FAILED_STAGE_NOT_CLASSIFIED'] or
            raw['execution_integrity']['permission']!='PASS' or
            not usage['complete'] or not usage['settled'] or usage['active_reservations'] or
            usage['unknown_tokens'] or usage['unknown_cost'] or
            usage['calls_by_category']['provider']!=1 or usage['calls_by_category']['tool']!=0 or
            archive['read_invocations'] or len(runs)!=1 or runs[0]['status']!='FAILED' or
            runs[0]['run_id']!=outer['run_id'] or trial['run_ids']!=[outer['run_id']] or
            archive['runtime_binding']['approved']!=archive['runtime_binding']['observed_at_end']):
        return None
    kinds=[e['event_type'] for e in events]
    if kinds!=['route_decided','agent_started','error'] or any(e['run_id']!=outer['run_id'] for e in events):return None
    if (json.loads(events[1]['payload_json'])!={'agent':'single_controller'} or
            json.loads(events[2]['payload_json'])['code']!='STRUCTURED_NODE_INVALID_RESPONSE'):return None
    return {'category':'KNOWN_PRETOOL_MODEL_RESPONSE_CONTRACT_FAILURE','task_outcome':'FAIL',
        'operational_continue':bool(cleanup and cleanup['residual_case_rows']==0 and cleanup['lease_released'] and cleanup['pending_actions']==0),
        'evidence':'Positive same-Run route/controller/error lifecycle; one known settled Provider call; explicit zero tool ledger and zero starts/reads; terminal failure and matching runtime. Actual exact cleanup proof is separately mandatory.',
        'original_checker_unchanged':True,'candidate_text':'NOT_CAPTURED; no fabricated response/finish_reason',
        'qualification':'Code identifies response JSON/finish contract rejection, not a proved network failure or a recoverable answer; preserve failed Run, no retry.'}

def isolation():
    services={}
    for name in ('application','control','proxy','postgres'):
        cid=docker('compose','-f','deploy/m62-eval/compose.yaml','ps','-q',name).strip()
        v=json.loads(docker('inspect',cid))[0]
        mounts=[{'name':m.get('Name'),'destination':m['Destination'],'rw':m['RW'],'type':m['Type']} for m in v['Mounts']]
        if name=='application':
            assert not any(m['type']=='bind' or any(x in (m['name'] or '') for x in ('assets','issuer-key','control-results')) for m in mounts)
        services[name]={'container_id':cid,'image_id':v['Image'],'user':v['Config']['User'],'mounts':mounts,
            'readonly_rootfs':v['HostConfig']['ReadonlyRootfs'],'cap_drop':v['HostConfig']['CapDrop']}
    code="import json;from pathlib import Path;from eval.m6_1.governance import verify_container_process;print(json.dumps({'access':{'control_assets':str(Path("+repr(LEDGER+'/main-cases.json')+").exists()),'issuer_private_key':str(Path('/run/formal-issuer-key/private.key').exists())},'identity':verify_container_process(10002)}))"
    access=json.loads(docker(*COMPOSE,'exec','-T','application','python','-c',code))
    assert set(access['access'].values())=={'False'}
    health=control("import httpx,json;print(json.dumps(httpx.get('http://application:8080/boundary/health',trust_env=False).json()))")
    versions=json.loads(docker(*COMPOSE,'exec','-T','application','python','-c',"import json;from eval.m6_1.formal_application import dependency_versions;print(json.dumps(dependency_versions()))"))
    control_versions=control("import json;from eval.m6_1.formal_application import dependency_versions;print(json.dumps(dependency_versions()))")
    assert versions==control_versions
    health['python_dependencies']=versions
    networks={n:{'internal':json.loads(docker('network','inspect','shoprelay-m62-eval_'+n))[0]['Internal']} for n in ('control','database')}
    proof={'services':services,'application_access':access,'control_access':{'private_key_mounted':False},'networks':networks,
        'actual_application_runtime_sha256':health['runtime'],'health':health,
        'scope':'Dedicated existing personal synthetic evaluation; trusted host/Docker administrator boundary; no unseen-Gold claim'}
    from eval.m6_1.contracts import sha256_json
    name='isolation-'+health['runtime'][:12]+('-'+sha256_json(proof)[:10] if TASK!='M63-P123-USER-RETEST-20261102' else '')+'.json'
    health_name='health-'+health['runtime'][:12]+'.json'
    if (OUT/health_name).exists() and read(OUT/health_name)!=health:
        health_name='health-'+health['runtime'][:12]+'-'+sha256_json(health)[:10]+'.json'
    put(OUT/name,proof);put(OUT/health_name,health)
    print(json.dumps({'actual_runtime':health['runtime'],'isolation_file':name,'health_file':health_name,'active_runs':health['active_runs'],'lease_free':health['lease_free'],'provider_calls':0}))


def accounting():
    code=f'''import json
from pathlib import Path
from eval.m6_1.store import ResultStore
from eval.m6_1.budget import BudgetLedger,BudgetLimits
p=Path({(LEDGER+'/results.sqlite3')!r})
r={{'runs':0,'provider':0,'tools':0,'tokens':0,'estimated_usd':0,'known_settled':True,'segments':[],'consumed_case_ids':[]}}
if p.exists():
 s=ResultStore(p)
 with s.transaction(immediate=False) as db:
  exps=[v[0] for v in db.execute('SELECT experiment_id FROM experiments')]
  if db.execute("SELECT 1 FROM sqlite_master WHERE name='personal_task_attempts'").fetchone():r['consumed_case_ids']=[v[0] for v in db.execute('SELECT case_id FROM personal_task_attempts')]
 for e in exps:
  snap=BudgetLedger(s,e,BudgetLimits(**s.budget_limits(e))).snapshot()
  r['runs']+=len(s.trials(e));r['provider']+=snap['calls_by_category']['provider'];r['tools']+=snap['calls_by_category']['tool']
  r['tokens']=None if r['tokens'] is None or snap['tokens'] is None else r['tokens']+snap['tokens']
  r['estimated_usd']=None if r['estimated_usd'] is None or snap['estimated_usd'] is None else r['estimated_usd']+snap['estimated_usd']
  r['known_settled']=r['known_settled'] and snap['complete'] and snap['settled'] and snap['token_status']!='UNKNOWN_USAGE' and snap['estimate_status']!='UNKNOWN_USAGE'
  r['segments'].append({{'experiment_id':e,'snapshot':snap}})
 if not r['known_settled']:
  with s.transaction(immediate=False) as db:
   known=db.execute("SELECT coalesce(sum(tokens),0),coalesce(sum(estimated_usd),0),sum(case when category='provider' and (tokens is null or estimated_usd is null) then calls else 0 end) FROM budget_events WHERE event_type='COMMIT'").fetchone()
  r['known_recorded_tokens'],r['known_recorded_estimated_usd'],r['unknown_usage_provider_calls']=known
  with s.transaction(immediate=False) as db:
   r['unknown_usage_reservations']=[{{'reservation_id':row[0],'trial_id':json.loads(row[1])['trial_id']}} for row in db.execute("SELECT c.reservation_id,r.payload_json FROM budget_events c JOIN budget_events r ON r.reservation_id=c.reservation_id AND r.event_type='RESERVE' WHERE c.event_type='COMMIT' AND c.category='provider' AND (c.tokens is null OR c.estimated_usd is null)")]
  r['tokens_conservative_budget_consumption']=sum(v['snapshot']['tokens_conservative_budget_consumption'] for v in r['segments'])
  r['estimated_usd_conservative_budget_consumption']=sum(v['snapshot']['estimated_usd_conservative_budget_consumption'] for v in r['segments'])
print(json.dumps(r))'''
    value=control(code)
    path=OUT/('accounting-'+str(value['runs'])+'.json')
    if path.exists() and read(path)!=value:
        from eval.m6_1.contracts import sha256_json
        path=OUT/('accounting-'+str(value['runs'])+'-'+sha256_json(value)[:10]+'.json')
    put(path,value)
    print(json.dumps({'path':str(path.relative_to(ROOT)),**value}))


def export_evidence():
    """Consistent read-only ledger backup plus this task's immutable artifacts."""
    import base64,hashlib,io,tarfile
    code=f'''import base64,io,json,sqlite3,tarfile,tempfile
from pathlib import Path
root=Path({LEDGER!r}).resolve()
with tempfile.TemporaryDirectory(prefix='ledger-export-') as directory:
 snapshot=Path(directory)/'results.sqlite3'
 source=sqlite3.connect('file:'+str(root/'results.sqlite3')+'?mode=ro',uri=True)
 target=sqlite3.connect(snapshot);source.backup(target);target.close();source.close()
 stream=io.BytesIO()
 with tarfile.open(fileobj=stream,mode='w:gz') as archive:
  for p in sorted(root.rglob('*')):
   if not p.is_file() or p.name in {{'results.sqlite3','results.sqlite3-wal','results.sqlite3-shm'}}:continue
   if not p.resolve().is_relative_to(root):raise ValueError('export path outside exact task ledger')
   archive.add(p,arcname=p.relative_to(root).as_posix(),recursive=False)
  archive.add(snapshot,arcname='results.sqlite3')
 print(json.dumps({{'archive_base64':base64.b64encode(stream.getvalue()).decode()}}))'''
    value=control(code);blob=base64.b64decode(value['archive_base64']);files=[]
    with tarfile.open(fileobj=io.BytesIO(blob),mode='r:gz') as archive:
        for member in archive.getmembers():
            relative=Path(member.name)
            if relative.is_absolute() or '..' in relative.parts or not member.isfile():raise ValueError('invalid task archive member')
            target=OUT/'evidence'/relative;data=archive.extractfile(member).read()
            if target.exists() and target.read_bytes()!=data:
                if relative.as_posix()!='results.sqlite3':raise ValueError('immutable task artifact changed: '+member.name)
                saved=OUT/'ledger-snapshots'/(hashlib.sha256(target.read_bytes()).hexdigest()+'.sqlite3')
                saved.parent.mkdir(exist_ok=True)
                if not saved.exists():saved.write_bytes(target.read_bytes())
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
            files.append({'path':member.name,'sha256':hashlib.sha256(data).hexdigest()})
    put(OUT/('export-'+hashlib.sha256(json.dumps(files,sort_keys=True).encode()).hexdigest()[:12]+'.json'),{'files':files})
    print(json.dumps({'exported_files':len(files),'source_task':TASK,'model_calls':0}))


def closure(revision=None):
    """Read actual task grant events on both sides, never infer closure from exit code."""
    code=f'''import json,sqlite3
from pathlib import Path
d=sqlite3.connect('file:'+{(LEDGER+'/results.sqlite3')!r}+'?mode=ro',uri=True)
permits=[]
for gid,payload in d.execute('select grant_id,payload_json from formal_grants'):
 g=json.loads(payload)
 if g['plan']['task_scope_ref']!={TASK!r}:raise ValueError('unexpected task ledger scope')
 counts=dict(d.execute('select event_type,count(*) from formal_grant_events where grant_id=? group by event_type',(gid,)))
 permits.append({{'grant_id':gid,'closed':counts.get('CLOSED',0)>0,'claimed':counts.get('CLAIMED',0),'settled':counts.get('SETTLED',0)}})
print(json.dumps({{'permits':permits,'mode':json.loads(Path('data/eval/formal-trust/mode.json').read_text())}}))'''
    controller=control(code)
    application=json.loads(docker(*COMPOSE,'exec','-T','application','python','-c',code))
    demo=json.loads(docker('inspect','shoprelay-m62-eval-delivery-1'))[0]['Config']['Image']
    value={'controller':controller,'application':application,'customer_demo_image':demo,'new_runs':0,'new_provider_calls':0}
    health=control("import json,httpx;print(json.dumps(httpx.get('http://application:8080/boundary/health',trust_env=False).json()))")
    suffix='-'+revision if revision else ''
    put(OUT/('final-permit-closure'+suffix+'.json'),value);put(OUT/('post-execution-health'+suffix+'.json'),health)
    print(json.dumps({'grants':len(controller['permits']),'all_closed':all(g['closed'] for v in (controller,application) for g in v['permits']),
        'active_runs':health['active_runs'],'pending_actions':health['pending_actions'],'lease_free':health['lease_free'],'model_gate':health['real_provider_activated']}))


def publish(request):
    req=read(Path(request));cfg=req['config'];phase=cfg['experiment_id']
    if cfg['plan']['ledger_path']!=LEDGER+'/results.sqlite3':raise PermissionError('publisher task ledger differs from signed plan')
    # New artifacts are controller-only. The app gets just permit metadata,
    # user authorization, price and isolation receipts, never expected answers.
    import base64
    names=['authorization.json','acceptance-cases.json','main-cases.json','variants-cases.json','price.json']
    if (OUT/'repeats-cases.json').exists():names.append('repeats-cases.json')
    files={n:base64.b64encode((OUT/n).read_bytes()).decode() for n in names}
    code='''import json,sys,os,base64,hashlib
from pathlib import Path
x=json.load(sys.stdin);root=Path('/results/m63-repair-retest-20261102');root.mkdir(exist_ok=True)
for name,encoded in x['files'].items():
 p=root/name;data=base64.b64decode(encoded)
 if p.exists() and p.read_bytes()!=data:
  # Preserve the pre-run newline-serialization error; no Run was claimed.
  previous=p.with_name(p.name+'.pre-run-newline-'+hashlib.sha256(p.read_bytes()).hexdigest()[:12])
  if previous.exists():raise ValueError('unreconciled existing byte mismatch')
  p.rename(previous)
 if not p.exists():p.write_bytes(data)
 os.chown(p,0,0)
 os.chmod(p,0o440)
 os.chown(p,10001,10001)
app=root/'application';app.mkdir(exist_ok=True);os.chown(app,0,0);os.chmod(app,0o2770);os.chown(app,10001,10001)
os.chown(root,0,0);os.chmod(root,0o2770);os.chown(root,10001,10001)
print(json.dumps({'controller_only_files':list(x['files'])}))'''
    code=code.replace('/results/m63-repair-retest-20261102','/results/'+Path(LEDGER).name)
    print(ops.admin(code,['shoprelay-m62-eval_control-results:/results'],{'files':files}).strip())
    control("import json,os;os.umask(0o007);from pathlib import Path;from eval.m6_1.store import ResultStore;p=Path("+repr(LEDGER+'/results.sqlite3')+");ResultStore(p);os.chmod(p,0o660);print(json.dumps({'ledger_initialized':True}))")
    isolation_file=req.get('isolation_evidence_file','isolation-'+cfg['plan']['execution_implementation_sha256'][:12]+'.json')
    public={'deployment.json':cfg,'registry.json':req['registry'],
        'retest-authorization.json':read(OUT/'authorization.json'),'retest-price.json':read(OUT/'price.json'),
        'retest-isolation.json':read(OUT/isolation_file),'mode.json':{'engineering_only':False,'real_provider_allowed':False}}
    import base64
    raw={name:base64.b64encode((OUT/local).read_bytes()).decode() for name,local in
         [('retest-authorization.json','authorization.json'),('retest-price.json','price.json'),('retest-isolation.json',isolation_file)]}
    if 'historical_uncertainty_disposition_file' in req:
        name='retest-uncertainty-disposition.json'
        local=Path(req['historical_uncertainty_disposition_file'])
        if not local.is_absolute():local=ROOT/local
        public[name]=read(local);raw[name]=base64.b64encode(local.read_bytes()).decode()
    ops.write_trust(public,phase,raw)


def execute(phase):
    # Existing Runner owns all model calls, receipts, claims and cleanup.
    code=f'''import json,os
os.umask(0o007)
from pathlib import Path
from eval.m6_1.execution_modes import formal_cli
cfg=json.loads(Path('data/eval/formal-trust/deployment.json').read_text())
if cfg['plan']['task_scope_ref']!={TASK!r} or '-{phase}-' not in cfg['experiment_id']:raise PermissionError('wrong task/phase')
r=formal_cli(['--formal-config','data/eval/formal-trust/deployment.json','--execute'],repository_root=Path('/opt/shoprelay'))
p=Path({LEDGER!r})/('result-'+cfg['experiment_id']+'.json')
p.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\\n')
print(json.dumps({{'completed':len(r['completed_cases']),'consumed':len(r['consumed_cases']),'not_run':len(r['not_run']),'stop_reason':r['stop_reason'],'budget':r['budget']}}))'''
    print(docker(*COMPOSE,'exec','-T','control','python','-c',code))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=['isolation','accounting','publish','execute','export','closure']);p.add_argument('--request');p.add_argument('--phase');p.add_argument('--task-config');p.add_argument('--revision',choices=['continuation-r1']);a=p.parse_args()
    if a.task_config:configure(a.task_config)
    if a.action=='isolation':isolation()
    elif a.action=='accounting':accounting()
    elif a.action=='publish':publish(a.request)
    elif a.action=='export':export_evidence()
    elif a.action=='closure':closure(a.revision)
    else:execute(a.phase)
