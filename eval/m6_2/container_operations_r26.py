"""Container provisioning and engineering acceptance on the existing CLI.

Secrets remain in dedicated volumes. Console output is metadata only. Candidate
construction and Gold are controller-only; engineering fixtures are separate.
"""
from __future__ import annotations
from pathlib import Path
from dataclasses import replace,asdict
from datetime import datetime,timedelta,timezone
import json,os,base64,secrets,sys,hashlib

ROOT=Path(__file__).resolve().parents[2]
TRUST=ROOT/'data/eval/formal-trust'

def bootstrap():
    for name,uid in [('assets',10001),('results',10001),('app-state',10002),('trust',10003),('issuer-key',10003),('db-secret',10002)]:
        path=Path('/'+name);path.mkdir(exist_ok=True);os.chown(path,uid,10001);path.chmod(0o770)
    Path('/trust').chmod(0o755)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding,PrivateFormat,NoEncryption,PublicFormat
    secret=Path('/issuer-key/private.key')
    if not secret.exists():
        key=Ed25519PrivateKey.generate();secret.write_bytes(key.private_bytes(Encoding.Raw,PrivateFormat.Raw,NoEncryption()));secret.chmod(0o400);os.chown(secret,10003,10003)
    key=Ed25519PrivateKey.from_private_bytes(secret.read_bytes())
    public=base64.b64encode(key.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw)).decode()
    password=Path('/db-secret/password')
    if not password.exists():password.write_text(secrets.token_urlsafe(32));password.chmod(0o440);os.chown(password,10002,10002)
    from eval.m6_1.formal_assets import write_new
    items={'public-key.json':{'base64':public,'scope':'R26_ENGINEERING_ONLY_NOT_PRODUCTION_TRUST'},
        'mode.json':{'engineering_only':True,'real_provider_allowed':False},
        'target-binding.json':{'url_env':'REMOTE_APP_DATABASE_URL','host':'m62-eval-postgres','port':5432,'database':'shoprelay_m11_test',
            'identity_manifest':'data/eval/formal-targets/container.json','checkpoint_schema':'shoprelay_langgraph','container_target':{'service':'m62-eval-postgres','uid':10002}}}
    for name,value in items.items():
        p=Path('/trust')/name
        if not p.exists():write_new(p,value);p.chmod(0o444);os.chown(p,10003,10001)
    for name in ['formal-ledgers','formal-targets']:
        p=Path('/app-state')/name;p.mkdir(exist_ok=True);os.chown(p,10002,10002)
    p=Path('/assets/isolation-canary-gold.txt')
    if not p.exists():p.write_text('FICTIONAL_GOLD_CANARY_'+secrets.token_hex(12));os.chown(p,10001,10001);p.chmod(0o600)
    print(json.dumps({'status':'NEW_CONTAINER_VOLUMES_INITIALIZED','existing_keys_preserved':True,'production_trust_enabled':False}))

def build_candidates():
    from .candidate_construction_r26 import construct,verify_repositories
    result=construct(ROOT)
    checked=verify_repositories(ROOT)
    result={**result,'repository_checked':checked['case_count'],'repository_read_count':checked['read_count']}
    print(json.dumps({k:result[k] for k in ['frozen_candidates','independent_dev_candidate_count','cohort_counts','route_design_counts','unique_case_ids','split_groups_disjoint','exact_duplicate_questions','cross_split_near_duplicate_flags','eligible_formal_assets','repository_checked','repository_read_count']},ensure_ascii=False))

