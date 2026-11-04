"""Complete real rejected candidates and counterexamples, not sentence allowlists."""
import json
import socket
from pathlib import Path
import pytest
from app.m2.self_rag import _answer_semantic_issues
from test_m63_repair_retest import short_project_tmp as tmp_path

BASE=Path('docs/eval/m6.3-faithfulness-and-retrieval-r1-20261102')
ANSWERS={r['case_id']:r for r in json.loads((BASE/'answer-reviews.json').read_text(encoding='utf8'))}
CANDIDATES=json.loads((BASE/'candidate-reviews.json').read_text(encoding='utf8'))
SUPPORTED=[r for r in CANDIDATES if r['whole_candidate_judgment']=='CONFIRMED_FALSE_REJECTION_AI']
OFFLINE_GUARDS=(socket.socket.connect,socket.socket.connect_ex,socket.create_connection,socket.getaddrinfo)


@pytest.fixture(autouse=True)
def keep_runner_offline_guard_for_async_wakeup(monkeypatch):
    # The historical report loader intentionally locks its whole process down.
    # Other files call it during pytest. Restore this runner's original denial
    # policy (which permits only asyncio's private socketpair construction),
    # never the unrestricted OS socket methods, for the actual ASGI CLI tests.
    for target,name,value in zip((socket.socket,socket.socket,socket,socket),
            ('connect','connect_ex','create_connection','getaddrinfo'),OFFLINE_GUARDS):
        monkeypatch.setattr(target,name,value)

def evidence(row):
    return [s for s in ANSWERS[row['case_id']]['policy_sources'] if s['chunk_id'] in row['candidate']['citation_ids']]


LATEST=json.loads(Path('docs/eval/m6.3-targeted-four-cause-repair-r1-20261102/scores-and-evidence.json').read_text(encoding='utf8'))['rows']


def latest_candidate(suffix):
    row=next(r for r in LATEST if r['source_case_id'].endswith(suffix))
    return row,[s for s in row['policy_sources'] if s['chunk_id'] in row['candidate']['citation_ids']]


@pytest.mark.parametrize('suffix',['CROSS_DOMAIN-054','POLICY-043'])
def test_latest_complete_candidates_and_different_surface_forms(suffix):
    row,sources=latest_candidate(suffix);text=row['candidate']['redacted_text']
    assert not _answer_semantic_issues(text,sources,row['question'])
    assert not _answer_semantic_issues(text.replace('「','').replace('」','').replace('：',':'),sources,row['question'])


def test_calendar_denial_is_not_a_positive_duration_and_incomplete_rule_stays_rejected():
    row,sources=latest_candidate('EXTRA-096');text=row['candidate']['redacted_text']
    findings=_answer_semantic_issues(text,sources,row['question'])
    assert findings  # missing resale/accessories conditions are not filled from evidence
    assert not any(i.get('window') in {'7个月','1天'} for i in findings)
    complete=('2026年7月1日是日历上的日期节点，本身不表示七个月或一天这样的持续时长。'
        '商品送达后七天内可申请退货；须未影响二次销售并保留完整附件，申请仍需审核。')
    assert not _answer_semantic_issues(complete,sources,row['question'])
    for wrong in [complete.replace('七天内','八天内'),complete.replace('商品送达','食品送达'),
            complete.replace('须未影响二次销售并保留完整附件','须保留完整附件'),
            complete+'送达后七个月可申请退货。']:
        assert _answer_semantic_issues(wrong,sources,row['question'])


@pytest.mark.parametrize('old,new',[
    ('送达后七天','送达后八天'),('送达后七天','送达后七个月'),
    ('商品本体','食品本体'),('送达后','付款后'),('完好条件','破损条件')])
def test_wrapping_window_wrong_value_clock_object_condition_still_rejected(old,new):
    row,sources=latest_candidate('POLICY-043');text=row['candidate']['redacted_text']
    assert old in text
    assert _answer_semantic_issues(text.replace(old,new),sources,row['question'])


def test_workflow_enumeration_does_not_authorize_actual_approval_or_settlement():
    row,sources=latest_candidate('CROSS_DOMAIN-054');text=row['candidate']['redacted_text']
    for assertion in ['退款已经到账。','退货已经批准。','不能视为已到账，但退款已经到账。']:
        assert _answer_semantic_issues(text+assertion,sources,row['question'])


def test_channel_scoped_conclusion_cannot_become_a_universal_clock():
    row=LATEST[9];sources=row['policy_sources'];text=row['answer']
    assert any(i['code']=='ANSWER_POLICY_CHANNEL_SCOPE_UNRESOLVED' for i in _answer_semantic_issues(text,sources,row['question']))
    bounded=text.replace('因此就你问的触发点而言，通用答案是购买日期。','因此购买日期只对应前述延保条款，不据此选择不同渠道。')
    assert not _answer_semantic_issues(bounded,[s for s in sources if s['chunk_id'] in {c['chunk_id'] for c in row['citations']}],row['question'])


