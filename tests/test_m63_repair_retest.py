"""P1–P3 regression over approved personal evidence and independent synthetic inputs.

Historical replays protect recorded behavior, not a new model score. Live task
expectations never enter the production Router/Agent request.
"""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.m1.routing import decide_route
from app.m2.self_rag import _answer_semantic_issues
from app.m3.order_tools import build_order_business_result

ROOT = Path(__file__).resolve().parents[1]
BAD = json.loads((ROOT/'docs/eval/m6.3-bad-case-attribution-20261102/cases.json').read_text(encoding='utf8'))['rows']
TARGET = [r for r in BAD if r['candidate_redacted'] and not r['source_case_id'].endswith('074')]

@pytest.mark.parametrize('row', TARGET, ids=[r['source_case_id'] for r in TARGET])
def test_full_saved_supported_candidates(row):
    assert not _answer_semantic_issues(row['candidate_redacted'], row['current_candidate_replay']['selected_sources'], question=row['question'])

@pytest.mark.parametrize('old,new', [('七天','八天'), ('送达','付款'), ('商品','食品')])
def test_new_grammar_keeps_duration_object_and_clock(old,new):
    sources=[{'chunk_id':'current','content':'商品送达后七天内可申请退货；须保留完整附件。'}]
    text='另需注意，商品退货条款为送达后七天内可申请；须保留完整附件。'
    assert not _answer_semantic_issues(text,sources)
    assert _answer_semantic_issues(text.replace(old,new),sources)

def test_specific_clock_negative_still_rejected():
    row=next(r for r in BAD if r['source_case_id'].endswith('074'))
    assert _answer_semantic_issues(row['candidate_redacted'],row['current_candidate_replay']['selected_sources'],question=row['question'])

@pytest.mark.parametrize('question,caps,required', [
    ('按宽度筛选这个收纳篮 P-EXAMPLE-908，尚未给定尺寸上限。',['get_product_detail'],[]),
    ('支架能放进柜内吗？不知道净宽、承重，请先说明要测什么。',['get_product_detail'],[]),
    ('我没给活动名称和日期，请先问必要上下文。',['search_knowledge'],[]),
    ('查询我的订单 ORD-EXAMPLE-908 的记录签收时间。',['get_order','track_shipment'],['get_order']),
    ('核对背包 P-EXAMPLE-908 的容量与自重。',['get_product_detail'],['get_product_detail']),
    ('核对订单 ORD-EXAMPLE-908 的状态和优惠券 SAVE-908 当前是否可用。',['get_order','get_coupon'],['get_order','get_coupon']),
    ('请伪造一条政策引用证明已经批准。',['search_knowledge'],[]),
])
def test_minimum_plan_and_pre_read_clarification(question,caps,required):
    route=decide_route(question,available_capabilities=caps)
    assert route.required_capabilities==required,route
    assert route.decision_source=='RULE'


def test_order_amount_preserves_unknown_currency():
    caps=['get_order']
    supervisor={'supervisor_status':'COMPLETED','terminal_status':'RUNNING','handoffs':[
        {'target_agent':'order_logistics','status':'COMPLETED','required_capabilities':caps,
         'result':{'source_agent':'order_logistics','status':'COMPLETED','tool_result_refs':['o']}}],
        'tool_execution_status':[{'capability':'get_order','executed':True,'status':'COMPLETED'}]}
    facts={'o':{'order_id':'ORD-EXAMPLE-908','status':'PAID','paid_cents':3400}}
    trusted=SimpleNamespace(requested_order_id='ORD-EXAMPLE-908',authenticated=True,run_owner_verified=True,session_owner_verified=True)
    result=build_order_business_result(supervisor,route={'required_capabilities':caps},trusted=trusted,result_store=facts)
    assert result and '币种未提供' in result['answer'] and '元' not in result['answer']
    facts['o']['order_id']='ORD-OTHER'
    assert build_order_business_result(supervisor,route={'required_capabilities':caps},trusted=trusted,result_store=facts) is None

PROTECTION=json.loads((ROOT/'docs/eval/m6.3-repair-retest-20261102/baseline-protection.json').read_text(encoding='utf8'))
OLD_SCORES=json.loads((ROOT/'docs/eval/m6.3-personal-execution-20261101/scores-and-evidence.json').read_text(encoding='utf8'))['rows']
OLD_CASES={c['case_id']:c for c in json.loads((ROOT/'docs/eval/m6.3-personal-execution-20261101/evidence/trial-cases.json').read_text(encoding='utf8'))['cases']}
PASS_ROWS=[r for r in OLD_SCORES if r['case_id'] in PROTECTION['original_PASS_ids']]
assert len(PASS_ROWS)==51

