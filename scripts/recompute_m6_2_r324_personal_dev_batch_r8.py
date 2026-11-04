"""Offline R8 reconciliation from immutable run archive and journal evidence."""
from __future__ import annotations
import argparse, hashlib, json
from pathlib import Path
from eval.m6_1.personal_dev import implementation_digest
from eval.m6_1.personal_dev_batch_m62 import score_archive_r8

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / 'docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029'
DATA = ROOT / 'eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json'

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main() -> int:
    ap=argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args=ap.parse_args()
    manifest=json.loads((PKG/'batch-manifest-r8.json').read_text(encoding='utf-8'))
    closeout_path=PKG/'reports/closeout-r8.json'
    results_path=PKG/'reports/case-results-r8.json'
    journal_path=PKG/'execution-journal-r8-029-DE229C353034.jsonl'
    closeout=json.loads(closeout_path.read_text(encoding='utf-8'))
    results=json.loads(results_path.read_text(encoding='utf-8'))
    rows=[json.loads(x) for x in journal_path.read_text(encoding='utf-8').splitlines() if x.strip()]
    closes=[x for x in rows if x.get('event')=='CASE_CLOSEOUT']
    if (len(closes)!=1 or closes[0].get('case_id')!='D1-POLICY-017' or
        closes[0].get('archive_sha256') != sha(ROOT/closes[0]['archive_path']) or
        closeout.get('case_results_sha256') != sha(results_path) or
        closeout.get('journal_sha256') != sha(journal_path) or
        results.get('implementation_digest') != manifest['bindings']['implementation_digest']):
        raise SystemExit('R8_RAW_BINDING_VALIDATION_FAILED')
    row=closes[0]
    archive=json.loads((ROOT/row['archive_path']).read_text(encoding='utf-8'))
    bundle=json.loads(DATA.read_text(encoding='utf-8'))
    cases=[x for x in bundle['cases'] if x.get('case_id')==row['case_id']]
    if len(cases)!=1 or archive.get('case_id')!=row['case_id']:
        raise SystemExit('R8_CASE_BINDING_INVALID')
    grade=score_archive_r8(cases[0],archive,budget_snapshot=row['budget'],
        cleanup_status='RECEIPT_CLEANED_AND_LEASE_RELEASED' if row.get('cleanup',{}).get('cleanup_status')=='CLEANED_AND_RELEASED' else 'UNKNOWN')
    result=archive['result']; usage=result.get('result',{}).get('usage',{})
    out={"schema_version":"m6.2-r324-r8-postrun-reconciliation-v1",
      "classification":"DERIVED_REASSESSMENT_NO_AUTHORIZATION_EFFECT",
      "grading_status":"NOT_GRADED","reference_status":"reference_unreviewed",
      "batch_id":manifest['batch_id'],"authorization_id":closeout['authorization_id'],
      "historical_frozen_implementation_digest":manifest['bindings']['implementation_digest'],
      "current_reconciliation_implementation_digest":implementation_digest(ROOT),
      "dataset_sha256":manifest['dataset']['sha256'],"manifest_sha256":sha(PKG/'batch-manifest-r8.json'),
      "rules_sha256":sha(PKG/'locked-rules-r8.json'),
      "source_archive_path":row['archive_path'],"source_archive_sha256":row['archive_sha256'],
      "source_journal_path":"docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029/execution-journal-r8-029-DE229C353034.jsonl",
      "source_case_results_sha256":sha(results_path),"source_historical_grade_path":row['grader_path'],
      "source_historical_grade_sha256":row['grader_sha256'],"case_id":row['case_id'],"run_id":row['run_id'],
      "run_status":result['status'],"route":result.get('result',{}).get('route'),
      "delivered_answer":result.get('result',{}).get('answer'),
      "policy_retrieval":result.get('result',{}).get('policy_retrieval'),
      "provider_requests":usage.get('model_requests'),"tokens":usage.get('prompt_tokens',0)+usage.get('completion_tokens',0),
      "provider_usage_complete":usage.get('usage_complete'),"provider_usage_cost_estimate_usd":usage.get('estimated_cost_usd'),
      "local_ledger":row['budget'],"supplier_invoice":"UNKNOWN_BILLING","cleanup":row['cleanup'],
      "historical_stop_reason":"R8 grader unconditionally treated non-capability-rejection execution as blocking",
      "historical_closeout_decision":"STOP_AFTER_D1-POLICY-017; 15 CASES NOT_RUN",
      "derived_program_score":grade,"derived_continue_decision_does_not_reopen_or_resume_batch":True}
    target=args.output if args.output.is_absolute() else ROOT/args.output
    target.parent.mkdir(parents=True,exist_ok=True)
    target.write_text(json.dumps(out,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({"output":target.relative_to(ROOT).as_posix(),"sha256":sha(target),
      "derived_decision":grade.get('continue_decision'),"task_outcome":grade.get('task_outcome'),
      "provider_requests":out['provider_requests'],"tokens":out['tokens']},ensure_ascii=False))
    return 0
if __name__=='__main__': raise SystemExit(main())