def test_flat_request_scope_and_tool_contracts_survive_premature_finish(tmp_path,monkeypatch):
    import test_m63_retest_round2 as shared
    seen=[]
    def model(payload):
        seen.append(payload)
        out={'business_answer':'','policy_answer':'','citations':[],'business_result_refs':[],
             'kind':'tool','tool':'get_order','arguments':{'order_id':'ORD-FC26-028'}}
        if payload['step']==1:return out
        if payload['step']==2:return {**out,'kind':'finish','tool':None,'arguments':{},'business_answer':'订单已付款，未核验退货。'}
        assert payload['requested_capabilities']==['get_refund_status']
        assert '退货/退款申请' in payload['tool_contracts']['get_refund_status']['observes']
        assert payload['tool_contracts']['get_refund_status']['argument']=='order_id'
        if payload['unobserved_requested_scope']:
            return {**out,'tool':'get_refund_status'}
        return {**out,'kind':'finish','tool':None,'arguments':{},'business_answer':'退货申请记录为UNDER_REVIEW；不是退款完成。',
            'business_result_refs':[v['result_ref'] for v in payload['tool_results']]}
    raw=shared.exercise_current_application('AFTER_SALES-028',tmp_path,monkeypatch,architecture='SINGLE',controller_response=model)
    assert len(seen)==4 and seen[2]['planning_feedback']
    records=raw['application_archive']['result']['observations']['observability_records']
    steps=[json.loads(r['payload_json']) for r in records if r['record_type']=='log'
        and json.loads(r['payload_json'])['event_type'].startswith('agent_plan_step.')]
    assert len(steps)==4


@pytest.mark.parametrize('name',['POLICY-043','CROSS_DOMAIN-054','EXTRA-096'])
def test_latest_candidate_through_actual_cli(tmp_path,monkeypatch,name):
    import test_m63_retest_round2 as shared
    row,_=latest_candidate(name)
    monkeypatch.setitem(shared.MAIN,name,{**shared.MAIN[name],'candidate':row['candidate']})
    shared.exercise_current_application(name,tmp_path,monkeypatch,expected_candidate_rejection=name=='EXTRA-096')

@pytest.mark.parametrize('row',SUPPORTED,ids=[r['case_id'] for r in SUPPORTED])
def test_complete_supported_real_policy_candidate(row):
    assert not _answer_semantic_issues(row['candidate']['redacted_text'],evidence(row),ANSWERS[row['case_id']]['requirements'][0])

@pytest.mark.parametrize('old,new',[
 ('十二小时','十三小时'),('十二小时','十二个月'),('付款后','送达后'),
 ('刻字订单','食品订单'),('确认图稿之前','确认图稿之后')])
def test_full_window_wrong_amount_unit_clock_object_condition(old,new):
    row=next(c for c in SUPPORTED if c['case_id']=='M63R3-main-B-r1-FC26-POLICY-030')
    text=row['candidate']['redacted_text'];assert old in text
    assert _answer_semantic_issues(text.replace(old,new),evidence(row),ANSWERS[row['case_id']]['requirements'][0])

def test_calendar_negative_does_not_hide_positive_wrong_duration():
    row=next(c for c in SUPPORTED if c['case_id']=='M63R3-main-B-r1-FC26-EXTRA-096')
    text=row['candidate']['redacted_text']
    assert _answer_semantic_issues(text+'送达后八天可申请。',evidence(row),'日期与期限是否同一事实？')


def test_english_equivalent_and_changed_prerequisite():
    row=next(c for c in SUPPORTED if c['case_id']=='M63R3-main-B-r1-FC26-POLICY-030')
    text='The engraving order may be cancelled within 12 hours after payment, before artwork confirmation.'
    assert not _answer_semantic_issues(text,evidence(row),'What is the engraving cancellation window?')
    assert _answer_semantic_issues(text.replace('before artwork','after artwork'),evidence(row),'What is the engraving cancellation window?')

def test_workflow_non_assertion_cannot_hide_affirmative_settlement():
    row=next(c for c in SUPPORTED if c['case_id']=='M63R3-main-B-r1-FC26-POLICY-036')
    text=row['candidate']['redacted_text']
    assert _answer_semantic_issues(text+'退款已经到账。',evidence(row),'团购条件是什么？')
    assert _answer_semantic_issues('不能视为已经到账但退款已经到账。',evidence(row),'团购条件是什么？')


@pytest.mark.parametrize('name',['POLICY-030','POLICY-036','POLICY-043','EXTRA-063','EXTRA-094','EXTRA-096'])
def test_actual_policy_cli_repository_validation_persistence_cleanup(name,tmp_path,monkeypatch):
    import test_m63_retest_round2 as shared
    candidate=next(c for c in SUPPORTED if c['source_case_id']=='FC26-'+name)
    # Replay the reviewed third-round candidate, not an older different answer
    # that can omit independent channel prerequisites.
    monkeypatch.setitem(shared.MAIN,name,{**shared.MAIN[name],'candidate':candidate['candidate']})
    shared.exercise_current_application(name,tmp_path,monkeypatch)


