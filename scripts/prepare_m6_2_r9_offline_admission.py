"""Run exact R8-unconsumed cases through the real app with in-process model interception."""
from __future__ import annotations
import hashlib, json, sys, uuid
import argparse
from dataclasses import replace
from pathlib import Path
from sqlalchemy import text
ROOT=Path(__file__).resolve().parents[1]
for item in (ROOT,ROOT/'.vendor',ROOT/'.vendor/g2',ROOT/'tests'):
    sys.path.insert(0,str(item))
import test_m6_2_personal_dev_batch_r7 as helpers
from app.m0.config import load_settings
from app.m0 import config as config_module
from eval.m6_1.budget import BudgetLimits
from eval.m6_1.personal_dev import D1Coordinator, implementation_digest, plan_case
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_batch_m62 import DATASET_REL, TaskQuotaLedger, plan_without_unauthorized_targets, score_archive_r8
from eval.m6_1.personal_dev_runtime import execute_application_case

def sha(path): return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--successor3", action="store_true")
    parser.add_argument("--successor4", action="store_true")
    parser.add_argument("--policy018-recovery", action="store_true")
    parser.add_argument("--policy018-recovery-r2", action="store_true")
    parser.add_argument("--policy018-recovery-r3", action="store_true")
    parser.add_argument("--policy018-recovery-r4", action="store_true")
    parsed = parser.parse_args()
    if sum((parsed.successor3, parsed.successor4, parsed.policy018_recovery, parsed.policy018_recovery_r2, parsed.policy018_recovery_r3, parsed.policy018_recovery_r4)) > 1:
        raise SystemExit('select one successor version')
    successor3, successor4, recovery = parsed.successor3, parsed.successor4, (parsed.policy018_recovery or parsed.policy018_recovery_r2 or parsed.policy018_recovery_r3 or parsed.policy018_recovery_r4)
    recovery_r2, recovery_r3, recovery_r4 = parsed.policy018_recovery_r2, parsed.policy018_recovery_r3, parsed.policy018_recovery_r4
    pkg=ROOT/'docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029'
    output=pkg/('application-admission-offline-policy018-recovery-r4.json' if recovery_r4 else 'application-admission-offline-policy018-recovery-r3.json' if recovery_r3 else 'application-admission-offline-policy018-recovery-r2.json' if recovery_r2 else 'application-admission-offline-policy018-recovery-r1.json' if recovery else 'application-admission-offline-r7.json' if successor4 else 'application-admission-offline-r6.json' if successor3 else 'application-admission-offline-r5.json')
    if output.exists(): raise SystemExit('refusing overwrite')
    r8=ROOT/'docs/eval/m6.2-r324-40case-personal-dev-batch-r8-20261029'
    prior_manifest=json.loads((r8/'batch-manifest-r8.json').read_text(encoding='utf-8'))
    state=json.loads((r8/'execution-state-r8-029-DE229C353034.json').read_text(encoding='utf-8'))
    r8_remaining=[cid for cid in prior_manifest['proposed_order'] if state['attempt_count'].get(cid,0)==0]
    raw_quota=json.loads((pkg/'task-cumulative-ledger-r1.json').read_text(encoding='utf-8'))
    quota=TaskQuotaLedger(pkg/'task-cumulative-ledger-r1.json',task_id=raw_quota['task_id'],
        seed_sha256=raw_quota['seed_sha256']).read()
    ids=(['D1-POLICY-018'] if recovery else [cid for cid in r8_remaining if quota.get('attempts',{}).get(cid,0)==0])
    if not recovery:
        assert len(r8_remaining)==15 and len(ids)==14 and ids[0]=='D1-AFTERSALES-023'
    matrix=json.loads((ROOT/'docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json').read_text(encoding='utf-8'))
    source={row['case_id']:row for row in matrix['cases']}
    bundle=json.loads((ROOT/DATASET_REL).read_text(encoding='utf-8'))
    cases={row['case_id']:row for row in bundle['cases']}
    isolated_root=ROOT/'data/eval/offline-tests'/f'm62-r9-app-admission-{uuid.uuid4().hex}'
    isolated_root.mkdir(parents=True)
    target=PersonalDevDatabase(isolated_root); engine,factory=target.open(initialize=True); coordinator=D1Coordinator(target,factory)
    records=[]
    limits=BudgetLimits(provider_calls=40,embedding_calls=0,judge_calls=0,total_tokens=600000,
        estimated_usd=2.0,tool_calls=20,max_calls_per_trial=20,max_concurrent_trials=1,
        max_duration_seconds=900,max_retries=0)
    try:
        for case_id in ids:
            plan=plan_case(bundle,case_id)
            allowed=source[case_id]['allowed_read_only_capabilities']
            if not allowed: plan=plan_without_unauthorized_targets(plan)
            sdk=helpers._InterceptedSDK()
            config_module.create_deepseek_client=lambda *a,_sdk=sdk,**k:_sdk
            settings=replace(load_settings(require_api_key=False),api_key='offline-only',
                database_url=f'sqlite:///{target.path.as_posix()}',orchestrator='langgraph',m3_supervisor_enabled=True)
            receipt=coordinator.prepare(plan)
            archive_path=ROOT/'data/eval/offline-tests'/f'm62-r9-app-{case_id.lower()}-{uuid.uuid4().hex}.json'
            try:
                result=execute_application_case(coordinator,plan,receipt,helpers._TestLedger(limits),settings)
                archive={'case_id':case_id,'result':result,'model_transport':'IN_PROCESS_INTERCEPTOR_NO_NETWORK'}
                archive_path.write_text(json.dumps(archive,ensure_ascii=False,indent=2,default=str)+'\n',encoding='utf-8')
                archive=json.loads(archive_path.read_text(encoding='utf-8'))
                safety=coordinator.diagnostic_cleanup_safety(receipt,archive_path)
                if safety.get('safe_to_cleanup') is True: coordinator.cleanup(receipt)
                cleanup=coordinator.diagnostic_cleanup_evidence(receipt,cleaned=bool(safety.get('safe_to_cleanup')))
                observations=result.get('observations') or {}; events=observations.get('run_events',[])
                route=result.get('result',{}).get('route',{}) if isinstance(result.get('result'),dict) else {}
                expected={row['tool_id'] for row in cases[case_id].get('input',{}).get('expected_tool_calls',[]) if row.get('mode')=='read_only'}
                business_expected=expected-{'search_knowledge'}
                grade=score_archive_r8(cases[case_id],archive,budget_snapshot={
                    'snapshot_kind':'FINAL','complete':True,'settled':True,'active_reservations':0,
                    'integrity_errors':[],'unknown_tokens':False,'unknown_cost':False,
                    'calls_by_category':{'provider':len(sdk.requests),'tool':sum(e.get('event_type') in {'tool_finished','policy_retrieval_finished'} for e in events),'judge':0,'embedding':0},
                    'tokens':90*len(sdk.requests),'estimated_usd':.001*len(sdk.requests)},
                    cleanup_status='RECEIPT_CLEANED_AND_LEASE_RELEASED' if cleanup.get('cleanup_status')=='CLEANED_AND_RELEASED' else 'UNKNOWN')
                observed=set(grade.get('observed_tools',[]))
                policy_ok=('search_knowledge' not in expected or grade['program_checks'].get('policy_retrieval_audit')=='PASS')
                expected_ok=(business_expected.issubset(observed) and policy_ok) if expected else (route.get('strategy') in {'DIRECT','REFUSE'} and not observed and not grade.get('policy_retrieval_call_ids'))
                ready=(result.get('status') in {'SUCCEEDED','FAILED','MODEL_UNAVAILABLE'} and
                    bool(result.get('run_id')) and bool(observations.get('traces')) and
                    set(route.get('required_capabilities',[])).issubset(set(allowed)) and expected_ok and
                    cleanup.get('cleanup_status')=='CLEANED_AND_RELEASED' and cleanup.get('residual_case_rows')==0 and
                    grade.get('program_checks',{}).get('run_trace_association')=='PASS' and
                    grade.get('program_checks',{}).get('tool_audit_completeness')=='PASS' and
                    grade.get('program_checks',{}).get('tool_result_audit_linkage')=='PASS')
                records.append({'case_id':case_id,'application_admission':'APPLICATION_READY' if ready else 'BLOCKED_ENGINEERING',
                    'run_status':result.get('status'),'route':route,'expected_capabilities':sorted(expected),
                    'observed_tools':sorted(observed),'intercepted_provider_requests':len(sdk.requests),
                    'offline_only':True,'run_id':result.get('run_id'),'archive_path':archive_path.relative_to(ROOT).as_posix(),
                    'archive_sha256':sha(archive_path),'program_score':grade,'cleanup':cleanup,
                    'blocked_reason':None if ready else 'APPLICATION_PATH_OR_SCOPE_OR_EVIDENCE_CHECK_FAILED'})
            finally:
                with factory() as db: active=db.execute(text('SELECT lease_id FROM d1_personal_target')).scalar_one()
                if active==receipt.lease_id: coordinator.cleanup(receipt)
        report={'schema_version':('m62-policy018-recovery-offline-application-v1' if recovery else 'm62-r9s4-application-admission-offline-r7' if successor4 else 'm62-r9s3-application-admission-offline-r6' if successor3 else 'm62-r9s2-application-admission-offline-r5'),'case_count':len(ids),'case_ids':ids,
            'records':records,'application_ready_count':sum(r['application_admission']=='APPLICATION_READY' for r in records),
            'blocked_count':sum(r['application_admission']!='APPLICATION_READY' for r in records),
            'dataset_sha256':sha(ROOT/DATASET_REL),'model_transport':'IN_PROCESS_INTERCEPTOR_NO_NETWORK',
            'external_calls':0,'formal_run_created':False,'offline_isolated_run_records':len(records),
            'implementation_digest':implementation_digest(ROOT),'isolated_database_identity':target.identity,
            'isolated_database_path':target.path.relative_to(ROOT).as_posix(),
            'predecessor_manifest_sha256':sha(r8/'batch-manifest-r8.json'),
            'predecessor_state_sha256':sha(r8/'execution-state-r8-029-DE229C353034.json'),
            'r9_prior_task_consumed_case_ids':sorted(cid for cid in r8_remaining if quota.get('attempts',{}).get(cid,0)!=0),
            'task_ledger_state_sha256_before_offline_replay':quota['state_sha256']}
        if report['application_ready_count']!=len(ids): raise SystemExit(f"application admission blocked: {report['application_ready_count']}/{len(ids)}")
        output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps({'status':'PASS','case_count':len(ids),'application_ready':len(ids),'external_calls':0,
            'implementation_digest':report['implementation_digest'],'report':output.relative_to(ROOT).as_posix()}))
    finally: engine.dispose()
if __name__=='__main__': main()
