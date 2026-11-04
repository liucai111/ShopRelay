"""Isolated formal signing/entry/recovery and exact user-adopted Dev references."""
from dataclasses import asdict,replace
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib,json,os,base64,subprocess,sys
import pytest

from test_m6_2_personal_dev_batch_r7 import tmp_path
from eval.m6_1.budget import BudgetLimits,BudgetLedger
from eval.m6_1.contracts import EvalCase,sha256_json
from eval.m6_1.registry import DatasetAsset,DatasetRegistry
from eval.m6_1.grants import FormalExecutionPlan,FormalGrantAuthority,GrantDenied,formal_case_digest,formal_runtime_digest
from eval.m6_1.execution_modes import ExecutionMode,ExecutionModeGate,formal_cli,ModeAuthorizationError
from eval.m6_1.store import ResultStore
from eval.m6_1.experiment import preregister_experiment
from eval.m6_1.runner import run_authorized_experiment

ROOT=Path(__file__).resolve().parents[1]


def setup_formal(tmp_path,mode="DEV_SMOKE",factory_text="value=1\n",cleanup_failure=False,issue=True,experiment_id="offline-recovery"):
    split="dev" if mode=="DEV_SMOKE" else "frozen_test"
    cases=[EvalCase(f"offline-case-{i}","offline-group",split,{"message":f"Offline read task{i}","user_id":"offline-owner"},
        {"route":"DIRECT","allowed_tools":[],"ownership":"PASS","business_outcome":"ANSWERED","allowed_evidence_ids":[]},
        (),None,"low","offline-test-v1") for i in range(3)]
    asset_path=tmp_path/"unprotected-test-asset.json"
    asset_path.write_text(json.dumps({"cases":[c.to_dict() for c in cases]}),encoding="utf8")
    digest=hashlib.sha256(asset_path.read_bytes()).hexdigest()
    now=datetime.now(timezone.utc)
    asset=DatasetAsset("isolated-test-asset","test_fixture","offline-test-v1",None,split,3,
        asset_path.relative_to(tmp_path).as_posix(),digest,"INDEPENDENT_REVIEWED","OFFLINE_TEST_ONLY",now.isoformat(),
        "ISOLATED_TEST_NOT_REAL_REVIEW","INDEPENDENT","UNEXPOSED_VERIFIED","ELIGIBLE",now.isoformat(),"TEST_ONLY",())
    registry=DatasetRegistry([asset]);registry.save_new(tmp_path/"registry.json")
    factory=tmp_path/"worker.py";factory.write_text(factory_text,encoding="utf8")
    limits=BudgetLimits(provider_calls=3,total_tokens=300,estimated_usd=.1,max_calls_per_trial=1,max_duration_seconds=120)
    config={"fixture_version":"offline-test-v1","fixture_sha256":digest,"grader_version":"test-grader",
        "judge_version":"none","retriever_config":{},"knowledge_index_version":None,"model_versions":{"test":"offline"},
        "prompt_versions":{},"tool_permissions":[],"retry_policy":{"max_retries":0},"random_seed":1,
        "environment":{"mode":mode,"test_cleanup_failure":cleanup_failure,"execution_factory":{"path":"worker.py","callable":"factory"}},
        "budget_limits":limits.to_dict(),"data_version":"offline-test-v1"}
    plan=FormalExecutionPlan("TEST-APPROVAL-ONLY",mode,asset.asset_id,asset.version,digest,
        tuple((c.case_id,formal_case_digest(c)) for c in cases),"test-runner",sha256_json(config),
        "local-test-target","isolated-test","test-db-identity","results.sqlite3",(now-timedelta(seconds=1)).isoformat(),
        (now+timedelta(hours=1)).isoformat(),limits,(("worker.py",hashlib.sha256(factory.read_bytes()).hexdigest()),),formal_runtime_digest(),experiment_id)
    store=ResultStore(tmp_path/"results.sqlite3")
    kwargs=dict(repository_root=tmp_path,store=store,issuer="TEST_ONLY",trusted_issuers={"TEST_ONLY"},signing_key=b"t"*32,
        approved_plan_hashes={sha256_json(asdict(plan))},allowed_targets={(plan.target_id,plan.target_profile,plan.target_identity)})
    authority=FormalGrantAuthority(registry,**kwargs);grant=authority.issue(plan) if issue else None
    gate=ExecutionModeGate(None,subject="test-runner",formal_authority=authority,config_sha256=plan.config_sha256,
        target=(plan.target_id,plan.target_profile,plan.target_identity))
    return cases,config,plan,store,authority,grant,gate,kwargs


