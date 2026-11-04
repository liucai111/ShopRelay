"""Personal real mode shares the actual Runner,not formal unseen eligibility."""
import json,base64
from dataclasses import replace,asdict
import pytest
from test_m6_2_personal_dev_batch_r7 import tmp_path
from test_m6_2_r24_formal_application import deploy,cli
from eval.m6_1.formal_assets import raw_hash,write_new
from eval.m6_1.registry import DatasetRegistry
from eval.m6_1.grants import FormalGrantAuthority,GrantDenied
from eval.m6_1.contracts import sha256_json
from eval.m6_1.container_boundary import read_grant


def personal(tmp_path,monkeypatch,**kwargs):
    cases,store,requests=deploy(tmp_path,monkeypatch,**kwargs)
    path=tmp_path/'deployment.json';cfg=json.loads(path.read_text(encoding='utf8'))
    old=DatasetRegistry.load_metadata(tmp_path/'registry.json').assets()[0]
    event={'event_id':'TEST_PERSONAL_USER_PERMISSION','bound_inputs':[{'path':old.source_path,'sha256':old.sha256}]}
    write_new(tmp_path/'personal-decision.json',event)
    asset=replace(old,asset_type='user_reviewed_synthetic_benchmark',review_status='BATCH_USER_CONFIRMED',
        review_method='USER_REVIEWED_PERSONAL_SYNTHETIC_NO_BLIND_V1',scoreability='NOT_SCOREABLE',
        independence_status='NOT_VERIFIED',exposure_status='NOT_VERIFIED',
        access_policy='EXACT_SIGNED_PERSONAL_REAL_EVAL_ONLY',historical_source_ref='personal-decision.json')
    DatasetRegistry([asset]).save_new(tmp_path/'personal-registry.json')
    cfg['registry_metadata']='personal-registry.json'
    cfg['experiment_config']['environment']['mode']='PERSONAL_REAL_EVAL'
    cfg['experiment_config']['environment']['classifier_semantic_failure_policy']='CASE_FAIL_CONTINUE_WHEN_FULLY_AUDITED'
    cfg['plan'].update(mode='PERSONAL_REAL_EVAL',task_scope_ref=event['event_id'],config_sha256=sha256_json(cfg['experiment_config']))
    cfg['plan']['runtime_bindings'].append(['personal-decision.json',raw_hash(tmp_path/'personal-decision.json')])
    cfg['approved_plan_hashes']=[sha256_json(cfg['plan'])]
    path.write_text(json.dumps(cfg),encoding='utf8')
    return cases,store,requests


def test_personal_full_cli_and_cross_segment_case_consumption(tmp_path,monkeypatch):
    cases,store,requests=personal(tmp_path,monkeypatch)
    cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    assert len(result['completed_cases'])==4 and result['stop_reason'] is None
    assert requests and result['budget']['active_reservations']==0
    assert any(o['task_success']=='FAIL' for o in result['outcomes'])
    cfg=json.loads((tmp_path/'deployment.json').read_text(encoding='utf8'));old=read_grant(json.loads((tmp_path/'test-grant.json').read_text(encoding='utf8')))
    plan=replace(old.plan,approval_ref='TEST_SUCCESSOR_SEGMENT')
    authority=FormalGrantAuthority(DatasetRegistry.load_metadata(tmp_path/'personal-registry.json'),repository_root=tmp_path,store=store,
        issuer=cfg['issuer'],trusted_issuers=set(cfg['trusted_issuers']),signing_key=b't'*32,
        verification_key=base64.b64decode(cfg['verification_public_key_base64']),approved_plan_hashes={sha256_json(asdict(plan))},allowed_targets={tuple(cfg['actual_target_binding'])})
    successor=authority.issue(plan)
    with pytest.raises(GrantDenied,match='already consumed in this personal task'):
        authority.claim(successor,case=cases[0],attempt_id='different-directory-or-segment',subject=plan.subject,mode=plan.mode,config_sha256=plan.config_sha256,target=tuple(cfg['actual_target_binding']))
    with pytest.raises(GrantDenied,match='eligibility'):
        authority._eligible(replace(plan,mode='FROZEN_EVAL'))


