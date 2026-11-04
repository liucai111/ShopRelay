"""New container/input trust boundary;no real model or protected old content."""
from dataclasses import replace
from pathlib import Path
import json
import pytest
from test_m6_2_personal_dev_batch_r7 import tmp_path
from eval.m6_1.contracts import sha256_json
from eval.m6_1.container_boundary import runtime_payload,runtime_case,Envelope
from eval.m6_1.grants import GrantDenied


@pytest.mark.parametrize('tool_cap',[0,4])
def test_control_reservation_uses_token_price_not_whole_case_cap_and_releases_partial(tmp_path,tool_cap):
    from types import SimpleNamespace
    from eval.m6_1.container_boundary import ContainerApplication
    from eval.m6_1.budget import BudgetLedger,BudgetLimits,BudgetStop
    from eval.m6_1.store import ResultStore
    from eval.m6_1.formal_assets import raw_hash
    from test_m6_2_r24_formal_application import fictional_cases
    case=fictional_cases()[0]
    budget={'provider_calls':2,'total_tokens':120000,'estimated_usd':0.5,'tool_calls':1,
        'max_calls_per_trial':4,'max_concurrent_trials':1,'max_duration_seconds':60,'max_retries':0}
    case=replace(case,expected_outcome={**case.expected_outcome,'task':{'per_case_budget':budget}})
    store=ResultStore(tmp_path/'budget.sqlite')
    limits=BudgetLimits(provider_calls=20,tool_calls=tool_cap,
        total_tokens=600000,estimated_usd=.5,max_calls_per_trial=20)
    from eval.m6_1.contracts import EvalExperiment
    config={'purpose':'synthetic-reservation-regression'}
    store.lock_experiment(EvalExperiment('reservation-test',config,sha256_json(config),'offline-test',None,{}, {},
        'DIRECT',(),limits.to_dict(),'test-v1','test-v1'),'2026-11-03T00:00:00Z')
    ledger=BudgetLedger(store,'reservation-test',limits)
    first=ledger.reserve(trial_id='earlier',category='provider',tokens_reserved=3397,estimated_usd_reserved=.0040764)
    ledger.settle(first,status='COMMIT',actual_calls=1,actual_tokens=3397,estimated_usd=.0040764)
    price=tmp_path/'price.json';price.write_text(json.dumps({'conservative_usd_per_million':1.2}))
    called=[]
    class AtRPC(Exception):pass
    def rpc(*args,**kwargs):called.append(True);raise AtRPC()
    worker=SimpleNamespace(root=tmp_path,env={'price_evidence':{'path':'price.json','sha256':raw_hash(price)}},rpc=rpc)
    with pytest.raises(AtRPC if tool_cap else BudgetStop):
        ContainerApplication.execute(worker,case,'next',{},ledger,remaining_seconds=60)
    snapshot=ledger.snapshot()
    assert bool(called)==bool(tool_cap)
    assert snapshot['active_reservations']==(2 if tool_cap else 0)
    assert snapshot['estimated_usd_conservative_budget_consumption']<.15


def test_partial_failure_inspection_is_unknown_not_keyerror(tmp_path):
    from eval.m6_1.runner import run_authorized_experiment
    from eval.m6_1.execution_modes import ExecutionMode
    from eval.m6_1.budget import BudgetStop
    from test_m6_2_r23_confirmed_and_formal import setup_formal,lock_formal,load_worker,WORKER
    cases,config,plan,store,authority,grant,gate,_=setup_formal(tmp_path,factory_text=WORKER)
    ledger=lock_formal(config,plan,store,gate,grant)
    worker=load_worker(tmp_path,config,store,ledger,gate,grant)
    def failed(*args,**kwargs):raise BudgetStop('synthetic pre-RPC reservation failure')
    result=run_authorized_experiment(cases=cases,mode=ExecutionMode.DEV_SMOKE,grant=grant,mode_gate=gate,
        experiment_id='offline-recovery',store=store,ledger=ledger,prepare=worker.prepare,execute=failed,
        cleanup=worker.cleanup,inspect_failure=lambda receipt:{'stage':'PREPARED'})
    assert result['stop_reason']=='EXECUTION_OR_EVALUATOR_UNKNOWN_NO_REPLAY'
    attempt=f'formal.{grant.grant_id}.{cases[0].case_id}'
    event=next(e for e in store.trial_events(attempt) if e['event_type']=='EXECUTION_UNKNOWN')
    assert event['payload']['run_created']=='UNKNOWN'

def test_runtime_dispatch_contains_input_and_limits_but_no_gold():
    from test_m6_2_r24_formal_application import fictional_cases
    case=fictional_cases()[0];first=runtime_payload(case)
    expected={**dict(case.expected_outcome),'hidden_reference_answer':'DO_NOT_TRANSMIT_THIS_GOLD'}
    altered=replace(case,expected_outcome=expected)
    assert runtime_payload(altered)==first
    assert 'DO_NOT_TRANSMIT_THIS_GOLD' not in json.dumps(first)
    rebuilt=runtime_case(first)
    assert set(rebuilt.expected_outcome)=={'task'}
    assert set(rebuilt.expected_outcome['task'])=={'per_case_budget'}
    for change in [{'owner_map':{'fiction-owner':'other'}},{'allowed_capabilities':['cancel_order']}]:
        drift={**first,'input':{**first['input'],**change}}
        assert sha256_json(drift)!=sha256_json(first)
    with pytest.raises(ValueError):runtime_case({**first,'expected_outcome':expected})

