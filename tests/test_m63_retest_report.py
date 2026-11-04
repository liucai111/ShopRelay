"""Read-only real evidence checks and in-memory negative review mutations."""
import base64,json,shutil,sqlite3
from pathlib import Path
import pytest
from scripts import report_m63_retest as report
from eval.m6_2 import personal_closeout_r30 as existing


def test_written_report_round_trip_recomputes(monkeypatch, tmp_path):
    output = {}
    original_read = report.read
    class Destination:
        def write_text(self, content, **kwargs):
            output.update(json.loads(content))
    class OutputRoot:
        def __truediv__(self, name):
            if name == 'scores-and-evidence.json':
                return Destination()
            return report.ROOT / 'docs/eval/m6.3-repair-retest-20261102' / name
    def read(path):
        return output if isinstance(path, Destination) else original_read(path)
    rows, attempts, records = report.load()
    monkeypatch.setattr(report, 'load', lambda: (rows, attempts, records))
    monkeypatch.setattr(report, 'OUT', OutputRoot())
    monkeypatch.setattr(report, 'read', read)
    monkeypatch.setattr(report, 'write_readable', lambda value: None)
    report.build()
    report.build(check=True)


def test_actual_recompute_has_100_main_and_no_missing_cleanup():
    rows,_,_=report.load()
    assert len(rows)==118
    main=[r for r in rows if r['phase']=='main']
    assert len(main)==100 and len({r['runtime'] for r in main})==1
    assert all(r['cleanup']['residual_case_rows']==0 and r['cleanup']['lease_released'] for r in rows)
    assert sum(r['answer'] is None for r in main)==13
    labels=report.read(report.OUT/'ai-output-review.json')
    by=existing.validate_ai(existing.load_trials(report.ROOT),labels)
    assert all(by[r['case_id']]['task_outcome']=='FAIL' for r in main if r['answer'] is None)


@pytest.mark.parametrize('mutation',['raw','run','human','false_pass','duplicate'])
def test_review_drift_and_known_failure_cannot_pass(mutation):
    existing.OUT=report.OUT.relative_to(report.ROOT)
    rows=existing.load_trials(report.ROOT);review=report.read(report.OUT/'ai-output-review.json')
    if mutation=='raw':review['items'][0]['raw_sha256']='0'*64
    elif mutation=='run':review['items'][0]['run_id']='OTHER'
    elif mutation=='human':review['items'][0]['human_decision']='PASS'
    elif mutation=='false_pass':
        failed=next(r for r in rows if r['raw']['code_assessment']['task_outcome']=='FAIL')
        next(i for i in review['items'] if i['case_id']==failed['case']['case_id'])['task_outcome']='PASS'
    else:review['items'][0]=review['items'][1]
    with pytest.raises(ValueError):existing.validate_ai(rows,review)


def test_real_closed(tmp_path,monkeypatch):
    from eval.m6_1.container_boundary import read_grant
    from eval.m6_1.grants import FormalGrantAuthority,GrantDenied
    from eval.m6_1.registry import DatasetRegistry
    from eval.m6_1.store import ResultStore
    cfg=report.read(report.OUT/'request-variants-1.json')['config']
    source=sqlite3.connect('file:'+(report.OUT/'evidence/results.sqlite3').as_posix()+'?mode=ro',uri=True)
    ledger=tmp_path/cfg['plan']['ledger_path'];ledger.parent.mkdir(parents=True)
    dest=sqlite3.connect(ledger);source.backup(dest);dest.close();source.close()
    store=ResultStore(ledger)
    with store.transaction(immediate=False) as db:
        payload=db.execute('SELECT payload_json FROM formal_grants WHERE approval_ref=?',(cfg['plan']['approval_ref'],)).fetchone()[0]
    grant=read_grant(json.loads(payload))
    for name,digest in grant.plan.runtime_bindings:
        actual=report.OUT/'authorization.json' if name.endswith('retest-authorization.json') else report.ROOT/name
        target=tmp_path/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(actual,target)
    authority=FormalGrantAuthority(DatasetRegistry.from_dict(report.read(report.OUT/'request-variants-1.json')['registry']),
        repository_root=tmp_path,store=store,issuer=cfg['issuer'],trusted_issuers=set(cfg['trusted_issuers']),signing_key=None,
        verification_key=base64.b64decode(cfg['verification_public_key_base64']),approved_plan_hashes=set(cfg['approved_plan_hashes']),
        allowed_targets={tuple(v) for v in cfg['allowed_targets']})
    # Recreate the recorded runtime precondition for this isolated historical
    # closure test. Otherwise a newer source tree is correctly refused earlier
    # for digest drift and never reaches the independently required close gate.
    monkeypatch.setattr('eval.m6_1.grants.formal_runtime_digest',lambda:grant.plan.execution_implementation_sha256)
    with pytest.raises(GrantDenied,match='closed'):
        authority.verify(grant,subject=cfg['subject'],mode='PERSONAL_REAL_EVAL',config_sha256=grant.plan.config_sha256,target=tuple(cfg['actual_target_binding']))
    assert len(store.trials(cfg['experiment_id']))==10
