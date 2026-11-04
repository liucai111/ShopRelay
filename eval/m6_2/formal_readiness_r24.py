"""Single additive R24 readiness release; metadata and isolated proof only."""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone, timedelta
import hashlib
import ast
import json
from pathlib import Path
import shutil
import xml.etree.ElementTree as ET

from eval.m6_1.formal_application import dependency_versions, PROFILE
from eval.m6_1.grants import formal_runtime_digest

OUT = Path("docs/eval/m6.2-formal-execution-readiness-r24-20261031")
R23 = Path("docs/eval/m6.2-r23-confirmed-calibration-and-formal-wiring-20261031")
FINAL_XML = Path("docs/probes/m62-r24-complete-final-r2.xml")
SOURCE_LOCK = Path("docs/probes/m62-r24-release-source-lock-r3.json")
SOURCES = (
    "eval/m6_1/formal_application.py", "eval/m6_1/formal_assets.py", "eval/m6_1/formal_scoring.py",
    "eval/m6_1/grants.py", "eval/m6_1/execution_modes.py", "eval/m6_1/personal_dev.py",
    "eval/m6_1/personal_dev_database.py", "eval/m6_1/personal_dev_runtime.py", "eval/m6_1/runner.py",
    "eval/m6_1/aggregate.py", "eval/m6_1/grader.py", "eval/m6_1/personal_dev_batch_m62.py",
    "eval/m6_1/budget.py", "eval/m6_1/store.py", "eval/m6_1/registry.py", "eval/m6_1/contracts.py",
    "scripts/run_m6_1_b_synthetic.py", "scripts/finalize_m6_2_r10_closeout.py", "pyproject.toml",
    "eval/m6_2/formal_readiness_r24.py", "requirements-formal-eval.lock",
    "tests/test_m6_2_r24_formal_application.py",
)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(root, path):
    return json.loads((root/path).read_text(encoding="utf8"))


def emit(root, path, value):
    target = root / OUT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    text = value.rstrip()+"\n" if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)+"\n"
    if target.exists():
        if target.read_text(encoding="utf8") != text:
            raise ValueError("release artifact changed; preserve it and make an explicit new version: " + str(path))
    else:
        target.write_text(text, encoding="utf8", newline="\n")