@pytest.mark.parametrize("mode",["DEV_SMOKE","FROZEN_EVAL"])
def test_formal_eligible_isolated_asset_can_be_loaded_in_exact_order(tmp_path,mode):
    cases,config,plan,store,authority,grant,gate,_=setup_formal(tmp_path,mode)
    loaded=authority.read_cases(grant,subject="test-runner",config_sha256=plan.config_sha256,target=gate.target)
    assert [c.to_dict() for c in loaded]==[c.to_dict() for c in cases]
    gate.claim(mode=ExecutionMode(mode),grant=grant,case=cases[0],attempt_id="first")
    gate.authorize(mode=ExecutionMode(mode),grant=grant,case=cases[0],attempt_id="first")
    with pytest.raises(ModeAuthorizationError): gate.claim(mode=ExecutionMode(mode),grant=grant,case=cases[0],attempt_id="again")


@pytest.mark.parametrize("bad",["mode","subject","config","target","case","signature","runtime","implementation","asset_metadata","expired"])
def test_wrong_binding_denied_before_prepare_or_model(tmp_path,bad):
    cases,config,plan,store,a,g,gate,_=setup_formal(tmp_path)
    binding=gate.formal_binding(ExecutionMode.DEV_SMOKE);case=cases[0]
    if bad=="mode": binding["mode"]="FROZEN_EVAL"
    if bad=="subject": binding["subject"]="other-owner"
    if bad=="config": binding["config_sha256"]="f"*64
    if bad=="target": binding["target"]=("production","wrong","wrong")
    if bad=="case": case=replace(case,input={"message":"different user input"})
    if bad=="signature": g=replace(g,signature="0"*64)
    if bad=="runtime": (tmp_path/"worker.py").write_text("changed=1",encoding="utf8")
    if bad=="implementation":
        import eval.m6_1.grants as module
        a.clock=lambda:datetime.now(timezone.utc)
        from unittest.mock import patch
        with patch.object(module,'formal_runtime_digest',return_value='f'*64):
            with pytest.raises(GrantDenied,match='implementation changed'): a.verify(g,case=case,**binding)
        return
    if bad=="asset_metadata": a.registry=DatasetRegistry([replace(a.registry.get(plan.asset_id),review_method="changed review")])
    if bad=="expired": a.clock=lambda:datetime.now(timezone.utc)+timedelta(days=1)
    with pytest.raises(GrantDenied): a.verify(g,case=case,**binding)
    with store.transaction(immediate=False) as db:
        assert db.execute("SELECT COUNT(*) FROM formal_grant_events").fetchone()[0]==0


def test_asset_bytes_tampering_and_unapproved_reissue_rejected(tmp_path):
    cases,config,p,s,a,g,gate,kwargs=setup_formal(tmp_path)
    (tmp_path/"unprotected-test-asset.json").write_text('{"cases":[]}',encoding="utf8")
    from eval.m6_1.registry import AccessDenied
    with pytest.raises(AccessDenied,match="HASH_MISMATCH"):
        a.read_cases(g,subject=p.subject,config_sha256=p.config_sha256,target=gate.target)
    with pytest.raises(GrantDenied,match="already issued"): a.issue(p)
    with pytest.raises(GrantDenied,match="not approved"): a.issue(replace(p,approval_ref="different approval"))


def test_revoke_close_and_unsettled_claim_survive_new_authority_process_state(tmp_path):
    cases,config,p,s,a,g,gate,kwargs=setup_formal(tmp_path)
    gate.claim(mode=ExecutionMode.DEV_SMOKE,grant=g,case=cases[0],attempt_id="first")
    restarted=FormalGrantAuthority(a.registry,**kwargs)
    with pytest.raises(GrantDenied,match="unsettled"):
        restarted.claim(g,case=cases[1],attempt_id="second",**gate.formal_binding(ExecutionMode.DEV_SMOKE))
    restarted.settle(g,case_id=cases[0].case_id,attempt_id="first",continue_allowed=True,cleanup_status="CLEANED")
    restarted.revoke(g)
    with pytest.raises(GrantDenied,match="revoked"): a.verify(g,**gate.formal_binding(ExecutionMode.DEV_SMOKE))