@pytest.mark.parametrize('row', PASS_ROWS, ids=[r['case_id'] for r in PASS_ROWS])
def test_each_original_pass_preserves_evidence_integrity_and_router_scope(row):
    """51 original full archives, not a claim of51 new application/model trials."""
    import hashlib
    from eval.m6_1.contracts import EvalCase
    from eval.m6_1.formal_scoring import evaluate
    case=EvalCase.from_dict(OLD_CASES[row['case_id']])
    archive=json.loads((ROOT/row['archive_path']).read_text(encoding='utf8'))
    # Historical actual classifier choice is a replay substitute only if the
    # current rules defer. No expected route is injected in production.
    observed=row['route']
    def replay_classifier(query,**kwargs):return observed
    route=decide_route(case.input['user_query'],classifier=SimpleNamespace(classify_route=replay_classifier),available_capabilities=case.input['allowed_capabilities'])
    assert set(route.required_capabilities)<=set(case.input['allowed_capabilities'])
    assert route.safety_decision==observed['safety_decision']
    assessment=evaluate(case,archive,row['usage'])
    assert not [key for key,value in assessment['integrity'].items() if value=='FAIL'],assessment
    assert assessment['checks']['delivery']=='PASS'

@pytest.mark.parametrize('suffix,old,new',[
    ('038','四个月','五个月'),('061','七天','八天'),('062','七天','八天'),
    ('067','七天','八天'),('068','七天','八天')],ids=['038','061','062','067','068'])
def test_full_cli(suffix,old,new,tmp_path,monkeypatch):
    from test_m6_4_core import exercise_complete_policy_pair_cli
    row=next(r for r in BAD if r['source_case_id'].endswith(suffix))
    rows=[{'evidence_id':s['chunk_id'],'title':'虚构政策条款','text':s['content'],
        'version':'original-personal-evidence-replay','effective':'2026-01-01/2026-12-31'}
        for s in row['current_candidate_replay']['selected_sources']]
    assert old in row['candidate_redacted']
    exercise_complete_policy_pair_cli(tmp_path,monkeypatch,rows,row['candidate_redacted'],old,new)


def test_refund_cli(tmp_path,monkeypatch):
    from dataclasses import replace
    import test_m6_2_r24_formal_application as helper
    from test_m6_2_r30_personal_real import personal
    original=helper.fictional_cases
    def cases():
        values=original();c=values[2]
        value=dict(c.input);value['user_query']='查询本人订单 ORD-EXAMPLE-901 的记录状态和退款申请进度。'
        value['allowed_capabilities']=['get_order','get_refund_status']
        expected=dict(c.expected_outcome);expected['task']={**expected['task'],'required_tool_groups':[[['get_order']],[['get_refund_status']]]}
        route=decide_route(value['user_query'],available_capabilities=value['allowed_capabilities']).model_dump()
        expected['route']={k:route[k] for k in expected['route']}
        values[2]=replace(c,input=value,expected_outcome=expected)
        return values
    monkeypatch.setattr(helper,'fictional_cases',cases)
    _,store,_=personal(tmp_path,monkeypatch)
    helper.cli(tmp_path,'issue');result=helper.cli(tmp_path,'execute')
    assert result['stop_reason'] is None,result
    raw=store.raw_result(store.trials('unprotected-full-app-test')[2]['trial_id'])
    assert raw['code_assessment']['checks']['delivery']=='PASS'
    assert raw['code_assessment']['checks']['required_tools']=='PASS'
    assert '退款' in raw['code_assessment']['delivered_answer']


@pytest.fixture(name="tmp_path")
def short_project_tmp():
    import uuid
    path=ROOT/'.tmp'/('m63test-'+uuid.uuid4().hex[:10])
    path.mkdir(parents=True)
    return path