def proposal(root, inventory):
    return {"schema_version": "formal-plan-proposal-r24", "status": "CONCRETE_PROPOSAL_NOT_APPROVED_NOT_AUTHORIZED",
        "M6.1": "COMPLETE", "M6.2": "IN_PROGRESS", "existing_dev_coverage": "39/40;033 deferred;039/040 post-STOP observations excluded from compliant Dev rates",
        "asset_metadata_source": {"path": (R23/"formal-asset-metadata-inventory.json").as_posix(), "sha256": sha(root/R23/"formal-asset-metadata-inventory.json")},
        "formal_eligible_real_assets": inventory["frozen_eligible_asset_count"],
        "capability_limits": "This first formal proposal is readonly. Confirmed simulated writes,cancellation submission and handoff-to-human actions are not implemented by the readonly formal profile;owner must choose a separate explicit action scope if those are required for final Outcome coverage.",
        "known_replacement": {"asset_id": "policy-candidate-family", "registered_family_count": 60,
            "per_case_ids": None, "decision": "M6.1-A-OWNER-DISPOSITION-20261024-01", "disposition": "QUARANTINE_AND_REPLACE",
            "source": "eval/registry/m6.1-a-governance-overlay-r3.json", "other_assets_exposure": "Asset-specific R23 metadata;UNKNOWN remains UNKNOWN"},
        "recommended_new_assets": {"frozen_unique_cases": 100, "separate_dev_smoke_cases": 8,
            "reason": "100 route objects,80 policy retrieval objects including at least25 compound questions,and60 primary business Outcome objects;cohorts fixed before execution",
            "primary_outcome_distribution": {"product":8,"order":8,"after_sales":12,"policy":18,"cross_domain":8,"safety":6},
            "additional_policy_qa":40, "retrieval_cohort_design":"40 additional policy QA +12 after-sales +18 policy +8 cross-domain +2 safety policy =80",
            "additional_policy_qa_topics":{"return_refund":12,"warranty":8,"coupon":6,"fulfillment":6,"conditional_conflict":8},
            "sampling_unit":"Independent scenario/policy-combination/template group;never an ID substitution of a seen Dev question",
            "case_ids": None, "033":"Excluded from this proposed electrical-conflict capability scope;still disclosed as39/40 Dev coverage,not a PASS",
            "not_claimed":"Neither these counts nor test fixture assets are approved unseen Gold;steward supplies real content under a separate exact content permit"},
        "runtime":{"factory":"eval/m6_1/formal_application.py:factory","formal_execution_digest":formal_runtime_digest(),
            "checkpoint_limit":"Application currently uses memory checkpointing. Terminal archive/ledger recovery is verified;this does not prove product SSE/PostgreSQL mid-Run checkpoint recovery. Attach applicable existing §10.2 evidence before release.",
            "database_profile":PROFILE,"database":"data/eval/formal-targets/frozen-v1.sqlite3",
            "ledger":"data/eval/formal-ledgers/first-frozen-v1/results.sqlite3","signature_scheme":"ED25519_V1",
            "SDK_dependencies":dependency_versions(),"proxy":"http://127.0.0.1:7892","TLS":True,
            "retries":0,"concurrency":1,"embeddings":0,"external_Judge":0,"business_writes":0,"Pending_Action":0,
            "retrieval":"Pinned full assigned synthetic-business policy corpus;BM25 baseline;dense/reranking comparisons need separate declared scope and authorization"},
        "proposed_budget_not_granted":{"application_runs":100,"attempts_per_case":1,"provider_per_case":8,"provider_total":800,
            "tokens_per_case":120000,"tokens_total":12000000,"local_USD_per_case":.50,"local_USD_total":50,
            "seconds_per_case":600,"seconds_total":60000,"readonly_per_case":12,"readonly_total":1200,
            "admission_repository_reads_per_case":8,"admission_repository_total":800,
            "basis":"At most1 classification +up to6 bounded policy answer/check steps +1 headroom;readonly tools plus policy fetches separately counted.120k tokens at historical peak USD1.2/M =USD0.144;USD0.50 allows margin,not supplier hard cap. Refresh actual rate before issue.",
            "dev_smoke_separate_proposal":{"application_runs":8,"provider":64,"tokens":960000,"local_USD":4,"seconds":4800,"readonly":96,"admission_reads":64},
            "historical_27_run_grant":"CLOSED;not extended,restored,or reused"},
        "stopping":{"continue":"Terminal,safe,auditable,settled,exactly cleaned quality FAIL/PARTIAL or known candidate rejection is retained;continue independent cases",
            "stop":"Permission/data identity failure,unknown call/usage,unresolved execution/public contract/grader fault,deadline/budget exhaustion,unmatched receipt or cleanup failure",
            "recovery":"Resume stored terminal rawTrial only to verify cleanup/settle;an unknown consumed attempt is never automatically replayed. Private signing key is not required to execute/report.",
            "no_hot_changes":"One signed segment immutable;any correction preserves old results and requires new exact signed scope without resetting consumed attempts"},
        "targets":{"router_macro_F1":.85,"required_policy_evidence_recall_at_5":.85,"human_key_fact_correctness":.90,
            "global_task_success_threshold":None,"all_cases_PASS_required":False,"M2_PASS_global_precondition":False,
            "hard_gates_source":"ShopRelay_Agent_Eval_完整评测方案.md §10.2;review applicability and existing evidence,do not replace with status names"},
        "metrics":{"route":"Full frozen route reference,capabilities,intents,clarification,confusion matrix/per-class F1;RULE/LLM/FALLBACK separate",
            "retrieval":"Actual query/version/config/rank before citation filtering;P@1,required Recall@3/5,P@5,MRR@5;unbounded MRR requires a complete ranking and is not invented",
            "evidence":"Separate independently reviewed required-evidence clauses and atomic facts;not the168 Dev candidate-relevance labels",
            "outcome":"60 primary Outcome objects;delivery,required tools,owner,safety,facts,completeness;PARTIAL and UNSCORABLE retained;all attempted delivery failures remain Task FAIL",
            "intervals":"Wilson95 for binomial counts where N>=2;macro F1/mean query metrics need group/query bootstrap or explicitly N/A. Do not apply Wilson to a mean rank score.",
            "quality_vs_execution":"Execution completed and quality targets passed are separate report fields. No general statistical claim from small Dev cohorts."},
        "real_inputs_missing":["Owner approves exact scope/cohorts and budget","Steward supplies new content,fixtures,corpus,provenance and exact IDs/hashes",
            "Real first independent labels and genuinely later-date blind review/adjudication","Actual issuer/executor/reviewer OS identities,public key,secret custody,ACL/audit evidence,target identity",
            "Fresh Provider price/account configuration and exact signed content/execution approval","Applicable §10.2 evidence plus actual authorized Frozen results/output reviews"]}


def deployment(root, plan):
    from app.m0.config import load_settings
    settings = load_settings(require_api_key=False)
    case_budget={"provider_calls":8,"embedding_calls":0,"judge_calls":0,"total_tokens":120000,"estimated_usd":.5,
        "tool_calls":12,"max_calls_per_trial":20,"max_concurrent_trials":1,"max_duration_seconds":600,"max_retries":0}
    total={**case_budget,"provider_calls":800,"total_tokens":12000000,"estimated_usd":50,"tool_calls":1200,"max_duration_seconds":60000}
    environment={"mode":"FROZEN_EVAL","issuer_subject":None,"application_database":plan["runtime"]["database"],
        "application_artifacts":"data/eval/formal-ledgers/first-frozen-v1/application","execution_factory":{"path":"eval/m6_1/formal_application.py","callable":"factory"},
        "repository_read_cap_per_case":40,"output_reviewers":[],"python_dependencies":dependency_versions(),
        "provider_binding":{"model":settings.model,"endpoint":settings.base_url,"nodes":{k:asdict(v) for k,v in settings.nodes.items()}},
        "price_evidence":{"path":"data/eval/formal-config/first-frozen-v1/pricing.json","sha256":None}}
    experiment={"fixture_version":None,"fixture_sha256":None,"grader_version":"formal-code-grading-v1","judge_version":"none",
        "retriever_config":{"mode":"bm25","source":"fixture_retrieval;full reviewed assigned corpus"},"knowledge_index_version":None,
        "model_versions":{"application":settings.model},"prompt_versions":{"source":"Included in formal execution working-tree/runtime hash;extract actual route/answer versions at lock"},
        "tool_permissions":[],"retry_policy":{"max_retries":0},"random_seed":1,"environment":environment,
        "budget_limits":total,"data_version":None,"reporting_cohorts":{"route":[],"retrieval":[],"outcome":[]}}
    return {"issuer":None,"trusted_issuers":[],"subject":None,"signature_scheme":"ED25519_V1","verification_public_key_base64":None,
        "signing_key_env":"SHOPRELAY_EVAL_FORMAL_SIGNING_KEY","approved_plan_hashes":[],
        "allowed_targets":[],"actual_target_binding":["formal-readonly-local-v1",PROFILE,None],
        "registry_metadata":"data/eval/formal-assets/first-frozen-v1/registry-locked.json",
        "result_store":plan["runtime"]["ledger"],"grant_file":"data/eval/formal-ledgers/first-frozen-v1/authorization.json",
        "experiment_id":"M62-FORMAL-FROZEN-PROPOSAL-V1","source_revision_label":"Working tree +mandatory runtime digest;not Git HEAD",
        "scoring_version":"formal-code-grading-v1","plan":{"approval_ref":None,"mode":"FROZEN_EVAL","asset_id":None,"asset_version":None,"asset_sha256":None,
            "case_bindings":None,"subject":None,"config_sha256":None,"target_id":"formal-readonly-local-v1","target_profile":PROFILE,
            "target_identity":None,"ledger_path":plan["runtime"]["ledger"],"valid_from_utc":None,"expires_at_utc":None,
            "budget":total,"runtime_bindings":[["eval/m6_1/formal_application.py",sha(root/"eval/m6_1/formal_application.py")],
                ["data/eval/formal-assets/first-frozen-v1/lock-record.json",None], ["data/eval/formal-config/first-frozen-v1/pricing.json",None]],
            "execution_implementation_sha256":formal_runtime_digest(),"experiment_id":"M62-FORMAL-FROZEN-PROPOSAL-V1"},"experiment_config":experiment}


