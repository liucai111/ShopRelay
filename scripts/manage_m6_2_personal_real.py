"""Trusted operations for the already authorized dedicated personal eval project.

Secrets travel only over stdin into separate Docker volumes;never repo/arguments.
Execution itself remains scripts/run_m6_1_b_synthetic.py and its existing Runner.
"""
import argparse,base64,hashlib,json,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
OUT=ROOT/'docs/eval/m6.2-personal-real-evaluation-r30-20261101'
PROVIDER='shoprelay-m62-personal-r30-provider-secret'
ISSUER='shoprelay-m62-personal-r30-issuer-key'
TRUST='shoprelay-m62-eval_public-trust'
IMAGE='shoprelay-m62-eval-app:r29-final'

def docker(*args,input=None):
    p=subprocess.run(['docker',*args],cwd=ROOT,input=input,text=True,capture_output=True)
    if p.returncode:raise RuntimeError('Docker operation failed: '+p.stderr[-2500:])
    return p.stdout

def admin(code,volumes,payload):
    args=['run','--rm','-i','--network','none','--user','0:0','--cap-drop','ALL','--cap-add','CHOWN','--cap-add','DAC_OVERRIDE','--security-opt','no-new-privileges:true']
    for mount in volumes:args+=['-v',mount]
    return docker(*args,IMAGE,'-c',code,input=json.dumps(payload))

def provision():
    from app.m0.config import load_settings
    settings=load_settings(require_api_key=True)
    if settings.model!='deepseek-flash' or settings.base_url!='https://api.deepseek.com':raise ValueError('approved provider changed')
    for name in (PROVIDER,ISSUER):docker('volume','create','--label','com.shoprelay.purpose=r30-user-authorized-personal-real-evaluation',name)
    code='''
import sys,json,os,base64,hashlib
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,PublicFormat,NoEncryption
data=json.load(sys.stdin);p=Path('/provider/key')
if p.exists():
 if p.read_text()!=data['key']:raise ValueError('existing dedicated provider credential differs')
else:p.write_text(data['key'])
os.chown(p,0,0);p.chmod(0o400);os.chown(p,10002,10002);os.chown('/provider',0,0);Path('/provider').chmod(0o700);os.chown('/provider',10002,10002)
p=Path('/issuer/private.key')
if not p.exists():p.write_bytes(Ed25519PrivateKey.generate().private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption()))
key=Ed25519PrivateKey.from_private_bytes(p.read_bytes());public=key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)
os.chown(p,0,0);p.chmod(0o400);os.chown(p,10003,10003);os.chown('/issuer',0,0);Path('/issuer').chmod(0o700);os.chown('/issuer',10003,10003)
record={'base64':base64.b64encode(public).decode(),'sha256':hashlib.sha256(public).hexdigest(),'issuer':'M62_R30_PERSONAL_ISSUER','scope':'Personal synthetic evaluation only;not production or Gold approval'}
p=Path('/trust/r30-public-key.json')
if p.exists():
 if json.loads(p.read_text())!=record:raise ValueError('personal public key drift')
else:p.write_text(json.dumps(record,indent=2)+'\\n')
os.chown(p,0,0);p.chmod(0o444);os.chown(p,10003,10001)
print(json.dumps({'provider_secret_supplied':True,'issuer_private_key_separated':True,'public_fingerprint':record['sha256'],'private_key_printed':False}))
'''
    result=json.loads(admin(code,[PROVIDER+':/provider',ISSUER+':/issuer',TRUST+':/trust'],{'key':settings.api_key}))
    (OUT/'secret-supply-receipt.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf8')
    print(json.dumps(result))

def publish(request):
    req=json.loads(Path(request).read_text(encoding='utf8'));cfg=req['config']
    if cfg['plan']['mode']!='PERSONAL_REAL_EVAL' or cfg['issuer']!='M62_R30_PERSONAL_ISSUER':raise PermissionError('wrong personal configuration')
    payload={'deployment.json':cfg,'registry.json':req['registry'],'mode.json':{'engineering_only':False,'real_provider_allowed':False},
        'personal-authorization.json':json.loads((ROOT/'docs/eval/m6.2-personal-real-eval-authorization-20261101/user-decision.json').read_text(encoding='utf8')),
        'r30-price.json':json.loads((OUT/'price-evidence.json').read_text(encoding='utf8'))}
    if 'authorization' in req:
        if req['authorization']['event_id']!=cfg['plan']['task_scope_ref']:raise PermissionError('execution authorization mismatch')
        payload['m63-authorization.json']=req['authorization']
    write_trust(payload,cfg['experiment_id'],{'personal-authorization.json':base64.b64encode((ROOT/'docs/eval/m6.2-personal-real-eval-authorization-20261101/user-decision.json').read_bytes()).decode()})

def write_trust(payload,segment,raw_files=None):
    code='''
import json,sys,os,hashlib,base64
from pathlib import Path
payload=json.load(sys.stdin)
for name,value in payload['files'].items():
 if '/' in name or '\\\\' in name:raise ValueError('public trust filename only')
 p=Path('/trust')/name;raw=base64.b64decode(payload['raw_files'][name]) if name in payload['raw_files'] else (json.dumps(value,ensure_ascii=False,indent=2)+'\\n').encode()
 if p.exists() and p.read_bytes()!=raw:
  previous=p.with_name(p.name+'.before-'+payload['segment']+'-'+hashlib.sha256(p.read_bytes()).hexdigest()[:12])
  if not previous.exists():p.rename(previous)
  else:p.unlink()
 if not p.exists():p.write_bytes(raw)
 os.chown(p,0,0);p.chmod(0o444);os.chown(p,10003,10001)
print(json.dumps({'public_files_published':list(payload['files']),'private_values_in_payload':False}))
'''
    print(admin(code,[TRUST+':/trust'],{'files':payload,'segment':segment,'raw_files':raw_files or {}}).strip())

