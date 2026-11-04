import json
from pathlib import Path
import shutil
import pytest
from scripts import report_customer_experience as report


def test_raw_report_reconciliation_and_known_failures_are_retained():
    result=report.build()
    assert result['lifecycle_counts']=={'FAILED':7,'SUCCEEDED':4}
    assert result['budget']['provider_requests']==10
    assert result['budget']['tokens']==14841
    assert result['route_sources']=={'RULE':9,'LLM':2}
    assert result['policy_retrievals']==8 and result['business_reads']==3
    assert len(result['limitations']) and not result['human_task_scores_created']


@pytest.fixture
def copied_evidence(tmp_path,monkeypatch):
    original=report.PROOF
    names=[*report.INPUTS,'exact-cleanup-archive.json','automated-ledger-closed.json',
        'provider-events.jsonl','exact-cleanup-proof.json','offline-final-verified.xml','final-deployment.json']
    for name in names:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True)
        shutil.copyfile(original/name,path)
    monkeypatch.setattr(report,'PROOF',tmp_path)
    return tmp_path


def test_report_rejects_http_status_drift(copied_evidence):
    path=copied_evidence/'real-acceptance.json'
    value=json.loads(path.read_text(encoding='utf8'))
    value['rows'][0]['run']['status']='SUCCEEDED'
    path.write_text(json.dumps(value),encoding='utf8')
    with pytest.raises(AssertionError):report.build()


def test_report_rejects_missing_finished_retrieval_audit(copied_evidence):
    path=copied_evidence/'exact-cleanup-archive.json'
    value=json.loads(path.read_text(encoding='utf8'))
    value['runs'][0]['events']=[e for e in value['runs'][0]['events'] if e['type']!='policy_retrieval_finished']
    path.write_text(json.dumps(value),encoding='utf8')
    with pytest.raises(AssertionError):report.build()


def test_report_rejects_provider_token_drift(copied_evidence):
    path=copied_evidence/'provider-events.jsonl'
    rows=[json.loads(line) for line in path.read_text().splitlines()]
    rows[0]['prompt_tokens']+=1
    path.write_text('\n'.join(json.dumps(r) for r in rows))
    with pytest.raises(AssertionError):report.build()


def test_conversation_reconciles_both_closed_accounts_and_all_attempts():
    result=report.build_conversation()
    assert result['provider_requests']==17 and result['tokens']==28685
    assert result['lifecycle_counts']=={'SUCCEEDED':11,'FAILED':2}
    assert result['original_sessions_unchanged']==4
    assert result['remaining_requests_unused']==3
    assert result['human_task_scores_created'] is False


@pytest.mark.parametrize('corruption',['tokens','delivered_answer','user_session'])
def test_conversation_rejects_raw_evidence_drift(monkeypatch,corruption):
    original=report.read
    def read(path):
        value=original(path)
        if corruption=='tokens' and path.name=='provider-events.json':value[0]['completion_tokens']+=1
        if corruption=='delivered_answer' and path.name=='exact-cleanup-archive.json' and path.parent.name=='segment-2':value['runs'][0]['result']['answer']='counterfeit delivered answer'
        if corruption=='user_session' and path.name=='all-sessions-after-final.json':next(iter(value['sessions'].values()))['runs']+=1
        return value
    monkeypatch.setattr(report,'read',read)
    with pytest.raises(AssertionError):report.build_conversation()



def test_order_report_reconciles_actual_owner_delivery_and_closed_budget():
    from scripts.report_customer_experience import build_orders
    result=build_orders()
    assert len(result['runs'])==4
    assert result['actual_business_reads']==5 and result['actual_policy_retrievals']==2
    assert result['budget']['provider_requests']==1 and result['budget']['tokens']==2452
    assert result['manual_account_unchanged'] and result['new_account_status']=='CLOSED'
    assert result['ai_task_outcomes']=={'PASS':3,'PARTIAL':1}


@pytest.mark.parametrize('artifact,kind', [('archive.json','answer'),('provider-events.json','usage'),
    ('cleanup.json','residual'),('fixture.json','owner'),('source-manifest-final.json','binding')])
def test_order_report_rejects_evidence_drift(monkeypatch,artifact,kind):
    import copy
    import scripts.report_customer_experience as report
    original=report.read
    def changed(path):
        value=copy.deepcopy(original(path))
        if 'customer-orders-20261101' not in str(path) or path.name!=artifact:return value
        if kind=='answer':value['runs'][0]['result']['answer']='altered delivered content'
        elif kind=='usage':value[0]['prompt_tokens']+=1
        elif kind=='residual':value['remaining_runs']=1
        elif kind=='owner':value['orders']=[]
        elif kind=='binding':value['files']['app/m3/order_tools.py']='0'*64
        return value
    monkeypatch.setattr(report,'read',changed)
    with pytest.raises(AssertionError):report.build_orders()


@pytest.mark.parametrize('mutation',['answer','budget','cleanup'])
def test_final_delivery_recalculation_rejects_original_fact_drift(tmp_path,mutation):
    proof=report.ROOT/'docs/probes/customer-final-20261102'
    names=['real/segment-1/receipt.json','archive.json','closed-account.json','budget-events.json',
           'provider-events.json','cleanup.json','fixture-cleanup.json','runtime-verification.json',
           'source-manifest-final.json','manual-continuation.json','answer-pre-review.json',
           'offline-final.xml','account-cleanup.xml','followup-final.xml','routing-report-final.xml','final-report-tests.xml']
    for name in names:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(proof/name,path)
    assert len(report.build_final(tmp_path)['rows'])==6
    name='archive.json' if mutation=='answer' else 'closed-account.json' if mutation=='budget' else 'cleanup.json'
    path=tmp_path/name;value=json.loads(path.read_text(encoding='utf8'))
    if mutation=='answer':value['runs'][0]['result']['answer']='forged answer'
    elif mutation=='budget':value['snapshot']['tokens']+=1
    else:value['remaining_runs']=1
    path.write_text(json.dumps(value),encoding='utf8')
    with pytest.raises(AssertionError):report.build_final(tmp_path)
