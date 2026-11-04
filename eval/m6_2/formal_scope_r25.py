"""R25 concrete scope/engineering/handoff release on the existing report entry."""
from __future__ import annotations

import hashlib,json,shutil
from datetime import datetime,timezone
from pathlib import Path
import xml.etree.ElementTree as ET

from eval.m6_1.grants import formal_runtime_digest

OUT=Path("docs/eval/m6.2-formal-scope-and-deployment-r25-20261031")
R24=Path("docs/eval/m6.2-formal-execution-readiness-r24-20261031")


def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()


def save(root,name,value):
    path=root/OUT/name;path.parent.mkdir(parents=True,exist_ok=True)
    text=value.rstrip()+"\n" if isinstance(value,str) else json.dumps(value,ensure_ascii=False,indent=2)+"\n"
    if path.exists() and path.read_text(encoding="utf8")!=text:raise ValueError("preserve existing release: "+name)
    if not path.exists():path.write_text(text,encoding="utf8",newline="\n")


def build(root):
    root=Path(root).resolve(strict=True)
    machine=json.loads((root/OUT/"machine-observation-r25.json").read_text(encoding="utf8"))
    previous=json.loads((root/R24/"formal-plan-r24.json").read_text(encoding="utf8"))
    source=json.loads((root/"docs/probes/m62-r25-accepted-source-lock.json").read_text(encoding="utf8"))
    if formal_runtime_digest()!=source["formal_runtime_sha256"] or any(digest(root/p)!=h for p,h in source["files"].items()):raise ValueError("final source drift")
    xml=root/"docs/probes/m62-r25-accepted-final.xml"
    suites=ET.parse(xml).getroot().findall("testsuite")
    tests={k:sum(int(s.attrib[k]) for s in suites) for k in ["tests","failures","errors","skipped"]}
    if tests["failures"] or tests["errors"]:raise ValueError("offline acceptance failed")
    props={p.attrib["name"]:p.attrib["value"] for p in ET.parse(xml).getroot().iter("property")}
    proof_path=root/props["r25_proof_root"]/"r25-proof.json"
    proof=json.loads(proof_path.read_text(encoding="utf8"))
    if proof["result"]["stop_reason"] is not None or proof["result"]["budget"]["active_reservations"]:raise ValueError("fictional end-to-end proof incomplete")
    event_path=root/OUT/"scope-and-engineering-r25.json"
    at=json.loads(event_path.read_text(encoding="utf8"))["recorded_at_utc"] if event_path.exists() else datetime.now(timezone.utc).isoformat()
    evidence={"schema_version":"r25-scope-and-engineering-v1","recorded_at_utc":at,
        "planning_adoption":{"event_id":"M62-R25-SCOPE-PLANNING-CHAT-USER-20261031-R1","actor":"CHAT_USER_SELF_CONFIRMED","method":"Current user explicitly adopted R24 construction scope and planning budget only",
            "source_plan":R24.as_posix()+"/formal-plan-r24.json","source_plan_sha256":digest(root/R24/"formal-plan-r24.json"),
            "frozen_unique_questions":100,"independent_dev_questions":8,"overlapping_cohorts":{"route":100,"retrieval":80,"primary_outcome":60},
            "not_granted":["real content access","Gold approval","execution permission","old27 allowance extension"]},
        "coverage_design":{"primary_outcome":{"product":8,"order":8,"after_sales":12,"policy":18,"cross_domain":8,"safety":6},
            "additional_policy_QA":40,"route_design_quotas":{"DIRECT":10,"SINGLE":40,"MULTI":20,"AGENT":23,"REFUSE":7},
            "route_note":"Design quotas,not forced runtime routes. Actual legal references are independently reviewed and locked before execution;any pre-freeze design adjustment is recorded.",
            "retrieval_cohort":"40 extra QA+12 after-sales+18 policy+8 cross-domain+2 safety=80;within100,not180",
            "compound_policy_minimum":25,"compound_design":"At least8 cross-domain+6 primary policy+11 extra QA;annotate actual condition sets,do not infer compound solely from MULTI label",
            "after_sales_12":{"return_pending":1,"return_confirmed_submission":1,"refund_pending":1,"refund_confirmed_submission":1,
                "unpaid_cancel_confirmed":1,"paid_cancel_with_refund_application":1,"human_escalation_draft":1,"ticket_confirmed":1,
                "missing_facts_unknown":1,"reject_or_expire":1,"cross_owner_denial":1,"status_snapshot":1},
            "safety_6":["cross-user access","confirmation hash tampering","expired action","duplicate/concurrent confirmation","untrusted-policy injection","credential boundary"],
            "all_15_tools":{"read":["get_order","get_order_items","track_shipment","get_refund_status","search_products","get_product_detail","recommend_products","check_inventory","search_knowledge","get_coupon","get_warranty"],
                "owner_confirmed_simulation":["create_return_request","create_refund_request","cancel_order","escalate_to_human"]},
            "Dev8_design":["DIRECT clarification","REFUSE unauthorized resource","SINGLE policy","MULTI conflicting/conditional policy","product facts","owned-order facts","pending action with zero business effect","owner-confirmed simulated action"],
            "033":"DEFERRED;electrical conflict expert capability excluded and disclosed;39/40 historical Dev unchanged",
            "unverified_capabilities":["LLM autonomous drafting/action choice has no new real evidence in R25","Live mid-node continuation after arbitrary process death is not claimed;unknown execution is retained without tool replay","M6.3 fair Single/Multi and M6.4 broad fault/security suites remain separately scoped"]},
        "requirement_mapping":[
            {"basis":"§5.1,§5.4,§13","capability":"pending draft;confirmation;refund;return;cancellation;human ticket","planned_scope":"after_sales12 plus safety6 within Outcome60","engineering":"Actual authenticated owner API and M4 transactions,not a readonly claim;model autonomy not established","formal_remaining":"New independently reviewed questions/actual model output and owner follow-up observations"},
            {"basis":"§6 process control,M6.2 task1","capability":"DIRECT,SINGLE,MULTI,AGENT,REFUSE;clarification;retrieval;handoff","planned_scope":"route100;retrieval80;cross-domain8","engineering":"Existing application routes and real two-node order-to-policy path;allowed zero-call clarification","formal_remaining":"Exact legal route/reference sets and actual frozen predictions;never force a route quota"},
            {"basis":"§5.1,§10.2","capability":"Run/Trace/Checkpoint/SSE association and recovery","planned_scope":"all trials;Dev8 reconnect and replay canary","engineering":"New PostgreSQL durable saver and real SSE app rebuild with same Run/cursor;terminal settlement recovery without replay","formal_remaining":"Real approved target and identities;arbitrary mid-node crash continuation remains unverified and is not silently claimed"},
            {"basis":"§3.4,§10.1,M6.2 tasks2–4","capability":"independent references;quality denominators;Judge disagreements;formal authorization","planned_scope":"four separate reference types,100/80/60 cohorts;separate Dev8","engineering":"Existing intake/review/registry/grant/report paths reused;four closed Dev Judge samples retained","formal_remaining":"Actual new assets,real initial/later-day reviews,live isolation/trust,precise permit,actual formal results and final report"}],
        "engineering":{"formal_runtime_sha256":formal_runtime_digest(),"digest_version":"formal-execution-runtime-v3",
            "changes":["Existing factory selects exact marked PostgreSQL target;refuses relabel of existing populated DB","Persistent Postgres saver on actual app path;formal Run uses API-compatible idempotency scope for SSE reconnection",
                "Explicit owner-followup input is separate from Router/Agent/Gold;real authenticated/CSRF-protected M4 API,server grants,hash confirmation,transaction and audit",
                "Actual source policy_id/clause_id preserved through fixture retrieval;no source metadata reconstructed from expected answer",
                "Nested action and policy reservations use conservative per-case calls including held reservations","Exact action rows and exact checkpoint thread teardown;raw results and audit precede cleanup",
                "Existing reporting_cohorts accepted and scope-checked at preregistration before any claim;exact100/80/60 overlap remains locked",
                "Optional separately budgeted full-query BM25 module with raw ranks;never represented as model input or as a historical global multihop rank",
                "Bounded artifact filenames derived from full Trial IDs leave Windows atomic-staging headroom;complete IDs stay in original records"],
            "tests":tests,"JUnit":xml.relative_to(root).as_posix(),"final_complete_files":3,"prior_complete_three_files":{"tests":60,"JUnit":"docs/probes/m62-r25-acceptance-final.xml","overlap_do_not_add":True,"later_change":"Raw action assessment,approved-return mapping,permitted zero-call clarification,and explicitly separate full-query retrieval module"},"prior_full_affected_regression":{"files":12,"tests":140,"JUnit":"docs/probes/m62-r25-final.xml","overlap_do_not_add":True,"reason_for_final_update":"Affected formal files rerun after bounded action/cleanup/scoring corrections;untouched business suites not mechanically repeated"},"proof_path":proof_path.relative_to(root).as_posix(),"proof_sha256":digest(proof_path),
            "fictional_application_trials":proof["fictional_cases"],"action_results":proof["action_rows"],"persistent_reopens":proof["persistent_reopens"],
            "model_requests":0,"real_Dev_or_Frozen_Runs":0,"substitute_SDK_requests":proof["substitute_requests"],
            "temporary_server_cleanup":json.loads((root/"docs/probes/m62-r25-acceptance-cleanup.json").read_text(encoding="utf8")),
            "interpretation":"Engineering evidence in a newly created fictional database;not model autonomy,real independent labels,or Frozen results"},
        "real_asset_state":{"eligible_real_assets":0,"required_replacement_family":"policy-candidate-family","registered_count":60,"per_question_ids":"UNKNOWN_FROM_PERMITTED_METADATA",
            "decision":"M6.1-A-OWNER-DISPOSITION-20261024-01","new_asset_count":108,"not_168":True,
            "protected_content_read_searched_copied_hashed":False,"current_debug_context_assets_can_be_unseen_Gold":False},
        "accepted_budget_plan":previous["proposed_budget_not_granted"],"machine":machine,
        "existing_hard_gate_sources":json.loads((root/OUT/"existing-hard-gate-evidence-r25.json").read_text(encoding="utf8")),
        "exit_tasks":[
            {"task":1,"done":"39 Dev confirmations and actual shared grading;formal action/persistent target engineering verified","remaining":"Real independent route/required-evidence/atomic-fact/Task references and formal observations","executor":"steward/reviewer/authorized executor"},
            {"task":2,"done":"Closed limited four-object Dev Judge calibration retained;not repeated","remaining":"New independent dimension labels only for wider calibration claims;actual discrepancies retained","executor":"real reviewer;separate exact Judge approval only if an additional study is chosen"},
            {"task":3,"done":"Existing formal CLI now supports bounded simulated actions and persistent PostgreSQL/SSE","remaining":"New108 actual assets,real initial/later-date reviews,isolated roles/ACL/audit/trust,target and fresh price;exact content and execution permit;actual Frozen100","executor":"owner/steward/reviewer/environment administrator/authorized executor"},
            {"task":4,"done":"Fixed raw-ledger scoring/recompute/report and failure separation","remaining":"Actual formal n/N,intervals,costs,Human disputes/failures and final exit decision","executor":"executor/reviewer/project owner"}],
        "stage":{"M6.1":"COMPLETE","M6.2":"IN_PROGRESS","formal_evaluation_completed":False,"quality_targets_passed":None,"M6.2_exit_satisfied":False}}
    save(root,"scope-and-engineering-r25.json",evidence)
    save(root,"single-handoff-r25.md",handoff(evidence,machine))
    save(root,"deployment-delta-r25.json",{
        "status":"IMPLEMENTED_INTERFACE_WITH_REAL_INPUTS_REQUIRED_NO_PERMIT","base_template":R24.as_posix()+"/deployment-inputs-r24.json",
        "actual_factory":"eval/m6_1/formal_application.py:factory","formal_runtime_sha256":formal_runtime_digest(),
        "use_profile":"M6.2-FORMAL-POSTGRES","application_postgres":{"url_env":"SHOPRELAY_FORMAL_DATABASE_URL","host":"127.0.0.1","port_proposal":55432,
            "database":"shoprelay_m11_test","identity_manifest":"data/eval/formal-targets/frozen-pg-identity.json","checkpoint_schema":"shoprelay_langgraph"},
        "simulated_action_tools":["create_return_request","create_refund_request","cancel_order","escalate_to_human"],
        "call_budget_interpretation":"R24 planning cap12/case includes all counted reads,internal action policy reads,owner API steps and optional one module query;derive tighter individual caps from actual question structure before issue;do not hide or add a second allowance",
        "asset_envelope":"formal-owner-action-assets-v2","credentials":"environment only;never a JSON DSN",
        "retrieval_scoring_object":{"default":"APPLICATION_TRACE_ONLY","optional":"FORMAL_FULL_QUERY_BM25","selection":"Declare before precise permission;module full-user-query ranking is separately metered,not injected into model or described as historical app-hop ranking;adaptive and owner-API retrieval evidence remains separate"},
        "remove_stale_binding":["SQLite application_database branch","M6.2-FORMAL-ISOLATED target profile","R24 runtime digest"],
        "freeze_from_actual_inputs":["actual host/port/DB/environment-variable name and marker identity","new registry asset/version/hash and exact case/cohort/order lists",
            "final model/nodes/prompts/dependencies;fresh price and actual billing profile","factory,asset lock,price and target-identity-manifest runtime bindings","real issuer/public key/executor/approved plan hash/expiry"],
        "not_changed":["real ACL/trust","production switches","old grants and consumed budgets"]})
    progress=root/"docs/progress.md";marker="## 2026-10-31 M6.2 R25：动作与持久恢复接线、已采纳范围及集中交接"
    if marker not in progress.read_text(encoding="utf8"):
        with progress.open("a",encoding="utf8") as f:f.write(f"\n\n{marker}\n\n- 用户已采纳新Frozen100+独立Dev8及重叠路由100/检索80/主要Outcome60建设规划；原预算为规划，不是空白执行许可，旧27次关闭授权不恢复。033延期，旧39案/Dev标签/Judge不重跑或重复审核。\n- 现有正式工厂新增精确标记的PostgreSQL模拟目标、持久checkpointer、API兼容Run幂等与SSE重连；新增显式owner-followup受控输入，经真实认证/CSRF、Pending Action、确认哈希和M4事务执行退款/退货/取消/转人工。Router/Agent不接收Gold，不把脚本用户确认当LLM自主成功。\n- 实际隔离工程验收{tests['tests']} passed；虚构{proof['fictional_cases']}个Trial、SDK替身{proof['substitute_requests']}次、真实Provider/Judge/Embedding及真实Dev/Frozen试次均0。验证确认前零业务效果、确认后精确一次、重开连接重复确认不增写、owner拒绝、精确receipt/Checkpoint清理、应用重建SSE游标回放。失败迭代保留，原有业务容器/卷未操作。\n- 修复政策适配器丢失显式policy_id/clause_id，以及嵌套请求的逐案预算漏计HELD预留；当前正式运行摘要{formal_runtime_digest()}。历史报告/源码快照保留。\n- 全部能力配额、动作12项分布、实际账号/SID、目录现状及A/B/C/D集中处理见本包single-handoff-r25.md。当前合格正式资产0，旧政策家族登记60按既有决定替换，逐题ID未知不杜撰；新100+8不直接当作已存在/已审核资产。\n- M6.1 COMPLETE，M6.2 IN_PROGRESS；真实资产/独立首日及后续日期盲审、真实角色/ACL审计与信任部署、有效价格和精确内容/执行许可尚缺。正式评测未执行、质量门槛未判断。中间节点未知执行不重放，任意进程崩溃后的无损续跑不冒称已验收。无受保护内容访问、无真实系统权限或信任修改、无Git写操作。\n")
    paths=set(source["files"])|{"eval/m6_2/formal_scope_r25.py","scripts/finalize_m6_2_r10_closeout.py","docs/progress.md","AGENTS.md","ShopRelay_Agent_Eval_完整评测方案.md","项目规格/evaluation-spec.md"}
    paths.update(item["path"] for item in evidence["existing_hard_gate_sources"])
    for p in set(source["files"])|{"eval/m6_2/formal_scope_r25.py","scripts/finalize_m6_2_r10_closeout.py"}:
        target=root/OUT/"source-final"/p;target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():shutil.copyfile(root/p,target)
        elif digest(target)!=digest(root/p):raise ValueError("source snapshot drift")
    paths.update(p.relative_to(root).as_posix() for p in (root/OUT).rglob("*") if p.is_file() and p.name not in {"SHA256SUMS.json","recompute-verification-r25.json"})
    paths.update(p.relative_to(root).as_posix() for p in (root/"docs/probes").glob("m62-r25-*") if p.is_file())
    paths.update("data/eval/offline-tests/r25-postgres-isolated/"+name for name in ("iteration2-fictional-db.dump","iteration-frozen-write-path.dump"))
    proof_root=proof_path.parent
    paths.update(p.relative_to(root).as_posix() for p in proof_root.rglob("*") if p.is_file() and p.suffix in {".json",".jsonl",".sqlite3"})
    save(root,"SHA256SUMS.json",{"entries":[{"path":p,"sha256":digest(root/p),"description":"R25 explicit source,public metadata,or isolated fictional proof"} for p in sorted(paths)],
        "progress_snapshot":"source-before/docs/progress.md preserves R24;current progress only appended;old manifests not overwritten","protected_content_included":False})
    return {"tests":tests,"runtime":formal_runtime_digest(),"package":OUT.as_posix(),"formal_evaluation_completed":False}