def isolation():
    from eval.m6_1.grants import formal_runtime_digest
    services={}
    for name in ('application','control','proxy','postgres'):
        cid=docker('compose','-f','deploy/m62-eval/compose.yaml','ps','-q',name).strip();value=json.loads(docker('inspect',cid))[0]
        assert value['Config']['Labels']['com.docker.compose.project']=='shoprelay-m62-eval'
        assert not value['HostConfig']['Privileged'] and not value['HostConfig']['PortBindings']
        mounts=[{'name':m.get('Name'),'destination':m['Destination'],'rw':m['RW'],'type':m['Type']} for m in value['Mounts']]
        assert not any(m['type']=='bind' for m in mounts)
        services[name]={'container_id':cid,'image_id':value['Image'],'user':value['Config']['User'],'mounts':mounts,
            'readonly_rootfs':value['HostConfig']['ReadonlyRootfs'],'cap_drop':value['HostConfig']['CapDrop'],'networks':list(value['NetworkSettings']['Networks'])}
    assert not any('assets' in (m['name'] or '') or 'issuer-key' in (m['name'] or '') for m in services['application']['mounts'])
    code="""
import json
from pathlib import Path
from eval.m6_1.governance import verify_container_process
access={}
for name in ['/opt/shoprelay/data/eval/new-assets-r26/v7/candidates/frozen100.json','/run/formal-issuer-key/private.key','/var/run/docker.sock']:
 try:
  with open(name,'rb') as f:f.read(1)
  access[name]='READ_SUCCEEDED_UNEXPECTED'
 except (FileNotFoundError,PermissionError) as e:access[name]=type(e).__name__
print(json.dumps({'access':access,'actual_identity':verify_container_process(10002)}))
"""
    access=json.loads(docker('compose','-f','deploy/m62-eval/compose.yaml','exec','-T','application','python','-c',code))
    assert 'READ_SUCCEEDED_UNEXPECTED' not in access['access'].values()
    code="import httpx,json;print(json.dumps(httpx.get('http://application:8080/boundary/health',trust_env=False).json()))"
    health=json.loads(docker('compose','-f','deploy/m62-eval/compose.yaml','exec','-T','control','python','-c',code))
    code="import json;from pathlib import Path;print(json.dumps({'private_key_mounted':Path('/run/formal-issuer-key/private.key').exists(),'provider_secret_mounted':Path('/run/provider-secret/key').exists(),'candidate_source_mounted':Path('data/eval/new-assets-r26/v7/candidates/frozen100.json').exists()}))"
    control_access=json.loads(docker('compose','-f','deploy/m62-eval/compose.yaml','exec','-T','control','python','-c',code))
    assert not control_access['private_key_mounted'] and not control_access['provider_secret_mounted'] and control_access['candidate_source_mounted']
    assert health['runtime']==formal_runtime_digest() and health['active_runs']==0 and health['pending_actions']==0 and health['lease_free']
    networks={}
    for name in ('control','database'):
        row=json.loads(docker('network','inspect','shoprelay-m62-eval_'+name))[0];networks[name]={'internal':row['Internal']}
        assert row['Internal']
    result={'services':services,'application_access':access,'control_access':control_access,
        'networks':networks,'actual_application_runtime_sha256':health['runtime'],'health':health,
        'trusted_administrator_limit':'Host/Docker administrator trusted;no claim to prevent same-admin reads','provider_requests_for_audit':0}
    (OUT/'actual-isolation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    write_trust({'r30-isolation.json':result},'r30-isolation-'+health['runtime'][:12])

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('action',choices=('provision','initialize-public','publish','publish-recovery','fix-controller-directory','isolation','activate','deactivate'));p.add_argument('--request');a=p.parse_args()
    if a.action=='provision':provision()
    elif a.action=='initialize-public':
        path=ROOT/'docs/eval/m6.2-personal-real-eval-authorization-20261101/user-decision.json'
        write_trust({'personal-authorization.json':json.loads(path.read_text(encoding='utf8')),'r30-price.json':json.loads((OUT/'price-evidence.json').read_text(encoding='utf8'))},
            'r30-initial-public',{'personal-authorization.json':base64.b64encode(path.read_bytes()).decode()})
    elif a.action=='publish':publish(a.request)
    elif a.action=='publish-recovery':write_trust({'r30-pre-run-recovery.json':json.loads(Path(a.request).read_text(encoding='utf8'))},'r30-pre-run-recovery')
    elif a.action=='fix-controller-directory':
        code="import os,json;from pathlib import Path;p=Path('/results/r30/application');assert p.is_dir() and not list(p.iterdir());os.chown(p,0,0);p.chmod(0o2770);os.chown(p,10001,10001);print(json.dumps({'exact_empty_directory_corrected':True,'uid':p.stat().st_uid,'mode':oct(p.stat().st_mode & 511)}))"
        print(admin(code,['shoprelay-m62-eval_control-results:/results'],{}))
    elif a.action=='isolation':isolation()
    elif a.action=='deactivate':write_trust({'mode.json':{'engineering_only':False,'real_provider_allowed':False}},'r30-pause')
    else:write_trust({'mode.json':{'engineering_only':False,'real_provider_allowed':True}},'r30-activation')