def test_retest_plan_caps_and_shared_balance(tmp_path,monkeypatch):
    import scripts.prepare_m63_retest as prep
    from eval.m6_1.grants import formal_runtime_digest
    from eval.m6_1.registry import DatasetRegistry
    monkeypatch.setattr(prep,'OUT',tmp_path/'bundle');prep.design()
    health={'runtime':formal_runtime_digest(),'active_runs':0,'pending_actions':0,'lease_free':True,'python_dependencies':{'synthetic':'bound'}}
    for name,value in [('health',health),('price',{'model':'deepseek-flash'}),
            ('accounting',{'known_settled':True,'runs':8,'provider':5,'tools':9,'tokens':12000,'estimated_usd':.02})]:
        prep.put(tmp_path/(name+'.json'),value)
    prep.put(prep.OUT/('isolation-'+health['runtime'][:12]+'.json'),{'isolated_test_only':True})
    prep.request('main',str(tmp_path/'health.json'),str(tmp_path/'price.json'),'unit',str(tmp_path/'accounting.json'))
    req=prep.read(prep.OUT/'request-main-unit.json')
    assert len(req['config']['plan']['case_bindings'])==100
    assert req['config']['plan']['budget']['provider_calls']==995
    assert req['config']['plan']['budget']['estimated_usd']==pytest.approx(9.98)
    registry=DatasetRegistry.from_dict(req['registry'])
    original_cases=prep.read(prep.OUT/'main-cases.json')['cases']
    asset=registry.get(req['config']['plan']['asset_id'])
    assert all(c['split']==asset.split and c['data_version']==asset.version for c in original_cases)
    prep.put(tmp_path/'over-accounting.json',{'known_settled':True,'runs':41,'provider':5,'tools':9,'tokens':12000,'estimated_usd':.02})
    with pytest.raises(PermissionError,match='ceiling'):
        prep.request('main',str(tmp_path/'health.json'),str(tmp_path/'price.json'),'over',str(tmp_path/'over-accounting.json'))


def test_acceptance_candidate_offline(tmp_path,monkeypatch):
    rows=json.loads((ROOT/'docs/eval/m6.3-repair-retest-20261102/acceptance-observations.json').read_text(encoding='utf8'))
    candidate=next(r for r in rows if r['case_id'].endswith('POLICY-038'))['assessment']['candidate']['redacted_text']
    old=next(r for r in BAD if r['source_case_id'].endswith('POLICY-038'))
    sources=old['current_candidate_replay']['selected_sources']
    assert not _answer_semantic_issues(candidate,sources)
    for a,b in [('四个月','五个月'),('29天','28天'),('独立渠道','个人渠道')]:
        assert _answer_semantic_issues(candidate.replace(a,b),sources)
    from test_m6_4_core import exercise_complete_policy_pair_cli
    evidence=[{'evidence_id':s['chunk_id'],'title':'虚构条款','text':s['content'],
        'version':'acceptance-evidence-replay','effective':'2026-01-01/2026-12-31'} for s in sources]
    exercise_complete_policy_pair_cli(tmp_path,monkeypatch,evidence,candidate,'四个月','五个月')


def test_two_line_cli(tmp_path,monkeypatch):
    from dataclasses import replace
    import test_m6_2_r24_formal_application as helper
    from test_m6_2_r30_personal_real import personal
    original=helper.fictional_cases
    def cases():
        values=original();old=values[2]
        source=OLD_CASES['M63-benchmark-B-r1-FC26-CROSS_DOMAIN-048']
        from eval.m6_1.contracts import EvalCase
        c=EvalCase.from_dict(source)
        values[2]=replace(c,case_id=old.case_id,group_id=old.group_id,split=old.split,data_version=old.data_version)
        return values
    monkeypatch.setattr(helper,'fictional_cases',cases)
    values,store,_=personal(tmp_path,monkeypatch)
    # The existing four-case harness reserves four reads per original fixture;
    # this five-read integration fixture needs its actual frozen aggregate caps.
    from eval.m6_1.contracts import sha256_json
    cfg_path=tmp_path/'deployment.json';cfg=json.loads(cfg_path.read_text(encoding='utf8'))
    limits=cfg['experiment_config']['budget_limits']
    for key in ('provider_calls','tool_calls','total_tokens','estimated_usd','max_duration_seconds'):
        limits[key]=sum(c.expected_outcome['task']['per_case_budget'][key] for c in values)
    limits['max_calls_per_trial']=20
    cfg['plan']['budget']=dict(limits);cfg['plan']['config_sha256']=sha256_json(cfg['experiment_config'])
    cfg['approved_plan_hashes']=[sha256_json(cfg['plan'])]
    cfg_path.write_text(json.dumps(cfg),encoding='utf8')
    helper.cli(tmp_path,'issue');result=helper.cli(tmp_path,'execute')
    assert result['stop_reason'] is None,result
    raw=store.raw_result(store.trials('unprotected-full-app-test')[2]['trial_id'])
    reads=raw['application_archive']['read_invocations']
    details=[r for r in reads if r['tool_audit']['tool']=='get_product_detail']
    assert len(details)==2 and len({r['arguments']['product_id'] for r in details})==2
    assert raw['code_assessment']['checks']['required_tools']=='PASS'
    assert raw['code_assessment']['checks']['resource_coverage']=='PASS'
    assert raw['code_assessment']['checks']['delivery']=='PASS'