HUMAN = r"""# R24 集中人员处理包

这是具体部署与审核交接，不是新一轮 Dev 评分确认。R23 已确认事项不再确认；本轮没有真实模型或应用试次。以下方案等待真实输入，不能据此签发空白 Frozen 许可。

## 1. 项目所有者：只决定真实范围和权限

| 决定 | 推荐项与影响 | 需要填的真实输入 |
|---|---|---|
| 正式范围 | 新 Frozen 100题：Router100、必需证据检索80、主要业务Outcome60；另8题独立Dev Smoke。按场景/政策组合/模板分组，三指标队列在运行前锁定；不是沿用旧39案或把Dev转Frozen。 | 是否采纳此范围；真实资产ID/版本/名单/顺序；三个队列的精确ID。未批准前这些只是设计数量。 |
| 033 | 此次排除电气冲突专业能力，保留旧033延期/39 of40覆盖披露。若另行纳入，必须有该案独立电气安全和业务/政策审核；不扩为所有题都需专家。 | 选择排除该能力，或另行专项审核范围。 |
| 预算 | Frozen最多100个应用试次、800 Provider、1200只读、1200万tokens、估算USD50、60000秒；每案1次/8请求/12只读/12万tokens/USD0.50/600秒。独立Dev8题另需8次/64请求/96只读/96万tokens/USD4/4800秒。并发1、重试0、Judge/Embedding/写入/Pending Action0。低于逐案合计的总上限会在Run前拒绝。 | 明确批准实际N和逐案更紧工具范围。现有27次旧授权不提供任何新名额；新增许可由精确计划签发。 |
| 访问/签发 | 先批准真实新资产的精确内容导入/审核访问，再批准完整绑定后的DEV_SMOKE和FROZEN_EVAL计划；两个模式、资产和授权分别签发。 | 内容permit编号、路径、SHA、有效期、steward；issuer命名空间/真实OS账号；执行账号；精确计划批准ref/hash/目标/时限/预算。 |

初始规模来自方案的Router约100、检索80–120且至少25复合、Outcome40–60。推荐100个实际应用问题，其中60主要Outcome、40额外政策QA；检索队列80=12售后+18政策+8跨域+2安全政策+40额外QA。具体题目完成后核验分布，不反推已存在这些题。质量目标仍为.85/.85/.90；没有全案PASS或M2先PASS的新门槛。BM25是本次首个正式基线；其他检索变体及公平Single/Multi消融另列范围，不谎称已覆盖。

## 2. 数据steward：交付实物，不是再填空状态

已知须替换：`policy-candidate-family`登记60，依据`M6.1-A-OWNER-DISPOSITION-20261024-01`及R3治理overlay。获准元数据未给出逐题ID/受影响子集，不能杜撰60个ID。该家族隔离、不能重新读旧题改名；其他11资产逐项见R23元数据盘点，未核定的完整性/暴露仍UNKNOWN，不能因共享目录判全部暴露。当前合格正式资产0。推荐的新100+8不是声称已有108份合格资产，也不额外把旧60叠加计算为168份。

推荐保存位置（尚未创建真实内容/改变权限）：

- Frozen原始输入：`data/eval/formal-assets/first-frozen-v1/intake-source.json`；首日/盲审/裁决/锁定记录同一受保护目录；最终`cases-locked.json`、`registry-locked.json`。
- 独立Dev8题另用`data/eval/formal-assets/formal-dev8-v1/`，`split=dev`；不要把已见的Dev Smoke题复制为Frozen。
- 可公开的元数据与暴露/导入授权证明单独保存。普通开发、Web及普通Runner不得获得题目读取；只有精确授权的steward/审核/正式执行主体可读各自范围。模型及AI调试暴露须逐资产记入，不以隔日重读“清除暴露”。

必须提供的数据：

| 层 | 字段与接受标准 |
|---|---|
| 文件 | `schema_version=formal-readonly-assets-v1,version,split,cases`；稳定case/group ID、来源/作者方法/暴露与独立性证明；实际件数与批准数一致。未提供的ID、作者、SHA、合格性不得填造。 |
| Case | EvalCase v1字段：case_id/group_id/split/input/expected_outcome/rubric_refs/fixture_ref/risk_level/data_version。group按真实模板/政策组合，不能只换资源号。 |
| runtime input | user_query,user_id,显式一对一owner_map,allowed_capabilities,fixture_refs,fixture,evaluation_at；只读白名单。Fixture含users/orders_and_snapshots/shipments/policies/knowledge_version及实际所需products/inventory/order_items/returns/refunds。不得给Router/Agent放expected答案/路由或隐藏补ID。 |
| 业务证据 | 资源ID/owner、订单行唯一product_id和数量、已记录状态、金额/paid_at/delivered_at的已知或null边界、库存观测时间/缺失、Shipment来源/事件/空记录含义。均为隔离的模拟业务事实，不导入生产数据。 |
| 政策语料 | 每chunk稳定evidence_id、text、文档version、effective日期区间及来源；独立锁定完整分配语料，不按Gold正例过滤。当前适配器把有效结束日转换为次日UTC零点排除；必须确认此数据语义，需时区/时刻精度的来源不得偷偷截断为日期。 |
| 独立参考 | 四套分开：完整RouteDecision参考；每查询必需证据source_ids/条件（整语料审核，非Dev候选相关性）；原子事实fact_id/主体/谓词/值/条件/来源；Task必需条件和合法工具替代组/零调用/逐案预算。题目、资格判断和工具参数来源必须能相互核对。 |

实际导入接收器是`--asset-workflow-config`，模板在本包`asset-workflow-inputs-r24.json`；缺身份、permit、来源SHA和独立/暴露证明时会在边界拒绝。新内容只有经过真实审核才可锁定Registry，不能由当前AI调试上下文构造后自称未见。

## 3. 审核者：实际首日独立标注，再在真实后续日期盲审

允许按方案§3.4由一人独立初标后隔日盲审；这不自动等于不同独立专家。033特例不被此流程替代。先由所有者指定真实审核OS身份和允许角色，不能将AI、当前测试账号或CHAT_USER_SELF_CONFIRMED改名为独立Gold。

1. 首日由CLI导出`day1-form`，逐题从完整问题/业务上下文/全部政策来源及rubric独立填写route、required_evidence、atomic_facts、task和理由。不要看旧AI预标/旧参考。
2. 导入`accept`时由实际OS账号接收，系统记录真实接收时间。不能导入他人的姓名冒签；OS认证只是输入来源，不替代真实独立标注行为和暴露说明。
3. 真正后续本地日期再导出`blind-form`、独立重标。两视图均不显示旧Gold标签、AI建议和第一次提交；保留必要问题/来源。接收器拒绝同日“盲审”。AI建议曾展示的Dev材料不能被改称未受提示的初标。
4. `compare`逐项输出差异。裁决文件绑定comparison SHA，必须逐项给最终值和理由，由实际有角色者导入`freeze`。旧两票不覆盖，锁定资产/标签/语料/fixture版本保留。
5. 正式执行后的输出审核另用`--report`生成per-Run表，再`--receive-review --review-phase day1/blind`。表保留实际答案/候选、来源和必要评分目标，但不展示旧Code结论、Gold数值/旧相关标签或拒绝错误码。原子事实逐项填PASS/FAIL/UNKNOWN/NOT_APPLICABLE，Task单独填PASS/PARTIAL/FAIL/UNSCORABLE。未交付Task仍FAIL；候选内容分开研究。分歧经`--adjudicate-review`追加，不覆盖原票。

核定参考时要区别候选相关/必要证据、政策有效期/个案资格、申请/审核/批准/到账、存储状态/实时物流、缺事实UNKNOWN/错误事实。完整排名由新执行保存，不从历史引用补排名。完整必要证据正集合经人审后才计算Recall，UNKNOWN及多hop缺query级qrels需明确排除理由。

## 4. 环境负责人：精确部署输入与验证

本轮未改真实ACL、信任列表、密钥、生产数据库或真实目标。拟目标仅专用SQLite；无生产DSN回退。操作前由负责人给出：issuer/steward/reviewer/正式executor的真实OS账号与SID，普通developer/Web/Runner账号或组，受保护资产与配置目录，私钥保管方式，OS审计权限。

- 使用`whoami /user`和`python -c "from eval.m6_1.formal_assets import authenticated_subject; print(authenticated_subject())"`取得实际身份；配置必须使用后者返回的准确字符串。不能把Fixture owner别名当Windows身份。
- 对上述两个新资产目录、`data/eval/formal-config/first-frozen-v1/`、`data/eval/formal-ledgers/first-frozen-v1/`、`data/eval/formal-targets/`先保存当前`Get-Acl -LiteralPath <确切目录>`。拟权限：steward维护输入和暴露记录；审核者仅其授权内容/提交；issuer维护受保护批准配置和私钥；正式executor读取精确授权资产/公钥配置，写自己的专用DB/固定账本/归档；普通开发/Web/普通Runner拒读题目。若同一SID承担普通开发和正式读取，不能同时用DENY和grant解决，应使用明确的独立执行账号/令牌或重新批准的隔离设计。
- 任何`icacls`、审计SACL或系统审计策略操作，先提交确切目录、SID/权限和原ACL备份给环境负责人批准；本轮没有执行。用**虚构canary文件**验证普通身份拒读、指定身份能读，并保留实际成功/失败审计（适用Windows4663等事件）；不能为探针打开真实题目。
- issuer从安全保管设施向其**本进程**提供`SHOPRELAY_EVAL_FORMAL_SIGNING_KEY`（Ed25519原始32字节seed的base64），只在`--issue`进程中使用。公钥32字节base64写入受保护部署配置。执行、报告、恢复都只读公钥，不需要签名私钥。不得把私钥放仓库JSON、日志或聊天；本包没有真实密钥。没有配置真实issuer/公钥/批准plan hash时不得正式签发。
- 在获准目标目录用初始化命令创建**新**标记库与固定账本，回填返回的identity；已有库拒绝重新标记。核对外键、integrity、事件CHECK、无活动Run/Pending Action/lease，未知receipt必须对账，不能删库重来。
- 使用本包部署模板中实际model/endpoint/nodes/dependency版本与即将运行环境逐项比对。新鲜价格证据必须包含model、endpoint、valid_from_utc、valid_until_utc、conservative_usd_per_million、source_refs及被锁定的SHA。R23价格只作有日期的依据；隔日审核后价格到期就刷新，不伪称长期有效。API账号/实际计费配置由负责人确认，真实发票在未取得前为UNKNOWN。
- 六个代理变量在实际Python父进程统一为7892，NO_PROXY和no_proxy清空，HTTPX在无请求preflight验证；TLS正常验证，SDK重试0。不发送独立模型探针。

## 5. 接收输入后继续同一任务的实际入口

以下均为已有`run_m6_1_b_synthetic.py`的新增正式分支，不是另一个Runner。运行前在根目录，实际路径由所有者批准；用配置中的确切范围替换模板缺项，先独立审核，不直接运行本包未填满的模板。

```powershell
# 已批准的新目标/账本：一次性初始化，不重置既有库或旧27次授权。
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-target-init data/eval/formal-targets/frozen-v1.sqlite3
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-ledger-init data/eval/formal-ledgers/first-frozen-v1/results.sqlite3

# steward的精确内容permit就绪后，w.json指向受保护工作配置；所有输出选全新路径。
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/formal-config/first-frozen-v1/w.json intake --output data/eval/formal-assets/first-frozen-v1/intake-receipt.json
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/formal-config/first-frozen-v1/w.json day1-form --output data/eval/formal-assets/first-frozen-v1/day1-form.json
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/formal-config/first-frozen-v1/w.json accept --submission data/eval/formal-assets/first-frozen-v1/day1-filled.json --output data/eval/formal-assets/first-frozen-v1/day1-receipt.json
# 真正后续日期再生成blind-form、独立填写、accept，不以本轮测试虚拟时钟代替。
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/formal-config/first-frozen-v1/w.json blind-form --output data/eval/formal-assets/first-frozen-v1/blind-form.json
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/formal-config/first-frozen-v1/w.json accept --submission data/eval/formal-assets/first-frozen-v1/blind-filled.json --output data/eval/formal-assets/first-frozen-v1/blind-receipt.json
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/formal-config/first-frozen-v1/w.json compare --output data/eval/formal-assets/first-frozen-v1/comparison.json
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --asset-workflow-config data/eval/formal-config/first-frozen-v1/w.json freeze --submission data/eval/formal-assets/first-frozen-v1/adjudication.json --output data/eval/formal-assets/first-frozen-v1/freeze-receipt.json
# 无分歧时freeze省略--submission；输入、两次标注和裁决全保留。

# 真正精确批准、时效价格、签发角色和公钥齐备，生成最终case/config/实现hash后：
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config data/eval/formal-config/first-frozen-v1/deployment.json --issue
# 下列步骤切换到正式执行账号；不向该进程提供签名私钥。
$env:HTTP_PROXY=$env:HTTPS_PROXY=$env:ALL_PROXY=$env:http_proxy=$env:https_proxy=$env:all_proxy='http://127.0.0.1:7892'
$env:NO_PROXY=$env:no_proxy=''
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config data/eval/formal-config/first-frozen-v1/deployment.json --preflight
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config data/eval/formal-config/first-frozen-v1/deployment.json --admission
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config data/eval/formal-config/first-frozen-v1/deployment.json --execute
.venv/Scripts/python.exe scripts/run_m6_1_b_synthetic.py --formal-config data/eval/formal-config/first-frozen-v1/deployment.json --report
# 再执行--report只重算，不创建Run、不调用模型。输出SHA应相同。
# --receive-review --submission <单Run填妥表> --review-phase day1 或 blind
# --adjudicate-review --submission <含trial_id/comparison_sha256/decisions/rationale的裁决>
```

当前方案是只读首轮正式范围，尚不覆盖实际模拟退款/取消提交及转人工动作Outcome。若正式最终范围必须包含这些能力，所有者需另定动作确认、状态前置、白名单、receipt与精确授权；不得只改枚举或绕过当前只读护栏。M6.2退出判断须披露这一范围差异，不能把只读完成写成所有业务能力已覆盖。

当前应用采用内存checkpoint。本轮证明的是终态归档、账本、清理与结算恢复，不能替代方案§10.2的SSE/Run/持久Checkpoint恢复证据。环境负责人需附上适用于正式目标的现有恢复测试记录；若其覆盖不足，只补明确缺项，不声称本轮虚构测试已经证明该产品硬门槛。

环境安装命令（本轮未执行安装/联网）：`.venv/Scripts/python.exe -m pip install ".[formal]" -r requirements-formal-eval.lock`。已有已验证隔离环境优先复用并核对版本，不擅自更换。

上述命令的issuer进程也须设置同一代理用于**无请求**配置检查；不发送探针。8题正式Dev Smoke用独立dev资产、目标/账本/批准配置及mode=DEV_SMOKE；不得让Frozen题先做Smoke。随后新100题FROZEN_EVAL由另一精确许可运行。签发落库但授权文件发布中断时，用原配置`--recover-grant`发布同一活跃grant；关闭/撤销不恢复。执行重启会跳过已结算案，只对已保存终态原档补清理/结算，未知执行保留现场、不自动重试。

## 6. 收口标准

当前工程可进入“接收真实输入”阶段，不需要再安排一轮纯报告整理。输入齐备且正式许可到位后，按同一入口完成试次、输出审核/裁决、独立重算、费用结算和清理，再作M6.2退出判定。已有Dev确认、有限Judge和隔离测试不变成Frozen成绩。

最终分别报告“正式评测是否完成”和“.85/.85/.90及§10.2质量门槛是否通过”。真实FAIL/PARTIAL保留；没有未授权写入、越权、未知用量或清理故障才允许独立后案继续，不因质量未全对无限重跑。
"""