def test_personal_unknown_usage_stops_without_second_case(tmp_path,monkeypatch):
    cases,store,requests=personal(tmp_path,monkeypatch,unknown_usage=True)
    cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    assert result['stop_reason'] and len(result['consumed_cases'])==1
    assert result['budget']['token_status']=='UNKNOWN_USAGE'
    assert len(result['not_run'])==3


def test_personal_wrong_decision_source_rejected_before_app(tmp_path,monkeypatch):
    cases,store,requests=personal(tmp_path,monkeypatch)
    cfg=json.loads((tmp_path/'deployment.json').read_text(encoding='utf8'));cfg['plan']['task_scope_ref']='UNAPPROVED_OTHER_TASK'
    cfg['approved_plan_hashes']=[sha256_json(cfg['plan'])];(tmp_path/'deployment.json').write_text(json.dumps(cfg),encoding='utf8')
    with pytest.raises(GrantDenied,match='does not authorize'):cli(tmp_path,'issue')
    assert not requests and not store.trials(cfg['experiment_id'])


@pytest.mark.parametrize('actual_runs',[0,1])
@pytest.mark.parametrize('remote_prepare',[False,True,'PREPARED'])
@pytest.mark.parametrize('cleanup_valid',[False,True])
def test_zero_run_recovery_is_append_only_and_cannot_resample(tmp_path,monkeypatch,actual_runs,remote_prepare,cleanup_valid):
    cases,store,requests=personal(tmp_path,monkeypatch);cli(tmp_path,'issue')
    cfg=json.loads((tmp_path/'deployment.json').read_text(encoding='utf8'))
    old=read_grant(json.loads((tmp_path/'test-grant.json').read_text(encoding='utf8')))
    binding={'subject':old.plan.subject,'mode':old.plan.mode,'config_sha256':old.plan.config_sha256,'target':tuple(cfg['actual_target_binding'])}
    def authority(plan):return FormalGrantAuthority(DatasetRegistry.load_metadata(tmp_path/'personal-registry.json'),repository_root=tmp_path,store=store,
        issuer=cfg['issuer'],trusted_issuers=set(cfg['trusted_issuers']),signing_key=b't'*32,verification_key=base64.b64decode(cfg['verification_public_key_base64']),
        approved_plan_hashes={sha256_json(asdict(plan))},allowed_targets={tuple(cfg['actual_target_binding'])})
    a=authority(old.plan);a.claim(old,case=cases[0],attempt_id='original-local-failure',**binding)
    if remote_prepare:
        with store.transaction() as db:
            db.execute('CREATE TABLE container_attempts(trial_id TEXT,stage TEXT,receipt_json TEXT,result_json TEXT)')
            db.execute('INSERT INTO container_attempts VALUES(?,?,?,?)',('original-local-failure',
                'PREPARED' if remote_prepare=='PREPARED' else 'CLAIMED','{}' if remote_prepare=='PREPARED' else None,None))
    else:store.add_trial_event('local-failure-event','original-local-failure','EXECUTION_UNKNOWN',{'receipt_saved':False},old.plan.valid_from_utc)
    a.close(old)
    proof={'task_ref':old.plan.task_scope_ref,'case_id':cases[0].case_id,'target_identity':old.plan.target_identity,
        'previous_attempt_id':'original-local-failure','classification':'VERIFIED_REMOTE_PREPARATION_FAILED_BEFORE_RUN' if remote_prepare else 'VERIFIED_PRE_RPC_LOCAL_SETUP_FAILURE',
        'actual_runs':actual_runs,'provider_calls':0,'tool_calls':0,'remote_attempt_present':bool(remote_prepare),'lease_free':True,'case_file_present':bool(remote_prepare),
        'remote_stage':'PREPARED' if remote_prepare=='PREPARED' else 'CLAIMED','remote_result_present':False,
        'cleanup_evidence':{'residual_case_rows':0,'pending_actions':0,'lease_released':cleanup_valid}}
    write_new(tmp_path/'recovery.json',proof)
    plan=replace(old.plan,approval_ref='TEST_RECOVERY',runtime_bindings=(*old.plan.runtime_bindings,('recovery.json',raw_hash(tmp_path/'recovery.json'))),
        pre_run_recoveries=((cases[0].case_id,'recovery.json',raw_hash(tmp_path/'recovery.json')),))
    a=authority(plan);grant=a.issue(plan)
    if remote_prepare=='PREPARED' and not cleanup_valid:
        with pytest.raises(GrantDenied,match='exact cleanup'):a.claim(grant,case=cases[0],attempt_id='next',**binding)
    elif actual_runs:
        with pytest.raises(GrantDenied,match='zero-execution'):a.claim(grant,case=cases[0],attempt_id='next',**binding)
    else:
        a.claim(grant,case=cases[0],attempt_id='next',**binding)
        with pytest.raises(GrantDenied,match='already consumed'):a.claim(grant,case=cases[0],attempt_id='third',**binding)
        with store.transaction(immediate=False) as db:
            assert db.execute('SELECT count(*) FROM personal_task_attempts').fetchone()[0]==1
            assert db.execute('SELECT count(*) FROM personal_task_recovery_claims').fetchone()[0]==1
    assert not requests


