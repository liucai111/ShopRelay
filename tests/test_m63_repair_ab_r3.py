"""Evidence-driven current application regressions and the actual SDK request boundary."""
import json
from pathlib import Path
from types import SimpleNamespace
import pytest
from app.m2.self_rag import _answer_semantic_issues
from app.m3.single_agent import BusinessAnswer,Step,response_instruction
from test_m63_repair_retest import short_project_tmp as tmp_path
import test_m63_retest_round2 as prior

BASE=Path(__file__).resolve().parents[1]/'docs/eval/m6.3-repair-retest-r2-20261102'
ROWS=json.loads((BASE/'scores-and-evidence.json').read_text(encoding='utf8'))['rows']
MAIN={r['source_case_id'].removeprefix('FC26-'):r for r in ROWS if r['phase']=='main'}
PROTECTED=sorted({n for n,r in MAIN.items() if r['ai_review']['task_outcome']=='PASS'}|prior.ORIGINAL_PASS)

@pytest.mark.parametrize('name',PROTECTED)
def test_current_application_protects_84_and_old_regressions(name,tmp_path,monkeypatch):
    # Protect the current accepted-output scope. The shared harness otherwise
    # selects round1 rejected candidates, which are a different review object.
    monkeypatch.setitem(prior.MAIN,name,MAIN[name])
    prior.exercise_current_application(name,tmp_path,monkeypatch)

@pytest.mark.parametrize('name',['AFTER_SALES-018','CROSS_DOMAIN-049','EXTRA-064','EXTRA-073','EXTRA-079','EXTRA-096'])
def test_complete_round2_supported_candidate(name):
    row=MAIN[name];c=row['candidate'];sources=[s for s in row['policy_sources'] if s['chunk_id'] in c['citation_ids']]
    assert not _answer_semantic_issues(c['redacted_text'],sources,row['question'])

@pytest.mark.parametrize('name,old,new',[
 ('AFTER_SALES-018','七天','八天'),('AFTER_SALES-018','商品送达后','商品付款后'),
 ('AFTER_SALES-018','商品送达后','食品送达后'),('EXTRA-064','38天','38个月'),
 ('EXTRA-064','服务登记','下单'),('EXTRA-064','商业使用','个人使用'),
 ('EXTRA-064','有效服务凭证','无效服务凭证'),('EXTRA-073','登记事件','付款事件'),
 ('EXTRA-079','六个月','七个月'),('EXTRA-079','翻新产品','食品'),
 ('EXTRA-079','记录交付','未核实交付'),('EXTRA-096','送达后七天','送达后八天')])
def test_duration_scope_clock_and_condition_counterexamples(name,old,new):
    row=MAIN[name];c=row['candidate'];assert old in c['redacted_text']
    sources=[s for s in row['policy_sources'] if s['chunk_id'] in c['citation_ids']]
    assert _answer_semantic_issues(c['redacted_text'].replace(old,new),sources,row['question'])

def test_real_omission_and_specific_object_still_rejected():
    for name in ['POLICY-038','EXTRA-074']:
        row=MAIN[name];c=row['candidate'];sources=[s for s in row['policy_sources'] if s['chunk_id'] in c['citation_ids']]
        assert _answer_semantic_issues(c['redacted_text'],sources,row['question'])

def test_negative_phase_reference_not_false_entitlement():
    row=next(r for r in ROWS if r['case_id']=='RETEST-variant-04-POLICY-034');c=row['candidate']
    sources=[s for s in row['policy_sources'] if s['chunk_id'] in c['citation_ids']]
    assert not _answer_semantic_issues(c['redacted_text'],sources,row['question'])
    assert _answer_semantic_issues(c['redacted_text']+'退款已批准。',sources,row['question'])

@pytest.mark.parametrize('step',[False,True])
def test_actual_sdk_messages_have_the_same_complete_response_schema(step):
    from test_m1_chat_provider import provider_for,completion
    value={'business_answer':'记录为五伏，尚不能保证兼容。','policy_answer':'','citations':[],
           'business_result_refs':['tool_result:current']}
    if step:value.update(kind='finish',tool=None,arguments={})
    provider=provider_for(completion(json.dumps(value),usage={'prompt_tokens':40,'completion_tokens':30,'total_tokens':70}))
    result=provider._structured_node('business_answer','unit-v2',response_instruction(step=step),
        {'question':'查询实际电压','tool_results':[{'result_ref':'tool_result:current','data':{'input_voltage':'5V'}}],'evidence':[]})
    model=Step if step else BusinessAnswer;assert model.model_validate(result['result'])
    actual=provider.client.chat.completions.last_kwargs
    system=actual['messages'][0]['content'];user=actual['messages'][1]['content']
    assert json.dumps(model.model_json_schema(),ensure_ascii=False) in system
    assert 'citations只用于policy_answer' in system and 'business_result_refs' in system
    assert 'tool_result:current' in user and 'expected_outcome' not in user
    assert actual['extra_body']['thinking']['type']=='disabled'
    from pydantic import ValidationError
    with pytest.raises(ValidationError):model.model_validate({**value,'type':'answer'})

@pytest.mark.parametrize('status',['PENDING_CONFIRMATION','SUCCEEDED'])
def test_confirmation_feedback_distinguishes_order_refund_and_payment(status):
    from app.m4.actions import action_feedback
    row=SimpleNamespace(status=status,tool_id='cancel_order',result_json=json.dumps({
        'order_id':'ORD-EXAMPLE-98','order_status':'CANCELLED','refund_id':'refund-current',
        'refund_status':'SUBMITTED','payment_status':'NOT_EXECUTED'}))
    text=action_feedback(row)
    if status=='SUCCEEDED':
        assert 'CANCELLED' in text and 'SUBMITTED' in text and '未执行支付' in text and '不代表已批准或到账' in text
    else:assert '没有据此确认业务已完成' in text and '取消事务已完成' not in text

def test_damage_missing_facts_is_targeted_without_reading():
    from app.m1.routing import decide_route
    decision=decide_route('我的货有故障但没有单号，需要先核实什么？',available_capabilities=['get_order','search_knowledge'])
    assert decision.strategy=='DIRECT' and decision.required_capabilities==[]
    assert all(s in decision.clarification_question for s in ['订单号','送达','具体问题'])


def test_real_anaphoric_clock_preserves_conditions_and_rejects_missing_origin():
    from scripts import report_m63_retest as report
    report.configure('docs/eval/m6.3-repair-ab-r3-20261102/task-config.json')
    rows,_,_=report.load()
    targets=[r for r in rows if r['phase']=='acceptance' and r['case_id'].endswith('EXTRA-064')]
    for row in targets:
        candidate=row['candidate'];sources=[s for s in row['policy_sources'] if s['chunk_id'] in candidate['citation_ids']]
        issues=_answer_semantic_issues(candidate['redacted_text'],sources,row['question'])
        if row['design']['variant']=='A':
            assert issues  # The actual A candidate omitted its clock; no blanket allow.
        else:
            assert not issues
            for old,new in [('服务登记事件','付款事件'),('38天','38个月'),
                    ('商业使用及有效服务凭证','商业使用及无需有效服务凭证')]:
                assert old in candidate['redacted_text']
                assert _answer_semantic_issues(candidate['redacted_text'].replace(old,new),sources,row['question'])
