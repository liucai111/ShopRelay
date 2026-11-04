"""Exact unconsumed scope and historical UNKNOWN budget, no live service calls."""
import json
import sqlite3
from pathlib import Path

import pytest

from scripts import prepare_m63_retest as prep

BASE=Path(__file__).resolve().parents[1]/'docs/eval/m6.3-repair-ab-r3-20261102'


def read(name):
    return json.loads((BASE/name).read_text(encoding='utf8'))


def test_remaining_scope_is_exact_and_unconsumed_on_both_sides():
    proof=read('continuation-r1/actual-scope-and-target.json')
    design=read('design.json');consumed=set(proof['controller']['attempts'])
    expected=[{'phase':phase,'case_id':c['case_id']} for phase,cases in design['phases'].items()
              for c in cases if c['case_id'] not in consumed]
    assert proof['remaining_exact_scope']==expected
    assert len(expected)==57 and len({r['case_id'] for r in expected})==57
    assert sum(r['phase']=='repeats' for r in expected)==37
    assert sum(r['phase']=='variants' for r in expected)==20
    assert not {r['case_id'] for r in expected}&set(proof['application']['attempts'])
    assert not {r['case_id'] for r in expected}&set(proof['controller']['trials'])
    assert 'M63R3-repeats-B-r3-FC26-ORDER-010' in consumed
    assert len(consumed)==267


def test_unknown_usage_is_not_rewritten_as_conservative_actual_usage():
    original=read('accounting-267.json')
    actual=read('unknown-usage-application-ledger.json')
    held=next(e for e in actual['events'] if e['event_type']=='RESERVE' and e['category']=='provider')
    committed=next(e for e in actual['events'] if e['event_type']=='COMMIT' and e['category']=='provider')
    assert held['tokens']==9761 and held['estimated_usd']==pytest.approx(.0117132)
    assert committed['tokens'] is None and committed['estimated_usd'] is None
    assert original['tokens'] is None and original['estimated_usd'] is None
    assert original['tokens_conservative_budget_consumption']-original['known_recorded_tokens']==120000
    assert original['estimated_usd_conservative_budget_consumption']-original['known_recorded_estimated_usd']==pytest.approx(.5)
    assert not original['known_settled'] and actual['active_reservations']==0


def test_unapproved_historical_unknown_cannot_silently_issue_successor(tmp_path,monkeypatch):
    monkeypatch.setattr(prep,'OUT',tmp_path)
    cfg=read('task-config.json')
    monkeypatch.setattr(prep,'TASK_CONFIG',cfg);monkeypatch.setattr(prep,'TASK',cfg['task_id'])
    monkeypatch.setattr(prep,'LEDGER',cfg['ledger_directory'])
    for name in ('design.json','repeats-cases.json','price.json','authorization.json'):
        (tmp_path/name).write_bytes((BASE/name).read_bytes())
    proof=read('continuation-r1/actual-scope-and-target.json')
    prep.put(tmp_path/'health.json',dict(proof['health'],python_dependencies={}))
    prep.put(tmp_path/'accounting.json',read('accounting-267.json'))
    prep.put(tmp_path/'isolation.json',{'offline_test':True})
    with pytest.raises(PermissionError,match='unsettled'):
        prep.request('repeats',str(tmp_path/'health.json'),str(tmp_path/'price.json'),'unapproved',
                     str(tmp_path/'accounting.json'),str(tmp_path/'isolation.json'))
    assert not (tmp_path/'request-repeats-unapproved.json').exists()


@pytest.mark.parametrize('change',[None,'new_unknown','different_reservation','wrong_task','unconfirmed'])
def test_explicit_single_exception_is_bounded_and_fully_charged(tmp_path,monkeypatch,change):
    cfg=read('task-config.json');disposition=read('continuation-r1/confirmed-disposition.json')
    accounting=json.loads(Path(disposition['accounting_before_successor']).read_text(encoding='utf8'))
    monkeypatch.setattr(prep,'OUT',tmp_path);monkeypatch.setattr(prep,'TASK_CONFIG',cfg)
    monkeypatch.setattr(prep,'TASK',cfg['task_id']);monkeypatch.setattr(prep,'LEDGER',cfg['ledger_directory'])
    for name in ('design.json','repeats-cases.json','price.json','authorization.json'):
        (tmp_path/name).write_bytes((BASE/name).read_bytes())
    if change=='new_unknown':accounting['unknown_usage_provider_calls']=2
    elif change=='different_reservation':accounting['unknown_usage_reservations'][0]['reservation_id']='another'
    elif change=='wrong_task':disposition['task_id']='another'
    elif change=='unconfirmed':disposition['decision']='PENDING'
    prep.put(tmp_path/'accounting.json',accounting)
    prep.put(tmp_path/'disposition.json',disposition)
    prep.put(tmp_path/'health.json',dict(read('continuation-r1/actual-scope-and-target.json')['health'],python_dependencies={}))
    prep.put(tmp_path/'isolation.json',{'offline_test':True})
    args=('repeats',str(tmp_path/'health.json'),str(tmp_path/'price.json'),'bounded',str(tmp_path/'accounting.json'),str(tmp_path/'isolation.json'),str(tmp_path/'disposition.json'))
    if change is not None:
        with pytest.raises(PermissionError):prep.request(*args)
        assert not (tmp_path/'request-repeats-bounded.json').exists()
    else:
        prep.request(*args)
        request=prep.read(tmp_path/'request-repeats-bounded.json')['config']
        assert len(request['plan']['case_bindings'])==37
        assert set(c for c,h in request['plan']['case_bindings'])<=set(disposition['remaining_case_ids'])
        assert request['plan']['budget']['estimated_usd']==pytest.approx(8.8408052)
        assert request['plan']['budget']['total_tokens']==39330671
        assert accounting['tokens'] is None and accounting['estimated_usd'] is None
        assert 'data/eval/formal-trust/retest-uncertainty-disposition.json' in dict(request['plan']['runtime_bindings'])