def test_issuer_preflight_does_not_create_controller_artifact_directory(tmp_path):
    from types import SimpleNamespace
    from eval.m6_1.container_boundary import ContainerApplication
    from eval.m6_1.store import ResultStore
    store=ResultStore(tmp_path/'ledger'/'results.sqlite3')
    worker=ContainerApplication(config={'experiment_config':{'environment':{}}},store=store,ledger=None,
        mode_gate=SimpleNamespace(formal_authority=SimpleNamespace(root=tmp_path)),grant=None)
    assert not worker.artifacts.exists()


@pytest.mark.parametrize('usage_known',[True,False])
def test_full_cli_known_semantic_rejection_continues_only_with_usage_and_audit(tmp_path,monkeypatch,usage_known):
    from types import SimpleNamespace
    from app.m0 import config as app_config
    cases,store,requests=personal(tmp_path,monkeypatch,scope_rejection=True)
    class SDK:
        def __init__(self):self.chat=self;self.completions=self
        def with_options(self,**kwargs):return self
        def create(self,**kwargs):
            requests.append(kwargs)
            output=json.dumps({'strategy':'DIRECT','sub_strategy':None,'intent_labels':['clarification'],
                'safety_decision':'ALLOW','reason_code':'TEST_SEMANTIC_MISMATCH','decision_source':'LLM','needs_clarification':False,'required_capabilities':['search_knowledge']})
            return SimpleNamespace(model='deepseek-flash',choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=output))],
                usage=SimpleNamespace(total_tokens=50,model_dump=lambda:{'prompt_tokens':30,'completion_tokens':20,'total_tokens':50}) if usage_known else None)
    monkeypatch.setattr(app_config,'create_deepseek_client',lambda *args,**kwargs:SDK())
    cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    if usage_known:
        detail=store.raw_result(store.trials('unprotected-full-app-test')[0]['trial_id'])['code_assessment']
        assert len(result['completed_cases'])==4 and not result['stop_reason'], {'route':detail['route'],'integrity':detail['integrity'],'blocking':detail['blocking_items']}
        first=store.raw_result(store.trials('unprotected-full-app-test')[0]['trial_id'])['code_assessment']
        assert first['known_classifier_semantic_rejection'] and first['checks']['route_reference']=='FAIL'
        assert first['integrity']['audit']=='PASS'
    else:assert result['stop_reason'] and len(result['consumed_cases'])==1


def test_natural_chinese_order_identifier_uses_real_tool_grammar():
    from app.m1.routing import _order_identifier_present,decide_route
    from app.m3.order_tools import requested_order_id
    for query in ('本人订单号为ORD-SANDBOX-481。','查本人订单ORD_SANDBOX_481的记录','own order ORD-SANDBOX-481'):
        assert _order_identifier_present(query)
        assert requested_order_id(query)[0]
    assert decide_route('查询本人订单号为ORD-SANDBOX-481的记录状态',available_capabilities=['get_order']).required_capabilities==['get_order']
    assert not _order_identifier_present('这是PREORD-SANDBOX-481说明')
    assert not _order_identifier_present('请查订单号为ORD-。')


