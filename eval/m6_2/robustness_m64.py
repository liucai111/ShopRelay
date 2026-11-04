"""Offline projection of M6.4 engineering evidence; never runs a model or a trial."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sys
import xml.etree.ElementTree as ET
from datetime import datetime,timezone


def build(root: Path, *, revision: str = 'r1') -> dict:
    from app.m2.self_rag import _answer_semantic_issues,_policy_window_facts,_source_window_facts
    from eval.m6_1.grants import formal_runtime_digest
    from eval.m6_1.personal_dev import implementation_digest
    root=root.resolve()
    if revision not in {'r1','r2'}: raise ValueError('unsupported M6.4 evidence revision')
    prior=root/'docs/eval/m6.4-lean-acceptance-20261101'
    out=prior if revision=='r1' else root/'docs/eval/m6.4-lean-acceptance-r2-20261101'
    out.mkdir(exist_ok=True)
    def read(path):return json.loads((root/path).read_text(encoding='utf8'))
    def write(name,value):
        (out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    suites={}
    for label,path in [('affected','docs/probes/m64-policy-final.xml' if revision=='r1' else 'docs/probes/m64-n01-final-r2.xml'),
            ('monitor','docs/probes/m64-monitor-final.xml'),('postgres','docs/probes/m64-pg-completion.xml')]:
        tree=ET.parse(root/path)
        cases=list(tree.iter('testcase'))
        suites[label]={'path':path,'tests':len(cases),'failed':sum(t.find('failure') is not None for t in cases),
            'errors':sum(t.find('error') is not None for t in cases),'skipped':sum(t.find('skipped') is not None for t in cases),
            'properties':[{**p.attrib,'test':t.attrib['name']} for t in cases for p in t.iter('property')]}
        if revision=='r2':suites[label]['evidence_scope']='CURRENT_R2_OFFLINE_APPLICATION' if label=='affected' else 'UNCHANGED_R1_COMPONENT_REUSED_NOT_RERUN'
        if suites[label]['failed'] or suites[label]['errors'] or suites[label]['skipped']:
            raise ValueError('current acceptance suite is not all executed successfully: '+label)
    health=read('docs/probes/m64-pg-health-completion.json')
    assert health['before']['marker']==health['after']['marker']
    assert not any(health['after']['counts'].values()) and not health['external_connection_attempts']
    from eval.m6_1.store import ResultStore
    from eval.m6_1.contracts import EvalCase
    from eval.m6_1.formal_scoring import evaluate
    evidence_root=root/'docs/probes/m64-pg-cli-completion'
    pg_proof=json.loads((evidence_root/'r25-proof.json').read_text(encoding='utf8'))
    store=ResultStore(evidence_root/'ledger/results.sqlite3')
    rescored=[]
    for trial in store.trials('unprotected-full-app-test'):
        raw=store.raw_result(trial['trial_id'])
        case=EvalCase.from_dict(json.loads((evidence_root/raw['case_path']).read_text(encoding='utf8')))
        assert evaluate(case,raw['application_archive'],trial['usage'])==raw['code_assessment'],trial['case_id']
        cleanup=json.loads((evidence_root/trial['raw_result_ref']).with_suffix('.cleanup.json').read_text(encoding='utf8'))
        assert cleanup['evidence']['residual_case_rows']==0
        run_id=raw['application_archive']['result']['run_id']
        checkpoint=json.loads((evidence_root/'ledger/application'/f'{run_id}.checkpoint-cleanup.json').read_text(encoding='utf8'))
        assert checkpoint['run_id']==run_id and checkpoint['deleted_exact_thread'] is True
        rescored.append({'trial_id':trial['trial_id'],'case_id':case.case_id,'raw_ref':trial['raw_result_ref'],
            'score_matches':True,'cleanup_residual':0,'exact_checkpoint_thread_deleted':True})
    assert len(rescored)==pg_proof['fictional_cases']==13
    event_path=prior/'execution-approval.json'
    if not event_path.exists():
        write('execution-approval.json',{'event_id':'M64-LEAN-EXECUTION-USER-APPROVAL-20261101',
            'actor':'CHAT_USER_SELF_CONFIRMED','kind':'EXECUTION_PERMISSION_NOT_TASK_GRADING',
            'source_thread':'01a1803e-e8da-7ac1-921a-741806cfda36',
            'source_turn':'01a19624-468a-7422-beea-85df4f177a3d',
            'source_message':'01a19624-4727-7b83-ab00-0bd9f8829335',
            'source_turn_started_at':datetime.fromtimestamp(1793520322,timezone.utc).isoformat(),
            'recorded_at':datetime.now(timezone.utc).isoformat(),
            'confirmed_text':'按方案集中执行，并允许确实缺少真实模型证据时最多4个只读Run、USD1；证据充分则零付费调用。',
            'plan':'docs/eval/m6.4-lean-execution-plan-20261101.md',
            'scope':'20 engineering check groups plus bounded positive/negative policy replay; no production data or formal Gate change',
            'overhead_threshold':'20% and 20ms diagnostic only, not a hard exit condition',
            'paid_condition_status':'NOT_ACTIVATED; known offline parser gap remains'})
    else:
        assert json.loads(event_path.read_text(encoding='utf8'))['source_message']=='01a19624-4727-7b83-ab00-0bd9f8829335'
    old='docs/eval/m6.3-personal-execution-20261101/'
    accepted=read(old+'final-stage-closeout-r1.json')
    prior_verification=read(old+'final-closeout-verification-r1.json')
    source_binding=next(r for r in prior_verification['source_bindings']
        if r['path']==old+'candidate-ablation-observations-final.json')
    source_bytes=(root/source_binding['path']).read_bytes()
    assert hashlib.sha256(source_bytes).hexdigest()==source_binding['sha256'],'historical candidate evidence changed'
    originals=json.loads(source_bytes)['items']
    bounds={r['run_id']:r for r in accepted['candidate_items']}
    before_parser=None
    if revision=='r2':
        import importlib.util
        snapshot=prior/'source-r1'
        snapshot_bindings=json.loads((snapshot/'snapshot.json').read_text(encoding='utf8'))['files']
        for name,digest in snapshot_bindings.items():
            assert hashlib.sha256((snapshot/name).read_bytes()).hexdigest()==digest
        spec=importlib.util.spec_from_file_location('_m64_historical_policy_r1',snapshot/'app/m2/self_rag.py')
        before_parser=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=before_parser
        # Loading a bound historical snapshot must not create __pycache__ in
        # that immutable evidence directory.
        exec(compile((snapshot/'app/m2/self_rag.py').read_bytes(),str(snapshot/'app/m2/self_rag.py'),'exec'),vars(before_parser))
    rows=[]
    for row in originals:
        decision=bounds[row['run_id']]
        if decision['accepted_diagnostic']!='REJECTED_RELEVANT_DURATION_ASSERTIONS_SUPPORTED_ONLY':continue
        assert row['raw_sha256']==decision['raw_sha256']
        answer=row['candidate']['redacted_text'];sources=row['citation_sources']
        facts=_policy_window_facts(answer)
        source_values={(f['amount'],f['unit']) for s in sources for f in _source_window_facts(s)}
        value_ok=bool(facts) and all((f['amount'],f['unit']) in source_values for f in facts)
        findings=_answer_semantic_issues(answer,sources)
        original_windows={f['window'] for f in row['candidate']['validation_findings'] if 'window' in f}
        remaining=[f for f in findings if f.get('window') in original_windows]
        rows.append({'id':'N01-'+str(len(rows)+1),'case_id':row['case_id'],'run_id':row['run_id'],
            'raw_sha256':row['raw_sha256'],'accepted_scope':decision['accepted_diagnostic'],
            'candidate_is_delivered':False,'candidate_text':answer,'selected_sources':sources,
            'parsed_assertions':facts,'value_and_binding_check':'PASS' if value_ok else 'FAIL',
            'original_rejected_windows':sorted(original_windows),'remaining_relevant_findings':remaining,
            'complete_candidate_findings':findings,
            'assertion_replay_status':'REMAINS_REJECTED_OR_UNRESOLVED' if remaining else 'ORIGINAL_WINDOW_FINDINGS_CLEARED',
            'whole_answer_correctness':'NOT_INFERRED_FROM_ASSERTION_ACCEPTANCE'})
        if before_parser is not None:
            rows[-1]['before_assertions']=before_parser._policy_window_facts(answer)
            rows[-1]['before_findings']=before_parser._answer_semantic_issues(answer,sources)
            source_facts=[{'evidence_id':s['chunk_id'],'assertion':f} for s in sources for f in _source_window_facts(s)]
            rows[-1]['source_assertions']=source_facts
            rows[-1]['local_scope_witnesses']=[{'candidate_assertion':f,
                'current_source_candidates':[s for s in source_facts
                    if s['assertion']['amount']==f['amount'] and s['assertion']['unit']==f['unit']
                    and s['assertion']['subject']==f['subject']],
                'unasserted_fields': ['omitted prerequisites are not asserted or approved'] if f.get('scope')=='REFERENCE_ONLY' else []}
                for f in facts if any(w.startswith(str(f['amount'])) for w in original_windows)]
    assert len(rows)==31
    # Reuse the exact test vectors, rather than maintaining a second set of
    # answer thresholds in the report. Import does not execute any pytest test.
    sys.path.insert(0,str(root/'tests'))
    from test_m6_4_core import POLICY_PAIRS
    for i,(source,answer,old_text,new_text) in enumerate(POLICY_PAIRS):
        sources=[{'chunk_id':'policy A','content':source}]
        if i in (0,7):sources.append({'chunk_id':'policy B','content':'Gift card does not expire.'})
        for negative in (False,True):
            text=answer.replace(old_text,new_text) if negative and old_text!='__citation__' else answer
            citations=['foreign'] if negative and old_text=='__citation__' else [r['chunk_id'] for r in sources]
            findings=_answer_semantic_issues(text,sources)
            valid=bool(citations) and set(citations).issubset({s['chunk_id'] for s in sources})
            check=bool(findings) or not valid
            assert check is negative
            rows.append({'id':f'N{3 if negative else 2:02d}-{i+1}','answer':text,'citations':citations,
                'sources':sources,'expected':'REJECT' if negative else 'ACCEPT','semantic_findings':findings,
                'citation_scope':'PASS' if valid else 'FAIL','regression':'PASS',
                'application_integration_test':f'test_complete_policy_answers_through_actual_cli[{i}]'})
    for bad in (False,True):
        sources=[{'chunk_id':'general','content':'General warranty is valid for 18 months.'},
            {'chunk_id':'specific','content':'Device warranty is valid for 24 months.'}]
        answer='General warranty is valid for 18 months. Device warranty is valid for 24 months.'
        if bad:answer=answer.replace('General warranty is valid for 18','General warranty is valid for 24')
        findings=_answer_semantic_issues(answer,sources)
        assert bool(findings) is bad
        rows.append({'id':'N04-'+str(int(bad)+1),'answer':answer,'sources':sources,
            'expected':'REJECT_WRONG_SCOPE' if bad else 'ACCEPT_DISTINCT_SCOPES','findings':findings,'regression':'PASS',
            'historical_074':'SCOPE_CONFLATION_NOT_CLEAN_FALSE_REJECTION; original Task FAIL unchanged'})
    assert len(rows)==49
    write('policy-regression.json',{'scope':'31 accepted local assertions, 8 complete positives, 8 counterexamples, 2 scope checks',
        'rows':rows,'relevant_replay_cleared':sum(not r['remaining_relevant_findings'] for r in rows[:31]),
        'historical_assertion_count':31,'full_answer_false_rejection_rate':'N/A; 31 were not accepted as whole-answer Gold'})
    checks=[]
    evidence={
        'P01':'PG authenticated owner/foreign Run and event API; PG repository foreign order refused',
        'P02':'actual CLI scope rejection, no unauthorized reads, complete audit, safe continue',
        'P03':'policy injection reaches actual SDK request; expected model output intercepted; guard/cleanup checked',
        'P04':'receipt-backed tool result injection reaches actual SDK request; scope stays unchanged',
        'C01':'PG 7 owner action interactions; pending zero, committed one; actual refund/return/cancel/ticket effects',
        'C02':'PG reject/leave pending zero; wrong owner/hash rejected; expiry tests reused',
        'C03':'two authenticated concurrent HTTP confirmation requests, one effect; app rebuild replay one effect',
        'C04':'transaction rollback and unknown-result no replay tests',
        'R01':'intercepted transport/HTTP/schema/usage tests; no live Provider probe',
        'R02':'SQLAlchemy/timeout failure distinct from successful empty/NOT_FOUND domain result',
        'R03':'actual loopback TCP SSE disconnect on SQLite plus current PG durable checkpoint reopen and app recreation/cursor replay; not a combined PG worker-kill trial',
        'R04':'post-execution checkpoint fault UNKNOWN_RESULT and refusal to replay',
        'R05':'actual multi-case CLI quality/rejection continue, missing accounting/audit and cleanup failure stop; ledger resume',
        'O01':'persistent Run/Trace/metrics, deny-by-default admin, bounded sanitized projections; retention via exact receipt and exported evidence, no automatic TTL service',
        'O02':'ISO dates and numeric usage retained; explicit credentials/phone redacted',
        'O03':'persistent local database-failure alert trigger, 60-second injected-clock cooldown, recovery; PG roundtrip',
        'N01':'31 precise Run/source/value bindings verified, but original relevant-window scope findings remain; not full candidate acceptance',
        'N02':'8 full legal answers pass actual SDK/application/persistence/grading/cleanup with fake Provider',
        'N03':'8 mutations rejected, delivery FAIL retained, audited single-case rejection continues',
        'N04':'general vs specific values not interchangeable; original 074 remains a scope limitation, not a clean false rejection'}
    if revision=='r2':
        evidence['N01']='31 real local-window assertions clear original findings; explicit clocks, objects, conditions and source attribution are behavior-tested; complete candidates not promoted'
    remaining=sum(bool(r['remaining_relevant_findings']) for r in rows[:31])
    for key,note in evidence.items():
        checks.append({'id':key,'status':'PARTIAL' if key=='N01' and remaining else 'PASS','evidence':note})
    write('acceptance-matrix.json',{'checks':checks,'passed':sum(c['status']=='PASS' for c in checks),
        'partial':sum(c['status']=='PARTIAL' for c in checks),'all_required_conditions_met':not remaining})
    changed=['app/m1/chat.py','app/m2/policy_execution.py','app/m2/self_rag.py','app/orchestration/persistence.py',
        'tests/test_m6_4_core.py','tests/test_m6_4_postgres.py','tests/test_m6_2_r25_persistent_actions.py',
        'tests/test_langgraph_g2.py','scripts/manage_m6_2_container.py','docs/probes/run_m64_container_tests.py',
        'eval/m6_2/robustness_m64.py','scripts/finalize_m6_2_r10_closeout.py']
    if revision=='r2':changed.append('tests/test_m6_4_policy_scope.py')
    bindings={'implementation_sha256':implementation_digest(root),'execution_runtime_sha256':formal_runtime_digest(),
        'files':{p:hashlib.sha256((root/p).read_bytes()).hexdigest() for p in changed}}
    if revision=='r1':
        assert (root/'docs/probes/m64-completion-runtime.txt').read_text(encoding='utf8').strip()==bindings['execution_runtime_sha256']
    else:
        previous=read('docs/eval/m6.4-lean-acceptance-20261101/verification.json')
        unchanged=('app/m1/chat.py','app/m2/policy_execution.py','app/orchestration/persistence.py',
            'tests/test_m6_4_postgres.py','tests/test_m6_2_r25_persistent_actions.py','tests/test_langgraph_g2.py')
        for name in unchanged:
            assert bindings['files'][name]==previous['bindings']['files'][name], 'reused evidence component changed: '+name
        bindings['reused_component_sha256']={name:bindings['files'][name] for name in unchanged}
        bindings['prior_engineering_runtime_sha256']=previous['bindings']['execution_runtime_sha256']
        bindings['reuse_boundary']='PG/monitor proof applies to unchanged components; current semantic behavior has separate offline CLI evidence, not a fresh PG execution'
    write('verification.json',{'tests':suites,'target':health,'bindings':bindings,'historical_candidate_source_binding':source_binding,
        'container_image':((root/'docs/probes/m64-completion-image.txt').read_text(encoding='utf8').strip() if revision=='r1' else
            {'image_id':(root/'docs/probes/m64-completion-image.txt').read_text(encoding='utf8').strip(),
                'scope':'R1_PG_ENGINEERING_ONLY_NOT_R2_PARSER_IMAGE','current_long_lived_application':'M63_UNCHANGED_NOT_DEPLOYED'}),
        'container_boundary_evidence':'docs/probes/m64-container-boundary-completion.txt',
        'raw_code_recomputation':rescored,'fake_cli_proof':'docs/probes/m64-pg-cli-completion/r25-proof.json',
        'engineering_only':{'PG_CLI_trials':13,'scripted_owner_interactions':7,'durable_checkpoint_reopens':13,
            'additional_action_context_Run':1,'SQLite_TCP_disconnect_Run':1,'not_paid_or_real_model_trials':True},
        'budget':{'new_real_Runs':0,'Provider':0,'Judge':0,'Embedding':0,'tokens':0,'local_estimate_usd':0,
            'conditional_limit':{'Runs':4,'Provider':16,'tokens':240000,'usd':1},
            'activation':'NOT_ACTIVATED; offline evidence sufficient' if revision=='r2' else 'NOT_ACTIVATED; known offline scope-parser gap is not grounds for new model sampling'},
        'historical_m63':'250 consumed claims remain closed; unchanged historical results and invoices UNKNOWN',
        'recompute':'python scripts/finalize_m6_2_r10_closeout.py --version m64'+(' --revision r2' if revision=='r2' else '')+'; offline only'})
    stage='COMPLETE' if not remaining else 'IN_PROGRESS_KNOWN_POLICY_SCOPE_GAP'
    write('exit-matrix.json',{'M6_1':'COMPLETE','M6_2_personal':'COMPLETE','M6_3_personal':'COMPLETE',
        'M6_4_personal':stage,'engineering_execution':'COMPLETE','security_violation_observed':0,
        'normal_business_gate':'NOT_FULLY_ESTABLISHED' if remaining else 'FIXED_REGRESSION_PASSED',
        'remaining_action':f'{remaining}/31 accepted relevant-window assertions still have original-window parser findings; improve local subject/condition attribution without granting whole-answer correctness or rerunning M6.3',
        'M6_5':'NOT_STARTED','strict_formal_Gates':'UNCHANGED',
        'excluded':'Embedding/Qdrant/MCP full failure matrix; electrical 033; real LLM injection answer quality; automatic retention service'})
    lines=['# M6.4个人精简验收结果','',
        f'工程执行完成；阶段状态 **{stage}**。20组中{20-int(bool(remaining))}组PASS，{int(bool(remaining))}组PARTIAL。',
        '','当前PG owner、动作确认/并发幂等、持久Checkpoint及应用重建、SSE实际断流、CLI结算清理和本地告警已验证。',
        '新Provider/Judge/Embedding及真实评测试次均0，费用USD0；条件付费额度未启用。工程替身Run不属于真实模型案例。','',
        '## 已修复','',
        '- 拒绝候选日期误脱敏；数值用量与凭据/号码分别处理。',
        '- 中文整数期限、中文日历日期、英文句号局部断言边界、期限有效性谓词与主体分离。',
        '- 无期限数值的肯定过期主张不能翻转所引不过期规则。',
        '- 越界引用/空正文/缺方面引用的拒绝审计统一；Provider先拒绝的有效候选也保存受控审计，再关联当前Run/Trace/检索及已知用量，不当成功交付。',
        '- 数值存在但主体词或关系未可靠解析时记SCOPE_UNRESOLVED并继续阻断候选，不伪称已经证明与来源矛盾；明确不同对象、错误数值/单位/关系的反例继续拒绝。',
        '- 已有持久日志接入一条数据库依赖告警；真实记录触发/冷却/恢复，重建sink不丢状态。',
        '- 无span日志以Run和节点共同去重，避免不同Run的同类错误被合并。',
        '- 旧PG测试可显式注入实际绑定目标，不初始化或改写marker；PG原始CLI证据导出到持久结果卷。',
        '- 修正旧admin测试的时钟漂移：固定fixture时间落在查询区间，不改生产指标。','',
        '## 测试与实际边界','',
        f"- 最终政策/实际CLI受影响文件：{suites['affected']['tests']}项，全通过；补充监控{suites['monitor']['tests']}项通过，与前者有重叠，不相加。前轮完整相关文件235通过的证据保留。",
        f"- 最终当前PG/真实TCP集成：{suites['postgres']['tests']}项，全执行、无SKIP。",
        '- 13个虚构CLI Trial、7个owner模拟动作、13次持久checkpoint重新打开；原档/账本/清理证明已导出，逐个从原始归档重评分一致。另有1个动作上下文Run及1个SQLite TCP断流Run，均属替身工程验证，不是新真实评测试次。',
        '- 早期容器测试tmpfs原档未导出，不能补造恢复；其日志/JUnit保留。最终13份原档、动作及checkpoint清理证明已实际持久保存。',
        '- PG身份 ca3b0b3d027448a09cc58caa49139862；前后活动标记为空，15类核心表零残留，外部连接尝试0。',
        '- 真实TCP断流使用隔离SQLite；当前PG另验证持久checkpoint、新连接和应用重建/游标恢复。不夸称杀死PG worker后的精确重启。',
        '- 注入到实际SDK序列化已捕获，模型返回由替身控制；证明执行边界，未测新真实LLM的注入回答质量。',
        '- 监控开销来自同一SQLite虚构fixture各20次的持久日志读写，见verification的JUnit属性；不是完整告警/PG/端到端p95。20%/20ms仅诊断，不是新退出门槛。',
        '- 数据保留依赖导出原档及精确receipt，无定时TTL；未扩展企业级监控或全依赖矩阵。','',
        '新源码只在隔离工程镜像验收；既有长期应用服务仍保留M6.3冻结镜像，没有热改原执行段。','',
        '## 49行政策回归及剩余缺口','',
        f'- 31份历史相关期限断言的Run/原档/所选来源和值绑定均核验；{31-remaining}/31消除原窗口发现，{remaining}/31仍被拒或无法解析。',
        '- 另8个完整合法答案、8个错误变体经过实际CLI全链；2个一般/具体对象反例保留，共49行。',
        '- 原31份接受范围没有扩成整答Gold。问题集中在叙述前缀、括号/跨句主体及条件归属，不能用数值匹配覆盖作用域不足。',
        '- 因已知离线缺口仍在，不花付费额度重新采样，不宣称“正常业务无大面积误拒”已完全成立。M6.4尚不标COMPLETE。',
        '- 下一步仅在同一离线语料内完善局部主体/条件解析并保留反例，复用现有测试；不重跑M6.3，不需要新Gold/盲审或重开历史许可。','',
        '## 重算','',
        '```powershell',
        '$env:PYTHONPATH="$PWD\\.vendor\\g2;$PWD\\.vendor;$PWD"',
        'python -X utf8 scripts/finalize_m6_2_r10_closeout.py --version m64',
        '```','',
        '该入口读取原始候选、JUnit、PG健康及本地工程SQLite账本，不调用模型、不创建Run、不连接应用PG或执行业务写入。历史报告和旧SHA不覆盖；旧阶段应在其原冻结镜像下重算。']
    (out/'closeout.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    if revision=='r2':
        assert remaining==0, 'N01 accepted local scope still fails'
        before_cleared=sum(not any(f.get('window') in r['original_rejected_windows'] for f in r['before_findings'])
            for r in rows[:31])
        assert before_cleared==0
        limited=next(r for r in originals if 'EXTRA-074' in r['case_id'])
        limited_findings=_answer_semantic_issues(limited['candidate']['redacted_text'],limited['citation_sources'])
        assert any(f.get('window')=='24个月' for f in limited_findings), '074 unsupported general/specific transfer was accepted'
        scope_remaining=[{'run_id':r['run_id'],'findings':r['complete_candidate_findings']}
            for r in rows[:31] if r['complete_candidate_findings']]
        write('before-after.json',{'before':{'relevant_rows_cleared':before_cleared,'source_snapshot':str((prior/'source-r1').relative_to(root))},
            'after':{'relevant_rows_cleared':31,'full_candidate_findings_remain':scope_remaining},
            'historical_delivery_and_task':'UNCHANGED; candidates were never delivered',
            '074':{'accepted_scope':'SCOPE_CONFLATION_NOT_CLEAN_FALSE_REJECTION; original Task FAIL unchanged',
                'run_id':limited['run_id'],'current_findings':limited_findings}})
        write('exit-matrix.json',{'M6_1':'COMPLETE','M6_2_personal':'COMPLETE','M6_3_personal':'COMPLETE',
            'M6_4_personal':'COMPLETE','scope':'approved bounded personal M6.4 engineering regression',
            'required_groups_passed':20,'required_groups_total':20,'accepted_local_assertions':31,
            'other_candidate_findings':scope_remaining,'M6_5':'NOT_STARTED','strict_formal_Gates':'UNCHANGED',
            'limits':['31 acceptance is local, not whole-answer correctness','no universal natural-language parser claim',
                'SQLite actual TCP disconnect and PG checkpoint/app recreation are separate; no PG worker-kill evidence',
                'no fresh model injection-quality test or deployment of current parser to long-lived app']})
        text=['# M6.4 N01离线根修与后继验收 R2','',
            '**M6.4个人精简范围COMPLETE：20/20逻辑组；N01的31/31份已接受局部期限断言清除原发现。**',
            '旧R1的19 PASS/1 PARTIAL、M6.3原始失败/未交付及接受范围均保留。M6.5未启动，正式Gate不变。','',
            '## 本轮实际修复','',
            '- 按局部断言保留冒号左侧对象、括号外主体及条件续句；登记/服务登记/购买/付款等起算事件独立于主体。',
            '- 商业用途、有效服务凭证、非耗材及图稿确认条件按有界语法归一；未知附加条件保留unparsed_condition并阻断，不静默丢弃。',
            '- 具体商品保修保留resource标识；不同商品、起算事件、数值/单位、过期/申请关系及条件变体继续拒绝。',
            '- 后文明确简称仅从唯一已陈述的同值同单位断言继承；涉及条款的引用性提及与完整规则分开，未陈述条件不冒充已证明。',
            '- 本轮所选来源A/B归属同时约束期限与不过期谓词，错配引用不能靠两条来源同时存在通过。','',
            '## 验证与边界','',
            f"- 当前4个完整受影响文件{suites['affected']['tests']} passed；日志/JUnit包含真实候选正例/针对性反例、真实SDK序列化至持久化/评分/多案清理。网络、DNS、遥测及历史评测写入受拦截。",
            '- 31条逐项保存旧/新解析与发现、所选来源及局部作用域见policy-regression.json；不是仅数值、哈希或任意issue即算反例通过。',
            f'- 完整候选另外仍有{len(scope_remaining)}份包含未接受范围的保守UNKNOWN发现，见before-after.json。未将这些答案批准为整答正确，也未回填历史交付。',
            '- 原19组证据按未变组件字节复用，13份已导出工程原档独立重评分一致；没有重做PG/监控或谎称新解析字节经过旧PG执行。',
            '- R03仍仅为SQLite真实TCP断流，加独立PG checkpoint重开/应用重建；没有PG worker-kill证明。',
            '- R1早期tmpfs原档未导出限制保留。旧冻结镜像/源码快照/授权不覆盖，长期应用未热部署。','',
            '## 用量与重算','',
            '本轮真实Run、Provider、Judge、Embedding及模型费用均0。工程SDK替身/隔离SQLite验证不计真实试次，条件4Run/USD1未启用。',
            '`python scripts/finalize_m6_2_r10_closeout.py --version m64 --revision r2`',
            '入口只读历史来源、JUnit与工程原档；新报告写R2目录。两进程断网重算并核对31条语义行为/反例及13份原档，不以输出哈希相同代替内容验收。']
        (out/'closeout.md').write_text('\n'.join(text)+'\n',encoding='utf8')
        evidence_paths=[p for p in out.iterdir() if p.is_file() and p.name!='SHA256SUMS.md']
        evidence_paths.extend(prior/name for name in ('acceptance-matrix.json','closeout.md','execution-approval.json',
            'exit-matrix.json','policy-regression.json','verification.json'))
        evidence_paths.extend(prior/'source-r1'/name for name in (*snapshot_bindings,'snapshot.json'))
        evidence_paths.extend(root/name for name in changed)
        evidence_paths.extend(root/name for name in ('docs/probes/recompute_m64_r2.py','docs/probes/m64-n01-before.log',
            'docs/probes/m64-n01-before.xml','docs/probes/m64-n01-final-r2.log','docs/probes/m64-n01-final-r2.xml','docs/progress.md'))
        # Intermediate test/recompute failures remain inspectable; logs are not
        # generated inside this manifest, avoiding self-referential hashes.
        evidence_paths.extend(root/'docs/probes'/f'm64-n01-iteration{i}.{ext}' for i in range(1,7) for ext in ('log','xml'))
        sums=['# M6.4 R2 SHA-256','',
            '路径与说明分列。progress绑定本轮追加后的快照；后续追加不改本清单。重算进程日志不纳入正在生成的自身清单。','',
            '| SHA-256 | 仓库相对路径 | 说明 |','|---|---|---|']
        for path in sorted(set(evidence_paths)):
            name=path.relative_to(root).as_posix()
            digest=hashlib.sha256(path.read_bytes()).hexdigest()
            sums.append(f'| {digest} | `{name}` | '+('历史保留' if path.is_relative_to(prior) else '当前证据或源文件')+' |')
        (out/'SHA256SUMS.md').write_text('\n'.join(sums)+'\n',encoding='utf8')
    return {'stage':stage,'checks':len(checks),'partial':int(bool(remaining)),'remaining_assertion_rows':remaining,
        'policy_rows':49,'tests':{k:v['tests'] for k,v in suites.items()},'new_model_requests':0,'report':str(out/'closeout.md')}