WORKER='''from dataclasses import replace
from eval.m6_1.shoprelay_adapter import SyntheticMockShopRelayTransport,FormalShopRelayAdapter
from eval.m6_1.runner import FormalTrialResult
from eval.m6_1.grader import CodeGrader
class Worker:
 def __init__(self,config,store,ledger,mode_gate,grant): self.config,self.store,self.ledger,self.gate,self.grant=config,store,ledger,mode_gate,grant
 def verify_execution_target(self,target):
  assert target==tuple(self.config['actual_target_binding'])
  return True
 def verify_execution_subject(self,subject):
  return subject=='test-runner'
 def prepare(self,case,trial): return {'case_id':case.case_id,'trial_id':trial,'receipt_id':trial+'-receipt'}
 def execute(self,case,trial_id,receipt,ledger,remaining_seconds=120):
  assert 0<remaining_seconds<=120
  assert receipt['case_id']==case.case_id
  reserve=ledger.reserve(trial_id=trial_id,category='provider',tokens_reserved=20,estimated_usd_reserved=.001)
  assert ledger.trial_snapshot(trial_id)['active_reservations']==1
  run='offline-run-'+case.case_id
  transport=SyntheticMockShopRelayTransport(session_response={'session':{'session_id':'offline-session'}},
   stream_receipt={'run_id':run},run_responses={run:{'run':{'run_id':run,'status':'SUCCEEDED','model_id':'deepseek-flash','usage':{'prompt_tokens':10,'completion_tokens':10},'answer':{'answer':'offline visible output','estimated_cost_usd':.001}}}},
   event_responses={run:[{'event_id':run+'-e','run_id':run,'seq':1,'type':'route_decided','payload':{'strategy':'DIRECT','decision_source':'RULE','safety_decision':'ALLOW'}}]},
   execution_responses={run:{'execution':{'run_id':run,'status':'SUCCEEDED','tool_calls':[],'citations':[]}}})
  transport.verify_execution_target=self.verify_execution_target
  transport.verify_execution_subject=self.verify_execution_subject
  adapter=FormalShopRelayAdapter(transport,self.gate)
  from eval.m6_1.execution_modes import ExecutionMode
  obs=adapter.execute_case(mode=ExecutionMode(self.grant.plan.mode),grant=self.grant,case=case,experiment_id=self.config['experiment_id'],client_request_id=trial_id)
  ledger.settle(reserve,status='COMMIT',actual_calls=1,actual_tokens=20,estimated_usd=.001)
  trial=obs.to_eval_trial(trial_id=trial_id,case_id=case.case_id,experiment_id=self.config['experiment_id'],trial_index=1)
  result={'route':'DIRECT','tools':[],'schema_valid':True,'ownership_check':'PASS','unconfirmed_write':False,'evidence_ids':[],'business_outcome':'PARTIAL' if case.case_id.endswith('-0') else 'ANSWERED','task_success_observable':not case.case_id.endswith('-0'),'safety_result':'PASS'}
  graded=CodeGrader().grade(trial_id=trial_id,expected=case.expected_outcome,result=result,lifecycle='SUCCEEDED')
  return FormalTrialResult(trial,{'visible_answer':'offline visible output','result':result},graded,{'permission':'PASS','audit':'PASS','usage':'PASS','terminal':'PASS'})
 def cleanup(self,receipt,trial,raw):
  assert receipt['trial_id']==trial['trial_id'] and receipt['receipt_id'].endswith('-receipt')
  return not (self.config['experiment_config']['environment']['test_cleanup_failure'] and receipt['case_id'].endswith('-1'))
def factory(**kwargs): return Worker(**kwargs)
'''


