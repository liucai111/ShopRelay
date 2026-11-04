"""Offline closeout of the existing R26 construction/engineering evidence.

No Docker operation, question generation, model request, review signature or
formal permission is performed here. Raw engineering archives drive recompute.
"""
from __future__ import annotations
from pathlib import Path
from collections import Counter
import hashlib,json,sqlite3,shutil
import xml.etree.ElementTree as ET

from eval.m6_1.contracts import EvalCase,sha256_json
from eval.m6_1.grants import formal_case_digest,formal_runtime_digest
from eval.m6_1.formal_assets import validate_asset,raw_hash
from eval.m6_1.formal_scoring import recompute
from eval.m6_1.store import ResultStore
from eval.m6_1.budget import BudgetLedger,BudgetLimits

REL=Path('docs/eval/m6.2-container-candidates-r26-20261031')
DATA=Path('data/eval/new-assets-r26/v3')
EVIDENCE=Path('data/eval/r26-engineering-evidence')

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))
def save(p,value):
    content=json.dumps(value,ensure_ascii=False,indent=2)+'\n'
    if p.exists():
        if p.read_text(encoding='utf8')!=content:raise ValueError('preserve closeout;source changed: '+str(p))
    else:p.write_text(content,encoding='utf8')

def build(root:Path):
    out=root/REL;candidate=root/DATA;evidence=root/EVIDENCE
    frozen=validate_asset(read(candidate/'candidates/frozen100.json'))
    dev=validate_asset(read(candidate/'candidates/dev8.json'))
    assert len(frozen)==100 and len(dev)==8 and len({c.case_id for c in frozen+dev})==108
    construction=read(candidate/'construction-validation.json');repositories=read(candidate/'repository-validation.json')
    assert construction['cohort_counts']=={'route':100,'retrieval':80,'outcome':60}
    assert repositories['case_count']==108 and len(repositories['rows'])==108
    assert all(r['cleanup']['residual_case_rows']==0 and r['cleanup']['lease_released'] for r in repositories['rows'])
    assert not construction['cross_split_near_duplicate_flags'] and construction['exact_duplicate_questions']==0
    for name,cases in [('frozen100',frozen),('dev8',dev)]:
        for phase in ['day1-form','blind-form-unfilled']:
            form=read(candidate/'review'/name/(phase+'.json'))
            assert len(form['items'])==len(cases)
            assert all(r['labels'] is None and r['rationale'] is None for r in form['items'])
    # Exported raw bytes are checked against their recorded receipt before code
    # grading. This is a new disk/recovery boundary, not a second Runner.
    segments=[];recomputed={};run_ids=set();action_rows=[]
    for segment in ['active','segment2','segment3','segment4','segment5']:
        base=evidence/'control-ledgers'/segment
        store=ResultStore(base/'results.sqlite3')
        with store.transaction(immediate=False) as db:
            exp=db.execute('SELECT experiment_id FROM experiments').fetchone()[0]
        budget=BudgetLedger(store,exp,BudgetLimits(**store.budget_limits(exp))).snapshot()
        trials=store.trials(exp);rows=[]
        for trial in trials:
            raw=store.raw_result(trial['trial_id'])
            case=EvalCase.from_dict(read(base/'application'/Path(raw['case_path']).name))
            assert formal_case_digest(case)==raw['case_sha256']
            archive_file=evidence/'app-ledgers'/segment/'application'/Path(raw['receipt']['archive_path']).name
            if not archive_file.exists():archive_file=base/'application'/Path(raw['receipt']['archive_path']).name
            assert raw_hash(archive_file)==raw['archive_sha256']
            archive=raw['application_archive'];rid=archive['result']['run_id']
            assert rid not in run_ids;run_ids.add(rid)
            rows.append({'case_id':case.case_id,'run_id':rid,'lifecycle':trial['lifecycle_status'],
                'task_code':raw['code_assessment']['task_outcome'],'runtime':raw['scoring_runtime_sha256']})
            if 'owner_interaction' in archive:
                from eval.m6_1.formal_actions import assess_interaction
                action=archive['owner_interaction'];assessment=assess_interaction(case,action)
                assert assessment['permission']=='PASS' and assessment['state_check']=='PASS' and assessment['business_effect_check']=='PASS'
                assert action['before_business_counts']==action['after_proposal_business_counts']
                action_rows.append({'case_id':case.case_id,'tool':case.input['owner_interaction']['tool_id'],
                    'decision':case.input['owner_interaction']['decision'],'assessment_from_rows':assessment,
                    'confirmation_before_effect_zero':True,'actual_state':action['snapshot']['controlled_pending_actions'][0]['status']})
        assert budget['active_reservations']==0
        if segment in {'segment3','segment4','segment5'}:
            cases=[EvalCase.from_dict(c) for c in read(evidence/'engineering-assets'/('engineering-'+segment+'.json'))['cases']]
            recalculated=recompute(cases,store,exp)
            save(out/('recompute-'+segment+'.json'),recalculated)
            recomputed[segment]={'binding':recalculated['binding_sha256'],'rows':len(recalculated['rows']),
                'code_equals_archived':True,'task_counts':recalculated['metrics']['task_outcome_counts']}
        original=read(base/'engineering-result.json') if (base/'engineering-result.json').exists() else None
        worker_store=ResultStore(evidence/'app-ledgers'/segment/'results.sqlite3')
        worker=BudgetLedger(worker_store,exp,BudgetLimits(**worker_store.budget_limits(exp))).snapshot()
        segments.append({'segment':segment,'trials':rows,'controller_budget':budget,'worker_budget':worker,
            'stop_reason':original['stop_reason'] if original else 'PRE_RUN_ENGINEERING_PREFLIGHT_RECONCILED',
            'not_run':original['not_run'] if original else 'ALL_PRE_RUN',
            'recompute':'CURRENT_FROZEN_SOURCE' if segment in recomputed else 'PRESERVED_OLD_SOURCE_SNAPSHOT_NOT_RELABELED'})
    assert len(run_ids)==13 and len(action_rows)==6
    repeats=[read(p) for p in (evidence/'control-ledgers/segment3/application').glob('*.repeat-proof.json')]
    assert len(repeats)==4 and all(r['repeat_confirm_replayed_without_effect'] and r['wrong_owner_denied'] for r in repeats)
    cleanup=read(out/'post-cleanup-target-check.json')
    assert all(cleanup[t]==0 for t in ['runs','orders','pending_actions','refunds','returns','support_tickets','checkpoints','checkpoint_blobs','checkpoint_writes'])
    assert cleanup['lease_free']
    audit=read(out/'actual-isolation-audit.json')
    assert audit['actual_application_runtime_sha256']==formal_runtime_digest()
    assert all(s==403 for s in audit['negative_permit_probes']['http_rejections'].values())
    requests=[json.loads(line) for line in (evidence/'app-ledgers/sdk-requests.jsonl').read_text(encoding='utf8').splitlines() if line]
    # The canary SDK itself rejects hidden reference fields in actual messages.
    assert len(requests)==4
    xml=ET.parse(root/'docs/probes/m62-r26-offline-final.xml').getroot()
    suites=[xml] if xml.tag=='testsuite' else list(xml)
    tests={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']}
    assert tests=={'tests':59,'failures':0,'errors':0,'skipped':0}
    summary={'schema_version':'r26-construction-container-closeout-v1','runtime':formal_runtime_digest(),
        'construction':construction,'repository_validation':{'cases':108,'reads':repositories['read_count'],'exact_cleanup':108},
        'assets_location':{'volume':'shoprelay-m62-eval_assets','container_path':'/opt/shoprelay/'+DATA.as_posix(),
            'host_export':DATA.as_posix(),'host_export_exposure':'Trusted project/host administrators may access;not UNEXPOSED_VERIFIED'},
        'engineering':{'segments':segments,'actual_fictional_application_runs':len(run_ids),'candidate_application_runs':0,
            'fake_sdk_requests':len(requests),'real_provider_judge_embedding':0,'action_rows':action_rows,
            'idempotency_owner_repeat_proofs':len(repeats),'extra_repeat_verification_calls':sum(r['verification_calls'] for r in repeats),
            'maintenance_cleanup':read(evidence/'control-ledgers/segment5/maintenance-cleanup.json'),
            'unknown_usage_canary':'Retained UNKNOWN;subsequent case NOT_RUN;not rewritten as zero usage',
            'real_provider_cost':'NOT_APPLICABLE_NO_EXTERNAL_MODEL_REQUESTS;fake estimates only exercise budget logic'},
        'isolation_evidence':'actual-isolation-audit.json','post_cleanup':cleanup,'recompute':recomputed,'tests':tests,
        'source_and_review':{'author_type':'AI_ASSISTED_PROGRAM_BUILDER_IN_CURRENT_DEBUG_CONTEXT','provenance':(DATA/'provenance.json').as_posix(),
            'formal_eligible_assets':0,'real_human_day1':0,'real_human_later_date_blind':0,
            'unresolved_near_duplicate_pairs':len(construction['within_frozen_near_duplicate_flags'])},
        'governance':'eval/registry/m6.2-container-governance-r4.json',
        'stage':{'M6.1':'COMPLETE','M6.2':'IN_PROGRESS','formal_evaluation':'NOT_EXECUTED','quality_gates':'NOT_ASSESSED'}}
    save(out/'closeout-r26.json',summary)
    _documents(out,summary)
    marker='## 2026-10-31 M6.2 R26：108题候选实物、容器隔离及工程验收'
    progress=root/'docs/progress.md'
    if marker not in progress.read_text(encoding='utf8'):
        with progress.open('a',encoding='utf8') as f:f.write('\n\n'+marker+'\n\n- 新100题Frozen候选＋独立场景Dev8实物已构建为v3，路由100/检索80/主要Outcome60重叠；108题格式/关联/真实Repository实读与receipt清理通过（589次无模型读取）。零精确重复、跨split近重复0、Frozen内部2对待人工判定。作者为当前调试上下文AI/程序，暴露与独立性NOT_VERIFIED，正式合格资产0；不自动Gold。数据与真实未填审核表在data/eval/new-assets-r26/v3，持久assets卷保留。\n- 后继治理r4仅替换旧Windows账号/ACL/SACL/OS审计实现门槛为容器/网络/最小挂载/Gold与模型输入分离/私钥分离/目标与账本核验；旧门槛为被替代，不写成通过。旧家族隔离替换、历史暴露、真实首日与后续日期盲审、033延期及精确正式授权保留。未创建三个Windows账号或修改系统权限/信任。\n- shoprelay-m62-eval专用PG、私有网络及独立持久卷已部署保留；无宿主端口/整仓挂载/Docker socket/privileged。真实访问探针拒绝应用读取题库/Gold/private key，错许可/owner输入/目标/配置/实际UID及关闭后重放拒绝；宿主/Docker管理员受信，不声称抵御同权限管理工具主动读。容器代理桥接宿主7892已TCP验证，工程模式阻止所有真实Provider转发。\n- 完整CLI工程13个虚构Run（非候选试次），SDK替身4次，真实Provider/Judge/Embedding0；主段10案完成含质量FAIL继续，6种owner动作确认前零效果/确认后精确效果，4个成功动作重复确认及错owner合计8次额外验证分账。UNKNOWN用量和注入清理失败分别停止后案，后者关闭许可后只做receipt维护清理。早期预检0 Run失败、SDK引用错误1 Run失败原档保留。最终专用PG业务/Run/Pending/Checkpoint零残留、lease空、全部reservation0，数据库marker跨重启保持。\n- 最终受影响3完整测试文件59 passed/0 failed/error/skipped；前57项为重叠迭代不相加。当前冻结运行摘要'+formal_runtime_digest()+'，主/故障段从原始归档独立重算一致；旧段源快照保留。工程预算占位价格/测试许可不等于真实模型计费或正式信任。\n- 统一收口docs/eval/m6.2-container-candidates-r26-20261031/；正式评测未执行、质量门槛未判定、M6.1 COMPLETE/M6.2 IN_PROGRESS。真实审核/独立性处置、真实部署激活与价格/模型凭据、精确内容和执行许可仍未完成；无旧受保护内容访问、生产业务操作或Git写操作。\n')
    final=out/'source-final';final.mkdir(exist_ok=True)
    for src in (root/'data/eval/container-build/r26/code').rglob('*'):
        if src.is_file():
            dst=final/src.relative_to(root/'data/eval/container-build/r26/code');dst.parent.mkdir(parents=True,exist_ok=True)
            if not dst.exists():shutil.copyfile(src,dst)
            assert raw_hash(dst)==raw_hash(src)
    paths=set(p for p in out.rglob('*') if p.is_file() and p.name not in {'SHA256SUMS.json','SHA256SUMS.md','recompute-verification.json'})
    paths.update(p for p in candidate.rglob('*') if p.is_file() and 'validation-work' not in p.parts)
    paths.update(p for p in evidence.rglob('*') if p.is_file() and p.suffix not in {'.shm','.wal'} and not p.name.endswith(('-wal','-shm')))
    paths.update(root/p for p in ['AGENTS.md','docs/progress.md','eval/registry/m6.2-container-governance-r4.json',
        'deploy/m62-eval/Dockerfile','deploy/m62-eval/compose.yaml','scripts/manage_m6_2_container.py','scripts/audit_m6_2_container.py',
        'eval/m6_2/candidate_construction_r26.py','eval/m6_2/container_operations_r26.py','eval/m6_2/container_closeout_r26.py',
        'scripts/finalize_m6_2_r10_closeout.py','tests/test_m6_2_r26_container_boundary.py',
        'docs/probes/m62-r26-offline-final.xml','docs/probes/m62-r26-offline-final.log',
        'docs/probes/m62-r26-offline-iteration1.xml','docs/probes/m62-r26-offline-iteration1.log',
        'docs/probes/m62-r26-engineering-preflight-failure-reconciliation.json'])
    paths={p for p in paths if p.is_file()}
    entries=[{'path':p.relative_to(root).as_posix(),'sha256':raw_hash(p),'description':'R26 authorized new material/source/engineering evidence;no signing key or DB secret'} for p in sorted(paths)]
    save(out/'SHA256SUMS.json',{'schema_version':'path-hash-description-v1','entries':entries})
    manifest='| Path | SHA-256 | Description |\n|---|---|---|\n'+''.join(f"| {e['path']} | {e['sha256']} | {e['description']} |\n" for e in entries)
    m=out/'SHA256SUMS.md'
    if not m.exists():m.write_text(manifest,encoding='utf8')
    elif m.read_text(encoding='utf8')!=manifest:raise ValueError('manifest changed')
    return {'candidate_counts':[100,8],'engineering_runs':13,'external_model_requests':0,'formal_evaluation':'NOT_EXECUTED','sha_entries':len(entries),'binding':sha256_json(summary)}

def _documents(out,s):
    text=f'''# R26交付：候选数据与已部署容器

100题Frozen候选＋Dev8已实际构建，108题均完成数据/Repository/receipt验证。专用Compose `shoprelay-m62-eval`已部署并保留。**真实审核未完成，正式Frozen未执行，M6.2未完成。**

## 数据实物与审核

- 仓库实物：`data/eval/new-assets-r26/v3/candidates/frozen100.json`、`dev8.json`。持久卷`shoprelay-m62-eval_assets`，容器路径`/opt/shoprelay/data/eval/new-assets-r26/v3`。完整fixture/政策语料与四类参考草案在每题，参数来源与构建族见case-metadata.json，逐题实读/清理见repository-validation.json。
- 路由100、检索80、主要Outcome60是同一100内重叠队列。实际合法参考草案DIRECT10/SINGLE40/MULTI12/AGENT31/REFUSE7；R25的20复杂槽=12政策MULTI＋8业务AGENT+MULTI，未为了标签强制违法业务MULTI。12项after-sales覆盖含9个显式owner交互，其余为状态/政策/资格咨询；Dev有2个owner交互。
- Frozen构建族11、Dev构建族5，group跨split分离。精确重复0，跨split近重复0，Frozen内部2对同族近重复待真实审核。不存在100个独立场景族的虚构声明。
- 作者类型AI辅助程序构建，生成规则与当前调试上下文有关；调试者接触规则及工程元数据，未批量打印题库不是独立性证明。provenance.json保留NOT_VERIFIED，候选Registry为NOT_REVIEWED/NOT_SCOREABLE，合格正式资产0。v1/v2历史草案保留在卷中，最终交付v3不把版本数计入题量。
- 首日表：`review/frozen100/day1-form.json`、`review/dev8/day1-form.json`。后续日未填表：各自`blind-form-unfilled.json`。108项labels/rationale及真实签署均未填写；独立审核视图不展示AI参考。已见AI参考的人须如实记录暴露，不伪称未受提示初标。033专项电气能力继续延期。
- 日期/动作有效性依据实际时间，正式签发前须重新检查；不自动改写固定fixture时间。两个近重复对、语义合理性、draft原子事实粒度/必需证据和独立性均是审核事项。

## 治理后继与运行边界

`eval/registry/m6.2-container-governance-r4.json`引用r3原始SHA和本次用户决定，仅替代Windows三账号、ACL/SACL/OS审计的隔离实现要求。旧门槛是SUPERSEDED，不是PASSED。旧受影响家族隔离替换、历史暴露UNKNOWN、独立审核、精确内容/执行许可不变。实际准入读取r4绑定并验证父历史，不要求新增Windows账号。没有改Windows账号、ACL、审计策略或生产信任。

应用UID10002/只读根/零cap/no-new-privileges，只挂app-state、公钥配置和专用DB口令；控制端UID10001持题库/参考和结果，无签发私钥；一次性签发进程UID10003独占私钥且network none；PG独立卷/marker、无宿主端口。宿主与Docker管理员受信，本方案不防同权限管理员或开发工具主动读取。私钥、DB口令仅在独立秘密卷，无仓库JSON或报告内容。当前是工程测试许可与测试公钥，未配置正式信任或Provider凭据。

actual-isolation-audit.json记录实际4服务inspect、挂载拒绝、错签名/owner输入/目标/配置/实际UID拒绝、关闭许可重启后重放拒绝。应用无Docker socket、题库、Gold和构建器。代理为容器proxy:7892→host.docker.internal:7892，实际TCP到达宿主7892，工程模式禁止Provider转发；未发送探针。现有业务项目/卷未操作。

## 工程验收与失败

最终受影响完整3文件59项通过。完整CLI的工程段实际13个虚构Run，SDK替身4次、真实模型0、候选试次0。主段10案中1个任务FAIL仍继续；6种动作走真实FastAPI/认证/CSRF/M4/PG，确认前零效果，确认后精确效果。4个已确认动作的重复确认和错owner共8次额外验证分账，数据库快照未增写。

未知usage段保留UNKNOWN并阻止后案；清理失败段STOP保留，关闭许可后仅receipt维护清理，不重跑。早期预检错误0 Run和SDK错引用1 Run失败均保留；不是隐藏失败后的统一成绩。旧源快照与新段绑定分开。主/两个故障段从原始归档重算，候选/实际交付/质量与工程检查分别保存；工程未签真实人工Task分数。

最终PG marker `{s['post_cleanup']['identity']}`不变，业务/Run/Pending/Checkpoint零残留，lease空，全部预留结清。Run原始证据先归档，receipt精确清理删除测试行不等于抹除历史。容器、卷及账本均保留可继续使用，未删库重建。

## 接续命令与真正剩余输入

```powershell
# 只查看现有部署，不重建或清空卷
docker compose -f deploy/m62-eval/compose.yaml ps
# 无模型重算已有工程/数据证据；不再生成108题
.venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version r26
# 实际审核者填写新版本submission后接收，不用AI代填
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v3/frozen100-workflow.json accept --submission <实际首日提交.json> --output <新回执.json>
# 真实后续日期重新生成隐藏首日票的视图，再实际填写/接收
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v3/frozen100-workflow.json blind-form --output <新盲审表.json>
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v3/frozen100-workflow.json compare --output <分歧.json>
```

Dev8用对应dev8-workflow配置。允许同一真实人员承担项目角色，首日及后续日期必须真实发生；审核服务身份不能充当人类。候选配置CANDIDATE_DRAFT明确拒绝freeze：真实审核、暴露/独立性处置后，由真实批准建立已验证后继版本，不能直接把测试签发器或AI题库改名Gold。

集中剩余输入：①真实审核者完成四类参考、两对近重复及语义裁决、真实后续日期盲审；②所有者/数据负责人明确AI/程序构建与调试暴露的独立性适用决定，必要时真实独立构建替换（不因未打印自动通过）；③真实正式issuer/执行许可、实际模型/价格/凭据的安全部署及激活配置——当前readonly mode刻意仅工程；④合格资产及配置齐备后精确Dev/Frozen范围/顺序/次数/预算/截止/停止规则授权，方可签发。100＋8建设数量、容器方案与历史Dev确认不再重问。

M6.1 COMPLETE。M6.2四项：Dev评分与有限校准证据保留；正式参考与独立校准仍待真实审核；正式部署工程及隔离已验收但正式资产准入/许可/真实Frozen未完成；正式结果与最终质量验收未完成。评测是否完成、质量是否通过分别判断，不要求全案PASS或M2先PASS。无旧受保护内容访问，无Git写操作。
'''
    p=out/'closeout-r26.md'
    if not p.exists():p.write_text(text,encoding='utf8')
    elif p.read_text(encoding='utf8')!=text:raise ValueError('closeout prose differs')