def engineering_cases():
    # Reuse existing engineering fixture constructor, never the108 candidates.
    import test_m6_2_r24_formal_application as previous
    from eval.m6_1.grants import formal_case_payload
    cases=[replace(c,split='synthetic',data_version='r26-isolated-engineering-v1') for c in previous.fictional_cases()]
    for index,(tool,decision) in enumerate([('create_return_request','CONFIRM'),('create_refund_request','CONFIRM'),
            ('cancel_order','CONFIRM'),('escalate_to_human','CONFIRM'),('create_refund_request','LEAVE_PENDING'),('create_refund_request','REJECT')]):
        value=formal_case_payload(cases[2]);inp=value['input'];inp['owner_map']={'fiction-owner':'u_demo'}
        order=inp['fixture']['orders_and_snapshots'][0];order.update(paid_at='2026-10-01T10:00:00Z',paid_cents=2250)
        if tool=='create_return_request':order.update(status='DELIVERED',delivered_at=datetime.now(timezone.utc).isoformat())
        inp['fixture']['policies']=[{'evidence_id':p+'-'+c,'policy_id':p,'clause_id':c,'text':t,'version':'engineering-only-v1','effective':'2026-01-01/2026-12-31'}
            for p,c,t in [('refund','before-shipping','未发货已付款订单可申请整单退款；申请待审核，不代表到账。'),('refund','uniqueness','同订单只允许一个开放退款申请。'),
                ('cancel','eligible','未发货订单可申请取消，已付款订单关联退款申请。'),('return-standard','window','商品送达后七天内保持完好和附件齐全可申请退货，申请待审核。')]]
        inp['fixture']['products']=[{'id':'engineering-product','name':'Separate engineering product','category':'ACCESSORY','price_cents':2250}]
        inp['fixture']['inventory']=[{'id':'engineering-inventory','product_id':'engineering-product','available_qty':10,'reserved_qty':1,'version':1}]
        inp['fixture']['order_items']=[{'id':'engineering-line','order_id':order['id'],'product_id':'engineering-product','quantity':1,'paid_cents':2250}]
        inp['fixture']['order_inventory_reservations']=[{'id':'engineering-reserve','order_id':order['id'],'product_id':'engineering-product','quantity':1,'status':'RESERVED','source_ref':'isolated:engineering','created_at':'2026-10-01T10:00:00Z'}]
        args={'order_id':order['order_id'],'reason_code':'OTHER','reason_summary':'explicit engineering owner followup'}
        if tool=='escalate_to_human':args={'order_id':order['order_id'],'issue_category':'ORDER_STATUS','user_description':'请人工核实本人订单','issue_summary':'核实记录','reason_code':'OTHER'}
        inp['owner_interaction']={'tool_id':tool,'target_resource_id':order['order_id'],'arguments':args,'decision':decision}
        value['case_id']=f'canary-action-{index}';value['group_id']=f'canary-action-group-{index}'
        value['expected_outcome']['task']['per_case_budget'].update(tool_calls=12,max_calls_per_trial=20)
        from eval.m6_1.contracts import EvalCase
        cases.append(EvalCase.from_dict(value))
    return cases