def test_natural_chinese_order_full_cli_reads_owner_fact_not_fixture_injection(tmp_path,monkeypatch):
    import test_m6_2_r24_formal_application as support
    base=support.fictional_cases
    def natural():
        rows=base();c=rows[2]
        rows[2]=replace(c,input={**dict(c.input),'user_query':'查询本人订单号为ORD-EXAMPLE-901的记录状态。'})
        return rows
    monkeypatch.setattr(support,'fictional_cases',natural)
    cases,store,requests=personal(tmp_path,monkeypatch);cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    assert not result['stop_reason'] and len(result['completed_cases'])==4
    trial=next(t for t in store.trials('unprotected-full-app-test') if t['case_id']==cases[2].case_id)
    raw=store.raw_result(trial['trial_id']);calls=raw['application_archive']['read_invocations']
    assert len(calls)==1 and calls[0]['operation']=='get_order'
    assert calls[0]['arguments']['order_id']=='ORD-EXAMPLE-901'
    assert raw['code_assessment']['integrity']['permission']=='PASS'


def test_sparse_order_action_setup_keeps_unknown_and_real_repository_readable(tmp_path):
    from types import SimpleNamespace
    from copy import deepcopy
    from test_m6_2_r24_formal_application import fictional_cases
    from eval.m6_1.formal_application import application_plan
    from eval.m6_1.personal_dev_database import PersonalDevDatabase
    from eval.m6_1.personal_dev import D1Coordinator
    from eval.m6_1.formal_actions import materialize_action_facts,execute_owner_interaction,assess_interaction,incomplete_core_action_fields
    from sqlalchemy import select,func
    from app.m1.models import OrderItem,Product
    from eval.m6_1.grants import formal_case_payload
    payload=formal_case_payload(fictional_cases()[2]);value=payload['input'];value['fixture']=deepcopy(value['fixture'])
    value['fixture']['orders_and_snapshots'][0].update(status='CREATED',paid_cents=None)
    value['fixture']['products']=[{'id':'sparse-item','name':'Fictional sparse material','category':'accessory','price_cents':120}]
    value['fixture']['order_items']=[{'id':'sparse-line','order_id':'fiction-order-source','product_id':'sparse-item','quantity':2,'paid_cents':120}]
    value['fixture_refs']+=['sparse-item','sparse-line']
    value['owner_interaction']={'tool_id':'cancel_order','target_resource_id':'ORD-EXAMPLE-901','arguments':{'order_id':'ORD-EXAMPLE-901','reason_code':'OTHER','reason_summary':'Explicit isolated request'},'decision':'CONFIRM'}
    from eval.m6_1.contracts import EvalCase
    case=EvalCase.from_dict(payload);target=PersonalDevDatabase(tmp_path);engine,factory=target.open(initialize=True);coordinator=D1Coordinator(target,factory)
    plan=application_plan(case);receipt=coordinator.prepare(plan)
    try:
        assert receipt.core_order_ids==()
        # This unit exercises real SQLite fixture/repository/schema writes;the
        # profile tag substitutes only the PG-only guard,not a real-PG claim.
        worker=SimpleNamespace(target=SimpleNamespace(profile='M6.2-FORMAL-POSTGRES'),session_factory=factory,coordinator=coordinator,env={'simulated_action_tools':['cancel_order']})
        materialize_action_facts(worker,case,receipt)
        repositories=coordinator.repositories(plan,receipt,maximum=4)
        source=repositories['order'].get_order('ORD-EXAMPLE-901','formal-fixture-owner')
        assert source['paid_cents'] is None and source['status']=='CREATED'
        with factory() as db:assert db.scalar(select(func.count()).select_from(OrderItem))==0 and db.scalar(select(func.count()).select_from(Product))==0
        evidence=execute_owner_interaction(worker,case,receipt,{'status':'SUCCEEDED'},None,'offline-no-run',remaining_seconds=30)
        assert evidence['status']=='NOT_EXECUTED_CORE_FACTS_INCOMPLETE' and evidence['calls']==[]
        grade=assess_interaction(case,evidence)
        assert grade['task']=='FAIL' and grade['permission']==grade['audit']=='PASS'
        damaged=deepcopy(evidence);damaged['snapshot']['order_items']=[{'id':'unexpected'}]
        assert assess_interaction(case,damaged)['audit']=='UNKNOWN'
        complete=deepcopy(payload);complete['input']['fixture']['orders_and_snapshots'][0]['paid_cents']=120
        with pytest.raises(ValueError,match='complete source'):incomplete_core_action_fields(EvalCase.from_dict(complete),receipt)
    finally:coordinator.cleanup(receipt);engine.dispose()


