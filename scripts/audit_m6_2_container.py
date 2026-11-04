"""Actual Docker/kernel/canary observations;no candidate content is printed."""
import json,subprocess,sys,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'docs/eval/m6.2-container-candidates-r26-20261031'
COMPOSE=str(ROOT/'deploy/m62-eval/compose.yaml')
def call(*args):return subprocess.check_output(['docker',*args],text=True,cwd=ROOT)
def inside(service,code):return json.loads(call('compose','-f',COMPOSE,'exec','-T',service,'python','-c',code))
def main():
    services={}
    for service in ['application','control','proxy','postgres']:
        cid=call('compose','-f',COMPOSE,'ps','-q',service).strip();raw=json.loads(call('inspect',cid))[0]
        assert raw['Config']['Labels']['com.docker.compose.project']=='shoprelay-m62-eval'
        mounts=[{'type':m['Type'],'name':m.get('Name'),'destination':m['Destination'],'RW':m['RW']} for m in raw['Mounts']]
        assert all(m['type']!='bind' for m in mounts)
        assert not raw['HostConfig']['Privileged'] and not raw['HostConfig']['PortBindings']
        services[service]={'container_id':cid,'image_id':raw['Image'],'user':raw['Config']['User'],'mounts':mounts,
            'readonly_rootfs':raw['HostConfig']['ReadonlyRootfs'],'cap_drop':raw['HostConfig']['CapDrop'],'security_opt':raw['HostConfig']['SecurityOpt'],
            'networks':list(raw['NetworkSettings']['Networks']),
            'configured_credential_names':[x.split('=',1)[0] for x in raw['Config']['Env'] if any(s in x.split('=',1)[0] for s in ['PASSWORD','API_KEY','DATABASE_URL'])]}
    assert not any('assets' in (m['name'] or '') or 'issuer-key' in (m['name'] or '') for m in services['application']['mounts'])
    code="""
import json,os
from pathlib import Path
paths=['/opt/shoprelay/data/eval/new-assets-r26/isolation-canary-gold.txt','/opt/shoprelay/data/eval/new-assets-r26/v3/candidates/frozen100.json','/run/issuer-key/private.key','/var/run/docker.sock','/opt/shoprelay/eval/m6_2/candidate_construction_r26.py']
out={}
for p in paths:
 try:
  with open(p,'rb') as f:f.read(1)
  out[p]='READ_SUCCEEDED_UNEXPECTED'
 except (FileNotFoundError,PermissionError) as e:out[p]=type(e).__name__
from eval.m6_1.governance import verify_container_process
print(json.dumps({'access':out,'actual_identity':verify_container_process(10002),'production_credential_configured':any(k in os.environ for k in ['SHOPRELAY_POSTGRES_PASSWORD','PRODUCTION_DATABASE_URL']),'provider_key_kind':'ENGINEERING_PLACEHOLDER_ONLY_IN_SERVER_PROCESS_NOT_A_REAL_KEY'}))
"""
    access=inside('application',code);assert 'READ_SUCCEEDED_UNEXPECTED' not in access['access'].values()
    control=inside('control',"import json;from pathlib import Path;print(json.dumps({'gold_canary_readable':bool(Path('data/eval/new-assets-r26/isolation-canary-gold.txt').read_bytes()),'private_key_mounted':Path('/run/issuer-key/private.key').exists()}))")
    assert control['gold_canary_readable'] and not control['private_key_mounted']
    proxy=inside('application',"import httpx,json; c=httpx.Client(trust_env=False,timeout=8); r=c.get('http://proxy:7892/__r26_proxy_health'); s=c.request('CONNECT','http://proxy:7892/',headers={'Host':'production.invalid:5432'});print(json.dumps({'health_status':r.status_code,'bridge':r.json(),'unapproved_proxy_request_status':s.status_code}))")
    assert proxy['health_status']==200 and proxy['unapproved_proxy_request_status']==403
    nets={}
    for name in ['control','database','egress']:
        raw=json.loads(call('network','inspect','shoprelay-m62-eval_'+name))[0];nets[name]={'id':raw['Id'],'internal':raw['Internal'],'containers':len(raw['Containers'])}
    assert nets['control']['internal'] and nets['database']['internal']
    # These are the already closed, fictional engineering permit and runtime
    # inputs. Rejection must not create a new Run or touch a candidate question.
    negatives=inside('control',"""
import json,copy,httpx
from pathlib import Path
from eval.m6_1.contracts import EvalCase
from eval.m6_1.container_boundary import runtime_payload
cfg=json.loads(Path('data/eval/formal-trust/deployment.json').read_text())
grant=json.loads(Path(cfg['grant_file']).read_text())
asset=json.loads(Path('data/eval/formal-trust/registry.json').read_text())
from eval.m6_1.registry import DatasetRegistry
entry=DatasetRegistry.from_dict(asset).get(grant['plan']['asset_id'])
case=EvalCase.from_dict(json.loads(Path(entry.source_path).read_text())['cases'][0])
base={'grant':grant,'payload':runtime_payload(case),'trial_id':'formal.'+grant['grant_id']+'.'+case.case_id}
variants={'closed_after_restart':base}
v=copy.deepcopy(base);v['grant']['signature']='invalid';variants['invalid_signature']=v
v=copy.deepcopy(base);v['payload']['input']['user_id']='wrong-engineering-owner';variants['owner_input_changed']=v
v=copy.deepcopy(base);v['grant']['plan']['target_identity']='wrong-marker';variants['target_changed']=v
v=copy.deepcopy(base);v['grant']['plan']['config_sha256']='0'*64;variants['configuration_changed']=v
client=httpx.Client(trust_env=False,timeout=15)
before=client.get('http://application:8080/boundary/health').json()
rows={k:client.post('http://application:8080/boundary/prepare',json=v).status_code for k,v in variants.items()}
after=client.get('http://application:8080/boundary/health').json()
assert all(s==403 for s in rows.values()) and before==after
print(json.dumps({'http_rejections':rows,'health_unchanged':True,'target_identity':after['target_identity'],'active_runs':after['active_runs'],'pending_actions':after['pending_actions'],'lease_free':after['lease_free']}))
""")
    kernel=call('compose','-f',COMPOSE,'exec','-T','--user','0','application','python','-c',"from eval.m6_1.governance import verify_container_process;import json\ntry:verify_container_process(10002);print(json.dumps({'wrong_actual_uid_denied':False}))\nexcept PermissionError:print(json.dumps({'wrong_actual_uid_denied':True}))")
    assert json.loads(kernel)['wrong_actual_uid_denied']
    result={'schema_version':'r26-actual-container-audit-v1','services':services,'application_access':access,'control_access':control,'proxy_bridge':proxy,'networks':nets,
        'actual_application_runtime_sha256':inside('control',"import httpx,json;print(json.dumps(httpx.get('http://application:8080/boundary/health',trust_env=False).json()))")['runtime'],
        'negative_permit_probes':negatives,'wrong_actual_uid':json.loads(kernel),
        'threat_limit':'Host/Docker administrators are trusted;not protection against same-privilege administrator/development tools',
        'windows_accounts_acl_sacl_or_system_audit_changed':False,'existing_business_project_modified':False,'real_model_requests':0}
    out=OUT/'actual-isolation-audit.json'
    if out.exists():raise ValueError('preserve prior audit;select a versioned output')
    out.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps({'services_inspected':len(services),'app_gold_key_access':'DENIED_OR_NOT_MOUNTED','host_proxy_tcp':True,'published_host_ports':0,'formal_quality_or_Gold_approved':False}))
if __name__=='__main__':main()