@pytest.mark.parametrize('change',[None,'no_cleanup','unknown_usage','tool_started','wrong_run','runtime_changed'])
def test_pretool_contract_failure_requires_positive_lifecycle_and_cleanup(change):
    from scripts.manage_m63_retest import classify_pretool_model_failure
    db=sqlite3.connect('file:'+(BASE/'evidence/results.sqlite3').as_posix()+'?mode=ro',uri=True)
    row=db.execute("select trial_id,payload_json from trials where case_id=?",('M63R3-repeats-A-r3-FC26-ORDER-010',)).fetchone()
    raw=json.loads(db.execute('select payload_json from raw_results where trial_id=?',(row[0],)).fetchone()[0]);trial=json.loads(row[1]);db.close()
    cleanup=read('continuation-r1/known-failure-exact-cleanup.json')['evidence']
    if change=='no_cleanup':cleanup=None
    elif change=='unknown_usage':trial['usage']['unknown_tokens']=True
    elif change=='tool_started':raw['application_archive']['result']['observations']['run_events'].append({'event_type':'tool_started','run_id':trial['run_ids'][0]})
    elif change=='wrong_run':trial['run_ids']=['another']
    elif change=='runtime_changed':raw['application_archive']['runtime_binding']['observed_at_end']='different'
    result=classify_pretool_model_failure(raw,trial,cleanup)
    if change is None:assert result['operational_continue'] and result['task_outcome']=='FAIL' and result['original_checker_unchanged']
    elif change=='no_cleanup':assert not result['operational_continue']
    else:assert result is None


def test_complete_successor_preserves_original_scores_and_counts_all_attempts(monkeypatch):
    from collections import Counter
    from scripts import report_m63_retest as report
    from eval.m6_2.personal_closeout_r30 import validate_ai
    monkeypatch.setattr(report,'OUT',BASE)
    monkeypatch.setattr(report,'TASK_CONFIG',read('task-config.json'))
    monkeypatch.setattr(report,'REPORT_REVISION','continuation-r1')
    rows,attempts,records=report.load()
    assert attempts==[] and len(rows)==324 and len({r['run_id'] for r in rows})==324
    assert Counter(r['phase'] for r in rows)=={'acceptance':8,'main':200,'repeats':96,'variants':20}
    review=read('ai-output-review-continuation-r1.json');by=validate_ai(records,review)
    old=read('ai-output-review.json')
    assert all(by[i['case_id']]==i for i in old['items'])
    assert by['M63R3-repeats-B-r3-FC26-ORDER-010']['task_outcome']=='FAIL'
    assert by['M63R3-repeats-A-r3-FC26-ORDER-010']['task_outcome']=='FAIL'
    assert sum(r['usage'].get('unknown_tokens',False) for r in rows)==1
    assert all(r['cleanup']['residual_case_rows']==0 and r['cleanup']['lease_released'] for r in rows)
    summary=read('scores-and-evidence-continuation-r1.json')['summary']
    assert summary['experiment_complete'] and summary['not_run']==[]
    assert not summary['actual_usage_fully_reconciled']
    assert {a:summary['benchmark'][a]['all100']['n'] for a in ('A','B')}=={'A':79,'B':83}
    assert len(summary['stability_paired_trials'])==72 and len(summary['new_expression_pairs'])==10
    actual_families={r['design']['group_id'] for r in rows if r['phase']=='main'}
    assert len(actual_families)==24
    assert summary['actual_group_id_delta_intervals']['main']['independent_group_count']==len(actual_families)
    assert summary['actual_group_id_delta_intervals']['repeated']['independent_group_count']==15
    for arm in ('A','B'):
        selected=[r for r in rows if r['phase'] in {'main','repeats'} and r['source_case_id'] in read('design.json')['selected_24'] and r['design']['variant']==arm]
        assert len(selected)==72
        assert summary['stability_24x3'][arm]['single_PASS_n']==sum(by[r['case_id']]['task_outcome']=='PASS' for r in selected)
    assert summary['budget']['provider']==351 and summary['budget']['tokens'] is None
    assert summary['budget']['tokens_conservative_budget_consumption']-summary['budget']['known_recorded_tokens']==120000
