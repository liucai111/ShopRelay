"""Bound successor revision of authorized new R26 data; not an Agent Runner."""
from __future__ import annotations
from pathlib import Path
from datetime import datetime,timedelta,timezone
from copy import deepcopy
from collections import Counter
import hashlib,json,re

from eval.m6_1.contracts import EvalCase,sha256_json
from eval.m6_1.formal_assets import write_new,validate_asset,AssetWorkflow,raw_hash
from eval.m6_1.grants import formal_case_payload,formal_case_digest
from eval.m6_1.fixture_read_models import product_id
from eval.m6_1.formal_reference_rules import V2,ROUTE_KEYS

OLD=Path('data/eval/new-assets-r26/v3')
NEW=Path('data/eval/new-assets-r26/v5')
REVIEW=Path('docs/eval/m6.2-r26-ai-candidate-review-20261031')
OUT=Path('docs/eval/m6.2-r27-final-revised-candidates-and-execution-20261031')
VERSION='m62-new-candidates-r27-v5'
R28_BASE=Path('data/eval/new-assets-r26/v7')
R28_OUT=Path('docs/eval/m6.2-r28-reference-and-adapter-repair-20261031')
R28_REVIEW=Path('docs/eval/m6.2-r27-review-20261031')

def read(p):return json.loads(p.read_text(encoding='utf-8-sig'))

