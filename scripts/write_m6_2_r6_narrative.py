"""Write the human-readable, source-bound R6 closeout addenda (offline only)."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029"
REPORTS = PKG / "reports"


def read(rel: str):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_new(path: Path, text: str) -> None:
    if path.exists():
        raise SystemExit(f"REFUSE_OVERWRITE:{path.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def main() -> int:
    r5res_rel = "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/reports/case-results-r5-r1.json"
    r5res = read(r5res_rel)
    p006 = next(c for c in r5res["cases"] if c["case_id"] == "D1-PRODUCT-006")
    p006_archive = read(p006["archive_path"])
    p006_result = p006_archive.get("result", {})
    p006_obs = p006_result.get("observations", {})
    p006_route_events = []
    for event in p006_obs.get("run_events", []):
        if event.get("event_type") == "route_decided":
            try:
                payload = json.loads(event.get("payload_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {"parse_status": "INVALID_EVENT_PAYLOAD"}
            p006_route_events.append({"event_type": "route_decided", "payload": payload})
    p006_analysis = {
        "schema_version": "m6.2-r324-product006-r5-root-cause-evidence-r1",
        "classification": "R5_HISTORICAL_FAILURE_ROOT_CAUSE_NOT_FULLY_IDENTIFIABLE",
        "run_id": p006["run_id"],
        "archive_path": p006["archive_path"],
        "archive_sha256": p006["archive_sha256"],
        "r5_result_path": r5res_rel,
        "r5_result_sha256": sha(ROOT / r5res_rel),
        "observed": {
            "provider_request_count": p006.get("actual_model_requests"),
            "token_usage": p006.get("token_usage"),
            "local_estimated_usd": p006.get("local_estimated_usd"),
            "response_received_and_usage_recorded": True,
            "route_source": p006.get("route", {}).get("decision_source"),
            "route_strategy": p006.get("route", {}).get("strategy"),
            "route_reason": p006.get("route", {}).get("reason_code"),
            "fallback_error_type": p006.get("route", {}).get("fallback_error"),
            "allowed_capability_not_invoked": "get_product_detail",
            "agent_started": p006.get("program_score", {}).get("agent_started"),
            "delivered_answer": p006.get("delivered_answer"),
            "raw_provider_response_archived": False,
            "route_event_count": len(p006_route_events),
            "route_events": p006_route_events,
        },
        "stage_assessment": {
            "network_or_connection_failure": "NOT_SUPPORTED_BY_EVIDENCE; a response was received and token usage recorded",
            "provider_http_response_or_transport_contract": "UNKNOWN; raw response/status not preserved",
            "provider_json_or_schema_failure": "UNKNOWN; only ValueError type survived",
            "local_RouteDecision_validation": "UNKNOWN; original response fields and failing validation location absent",
            "_validate_classifier_decision_semantic_contract": "UNKNOWN; no field-level diagnostic was present in the historical record",
            "capability_scope_violation": "NOT_OBSERVED; fallback carried no capabilities and invoked no tool; missing required capability caused task failure, not unauthorized access",
            "downstream_executor_failure": "NOT_REACHED; no agent/tool execution occurred after fallback",
        },
        "limitations": [
            "The original model response was not archived; the precise ValueError cause cannot be reconstructed.",
            "No synthetic offline response is represented as the historical Provider response.",
            "The later R6 safe diagnostics are a new binding and do not retroactively identify the R5 failure.",
        ],
        "r5_attempt_remains_consumed": True,
        "no_r5_retry_or_replay": True,
        "offline_regression_evidence": {
            "test_files": ["tests/test_m1_chat_provider.py", "tests/test_m2_routing.py"],
            "result": "98 passed, 0 failed, 0 errors, 0 skipped",
            "log": "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029/logs/route-provider-r6-r3.log",
            "junit": "docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029/logs/route-provider-r6-r3.xml",
            "synthetic_cases_are_labeled_offline_only": True,
        },
    }
    p006_path = REPORTS / "product006-failure-analysis-r1.json"
    write_new(p006_path, json.dumps(p006_analysis, ensure_ascii=False, indent=2) + "\n")

    matrix = read("docs/eval/m6.2-r324-40case-personal-dev-batch-r1-20261029/all40-case-admission-matrix-r2.json")
    matrix_rows = {r["case_id"]: r for r in matrix["cases"]}
    r5app = read("docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/application-admission-offline-r1.json")
    r5app_rows = {r["case_id"]: r for r in r5app["records"]}
    r6ledger = read("docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029/reports/40-case-coverage-ledger-r6-r3.json")
    blocked = []
    for row in r6ledger["rows"]:
        status = row["final_status"]
        if status not in {"BLOCKED_ENGINEERING", "HELD_REVIEW_OR_AUTHORIZATION", "BLOCKED_CONTRACT_OR_DATA", "UNSCORABLE", "BLOCKED_APPLICATION_ADMISSION", "BLOCKED_REPOSITORY_PREFLIGHT"}:
            continue
        case_id = row["case_id"]
        source = matrix_rows.get(case_id, {})
        app = r5app_rows.get(case_id, {})
        if case_id in {"D1-AFTERSALES-019", "D1-AFTERSALES-020"}:
            reason = "真实应用离线执行路由为SINGLE/search_knowledge，漏掉允许范围内必需的get_order；需修正通用路由/组合能力契约后重新准入。"
            source_path = "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/application-admission-offline-r1.json"
        elif case_id == "D1-CROSSAGENT-032":
            reason = "用户输入及合法上游结果不能提供get_warranty所需product_id，且不允许用未授权search_products补齐；需数据/参数契约决定。"
            source_path = "docs/eval/m6.2-r324-40case-personal-dev-batch-r5-20261029/repository-admission-r4.json"
        else:
            reason = source.get("admission_reason")
            source_path = source.get("blocking_source")
        blocked.append({"case_id": case_id, "status": status, "reason": reason,
            "source": source_path, "actual_route": app.get("route") if app else None,
            "shortest_next_action": {
                "BLOCKED_ENGINEERING": "修复通用应用路由/适配器并按原能力重验。",
                "HELD_REVIEW_OR_AUTHORIZATION": "完成该案记录指定的复核/数据 steward 决定后再请求独立授权。",
                "BLOCKED_CONTRACT_OR_DATA": "补充经批准的参数来源或明确修订任务契约；不得猜值或扩权。",
                "UNSCORABLE": "等待业务负责人明确paid_at/退货资格规则，保留只读与未知边界。",
                "BLOCKED_APPLICATION_ADMISSION": "修复get_order必需能力的实际路由接线后重新离线准入。",
                "BLOCKED_REPOSITORY_PREFLIGHT": "需要允许能力内的稳定product_id上游来源或经审核的数据契约。",
            }.get(status, "按来源阻塞项处理。")})
    blockers_path = REPORTS / "12-excluded-case-dispositions-r1.json"
    write_new(blockers_path, json.dumps({"schema_version": "m6.2-r6-12-excluded-dispositions-r1",
        "count": len(blocked), "cases": blocked}, ensure_ascii=False, indent=2) + "\n")

    closeout = read("docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029/reports/closeout-r6-r3.json")
    cases = read("docs/eval/m6.2-r324-40case-personal-dev-batch-r6-20261029/reports/case-results-r6-r3.json")["cases"]
    ledger = closeout["execution"]
    table = []
    for row in cases:
        route = row.get("route", {})
        semantic = row.get("ai_semantic_pre_review", {})
        table.append(f"| {row['case_id']} | `{row['run_id']}` | {row['run_status']} | {route.get('strategy')}/{route.get('decision_source')} | {row['actual_provider_requests']} / {row['actual_read_only_calls']} / {row['tokens']} | {semantic.get('suggestion')} | {row.get('cleanup', {}).get('cleanup_status')} |")
    blocker_table = [f"| {b['case_id']} | {b['status']} | {b['reason']} | {b['shortest_next_action']} |" for b in blocked]
    md = f'''# M6.2 R6 余量批次收口

范围：个人 Dev，`PROVISIONAL_DEV_CHECKS` / `reference_unreviewed` / `NOT_GRADED`。M6.1 仍为 COMPLETE；不构成正式 Smoke、Gold、M2 或 Frozen 结果。

## R5 核验与范围

R5 批次 `{closeout['execution'].get('r5_batch_id', 'M62-R324-REMAINDER-R5-20261029-a5387a6411f2')}` 的授权保持 `CLOSED_STOPPED`。R5 冻结清单中的 22 个 `NOT_RUN_BATCH_STOP` 项逐项核对：state `attempt_count=0`，没有 R5 `CASE_CLOSEOUT` 事件或 R5 Run。完整 ID 在 `40-case-coverage-ledger-r6-r3.json`；R5 原件未改写。

R5 PRODUCT-006 是已执行失败案，保留其一次尝试。Provider 请求返回且账本记 859 tokens / 本地估算 USD 0.0010308；随后出现 `ValueError`，回退成 `DIRECT` 澄清，没有调用 `get_product_detail`。归档未保留原始模型响应，只留 `ValueError` 类型、FALLBACK 路由和 route_decided 事件。因此：网络失败不受证据支持；Provider JSON/schema、RouteDecision 字段校验、`_validate_classifier_decision` 语义契约的具体分支均为 UNKNOWN；能力越权未观察到；下游执行器未被进入。合成离线响应仅用于分支回归，不冒充历史响应。详见 `product006-failure-analysis-r1.json`。

## 离线修复与证据

- route/provider 安全阶段诊断、错误字段路径及用量关联已加入 R6 绑定；不记录密钥、完整响应或推理正文。路由版本为 `m2.1-route-v11`。
- PRODUCT-003 R5 答案与此前用户确认 FAIL 所绑定的完整答案逐字一致；要求也一致（需询问可用净深度和余量后再推荐适配）。R5 旧预审 PARTIAL 缺乏规则差异依据，派生建议更正为 FAIL；不复制旧确认作为 R5 人工确认。参见 `product003-r5-correction-r3.json`。
- 相关完整离线回归：`tests/test_m1_chat_provider.py tests/test_m2_routing.py` 为 98 passed；R6 应用准入/福利复合引用测试最终 2 passed， continuation/admission 筛选 3 passed。日志和 JUnit 保存在本目录 `logs/` 和 `tests-r6-*.xml`。失败迭代保留。
- R6 应用级离线准入 22/22 `APPLICATION_READY`，模型请求被拦截，真实外部调用 0；Repository/CLI 无模型预检读取 23 次。专用 SQLite identity `{closeout['personal_database_identity']}`。

## R6 实际执行

冻结批次 `{closeout['batch_id']}` 授权 `{closeout['authorization_id']}` 已 `CLOSED_STOPPED`。实际完成 5/22；在 POLICY-014 的交付/检查阻断后停止，余下 17 项均零尝试，不重试、不热修。

| 案例 | Run | Run状态 | 路由/来源 | Provider / 只读调用 / tokens | AI辅助预审（未确认） | 清理 |
|---|---|---|---|---:|---|---|
{chr(10).join(table)}

POLICY-014 的真实链路为规则 `SINGLE/BM25`，同一 Run 已有 `policy_retrieval_started/finished` 且来源/版本/索引信息落盘；Provider 回答随后被语义校验拒绝，归档没有候选答案或 assistant 最终输出。故任务交付 FAIL、候选答案内容不可评分。程序评分中的 route/audit/provider 项仍有 UNKNOWN，但离线报告可从同 Run 的 `route_decided` 事件恢复真实路由；此处记录为评分器/失败结果投影缺口，不据此判政策检索失败，也不将失败归因到模型幻觉。批次按冻结 stop rule 停止。

批次账本：Provider 3，业务只读调用 3，tokens 2,439，本地估算 USD 0.0029268，供应商账单 UNKNOWN_BILLING；未结 reservation 0，Judge/Embedding/写入/Pending Action 0。五个 Run 均终态并完成 receipt 精确清理，残余行 0。Dedicated SQLite 当前空闲。每案数据见 `case-results-r6-r3.json`，检查见 `scores/`，原始 archive 和 journal 均通过哈希重算。

## 40 案守恒

R6 仅将 R5 的22个未运行项中的5个标为 R6 已执行（其中 POLICY-014 触发停止），其余17为 `NOT_RUN_AFTER_R6_BATCH_STOP`；R4 POLICY-013、R5 五案和原12个阻塞项保持独立分类。40 案逐案台账及计数见 `40-case-coverage-ledger-r6-r3.json`，总数与分类和均为40。R4/R5/R6 不同代码/规则绑定的任务成功率不混算。

### 本轮未纳入的 12 项最短处置

| Case | 分类 | 依据/缺口 | 下一步最小动作 |
|---|---|---|---|
{chr(10).join(blocker_table)}

## 重算、人工确认和阶段状态

`scripts/recompute_m6_2_r6_closeout.py` 从锁定 manifest、R5/R6 journal、原始 archive、grader、授权及状态文件离线重算。本报告执行两次独立进程的 `--verify` 比较；完整结果见 `recompute-consistency-r6-r3.json`。R5 五案及 R4 POLICY-013、R6 五案的建议列入 `pending-user-review-r6-r3.json`；reviewer、时间和决定均为空，不是新人工确认。

M6.2仍 `IN_PROGRESS`：已有部分真实模型/规则路由与工具链证据，但本轮批次在5/22停止；Outcome复核待用户确认；PRODUCT-006根因仍因原响应未留存而 UNKNOWN；政策拒绝结果投影需保持评测器缺口；其余17案未运行。Judge缺适当人工负例和独立校准；路由/检索分母须按绑定分开；正式冻结审核、Registry/运行授权、Frozen Test及总报告仍未完成。M6.1 COMPLETE不变，正式 Gold/Registry/M2/Dev Smoke/Frozen 状态不变。

本目录 R1/R2 是保留的中间派生版本，R3 为完成 R5 22 案零尝试核验后的本轮最终版本。历史报告和 R5/R4 文件未被覆盖。
'''
    md_path = REPORTS / "closeout-r6-r3.md"
    write_new(md_path, md)
    print(json.dumps({"written": [str(p.relative_to(ROOT)) for p in (p006_path, blockers_path, md_path)],
                      "blocked_count": len(blocked), "execution_count": len(cases)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