@pytest.mark.parametrize("cleanup_failure",[False,True])
def test_real_cli_multiple_trials_quality_fail_continues_cleanup_failure_stops_and_closed_replay_denied(tmp_path,cleanup_failure):
    # Fixture metadata alone has test review labels; no project Candidate content.
    cases,config,p,s,a,g,gate,_=setup_formal(tmp_path,factory_text=WORKER,cleanup_failure=cleanup_failure,issue=False,experiment_id='offline-formal-cli')
    deployment={'plan':asdict(p),'signing_key_env':'SHOPRELAY_OFFLINE_TEST_KEY','result_store':'results.sqlite3','registry_metadata':'registry.json',
        'issuer':'TEST_ONLY','trusted_issuers':['TEST_ONLY'],'approved_plan_hashes':[sha256_json(asdict(p))],
        'allowed_targets':[gate.target],'actual_target_binding':gate.target,'subject':p.subject,'grant_file':'grant.json',
        'experiment_config':config,'experiment_id':'offline-formal-cli','source_revision_label':'WORKING_TREE_TEST_HASH',
        'scoring_version':'offline-test-rubric'}
    # Test-only launcher uses the actual existing CLI's main and a confined root.
    deploy=tmp_path/'deployment.json';deploy.write_text(json.dumps(deployment),encoding='utf8')
    env=dict(os.environ,SHOPRELAY_OFFLINE_TEST_KEY=base64.b64encode(b't'*32).decode(),
        PYTHONPATH=os.pathsep.join([str(ROOT),os.environ.get('PYTHONPATH','')]))
    for action in ('issue','preflight','execute'):
        code="import sys,socket;socket.socket.connect=lambda *a,**k:(_ for _ in()).throw(RuntimeError('OFFLINE_TEST_NETWORK_BLOCKED'));from pathlib import Path;import scripts.run_m6_1_b_synthetic as entry;entry.ROOT=Path("+repr(str(tmp_path))+");sys.argv=['scripts/run_m6_1_b_synthetic.py',*sys.argv[1:]];entry.main()"
        r=subprocess.run([sys.executable,'-c',code,'--formal-config',str(deploy),'--'+action],env=env,capture_output=True,text=True)
        assert r.returncode==0,r.stderr
        output=json.loads(r.stdout)
        if action=='execute':
            assert len(output['trials'])==(2 if cleanup_failure else 3)
            assert output['outcomes'][0]['task_success']=='FAIL'
            assert bool(output['stop_reason'])==cleanup_failure
    r=subprocess.run([sys.executable,'-c',code,'--formal-config',str(deploy),'--execute'],env=env,capture_output=True,text=True)
    assert r.returncode!=0 and ('closed' in r.stderr or 'stopped' in r.stderr)


def test_user_confirmed_qrels_are_exact_not_independent_gold(tmp_path,monkeypatch):
    from eval.m6_2 import confirmed_review_r23 as module
    monkeypatch.setattr(module,'OUT',tmp_path.relative_to(ROOT))
    report=module.build(ROOT)
    assert report['confirmed_tasks']=={'PASS':2,'PARTIAL':1,'FAIL':4}
    assert (report['positive'],report['negative'],report['unknown'])==(24,144,28)
    qrels=json.loads((tmp_path/'confirmed-dev-qrels.json').read_text(encoding='utf8'))
    assert qrels['independent_annotation'] is False and qrels['next_day_blind_review'] is False
    assert {(r['query_id'],r['chunk_id']) for r in qrels['changes']}==module.QREL_CHANGES
    assert report['retrieval_metrics']['Recall@5']['value']!=pytest.approx(.7777777778)
    event=json.loads((tmp_path/'confirmation-event.json').read_text(encoding='utf8'))
    event['task_decisions']['D1-ORDER-008']='FAIL'
    (tmp_path/'confirmation-event.json').write_text(json.dumps(event),encoding='utf8')
    with pytest.raises(ValueError,match='EVENT_DRIFT'): module.build(ROOT)


def load_worker(tmp_path,config,store,ledger,gate,grant):
    import importlib.util
    spec=importlib.util.spec_from_file_location('offline_bound_worker',tmp_path/'worker.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module.factory(config={'actual_target_binding':gate.target,'experiment_id':'offline-recovery',
        'experiment_config':config},store=store,ledger=ledger,mode_gate=gate,grant=grant)


def lock_formal(config,p,store,gate,grant):
    preregister_experiment(store=store,experiment_id='offline-recovery',config=config,
        git_commit='offline-working-tree-binding',strategy=p.mode,scoring_version='test-r1',formal_gate=gate,formal_grant=grant)
    return BudgetLedger(store,'offline-recovery',p.budget)