def prepare():
    import httpx
    from eval.m6_1.grants import FormalExecutionPlan,formal_case_payload,formal_case_digest,formal_runtime_digest
    from eval.m6_1.container_boundary import runtime_payload
    from eval.m6_1.budget import BudgetLimits
    from eval.m6_1.formal_application import dependency_versions
    from eval.m6_1.formal_assets import write_new,raw_hash
    from eval.m6_1.registry import DatasetAsset,DatasetRegistry
    from eval.m6_1.contracts import sha256_json
    from eval.m6_1.store import ResultStore
    from app.m0.config import load_settings
    result=httpx.get('http://application:8080/boundary/health',trust_env=False).json()
    segment=sys.argv[2] if len(sys.argv)>2 else 'segment2'
    import re
    if not re.fullmatch('segment[0-9]+',segment):raise ValueError('bounded engineering segment name required')
    asset_path=f'data/eval/new-assets-r26/engineering-{segment}.json'
    if (ROOT/asset_path).exists():
        from eval.m6_1.contracts import EvalCase
        cases=[EvalCase.from_dict(c) for c in json.loads((ROOT/asset_path).read_text())['cases']]
    else:
        cases=engineering_cases()
        if '--unknown-usage' in sys.argv:
            cases=[replace(cases[0],input={**dict(cases[0].input),'user_query':cases[0].input['user_query']+' unknown usage canary'}),cases[2]]
        elif '--cleanup-failure' in sys.argv:cases=cases[:2]
        write_new(ROOT/asset_path,{'cases':[formal_case_payload(c) for c in cases]})
    asset=DatasetAsset('R26-ENGINEERING-NOT-GOLD','isolated_engineering_fixture',cases[0].data_version,None,'synthetic',len(cases),asset_path,
        raw_hash(ROOT/asset_path),'NOT_REVIEWED',None,None,None,'NOT_VERIFIED','NOT_VERIFIED','NOT_SCOREABLE',None,'ENGINEERING_SIGNED_PERMIT_ONLY',
        ('Separate fictional canary;no independent review or formal approval is represented',))
    registry=DatasetRegistry([asset]).to_dict()
    now=datetime.now(timezone.utc);settings=load_settings(require_api_key=False)
    price={'valid_from_utc':(now-timedelta(minutes=5)).isoformat(),'valid_until_utc':(now+timedelta(days=2)).isoformat(),
        'conservative_usd_per_million':1.2,'model':settings.model,'endpoint':settings.base_url,'source_refs':['ENGINEERING_OFFLINE_PLACEHOLDER_NOT_REAL_PROVIDER_PRICE']}
    pricepath='data/eval/formal-trust/price.json'
    binding=json.loads((TRUST/'target-binding.json').read_text())
    env={'mode':'ENGINEERING_EVAL','execution_factory':{'path':'eval/m6_1/formal_application.py','callable':'factory'},
        'application_postgres':binding,'application_artifacts':f'data/eval/formal-ledgers/{segment}/application','repository_read_cap_per_case':32,
        'python_dependencies':dependency_versions(),'provider_binding':{'model':settings.model,'endpoint':settings.base_url,'nodes':{k:asdict(v) for k,v in settings.nodes.items()}},
        'price_evidence':{'path':pricepath,'sha256':hashlib.sha256((json.dumps(price,ensure_ascii=False,indent=2)+'\n').encode()).hexdigest()},
        'container_endpoint':'http://application:8080','container_proxy':'http://proxy:7892','issuer_subject':'evalissuer','output_reviewers':[],
        'simulated_action_tools':['create_return_request','create_refund_request','cancel_order','escalate_to_human']}
    limits=BudgetLimits(provider_calls=4*len(cases),tool_calls=sum(c.expected_outcome['task']['per_case_budget']['tool_calls'] for c in cases),
        total_tokens=60000*len(cases),estimated_usd=.3*len(cases),max_calls_per_trial=20,max_duration_seconds=120*len(cases))
    exp={'fixture_version':asset.version,'fixture_sha256':asset.sha256,'grader_version':'formal-code-grading-v1','judge_version':'none','retriever_config':{'mode':'bm25'},
        'knowledge_index_version':'engineering-only-corpus','model_versions':{'application':settings.model},'prompt_versions':{'application':'runtime-bound'},
        'tool_permissions':sorted({t for c in cases for t in c.input['allowed_capabilities']}),'retry_policy':{'max_retries':0},'random_seed':1,'environment':env,'budget_limits':limits.to_dict(),'data_version':asset.version}
    plan=FormalExecutionPlan('R26-USER-ENGINEERING-BOUNDARY-ONLY-'+segment,'ENGINEERING_EVAL',asset.asset_id,asset.version,asset.sha256,
        tuple((c.case_id,formal_case_digest(c)) for c in cases),'evalcontrol',sha256_json(exp),'shoprelay-m62-eval-pg',result['target_profile'],result['target_identity'],
        f'data/eval/formal-ledgers/{segment}/results.sqlite3',(now-timedelta(minutes=1)).isoformat(),(now+timedelta(days=1)).isoformat(),limits,
        (('eval/m6_1/formal_application.py',raw_hash(ROOT/'eval/m6_1/formal_application.py')),),formal_runtime_digest(),'R26-ISOLATED-ENGINEERING-'+segment,
        tuple((c.case_id,sha256_json(runtime_payload(c))) for c in cases))
    public=json.loads((TRUST/'public-key.json').read_text())['base64']
    governance='eval/registry/m6.2-container-governance-r4.json'
    config={'engineering_only':True,'engineering_asset_ids':[asset.asset_id],'issuer':'R26_ENGINEERING_ONLY','trusted_issuers':['R26_ENGINEERING_ONLY'],
        'subject':'evalcontrol','signature_scheme':'ED25519_V1','verification_public_key_base64':public,'signing_key_env':'SHOPRELAY_R26_ENGINEERING_SIGNING_KEY',
        'registry_metadata':'data/eval/formal-trust/registry.json','result_store':plan.ledger_path,'grant_file':f'data/eval/formal-ledgers/{segment}/engineering-grant.json',
        'actual_target_binding':[plan.target_id,plan.target_profile,plan.target_identity],'allowed_targets':[[plan.target_id,plan.target_profile,plan.target_identity]],
        'approved_plan_hashes':[sha256_json(asdict(plan))],'plan':json.loads(json.dumps(asdict(plan))),'experiment_id':plan.experiment_id,'experiment_config':exp,
        'source_revision_label':'LOCKED_R26_IMAGE_NOT_GIT_HEAD','scoring_version':'formal-code-grading-v1',
        'governance_binding':{'path':governance,'sha256':raw_hash(ROOT/governance)}}
    ResultStore(ROOT/plan.ledger_path)
    (ROOT/plan.ledger_path).chmod(0o660);(ROOT/plan.ledger_path).parent.chmod(0o2770)
    write_new(ROOT/f'data/eval/formal-ledgers/approval-request-{segment}.json',{'config':config,'price':price,'registry':registry})
    print(json.dumps({'engineering_case_count':len(cases),'approval_request_saved':True,'runtime':formal_runtime_digest(),'real_candidates_used':False}))