@pytest.mark.parametrize('field',['labels','reference_answer','expected_route'])
def test_worker_envelope_rejects_extra_reference_fields(field):
    with pytest.raises(ValueError):Envelope.model_validate({'grant':{},'payload':{},'trial_id':'one',field:'not-authorized'})

def test_draft_intake_can_receive_new_unverified_material_but_not_freeze(tmp_path,monkeypatch):
    from test_m6_2_r24_formal_application import make_workflow
    from eval.m6_1.formal_assets import AssetWorkflow
    _,workflow,_=make_workflow(tmp_path,monkeypatch)
    cfg={**workflow.config,'intake_stage':'CANDIDATE_DRAFT','provenance':{**workflow.config['provenance'],'exposure_status':'NOT_VERIFIED'}}
    candidate=AssetWorkflow(tmp_path,cfg);record=candidate.intake()
    assert record['status']=='INTAKE_VALIDATED_NOT_GOLD'
    assert all(row['labels'] is None for row in candidate.form('day1')['items'])
    with pytest.raises(PermissionError,match='candidate draft'):candidate.freeze()

def test_formal_intake_still_rejects_unknown_provenance(tmp_path,monkeypatch):
    from test_m6_2_r24_formal_application import make_workflow
    from eval.m6_1.formal_assets import AssetWorkflow
    _,workflow,_=make_workflow(tmp_path,monkeypatch)
    cfg={**workflow.config,'provenance':{**workflow.config['provenance'],'exposure_status':'NOT_VERIFIED'}}
    with pytest.raises(ValueError,match='unseen/independence'):AssetWorkflow(tmp_path,cfg).intake()

def test_engineering_mode_cannot_relabel_an_eligible_formal_asset(tmp_path):
    from test_m6_2_r23_confirmed_and_formal import setup_formal
    _,_,plan,_,authority,_,_,_=setup_formal(tmp_path)
    with pytest.raises(GrantDenied,match='engineering permit'):authority._eligible(replace(plan,mode='ENGINEERING_EVAL'))

def test_non_gold_engineering_metadata_cannot_enter_frozen_mode(tmp_path):
    from test_m6_2_r23_confirmed_and_formal import setup_formal
    from eval.m6_1.registry import DatasetRegistry
    _,_,plan,_,authority,_,_,_=setup_formal(tmp_path)
    asset=replace(authority.registry.get(plan.asset_id),asset_type='isolated_engineering_fixture',split='synthetic',
        review_status='NOT_REVIEWED',reviewer_id=None,reviewed_at_utc=None,review_method=None,independence_status='NOT_VERIFIED',
        exposure_status='NOT_VERIFIED',scoreability='NOT_SCOREABLE',locked_at_utc=None,access_policy='ENGINEERING_SIGNED_PERMIT_ONLY')
    authority.registry=DatasetRegistry([asset])
    assert authority._eligible(replace(plan,mode='ENGINEERING_EVAL'))[0]==asset
    with pytest.raises(GrantDenied):authority._eligible(replace(plan,mode='FROZEN_EVAL'))

def test_signed_runtime_scope_owner_drift_and_replay_are_rejected(tmp_path):
    from test_m6_2_r23_confirmed_and_formal import setup_formal
    from dataclasses import asdict
    cases,_,plan,store,authority,_,gate,_=setup_formal(tmp_path,issue=False)
    inputs=[{'case_id':c.case_id,'input':{'owner':'legitimate'},'limits':{'calls':1}} for c in cases]
    plan=replace(plan,runtime_input_bindings=tuple((r['case_id'],sha256_json(r)) for r in inputs))
    authority.approved_plans=frozenset({sha256_json(asdict(plan))});grant=authority.issue(plan)
    binding=gate.formal_binding(__import__('eval.m6_1.execution_modes',fromlist=['ExecutionMode']).ExecutionMode.DEV_SMOKE)
    with pytest.raises(GrantDenied,match='dispatched runtime'):authority.claim_runtime(grant,runtime_payload={**inputs[0],'input':{'owner':'other'}},attempt_id='bad',**binding)
    authority.claim_runtime(grant,runtime_payload=inputs[0],attempt_id='valid',**binding)
    with pytest.raises(GrantDenied,match='already consumed'):authority.claim_runtime(grant,runtime_payload=inputs[0],attempt_id='again',**binding)

def test_container_role_environment_cannot_replace_actual_kernel_identity(monkeypatch):
    from eval.m6_1.governance import verify_container_process
    monkeypatch.setenv('SHOPRELAY_ROLE','evalapp');monkeypatch.setenv('UID','10002')
    with pytest.raises(PermissionError):verify_container_process(10002)

def test_governance_successor_keeps_review_and_exact_permit_gates():
    from eval.m6_1.governance import load_governance
    from eval.m6_1.formal_assets import raw_hash
    root=Path(__file__).resolve().parents[1];p=Path('eval/registry/m6.2-container-governance-r4.json')
    record=load_governance(root,{'path':p.as_posix(),'sha256':raw_hash(root/p)})
    assert 'OS_LEVEL_PROTECTED_STORAGE_AND_ACL_VERIFIED' not in record['pre_blind_test_gates']
    assert 'INDEPENDENT_REVIEW_OF_REPLACEMENT_DATASET' in record['pre_blind_test_gates']
    assert record['formal_execution_authorized'] is False
