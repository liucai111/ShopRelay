"""Actual adapted reads through existing CLI;no candidate text in model tests."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from test_m6_2_personal_dev_batch_r7 import tmp_path
from test_m6_2_r24_formal_application import fictional_cases as base_fixture_cases
from eval.m6_1.contracts import EvalCase
from eval.m6_1.grants import formal_case_payload
from eval.m6_1.fixture_read_models import FixtureProductRepository,product_id
from eval.m6_1.formal_application import MeteredReads
from eval.m6_1.formal_reference_rules import V3,resource_coverage,result_row_coverage,direct_source_facts
from eval.m6_1.formal_assets import validate_labels

def mini_cases():
    originals=base_fixture_cases();product=formal_case_payload(originals[2])
    product['case_id']='mini-two-products';product['group_id']='mini-catalog-family'
    a,b='mini-amber-device','mini-cobalt-device';pa,pb=product_id(a),product_id(b)
    product['input']['user_query']=f'比较两款商品{pa}和{pb}的记录重量，说明差值。'
    product['input']['allowed_capabilities']=['get_product_detail']
    product['input']['fixture_refs']=[a,b]
    product['input']['fixture']['products']=[{'id':a,'name':'离线量杯甲','weight_g':120},{'id':b,'name':'离线量杯乙','weight_g':185}]
    route=product['expected_outcome']['route'];route.update(strategy='AGENT',sub_strategy='AGENT+SINGLE',
        safety_decision='ALLOW',needs_clarification=False,required_capabilities=['get_product_detail'])
    product['expected_outcome']['task'].update(required_tool_groups=[[['get_product_detail']]],zero_calls=False,
        required_resources=[{'tool':'get_product_detail','argument':'product_id','values':[pa,pb]}])
    product['rubric_refs'].append(V3)
    order=formal_case_payload(originals[2]);order['case_id']='mini-two-order-lines';order['group_id']='mini-order-family'
    order['input']['user_query']='读取本人订单 ORD-EXAMPLE-901 的两条明细，分别给出商品及数量。'
    order['input']['allowed_capabilities']=['get_order_items'];order['input']['fixture_refs']=['mini-line-a','mini-line-b']
    order['input']['fixture']['products']=deepcopy(product['input']['fixture']['products'])
    order['input']['fixture']['order_items']=[{'id':'mini-line-a','order_id':'fiction-order-source','product_id':a,'quantity':2,'paid_cents':1000},
        {'id':'mini-line-b','order_id':'fiction-order-source','product_id':b,'quantity':3,'paid_cents':1250}]
    order['expected_outcome']['route'].update(strategy='AGENT',sub_strategy='AGENT+SINGLE',
        safety_decision='ALLOW',needs_clarification=False,required_capabilities=['get_order_items'])
    order['expected_outcome']['task'].update(required_tool_groups=[[['get_order_items']]],zero_calls=False,
        required_result_rows=[{'tool':'get_order_items','result_path':['items'],'source_field':'fixture_provenance',
            'rows':[{'source_id':'mini-line-a','fields':{'product_id':pa,'quantity':2}},
                    {'source_id':'mini-line-b','fields':{'product_id':pb,'quantity':3}}]}])
    order['rubric_refs'].append(V3)
    return [*originals,EvalCase.from_dict(product),EvalCase.from_dict(order)]

def test_actual_product_adapter_metered_signature_and_resource_cases(tmp_path):
    from eval.m6_1.budget import BudgetLedger,BudgetLimits
    from eval.m6_1.store import ResultStore
    case=mini_cases()[4];rows=[]
    for p in case.input['fixture']['products']:
        rows.append({'resource_id':product_id(p['id']),'source_id':p['id'],'fact':dict(p)})
    class Storage:
        factory=None
        def rows(self,kind,**kwargs):assert kind=='products';return rows
    store=ResultStore(tmp_path/'budget.sqlite3');limits=BudgetLimits(tool_calls=10,max_calls_per_trial=10)
    from eval.m6_1.contracts import EvalExperiment,sha256_json
    from datetime import datetime,timezone
    store.lock_experiment(EvalExperiment('mini-real-product-adapter',{},sha256_json({}),'isolated-no-git',None,{}, {},
        'ENGINEERING_EVAL',('get_product_detail',),limits.to_dict(),'mini-v1','reference-v3'),datetime.now(timezone.utc).isoformat())
    ledger=BudgetLedger(store,'mini-real-product-adapter',limits)
    # Register the real budget contract;storage alone is an in-memory substitute.
    records=[];repo=MeteredReads(FixtureProductRepository(Storage()),ledger,'trial-product',records)
    values=case.expected_outcome['task']['required_resources'][0]['values']
    assert repo.get_product_detail(values[0])['product']['product_id']==values[0]
    assert records[0]['arguments']=={'product_id':values[0]}
    assert resource_coverage(case,records)=='FAIL'
    repo.get_product_detail(product_id=values[1])
    assert resource_coverage(case,records)=='PASS'
    duplicate=[records[0],records[0]]
    assert resource_coverage(case,duplicate)=='FAIL'
    historical=deepcopy(records)
    for r in historical:r['arguments']={'product':r['arguments']['product_id']}
    assert resource_coverage(case,historical)=='UNKNOWN'

def test_full_multi_cli_product_rows_quality_failure_and_cleanup(tmp_path,monkeypatch,record_property):
    import test_m6_2_r24_formal_application as helpers
    monkeypatch.setattr(helpers,'fictional_cases',mini_cases)
    cases,store,requests=helpers.deploy(tmp_path,monkeypatch)
    helpers.cli(tmp_path,'issue');result=helpers.cli(tmp_path,'execute')
    assert len(result['completed_cases'])==6 and result['stop_reason'] is None
    assert result['budget']['active_reservations']==0
    trials=store.trials('unprotected-full-app-test')
    by={t['case_id']:store.raw_result(t['trial_id']) for t in trials}
    product=by['mini-two-products'];rows=by['mini-two-order-lines']
    assert product['code_assessment']['checks']['resource_coverage']=='PASS'
    calls=[r for r in product['application_archive']['read_invocations'] if r['operation']=='get_product_detail']
    assert len(calls)==2 and len({r['arguments']['product_id'] for r in calls})==2
    assert not product['execution_blocking_items']
    assert rows['code_assessment']['checks']['result_row_coverage']=='PASS'
    assert len(rows['code_assessment']['result_row_assessments']['rows'])==2
    assert rows['code_assessment']['checks']['answer_completeness']=='UNKNOWN'
    assert rows['code_assessment']['task_outcome']!='PASS' # read rows are not answer semantics
    assert by['unprotected-fixture-1']['code_assessment']['task_outcome']=='FAIL'
    constraints=next(c for c in cases if c.case_id=='mini-two-order-lines')
    genuine=rows['application_archive']['read_invocations']
    missing=deepcopy(genuine);missing[0]['read_result']['items'].pop()
    assert result_row_coverage(constraints,missing)['status']=='FAIL'
    wrong=deepcopy(genuine);wrong[0]['read_result']['items'][1]['quantity']=4
    assert result_row_coverage(constraints,wrong)['status']=='FAIL'
    no_audit=deepcopy(genuine);no_audit[0].pop('read_result')
    assert result_row_coverage(constraints,no_audit)['status']=='UNKNOWN'
    for t in trials:
        with store.transaction(immediate=False) as db:
            event=json.loads(db.execute("SELECT payload_json FROM trial_events WHERE trial_id=? AND event_type='CLEANUP'",(t['trial_id'],)).fetchone()[0])
        assert event['status']=='CLEANED' and event['decision']=='CONTINUE' and not event['blocking_items']
    report=helpers.cli(tmp_path,'report')
    assert report is not None
    record_property('actual_product_parameter_contract','product_id via real FixtureProductRepository+MeteredReads+application+Runner')

def test_v3_boundary_rejects_wrong_direct_name_and_duplicate_fact_id():
    v=formal_case_payload(mini_cases()[4]);source=v['input']['fixture']['products'][0]
    v['expected_outcome']['atomic_facts']=[{'fact_id':'name-a','subject':source['id'],'predicate':'name',
        'value':'wrong object','condition':'stored name','source_ids':[source['id']]}]
    case=EvalCase.from_dict(v)
    assert len(direct_source_facts(case)['mismatches'])==1
    with pytest.raises(ValueError,match='contradicts'):validate_labels(v['expected_outcome'],case)
    v['expected_outcome']['atomic_facts'][0]['value']=source['name']
    v['expected_outcome']['atomic_facts'].append(deepcopy(v['expected_outcome']['atomic_facts'][0]))
    with pytest.raises(ValueError,match='duplicate atomic'):validate_labels(v['expected_outcome'],EvalCase.from_dict(v))

def test_revision_preserves_unchanged_policy_facts_not_just_source_suffixes():
    from eval.m6_2.candidate_revision_r27 import revise_labels
    v=formal_case_payload(base_fixture_cases()[0]);f=v['input']['fixture']
    for name in ['returns','products','inventory','order_items']:f[name]=[]
    p5={'evidence_id':'mini-DOC-05','policy_id':'mini-a','text':'附件必须齐全','version':'v1'}
    p6={'evidence_id':'mini-DOC-06','policy_id':'mini-b','text':'旧条款','version':'v1'}
    f['policies']=[p5,p6];before=deepcopy(v['input'])
    keep={'fact_id':'keep-a','subject':'mini-a','predicate':'policy_claim','value':'附件必须齐全','condition':'申请条件','source_ids':['mini-DOC-05']}
    replace_fact={'fact_id':'old-b','subject':'mini-b','predicate':'policy_claim','value':'旧条款','condition':'旧源','source_ids':['mini-DOC-06']}
    review={'labels':deepcopy(v['expected_outcome'])};review['labels']['atomic_facts']=[keep,replace_fact]
    p6.update(text='仅规定商业服务手续费减免的申请窗口：从服务登记事件起算18天，且须商业使用及有效凭证。此窗口不是商品退货、退款或保修期限；无渠道事实时不得判断资格。',version='repaired')
    result=revise_labels(v,review,True,source_before=before)
    assert keep in result['atomic_facts'] and replace_fact not in result['atomic_facts']
    units=[r for r in result['atomic_facts'] if r['source_ids']==['mini-DOC-06']]
    assert len(units)>5 and all(p6['text'] in r['condition'] for r in units)
    assert any(r['predicate']=='duration_days' and r['value']==18 for r in units)
    assert any(r['predicate']=='necessary_condition' and r['value']=='有效凭证' for r in units)

def test_explicit_product_alternatives_not_any_whitelisted_capability():
    from eval.m6_1.formal_reference_rules import route_reference,ROUTE_KEYS
    v=formal_case_payload(mini_cases()[4]);v['input']['allowed_capabilities']+=['recommend_products','search_products','check_inventory']
    ref=v['expected_outcome']['route']
    v['expected_outcome']['task']['route_equivalents']=[{**{k:ref[k] for k in ROUTE_KEYS},'required_capabilities':[cap]}
        for cap in ['recommend_products','search_products']]
    c=EvalCase.from_dict(v)
    for cap in ['get_product_detail','recommend_products','search_products']:
        assert route_reference(c,{**ref,'required_capabilities':[cap]})=='PASS'
    assert route_reference(c,{**ref,'required_capabilities':['check_inventory']})=='FAIL'

def test_reference_fixture_link_repair_only_adds_authenticated_owners_lines():
    from eval.m6_2.candidate_revision_r27 import _repair_reference_values
    v=formal_case_payload(mini_cases()[5]);f=v['input']['fixture']
    v['input']['owner_map']['unrelated-user']='unrelated-auth-owner'
    f['orders_and_snapshots'].append({'id':'foreign-order-source','order_id':'ORD-FOREIGN-123','owner_id':'unrelated-auth-owner','status':'PAID'})
    f['order_items'].append({'id':'missing-owned-line','order_id':'fiction-order-source','product_id':'mini-amber-device','quantity':1})
    f['order_items'].append({'id':'foreign-line','order_id':'foreign-order-source','product_id':'mini-cobalt-device','quantity':1})
    _repair_reference_values(v,deepcopy(v),deepcopy(v['expected_outcome']))
    assert 'missing-owned-line' in v['input']['fixture_refs']
    assert 'foreign-line' not in v['input']['fixture_refs']