def test_flat_actual_cli_cannot_finish_after_only_one_comparison_read(tmp_path,monkeypatch):
    import test_m63_retest_round2 as shared
    from app.m3.product_tools import parse_product_request_many
    seen_feedback=[];premature=[False]
    def model(payload):
        ids,_,_=parse_product_request_many(payload['question'])
        values=[v for v in payload['tool_results'] if v['tool']=='get_product_detail']
        seen={v['data']['product']['product_id'] for v in values}
        missing=[pid for pid in ids if pid not in seen]
        out={'business_answer':'','policy_answer':'','citations':[],'business_result_refs':[],
             'kind':'tool','tool':'get_product_detail','arguments':{}}
        if values and not premature[0]:
            premature[0]=True
            return {**out,'kind':'finish','tool':None,'business_answer':'另一商品未查，请确认是否继续。'}
        if payload['planning_feedback']:seen_feedback.extend(payload['planning_feedback'])
        if missing:
            out['arguments']={'product_id':missing[0]};return out
        weights=[v['data']['product']['recorded_attributes']['weight_g'] for v in values]
        return {**out,'kind':'finish','tool':None,'business_answer':f'目录记录重量分别{weights[0]}克和{weights[1]}克，差{abs(weights[1]-weights[0])}克。',
                'business_result_refs':[v['result_ref'] for v in values]}
    shared.exercise_current_application('PRODUCT-001',tmp_path,monkeypatch,architecture='SINGLE',controller_response=model)
    assert premature[0] and seen_feedback


def test_sources_transmit_minor_units_and_catalog_scope():
    from types import SimpleNamespace
    from app.m3.single_agent import _sources,response_instruction
    value={'source':'shoprelay_product_catalog','availability':'AVAILABLE','product':{'price_cents':1234}}
    context=SimpleNamespace(tool_execution_records=[{'tool':'get_product_detail','result_ref':'r'}],tool_result_store={'r':value})
    values,_=_sources(context)
    assert values[0]['data']==value
    assert 'does not establish inventory' in values[0]['field_semantics']['availability']
    assert 'minor monetary' in values[0]['field_semantics']['price_cents/paid_cents']
    assert '购买起算不代表可以省略登记前提' in response_instruction()


def test_targeted_design_uses_existing_entry_and_exact_predeclared_scope(tmp_path,monkeypatch):
    from scripts import prepare_m63_retest as prep
    config=json.loads(Path('docs/eval/m6.3-targeted-four-cause-repair-r1-20261102/task-config.json').read_text(encoding='utf8'))
    monkeypatch.setattr(prep,'TASK_CONFIG',config);monkeypatch.setattr(prep,'OUT',tmp_path)
    monkeypatch.setattr(prep,'TASK',config['task_id']);monkeypatch.setattr(prep,'LEDGER',config['ledger_directory'])
    prep.design()
    design=json.loads((tmp_path/'design.json').read_text(encoding='utf8'))
    assert len(design['phases']['variants'])==12
    assert not design['phases']['main'] and not design['phases']['acceptance']
    assert {t['architecture'] for t in design['trials']}=={'SINGLE','MULTI'}
    for c,s in zip(design['phases']['variants'],config['targeted_specs']):
        if 'question_override' in s:assert c['input']['user_query']==s['question_override']


def test_not_found_comparison_is_an_observation_not_an_infinite_read_retry():
    from types import SimpleNamespace
    from app.m3.single_agent import _pending_observations
    context=SimpleNamespace(query='比较商品 P-ONE 与 P-TWO 的重量',route_payload={
        'required_capabilities':['get_product_detail'],'intent_labels':['product_comparison']})
    values=[{'tool':'get_product_detail','data':{'availability':'NOT_FOUND','product_id':pid}} for pid in ('P-ONE','P-TWO')]
    assert _pending_observations(context,values)==[]


@pytest.mark.parametrize('mutation',['duplicate','extra'])
def test_targeted_scope_rejects_extra_or_duplicate_trials(tmp_path,monkeypatch,mutation):
    from scripts import prepare_m63_retest as prep
    config=json.loads(Path('docs/eval/m6.3-targeted-four-cause-repair-r1-20261102/task-config.json').read_text(encoding='utf8'))
    if mutation=='extra':config['targeted_specs'].append(config['targeted_specs'][0])
    else:config['targeted_specs'][1]=config['targeted_specs'][0]
    monkeypatch.setattr(prep,'TASK_CONFIG',config);monkeypatch.setattr(prep,'OUT',tmp_path)
    with pytest.raises(ValueError):prep.design()


def test_deployment_followup_keeps_legacy_profile_and_exact_five(tmp_path,monkeypatch):
    from scripts import prepare_m63_retest as prep
    config=json.loads(Path('docs/eval/m6.3-targeted-four-cause-repair-r1-20261102/task-config.json').read_text())
    config.update(purpose='DEPLOYMENT_FOLLOWUP',trial_prefix='M63DEP')
    config['targeted_specs']=config['targeted_specs'][:5]
    monkeypatch.setattr(prep,'TASK_CONFIG',config);monkeypatch.setattr(prep,'OUT',tmp_path)
    prep.design()
    data=json.loads((tmp_path/'design.json').read_text())
    assert len(data['trials'])==5 and all(t['case_id'].startswith('M63DEP-') for t in data['trials'])