def test_terminal_archive_restart_cleans_and_continues_without_reexecuting_consumed_case(tmp_path):
    cases,config,p,s,a,g,gate,kwargs=setup_formal(tmp_path,factory_text=WORKER)
    ledger=lock_formal(config,p,s,gate,g);worker=load_worker(tmp_path,config,s,ledger,gate,g)
    tid=f'formal.{g.grant_id}.{cases[0].case_id}'
    gate.claim(mode=ExecutionMode.DEV_SMOKE,grant=g,case=cases[0],attempt_id=tid)
    receipt=worker.prepare(cases[0],tid);result=worker.execute(cases[0],tid,receipt,ledger)
    s.append_trial_bundle(trial=result.trial,raw_result={**result.raw_result,'receipt':receipt,'execution_integrity':dict(result.integrity_checks)},
        outcome=result.grade.outcome,scores=list(result.grade.scores),at=datetime.now(timezone.utc).isoformat(),
        terminal_event={'event_id':'offline-terminal-event','event_type':'SUCCEEDED','payload':{},'created_at_utc':datetime.now(timezone.utc).isoformat()})
    # Simulated restart after raw commit, before exact cleanup/settlement.
    gate.formal_authority=FormalGrantAuthority(a.registry,**kwargs)
    executed=[]
    def execute(case,*args,**kwargs): executed.append(case.case_id);return worker.execute(case,*args,**kwargs)
    report=run_authorized_experiment(cases=cases,mode=ExecutionMode.DEV_SMOKE,grant=g,mode_gate=gate,
        experiment_id='offline-recovery',store=s,ledger=ledger,prepare=worker.prepare,execute=execute,cleanup=worker.cleanup)
    assert report['stop_reason'] is None and executed==[cases[1].case_id,cases[2].case_id]
    assert report['budget']['calls_by_category']['provider']==3 and len(report['trials'])==3


def test_uncommitted_execution_restart_does_not_replay_or_cleanup_unknown_run(tmp_path):
    cases,config,p,s,a,g,gate,_=setup_formal(tmp_path,factory_text=WORKER)
    ledger=lock_formal(config,p,s,gate,g)
    gate.claim(mode=ExecutionMode.DEV_SMOKE,grant=g,case=cases[0],attempt_id=f'formal.{g.grant_id}.{cases[0].case_id}')
    def forbidden(*args): raise AssertionError('Interrupted unknown must not execute or delete')
    report=run_authorized_experiment(cases=cases,mode=ExecutionMode.DEV_SMOKE,grant=g,mode_gate=gate,
        experiment_id='offline-recovery',store=s,ledger=ledger,prepare=forbidden,execute=forbidden,cleanup=forbidden)
    assert report['stop_reason']=='INTERRUPTED_UNKNOWN_NO_REPLAY' and report['trials']==[]


def test_unknown_usage_stops_after_safe_terminal_cleanup_and_cannot_move_ledger(tmp_path):
    text=WORKER.replace('actual_tokens=20,estimated_usd=.001','actual_tokens=None,estimated_usd=None').replace("'usage':'PASS'","'usage':'UNKNOWN'")
    cases,config,p,s,a,g,gate,kwargs=setup_formal(tmp_path,factory_text=text)
    ledger=lock_formal(config,p,s,gate,g);worker=load_worker(tmp_path,config,s,ledger,gate,g)
    report=run_authorized_experiment(cases=cases,mode=ExecutionMode.DEV_SMOKE,grant=g,mode_gate=gate,
        experiment_id='offline-recovery',store=s,ledger=ledger,prepare=worker.prepare,execute=worker.execute,cleanup=worker.cleanup)
    assert report['stop_reason'] and len(report['trials'])==1 and report['budget']['token_status']=='UNKNOWN_USAGE'
    assert s.trial_events(report['trials'][0]['trial_id'])[-1]['payload']['status']=='CLEANED'
    changed={**kwargs,'store':ResultStore(tmp_path/'another-ledger.sqlite3')}
    other=FormalGrantAuthority(a.registry,**changed)
    with pytest.raises(GrantDenied,match='ledger path'): other.issue(p)


def test_new_experiment_name_cannot_reset_signed_budget_and_secret_redaction_does_not_change_case_scope(tmp_path):
    cases,config,p,s,a,g,gate,_=setup_formal(tmp_path)
    with pytest.raises(ValueError,match='differs from signed plan'):
        preregister_experiment(store=s,experiment_id='different-budget',config=config,git_commit='test',strategy=p.mode,
            scoring_version='test',formal_gate=gate,formal_grant=g)
    first=replace(cases[0],input={'message':'api_key=synthetic-first'})
    second=replace(cases[0],input={'message':'api_key=synthetic-second'})
    assert first.to_dict()==second.to_dict()  # logging redaction is intentionally lossy
    assert formal_case_digest(first)!=formal_case_digest(second)


def test_real_application_dependency_digest_is_part_of_formal_execution_binding(tmp_path,monkeypatch):
    cases,config,p,s,a,g,gate,_=setup_formal(tmp_path)
    from eval.m6_1 import personal_dev
    monkeypatch.setattr(personal_dev,'implementation_digest',lambda root:'f'*64)
    with pytest.raises(GrantDenied,match='implementation changed'):
        a.verify(g,case=cases[0],**gate.formal_binding(ExecutionMode.DEV_SMOKE))
