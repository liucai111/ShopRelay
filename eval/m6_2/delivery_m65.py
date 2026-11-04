"""M6.5 delivery projection over existing evidence; no execution or model calls."""
from datetime import datetime,timezone
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET


def build(root: Path) -> dict:
    from eval.m6_1.grants import formal_runtime_digest
    from eval.m6_1.personal_dev import implementation_digest
    root=root.resolve();out=root/'docs/eval/m6.5-personal-delivery-20261101'
    out.mkdir(exist_ok=True)
    proof=root/'docs/probes/m65'
    def read(path):return json.loads((root/path).read_text(encoding='utf8'))
    def local(name):return json.loads((proof/name).read_text(encoding='utf8'))
    def write(name,value):(out/name).write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    def suite(path):
        cases=list(ET.parse(root/path).iter('testcase'))
        assert cases and all(c.find('failure') is None and c.find('error') is None and c.find('skipped') is None for c in cases)
        return {'path':path,'passed':len(cases),'failed':0,'errors':0,'skipped':0}
    tests={name:suite(path) for name,path in {
        'host_complete_API':'docs/probes/m65/api-final.xml',
        'clean_semantic_unit':'docs/probes/m65/reproduction-partial-r3/unit.xml',
        'clean_BM25_API':'docs/probes/m65/reproduction-partial-r3/api.xml',
        'clean_CLI_E2E':'docs/probes/m65/reproduction-partial-r3/cli.xml'}.items()}
    assert [tests[k]['passed'] for k in tests]==[38,66,37,8]
    steps=local('reproduction-partial-r3/steps.json')
    assert [s['step'] for s in steps[:4]]==['static','unit','api','cli']
    assert all(s['exit_code']==0 for s in steps[:4])
    assert (proof/'reproduction-report-exit.txt').read_text().strip()=='0'
    before=read('docs/eval/m6.4-lean-acceptance-r2-20261101/policy-regression.json')
    after=local('reproduced-report-final/policy-regression.json')
    assert before==after and after['relevant_replay_cleared']==31
    assert local('reproduced-report-final/acceptance-matrix.json')==read('docs/eval/m6.4-lean-acceptance-r2-20261101/acceptance-matrix.json')
    assert local('reproduced-report-final/exit-matrix.json')==read('docs/eval/m6.4-lean-acceptance-r2-20261101/exit-matrix.json')
    assert len(local('reproduced-report-final/verification.json')['raw_code_recomputation'])==13
    assert local('reproduction-r3-bindings.json')==local('reproduction-r5-bindings.json')
    actual=local('running-bindings.json');boundary=local('boundary-health-final.json')
    staged=read('data/eval/container-build/m65/source-manifest.json')
    assert len(staged['files'])>=30 and staged['secrets_or_gold_or_whole_repository'] is False
    for path,digest in staged['files'].items():
        assert hashlib.sha256((root/path).read_bytes()).hexdigest()==digest,'deployed overlay source changed: '+path
    runtime=formal_runtime_digest();implementation=implementation_digest(root)
    assert actual['runtime']==boundary['runtime']==runtime
    assert actual['implementation']==implementation
    assert boundary['uid']==10002 and boundary['no_new_privileges'] and boundary['cap_effective_zero']
    assert boundary['active_runs']==boundary['pending_actions']==0 and boundary['lease_free']
    assert boundary['real_provider_activated'] is False
    assert boundary['target_identity']=='ca3b0b3d027448a09cc58caa49139862'
    assert (proof/'final-health.txt').read_text().strip()=='healthy'
    assert (proof/'rollback-image.txt').read_text().strip()!=(proof/'restored-image.txt').read_text().strip()
    http=local('http-execution.json');browser=local('browser-e2e.json');recovery=local('restart-recovery.json')
    assert all(http['checks'].values()) and len(http['rows'])==3
    assert all(browser[k] for k in ('login','session','SSE_answer','citation_panel','reload_restores_record','execution_panel'))
    assert browser['page_errors']==[] and browser['paid_provider_calls']==0
    assert len(recovery['recovered_after_service_restart'])==3
    cleanup=local('engineering-archive-and-cleanup.json');receipt=cleanup['receipt']
    assert len(cleanup['engineering_archive'])==9
    assert not any(receipt['residual'].values()) and not any(receipt['checkpoints'].values())
    assert not any(receipt['public_evaluation_tables_unchanged_empty'].values())
    assert len({r['run_id'] for r in cleanup['engineering_archive']})==9
    front=local('reproduction-comparison.json')
    assert front['frontend_clean_build_identical'] and front['critical_code_and_tests_identical_r3_r5']
    assert 'tests 5' in (proof/'frontend-clean-test.log').read_text(encoding='utf8')
    assert 'built in' in (proof/'frontend-clean-build.log').read_text(encoding='utf8')
    acceptance=out/'execution-approval.json'
    if not acceptance.exists():
        write('execution-approval.json',{'event_id':'M65-PERSONAL-DELIVERY-EXECUTION-APPROVAL-20261101-R1',
            'actor':'CHAT_USER_SELF_CONFIRMED','kind':'EXECUTION_AUTHORIZATION_NOT_TASK_OR_GOLD_GRADING',
            'source_thread':'01a1803e-e8da-7ac1-921a-741806cfda36','source_turn':'01a196bf-86d0-7d12-9800-fcf8ea31e8d1',
            'source_message':'01a196bf-8756-7e22-927b-4eb5f83e0bc1','confirmed_text':'按这个计划执行',
            'source_turn_started_at':datetime.fromtimestamp(1793530496,timezone.utc).isoformat(),
            'recorded_at':datetime.now(timezone.utc).isoformat(),
            'scope':'M6.5 personal lean deployment, deterministic verification, reproducible core/CI and delivery materials',
            'paid_Provider_Judge_Embedding_budget':0,'protected_assets_and_production':'NOT_AUTHORIZED'})
    m63=read('docs/eval/m6.3-personal-execution-20261101/final-stage-closeout-r1.json')
    metrics=read('docs/eval/m6.3-personal-execution-20261101/metrics-and-budget.json')
    assert m63['actual_trials']==250 and metrics['budget']['N']==250
    assert m63['accepted_task_counts_coverage_only']=={'PASS':116,'PARTIAL':54,'FAIL':80}
    gates=[
        {'gate':'ENGINEERING','scope':'personal local deployment and default BM25 core','status':'PASS',
            'evidence':['running-bindings.json','boundary-health-final.json','http-execution.json','reproduction-partial-r3/steps.json'],
            'limits':'No production rollout; optional clean Qdrant/Hybrid stack not included; host full API test covers its existing local-vector fixture.'},
        {'gate':'QUALITY','scope':'strict original §10.1 quality release','status':'BLOCKED',
            'evidence':['docs/eval/m6.3-personal-execution-20261101/final-stage-closeout-r1.json'],
            'reason':'No qualifying independent Frozen Macro F1/required-evidence Recall/Human atomic-fact accuracy proof. Retained task failures and five other UNKNOWN candidates. No new global Task Success threshold invented.'},
        {'gate':'SAFETY','scope':'approved personal M6.4 bounded checks and deployed owner/guard paths','status':'PASS',
            'evidence':['docs/eval/m6.4-lean-acceptance-r2-20261101/acceptance-matrix.json','http-execution.json'],
            'limits':'Not a new full MCP/Embedding/Qdrant failure matrix or 033 electrical review.'},
        {'gate':'OBSERVABILITY','scope':'persisted Run/Trace, bounded local alert/recovery and sanitization','status':'PASS',
            'evidence':['http-execution.json','restart-recovery.json','docs/eval/m6.4-lean-acceptance-r2-20261101/verification.json'],
            'limits':'SQLite TCP disconnection and PG checkpoint/app recreation remain separate. No PG-worker-kill or production SLO claim.'},
        {'gate':'DELIVERY','scope':'local personal cold container/core, clean frontend, browser demo and replay','status':'PASS',
            'evidence':['browser-e2e.json','reproduction-comparison.json','.github/workflows/core.yml'],
            'limits':'Remote GitHub CI not run; no commit/push. Cached Python/container dependencies reused; clean frontend dependencies installed via 7892/TLS.'},
        {'gate':'STRICT_FORMAL_ASSETS_AND_EXECUTION','scope':'independent Gold/Registry/M2/Frozen and production','status':'BLOCKED',
            'reason':'Personal disclosed-synthetic acceptance does not grant independent unseen identity, specialist 033 review, formal exact execution permit or production trust.'}]
    manifest={'schema':'m65-personal-delivery-v1','M6_5_personal':'COMPLETE','M6_full_strict_acceptance':'NOT_PASSED',
        'implementation_sha256':implementation,'execution_runtime_sha256':runtime,'self_rag_sha256':actual['self_rag'],
        'application_image':(proof/'restored-image.txt').read_text().strip(),
        'rollback_image':(proof/'rollback-image.txt').read_text().strip(),'local_url':'http://127.0.0.1:18065/',
        'database':{'name':'shoprelay_m11_test','parent_identity':boundary['target_identity'],
            'demo_schema':'m65_delivery','checkpoint_schema':'m65_delivery_checkpoint','original_public_rows_unchanged':True},
        'build_source_manifest':'data/eval/container-build/m65/source-manifest.json',
        'deployment_overlay_sha256':hashlib.sha256((root/'data/eval/container-build/m65/source-manifest.json').read_bytes()).hexdigest(),
        'tests':tests,'clean_core_exclusion':{'count':1,'name':'test_single_chat_can_use_real_rrf_retrieval_service',
            'reason':'optional Qdrant dependency outside clean BM25 image; no skipped core assertions; host full API38 passed'},
        'clean_reproduction':{'test_image':'shoprelay-m65-reproduction:r3','report_image':'shoprelay-m65-reproduction:r5',
            'critical_application_and_test_bytes_equal':True,'report_semantic_and_numeric_results_equal':True,
            'raw_byte_hash_difference':'Windows CRLF/Linux LF and source/control metadata/relative path separators; not changed scores'},
        'CI_remote':'NOT_RUN_NO_GIT_WRITES','front_clean_tests':5,'frontend_artifacts_equal':True,
        'engineering_http_runs':9,'paid_Provider':0,'Judge':0,'Embedding':0,'new_real_model_trials':0,
        'new_model_tokens':0,'new_model_estimate_usd':0,'retained_baseline':receipt['retained_fictional_baseline'],
        'cleanup':receipt,'historical_M63_budget_unchanged':metrics['budget'],
        'recompute':'python scripts/finalize_m6_2_r10_closeout.py --version m65'}
    sources=['docs/probes/m65/'+name for name in ('running-bindings.json','boundary-health-final.json',
        'http-execution.json','browser-e2e.json','restart-recovery.json','engineering-archive-and-cleanup.json',
        'reproduction-report-r5.log','reproduction-r3-bindings.json','reproduction-r5-bindings.json',
        'frontend-clean-build.log','frontend-clean-test.log','reproduction-comparison.json')]
    sources += [row['path'] for row in tests.values()]
    sources += ['.github/workflows/core.yml','scripts/m65_local_delivery.py','scripts/m65_web_gateway.py',
        'scripts/verify_m65_delivery.py','scripts/cleanup_m65_delivery.py','scripts/verify_m65_reproduction.py',
        'eval/m6_2/delivery_m65.py','scripts/finalize_m6_2_r10_closeout.py',
        'docs/eval/m6.3-personal-execution-20261101/final-stage-closeout-r1.json',
        'docs/eval/m6.3-personal-execution-20261101/metrics-and-budget.json',
        'docs/eval/m6.4-lean-acceptance-r2-20261101/policy-regression.json']
    manifest['source_bindings']=[{'path':path,'sha256':hashlib.sha256((root/path).read_bytes()).hexdigest()} for path in sources]
    write('delivery-manifest.json',manifest)
    write('acceptance.json',{'stage':{'M6_1':'COMPLETE','M6_2_personal':'COMPLETE','M6_3_personal':'COMPLETE',
        'M6_4_personal':'COMPLETE','M6_5_personal':'COMPLETE'},'gates':gates,
        'strict_full_M6':'NOT_PASSED; §10.3 requires all applicable hard gates, not personal delivery alone',
        'historical_task_scores_unchanged':True,'new_Provider_Judge_Embedding':0})
    lines=['# M6.5个人精简范围：最终验收与交付','',
        '**M6.5个人交付任务COMPLETE。当前修复版已部署、核心链路与报告可复现；严格M6完整质量/正式放行尚未通过。**','',
        '## 实际运行与版本','',
        '- 网页 http://127.0.0.1:18065/；真实FastAPI、LangGraph、owner Repository、政策检索、Run/SSE/Trace与PG持久Checkpoint。回答为公开的确定性替身，不是新模型能力试验。',
        f'- 当前实际镜像 `{manifest["application_image"]}`，回退镜像 `{manifest["rollback_image"]}`；已实际回退执行边界再恢复当前版。',
        f'- 实现 `{implementation}`；运行依赖 `{runtime}`；实际运行字节与工作树一致。',
        '- 同项目内部应用网络＋固定上游Web网关，仅loopback发布；应用不挂Gold/私钥/Provider秘密/宿主仓库/Docker socket，无生产DSN。宿主及Docker管理员仍在可信管理范围。',
        '- 专用PG原marker不改；演示使用两个独立schema。Bootstrap跑真实迁移到20261027_17，正常启动不迁移/seed/签发。',
        '- 新增付费Provider/Judge/Embedding、真实模型评测试次、tokens和模型费用均0；依赖下载为工程联网，固定7892/TLS，不是模型请求。','',
        '## 验证结果','',
        '- 实际HTTP：登录、正常政策答案与同Run引用、本人订单读取、Run/Trace/管理员投影、他人访问404；错误天/月候选真实被护栏拒绝且未交付。',
        '- 实际Edge浏览器：登录、新会话、SSE答案、引用和执行面板、刷新恢复；page errors=0。服务实际restart后3个原Run的状态/答案/事件相同。截图见docs/probes/m65/browser-restored.png。',
        '- 新schema中9个虚构工程Run（含探针迭代与预期拒绝）全部导出；Run/Session/checkpoint/auth残留0，保留2用户、1商品、1订单及schema/卷供继续演示。原public评测表仍空。',
        '- 干净容器：静态检查通过；66单测、37 BM25 API、8完整CLI通过；当前报告31条语义、49行输出与13份工程原档重评分复现。报告打包修复后只重验报告步骤，R3/R5的实际应用与测试字节一致。',
        '- 宿主完整API38 passed；干净镜像明确排除1个可选Qdrant/RRF测试，不以skip冒充通过；不声称干净复现全部Hybrid模型依赖。',
        f"- 独立前端目录无node_modules/dist/.env起步，按lock经7892安装81个依赖；typecheck、5 tests及build通过，{front['frontend_files']}份构建文件与部署产物逐字节一致。",
        '- 轻量CI已落地.github/workflows/core.yml，静态/单测/API/CLI/前端层均本地执行；远端CI未运行，不提交/推送。',
        '- 原失败保留：沙箱esbuild EPERM按正常权限解决；内部网络端口发布由独立无秘密网关解决；SPA刷新404已修；只读pycache转/tmp；可选Qdrant依赖明确划范围；报告缺少绑定测试文件已补齐。没有改生产护栏、冻结数据或旧失败。','',
        '## 统一验收矩阵','', '| Gate | 范围 | 状态 | 限制/后续 |','|---|---|---|---|']
    for g in gates:lines.append(f"| {g['gate']} | {g['scope']} | {g['status']} | {g.get('reason',g.get('limits',''))} |")
    lines+=['','## 可引用的真实成果与限制','',
        '- M6.3历史250次唯一Run：6 Smoke、100 B基准、24题A/B各三轮。B基准51 PASS/10 PARTIAL/39 FAIL；主要Outcome33/60；未交付FAIL保留。',
        '- 72对B胜21/A胜6/同分45；严格PASS A27/72、B38/72，差15.28个百分点，24题成组bootstrap描述区间6.94–23.61个百分点。A/B各63/72有真实Provider，A47/72进入flat controller、B22/72实际多节点；不是纯多Agent结构普遍因果收益。',
        '- 三轮全PASS A4/24、B9/24；至少一次A14/24、B17/24。质量负结果不因后修或重复采样覆盖。',
        '- 原账本Provider309、tokens421275、估算USD0.50553，供应商账单UNKNOWN。B请求/延迟更低，但tokens及估算费用略高；不把估算当账单或零成本能力。',
        '- M6.4的31条接受仅限局部期限断言；另5份完整候选UNKNOWN与074范围混同仍保留。原始Task/交付不升级为成功。',
        '- 250次原始数据、分母、manifest、接受事件、关闭许可和精确receipt仍在docs/eval/m6.3-personal-execution-20261101；不复制原始Run或制造新成绩。',
        '- Router Macro F1≥0.85、必需证据Recall@5≥0.85、真人关键事实≥0.90的严格质量门槛未获本批独立Frozen证明；本轮不发明全局Task硬阈值，不要求把所有题修到PASS才能交付。','',
        '## 简历可用表述','',
        '“实现FastAPI/LangGraph电商客服与owner只读工具、引用审计、确认幂等及持久恢复；建立逐Trial预算、Code评分和可离线重算的证据链。在披露合成数据上完成250次真实试次，固定24题三轮配对得到当前混合策略系统B胜21、A胜6、同分45；保留质量失败及成本权衡。通过容器隔离部署、实际浏览器演示和分层本地CI验收。”',
        '不要写独立Gold全面通过、多Agent普遍优于Single、生产SLO或测试条数等于任务成功数。','',
        '## 重现与后续','',
        '- 运行、回退、干净容器及精确清理命令：docs/runtime-and-deployment.md的M65章节。五分钟路径：docs/demo/five-minute-demo.md。系统图沿用docs/architecture/system-overview.mmd，新增evaluation-evidence-flow.mmd展示评分/治理边界。',
        '- 汇总入口：`python scripts/finalize_m6_2_r10_closeout.py --version m65`。只读已有证明并写本M6.5目录，不创建Run或联网；源文件/清单的历史快照不覆盖。',
        '- 可选后续：经用户另行安排部署真实模型凭据与精确预算、受控诊断或独立正式资产审核；本轮不需要新付费请求，也不自动启动新评测。',
        '- 严格正式路径仍需资格合格的资产/人工维度标签、Gold/Registry与精确执行许可；033继续延期。远端CI需用户实际提交后运行。完整Hybrid冷环境需其独立依赖/缓存，不在本次BM25核心声明中。',
        '- M6.1–M6.5个人范围完成不等于M6严格全部强制Gate通过；工程交付完成与质量门槛通过分开。']
    (out/'closeout.md').write_text('\n'.join(lines)+'\n',encoding='utf8')
    return {'M6_5_personal':'COMPLETE','strict_M6':'NOT_PASSED','current_runtime':runtime,
        'clean_tests':{k:v['passed'] for k,v in tests.items()},'frontend_tests':5,'paid_model_calls':0,
        'engineering_runs_exported_and_cleaned':9,'report':str(out/'closeout.md')}
