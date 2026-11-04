"""Current round's persisted evidence and reporting negative boundaries, no I/O services."""
import json
from pathlib import Path
import pytest
from scripts import report_m63_retest as report
from eval.m6_2 import personal_closeout_r30 as existing

ROOT=Path(__file__).resolve().parents[1]

@pytest.fixture
def round2(monkeypatch):
    monkeypatch.setattr(report,'OUT',ROOT/'docs/eval/m6.3-repair-retest-r2-20261102')
    monkeypatch.setattr(report,'TASK_CONFIG',json.loads((report.OUT/'task-config.json').read_text()))
    monkeypatch.setattr(existing,'OUT',report.OUT.relative_to(ROOT))
    return report

def test_actual_round2_recompute_preserves_failures_and_distinct_counts(round2):
    rows,attempts,records=round2.load()
    assert len(rows)==117 and attempts==[]
    assert {p:sum(r['phase']==p for r in rows) for p in ('acceptance','main','variants')}=={'acceptance':7,'main':100,'variants':10}
    assert all(r['cleanup']['residual_case_rows']==0 and r['cleanup']['lease_released'] for r in rows)
    review=round2.read(round2.OUT/'ai-output-review.json');by=existing.validate_ai(records,review)
    assert all(by[r['case_id']]['task_outcome']=='FAIL' for r in rows if r['answer'] is None)
    assert by['RETEST-acceptance-AFTER_SALES-028']['task_outcome']=='FAIL'
    assert by['RETEST-main-FC26-AFTER_SALES-028']['task_outcome']=='PASS'
    rejected=next(r for r in rows if r['case_id']=='RETEST-main-FC26-CROSS_DOMAIN-049')
    assert rejected['answer'] is None
    assert 'ORD-FC26-049' in rejected['candidate_sections']['business_answer']['text_redacted']
    assert rejected['candidate_sections']['policy_answer']['text_redacted']==rejected['candidate']['redacted_text']
    abort=round2.read(round2.OUT/'pre-run-zero-execution-cleanup.json')
    assert abort['case_id'] not in {r['case_id'] for r in rows}
    assert not abort['run_created'] and abort['provider_calls']==abort['tool_calls']==0
    grants=round2.read(round2.OUT/'final-permit-closure.json')
    for side in ('controller','application'):
        assert sum(g['claimed'] for g in grants[side]['permits'])==118
        assert sum(g['settled'] for g in grants[side]['permits'])==118
        assert all(g['closed'] for g in grants[side]['permits'])

@pytest.mark.parametrize('mutation',['wrong_run','known_non_delivery_pass','missing_item','human_signoff'])
def test_new_review_misbindings_and_false_pass_are_rejected(round2,mutation):
    rows,_,records=round2.load();review=round2.read(round2.OUT/'ai-output-review.json')
    if mutation=='wrong_run':review['items'][0]['run_id']='different'
    elif mutation=='known_non_delivery_pass':
        failed=next(r for r in rows if r['answer'] is None)
        next(i for i in review['items'] if i['case_id']==failed['case_id'])['task_outcome']='PASS'
    elif mutation=='missing_item':review['items'].pop()
    else:review['items'][0]['human_decision']='PASS'
    with pytest.raises(ValueError):existing.validate_ai(records,review)

def test_health_change_preserves_initial_evidence_and_writes_version(monkeypatch,tmp_path):
    from scripts import manage_m63_retest as manager
    health={'runtime':'a'*64,'active_runs':0,'lease_free':True,'real_provider_activated':False}
    monkeypatch.setattr(manager,'OUT',tmp_path);monkeypatch.setattr(manager,'TASK','UNIT_ROUND2')
    monkeypatch.setattr(manager,'LEDGER','data/eval/formal-ledgers/unit')
    def docker(*args,**kwargs):
        if args[0]=='inspect':
            return json.dumps([{'Mounts':[],'Image':'test-image','Config':{'User':'10002'},'HostConfig':{'ReadonlyRootfs':True,'CapDrop':['ALL']}}])
        if args[0]=='network':return '[{"Internal":true}]'
        if args[0]=='compose' and 'ps' in args:return 'test-container'
        code=args[-1]
        if 'verify_container_process' in code:return json.dumps({'access':{'control_assets':'False','issuer_private_key':'False'},'identity':{'uid':10002}})
        return '{}'
    monkeypatch.setattr(manager,'docker',docker)
    monkeypatch.setattr(manager,'control',lambda code:dict(health) if 'httpx.get' in code else {})
    manager.isolation()
    first=tmp_path/('health-'+health['runtime'][:12]+'.json');before=first.read_bytes()
    health['real_provider_activated']=True;manager.isolation()
    assert first.read_bytes()==before
    files=list(tmp_path.glob('health-*.json'))
    assert len(files)==2
    assert {json.loads(p.read_text())['real_provider_activated'] for p in files}=={False,True}