def handoff(e,machine):
    return f'''# R25 唯一集中处理清单与继续入口

已完成：新100+独立Dev8规划采纳，三队列在同一100内重叠；动作与PostgreSQL持久恢复实际接线、隔离工程验收。没有新的真实Dev/Frozen、Provider、Judge或Embedding。已有39案及已确认标签/四条Judge保持原范围。正式评测未完成、质量门槛尚无正式结果、M6.2未退出。

测试证据：完整受影响12文件140 passed；其后关键3文件60 passed；最终有界修正后同3个完整文件{e['engineering']['tests']['tests']} passed。各批重叠不相加。最终PG实测为{e['engineering']['fictional_application_trials']}个虚构Trial，含真实双节点交接、7种owner动作场景及允许能力存在但正确零调用的缺参澄清，模型仅SDK替身、外部请求0。受控候选、用户交付、动作状态、实际业务效果和Task结论分别保存；盲审核材料保留用户API输出/实际事实，隐藏Code判定。动作判定从实际行/事件重算，不复制生产端PASS。

## A. 所有者：只补精确范围与真实权限，不重复已采纳规划

| 已确定 | 真正缺少 | 接收入口及完成标准 |
|---|---|---|
| Frozen100、Dev8；路由100/检索80/主要Outcome60重叠；033延期。主要Outcome仍为商品8/订单8/售后12/政策18/跨域8/安全6。 | 真实题面及审核完成后的精确case/group/cohort/顺序；逐案合法工具与owner动作脚本；正式动作许可仅模拟，不开生产写。 | 同一asset工作流导入并锁定；审批引用绑定实际asset/cases/config/目标/身份/预算/有效期/hash。未齐不签发空白grant。 |
| Frozen上限100 Run/800 Provider/1200万tokens/USD50；Dev8另8/64/96万/USD4。每案一次、并发1、重试0；不是新获执行许可。 | 按最终调用结构和当前计费价格确认具体逐案分配及截止时间；内容访问许可与执行许可分开。 | 正式CLI签发前验证当前价格、逐案合计、配置摘要与实际主体；旧27次消费不清零、不复用。 |
| 提交、批准、支付到账分开；退款和转人工仅提交待审核/待受理。 | 实际题面/rubric须区分“应用合法路径”和“模型主动规划”；不能把显式owner后续API步骤冒充自然语言Agent自主生成Pending。此为构建/审核字段，不新增一轮规划审批。 | 在既定60题内保留适用目标并测真实正负结果；模型自主能力本轮没有新增实测，不删强制项。 |

售后12题具体配额：退货待确认1、退货确认提交1、退款待确认1、退款确认提交1、未付取消1、已付取消关联退款1、人工草案1、确认工单1、缺事实1、拒绝/到期1、跨用户拒绝1、状态快照1。安全6覆盖越权、哈希篡改、过期、重复/并发、注入、凭据。15工具=11只读+4受确认模拟动作；工具调用、权限、效果与回答质量分开。正式初始路线设计配额DIRECT10/SINGLE40/MULTI20/AGENT23/REFUSE7，须按实际合法题面独立核定，不强制模型路由凑数。

检索80=额外QA40+售后12+政策18+跨域8+安全政策2；至少25个实际复合条件问题（建议8跨域+6政策+11额外QA）。额外QA40按退货退款12/保修8/券6/履约6/条件冲突8。主要Outcome60之外的40题仍保存交付、失败及用量，不混入预锁主Outcome分母。Dev8分别覆盖DIRECT澄清、REFUSE、SINGLE、MULTI、商品、本人订单、待确认零效果、确认模拟动作。

## B. 数据构建者：交付108个真实新问题及配套资产

已有公开依据：旧`policy-candidate-family`登记60、按`M6.1-A-OWNER-DISPOSITION-20261024-01`隔离替换，逐题ID/暴露子集在获准元数据中未知。该60是旧家族数量，不是另加60题；新规划总计108。当前合格真实正式资产0。不得读旧受保护题改名、不得将当前AI/调试者已见题声明为未见Gold。

- 路径：`D:/ShopRelay/data/eval/formal-assets/first-frozen-v1/intake-source.json`；独立Dev用`formal-dev8-v1/intake-source.json`。本轮未创建真实题目或授予内容读取。
- 包：`schema_version=formal-owner-action-assets-v2,version,split,cases`。每题现有EvalCase字段为schema_version（1.0）、case_id、group_id、split、input、expected_outcome、rubric_refs、fixture_ref、risk_level、data_version；真实稳定ID/group/来源作者方法/暴露记录/fixture/rubric不得由本报告伪造。独立场景/政策组合/模板分组，Dev与Frozen不近重复。
- runtime input沿用R24七字段；动作题另有`owner_interaction={{tool_id,target_resource_id,arguments,decision}}`。decision仅LEAVE_PENDING/CONFIRM/REJECT，金额、policy快照、owner、action_id、确认hash不从expected补造。用户明确给出的后续动作与模型策略分开。所有Router/Agent仍不接收Gold。
- 动作Fixture必须提供订单owner/记录状态/paid_at/paid_cents/delivered_at（未知明确null）、商品真实price_cents、订单行数量/金额、库存available/reserved/version、订单行独占reservation（id/quantity/status/source_ref/created_at）。缺取消预留不得凭库存总量推断。现有服务器模拟动作授权主体为u_demo；工单也允许u_other。owner_map需按已有真实服务契约映射，不把这些业务用户当OS身份。
- 已批准退货后退款的Fixture还需显式returns/refunds及关联events：稳定记录ID、真实状态、submitted_at/approved_at、source_type/source_ref及RETURN_APPROVED等实际事件。仅用户声称批准不生成批准事实；本轮已验证规范化映射到真实M4 API消费的表。
- 政策须有evidence_id、policy_id、clause_id、text、文档version、有效起止、知识库版本和来源。已修复显式policy_id/clause_id丢失；所有本轮分配语料都加载，不按Gold正例筛选。退款现有合同需要refund/before-shipping和refund/uniqueness、取消需要cancel/eligible、退货需要return-standard条款；缺少这些业务政策映射时是真实契约缺口，不能私改为通过。日期粒度按结束日次日UTC零点排除，其他时区/时刻不得截断套用。
- 四套参考分别提供：完整RouteDecision；逐查询必需证据集合/条件（不是Dev候选相关性）；原子事实主体/关系/值/条件/source_ids；Task必需条件/合法工具替代组/零调用/预算。模拟结果ID可运行后生成，参考不得编造动态ID；用对象类别、状态、金额和效果数量表达条件。
- 必须有真实内容导入permit：路径、SHA、范围、有效期、批准人及真实steward角色；附作者/独立性/暴露来源证明。首次接收器核对字节/数量/来源/参数和scope，错误即拒绝。当前调试上下文产物只能作已暴露工程样本，不能自批正式Gold。

## C. 审核者：真实首日独立标注及后续日期盲审

按§3.4，一人可以承担项目所有者、构建、审核等角色；独立初标和隔日盲审须真实发生，不能由AI、用户概括性采纳或虚拟时钟替代。不虚构必须多人。033如未来加入仍需该案真实电气安全及业务/政策专案审核，本轮继续排除。

复用`--asset-workflow-config ... day1-form/accept/blind-form/accept/compare/freeze`，表中提供完整问题、业务/政策上下文和评分目标，隐藏旧标签/AI建议/首日票。真实OS角色和接收时间由程序记录；同日盲审拒绝。审核route、必需证据、原子事实和Task四维度，分歧逐项给真实裁决理由后锁定Registry。没有参考的维度不伪造分母。首日/盲审/裁决文件、来源和原票均保存。

结果产生后用既有`--report`生成每Run输出审核表；候选、交付、脚本owner API结果分列。再`--receive-review --review-phase day1/blind`及`--adjudicate-review`，不把候选合理覆盖交付失败，不把Task失败当作事实幻觉。接收真审核后`--report`只离线重算，不新增请求。

## D. 环境审批：已填机器事实、确切操作与回退

当前实际普通开发身份：`{machine['authenticated_subject']}`，SID `{machine['ordinary_user_sid']}`；只读查看的父目录为`D:/ShopRelay/data/eval`，其DACL仍继承，具有普通用户继承访问；正式assets/config/ledgers/targets四目录实际均未创建。没有真实issuer、executor、reviewer映射或公钥已部署的证据。不得直接把当前调试身份写进正式信任列表。

本轮新建的随机端口Docker/PostgreSQL只用于虚构测试，原`shoprelay-m0-postgres-1`/Qdrant及旧卷未改。它不自动成为正式环境。建议新建专用正式PostgreSQL（复用已缓存postgres16.12镜像、独立持久卷、仅127.0.0.1:55432，先确认端口未占用），库名shoprelay_m11_test、schema shoprelay_langgraph；凭据只由执行进程环境提供。新目标初始化拒绝已有表的数据库；不能把旧test库或生产库重新标记。需要环境负责人批准实际容器/卷/端口、服务身份、数据目录、口令保管和维护权限。

具体审批操作按以下顺序执行，本轮没有执行它们：

1. 提供并验证真实steward/reviewer/issuer/executor账号及SID、普通developer/Web/Runner的SID。本机已只读核对建议名ShopRelayData、ShopRelayIssuer、ShopRelayExecutor，三者均不存在；建议由获准管理员新建这三个最小分离账号（Data承担构建与审核），或指定实际已有合格账号替代。创建账号/安全口令/登录权尚未批准或执行；口令只在本机安全录入，不进聊天/仓库。可由同一人使用分离OS角色账号；普通身份与受保护读取身份不能是同一SID再靠相互冲突的DENY/ALLOW补救。记录管理员/Docker管理员的可信边界，不能声称目录ACL可以约束拥有宿主机管理员权限的人。
2. 确定新目录：`D:/ShopRelay/data/eval/formal-assets/first-frozen-v1`、`formal-assets/formal-dev8-v1`、`formal-config/first-frozen-v1`、`formal-ledgers/first-frozen-v1`、`formal-targets`。变更前对精确目录及父目录保存Get-Acl的SDDL/owner/继承和auditpol备份；已存在非空内容时先核准，不能递归重置全仓ACL。
3. 由获准管理员创建空目录，关闭这几个目录的DACL继承并改为允许清单（不要对Users组盲目DENY而同时阻止合法角色）。SYSTEM与可信Administrators FullControl；assets由steward修改、reviewer读取及写各自提交子目录、executor只读；config由issuer修改、executor只读；ledger/目标只允许executor修改及issuer审计读。目录owner设可信Administrators/批准主体，普通开发用户不保留隐式改DACL能力。备份后才Set-Acl；不得改整个D:/ShopRelay或系统全局信任。
4. 审计审批单独列项：查询`auditpol /list /subcategory:* /v`确认File System子类别GUID，保存`auditpol /backup /file:<精确备份路径>`。获准后只开启File System成功/失败审计（GUID 0CCE921D-69AE-11D9-BED3-505054503030），对上述受保护目录设置ReadData/WriteData及失败访问SACL。若无SeSecurityPrivilege，停止该操作并由获准管理员执行，不改ACL绕过。
5. 仅放入明确虚构的`acl-canary.txt`。分别在普通、steward、executor真实账号下读取/写入；记录实际SID、时间、AccessDenied或成功及对应4663等审计。普通身份必须拒读题目目录和私钥位置；executor读资产写自身ledger，不能读取签发私钥。canary通过才接收真实内容。
6. 回退：先停止这两个新正式配置的执行入口，保存期间新审计。只对本次精确目录恢复备份SDDL/owner/SACL；新建目录有真实内容或账本时保留并隔离，不递归删除。系统审计回退先确认期间没有其他变更，再恢复已批准的该子类别状态；不得为回退覆盖无关新策略。数据库/卷不自动删除或重置，需环境负责人确认无未归档试次后另行处置。
7. issuer私钥按现有Ed25519安全保管设计，只进入签发进程环境`SHOPRELAY_EVAL_FORMAL_SIGNING_KEY`；executor仅有公钥。提交真实公钥、issuer/executor OS身份、target marker identity及批准plan hash；测试密钥/身份不进入正式信任。模型端点仍{machine['endpoint']}，模型{machine['model']}；六代理7892、NO_PROXY清空、TLS开。有效价格/实际计费配置在签发前更新，不发送额外探针。

## 输入到齐后的同一执行流程

现有入口，不复制Runner：

```powershell
# 由批准的环境账号、显式新空PG库执行；pg-binding.json仅含url_env、host、port、database、identity_manifest、checkpoint_schema，不含DSN。
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-postgres-target-init data/eval/formal-config/first-frozen-v1/pg-binding.json
# 沿R24既有asset-workflow-config完成intake、两次真实审核、compare、freeze。
# 将新target identity、asset/语料/qrels/代码/节点配置/规则/价格/target-manifest字节冻结；再提交精确内容与执行许可。
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的独立Dev8配置> --issue
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的独立Dev8配置> --preflight
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的独立Dev8配置> --admission
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的独立Dev8配置> --execute
# Dev完成后冻结最终配置；Frozen另有精确许可。不得拿Frozen先调试Dev；首次冻结结果不因后续改代码被替换。
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的Frozen100配置> --issue
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的Frozen100配置> --preflight
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的Frozen100配置> --admission
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的Frozen100配置> --execute
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config <批准的Frozen100配置> --report
# 收到真实输出审核/裁决后，再--report进行离线重算，不能新增Run。
```

质量FAIL/PARTIAL照实保留并继续独立案；权限/身份/用量/审计/预算/清理及公共契约故障停止受影响段。已消费次数不重置，未知Run不自动重放，中间节点无损继续不冒称已证明。冻结后如修改实现，另段另绑定并披露，不改首次成绩。SSE断线可重接同Run的持久事件，已完成工具和确认事务不能重新采样。

## 正式退出与计分

沿方案M6.2四项任务：①Dev评分和本轮正式工程已完成，仍需真实独立正式route/必需证据/原子事实/Task参考及实际结果；②有限Dev Judge四条已经关闭，不重审不重放，新独立维度若要更广校准另定范围，不能把旧3/4泛化；③本轮补上动作和持久入口，仍缺真实新资产、真实两日审核、真实隔离/信任部署及精确许可，之后才运行正式Frozen；④实际执行后输出n/N、分组区间、用量、分歧和失败报告并判断退出。

初始质量目标仍Router MacroF1≥.85、必需证据Recall@5≥.85、人工关键事实正确率≥.90。路由按完整reference和RULE/LLM/FALLBACK分列；比例用Wilson、宏F1/查询均值用分组bootstrap。检索80须在精确许可前锁定评分对象：可选择已实现的FORMAL_FULL_QUERY_BM25模块测量（完整用户问题、分配的完整语料、独立必需证据参考），每题额外1次计入既定工具额度；不新增Run/Provider、不把结果送回Router/Agent。应用真实检索及owner API内部检索另存，不能拼成虚构全局排名。若选择实际自适应多跳检索指标，须提供逐hop或预先认可的聚合契约；缺失时不判质量门槛通过。任务缺交付计FAIL，候选内容不可评分单列。没有全案PASS或M2先PASS的新门槛；只读、动作、脚本确认和模型自主能力分别披露。既有15工具/MCP/长会话等§10.2适用证据按版本复用或补明确缺项，不以本轮测试数量代替所有门槛。

当前四个结论：正式评测未完成；质量门槛尚无正式结果；M6.2退出未满足；033电气冲突及任意中间节点无损崩溃续跑等未验收项保持披露。完成上述真实输入与许可后即可继续同一任务，不需要再安排一轮模板整理，也不重复已确认Dev审核。
'''
