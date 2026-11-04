"""Real archived pair/repeat evidence and strict review boundaries, offline only."""
import json
import contextlib
import io
import sqlite3
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import report_m63_retest as report
from eval.m6_2 import personal_closeout_r30 as existing

BASE=Path(__file__).resolve().parents[1]/'docs/eval/m6.3-repair-ab-r3-20261102'


@pytest.fixture(scope='module')
def evidence():
    old_out,old_config=report.OUT,report.TASK_CONFIG
    report.OUT=BASE
    report.TASK_CONFIG=json.loads((BASE/'task-config.json').read_text(encoding='utf8'))
    try:
        rows,attempts,records=report.load()
        review=json.loads((BASE/'ai-output-review.json').read_text(encoding='utf8'))
        yield rows,attempts,records,review
    finally:
        report.OUT,report.TASK_CONFIG=old_out,old_config


def test_all_predetermined_trials_and_independent_accounting(evidence):
    rows,attempts,records,review=evidence
    by=existing.validate_ai(records,review)
    assert attempts==[]
    assert Counter(r['phase'] for r in rows)=={'acceptance':8,'main':200,'repeats':59}
    assert len({r['run_id'] for r in rows})==267
    assert all(r['cleanup']['residual_case_rows']==0 and r['cleanup']['lease_released'] for r in rows)
    actual=json.loads((BASE/'scores-and-evidence.json').read_text(encoding='utf8'))['summary']
    for arm in ('A','B'):
        main=[r for r in rows if r['phase']=='main' and r['design']['variant']==arm]
        labels=Counter(by[r['case_id']]['task_outcome'] for r in main)
        assert len(main)==100
        assert actual['benchmark'][arm]['all100']['counts']==dict(labels)
        selected=set(json.loads((BASE/'design.json').read_text(encoding='utf8'))['selected_24'])
        repeated=[r for r in rows if r['phase'] in {'main','repeats'} and r['source_case_id'] in selected and r['design']['variant']==arm]
        groups={s:[r for r in repeated if r['source_case_id']==s] for s in selected}
        assert all(len({r['design']['round'] for r in rs})==len(rs) and {r['design']['round'] for r in rs}<={1,2,3} for rs in groups.values())
        assert actual['stability_24x3'][arm]['single_PASS_n']==sum(by[r['case_id']]['task_outcome']=='PASS' for r in repeated)
        assert actual['stability_24x3'][arm]['all_three_n']==sum(len(rs)==3 and all(by[r['case_id']]['task_outcome']=='PASS' for r in rs) for rs in groups.values())
    assert len(actual['main_pairs'])==100 and len(actual['stability_paired_trials'])==53
    assert not actual['experiment_complete'] and len(actual['not_run'])==57
    assert Counter(r['phase'] for r in actual['not_run'])=={'repeats':37,'variants':20}
    assert actual['target_B_gt85_and_above_A']==(actual['benchmark']['B']['all100']['n']>=86 and actual['strict_B_minus_A_pp']>0)
    assert actual['budget']['provider']==sum(r['usage']['calls_by_category']['provider'] for r in rows)
    assert actual['budget']['tokens'] is None and actual['budget']['estimated_usd'] is None
    assert actual['budget']['tokens_conservative_budget_consumption']==sum(r['usage']['tokens'] for r in rows)
    assert actual['budget']['known_recorded_tokens']==sum(r['usage']['tokens'] for r in rows if not r['usage'].get('unknown_tokens'))
    assert sum(r['usage'].get('unknown_tokens',False) for r in rows)==1


@pytest.mark.parametrize('mutation',['run','raw','missing','duplicate','human','false_pass','false_partial'])
def test_wrong_binding_and_known_task_failure_never_upgraded(evidence,mutation):
    rows,_,records,original=evidence
    review=deepcopy(original)
    if mutation=='run':review['items'][0]['run_id']='wrong'
    elif mutation=='raw':review['items'][0]['raw_sha256']='0'*64
    elif mutation=='missing':review['items'].pop()
    elif mutation=='duplicate':review['items'][0]=review['items'][1]
    elif mutation=='human':review['items'][0]['human_decision']='PASS'
    else:
        rejected=next(r for r in rows if r['answer'] is None)
        next(i for i in review['items'] if i['case_id']==rejected['case_id'])['task_outcome']='PASS' if mutation=='false_pass' else 'PARTIAL'
    with pytest.raises(ValueError):existing.validate_ai(records,review)


def test_accounting_retains_null_usage_and_conservative_reserve(tmp_path,monkeypatch):
    from scripts import manage_m63_retest as manager
    ledger=tmp_path/'ledger';ledger.mkdir()
    source=sqlite3.connect('file:'+(BASE/'evidence/results.sqlite3').as_posix()+'?mode=ro',uri=True)
    count=source.execute('select count(*) from trials').fetchone()[0]
    target=sqlite3.connect(ledger/'results.sqlite3');source.backup(target);source.close();target.close()
    out=tmp_path/'output';out.mkdir()
    monkeypatch.setattr(manager,'LEDGER',ledger.as_posix())
    monkeypatch.setattr(manager,'OUT',out);monkeypatch.setattr(manager,'ROOT',tmp_path)
    def local_control(code):
        capture=io.StringIO()
        with contextlib.redirect_stdout(capture):exec(code,{})
        return json.loads(capture.getvalue())
    monkeypatch.setattr(manager,'control',local_control)
    manager.accounting()
    actual=json.loads((out/f'accounting-{count}.json').read_text(encoding='utf8'))
    assert actual['tokens'] is None and actual['estimated_usd'] is None
    assert actual['unknown_usage_provider_calls']==1 and not actual['known_settled']
    assert actual['tokens_conservative_budget_consumption']-actual['known_recorded_tokens']==120000
    assert actual['estimated_usd_conservative_budget_consumption']-actual['known_recorded_estimated_usd']==pytest.approx(.5)


def test_manifest_question_drift_rejected_before_regrading(evidence,monkeypatch):
    original_read=report.read
    def changed(path):
        value=original_read(path)
        if Path(path)==BASE/'design.json':
            value['phases']['main'][0]['input']['user_query']+=' altered question'
        return value
    monkeypatch.setattr(report,'read',changed)
    with pytest.raises(ValueError,match='frozen design differs'):
        report.load()