def sign():
    import pwd
    if os.geteuid()!=10003 or pwd.getpwuid(os.geteuid()).pw_name!='evalissuer' or not Path('/.dockerenv').is_file():
        raise PermissionError('actual isolated issuer process identity required')
    from eval.m6_1.formal_assets import write_new
    segment=sys.argv[2] if len(sys.argv)>2 else 'segment2'
    req=json.loads((ROOT/f'data/eval/formal-ledgers/approval-request-{segment}.json').read_text())
    cfg=req['config']
    if cfg['engineering_only'] is not True or cfg['plan']['mode']!='ENGINEERING_EVAL' or cfg['plan']['asset_id']!='R26-ENGINEERING-NOT-GOLD':
        raise PermissionError('only this user-authorized engineering permit can be issued')
    for name,value in [('deployment.json',cfg),('registry.json',req['registry']),('price.json',req['price'])]:
        path=TRUST/name
        if path.exists():
            old=TRUST/(name+'.before-'+segment)
            if old.exists():raise ValueError('segment activation already used;do not overwrite')
            path.rename(old)
        write_new(path,value);path.chmod(0o444)
    os.environ[cfg['signing_key_env']]=base64.b64encode(Path('/run/issuer-key/private.key').read_bytes()).decode()
    # Issuer does not receive Gold volume or contact the app. The existing
    # authority signs the exact approved configuration; target checked at prepare.
    from eval.m6_1.container_boundary import read_grant
    from eval.m6_1.grants import FormalGrantAuthority,FormalExecutionGrant
    from eval.m6_1.registry import DatasetRegistry
    from eval.m6_1.store import ResultStore
    preview=read_grant({'grant_id':'UNISSUED','issuer':cfg['issuer'],'plan':cfg['plan'],'asset_metadata_sha256':'0'*64,'signature':''})
    authority=FormalGrantAuthority(DatasetRegistry.from_dict(req['registry']),repository_root=ROOT,store=ResultStore(ROOT/cfg['result_store']),issuer=cfg['issuer'],
        trusted_issuers=set(cfg['trusted_issuers']),signing_key=base64.b64decode(os.environ[cfg['signing_key_env']]),verification_key=base64.b64decode(cfg['verification_public_key_base64']),
        approved_plan_hashes=set(cfg['approved_plan_hashes']),allowed_targets={tuple(t) for t in cfg['allowed_targets']})
    grant=authority.issue(preview.plan);write_new(ROOT/cfg['grant_file'],asdict(grant))
    print(json.dumps({'grant_kind':'ENGINEERING_EXECUTION_V1','issued':True,'formal_candidates_authorized':False}))

def run():
    from eval.m6_1.execution_modes import formal_cli
    from eval.m6_1.container_boundary import ContainerApplication
    from eval.m6_1.contracts import EvalCase
    original=ContainerApplication.cleanup
    def observed_cleanup(worker,receipt,trial,raw):
        if '--cleanup-failure' in sys.argv:return False  # isolated harness injection,not production case routing
        ev=raw['application_archive'].get('owner_interaction')
        if ev and ev['required_action_state']=='SUCCEEDED' and ev['state_check']=='PASS':
            case=EvalCase.from_dict(json.loads((worker.root/raw['case_path']).read_text()))
            proof=worker.rpc('verify-repeat',case,trial['trial_id'])
            from eval.m6_1.formal_assets import write_new
            write_new(worker.artifact_path(trial['trial_id'],'.repeat-proof.json'),proof)
        return original(worker,receipt,trial,raw)
    ContainerApplication.cleanup=observed_cleanup
    try:result=formal_cli(['--formal-config','data/eval/formal-trust/deployment.json','--execute'],repository_root=ROOT)
    finally:ContainerApplication.cleanup=original
    from eval.m6_1.formal_assets import write_new
    write_new(ROOT/cfg_result_path(),result)
    print(json.dumps({'completed':len(result['completed_cases']),'stop_reason':result['stop_reason'],'reservations':result['budget']['active_reservations'],
        'fake_provider_requests':result['budget']['provider_requests'],'real_model_calls':0}))

def cfg_result_path():
    cfg=json.loads((TRUST/'deployment.json').read_text())
    return str(Path(cfg['plan']['ledger_path']).parent/'engineering-result.json')

if __name__=='__main__':
    {'bootstrap':bootstrap,'candidates':build_candidates,'prepare':prepare,'sign':sign,'run':run}[sys.argv[1]]()