def build(root: Path):
    root = root.resolve(strict=True)
    inventory = read(root, R23/"formal-asset-metadata-inventory.json")
    for path, digest in inventory["source_bindings"].items():
        if sha(root/path) != digest: raise ValueError("public metadata binding changed: " + path)
    lock = read(root, SOURCE_LOCK)
    if formal_runtime_digest() != lock["formal_runtime_digest"] or any(sha(root/p) != h for p,h in lock["files"].items()):
        raise ValueError("runtime changed during final offline acceptance")
    suites = ET.parse(root/FINAL_XML).getroot().findall("testsuite")
    tests = {k:sum(int(s.attrib[k]) for s in suites) for k in ("tests","failures","errors","skipped")}
    if tests["failures"] or tests["errors"]: raise ValueError("final offline regression not passed")
    props = {p.attrib["name"]:p.attrib["value"] for p in ET.parse(root/FINAL_XML).getroot().iter("property")}
    fixture_root = root / props["isolated_e2e_root"]
    if not fixture_root.resolve().is_relative_to((root/"data/eval/offline-tests").resolve()): raise ValueError("isolated proof outside test directory")
    proof = json.loads((fixture_root/props["isolated_report"]).read_text(encoding="utf8"))
    if len(proof["rows"]) != 4 or proof["not_run_case_ids"] or proof["budget"]["active_reservations"]:
        raise ValueError("isolated actual application proof incomplete")
    before_progress=root/OUT/"source-before/progress-before-r24.md"
    if not before_progress.exists():
        before_progress.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(root/"docs/progress.md",before_progress)
    plan = proposal(root, inventory)
    emit(root, "formal-plan-r24.json", plan)
    emit(root, "deployment-inputs-r24.json", deployment(root, plan))
    prefix = "data/eval/formal-assets/first-frozen-v1/"
    workflow={"stewards":[],"reviewers":[],"adjudicators":[],"asset_id":None,"expected_case_count":100,"required_replacements":["policy-candidate-family"],
        "provenance":{"replacement_of":["policy-candidate-family"],"copied_or_relabelled_dev":None,"exposure_status":"UNKNOWN","exposure_evidence_refs":[],"independence_evidence_refs":[]},
        "content_intake_permit":{"authorization_ref":None,"approved_by":None,"source_path":prefix+"intake-source.json","source_sha256":None,"valid_from_utc":None,"expires_at_utc":None},
        "intake_record":prefix+"intake-record.json","day1_record":prefix+"day1-record.json","blind_record":prefix+"blind-record.json",
        "frozen_asset":prefix+"cases-locked.json","lock_record":prefix+"lock-record.json","registry_output":prefix+"registry-locked.json"}
    emit(root, "asset-workflow-inputs-r24.json", workflow)
    emit(root, "human-input-and-operations-r24.md", HUMAN)
    emit(root, "offline-acceptance-r24.json", {"primary_full_affected_regression":{"JUnit":"docs/probes/m62-r24-final.xml","tests":111,"failures":0,"overlap_with_release":True},
        "final_current_runtime_regression":tests,"JUnit":FINAL_XML.as_posix(),"source_lock":SOURCE_LOCK.as_posix(),
        "isolated_proof":{"directory":props["isolated_e2e_root"],"report":props["isolated_report"],"report_sha256":sha(fixture_root/props["isolated_report"]),
            "fictional_cases":4,"real_model_requests":0,"SDK_transport_substitute_requests":proof["budget"]["provider_requests"],
            "task_results_not_all_PASS":[r["assessment"]["task_outcome"] for r in proof["rows"]],"budget":proof["budget"],
            "review_identity_and_next_day_clock":"PYTEST_INJECTIONS_FOR_VIRTUAL_FIXTURES_ONLY_NOT_REAL_HUMAN_APPROVAL", "project_registry_modified":False},
        "meaningful_coverage":["Existing script dispatch and full formal factory source import","Exact permit/input/owner/fixture Repository path",
            "Independent-reference schema vs blind context without old Gold values","Ed25519 public verifier cannot issue;executor without private key",
            "Quality FAIL and known rejection continue;unknown usage stops;cleanup failure preserves Run",
            "Closed grant denied,orphan publication recovery,terminal cleanup settlement resume without replay",
            "Output review binding,next-day rejection,PARTIAL adjudication,immutable report recompute"],
        "historical_write_and_network_guard":"docs/probes/run_m62_r22_offline.py denies sockets/DNS/telemetry and historical docs/eval writes",
        "real_application_runs":0,"Provider":0,"Judge":0,"Embedding":0,"protected_content_access":False,"system_ACL_or_real_trust_changes":False})
    emit(root, "exit-matrix-r24.json", {"M6.1":"COMPLETE","M6.2":"IN_PROGRESS","formal_evaluation_completed":False,"quality_targets_passed":None,
        "tasks":[{"task":1,"status":"DEV_CONFIRMED_FORMAL_SCORER_READY","remaining":"Real independently reviewed asset/reference/cohort input;actual formal observations and output review","owner":"steward/reviewer/authorized executor"},
            {"task":2,"status":"LIMITED_DEV_CALIBRATION_DONE_DISCLOSED","remaining":"Closed four-sample calibration and confirmed Human Task labels remain valid;no repeat approval or call required. Additional independent dimension labels are needed only for a wider calibration claim;Judge never overwrites Human decisions.","owner":"real reviewer;owner exact separate Judge authorization only if additional study is selected"},
            {"task":3,"status":"FULL_OFFLINE_ENTRY_VERIFIED_REAL_INPUTS_AND_PERMITS_MISSING","remaining":"Unseen replacement assets;first-day/later-date blind adjudication;real trust/ACL/identity/price;separate exact DEV_SMOKE/FROZEN content+execution permits;actual Frozen","owner":"owner/steward/reviewer/environment owner/executor"},
            {"task":4,"status":"RECOMPUTE_REPORT_AND_METRICS_IMPLEMENTED","remaining":"Actual formal n/N,costs,failures,Human disagreement record and final acceptance;execution completion separate from passing quality","owner":"executor/reviewer/project owner"}],
        "governance_basis":["ShopRelay_Agent_Eval_完整评测方案.md §3.4,§10.1,§10.2,M6.2","项目规格/evaluation-spec.md first Frozen prerequisites","eval/registry/m6.1-a-governance-overlay-r3.json"],
        "not_new_conditions":["No all-case PASS","No rerun39Dev","No global M2 PASS dependency","No general033 specialist signature requirement"],
        "historical_incident":"R6 overwritten offline report bytes not restored;retained event;real Run evidence remains separate",
        "coverage_limits":["Readonly proposed scope does not cover refund/cancellation submission or human-escalation action Outcome;owner must explicitly settle applicable formal scope,not silently waive it.","Terminal ledger recovery is verified;current in-memory app checkpoint is not proof of §10.2 SSE/PostgreSQL mid-Run recovery. Attach applicable existing evidence or identify specific unverified gate."],
        "exact_handoff":"human-input-and-operations-r24.md sections1–5;real asset IDs/approvals are intentionally unfilled,not fictionalized"})
    bound_sources = set(SOURCES) | {"app/m2/self_rag.py"}
    # Read only the explicitly declared source lists of the two digest functions.
    # No dataset or repository traversal is needed to preserve this release.
    for module, function in (("eval/m6_1/personal_dev.py", "implementation_digest"), ("eval/m6_1/grants.py", "formal_runtime_digest")):
        tree = ast.parse((root/module).read_text(encoding="utf8"))
        node = next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name==function)
        for literal in ast.walk(node):
            if isinstance(literal,ast.Constant) and isinstance(literal.value,str) and literal.value.endswith((".py",".toml",".lock")):
                relative=literal.value
                if (root/relative).is_file(): bound_sources.add(relative)
    for relative in sorted(bound_sources):
        target=root/OUT/"source-final"/relative;target.parent.mkdir(parents=True,exist_ok=True)
        if target.exists() and sha(target)!=sha(root/relative): raise ValueError("source-final already frozen")
        if not target.exists():shutil.copyfile(root/relative,target)
    existing_summary=root/OUT/"engineering-closeout-r24.json"
    archived_at=(read(root,OUT/"engineering-closeout-r24.json")["archived_at_utc"] if existing_summary.exists()
        else datetime.now(timezone.utc).isoformat())
    summary={"archived_at_utc":archived_at,"formal_runtime_sha256":formal_runtime_digest(),"final_tests":tests,"real_application_runs":0,"real_provider_requests":0,
        "formal_eligible_real_assets":inventory["frozen_eligible_asset_count"],"path":OUT.as_posix(),"M6.2":"IN_PROGRESS"}
    emit(root,"engineering-closeout-r24.json",summary)
    emit(root,"closeout-r24.md",f"""# R24 正式执行准备收口

实际应用工厂、只持公钥的执行验证、Fixture/Repository准入、逐请求与逐案预算、终态恢复、原子清理和固定报告已接到既有CLI。资产导入/首日审核/后续日期盲审/裁决/Registry锁定和真实输出审核分别有接收入口；旧签发/Dev入口保留。

前次完整受影响7文件111 passed；最终当前运行时同7文件{tests['tests']} passed，两批范围重叠，不能相加。迭代日志保留：最初字段名、前注册预算FK、配置相对路径缺陷已修复；一次测试期间源码修改触发了冻结摘要拒绝，最终按source-lock重跑一致。原权限/历史文件未覆盖。虚拟审核者及后续日时钟仅用于测试，没有真实人员签署。

当前摘要：`{summary['formal_runtime_sha256']}`。可重算虚构实际应用证据见`offline-acceptance-r24.json`（4个隔离试次，含任务FAIL，全部结算/精确清理；它们不是新增真实Dev或Frozen试次）。本轮真实应用Run/Provider/Judge/Embedding均0。

正式方案已具体推荐：新100 Frozen（Router100/检索80/主要Outcome60）+独立Dev8，公开元数据可确认旧政策家族60必须替换、现有正式合格资产0。真实逐题ID、独立来源、审核、签发身份、目标identity及精确许可尚未取得；不存在可签发的空白许可。39/40 Dev覆盖及033延期不变，旧27次授权不复用。

集中人员操作见[human-input-and-operations-r24.md](human-input-and-operations-r24.md)，机器方案与部署/导入配置各一个。复用生成入口：`python scripts/finalize_m6_2_r10_closeout.py --version r24`。输入到齐后使用既有formal CLI继续，无需另建Runner；已闭授权不恢复。

M6.1保持COMPLETE，M6.2未完成；正式评测尚未执行，质量门槛尚无正式结果。Gold/Registry/M2/Frozen Gate、033及R6覆盖事件保持原决定。未读取、搜索、复制或散列受保护题目，未改ACL/真实信任或Git。
""")
    progress=root/"docs/progress.md"
    marker="## 2026-10-31 M6.2 R24：正式应用接线与真实输入交接"
    if marker not in progress.read_text(encoding="utf8"):
        with progress.open("a",encoding="utf8",newline="\n") as handle:
            handle.write(f"\n\n{marker}\n\n"
                f"- 复用既有Registry、grant、Runner和正式CLI，接通真实应用工厂、签发身份/执行目标、只持公钥的Ed25519验证、逐步预算、固定账本、异常实际Run计数、终态恢复与receipt原子清理；旧入口和关闭授权不改。运行摘要{summary['formal_runtime_sha256']}。\n"
                f"- 本轮最终同7个完整受影响测试文件{tests['tests']} passed，0 failures/errors；前次111项范围重叠不相加。隔离虚构资产贯穿导入、首日/后续日测试标注、锁定、签发、实际应用执行、质量失败继续、用量未知/清理失败停止、输出审核、重算和关闭防重放；额外验证同Run订单行到保修product_id及双节点交接。虚拟时钟/身份只属于测试，无真实审核签署。日志、失败迭代及来源锁保留。\n"
                "- 正式参考接收分开路由、必需证据、原子事实和Task；未知用量/审计不补0，已证实未检索的计划查询按端到端检索0命中计，不伪造排名；多hop缺逐查询qrels仍明确排除。候选、交付、任务结论分开。\n"
                "- 具体方案与集中人员包在docs/eval/m6.2-formal-execution-readiness-r24-20261031/。建议新Frozen100（路由100/检索80/主要Outcome60）及独立Dev8，预算仅草案，未获新真实执行授权。旧政策家族登记60按既有决定替换；当前正式合格真实资产0，逐题ID未知不杜撰；39/40 Dev及033延期不变。\n"
                "- 待真实输入为：所有者精确范围/次数/预算和内容及执行许可；steward新资产/fixture/全语料/来源暴露证明；真实首日独立标注及后续日期盲审裁决；实际OS身份/公钥与私钥保管/ACL canary和审计/目标identity/有效价格。只读方案未覆盖提交退款等动作Outcome，内存checkpoint不代替§10.2持久恢复证据。\n"
                "- M6.1保持COMPLETE，M6.2 IN_PROGRESS。正式评测未执行，质量门槛未判定。真实Run/Provider/Judge/Embedding均0；未改真实ACL/信任、未访问受保护内容、无Git写操作。旧progress哈希为历史快照，本轮只追加并另存前后快照；不重写旧SHA清单。\n")
    after=root/OUT/"progress-after-r24.md"
    if not after.exists(): shutil.copyfile(progress,after)
    elif sha(after)!=sha(progress): raise ValueError("progress changed after release; keep snapshot and append a later manifest")
    sources=set(bound_sources) | set(inventory["source_bindings"]) | {
        "AGENTS.md","ShopRelay_Agent_Eval_完整评测方案.md","项目规格/evaluation-spec.md",
        "docs/progress.md",SOURCE_LOCK.as_posix(),FINAL_XML.as_posix(),FINAL_XML.with_suffix(".log").as_posix(),
        "docs/probes/m62-r24-final.xml","docs/probes/m62-r24-final.log","docs/probes/run_m62_r22_offline.py"}
    sources.update(p.relative_to(root).as_posix() for p in (root/OUT).rglob("*")
        if p.is_file() and p.name not in {"SHA256SUMS.json","SHA256SUMS.md","recompute-verification-r24.json"})
    sources.update(p.relative_to(root).as_posix() for p in (root/"docs/probes").glob("m62-r24-*") if p.is_file())
    # Only this newly created fictional proof directory, never real evaluation content.
    sources.update(p.relative_to(root).as_posix() for p in fixture_root.rglob("*")
        if p.is_file() and p.suffix in {".json",".jsonl",".sqlite3"})
    entries=[{"path":p,"sha256":sha(root/p),"description":
        "R24 isolated fictional proof" if p.startswith(props["isolated_e2e_root"]+"/") else
        "R24 release or explicit source/test/spec binding"} for p in sorted(sources)]
    emit(root,"SHA256SUMS.json",{"schema_version":"path-sha-description-v1","entries":entries,
        "progress_note":"docs/progress.md may later be appended;source-before/progress-before-r24.md and progress-after-r24.md preserve exact historical snapshots. Old manifests remain unchanged.",
        "protected_asset_content_included":False})
    emit(root,"SHA256SUMS.md","# R24 SHA-256\n\n路径与说明分列；未包含受保护题目。progress后续追加按本包前后快照解释，不覆盖旧清单。\n\n| 路径 | SHA-256 | 说明 |\n|---|---|---|\n"+
        "\n".join(f"| {e['path']} | {e['sha256']} | {e['description']} |" for e in entries))
    return summary