def test_rule_outside_read_scope_full_cli_audited_failure_continues(tmp_path,monkeypatch):
    import test_m6_2_r24_formal_application as support
    base=support.fictional_cases
    def action_request():
        rows=base();c=rows[2]
        rows[2]=replace(c,input={**dict(c.input),'user_query':'请帮我提交退款申请，订单号ORD-EXAMPLE-901。'})
        return rows
    monkeypatch.setattr(support,'fictional_cases',action_request)
    cases,store,requests=personal(tmp_path,monkeypatch);cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    trial=next(t for t in store.trials('unprotected-full-app-test') if t['case_id']==cases[2].case_id)
    raw=store.raw_result(trial['trial_id']);grade=raw['code_assessment']
    assert not result['stop_reason'] and len(result['completed_cases'])==4,raw
    assert raw['application_archive']['result']['status']=='FAILED'
    assert raw['application_archive']['read_invocations']==[]
    assert grade['task_outcome']=='FAIL' and grade['integrity']['audit']=='PASS'
    assert 'LOCAL_RULE_CAPABILITY_CHOICE_BLOCKED_ZERO_EXECUTION' in grade['failure_categories']
    assert result['budget']['active_reservations']==0


def test_warranty_selected_citation_subset_full_application_and_wrong_refs(tmp_path,monkeypatch):
    from copy import deepcopy
    from eval.m6_1.personal_dev_batch_m62 import _warranty_composite_result_is_linked
    cases,store,requests=personal(tmp_path,monkeypatch,multi=True)
    cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    assert not result['stop_reason'] and len(result['completed_cases'])==5
    trial=next(t for t in store.trials('unprotected-full-app-test') if t['case_id']=='unprotected-multi')
    archive=store.raw_result(trial['trial_id'])['application_archive']
    assert _warranty_composite_result_is_linked(archive)
    bad=deepcopy(archive);bad['result']['result']['business_results']['policy']['evidence_ids']=['wrong-unretrieved-source']
    assert not _warranty_composite_result_is_linked(bad)
    bad=deepcopy(archive);bad['result']['result']['business_results']['policy']['knowledge_version']='wrong-version'
    assert not _warranty_composite_result_is_linked(bad)
    bad=deepcopy(archive);bad['result']['result']['tool_references']=[]
    assert not _warranty_composite_result_is_linked(bad)


def test_duration_tokens_do_not_parse_fragments_of_reference_identifiers():
    from app.m2.self_rag import _policy_duration_matches,_answer_semantic_issues
    assert not _policy_duration_matches('证据REF-407-DOC-01；ABC-003-D；CODE32D')
    assert [m.group('amount') for m in _policy_duration_matches('after 3D; within 7-days;送达后7天；保修12个月')]==['3','7','7','12']
    evidence=[{'chunk_id':'source-abc','content':'Warranty lasts 18 months from recorded purchase.'}]
    assert not _answer_semantic_issues('Warranty lasts 18 months from recorded purchase. (REF-407-DOC-01)',evidence)
    assert _answer_semantic_issues('Warranty lasts 72 months from recorded purchase. (REF-407-DOC-01)',evidence)


def test_reference_identifier_in_complete_answer_full_cli_not_duration(tmp_path,monkeypatch):
    from app.m0 import config as app_config
    cases,store,requests=personal(tmp_path,monkeypatch)
    original=app_config.create_deepseek_client
    sdk=original()
    create=sdk.create
    def response(**kwargs):
        value=create(**kwargs);output=json.loads(value.choices[0].message.content)
        output['answer']+=' (REF-407-DOC-01)'
        value.choices[0].message.content=json.dumps(output)
        return value
    sdk.chat.completions.create=response
    monkeypatch.setattr(app_config,'create_deepseek_client',lambda *a,**k:sdk)
    cli(tmp_path,'issue');result=cli(tmp_path,'execute')
    assert not result['stop_reason'] and len(result['completed_cases'])==4
    raw=store.raw_result(store.trials('unprotected-full-app-test')[0]['trial_id'])
    assert raw['code_assessment']['checks']['delivery']=='PASS' and raw['code_assessment']['candidate'] is None