def finalize_r29(root:Path):
    """Receive the actual user review and prepare a later, separate AI context.

    No assessment, model request, asset qualification or grant is performed here.
    """
    from dataclasses import asdict
    import shutil
    from app.m0.config import load_settings
    from eval.m6_1.formal_assets import AI_REVIEW_METHOD,authenticated_subject
    from eval.m6_1.grants import formal_runtime_digest
    out=root/'docs/eval/m6.2-r29-human-method-and-blind-preparation-20261031'
    state=root/'data/eval/new-assets-r26/r29-review'
    event_path=Path('docs/eval/m6.2-r28-review-20261031/user-method-confirmation.json')
    event=read(root/event_path)
    if event['event_id']!='M62-R28-HUMAN-REVIEW-METHOD-AND-PREPARATION-CONFIRMED':raise ValueError('wrong user method event')
    for b in event['bound_files']:
        if raw_hash(root/b['path'])!=b['sha256']:raise ValueError('user-confirmed source changed:'+b['path'])
    out.mkdir(parents=True,exist_ok=True);state.mkdir(parents=True,exist_ok=True)
    clock_path=out/'recording-time.json'
    if not clock_path.exists():write_new(clock_path,{'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'user_statement_time_evidence':event['recorded_at_utc']})
    now=read(clock_path)['recorded_at_utc']
    amendment_path=Path('eval/registry/m6.2-review-method-amendment-r5.json')
    amendment={'schema_version':'shoprelay-review-method-amendment-v1','version':'r5-r29',
        'method_id':AI_REVIEW_METHOD,'status':'OWNER_APPROVED_METHOD_AMENDMENT','recorded_at_utc':now,
        'confirmation':{'path':event_path.as_posix(),'sha256':raw_hash(root/event_path),'event_id':event['event_id']},
        'applies_only_to':[b for b in event['bound_files'] if '/v7/candidates/' in b['path']],
        'replaces_for_this_batch':'Evaluation §3.4 item3 independent initial human annotation and later human blind review implementation;not an additional simultaneous old-method gate',
        'method_steps':['AI-assisted references','actual user review and confirmation','later-date Codex assessment in fresh context without old labels/reviews/conversations','actual user adjudication of disagreements'],
        'human_actor':'CHAT_USER_SELF_CONFIRMED','blind_actor':'CODEX_AI_NOT_HUMAN',
        'retained':['Source/owner/capability/action safeguards','Four separate reference dimensions and real semantic support','Data qualification relative to evaluated configuration','Integrity,Registry and exact signed permission','Old source/method/review records;033 deferral'],
        'limitations':['AI-assisted user review is not independent human first annotation','AI blind assessment is not human blind review or an independent model-validation sample','Context receipt is accountable evidence,not proof against pretraining exposure','User confirmation does not create unseen eligibility or approve USD54 execution'],
        'historical_methods':['Evaluation §3.4 original text retained','eval/registry/m6.2-container-governance-r4.json']}
    put(root/amendment_path,amendment)
    human_index=[];workflow_paths=[];blind_files=[]
    for name in ('frozen100','dev8'):
        cfg=deepcopy(read(root/R28_BASE/(name+'-workflow.json')))
        cfg.update(review_method=AI_REVIEW_METHOD,method_amendment=amendment_path.as_posix(),method_amendment_sha256=raw_hash(root/amendment_path),
            asset_id='m62-r29-'+name+'-v7-human-ai-method')
        cfg['content_intake_permit']['authorization_ref']=event['event_id']
        for field in ('intake_record','day1_record','blind_record','frozen_asset','lock_record','registry_output'):
            cfg[field]=(state.relative_to(root)/name/(field+'.json')).as_posix()
        path=state/(name+'-workflow.json');put(path,cfg);workflow_paths.append(path.relative_to(root).as_posix())
        w=AssetWorkflow(root,cfg)
        if not (root/cfg['intake_record']).exists():w.intake()
        else:w._bound()
        if not (root/cfg['day1_record']).exists():w.confirm_ai_review()
        receipt=read(root/cfg['day1_record']);_,cases=w._bound()
        if receipt['confirmation_event_id']!=event['event_id'] or [r['labels'] for r in receipt['submission']['items']] != [formal_case_payload(c)['expected_outcome'] for c in cases]:
            raise ValueError('human-confirmed per-item labels drift')
        for row in receipt['submission']['items']:
            human_index.append({'case_id':row['case_id'],'case_sha256':row['case_sha256'],'labels_sha256':sha256_json(row['labels']),
                'confirmation_event':event['event_id'],'receipt':cfg['day1_record'],'status':'HUMAN_USER_REVIEWED_AND_CONFIRMED_AI_ASSISTED_REFERENCE',
                'per_item_user_edits':None,'reason_source':'Bound original conditions,requirements and AI reference rationale; no invented item-specific human wording'})
        package_path=state/'blind-input-only'/name/'assessment-package.json';put(package_path,w.blind_package());blind_files.append(package_path.relative_to(root).as_posix())
        if (root/cfg['blind_record']).exists():raise ValueError('R29 preparation must not create or consume a blind assessment')
    put(out/'human-confirmed-references.json',{'event_id':event['event_id'],'method':AI_REVIEW_METHOD,'item_count':len(human_index),'items':human_index,
        'independent_human_initial_annotation':False,'actual_user_review_confirmed':True,'blind_votes_created':0})
    from eval.m6_1.registry import DatasetAsset,DatasetRegistry
    effective=[]
    for name in ('frozen100','dev8'):
        source=R28_BASE/'candidates'/f'{name}.json';payload=read(root/source)
        effective.append(DatasetAsset('m62-r29-'+name+'-human-confirmed-reference','candidate_with_confirmed_ai_assisted_reference',
            payload['version'],None,payload['split'],len(payload['cases']),source.as_posix(),raw_hash(root/source),
            'BATCH_USER_CONFIRMED','CHAT_USER_SELF_CONFIRMED',event['recorded_at_utc'],AI_REVIEW_METHOD,
            'NOT_VERIFIED','NOT_VERIFIED','NOT_SCOREABLE',None,'NO_EXECUTION_GRANT',
            ('Actual user review;later AI blind/adjudication pending','Not independent unseen Gold;reference confirmation does not authorize execution'),
            historical_source_ref=event_path.as_posix()))
    metadata_path=out/'effective-registry-reference-status.json'
    if not metadata_path.exists():DatasetRegistry(effective).save_new(metadata_path)
    elif [asdict(a) for a in DatasetRegistry.load_metadata(metadata_path).assets()] != [asdict(a) for a in effective]:
        raise ValueError('effective human review registry drift')
    blind_manifest={'schema_version':'r29-blind-input-manifest-v1','status':'PREPARED_NOT_STARTED','planned_time':'User said tomorrow morning; start only on a real subsequent Asia/Shanghai date,record actual start/completion',
        'files':[{'path':p,'sha256':raw_hash(root/p)} for p in blind_files],
        'allowed_context':'Only these two packages and generic assessment instructions;no repository,original/review chats,source candidate JSON,adopted references,AI review or labels',
        'fresh_context_required':True,'new_chat_or_task_created_now':False,'scheduler_created':False,
        'submission':'submission=form with four independent labels/rationale;context_receipt documents actual fresh context,package hash and real timing',
        'cannot_prove':'Hiding labels alone does not erase past context/model pretraining; if this same chat is used,do not call the result blind'}
    put(out/'blind-input-manifest.json',blind_manifest)
    shared=read(root/R28_BASE/'semantic-groups-and-exposure.json')
    snapshots=root/R28_OUT/'source-final/app'
    app_paths=list(snapshots.rglob('*.py'))
    changed=[p.relative_to(snapshots).as_posix() for p in app_paths if p.read_bytes()!=(root/'app'/p.relative_to(snapshots)).read_bytes()]
    if changed:raise ValueError('application changed;must re-evaluate the qualification evidence')
    payloads=[read(root/R28_BASE/'candidates'/f'{name}.json') for name in ('frozen100','dev8')]
    def clauses(payload):return {hashlib.sha256(p['text'].encode()).hexdigest() for c in payload['cases'] for p in c['input']['fixture']['policies']}
    overlap=len(clauses(payloads[0]) & clauses(payloads[1]))
    eligibility={'status':'DISCLOSED_SYNTHETIC_BENCHMARK_NOT_QUALIFIED_INDEPENDENT_UNSEEN_FROZEN',
        'current_evaluated_application_files_compared':len(app_paths),'application_files_changed_since_r28':changed,
        'candidate_questions_sent_to_target_real_model':0,'target_model_training_data_or_provider_access':'UNKNOWN_NOT_INSPECTED',
        'human_review_is_real':True,'method_change_does_not_change_independence':True,
        'shared_cross_split_exact_policy_texts':overlap,'recorded_cross_split_families':shared['cross_split_families'],
        'construction':'Current debugging-context AI/program construction;no old protected questions read or relabelled',
        'known_exposure':'Candidate and reference authoring,AI review and reference-driven evaluator/adapter fixes have used current material;human user reviewed it',
        'proven_scope':'No observed real target-model candidate evaluation or candidate-driven Router/Agent source changes;engineering adapters/graders deliberately repaired with source facts',
        'not_proven':'Statistical/semantic independence relative to final evaluation pipeline;pretraining/provider exposure absence;Dev/Frozen policy-family separation',
        'conclusion':'Not a blanket model-contamination finding;not qualified as independent unseen Frozen on current evidence. Human signature or blind reassessment cannot establish unseen provenance.',
        'reusable':['108 actual fixtures/corpora','human-confirmed references and method','container target and isolated action/checkpoint evidence','signature/budget/receipt/grading pipeline'],
        'route_not_switched':'Keep approved100/67/60+Dev8;no new assets generated or new evaluation route started',
        'evidence_refs':[str(R28_BASE/'semantic-groups-and-exposure.json'),str(R28_OUT/'independence-and-reference-lineage.json'),str(R28_OUT/'reference-repair-audit.json') if (root/R28_OUT/'reference-repair-audit.json').exists() else str(R28_BASE/'reference-repair-audit.json')]}
    put(out/'data-qualification.json',eligibility)
    settings=load_settings(require_api_key=False)
    prior=root/R28_OUT/'precise-execution-request.json';plan=deepcopy(read(prior))
    if (settings.model,settings.base_url)!=(plan['model'],plan['endpoint']):raise ValueError('prepared model/endpoint changed')
    nodes={k:asdict(v) for k,v in settings.nodes.items()}
    if nodes!=plan['node_configuration']:raise ValueError('node configuration changed')
    plan.pop('request_binding_sha256',None)
    plan.update(schema_version='r29-execution-preparation-v1',implementation_sha256=formal_runtime_digest(),
        review_method=AI_REVIEW_METHOD,human_confirmation_event=event['event_id'],
        human_confirmed_reference_index_sha256=raw_hash(out/'human-confirmed-references.json'),
        workflow_configs=[{'path':p,'sha256':raw_hash(root/p)} for p in workflow_paths],
        blind_review_status='PREPARED_NOT_STARTED',formal_activation_now=False,
        missing_before_issue=['Actual subsequent-date clean-context Codex assessment and actual user disagreement adjudication;not legacy independent initial annotation',
            'Evidence-based data qualification;current disclosed synthetic identity is not independent unseen qualification',
            'Actual formal trust/key fingerprint and approved secret supply,not engineering identity',
            'Final qualified/adjudicated Gold and Registry hashes,live target/runtime/price validation,and precise per-phase execution permission'],
        inputs_already_confirmed=['100/67/60+Dev8 scope','Actual user review of exact v7 references','Batch-specific amended review method'])
    plan['request_binding_sha256']=sha256_json(plan);put(out/'execution-preparation.json',plan)
    put(out/'exit-matrix.json',{'M6.1':'COMPLETE','M6.2':'IN_PROGRESS','method':AI_REVIEW_METHOD,'tasks':[
        {'task':1,'done':'Historical Dev evidence retained;108 exact v7 human confirmations registered;candidate source facts and scorer repaired',
         'remaining':'Later clean-context AI assessment/user adjudication under amended method;qualified locked-config real route/retrieval/evidence/Outcome measurements'},
        {'task':2,'done':'Existing limited Dev Judge results retained without rerun;Human reference confirmation real but not blind',
         'remaining':'Dimension-matched reviewed references and any necessary finite Judge calibration require explicit call permission;no merged unlike targets'},
        {'task':3,'done':'Container/PG/signing/budget/receipt engineering retained;amended method actually wired without weakening data qualification',
         'remaining':'Actual later AI blind/adjudication,qualification/Gold/Registry,formal trust/secrets,and exact Dev/Frozen execution authorization;033 remains deferred'},
        {'task':4,'done':'Versioned source/run/review/usage evidence and offline recomputation retained',
         'remaining':'Actual formal measured scores,n/N/intervals,human disagreements,usage/failures/final acceptance report'}],
        'old_initial_annotation_requirement_replaced_for_this_batch':True,'all_cases_pass_required':False})
    # Preserve the exact earlier running source;no historical source/report overwritten.
    for name in ['eval/m6_1/formal_assets.py','eval/m6_1/registry.py','eval/m6_1/contracts.py','scripts/run_m6_1_b_synthetic.py']:
        old=root/R28_OUT/'source-final'/name;dest=out/'source-before'/name
        if old.exists() and not dest.exists():dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(old,dest)
    _handoff_r29(root,out,state,workflow_paths,plan,eligibility,now)
    summary={'version':'r29','human_confirmed_items':len(human_index),'method':AI_REVIEW_METHOD,'scope':'100/67/60+Dev8',
        'blind_assessment_started':False,'runtime':formal_runtime_digest(),'formal_activation':False,'new_real_model_calls':0}
    xml_path=root/'docs/probes/m62-r29-legacy-binding-final.xml'
    deployment_path=out/'deployment-verification-final.json'
    if xml_path.exists() and deployment_path.exists():
        import xml.etree.ElementTree as ET
        x=ET.parse(xml_path).getroot();suites=[x] if x.tag=='testsuite' else list(x)
        tests={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ('tests','failures','errors','skipped')}
        if tests!={'tests':61,'failures':0,'errors':0,'skipped':0}:raise ValueError('final affected acceptance not passed')
        deployment=read(deployment_path)
        if deployment['health']['runtime']!=summary['runtime'] or deployment['health']['real_provider_activated'] is not False:
            raise ValueError('actual final engineering deployment binding differs')
        # Reuse the exact immutable R28 source base;archive only changed files.
        source_base=root/R28_OUT/'source-final'
        overlay=[]
        for source in sorted(p for p in source_base.rglob('*') if p.is_file()):
            relative=source.relative_to(source_base);current=root/relative
            if current.read_bytes()!=source.read_bytes():overlay.append(relative.as_posix())
        overlay+=['eval/m6_2/candidate_revision_r27.py','scripts/finalize_m6_2_r10_closeout.py',
                  'tests/test_m6_2_r29_review_method.py','ShopRelay_Agent_Eval_完整评测方案.md']
        for relative in overlay:
            dest=out/'source-final'/relative;dest.parent.mkdir(parents=True,exist_ok=True)
            if dest.exists() and dest.read_bytes()!=(root/relative).read_bytes():raise ValueError('final source snapshot drift')
            if not dest.exists():shutil.copyfile(root/relative,dest)
        put(out/'source-assembly.json',{'base':(R28_OUT/'source-final').as_posix(),'overlay':overlay,'runtime':summary['runtime'],
            'meaning':'Unchanged archived runtime files reused;new method/Registry/source generation overlay explicitly preserved'})
        summary.update(tests=tests,blind_votes_created=0,human_confirmed_reference_items=108,
                       actual_frozen_trials=0,Gold_eligible_assets=0,M6_1='COMPLETE',M6_2='IN_PROGRESS')
        put(out/'closeout-r29.json',summary)
        paths=set(p for p in out.rglob('*') if p.is_file() and p.name not in ('SHA256SUMS.json','verification.json'))
        paths.update(p for p in state.rglob('*') if p.is_file())
        paths.update(root/relative for relative in [amendment_path.as_posix(),event_path.as_posix(),
            'data/eval/new-assets-r26/v7/candidates/frozen100.json','data/eval/new-assets-r26/v7/candidates/dev8.json',
            'eval/m6_1/formal_assets.py','eval/m6_1/contracts.py','eval/m6_1/registry.py',
            'eval/m6_2/candidate_revision_r27.py','scripts/finalize_m6_2_r10_closeout.py',
            'tests/test_m6_2_r29_review_method.py','deploy/m62-eval/images-r29.yaml',
            'data/eval/container-build/r29-final/Dockerfile.cached','docs/probes/verify_m62_r29_deployment.py',
            'docs/probes/m62-r29-method-iteration1.xml','docs/probes/m62-r29-method-iteration1.log',
            'docs/probes/m62-r29-method-iteration2.xml','docs/probes/m62-r29-method-iteration2.log',
            'docs/probes/m62-r29-method-final.xml','docs/probes/m62-r29-method-final.log',
            'docs/probes/m62-r29-legacy-binding-final.xml','docs/probes/m62-r29-legacy-binding-final.log',
            'docs/probes/m62-r29-image-app.log','docs/probes/m62-r29-image-cached-app.log',
            'docs/probes/m62-r29-image-cached-control.log','docs/probes/m62-r29-final-image-app.log',
            'docs/probes/m62-r29-final-image-control.log','docs/probes/m62-r29-final-container-update.log',
            'ShopRelay_Agent_Eval_完整评测方案.md','docs/progress.md'])
        put(out/'SHA256SUMS.json',{'entries':[{'path':p.relative_to(root).as_posix(),'sha256':raw_hash(p),
            'description':'R29 current method/review/preparation source or actual evidence;not execution authorization'} for p in sorted(paths)]})
    return summary


def _handoff_r29(root,out,state,workflows,plan,eligibility,now):
    """One operational handoff;no fabricated future review or permit."""
    text=f'''# R29：实际人类确认、方法替代与后续盲评

108题v7参考已登记为**AI辅助、用户实际复核确认**。事件为`{plan['human_confirmation_event']}`；逐题case/source/labels哈希见human-confirmed-references.json，原标签及条件/理由原样关联。用户没有给出逐题新修改，未创造此类内容。登记时间{now}是本次归档，不推定人工阅题的逐题时刻。

本批方法为`{plan['review_method']}`：AI辅助→用户复核确认→后续真实日期Codex盲评→用户裁决。它**替代**本批旧独立人工初标/真人隔日盲审实现要求；不是更名为旧method，也不叠加旧初标门槛。Codex票不称人工票。范围100/67/60+Dev8已批准，无需重复确认。M6.1 COMPLETE，M6.2 IN_PROGRESS。

## 明早实际操作（当前未启动）

1. 用户实际开始时，在全新且不继承原执行/审核聊天、无原仓库读取权限的上下文进行；不要直接续接这两段已阅参考的对话。将`{state.relative_to(root).as_posix()}/blind-input-only`的两个JSON单独复制到新的输入环境，仅提供包内问题、Fixture/政策及公共Rubric/schema。不得附带v7源JSON、参考、旧意见、确认票或本报告。没有创建聊天、自动任务或模型请求。
2. 记录实际新context ID、可见文件清单、开始/结束UTC和当地区日期、使用模型/工具/上下文隔离方式；日期须在确认事件的实际本地日期之后。若沿用已见参考上下文，必须标非盲评，不能仅隐藏表单字段。
3. 分别填充包内form的四类labels和rationale；不要读取旧label。无法判定的项单列具体缺失，保留待裁决，不为了导入编造。公共schema不提供本题正确路由或预期事实。
4. 返回`{{"submission": form, "context_receipt": {{"assessment_context_id":"实际ID","fresh_context":true,"prior_conversation_loaded":false,"reference_labels_loaded":false,"package_path":"仓库内原输入包路径","package_sha256":"实际包SHA","started_at_utc":"实际时刻","completed_at_utc":"实际时刻","assessor_type":"CODEX_AI"}}}}`。这些是需真实填写的字段，不是已完成票。
5. 在可信接收端按下面入口接收并compare。对比输出逐case/route|required_evidence|atomic_facts|task的差异，不自动用AI覆盖用户确认；用户填写comparison_sha256、每个差异的value/rationale后裁决。无争议维度保留确认值。候选资格未满足时freeze仍拒绝，不生成空白Gold或许可。

## 已有接收入口（不执行未来票）

'''
    for wf in workflows:
        text+=f'''配置：`{wf}`
```powershell
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config {wf} accept-ai-blind --submission <实际回传票相对路径> --output <新的接收回执相对路径>
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config {wf} compare --output <新的对比结果相对路径>
```
'''
    text+='''
## 数据资格与真实待填项

本批保持披露构建/暴露的合成候选身份，当前不能证明独立未见Frozen资格；不是宣称已证实目标模型受污染。56个app源文件与R28一致，未观察真实目标模型获得候选；参考/评测器/适配器则已针对本批修订，Dev/Frozen存在共享条款和家族。用户确认和AI盲评不能填补这种独立性证据。详见data-qualification.json。未另换路线、未造新题。

后续真实输入为：实际盲评票及上下文证据、用户的逐差异裁决、资产资格的可核验证据/范围处置、正式公钥指纹与实际签发者/信任配置，以及最终Gold/Registry与精确分阶段执行许可。不存在要求重做旧人工独立初标的并列门槛。

模型deepseek-flash、现有节点thinking/timeout/endpoint、提示来源、数据及工具范围沿R28核对；价格沿用R28官方观察证据、正式激活前再核有效期。USD4+50及有效窗口仍是未授权提案。私钥仅供按需issuer，应用只拿公钥和独立模拟PG；正式Provider密钥只通过既有外部secret卷供应，禁止打印/落仓。formal-secret-mounts.proposed.yaml仍未应用，工程禁外呼模式不变。

签发前必须绑定实际裁决参考哈希、合格资产身份、Registry哈希、公钥指纹、签发者/执行主体、目标marker、最终镜像/runtime、模型/节点/提示/语料/qrels/规则、案例顺序、次数/预算/窗口、撤销状态。先独立Dev，再冻结段；质量FAIL/PARTIAL记录后继续，权限/身份/未知用量/预算/审计/清理故障停止受影响段。已消费不重放，公共修复用后继段延续同一账本，不热改冻结段。

没有真实Provider/Judge/Embedding、候选应用Run、盲评或正式许可；没有修改OS账号/ACL或正式信任。真实评测是否完成与质量门槛是否通过仍分别记录。
'''
    path=out/'closeout-r29.md'
    if path.exists():
        if path.read_text(encoding='utf8')!=text:raise ValueError('R29 handoff content changed;append explicit correction')
    else:path.write_text(text,encoding='utf8')
    marker='## 2026-10-31 M6.2 R29：实际人工确认与本批审核方法替代'
    progress=root/'docs/progress.md'
    if marker not in progress.read_text(encoding='utf8'):
        with progress.open('a',encoding='utf8') as stream:stream.write('\n\n'+marker+'\n\n- 已核M62-R28-HUMAN-REVIEW-METHOD-AND-PREPARATION-CONFIRMED及v7精确来源，108题参考登记为AI辅助、用户实际复核确认，无逐题编辑杜撰。新方法USER_REVIEWED_AI_THEN_CONTEXT_ISOLATED_AI_BLIND_V1替代本批旧初标/真人盲审实现，不冒充旧method；方案§3.4追加明确范围，其他资产旧方法保留。\n- 准备无旧标签/旧意见的108题输入包与实际后续日期/全新上下文接收、四维比较和用户裁决入口；当前无盲评票、无新聊天或自动任务。数据保持披露合成候选，未证实独立未见，用户签字不改变资格。100/67/60+Dev8不重问。\n- 工程模式/专用PG和已有账本保留，正式秘密/信任/许可未启用；USD4+50仍提案。真实Provider/Judge/Embedding及候选试次0。材料在docs/eval/m6.2-r29-human-method-and-blind-preparation-20261031/。M6.1 COMPLETE，M6.2 IN_PROGRESS；仍需实际后续盲评/裁决、资产资格、正式信任及精确执行许可，不增加全案PASS/M2先PASS门槛。\n')
def put(p,v):
    if p.exists():
        if read(p)!=v:raise ValueError('versioned artifact exists with different content: '+str(p))
    else:write_new(p,v)

def bind_confirmation(root):
    event=read(root/REVIEW/'user-confirmation.json')
    for row in event['bound_review_artifacts']+event['source_bindings']:
        if raw_hash(root/row['path'])!=row['sha256']:raise ValueError('confirmed source drift: '+row['path'])
    return event

def _policy(c,suffix,text):
    p=next(p for p in c['input']['fixture']['policies'] if p['evidence_id'].endswith(suffix))
    p.update(text=text,version='r27-repair-v2',source_ref='new-authoring-r27:'+p['evidence_id'])
    return p['evidence_id']

def _history(row):
    """Construct a compatible fictional audit history, not inferred history."""
    rid=row['id'];oid=row['order_id'];src=row['source_ref']
    states=[('SUBMITTED','submitted_at'),('REVIEW_STARTED','review_started_at')]
    if row['status']=='APPROVED':
        approved=datetime.fromisoformat(row['approved_at'].replace('Z','+00:00'))
        row['submitted_at']=(approved-timedelta(hours=2)).isoformat()
        row['created_at']=row['submitted_at'];row['review_started_at']=(approved-timedelta(hours=1)).isoformat()
        states.append(('APPROVED','approved_at'))
    row['events']=[{'event_id':rid+'-r27-'+name.lower(),'order_id':oid,'return_id':rid,'refund_id':None,
        'event_type':'RETURN_'+name,'occurred_at':row[field],'recorded_at':row[field],
        'actor_type':'SIMULATED_TEST_DATA','source_type':row['source_type'],'source_ref':src,'details_json':'{}'} for name,field in states]

def repair_source(c):
    """Explicit data-authoring patch table. No production code consumes IDs."""
    cid=c['case_id'];f=c['input']['fixture'];notes=[]
    if cid=='FC26-PRODUCT-003':
        notes.append('Missing width threshold is intentionally a clarification task,not fabricated filter input')
    names={'FC26-CROSS_DOMAIN-047':'桌面音箱','FC26-CROSS_DOMAIN-051':'收纳灯','DV26-PRODUCT-005':'纸质订阅存档架'}
    if cid in names:
        old=f['products'][0]['name'];name=names[cid];f['products'][0]['name']=name
        c['input']['user_query']=c['input']['user_query'].replace(old,name)
        for p in f['products']:
            if p['name']==old+'乙型':p['name']=name+'乙型'
        notes.append('Aligned described/catalog product;stable product resource identifier retained')
    descriptions={'FC26-AFTER_SALES-023':('本人订单金额存在争议，请人工核对记录金额；尚未确认具体差额','核对本人订单金额争议','ORDER_STATUS'),
        'DV26-AFTER_SALES-007':('商品说明书存在排版歧义，请人工解释；尚未确认产品故障','说明书排版咨询，不声称已确认故障','POLICY_QUESTION')}
    if cid in descriptions:
        description,summary,category=descriptions[cid];c['input']['owner_interaction']['arguments'].update(
            user_description=description,issue_summary=summary,issue_category=category)
        notes.append('Aligned scripted owner follow-up with actual question;not autonomous Agent planning')
    if cid in {'FC26-ORDER-013','FC26-AFTER_SALES-028','FC26-CROSS_DOMAIN-054','DV26-AFTER_SALES-008'}:
        for row in f['returns']:_history(row)
        notes.append('Added fictional SUBMITTED/review/approval chain with matching aggregate/source/times')
    if cid in {'FC26-CROSS_DOMAIN-048','FC26-CROSS_DOMAIN-050'}:
        first,second=f['products'][:2];oid=f['orders_and_snapshots'][0]['id']
        if not any(r['product_id']==second['id'] for r in f['order_items']):
            f['order_items'].append({'id':cid+'-line-b-r27','order_id':oid,'product_id':second['id'],'quantity':3,'paid_cents':second['price_cents']*3})
            f['orders_and_snapshots'][0]['paid_cents']=sum(r['paid_cents'] for r in f['order_items'])
        notes.append('Added actual second owned order line;preserved both product resources')
    policies={
        'FC26-CROSS_DOMAIN-048':'本虚构订单的易碎商品包装须使用独立内盒和防震填充；是否接受承运仍须核验尺寸、包装检验和承运条款，不保证运输资格。',
        'FC26-CROSS_DOMAIN-050':'普通换货申请需要核对本人订单行数量、规格、完好状态和附件；缺少具体换货原因或商品状态时应补问，申请不等于批准。',
        'FC26-CROSS_DOMAIN-052':'虚构延误核验从承运商实际揽收扫描起算三个工作日；没有揽收与后续扫描记录不能确认已延误、未发货或取消资格。',
        'FC26-CROSS_DOMAIN-053':'预订从供应方备货完成通知起算处理时间；无备货通知及库存观测时点不能确认当前有货或次日送达，目录快照不能替代实时承诺。',
        'FC26-CROSS_DOMAIN-054':'退款只有支付渠道实际确认完成才可表述到账；审核批准后通常三个工作日处理，但提出申请、退货审核、退款批准和到账是不同阶段，没有退款申请记录不得推断已退款。',
        'DV26-POLICY-003':'虚构防尘罩存放前应清洁并完全晾干，置于通风干燥处，避免密封潮湿包装及热源；未提供环境湿度时不保证防霉效果。'}
    if cid in policies:_policy(c,'05',policies[cid]);notes.append('Provided missing task-specific operative clause;new synthetic authoring,not recovered source')
    if cid.startswith('FC26-EXTRA-') and 61<=int(cid.rsplit('-',1)[1])<=68:
        old=next(p for p in f['policies'] if p['evidence_id'].endswith('06'))['text']
        days=re.search(r'起算(\d+)天',old).group(1)
        _policy(c,'06',f'本独立商业渠道仅规定商业服务手续费减免的申请窗口：从服务登记事件起算{days}天，且须商业使用及有效服务凭证。此窗口不是商品退货、退款或保修期限，不能代替其他条款；无渠道适用事实时不得判个人资格。')
        notes.append('Specified commercial exception right;not a universal return/refund deadline')
    return notes

def policy_fact_units(policy):
    """Candidate-reference authoring only. Each unit retains full source scope.

    Not a production semantic parser and never proof of human approval.
    """
    text=policy['text'];scope='仅限本条来源权利/对象及全部条件：'+text
    window=re.search(r'仅规定(.+?)的申请窗口：从(.+?)起算(\d+)天，且须(.+?)。',text)
    if window:
        right,start,days,requirements=window.groups()
        units=[('right',right),('duration_days',int(days)),('start_event',start)]
        units.extend(('necessary_condition',v) for v in re.split('及|并且|且',requirements) if v)
        units.extend(('does_not_define_window',v) for v in ['商品退货','退款','保修'])
        units.append(('qualification','缺少渠道适用事实时不能确认个人资格'))
    else:
        units=[]
        for clause in [p.strip() for p in re.split('[；。]',text) if p.strip()]:
            if '需要核对' in clause:
                subject,requirements=clause.split('需要核对',1)
                units.extend(('necessary_condition',subject+'需要核对'+v) for v in re.split('、|和',requirements) if v)
            elif '应清洁并完全晾干' in clause:
                units.extend(('storage_requirement',v) for v in ['存放前清洁','存放前完全晾干','通风干燥存放','避免密封潮湿包装','避免热源','无环境湿度不能保证防霉效果'])
            else:
                units.extend(('policy_claim',v.strip()) for v in re.split('，',clause) if v.strip())
    return [{'fact_id':policy['evidence_id']+'-unit-'+str(i),'subject':policy['policy_id'],'predicate':predicate,
        'value':value,'condition':scope,'source_ids':[policy['evidence_id']]} for i,(predicate,value) in enumerate(units,1)]

def revise_labels(c,review,source_changed,source_before=None):
    labels=deepcopy(review['labels']);f=c['input']['fixture'];cid=c['case_id']
    # Approved review describes the old source. Repair-specific reference changes
    # are new AI proposals and are explicitly rebound,not inherited human Gold.
    if cid in {'FC26-POLICY-041','FC26-POLICY-043'}:
        labels['route'].update(strategy='SINGLE',sub_strategy='BM25')
        labels['task']['route_equivalents']=[{**{k:labels['route'][k] for k in ROUTE_KEYS},'strategy':'MULTI','sub_strategy':'MULTI'}]
    if source_changed:
        labels['task']['requirements']=[c['input']['user_query'],
            '只陈述本轮取得的事实、对象、单位及条件；缺失事实明确补问，不把申请、审核、批准和到账混同。']
        if 'owner_interaction' in c['input']:
            labels['task']['requirements']+=['预设owner后续输入与Agent答复分别评分；未确认无业务效果，确认后精确一次，拒绝不得写入。']
        if f['returns']:
            labels['task']['requirements']+=['以匹配source_ref及完整事件链核实退货记录；退款无记录不得改称已退款或已到账。']
        if cid=='DV26-AFTER_SALES-008':
            c['input']['allowed_capabilities'].append('get_refund_status')
            labels['route']['required_capabilities']=list(dict.fromkeys(labels['route']['required_capabilities']+['get_refund_status']))
            labels['task']['required_tool_groups']=[[["get_order","get_refund_status"]]]
            labels['task']['requirements']+=['Agent必须读取已批准退货证据；owner API再次核验不能代替Agent能力。']
        # Rebind changed operative clauses to their new text; do not carry the
        # old generic or contradictory fact after a source change.
        before=source_before['fixture'] if source_before is not None else f
        old_policies={p['evidence_id']:p for p in before['policies']}
        changed_policies={p['evidence_id'] for p in f['policies'] if p!=old_policies.get(p['evidence_id'])}
        old_returns={r['id']:r for r in before['returns']}
        changed_returns={r['id'] for r in f['returns'] if r!=old_returns.get(r['id'])}
        labels['atomic_facts']=[x for x in labels['atomic_facts'] if not set(x['source_ids'])&(changed_policies|changed_returns)]
        if cid in {'FC26-CROSS_DOMAIN-048','FC26-CROSS_DOMAIN-050','FC26-CROSS_DOMAIN-052','FC26-CROSS_DOMAIN-053','FC26-CROSS_DOMAIN-054','DV26-POLICY-003'}:
            p=next(p for p in f['policies'] if p['evidence_id'].endswith('05'))
            labels['required_evidence']=[{'requirement':p['text'],'condition':'本题所问对象与条款条件；政策规则不证明个案资格','source_ids':[p['evidence_id']]}]
        for p in f['policies']:
            if p['evidence_id'] in changed_policies:
                labels['atomic_facts'].extend(policy_fact_units(p))
                for requirement in labels['required_evidence']:
                    if p['evidence_id'] in requirement['source_ids']:
                        originals={s['evidence_id']:s for s in f['policies']}
                        requirement['requirement']='；'.join(originals[s]['text'] for s in requirement['source_ids'])
                        requirement['condition']='分别保留各来源权利、对象、起算点及条件；不同权利的窗口不能移用，政策不证明个案资格。'
        for row in f['returns']:
            labels['atomic_facts'].append({'fact_id':row['id']+'-verified-state','subject':row['id'],
                'predicate':'verified_return_status','value':row['status'],'condition':'完整匹配事件链；不推断退款','source_ids':[row['id']]})
    if cid in {'FC26-PRODUCT-001','FC26-PRODUCT-004','FC26-CROSS_DOMAIN-048'}:
        values=[product_id(p['id']) for p in f['products'][:2]]
        labels['task']['required_resources']=[{'tool':'get_product_detail','argument':'product_id','values':values}]
    c['expected_outcome']=labels
    by_id={r['id']:r for kind in ['products','order_items','orders_and_snapshots','inventory'] for r in f[kind]}
    for fact in labels['atomic_facts']:
        if len(fact['source_ids'])==1:
            row=by_id.get(fact['source_ids'][0])
            if row is not None and fact['predicate'] in row:fact['value']=row[fact['predicate']]
    c['rubric_refs']=list(dict.fromkeys(c['rubric_refs']+[V2]))
    return labels

def build(root:Path):
    event=bind_confirmation(root);base=root/NEW;out=root/OUT;out.mkdir(parents=True,exist_ok=True)
    if (base/'revision-record.json').exists():return read(base/'revision-record.json')
    all_cases=[];diffs=[];dispositions=[]
    for name in ['frozen100','dev8']:
        old=read(root/OLD/'candidates'/(name+'.json'));review=read(root/REVIEW/(name+'-ai-reviewed.json'))
        by={r['case_id']:r for r in review['items']};cases=[]
        for original in old['cases']:
            source=EvalCase.from_dict(original);r=by[source.case_id]
            if r['case_sha256']!=formal_case_digest(source):raise ValueError('review/source case drift')
            c=deepcopy(original);notes=repair_source(c);changed=c['input']!=original['input']
            revise_labels(c,r,changed,source_before=original['input'])
            c['data_version']=VERSION
            # Group keys are semantic tool/interaction/scenario families,not
            # FC/DV prefixes. Shared families remain visible across splits.
            interaction=c['input'].get('owner_interaction')
            family=('owner:'+interaction['tool_id'] if interaction else
                'scope:'+c['expected_outcome']['route']['strategy']+':'+','.join(sorted(c['input']['allowed_capabilities'])))
            c['group_id']='SEM-'+hashlib.sha256(family.encode()).hexdigest()[:14]
            case=EvalCase.from_dict(c);cases.append(case);all_cases.append(case)
            diffs.append({'case_id':case.case_id,'old_case_sha256':formal_case_digest(source),'new_case_sha256':formal_case_digest(case),
                'input_changed':changed,'notes':notes,'old_disposition':r['candidate_disposition'],
                'reference_status':'AI_REVISED_PENDING_HUMAN' if changed or notes else 'USER_ADOPTED_OLD_SOURCE_AI_REFERENCE_REBOUND_NOT_GOLD',
                'confirmation_event':event['event_id'],'source_bound_adoption_only':True,
                'changed_reference_dimensions':[k for k in c['expected_outcome'] if c['expected_outcome'][k]!=original['expected_outcome'][k]]})
            dispositions.append({'case_id':case.case_id,'status':'DATA_REPAIRED_PENDING_REVIEW' if changed else
                'CLARIFICATION_CONTRACT_RESOLVED' if case.case_id=='FC26-PRODUCT-003' else
                'EQUIVALENT_ROUTE_PROPOSAL' if r['candidate_disposition']=='ROUTE_ADJUDICATION_REQUIRED' else 'REFERENCE_REBOUND',
                'case_sha256':formal_case_digest(case),'formal_eligible':False})
        payload={**{k:v for k,v in old.items() if k!='cases'},'version':VERSION,'cases':[formal_case_payload(c) for c in cases]}
        validate_asset(payload);put(base/'candidates'/(name+'.json'),payload)
    frozen=all_cases[:100];dev=all_cases[100:]
    oldmeta=read(root/OLD/'case-metadata.json')
    def policy_required(c):
        policies={p['evidence_id'] for p in c.input['fixture']['policies']}
        return bool(c.expected_outcome['required_evidence']) and all(set(r['source_ids'])<=policies for r in c.expected_outcome['required_evidence'])
    cohorts={'route':[c.case_id for c in frozen],'retrieval':[c.case_id for c in frozen if policy_required(c)],'outcome':oldmeta['cohorts']['outcome']}
    excluded=[{'case_id':c.case_id,'reason':'Clarification/refusal/pure business fact task has no required policy;not a zero-recall PASS'} for c in frozen if c.case_id not in cohorts['retrieval']]
    put(base/'case-metadata.json',{'cases':[{**r,'group_id':next(c.group_id for c in frozen if c.case_id==r['case_id']),
        'retrieval_queue':r['case_id'] in cohorts['retrieval']} for r in oldmeta['cases']],
        'dev_cases':[{**r,'group_id':next(c.group_id for c in dev if c.case_id==r['case_id'])} for r in oldmeta['dev_cases']],
        'cohorts':cohorts,'original_approved_planning_counts':{'route':100,'retrieval':80,'outcome':60},
        'actual_retrieval_count':len(cohorts['retrieval']),'retrieval_scope_change_approved':False,'retrieval_exclusions':excluded})
    put(base/'reference-and-source-diff.json',{'cases':diffs,'all_case_dispositions':dispositions,'old_data_preserved':True})
    groups=[]
    for gid in sorted({c.group_id for c in all_cases}):
        members=[c for c in all_cases if c.group_id==gid]
        groups.append({'group_id':gid,'members':[c.case_id for c in members],'cross_split':len({c.split for c in members})>1,
            'basis':'Conservative tool/scenario/owner-action family;not a proof of distinct independent templates'})
    # Exact common clauses are exposure/grouping evidence,not a relevance label.
    fc={p['text'] for c in frozen for p in c.input['fixture']['policies']}
    dc={p['text'] for c in dev for p in c.input['fixture']['policies']}
    put(base/'semantic-groups-and-exposure.json',{'groups':groups,'shared_policy_clause_count':len(fc&dc),
        'shared_clause_hashes':sorted(sha256_json(t) for t in fc&dc),'cross_split_families':sum(g['cross_split'] for g in groups),
        'independence':'NOT_ESTABLISHED_SYNTHETIC_BENCHMARK','exposure_refs':[str(REVIEW/'exposure-record.json'),str(REVIEW/'user-confirmation.json')],
        'modification_actor':'AI_ASSISTANT_CURRENT_DEBUG_CONTEXT','agent_questions_sent_to_real_model':0,
        'current_work':'Changes to data/reference/evaluator boundary;no Router/Agent prompt tuning from these questions'})
    now=datetime.now(timezone.utc)
    for name,count in [('frozen100',100),('dev8',8)]:
        cfg=read(root/OLD/(name+'-workflow.json'))
        cfg.update(asset_id='m62-r27-'+name+'-synthetic-'+NEW.name,intake_stage='CANDIDATE_DRAFT')
        for field in ['intake_record','day1_record','blind_record','frozen_asset','lock_record','registry_output']:
            cfg[field]=cfg[field].replace('/v3/','/'+NEW.name+'/')
        cfg['provenance']['exposure_evidence_refs']=[str(NEW/'semantic-groups-and-exposure.json')]
        cfg['provenance']['independence_evidence_refs']=['NOT_ESTABLISHED_SYNTHETIC_BENCHMARK']
        permit=cfg['content_intake_permit'];permit.update(authorization_ref=event['event_id']+'-OFFLINE-REPAIR',
            source_path=(NEW/'candidates'/(name+'.json')).as_posix(),source_sha256=raw_hash(base/'candidates'/(name+'.json')),
            valid_from_utc=(now-timedelta(minutes=1)).isoformat(),expires_at_utc=(now+timedelta(days=30)).isoformat())
        config_path=base/(name+'-workflow.json')
        if config_path.exists() and not read(config_path)['intake_record'].startswith(NEW.as_posix()+'/'):
            # Preserve a failed pre-intake local configuration,never overwrite
            # the previous version's intake or any actual review record.
            config_path.rename(base/(name+'-workflow.invalid-preflight.json'))
        put(config_path,cfg)
        workflow=AssetWorkflow(root,cfg);workflow.intake()
        put(base/'review'/name/'day1-form.json',workflow.form('day1'))
        put(base/'review'/name/'blind-form-unfilled.json',workflow.form('blind'))
    affected=[r['case_id'] for r in diffs if r['input_changed'] or r['old_disposition']!='USABLE_WITH_REVISED_LABELS']
    put(base/'targeted-re-review.json',{'source_affected_and_related_cases':affected,'count':len(affected),
        'purpose':'Recheck changed sources and equivalent route groups;not repeat accepted old-source opinions as human initial review',
        'independent_initial_review_still_required_for':108,'human_decisions':None})
    record={'version':VERSION,'counts':{'frozen_candidates':100,'dev':8,'source_changed':sum(r['input_changed'] for r in diffs),
        'targeted_review':len(affected),'retrieval_actual':len(cohorts['retrieval']),'retrieval_planned':80},
        'confirmation_event':event['event_id'],'formal_eligible_assets':0,'real_model_calls':0,'old_source_binding':event['source_bindings'],
        'source_hashes':{name:raw_hash(base/'candidates'/(name+'.json')) for name in ['frozen100','dev8']}}
    put(base/'revision-record.json',record)
    return record

def verify_repaired_sources(root:Path):
    from eval.m6_1.personal_dev_database import PersonalDevDatabase
    from eval.m6_1.personal_dev import D1Coordinator
    from eval.m6_1.formal_application import application_plan
    from app.m3.order_tools import requested_order_id
    base=root/NEW;destination=base/'repaired-source-verification.json'
    if destination.exists():return read(destination)
    cases=[EvalCase.from_dict(c) for n in ['frozen100','dev8'] for c in read(base/'candidates'/(n+'.json'))['cases']]
    target=PersonalDevDatabase(base/'validation-work');engine,factory=target.open()
    coordinator=D1Coordinator(target,factory);histories=[];resources=[];reads=0
    history_ids={'FC26-ORDER-013','FC26-AFTER_SALES-028','FC26-CROSS_DOMAIN-054','DV26-AFTER_SALES-008'}
    for case in cases:
        if case.case_id not in history_ids and not case.expected_outcome['task'].get('required_resources'):continue
        plan=application_plan(case);receipt=coordinator.prepare(plan)
        try:
            adapters=coordinator.repositories(plan,receipt,maximum=32)
            if case.case_id in history_ids:
                oid,_=requested_order_id(case.input['user_query']);result=adapters['after_sales'].get_refund_status(oid,plan['owner_id'])
                row=result['returns']['latest_request'];assert row['verified'] is True
                assert row['status']==case.input['fixture']['returns'][0]['status']
                histories.append({'case_id':case.case_id,'actual_verified':row['verified'],'actual_return_status':row['status'],
                    'event_ids':row['source']['event_ids'] if 'source' in row else row['event_ids'],
                    'refund_record_present':result['refunds']['latest_request'] is not None,
                    'order_and_owner_from':{'order_id':oid,'owner_id':plan['owner_id']}})
            for constraint in case.expected_outcome['task'].get('required_resources',[]):
                actual=[]
                for identifier in constraint['values']:
                    value=adapters['product'].get_product_detail(identifier)
                    assert value['availability']=='AVAILABLE'
                    actual.append(value['product']['product_id'])
                assert actual==list(constraint['values']) and len(set(actual))==len(actual)
                resources.append({'case_id':case.case_id,'actual_resource_ids':actual,'distinct':True})
            reads+=adapters['store'].allowance.calls
        finally:coordinator.cleanup(receipt)
        proof=coordinator.diagnostic_cleanup_evidence(receipt,cleaned=True)
        assert proof['residual_case_rows']==0 and proof['lease_released']
    engine.dispose();assert len(histories)==4 and len(resources)==3
    clocks=[];now=datetime.now(timezone.utc)
    for case in cases:
        if 'owner_interaction' not in case.input:continue
        command=case.input['owner_interaction'];order=case.input['fixture']['orders_and_snapshots'][0]
        delivery=order.get('delivered_at')
        clocks.append({'case_id':case.case_id,'tool':command['tool_id'],'actual_now_utc':now.isoformat(),
            'delivery_fact':delivery,'source_seven_day_boundary':(datetime.fromisoformat(delivery.replace('Z','+00:00'))+timedelta(days=7)).isoformat() if delivery else None,
            'qualification':'M4 records a return application with eligibility NOT_DETERMINED;no automatic seven-day approval/refusal is claimed. Policy time applicability remains a scored condition,not rewritten timestamps.'})
    result={'actual_repository_history_replays':histories,'actual_two_resource_reads':resources,'additional_reads':reads,
        'clock_observations':clocks,'m4_evidence_refs':['app/m4/actions.py:prepare_return_action','app/m4/actions.py:_revalidate_return_snapshot',
        'docs/eval/m6.2-container-candidates-r26-20261031/closeout-r26.json'],
        'action_effects':'R26 actual API/PG confirmation/idempotency evidence retained;no production action implementation changed in R27',
        'actual_model_calls':0,'application_trials':0}
    put(destination,result);return result

def _r28_confirmation(root):
    # Restored facts come from the earlier user-adopted review,so verify that
    # source event too at this input/recovery boundary,not just its row count.
    bind_confirmation(root)
    event=read(root/R28_REVIEW/'user-confirmation.json')
    for row in event['bound_review_artifacts']+event['candidate_sources_at_confirmation']:
        if raw_hash(root/row['path'])!=row['sha256']:raise ValueError('R28 confirmed source drift: '+row['path'])
    if event['approved_scope_change']['required_policy_retrieval_cases']!=67:raise ValueError('exact confirmed retrieval scope required')
    return event

def _repair_reference_values(value,old_source,adopted_labels):
    """Data/reference revision,not production behavior keyed by evaluation ID."""
    from eval.m6_1.formal_reference_rules import V3
    facts=value['expected_outcome']['atomic_facts'];fixture=value['input']['fixture']
    sources={r['id']:r for collection in fixture.values() if isinstance(collection,list) for r in collection if 'id' in r}
    synced=[]
    for fact in facts:
        if len(fact['source_ids'])==1:
            source=sources.get(fact['source_ids'][0]);field=fact['predicate']
            if source is not None and field in source and fact['value']!=source[field]:
                synced.append({'fact_id':fact['fact_id'],'field':field,'old':fact['value'],'new':source[field]})
                fact['value']=source[field]
    old_policies={p['evidence_id']:p for p in old_source['input']['fixture']['policies']}
    policies={p['evidence_id']:p for p in fixture['policies']}
    unchanged={key for key,p in policies.items() if old_policies.get(key)==p}
    restored=[]
    def key(f):return sha256_json({k:v for k,v in f.items() if k!='fact_id'})
    existing={key(f) for f in facts};ids={f['fact_id'] for f in facts}
    for f in adopted_labels['atomic_facts']:
        if set(f['source_ids'])<=unchanged and key(f) not in existing:
            restored.append(deepcopy(f));copy=deepcopy(f)
            if copy['fact_id'] in ids:copy['fact_id']='restored-'+key(f)[:16]
            facts.append(copy);ids.add(copy['fact_id']);existing.add(key(f))
    whole=[f for f in facts if f['predicate']=='source_clause_with_conditions']
    facts[:]=[f for f in facts if f['predicate']!='source_clause_with_conditions']
    for f in whole:
        if len(f['source_ids'])!=1:raise ValueError('atomic source decomposition requires exact source')
        facts.extend(policy_fact_units(policies[f['source_ids'][0]]))
    task=value['expected_outcome']['task'];cid=value['case_id'];notes=[]
    if any('get_order_items' in option for group in task['required_tool_groups'] for option in group):
        user=value['input']['user_id'];owner=value['input']['owner_map'][user]
        owned={o['id'] for o in fixture['orders_and_snapshots'] if o['owner_id'] in {user,owner}}
        refs=value['input']['fixture_refs']
        for line in fixture['order_items']:
            if line['order_id'] in owned and line['id'] not in refs:
                refs.append(line['id']);notes.append('Added declared owned order-line source to actual fixture preparation: '+line['id'])
    # Explicit corrections to reference data,never input to the Agent/Router.
    if cid in {'FC26-CROSS_DOMAIN-048','FC26-CROSS_DOMAIN-050'}:
        rows=[]
        for line in fixture['order_items']:
            for field in (['product_id','quantity'] if cid.endswith('050') else ['product_id']):
                if not any(f['source_ids']==[line['id']] and f['predicate']==field for f in facts):
                    facts.append({'fact_id':line['id']+'-'+field,'subject':line['id'],'predicate':field,'value':line[field],
                        'condition':'同一已认证订单的该条明细，逐行判分；不能以另一商品数量替代','source_ids':[line['id']]})
            fields={'product_id':product_id(line['product_id'])}
            if cid.endswith('050'):fields['quantity']=line['quantity']
            rows.append({'source_id':line['id'],'fields':fields})
        task['required_result_rows']=[{'tool':'get_order_items','result_path':['items'],'source_field':'fixture_provenance','rows':rows}]
        task['requirements'].append('两条本人订单的商品关联分别核对；题目要求数量时逐行回答数量。真实工具结果逐行校验不等于用户答案已完整交付。')
        notes.append('Both actual order lines have product/quantity references and per-row source-result checks')
    if cid=='FC26-PRODUCT-006':
        reference=value['expected_outcome']['route'];alts=[]
        for option in task['required_tool_groups'][0]:
            if set(option)!=set(reference['required_capabilities']):
                alts.append({**{k:deepcopy(reference[k]) for k in ROUTE_KEYS},'required_capabilities':list(option)})
        task['route_equivalents']=alts
        notes.append('Only the three task-approved product paths are legal routing references;other whitelisted tools are not substitutes')
    value['rubric_refs']=list(dict.fromkeys(value['rubric_refs']+[V3]))
    return {'direct_fields_synchronized':synced,'unchanged_source_facts_restored':restored,
        'long_source_facts_split':len(whole),'notes':notes}

def build_r28(root:Path):
    from eval.m6_1.formal_reference_rules import V3,direct_source_facts
    event=_r28_confirmation(root);base=root/R28_BASE;out=root/R28_OUT;out.mkdir(parents=True,exist_ok=True)
    if (base/'revision-record.json').exists():return read(base/'revision-record.json')
    rows=[];all_cases=[];unchanged_inputs=[];old_counts={};new_counts={}
    for name in ['frozen100','dev8']:
        parent=read(root/NEW/'candidates'/(name+'.json'))
        old={c['case_id']:c for c in read(root/OLD/'candidates'/(name+'.json'))['cases']}
        adopted={r['case_id']:r for r in read(root/REVIEW/(name+'-ai-reviewed.json'))['items']}
        cases=[];old_counts[name]=sum(len(c['expected_outcome']['atomic_facts']) for c in parent['cases'])
        for original in parent['cases']:
            value=deepcopy(original)
            changes=_repair_reference_values(value,old[value['case_id']],adopted[value['case_id']]['labels'])
            value['data_version']='m62-new-candidates-r28-'+R28_BASE.name;case=EvalCase.from_dict(value)
            checked=direct_source_facts(case);assert not checked['mismatches']
            rows.append({'case_id':case.case_id,'parent_case_sha256':formal_case_digest(EvalCase.from_dict(original)),
                'case_sha256':formal_case_digest(case),**changes,'direct_facts_checked':len(checked['checked']),
                'reference_fact_count':len(case.expected_outcome['atomic_facts']),'human_decision':None})
            unchanged_inputs.append({'case_id':case.case_id,'input_sha256':sha256_json(value['input']),
                'parent_input_sha256':sha256_json(original['input']),'unchanged':original['input']==value['input']})
            cases.append(case);all_cases.append(case)
        new_counts[name]=sum(len(c.expected_outcome['atomic_facts']) for c in cases)
        payload={**{k:v for k,v in parent.items() if k!='cases'},'version':'m62-new-candidates-r28-'+R28_BASE.name,'cases':[formal_case_payload(c) for c in cases]}
        validate_asset(payload);put(base/'candidates'/(name+'.json'),payload)
    meta=deepcopy(read(root/NEW/'case-metadata.json'))
    meta.update(retrieval_scope_change_approved=True,scope_authorization_event=event['event_id'])
    assert {k:len(v) for k,v in meta['cohorts'].items()}=={'route':100,'retrieval':67,'outcome':60}
    put(base/'case-metadata.json',meta)
    put(out/'scope-change.json',{'event_id':event['event_id'],'previous_retrieval_plan':80,'approved_retrieval_count':67,
        'coverage_reduction':13,'route':100,'outcome':60,'frozen_candidate_questions':100,'separate_dev':8,
        'execution_authorized':False,'independence_or_Gold_approved':False,'confirmed_source_sha256':raw_hash(root/R28_REVIEW/'user-confirmation.json')})
    put(base/'reference-repair-audit.json',{'cases':rows,'direct_fields_changed':sum(len(r['direct_fields_synchronized']) for r in rows),
        'unchanged_source_facts_restored':sum(len(r['unchanged_source_facts_restored']) for r in rows),
        'old_atomic_fact_counts':old_counts,'new_atomic_fact_counts':new_counts,
        'review_scope':'AI correction proposals,new bytes not independent human/Gold labels'})
    # Input and fixture bytes did not change. Retain original real-read receipts,
    # not another108 reads. Affected adapter behavior has separate full CLI tests.
    for filename in ['repository-validation.json','repaired-source-verification.json']:
        original=read(root/NEW/filename)
        put(base/filename,{**original,'reused_from':(NEW/filename).as_posix(),'reused_source_sha256':raw_hash(root/NEW/filename),
            'unchanged_input_bindings':[r for r in unchanged_inputs if r['unchanged']],
            'excluded_changed_inputs':[r['case_id'] for r in unchanged_inputs if not r['unchanged']],
            'qualification':'Historical reads reused only for106 unchanged inputs;changed cases require new actual-adapter proof',
            'new_reads_for_reference_only_revision':0})
    put(base/'semantic-groups-and-exposure.json',{**read(root/NEW/'semantic-groups-and-exposure.json'),
        'successor_reference_revision':'r28-'+R28_BASE.name,'input_parent':'v5','reference_editor':'AI_ASSISTANT_CURRENT_DEBUG_CONTEXT',
        'additional_review_event':event['event_id'],'independence':'NOT_ESTABLISHED_SYNTHETIC_BENCHMARK'})
    now=datetime.now(timezone.utc)
    for name in ['frozen100','dev8']:
        cfg=deepcopy(read(root/NEW/(name+'-workflow.json')))
        cfg['asset_id']='m62-r28-'+name+'-synthetic-'+R28_BASE.name
        for field in ['intake_record','day1_record','blind_record','frozen_asset','lock_record','registry_output']:
            cfg[field]=cfg[field].replace('/v5/','/'+R28_BASE.name+'/')
        cfg['provenance']['exposure_evidence_refs']=[(R28_BASE/'semantic-groups-and-exposure.json').as_posix()]
        cfg['content_intake_permit'].update(authorization_ref=event['event_id']+'-OFFLINE-REFERENCE-CORRECTION',
            source_path=(R28_BASE/'candidates'/(name+'.json')).as_posix(),source_sha256=raw_hash(base/'candidates'/(name+'.json')),
            valid_from_utc=(now-timedelta(minutes=1)).isoformat(),expires_at_utc=(now+timedelta(days=30)).isoformat())
        put(base/(name+'-workflow.json'),cfg);w=AssetWorkflow(root,cfg);w.intake()
        put(base/'review'/name/'day1-form.json',w.form('day1'));put(base/'review'/name/'blind-form-unfilled.json',w.form('blind'))
    registry=deepcopy(read(root/NEW/'candidate-registry-metadata.json'))
    for asset in registry['assets']:
        name='frozen100' if asset['case_count']==100 else 'dev8'
        asset.update(asset_id='m62-r28-'+name+'-synthetic-'+R28_BASE.name,version='m62-new-candidates-r28-'+R28_BASE.name,
            source_path=(R28_BASE/'candidates'/(name+'.json')).as_posix(),sha256=raw_hash(base/'candidates'/(name+'.json')))
    put(base/'candidate-registry-metadata.json',registry)
    result={'version':'r28-'+R28_BASE.name,'case_count':108,'inputs_unchanged':sum(r['unchanged'] for r in unchanged_inputs),
        'changed_inputs':[r['case_id'] for r in unchanged_inputs if not r['unchanged']],
        'cohort_counts':{k:len(v) for k,v in meta['cohorts'].items()},
        'old_atomic_fact_counts':old_counts,'new_atomic_fact_counts':new_counts,'direct_fields_synchronized':sum(len(r['direct_fields_synchronized']) for r in rows),
        'restored_unchanged_source_facts':sum(len(r['unchanged_source_facts_restored']) for r in rows),
        'direct_fields_checked':sum(r['direct_facts_checked'] for r in rows),
        'source_hashes':{n:raw_hash(base/'candidates'/(n+'.json')) for n in ['frozen100','dev8']},
        'adoption_scope':event['event_id'],'new_human_decisions':0,'formal_eligible_assets':0,'model_calls':0}
    put(base/'revision-record.json',result);return result

def validate_r28(root:Path):
    from eval.m6_1.formal_application import application_plan,MeteredReads
    from eval.m6_1.personal_dev_database import PersonalDevDatabase
    from eval.m6_1.personal_dev import D1Coordinator
    from eval.m6_1.store import ResultStore
    from eval.m6_1.budget import BudgetLedger,BudgetLimits
    from eval.m6_1.contracts import EvalExperiment
    from eval.m6_1.formal_reference_rules import resource_coverage,result_row_coverage,direct_source_facts,route_reference
    from app.m3.order_tools import requested_order_id
    out=root/R28_OUT;path=out/'actual-adapter-and-reference-verification.json'
    if path.exists():return read(path)
    build_r28(root)
    cases=[EvalCase.from_dict(v) for n in ['frozen100','dev8'] for v in read(root/R28_BASE/'candidates'/(n+'.json'))['cases']]
    validation_root=root/'data/eval/r28-offline-validation';validation_root.mkdir(parents=True,exist_ok=True)
    target=PersonalDevDatabase(validation_root);engine,factory=target.open(initialize=not target.path.exists())
    coordinator=D1Coordinator(target,factory)
    store=ResultStore(root/'data/eval/r28-offline-validation/adapter-budget.sqlite3')
    limits=BudgetLimits(tool_calls=8,max_calls_per_trial=8)
    cfg={'purpose':'OFFLINE_REPOSITORY_ADMISSION_NOT_APPLICATION_TRIAL','data':read(root/R28_BASE/'revision-record.json')['source_hashes']}
    experiment=EvalExperiment('r28-offline-adapter-'+R28_BASE.name,cfg,sha256_json(cfg),'NO_GIT_OPERATION',None,{}, {},'ENGINEERING_EVAL',
        ('get_product_detail','get_order_items'),limits.to_dict(),'r28-'+R28_BASE.name,'formal-reference-v3')
    store.lock_experiment(experiment,datetime.now(timezone.utc).isoformat());ledger=BudgetLedger(store,experiment.experiment_id,limits)
    observations=[];all_direct=[];policy_gaps=[]
    for case in cases:
        direct=direct_source_facts(case)
        assert not direct['mismatches'];all_direct.append({'case_id':case.case_id,'checked':direct['checked'],'non_direct_count':len(direct['non_direct_facts'])})
        needed={s for r in case.expected_outcome['required_evidence'] for s in r['source_ids']}
        facts={s for r in case.expected_outcome['atomic_facts'] for s in r['source_ids']}
        if needed-facts:policy_gaps.append({'case_id':case.case_id,'uncovered_sources':sorted(needed-facts)})
        constraints=case.expected_outcome['task']
        if not (constraints.get('required_resources') or constraints.get('required_result_rows')):continue
        plan=application_plan(case);receipt=coordinator.prepare(plan);records=[]
        try:
            adapters=coordinator.repositories(plan,receipt,maximum=32)
            for resource in constraints.get('required_resources',[]):
                product=MeteredReads(adapters['product'],ledger,'admission.'+case.case_id,records)
                for identifier in resource['values']:
                    assert identifier in case.input['user_query']
                    assert product.get_product_detail(product_id=identifier)['availability']=='AVAILABLE'
            if constraints.get('required_result_rows'):
                order=MeteredReads(adapters['order'],ledger,'admission.'+case.case_id,records)
                identifier,error=requested_order_id(case.input['user_query']);assert identifier and not error
                order.get_order_items(identifier,plan['owner_id'])
            resource_status=resource_coverage(case,records);row_status=result_row_coverage(case,records)
            if resource_status not in {'PASS','NOT_APPLICABLE'} or row_status['status'] not in {'PASS','NOT_APPLICABLE'}:
                write_new(out/('failed-adapter-check-'+case.case_id+'-'+datetime.now(timezone.utc).strftime('%H%M%S')+'.json'),
                    {'case_id':case.case_id,'records':records,'resource_coverage':resource_status,'row_coverage':row_status})
            assert resource_status in {'PASS','NOT_APPLICABLE'} and row_status['status'] in {'PASS','NOT_APPLICABLE'}
            observations.append({'case_id':case.case_id,'receipt_lease':receipt.lease_id,'records':records,
                'resource_coverage':resource_status,'result_rows':row_status,'repository_reads':adapters['store'].allowance.calls})
        finally:coordinator.cleanup(receipt)
        proof=coordinator.diagnostic_cleanup_evidence(receipt,cleaned=True)
        assert proof['residual_case_rows']==0 and proof['lease_released'];observations[-1]['cleanup']=proof
    engine.dispose();assert not policy_gaps
    alternative=next(c for c in cases if c.case_id=='FC26-PRODUCT-006')
    routes={tool:route_reference(alternative,{**alternative.expected_outcome['route'],'required_capabilities':[tool]})
        for tool in ['get_product_detail','search_products','recommend_products','check_inventory']}
    assert routes=={'get_product_detail':'PASS','search_products':'PASS','recommend_products':'PASS','check_inventory':'FAIL'}
    result={'scope':'Actual Repository+MeteredReads on new isolated synthetic database;not Agent or real-model trial',
        'cases_checked':108,'direct_field_checks':all_direct,'direct_field_mismatches':[],'required_policy_sources_without_atomic_items':[],
        'affected_adapter_cases':observations,'route_alternative_probes':routes,'budget':ledger.snapshot(),
        'actual_new_repository_reads':sum(r['repository_reads'] for r in observations),'application_runs':0,'provider_requests':0,'human_decisions':0}
    assert result['actual_new_repository_reads']==8 and result['budget']['active_reservations']==0
    put(path,result);return result

def finalize_r28(root:Path):
    """R28 evidence projection using the existing fixed finalizer,not a Runner."""
    import shutil,xml.etree.ElementTree as ET
    from dataclasses import asdict
    from eval.m6_1.grants import formal_runtime_digest
    from eval.m6_1.store import ResultStore
    from eval.m6_1.budget import BudgetLedger,BudgetLimits
    from app.m0.config import load_settings
    event=_r28_confirmation(root);revision=build_r28(root);verified=validate_r28(root)
    out=root/R28_OUT;base=root/R28_BASE
    audit=read(out/'actual-isolation-audit.json')
    assert audit['actual_application_runtime_sha256']==formal_runtime_digest()
    xml=ET.parse(root/'docs/probes/m62-r28-owner-final.xml').getroot()
    suites=[xml] if xml.tag=='testsuite' else list(xml)
    tests={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']}
    assert tests=={'tests':84,'failures':0,'errors':0,'skipped':0}
    frozen=[EvalCase.from_dict(c) for c in read(base/'candidates/frozen100.json')['cases']]
    dev=[EvalCase.from_dict(c) for c in read(base/'candidates/dev8.json')['cases']]
    meta=read(base/'case-metadata.json');assert meta['retrieval_scope_change_approved'] is True
    assert len(meta['cohorts']['retrieval'])==67
    counts={k:len(v) for k,v in meta['cohorts'].items()}
    fact_counts={'frozen100':sum(len(c.expected_outcome['atomic_facts']) for c in frozen),
        'dev8':sum(len(c.expected_outcome['atomic_facts']) for c in dev),
        'primary_outcome60':sum(len(c.expected_outcome['atomic_facts']) for c in frozen if c.case_id in meta['cohorts']['outcome']),
        'retrieval67':sum(len(c.expected_outcome['atomic_facts']) for c in frozen if c.case_id in meta['cohorts']['retrieval'])}
    for case in frozen:
        if case.case_id in meta['cohorts']['retrieval']:
            assert case.expected_outcome['required_evidence']
            policies={p['evidence_id'] for p in case.input['fixture']['policies']}
            assert all(set(r['source_ids'])<=policies for r in case.expected_outcome['required_evidence'])
    denominators={'approved_cohorts':counts,'scope_event':event['event_id'],'previous_retrieval_plan':80,'approved_reduction':13,
        'reference_fact_items':fact_counts,'actual_scored_formal_trials':0,'human_scored_fact_items':0,
        'formal_quality_metrics':'N/A_NOT_RUN_AND_NO_REAL_HUMAN_OUTPUT_LABELS',
        'denominator_note':'Reference counts are pending-review items,not measured model accuracy;379 across108 must not be confused with the Outcome60 fact metric.'}
    put(out/'denominators-and-scope.json',denominators)
    ledger_store=ResultStore(root/'data/eval/r28-offline-validation/adapter-budget.sqlite3')
    with ledger_store.transaction(immediate=False) as db:experiments=[r[0] for r in db.execute('SELECT experiment_id FROM experiments')]
    offline_budgets={eid:BudgetLedger(ledger_store,eid,BudgetLimits(**ledger_store.budget_limits(eid))).snapshot() for eid in experiments}
    assert all(b['active_reservations']==0 for b in offline_budgets.values())
    measured_reads=sum(b['calls_by_category']['tool'] for b in offline_budgets.values())
    snapshot=root/'docs/eval/m6.2-r27-final-revised-candidates-and-execution-20261031/source-final/app'
    app_paths=[p for p in snapshot.rglob('*.py')]
    changes=[p.relative_to(snapshot).as_posix() for p in app_paths if p.read_bytes()!=(root/'app'/p.relative_to(snapshot)).read_bytes()]
    assert not changes
    independence={'current_identity':'DISCLOSED_AI_PROGRAM_SYNTHETIC_CANDIDATES','unseen_independent_Frozen_eligible':False,
        'human_initial_or_later_date_records_created':0,'app_files_compared':len(app_paths),'changed_app_files':changes,
        'evaluation_adapter_and_grader_changed':True,'runtime':formal_runtime_digest(),
        'exposure_record':(R28_BASE/'semantic-groups-and-exposure.json').as_posix(),
        'interpretation':'Reviewer access alone does not prove model contamination;source construction/exposure and reference-driven evaluator fixes do not establish independent unseen qualification.'}
    put(out/'independence-and-reference-lineage.json',independence)
    settings=load_settings(require_api_key=False);price_path=out/'public-price-observation.json'
    if settings.model!='deepseek-flash':raise ValueError('prepared model changed;do not reuse Flash pricing')
    price=read(price_path) if price_path.exists() else {'observed_at_utc':datetime.now(timezone.utc).isoformat(),
        'source':'https://api-docs.deepseek.com/quick_start/pricing/','model':settings.model,'endpoint':settings.base_url,
        'peak_input_cache_miss_usd_per_million':.30,'peak_output_usd_per_million':1.20,
        'conservative_all_tokens_usd_per_million':1.20,'cache_off_peak_discount_assumed':False,
        'price_revalidate_before_issue':True,'account_balance_and_actual_invoice':'UNKNOWN_NO_ACCOUNT_OR_MODEL_PROBE'}
    put(price_path,price)
    parent_plan=root/'docs/eval/m6.2-r27-final-revised-candidates-and-execution-20261031/precise-execution-request.json'
    plan=deepcopy(read(parent_plan));plan.update(schema_version='r28-precise-execution-request-v1',
        data_version='m62-new-candidates-r28-v7',data_sha256=revision['source_hashes'],implementation_sha256=formal_runtime_digest(),
        model=settings.model,endpoint=settings.base_url,node_configuration={k:asdict(v) for k,v in settings.nodes.items()},
        draft_references_sha256=sha256_json({n:[formal_case_payload(c)['expected_outcome'] for c in cs] for n,cs in [('frozen100',frozen),('dev8',dev)]}),
        price_observation_sha256=raw_hash(price_path),cohorts=meta['cohorts'],retrieval_count=67,
        retrieval_scope_approved=True,retrieval_scope_event=event['event_id'],grading_contract='formal-reference-constraints-v3',
        current_asset_identity='DISCLOSED_SYNTHETIC_CANDIDATES_NOT_INDEPENDENT_FROZEN',
        requested_valid_window_utc={'start':'2026-11-02T00:00:00+00:00','end':'2026-11-05T12:00:00+00:00'})
    plan.pop('scope_change_request',None)
    plan['missing_before_issue']=[s for s in plan['missing_before_issue'] if '67-vs80' not in s]
    plan['execution_paths']={
        'disclosed_synthetic_benchmark':{'current_identity_permitted':True,'execution_authorized_now':False,
            'claim':'Disclosed Dev/synthetic benchmark only;not independent Frozen or a substitute for M6.2 formal quality evidence',
            'needed':'Matching precise personal benchmark authorization/profile if this path is selected;not rename engineering grants or waive formal Gold gates'},
        'independent_unseen_formal_Frozen':{'qualified_now':False,'execution_authorized_now':False,
            'needed':'Actual review/adjudicated Gold and Registry,documented qualification relative to final config,formal trust/secrets,and exact permission'}}
    for phase in plan['phases']:
        phase['name']='PROPOSED_DEV8_VALIDATION_AFTER_REVIEW' if phase['N']==8 else 'PROPOSED_FORMAL_FROZEN100_AFTER_QUALIFICATION'
        phase['current_identity_is_not_a_formal_approval']=True
    plan['request_binding_sha256']=sha256_json({k:v for k,v in plan.items() if k!='request_binding_sha256'})
    put(out/'precise-execution-request.json',plan)
    repair=read(base/'reference-repair-audit.json')
    changed=[r for r in repair['cases'] if r['direct_fields_synchronized'] or r['unchanged_source_facts_restored'] or r['long_source_facts_split'] or r['notes']]
    put(base/'targeted-review.json',{'cases':changed,'count':len(changed),'human_decisions':None,
        'source_inputs_changed':revision['changed_inputs'],'actual_independent_initial_review_still_required':108,
        'method':'AI correction proposals are separate;actual first annotation and subsequent-date blind review remain unchanged under evaluation §3.4'})
    matrix=[
        {'task':1,'status':'DEV_EVIDENCE_AND_ENGINEERING_PARTIAL','done':'Historical39 Dev Task confirmations retained;five reference defects and approved100/67/60 scope now implemented',
            'remaining':'Real independent reference/output review,qualified assets and locked-config real-model route/evidence/Outcome measurements'},
        {'task':2,'status':'LIMITED_DEV_CALIBRATION_RETAINED','done':'Prior four-object Judge batch remains closed;no rerun',
            'remaining':'Actual dimension-matched human references and precise finite Judge permission if broader calibration is required;do not merge unlike scoring targets'},
        {'task':3,'status':'ENGINEERING_VERIFIED_FORMAL_NOT_EXECUTED','done':'Existing container/PG/roles/secret separation preserved;actual product/row CLI path and failure continuation checked',
            'remaining':'Real §3.4 review and qualification,Gold/Registry and formal key/activation permission,then exact Dev/Frozen execution;033 still deferred'},
        {'task':4,'status':'DEV_REPORTS_AND_OFFLINE_RECOMPUTE_AVAILABLE','done':'Versioned raw inputs/source references,corrected denominators and failure evidence retained',
            'remaining':'Formal raw runs,model usage/human disagreements/n/N/intervals and final report;completion and passing quality thresholds are distinct'}]
    put(out/'exit-matrix.json',{'M6.1':'COMPLETE','M6.2':'IN_PROGRESS','tasks':matrix,'new_hard_gates_added':False})
    summary={'version':'R28','data':revision,'tests':tests,'runtime':formal_runtime_digest(),
        'measured_offline_adapter_reads':measured_reads,'successful_final_adapter_reads':verified['actual_new_repository_reads'],
        'failed_earlier_adapter_reads':measured_reads-verified['actual_new_repository_reads'],'offline_ledgers':offline_budgets,
        'denominators':denominators,'targeted_reference_review':len(changed),
        'actual_container_target':audit['negative_permit_probes']['target_identity'],
        'real_provider_judge_embedding_calls':0,'new_real_application_trials':0,'formal_grants_issued':0,
        'formal_activation':False,'human_initial_or_blind_decisions_created':0,'M6.1':'COMPLETE','M6.2':'IN_PROGRESS'}
    put(out/'closeout-r28.json',summary)
    _handoff_r28(root,summary)
    final=out/'source-final'
    for p in (root/'data/eval/container-build/r26/code').rglob('*'):
        if p.is_file():
            dst=final/p.relative_to(root/'data/eval/container-build/r26/code');dst.parent.mkdir(parents=True,exist_ok=True)
            if not dst.exists():shutil.copyfile(p,dst)
            assert raw_hash(p)==raw_hash(dst)
    files={p for p in out.rglob('*') if p.is_file() and p.name not in {'SHA256SUMS.json','verification.json'}}
    files.update(p for p in base.rglob('*') if p.is_file())
    files.update(root/p for p in ['docs/progress.md','eval/m6_2/candidate_revision_r27.py','scripts/finalize_m6_2_r10_closeout.py',
        'tests/test_m6_2_r28_reference_integrity.py','deploy/m62-eval/images-r28.yaml',
        'data/eval/r28-offline-validation/adapter-budget.sqlite3',
        'docs/probes/m62-r28-repro-iteration1.xml','docs/probes/m62-r28-repro-iteration1.log',
        'docs/probes/m62-r28-repro-iteration2.xml','docs/probes/m62-r28-repro-iteration2.log',
        'docs/probes/m62-r28-repro-iteration3.xml','docs/probes/m62-r28-repro-iteration3.log',
        'docs/probes/m62-r28-final.xml','docs/probes/m62-r28-final.log',
        'docs/probes/m62-r28-complete-final.xml','docs/probes/m62-r28-complete-final.log',
        'docs/probes/m62-r28-owner-final.xml','docs/probes/m62-r28-owner-final.log'])
    files.update(root/r['path'] for r in event['bound_review_artifacts']);files.add(root/R28_REVIEW/'user-confirmation.json')
    files.update(root/REVIEW/name for name in ['user-confirmation.json','frozen100-ai-reviewed.json','dev8-ai-reviewed.json'])
    files={p for p in files if p.name!='SHA256SUMS-before-final.json'}
    inventory={'entries':[{'path':p.relative_to(root).as_posix(),'sha256':raw_hash(p),
        'description':'R28 new reference/source/actual offline evidence;no secret'} for p in sorted(files)]}
    destination=out/'SHA256SUMS.json'
    if destination.exists() and read(destination)!=inventory:
        history=out/'SHA256SUMS-before-final.json'
        if not history.exists():write_new(history,read(destination))
        temporary=out/'SHA256SUMS-next.json';write_new(temporary,inventory);temporary.replace(destination)
    else:put(destination,inventory)
    return summary

def _handoff_r28(root,s):
    out=root/R28_OUT
    text=f'''# R28：五项修复、67题范围及正式执行准备

五项修复已完成，最终数据为`data/eval/new-assets-r26/v7`；v3/v4/v5及本轮v6草案保留。用户事件`M62-R27-REVIEW-AND-RETRIEVAL67-USER-CONFIRMED`已核验，**路由100/政策必需证据67/主要Outcome60已批准**，覆盖比原80减少13题，不再请求相同范围确认。未批准独立未见资格、真实模型调用或USD54执行。

| 事项 | 修复与实际验证 |
|---|---|
| 两资源误停 | FixtureProductRepository实参改为与正式仓库一致的product_id；真实适配器→MeteredReads→评分→完整6案CLI走通，两个商品均读取、无资源UNKNOWN、后案继续。旧arguments.product不改写，旧版未知语义不静默转PASS。 |
| 旧商品标签 | 同步桌面音箱、收纳灯、纸质订阅存档架3项name；全108题70项同源直接字段检查无矛盾，不把其余语义项当成已证实。 |
| 未变来源事实丢失 | 从已绑定旧审核保留23条Frozen DOC05＋3条Dev DOC06事实，共26条；源修订函数只重建真正改变的来源。长条款分为权利、时长、起算点、各必要条件与排除项，每项保留完整条件范围。 |
| 第二订单行 | 050补第二商品及3件的原子事实，首行2件分别保留；逐行核对同一工具结果的source/product/quantity。实际读数2/3通过。关联048也核对两件本人订单商品的归属行。 |
| 合法路径不一致 | 006明确详情/搜索/推荐三种路由参考，三者均PASS；库存单独替代FAIL，不放宽为任意白名单能力。 |

## 实际发现与处理

定向真实Repository复验发现：048/050第二行虽在Fixture中，却不在fixture_refs，原应用准备只装载第一行。后继v7补入已声明、已认证owner所属订单行；不会遍历或加入其他用户订单。两项input改变，所以只复用其余106题的实读/清理证据，两项及受影响产品路径重新实读。v6未执行且原样保留。没有扩大工具白名单、猜参数或在生产代码按case ID分流。

专项离线账本实际共{s['measured_offline_adapter_reads']}次读取，含前两次行缺失失败共{s['failed_earlier_adapter_reads']}次、最终成功{s['successful_final_adapter_reads']}次，均无Provider，预留0、receipt释放。失败Ledger和已保存的缺行结果保留，第一轮仅有异常/账本的部分不补造。测试内虚构Run及SDK替身另列，不冒充候选试次。

最终5个完整受影响文件84 passed/0 failures/errors/skipped；先前81/83及测试配置错误是重叠迭代，不相加。完整CLI使用独立微型商品、订单、政策、拒绝及安全质量FAIL场景，调用真实应用/Repository/审计/预算/清理，SDK仅替身。少一资源、旧参数字段、缺第二行、错数量、缺结果审计及错对象都有负例。结果行Code PASS只证明来源返回，不证明用户答案逐行完整；答案完整性与Task仍须真实语义审核。

## 参考、分母与真实审核

当前草案原子事实：Frozen100为352项、Dev8为27项；主要Outcome60为{s['denominators']['reference_fact_items']['primary_outcome60']}项，检索67对应{s['denominators']['reference_fact_items']['retrieval67']}项，队列重叠，不能相加。67个查询都有真实必需政策来源；所有必需来源均有对应事实项。379是待审参考项，不是模型评分分母或379项正确结论。正式模型尝试/真实输出审核为0，质量指标N/A。

`reference-repair-audit.json`给逐题差异及26条恢复记录；`targeted-review.json`列实际改动的{s['targeted_reference_review']}题及关联源。未变AI意见继续保留原确认范围，不重复生成108套AI意见。真正独立初标尚无，因此`review/frozen100/day1-form.json`及Dev表仍108项空票；盲审另108项空票，均隐藏AI标签和前票。真实人员按§3.4独立填写，实际后续日期再盲审、比较和裁决；不得让AI在当前OS身份下把自己的票accept成人类独立决定。可由一人承担项目角色，不虚构必须多人。

```powershell
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v7/frozen100-workflow.json accept --submission <真实首日提交.json> --output <新回执.json>
# 真实后续日期操作；Dev改用dev8-workflow.json
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v7/frozen100-workflow.json blind-form --output <后续日盲审表.json>
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v7/frozen100-workflow.json compare --output <实际分歧.json>
# 重算已有材料，无模型、无新读取、无签发
.venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version r28
```

当前仍为披露AI/程序构建的合成候选；授权阅题不自动证明被评Agent污染，也不能洗掉构建/评测器修订暴露。56个app源码与R27相比不变，未用题目调Router/Agent提示；适配器和评测器已修正，必须用本轮新运行摘要绑定。真实独立未见资格未证实，CANDIDATE_DRAFT继续拒绝freeze，不将用户采纳改名Gold。

## 正式准备与真正剩余输入

容器治理r4、原PG marker `{s['actual_container_target']}`、网络和卷保留，r28镜像仍工程禁模型模式。未启用正式secret挂载、未换公钥、未签发或恢复许可。实际隔离观察及卷中v7字节另有核验。Provider/Judge/Embedding和真实应用试次均0，历史授权/消费不清零，033仍延期，旧受保护题和生产数据未访问，无Git/Windows账号或ACL修改。

`precise-execution-request.json`更新了v7两份源SHA、379项参考草案摘要、scoring v3/最终运行摘要、实际模型/节点/端点/目标/顺序、价格来源和批准的67题范围。拟Dev8各一次/64请求/96万token/USD4/4800秒；拟独立资格后的Frozen100各一次/800请求/1200万token/USD50/60000秒；单案8请求/12万token/USD0.5/600秒、只读及模拟owner动作计数≤12、总调用≤20、并发1/重试0。规划不等于本次执行许可，禁止动用USD54。拟窗口11月2日00:00至11月5日12:00UTC，若真实输入未齐就重绑申请，不改固定业务时间。

申请明确分两条身份路径：披露合成基准可以继续保留使用价值，但需要匹配其身份的精确许可，不能顶替独立未见正式Frozen；正式Frozen需先补实真实审核、资格、Gold/Registry。没有用工程许可改名放行，也不签发Gold为空的许可。

剩余真实输入集中为：①人员实际完成四类首日票、后续日期盲审和分歧裁决；②负责人对具体配置下的独立性/资产身份作有证据的处置，不能证明时仅用合成基准，必要时由隔离构建者提供真实留出集；③实际正式公钥指纹及获准secret供应、模型/价格/目标最终锁和精确执行许可。67题决定已落实，不再列为待确认。现有私钥仅工程用途，真实凭据不在聊天或仓库JSON中交付。

M6.1 COMPLETE。M6.2四项状态见唯一`exit-matrix.json`：Dev工程与历史评分保留；正式质量、独立参考/维度匹配校准、真实授权Frozen、用量与人工分歧及正式报告仍未完成。不以全案PASS为结束条件，不提前宣称M6.2完成。
'''
    path=out/'closeout-r28.md'
    if not path.exists():path.write_text(text,encoding='utf8')
    elif path.read_text(encoding='utf8')!=text:raise ValueError('preserve R28 report;input changed')
    marker='## 2026-10-31 M6.2 R28：五项修复与67题范围正式落实'
    progress=root/'docs/progress.md'
    if marker not in progress.read_text(encoding='utf8'):
        with progress.open('a',encoding='utf8') as f:f.write('\n\n'+marker+'\n\n- 已验证M62-R27-REVIEW-AND-RETRIEVAL67-USER-CONFIRMED及5份审核材料，范围变更100/67/60已批准，减少13个不适用检索项，不再待确认；仍不批准独立未见身份、真实模型调用或USD54执行。\n- 实际商品适配器参数改为product_id，经MeteredReads/评分/6案完整CLI不再误停；旧product参数未知记录不改。同步3个商品名，恢复9题26条未变源事实，长条款按权利/期限/起算/条件分项；第二订单行产品/数量及逐行来源核验补齐，006三条合法路径与路由参考一致，库存单独替代拒绝。\n- 定向实际读取发现048/050第二行未列fixture_refs，最终v7补合法本人订单关联；仅106项原input证据复用，2项重新核验，不把Repository目录存在当应用准入。全108直接字段70项无矛盾，Frozen352/Dev27草案事实，主要Outcome60事实'+str(s['denominators']['reference_fact_items']['primary_outcome60'])+'项；非正式分数。专项离线读取'+str(s['measured_offline_adapter_reads'])+'次含失败'+str(s['failed_earlier_adapter_reads'])+'与最终成功8次，原账本均保留、预留0、receipt释放。\n- 最终5个完整文件84 passed，81/83重叠迭代与测试设置错误保留；离线虚构应用/SDK与真实试次分列，真实Provider/Judge/Embedding/应用试次0。原容器/PG/网络/卷保留，r28工程镜像更新，marker不变，无正式secret挂载/签发/激活。\n- 最终数据data/eval/new-assets-r26/v7，统一报告'+R28_OUT.as_posix()+'；v3/v4/v5/v6和旧报告保留。新参考是真实审核待审草案，不把AI采纳或OS身份变成人类盲审。56个app文件未变，无题目驱动Router/Agent提示修改；仍缺真实审核/资格、Gold/Registry/公钥与精确许可、实际Frozen及最终报告。M6.1 COMPLETE/M6.2 IN_PROGRESS，无受保护旧内容、生产业务、Windows ACL或Git写操作。\n')

def finalize(root:Path):
    """Fixed existing finalizer entry: no models,no authorization issuance."""
    from dataclasses import asdict
    import shutil,xml.etree.ElementTree as ET
    from app.m0.config import load_settings
    from eval.m6_1.grants import formal_runtime_digest
    record=build(root);base=root/NEW;out=root/OUT
    previous=root/'data/eval/new-assets-r26/v4'
    if NEW.name=='v5' and not (base/'repository-validation.json').exists():
        bindings=[]
        for name in ['frozen100','dev8']:
            a=read(previous/'candidates'/(name+'.json'))['cases'];b=read(base/'candidates'/(name+'.json'))['cases']
            for old,new in zip(a,b):
                assert old['case_id']==new['case_id'] and old['input']==new['input']
                bindings.append({'case_id':new['case_id'],'input_sha256':sha256_json(new['input'])})
        for filename in ['repository-validation.json','repaired-source-verification.json']:
            original=read(previous/filename)
            put(base/filename,{**original,'reused_from':str(previous.relative_to(root)/filename),
                'reused_source_sha256':raw_hash(previous/filename),'unchanged_input_bindings':bindings,'new_reads_for_reference_only_revision':0})
    verified=verify_repaired_sources(root)
    audit=read(out/'actual-isolation-audit.json')
    assert audit['actual_application_runtime_sha256']==formal_runtime_digest()
    x=ET.parse(root/'docs/probes/m62-r27-metrics-final.xml').getroot()
    suites=[x] if x.tag=='testsuite' else list(x)
    tests={k:sum(int(s.attrib.get(k,0)) for s in suites) for k in ['tests','failures','errors','skipped']}
    assert tests=={'tests':78,'failures':0,'errors':0,'skipped':0}
    data={name:read(base/'candidates'/(name+'.json')) for name in ['frozen100','dev8']}
    cases={n:[EvalCase.from_dict(c) for c in d['cases']] for n,d in data.items()}
    settings=load_settings(require_api_key=False);now=datetime.now(timezone.utc)
    price_path=out/'public-price-observation.json'
    price=read(price_path) if price_path.exists() else {'observed_at_utc':now.isoformat(),'model':settings.model,'endpoint':settings.base_url,
        'official_source':'https://api-docs.deepseek.com/quick_start/pricing/','model_version_listed':'DeepSeek-V4.1-Flash-1017',
        'usd_per_million':{'cache_hit_peak':.006,'cache_miss_peak':.30,'output_peak':1.20},
        'local_reservation_usd_per_million_all_tokens':1.20,'no_off_peak_or_cache_discount_assumed':True,
        'status':'PUBLIC_PRICE_OBSERVATION_NOT_ACCOUNT_BILLING_ATTESTATION','revalidate_before_issue':True,'provider_probe_requests':0}
    put(price_path,price)
    # Keep registry metadata truthful. Payload split alone does not grant access.
    registry=deepcopy(read(root/OLD/'candidate-registry-metadata.json'))
    for asset in registry['assets']:
        name='frozen100' if asset['case_count']==100 else 'dev8'
        asset.update(asset_id='m62-r27-'+name+'-synthetic-'+NEW.name,version=VERSION,
            source_path=(NEW/'candidates'/(name+'.json')).as_posix(),sha256=record['source_hashes'][name])
    put(base/'candidate-registry-metadata.json',registry)
    snapshot=root/'docs/eval/m6.2-container-candidates-r26-20261031/source-final/app'
    app_paths=[p for p in snapshot.rglob('*.py')]
    app_diffs=[p.relative_to(snapshot).as_posix() for p in app_paths if raw_hash(p)!=raw_hash(root/'app'/p.relative_to(snapshot))]
    assert not app_diffs
    independence={'status':'INDEPENDENCE_NOT_ESTABLISHED_RETAIN_DISCLOSED_SYNTHETIC_BENCHMARK',
        'app_sources_compared_to_r26':len(app_paths),'app_changes':app_diffs,'actual_model_requests':0,
        'evaluated_application_prompt_tuning_from_these_questions':'NO_CHANGE_IN_THE_56_BOUND_APP_PYTHON_FILES',
        'evaluator_and_data_were_repaired_using_reviewed_candidates':True,
        'review_access_alone_is_not_proof_of_agent_contamination':True,
        'cannot_claim_unexposed_gold':'Construction/review exposure and common semantic families remain;real §3.4 review and provenance decision absent',
        'semantic_group_evidence':(NEW/'semantic-groups-and-exposure.json').as_posix(),
        'confirmation_event':record['confirmation_event'],'human_independent_initial':False,'later_date_blind_review':False}
    put(out/'independence-and-tuning-evidence.json',independence)
    meta=read(base/'case-metadata.json');groups=read(base/'semantic-groups-and-exposure.json')
    target=audit['negative_permit_probes']['target_identity']
    plan={'schema_version':'r27-precise-execution-request-v1','status':'BLOCKED_NOT_ISSUABLE_NOT_AN_EXECUTION_PERMISSION',
        'purpose':'Independent Dev8 then separately locked Frozen100,only after actual review/provenance/precise permission',
        'data_version':VERSION,'data_sha256':record['source_hashes'],'draft_references_sha256':sha256_json({n:[c.expected_outcome for c in cs] for n,cs in cases.items()}),
        'approved_Gold_sha256':None,'approved_registry_sha256':None,'formal_public_key_fingerprint':None,
        'implementation_sha256':formal_runtime_digest(),'dependency_lock_sha256':raw_hash(root/'requirements-formal-eval.lock'),
        'model':settings.model,'endpoint':settings.base_url,'node_configuration':{n:asdict(v) for n,v in settings.nodes.items()},
        'prompt_source_sha256':{p:raw_hash(root/p) for p in ['app/m1/routing.py','app/m1/chat.py']},
        'price_observation_sha256':raw_hash(price_path),'target':{'project':'shoprelay-m62-eval','profile':'M6.2-FORMAL-POSTGRES',
            'database':'shoprelay_m11_test','service':'m62-eval-postgres','identity':target,'host_port_published':False},
        'service_subjects':{'controller':{'principal':'evalcontrol','uid':10001},'application':{'principal':'evalapp','uid':10002},'issuer':{'principal':'evalissuer','uid':10003}},
        'trust':'Existing R26 key is engineering-only;separate real public key and exact approved plan required',
        'host_provider_key_configured':bool(settings.api_key),'key_transport':'Do not paste keys;approved administrator provisions external provider-secret/key and formal-issuer-key/private.key;no migration performed',
        'cohorts':meta['cohorts'],'retrieval_count':len(meta['cohorts']['retrieval']),'approved_original_retrieval_plan':80,
        'scope_change_request':'Approve67 genuinely required-policy queries or approve substantive task changes/additions for13;do not count empty qrels',
        'phases':[{"name":name,"N":len(cases[n]),'case_order':[c.case_id for c in cases[n]],'attempts_per_case':1,
            'provider_limit':8*len(cases[n]),'token_limit':120000*len(cases[n]),'usd_limit':limit,'execution_seconds_limit':600*len(cases[n]),
            'local_all_token_peak_estimate_usd':120000*len(cases[n])*1.20/1000000,
            'action_scope':[{'case_id':c.case_id,**formal_case_payload(c)['input']['owner_interaction']} for c in cases[n] if 'owner_interaction' in c.input]}
            for name,n,limit in [('INDEPENDENT_DEV8','dev8',4),('FROZEN100','frozen100',50)]],
        'per_case':{'provider':8,'tokens':120000,'usd':.5,'seconds':600,'counted_tool_calls':12,'all_calls':20,'retries':0,'concurrency':1},
        'requested_valid_window_utc':{'start':'2026-11-01T00:00:00+00:00','end':'2026-11-04T12:00:00+00:00'},
        'time_window_condition':'Not granted;review must actually finish before issue;recheck fixed policy/action clocks.If missed,revise/request a new bound window,not silently shift facts.',
        'stop':{'quality':'Record FAIL/PARTIAL then continue independent cases','critical':'Stop on permission/identity/unknown usage/audit/budget/cleanup failures',
            'public_repair':'Preserve segment,offline repair,new frozen successor shares counters;never resample consumed case'},
        'caps_not_extra_old_authorization':True,'judge_embedding_production_business':0,
        'missing_before_issue':['Real initial/subsequent-date review and adjudicated reference hash','Independent/unseen eligibility disposition or approved disclosed-benchmark scope',
            '67-vs80 retrieval scope decision','Real formal public key and approved secret mounting/activation','Final target/isolation/price/config lock and precise per-phase permission'],
        'formal_activation_now':False}
    put(out/'precise-execution-request.json',plan)
    correction={'confirmed_old_scope':108,'adoption_event':record['confirmation_event'],'new_source_changed':record['counts']['source_changed'],
        'targeted_review_cases':read(base/'targeted-re-review.json'),'cohort_counts':{k:len(v) for k,v in meta['cohorts'].items()},
        'retrieval_original_empty_or_missing':18,'new_operating_clauses_make_scorable':5,'remaining_gap_to80':80-len(meta['cohorts']['retrieval']),
        'retrieval_exclusions':meta['retrieval_exclusions'],'true_human_decisions_created':0,
        'reasoned_corrections':['PRODUCT-003 missing threshold permits clarification without invented value;no source rewrite required',
            'History repair proves returns only;three refund records remain absent and cannot be called refunds UNDER_REVIEW',
            'M4 return application eligibility is NOT_DETERMINED;seven-day source is not an implemented universal M4 rejection gate',
            '041/043 complete source05 allows SINGLE;MULTI remains legal alternative,not mandatory',
            'Dev8 source8 proposed get_refund_status explicitly in revised data scope;not an expansion of production permissions or retroactive old-run authorization'],
        'route_contract':'Stable typed routing dimensions,explanatory prose excluded only in new version;strict canonical F1 and accepted-path accuracy separate',
        'resource_contract':'Returned distinct resource arguments required;missing audit UNKNOWN,one/two-identical resource FAIL'}
    put(out/'review-disposition-and-denominators.json',correction)
    summary={'version':'R27','data':record,'tests':tests,'repository':{'cases':108,'reads':read(base/'repository-validation.json')['read_count'],
        'additional_repair_reads':verified['additional_reads'],'cleaned':108},'verified_histories':verified['actual_repository_history_replays'],
        'resource_coverage_cases':len(verified['actual_two_resource_reads']),'semantic_families':len(groups['groups']),
        'cross_split_families':groups['cross_split_families'],'shared_policy_clauses':groups['shared_policy_clause_count'],
        'runtime':formal_runtime_digest(),'deployed_engineering_identity':target,'isolation_audit':'actual-isolation-audit.json',
        'real_model_requests':0,'new_real_application_runs':0,'formal_grants_issued':0,'formal_activation':False,
        'M6.1':'COMPLETE','M6.2':'IN_PROGRESS','formal_evaluation':'NOT_EXECUTED','quality_gates':'NOT_ASSESSED'}
    put(out/'closeout-r27.json',summary)
    _handoff(root,summary,plan)
    # Current app sources and prior immutable snapshots are separate. Do not
    # assert mutable working files still equal their historical R26 manifest.
    final=out/'source-final'
    for p in (root/'data/eval/container-build/r26/code').rglob('*'):
        if p.is_file():
            dst=final/p.relative_to(root/'data/eval/container-build/r26/code');dst.parent.mkdir(parents=True,exist_ok=True)
            if not dst.exists():shutil.copyfile(p,dst)
            assert raw_hash(p)==raw_hash(dst)
    files={p for p in out.rglob('*') if p.is_file() and p.name not in {'SHA256SUMS.json','verification.json'}}
    files.update(p for p in base.rglob('*') if p.is_file() and 'validation-work' not in p.parts)
    files.update(root/p for p in ['docs/progress.md','eval/m6_2/candidate_revision_r27.py','eval/m6_2/candidate_construction_r26.py',
        'scripts/manage_m6_2_container.py','scripts/finalize_m6_2_r10_closeout.py','tests/test_m6_2_r27_references.py',
        'deploy/m62-eval/images-r27.yaml','deploy/m62-eval/formal-secret-mounts.proposed.yaml','deploy/m62-eval/issue-approved-formal.py',
        'docs/probes/m62-r27-iteration1.xml','docs/probes/m62-r27-iteration1.log','docs/probes/m62-r27-final.xml','docs/probes/m62-r27-final.log',
        'docs/probes/m62-r27-activation-final.xml','docs/probes/m62-r27-activation-final.log','docs/probes/m62-r27-metrics-final.xml','docs/probes/m62-r27-metrics-final.log'])
    files.update(root/row['path'] for row in bind_confirmation(root)['bound_review_artifacts'])
    files.add(root/REVIEW/'user-confirmation.json')
    manifest={'entries':[{'path':p.relative_to(root).as_posix(),'sha256':raw_hash(p),'description':'R27 source/new data/review/engineering metadata;no secret'} for p in sorted(files)]}
    path=out/'SHA256SUMS.json'
    if path.exists() and read(path)!=manifest:
        # Preserve the in-turn snapshot before publishing the final complete
        # evidence inventory. Historical R26 and previous-turn manifests stay.
        history=out/'SHA256SUMS-before-final.json'
        if not history.exists():write_new(history,read(path))
        temporary=out/'SHA256SUMS-next.json'
        write_new(temporary,manifest);temporary.replace(path)
    else:put(path,manifest)
    return summary

def _handoff(root,summary,plan):
    out=root/OUT
    text=f'''# R27 集中修正与正式执行申请

用户确认事件 `{summary['data']['confirmation_event']}` 已核验。原v3和审核意见保留；确认只采纳AI辅助旧源审核，不是真人独立初标/盲审或Gold。当前v4仍是来源披露的合成候选基准。

## 完成结果

- 108题保留，源数据改变22题；缺阈值题不编造阈值，保留正确澄清。连同2题等价路由，共25题定向复审；新参考全部重绑case/数据/规则摘要，旧确认不直接批准新字节。
- 修正商品/工单对象、第二订单行、缺失条款、商业渠道具体权利和4条完整售后事件链。真实Repository读到3条UNDER_REVIEW退货和1条APPROVED；退款空记录仍空，不把退货当退款。
- 共享评分v2不精确匹配自由文本intent/reason，仍校验稳定路由、安全、能力和澄清字段；旧版本保留旧口径。041/043有单条完整来源，SINGLE或合法MULTI均可，规范MacroF1与等价路径命中率分列。三个两商品题必须覆盖两个实际资源，不能仅匹配工具名。
- 实际队列为路由100、必需政策检索67、主要Outcome60，重叠不相加。原80中18题不应计，补齐5条真实缺失条款后恢复5题，仍差13。澄清/拒绝/纯业务题不会用空qrels自动通过。67不是已批准替换80的正式范围。
- 完整4个受影响文件78 passed。之前74项与78项重叠，不相加；首次CLI测试参数错误日志保留。108题实读590次＋修复专项10次，receipt均清理；无真实模型/应用试次。确认效果复用R26真实虚构API/PG证据，未改生产动作实现。
- R27镜像部署到原容器/PG/卷，身份marker `{summary['deployed_engineering_identity']}` 未变；真实隔离探针保留。工程模式仍禁止Provider。已补工程/仅准备/签名后正式激活三态：没有真实Gold/计划/签名/价格/secret不能激活，尚未启用正式信任。

## 真实审核入口

`data/eval/new-assets-r26/v4/review/frozen100/day1-form.json`及dev8对应表包含实际问题、完整必要上下文，labels/rationale为空，不展示AI建议；盲审表同样隐藏前票。`targeted-re-review.json`列25个源/契约定向项；`reference-and-source-diff.json`提供逐题源摘要、变更与建议身份。

真实人员独立填写四类参考；本人可以承担多个项目角色，但不能让AI填好后以OS身份调用accept伪称真人独立判断。已有AI材料暴露须如实披露。实际首日后，真实后续日期再生成并填写blind-form，accept后compare，分歧由真实人员裁决。AI本轮未调用accept、没有真人记录或日期。CANDIDATE_DRAFT仍拒绝freeze，须在独立性/审核处置完成后另建已验证后继登记，不直接重命名。

```powershell
# 原有唯一入口；Dev用dev8-workflow.json。输出必须用新路径。
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v4/frozen100-workflow.json accept --submission <实际首日提交.json> --output <首日回执.json>
# 真正后续日期操作，不能用虚拟时钟或提前代填
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v4/frozen100-workflow.json blind-form --output <后续日盲审表.json>
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/new-assets-r26/v4/frozen100-workflow.json compare --output <实际分歧.json>
# 本轮离线重算，不调用模型、不重新构建题目
.venv/Scripts/python.exe scripts/finalize_m6_2_r10_closeout.py --version r27
```

## 独立性与集中决定

56个生产app源码与R26相比无变化，未用候选调Router/Agent提示词，也未将题目发给真实模型。数据与评测器已利用审核材料修复，构建者/审阅者见过规则或题目；语义分组跨Dev/Frozen共享，不能称未见Gold。`semantic-groups-and-exposure.json`逐组列成员及共享语料，不能靠FC/DV前缀证明独立，也不凭阅题直接认定模型污染。

只剩以下具体真实输入，已采纳的108题AI审核不重复确认：

1. **范围决定**：推荐保留100题且如实采用67可评政策查询的披露合成基准，须明确变更原80规划；若坚持80，应批准13个有实质政策目标的新任务修改，重新绑定并复审，不能强迫现有澄清题检索。当前没有替用户选定。
2. **审核与资格**：真实人员完成四类初标、后续日期盲审和必要裁决；决定相对具体配置的独立性可否证明。证据不足时保留合成基准/Dev身份；需独立未见结论则另由隔离构建者提供真正留出资产，不全部丢弃当前数据，也不打开旧隔离家族。
3. **正式信任与许可**：宿主已有Provider配置，仅记录存在性，不迁移/打印密钥。受信管理员需批准把凭据放入`shoprelay-m62-formal-provider-secret`的key文件（UID10002只读）及独立`shoprelay-m62-formal-issuer-key`私钥（UID10003只读），登记实际正式公钥fingerprint与获准计划。工程测试公钥不转正，不创建Windows三账号、不改ACL或清空卷。

## 可直接审查的精确申请

`precise-execution-request.json`已填v4两份实际SHA、完整Dev8/Frozen100顺序、最终实现/依赖/提示源摘要、deepseek-flash及当前节点配置、端点、实际PG marker、动作参数和停止规则。规划Dev8各一次/64请求/96万token/USD4/4800秒；Frozen100各一次/800请求/1200万token/USD50/60000秒，串行、重试0。单案8请求/12万token/USD0.5/600秒，工具12次包含owner API/政策模块，总调用20。按官方当前peak价最保守全部token按输出价USD1.2/M预留，分别USD1.152和14.40，保留费用余量；实际签发再核价，不承诺供应商硬封顶。

申请暂不可签发：真实Gold/裁决摘要、独立性决定、67/80范围决定、正式公钥/秘密挂载和精确许可尚缺，均明确列出，没有空白Frozen许可。拟有效窗11月1日至11月4日12:00UTC不是授权；若真实审核未赶上，另绑新窗口及核实固定事实，不悄改时间。源码和配置若在Dev后改变，Frozen须绑定实际最终摘要，不把本草案自动作为许可。

真实输入齐备后，用`compose.yaml`＋`images-r27.yaml`＋**未应用**的`formal-secret-mounts.proposed.yaml`部署经批准的secret范围。先只准备模式(engineering_only=false,real_provider_allowed=false)核对身份和target；在只拿私钥的一次性issuer中，使用已绑定`issue-approved-formal.py --config data/eval/formal-trust/<批准配置>.json`调用现有正式CLI签发。实际公钥/Registry/Gold/plan必须匹配，旧工程授权不恢复。得到精确许可后才启用real_provider_allowed；启动会验证Gold/签名/计划/隔离/价格再读secret，代理才允许api.deepseek.com:443。控制端沿原正式CLI执行、report和review；没有新Runner。

## 阶段矩阵

| M6.2任务 | 已完成 | 剩余及完成标准 |
|---|---|---|
| 路由/检索/证据/Outcome | 历史Dev确认保留；v4数据/参考与评分缺陷修复，真实分母67 | 真实参考与批准范围、正式实际观测、按锁定配置算指标；不拿工程Stub成绩替代 |
| Judge/Human校准 | 历史有限Dev结果不重放；本轮准备真实标签 | 真实维度匹配参考、有限精确Judge许可及分歧；无分母N/A |
| 冻结审查/正式Frozen | 容器治理r4、PG与恢复、正式接线工程通过 | 独立性/人类审核/Gold/正式信任/精确权限后实际执行；033仍延期 |
| 指标/报告 | 版本/队列/来源/预算与重算入口齐备 | 正式用量、真实负结果、人工分歧、区间及最终报告；完成评测和质量达标分别判断 |

M6.1 COMPLETE，M6.2 IN_PROGRESS。没有真实Agent/Provider/Judge/Embedding请求、没有正式许可或Gold审批。旧受保护内容、生产数据与Windows权限未访问或修改，无Git写操作。工程环境继续保留。
'''
    text=text.replace('v4',NEW.name)
    text+='\n最终v5还同步改写8项商业条款的必需证据要求，防止源权利变更而旧要求残留。v4中间草案未执行且保留；v5与v4的108个input哈希逐一相等，故实读与receipt证据绑定复用，未重复进行600次读取。一次构建配置路径错误在写入既有intake前被排他写入拒绝，旧记录未覆盖，错误配置另存invalid-preflight，随后恢复成功。\n'
    p=out/'closeout-r27.md'
    if not p.exists():p.write_text(text,encoding='utf8')
    elif p.read_text(encoding='utf8')!=text:raise ValueError('handoff changed')
    marker='## 2026-10-31 M6.2 R27：用户采纳后集中修订、真实检索分母与正式准备'
    progress=root/'docs/progress.md'
    if marker not in progress.read_text(encoding='utf8'):
        with progress.open('a',encoding='utf8') as f:f.write('\n\n'+marker+'\n\n- 已验证M62-R26-AI-REVIEW-USER-ADOPTED-20261031的7份材料及v3两份源；采纳是AI辅助用户审核，不是真人独立初标/盲审。v3与旧原档保留，后继v4仍100+8。22题源数据修正，1题缺阈值保留合法澄清，2题允许SINGLE/MULTI等价路径，25题定向复审；没有把旧SHA标签直接批准到新源。\n- 真实队列路由100/检索67/主要Outcome60；80中18个空必需政策题剔出检索分母，补实5条条款后可评67，剩13差额明确待范围决定，不凑80或空集合通过。四条售后源链实际Repository验证3审核中/1批准，退款仍无记录；3题分别读到2个商品。全108题590次实读＋专项10次，全部receipt清理；无真实模型或案例试次。\n- 共享评分v2排除reason/intent自由文案精确匹配，保留稳定路由/能力/安全边界；记录真实工具参数，两个资源需各自成功，缺审计UNKNOWN；等价路径命中率与规范MacroF1分列。仅正式准备/签名后真实激活与工程模式分开，未激活真实模型。最终4个完整受影响文件78 passed，先前74/78为重叠迭代不相加；首次CLI测试参数错误保留。\n- R27容器仍用原PG/网络/卷，marker '+summary['deployed_engineering_identity']+'，工程禁模型模式保留，实际隔离/7892桥接核验通过。56个app源码与R26无变动，未以候选调Router/Agent提示，但已暴露构建/审核及跨split语义家族，独立未见资格未证实；保留披露合成基准身份。真实初标与后续日期盲审未伪造。\n- 实际数据、未填审核表/25项定向复审在data/eval/new-assets-r26/v4；唯一收口及不可签发的精确申请在'+OUT.as_posix()+'。已查官方deepseek-flash当前peak价，真实key仅核存在性未迁移；正式公钥/秘密挂载、Gold、范围67/80与独立性及最终精确许可尚缺。M6.1 COMPLETE/M6.2 IN_PROGRESS；正式评测未执行、质量门槛未判断，无旧受保护内容或Git写操作。\n')
    amendment='## 2026-10-31 M6.2 R27最终补记：v5参考权利重绑'
    if NEW.name=='v5' and amendment not in progress.read_text(encoding='utf8'):
        with progress.open('a',encoding='utf8') as f:f.write('\n\n'+amendment+'\n\n- 最终交付改为data/eval/new-assets-r26/v5及'+OUT.as_posix()+'；v3/v4及原报告不覆盖。8项商业条款变更权利后同步修正必需证据文本，旧参考不能只换源SHA。两版108个input逐项相等，590+10次实读证据按输入哈希复用，不重复计数。仍为100/67/60队列、25项定向复审、正式Gold/模型激活0。一次本地配置路径错误在旧intake写入前安全拒绝，旧记录字节未改；错误配置保留并修复恢复。\n')
