"""Build the final, still-human-reviewable M0 policy dataset candidate."""
from __future__ import annotations

import csv
import hashlib
import json
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "eval/dataset/policy-retrieval-v2-candidate-20261019-r3.json"
POLICY_FILE = ROOT / "knowledge/policies.json"
OUT = ROOT / "eval/dataset/policy-retrieval-v2-final-candidate-20261019-r7.json"
SOURCE_SUGGESTIONS = ROOT / "eval/review/policy-retrieval-v2-source-suggestions-final-r7.csv"
AUDIT_JSON = ROOT / "docs/probes/policy-retrieval-final-technical-audit-r7.json"
SPLIT_JSON = ROOT / "docs/probes/policy-retrieval-final-split-r7.json"
DETAIL_CSV = ROOT / "eval/review/policy-retrieval-final-audit-r7.csv"
SUMMARY_MD = ROOT / "eval/review/policy-retrieval-final-confirmation-r7.md"
VERSION = "policy-retrieval-v2-final-candidate-20261019-r7"

REFUND = "policies/refund@2026-01#before-shipping"
REFUND_UNIQUENESS = "policies/refund@2026-01#uniqueness"
RETURN_QUALITY = "policies/return-standard@2026-07#quality"
RETURN_NO_REASON = "policies/return-standard@2026-07#no-reason"
DELIVERY = "policies/delivery@2026-01#tracking"
CANCEL = "policies/cancel@2026-01#eligible"


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def combo_id(case: dict) -> str:
    ids = sorted(case.get("acceptable_evidence_ids") or case.get("possible_evidence_ids", []))
    return "pc-" + digest("|".join(ids).encode("utf-8"))[:12]


def c(cid: str, text: str, required: bool) -> dict:
    return {"condition_id": cid, "text": text, "required": required}


def j(text: str, evidence_ids: list[str], relation: str) -> dict:
    return {"condition": text, "evidence_ids": evidence_ids, "relation": relation}


def set_conditions(case: dict, conditions: list[dict], judgments: list[dict], gold: list[str], strategy: str, conclusion: str, source_types: dict[str, str], rationale: str) -> None:
    case["required_conditions"] = conditions
    case["evidence_judgments"] = judgments
    case["acceptable_evidence_ids"] = sorted(gold)
    case["expected_strategy"] = strategy
    case["expected_conclusion"] = conclusion
    case.setdefault("_final_source_suggestions", {}).update(source_types)
    case["_final_revision_reason"] = rationale


def revise_case(case: dict) -> None:
    group, tail = case["template_group"], case["case_id"][-2:]
    if group == "delivery_fact_boundary" and tail in {"02", "03", "04"}:
        case["expected_conclusion"] = {
            "02": "政策条文不能确认这笔订单是否已发货；应以查询时的运单事件核实。当前问题是在询问证据来源，并未要求实际查件；如需查询，须提供可识别的订单并完成属主授权。",
            "03": "知识库不能断定这个包裹的签收日期；具体签收时间应以该运单的物流事件为准。若要实际核实，需要订单标识和授权。",
            "04": "物流政策说明应以查询时的运单事件判断进度，但不等于这张运单当前的实时状态。实际状态须查询运单事件。",
        }[tail]
        case["_final_revision_reason"] = "按用户确认修正结论，使其分别对应发货状态、签收日期和实时运单状态；不把政策文本描述成已核实事实。"

    if group == "refund_delivered_return":
        context = {
            "01": "题面陈述商品已签收且质量有问题；这些个案事实未经业务系统核验。",
            "02": "题面称包裹已送达、退货审核已通过；送达与审核状态未经业务系统核验。",
            "03": "题面称商品已签收、退货仍在人工审核；这些个案事实未经业务系统核验。",
            "04": "题面称退货已批准且商品已签收；这些个案事实未经业务系统核验。",
        }[tail]
        rule = "已签收订单需要已批准的退货申请后，才能申请退款。"
        conclusion = {
            "01": "若题面所述退货申请确已获批，该已签收订单即可按政策提交退款申请；提交不代表真实到账。质量问题本身不使本题增加另一项退款前置规则。",
            "02": "若题面所述退货审核已通过，之后可提交退款申请；申请不代表真实到账。个案审核状态仍须由业务系统核实。",
            "03": "退货仍在人工审核不等于已批准，因此尚未满足已签收订单的退款申请前置条件。",
            "04": "若题面所述退货单确已批准，已签收订单满足该退款前置条件，可提交退款申请；提交不代表真实到账。",
        }[tail]
        set_conditions(case, [c("c1", context, False), c("c2", rule, True)], [j(context, [], "unknown"), j(rule, [REFUND], "supports")], [REFUND], "SINGLE", conclusion, {"c1": "USER_PROVIDED", "c2": "POLICY"}, "退款条款单独回答题目所问的退款前置顺序；退货质量时限不是这些问题的必要证据。")
        if "compound_reasoning" in case.get("tags", []):
            case["tags"].remove("compound_reasoning")

    if group in {"cancel_open_refund_conflict", "multi_clause_cancel_conflict"}:
        is_multiclause_template = group == "multi_clause_cancel_conflict"
        use_two = (tail == "04") if group == "cancel_open_refund_conflict" else tail in {"03", "04"}
        context_texts = {
            "cancel_open_refund_conflict": {
                "01": "题面称订单待发货且已有开放退款申请。",
                "02": "题面称同一订单退款申请正在处理；是否仍为开放状态应以业务记录核实。",
                "03": "题面称同一订单存在未结束的退款申请。",
                "04": "题面称订单未发货且已有退款申请正在处理。",
            },
            "multi_clause_cancel_conflict": {
                "01": "题面称待发货订单已有开放退款申请。",
                "02": "题面称同一订单已有退款处理中的记录。",
                "03": "题面设定取消会触发退款，且同一订单已有开放申请。",
                "04": "题面称同一订单已有未结退款记录，并询问取消是否会新建另一笔。",
            },
        }[group][tail]
        cancel_rule = "已有开放退款申请时，不得取消该订单。"
        conditions = [c("c1", context_texts, False), c("c2", cancel_rule, True)]
        judgments = [j(context_texts, [], "unknown"), j(cancel_rule, [CANCEL], "supports")]
        gold = [CANCEL]
        sources = {"c1": "USER_PROVIDED", "c2": "POLICY"}
        if use_two:
            uniqueness_rule = "同一订单任一时刻只允许一个开放的整单退款申请；不得因取消再产生第二笔开放申请。"
            conditions.append(c("c3", uniqueness_rule, True))
            judgments.append(j(uniqueness_rule, [REFUND_UNIQUENESS], "supports"))
            gold.append(REFUND_UNIQUENESS)
            sources["c3"] = "POLICY"
        conclusion = "题面所述退款若仍为开放申请，政策不允许继续取消该订单。"
        if use_two:
            conclusion += "此外，同一订单也不能再产生第二笔开放整单退款申请；执行前应以业务记录核实申请状态。"
        else:
            conclusion += "该结论由取消政策的前置限制即可确定，不需要额外检索退款唯一性条款。"
        set_conditions(case, conditions, judgments, gold, "MULTI" if use_two else "SINGLE", conclusion, sources, "按题目实际要求分别判断取消限制与重复退款限制；仅在题目明确要求同时判断两项约束时保留 MULTI。")
        if use_two:
            if "compound_reasoning" not in case.get("tags", []): case.setdefault("tags", []).append("compound_reasoning")
        elif "compound_reasoning" in case.get("tags", []):
            case["tags"].remove("compound_reasoning")

    # Normalize conditions that had mixed user-provided facts and policy rules.
    if group in {"return_custom_exception", "return_food_exception"}:
        category = "定制品" if group == "return_custom_exception" else "食品"
        fact = f"题面称商品为{category}；此为用户给定商品属性。"
        intent = "用户询问该商品能否无理由退货。"
        rule = f"{category}不适用无理由退货；七天时限不会覆盖该品类例外。"
        set_conditions(case, [c("c1", fact, False), c("c2", intent, False), c("c3", rule, True)], [j(fact, [], "unknown"), j(intent, [], "unknown"), j(rule, [RETURN_NO_REASON], "supports")], [RETURN_NO_REASON], "SINGLE", case["expected_conclusion"], {"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"}, "拆开题面商品属性/用户意图与政策品类例外；政策条款不证明个案商品属性。")

    if group in {"return_quality_conditions", "return_quality_expired"}:
        expired = group == "return_quality_expired"
        fact_problem = "题面称商品存在质量问题；此为用户给定事实。"
        fact_time = "题面称从签收到发现质量问题已超过30天。" if expired else "题面称质量问题发现时距签收未超过30天。"
        rule = "质量问题在签收后30天内可提交整单退货申请并进入人工审核；不保证批准。"
        conclusion = "超过30天不能按该条款直接判定可申请质量退货，应转人工核对保修。" if expired else "符合签收后30天窗口时可提交整单质量问题退货申请，由人工审核，不保证批准。"
        set_conditions(case, [c("c1",fact_problem,False),c("c2",fact_time,False),c("c3",rule,True)], [j(fact_problem,[],"unknown"),j(fact_time,[],"unknown"),j(rule,[RETURN_QUALITY],"supports")], [RETURN_QUALITY], "SINGLE", conclusion, {"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"}, "用户提供的商品状态/时间与政策30天规则分开；不把政策条款当作已核实签收时间。")

    if group == "refund_pre_ship":
        paid = "题面称订单已付款；未经业务系统核实。"
        unshipped = "题面称订单尚未发货；未经业务系统核实。"
        rule = "已付款且未发货的订单可申请整单退款；申请不代表真实到账。"
        set_conditions(case,[c("c1",paid,False),c("c2",unshipped,False),c("c3",rule,True)],[j(paid,[],"unknown"),j(unshipped,[],"unknown"),j(rule,[REFUND],"supports")],[REFUND],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"},"将题面订单状态与退款资格政策分开标注。")

    if group == "refund_shipped_unreceived":
        fact = "题面称订单已发货但尚未签收；未经物流系统核实。"
        distinction = "已发货未签收不等同于已付款未发货，不能套用后者的直接退款条件。"
        rule = "已发货未签收的退款申请默认转人工处理。"
        set_conditions(case,[c("c1",fact,False),c("c2",distinction,True),c("c3",rule,True)],[j(fact,[],"unknown"),j(distinction,[REFUND],"supports"),j(rule,[REFUND],"supports")],[REFUND],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"POLICY","c3":"POLICY"},"实际物流状态是用户陈述；退款分支和转人工要求来自退款政策。")

    if group == "refund_open_uniqueness":
        fact = "题面称同一订单已有未结束的整单退款申请；实际状态未经业务系统核实。"
        intent = "用户希望修改原因或再次提交退款申请。"
        rule = "同一订单任一时刻只允许一个开放整单退款申请，变更原因不会解除限制。"
        set_conditions(case,[c("c1",fact,False),c("c2",intent,False),c("c3",rule,True)],[j(fact,[],"unknown"),j(intent,[],"unknown"),j(rule,[REFUND_UNIQUENESS],"supports")],[REFUND_UNIQUENESS],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"},"区分个案退款状态/用户意图与唯一性规则。")

    if group == "refund_partial_unsupported":
        fact = "用户要求按商品或金额部分退款。"
        rule = "模拟政策仅支持整单申请，部分退款暂不支持。"
        set_conditions(case,[c("c1",fact,False),c("c2",rule,True)],[j(fact,[],"unknown"),j(rule,[REFUND_UNIQUENESS],"supports")],[REFUND_UNIQUENESS],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"POLICY"},"将用户退款诉求与退款政策能力边界区分。")

    if group in {"cancel_unshipped", "cancel_shipped"}:
        shipped = group == "cancel_shipped"
        fact = "题面称订单已发货；未经订单/物流系统核实。" if shipped else "题面称订单尚未发货；未经订单系统核实。"
        intent = "用户提出取消订单的请求。"
        rule = "已发货订单不可取消。" if shipped else "未发货订单可请求取消；实际执行前须用户确认。"
        set_conditions(case,[c("c1",fact,False),c("c2",intent,False),c("c3",rule,True)],[j(fact,[],"unknown"),j(intent,[],"unknown"),j(rule,[CANCEL],"supports")],[CANCEL],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"},"订单状态和取消意图属于题面上下文；可否取消及确认要求属于政策。")

    if group == "cancel_paid_refund":
        tail = case["case_id"][-2:]
        fact = "题面称订单已付款且尚未发货；未经业务系统核实。"
        intent = "用户请求取消该订单。"
        cancel_rule = "已付款订单取消时须同步创建整单退款申请，且取消前需要用户确认。"
        refund_rule = "已付款且未发货可申请整单退款；申请不代表真实到账。"
        needs_refund_rule = tail == "03"
        conditions=[c("c1",fact,False),c("c2",intent,False),c("c3",cancel_rule,True)]
        judgments=[j(fact,[],"unknown"),j(intent,[],"unknown"),j(cancel_rule,[CANCEL],"supports")]
        gold=[CANCEL]
        sources={"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"}
        if needs_refund_rule:
            conditions.append(c("c4",refund_rule,True)); judgments.append(j(refund_rule,[REFUND],"supports")); gold.append(REFUND); sources["c4"]="POLICY"
        strategy="MULTI" if needs_refund_rule else "SINGLE"
        conclusion={"01":"需要先确认取消；已付款订单取消时同步创建整单退款申请。","02":"执行取消前须用户确认；取消操作同步创建整单退款申请。","03":"取消成功只表示同步建立退款申请，不表示真实到账；退款政策明确申请不等于到账。","04":"不能跳过退款申请：已付款订单取消时必须同步建立申请，并须先确认。"}[tail]
        set_conditions(case,conditions,judgments,gold,strategy,conclusion,sources,"逐题判断政策覆盖：取消和确认要求由取消条款单独支持；仅询问申请是否等于到账的变体再需要退款条款。")
        if strategy=="SINGLE" and "compound_reasoning" in case.get("tags",[]): case["tags"].remove("compound_reasoning")

    if group == "multi_clause_cancel_refund":
        tail=case["case_id"][-2:]
        fact="题面称订单已付款且尚未发货；未经业务系统核实。"
        intent="用户请求取消订单并询问退款处理。"
        cancel_rule="已付款订单取消时须同步创建整单退款申请，且取消前需要用户确认。"
        refund_rule="退款申请不代表真实到账；已付款未发货订单符合整单退款申请条件。"
        needs_refund_rule=tail in {"02","03"}
        conditions=[c("c1",fact,False),c("c2",intent,False),c("c3",cancel_rule,True)]
        judgments=[j(fact,[],"unknown"),j(intent,[],"unknown"),j(cancel_rule,[CANCEL],"supports")]
        gold=[CANCEL]; sources={"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"}
        if needs_refund_rule:
            conditions.append(c("c4",refund_rule,True)); judgments.append(j(refund_rule,[REFUND],"supports")); gold.append(REFUND); sources["c4"]="POLICY"
        strategy="MULTI" if needs_refund_rule else "SINGLE"
        conclusion={"01":"取消前需用户确认，已付款取消时同步创建退款申请。","02":"取消操作须先确认并建立退款申请；申请建立不代表真实到账。","03":"取消规则要求同步建立整单退款申请，退款政策说明申请不等于到账。","04":"不能在已付款订单取消时省略退款申请；须先确认，随后按取消规则同步建申请。"}[tail]
        set_conditions(case,conditions,judgments,gold,strategy,conclusion,sources,"取消/确认由取消条款覆盖；仅问题涉及到账含义或明确要求合并解释退款政策时增加退款条款。")
        if strategy=="SINGLE" and "compound_reasoning" in case.get("tags",[]): case["tags"].remove("compound_reasoning")

    if group == "coupon_paid_version":
        fact="题面称订单已付款；未经业务系统核实。"
        rule="已付款订单按付款时有效的优惠券规则判断适用范围、门槛及版本。"
        tool="该订单的实际优惠券使用记录需由业务工具查询。"
        set_conditions(case,[c("c1",fact,False),c("c2",rule,True),c("c3",tool,False)],[j(fact,[],"unknown"),j(rule,["policies/coupon@2026-01#validity"],"supports"),j(tool,[],"unknown")],["policies/coupon@2026-01#validity"],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"POLICY","c3":"BUSINESS_TOOL"},"政策确定适用时间点；具体订单的用券记录是动态业务数据。")

    if group == "coupon_unpaid_querytime":
        fact="题面称订单尚未付款。"
        intent="用户在付款前咨询优惠券规则。"
        rule="未付款订单的优惠券问题按查询时有效规则说明，不追溯为已取得的订单权益。"
        set_conditions(case,[c("c1",fact,False),c("c2",intent,False),c("c3",rule,True)],[j(fact,[],"unknown"),j(intent,[],"unknown"),j(rule,["policies/coupon@2026-01#validity"],"supports")],["policies/coupon@2026-01#validity"],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"},"区分用户咨询情境与未付款政策规则。")

    if group == "multi_clause_category_time":
        category="题面称商品属于食品或定制品；此为用户提供的商品属性。"
        time_fact="题面称商品在签收后七天内；该时长未经订单时间核实。"
        rule="食品和定制商品不适用无理由退货，七天期限不会覆盖品类例外。"
        set_conditions(case,[c("c1",category,False),c("c2",time_fact,False),c("c3",rule,True)],[j(category,[],"unknown"),j(time_fact,[],"unknown"),j(rule,[RETURN_NO_REASON],"supports")],[RETURN_NO_REASON],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"USER_PROVIDED","c3":"POLICY"},"问题的两个前提来自用户，决定性例外规则由单一无理由退货条款支持。")

    if group == "refund_reason_hard_negative":
        fact="题面称退款原因不同或用户希望更改原因。"
        rule="同一订单任一时刻最多一个开放整单退款申请，原因不同不例外。"
        set_conditions(case,[c("c1",fact,False),c("c2",rule,True)],[j(fact,[],"unknown"),j(rule,[REFUND_UNIQUENESS],"supports")],[REFUND_UNIQUENESS],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"POLICY"},"原因差异属于问题前提，唯一性限制属于退款政策。")

    if group == "return_old_version":
        time_fact="题面指定查询时点为2026-07-01生效边界前。"
        old_rule="按查询时点选择当时有效的2026-01旧版退货条款。"
        no_mix="不能以之后生效的2026-07版追溯替代该时点的旧版。"
        evidence="policies/return-standard@2026-01#no-reason"
        set_conditions(case,[c("c1",time_fact,False),c("c2",old_rule,True),c("c3",no_mix,True)],[j(time_fact,[],"unknown"),j(old_rule,[evidence],"supports"),j(no_mix,[evidence],"supports")],[evidence],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"POLICY","c3":"POLICY"},"查询时间是案例前提；版本选择和不得追溯混用是政策版本判断。")

    if group == "return_exact_boundary":
        time_fact="题面/评估元数据给定时点为2026-07-01T00:00:00Z。"
        start_rule="新版effective_from包含该精确生效时刻。"
        end_rule="旧版effective_to不包含该精确时刻。"
        evidence="policies/return-standard@2026-07#no-reason"
        set_conditions(case,[c("c1",time_fact,False),c("c2",start_rule,True),c("c3",end_rule,True)],[j(time_fact,[],"unknown"),j(start_rule,[evidence],"supports"),j(end_rule,[evidence],"supports")],[evidence],"SINGLE",case["expected_conclusion"],{"c1":"USER_PROVIDED","c2":"POLICY","c3":"POLICY"},"把评估时点作为题目输入，按 UTC 左闭右开生效元数据判断版本。")

    if group == "warranty_specific_missing":
        case.setdefault("_final_source_suggestions", {}).update({"c1":"USER_PROVIDED","c2":"UNKNOWN","c3":"POLICY"})

    if group == "warranty_general":
        case.setdefault("_final_source_suggestions", {}).update({"c1":"POLICY","c2":"POLICY","c3":"POLICY"})

    if group == "delivery_policy_limits":
        tail_text = {
            "01": "题面询问政策能否证明这笔订单昨天签收且仍在退货期限内。",
            "02": "题面询问核算签收后退货窗口所缺少的订单事实。",
            "03": "题面询问普通商品无理由退货期限及具体包裹签收时间的核实来源。",
            "04": "题面询问政策能否替代物流查询确认签收日，以及退货期限从何时起算。",
        }[tail]
        rule = "普通商品无理由退货申请窗口为签收后7×24小时，且商品未影响二次销售。"
        source_rule = "政策文档不能证明具体订单的签收日期；物流进度以查询时的运单事件为准。"
        unknown = "该订单的实际签收时间尚未由订单或物流记录核实；如需判断是否在窗口内，须通过授权业务查询获取。"
        conclusions = {
            "01": "政策只能说明普通商品的无理由退货窗口为签收后7×24小时，不能证明这笔订单昨天签收。当前无法判断是否仍在期限内；需要该订单的运单签收事件及订单标识/授权。",
            "02": "缺少该订单经物流记录核实的实际签收时间；政策规定签收后7×24小时的窗口，但不能提供个案签收时点。",
            "03": "普通商品无理由退货窗口从签收起算，为7×24小时且商品未影响二次销售；具体包裹签收时间应查该运单事件。",
            "04": "政策条文不能替代物流查询证明签收日；普通商品窗口从签收后7×24小时内计算，个案日期需通过运单事件核实。",
        }
        conditions = [c("c1", rule, True), c("c2", source_rule, True), c("c3", unknown, False)]
        judgments = [j(rule, [RETURN_NO_REASON], "supports"), j(source_rule, [DELIVERY], "supports"), j(unknown, [], "unknown")]
        set_conditions(case, conditions, judgments, [RETURN_NO_REASON, DELIVERY], "MULTI", conclusions[tail], {"c1": "POLICY", "c2": "POLICY", "c3": "BUSINESS_TOOL"}, "分别保留退货时间规则与物流事实来源边界；实际签收时点是未核实的业务事实，不作为政策证据。")


def load_old_suggestions() -> dict[tuple[str, str, str], dict]:
    path = ROOT / "eval/review/policy-retrieval-v2-source-suggestions-r3.csv"
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return {(r["case_id"], r["condition_id"], r["condition_text"]): r for r in csv.DictReader(f)}


def main() -> int:
    for path in (OUT, SOURCE_SUGGESTIONS, AUDIT_JSON, SPLIT_JSON, DETAIL_CSV, SUMMARY_MD):
        if path.exists(): raise FileExistsError(f"refusing to overwrite {path}")
    source_bytes = SOURCE.read_bytes()
    payload = json.loads(source_bytes)
    old_suggestions = load_old_suggestions()
    before_by_id = {x["case_id"]: deepcopy(x) for x in payload["cases"]}
    payload["dataset_version"] = VERSION
    payload["revision_parent"] = "policy-retrieval-v2-candidate-20261019-r3"
    now = datetime.now(timezone.utc).isoformat()

    for case in payload["cases"]:
        revise_case(case)

    # Route corrections create shared single-clause evidence bundles. Move the
    # complete connected bundles together to keep exact evidence groups isolated.
    to_validation = {"delivery_policy_limits"}
    to_test = {"cancel_shipped", "cancel_unshipped", "cancel_paid_refund", "multi_clause_cancel_refund", "cancel_open_refund_conflict", "multi_clause_cancel_conflict"}
    for case in payload["cases"]:
        if case["template_group"] in to_validation: case["dataset_split"] = "validation"
        elif case["template_group"] in to_test: case["dataset_split"] = "test_candidate"
        case["test_candidate_only"] = case["dataset_split"] == "test_candidate"
        case["frozen"] = False
        case["policy_combination_group"] = combo_id(case)
        case["review_status"] = "PENDING_REVIEW"
        case["reviewer"] = None
        case["reviewed_at"] = None

    # Version every changed row; human review history is untouched.
    changed = []
    moved_groups = to_validation | to_test
    for case in payload["cases"]:
        before = before_by_id[case["case_id"]]
        changed_fields = {}
        keys = ("required_conditions", "evidence_judgments", "acceptable_evidence_ids", "expected_strategy", "expected_conclusion", "expected_tool_calls", "expected_intent", "business_scenario", "tags", "dataset_split", "test_candidate_only", "policy_combination_group")
        for key in keys:
            if before.get(key) != case.get(key): changed_fields[key] = {"before": before.get(key), "after": case.get(key)}
        if changed_fields:
            reason = case.get("_final_revision_reason") or ("按完整证据组合移动模板组，保持模板组与证据组合隔离。" if case["template_group"] in moved_groups else "最终技术审查发现并修正明确的标签/证据问题。")
            event = {"revision_id": "m0-policy-final-r7", "event_type": "AI_AUDITED_CANDIDATE_REVISION", "created_at_utc": now, "actor": "AI technical review; not human approval", "human_review_required": True, "approval_status": "PENDING_REVIEW", "reason": reason, "changes": changed_fields}
            case.setdefault("annotation_revision_history", []).append(event)
            changed.append({"case_id": case["case_id"], "template_group": case["template_group"], "before": {k: v["before"] for k,v in changed_fields.items()}, "after": {k: v["after"] for k,v in changed_fields.items()}, "reason": reason})

    # Create per-case technical review annotations and non-authoritative source suggestions.
    source_rows = []
    detail_rows = []
    cases_audit = []
    policy_docs = json.loads(POLICY_FILE.read_text(encoding="utf-8"))["documents"]
    policy_by_id = {}
    for doc in policy_docs:
        for clause in doc["clauses"]:
            policy_by_id[f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"] = doc
    policy_ids = set(policy_by_id)
    for case in payload["cases"]:
        suggestions = {}
        local_reason = next((item["reason"] for item in changed if item["case_id"] == case["case_id"]), "逐题核对问题、条件、结论、证据 ID、生效时间和策略路由；未发现本轮指令可直接证实的额外错误。")
        for condition in case["required_conditions"]:
            cid, text = condition["condition_id"], condition["text"]
            manual = case.get("_final_source_suggestions", {}).get(cid)
            previous = old_suggestions.get((case["case_id"], cid, text))
            if manual: source, rationale = manual, local_reason
            elif previous: source, rationale = previous["suggested_source_type"], previous["rationale"]
            else: source, rationale = "UNKNOWN", "条件语义或来源无法由当前案例和政策原文确定，需人工确认。"
            suggestions[cid] = source
            source_rows.append({"case_id": case["case_id"], "template_group": case["template_group"], "dataset_split": case["dataset_split"], "condition_id": cid, "condition_text": text, "required": condition["required"], "suggested_source_type": source, "rationale": rationale, "review_status": "PENDING_HUMAN_REVIEW", "authoritative": False})
        unresolved = case.get("scoreability") != "SCORABLE_CANDIDATE"
        case["technical_review"] = {"status": "AI_AUDITED_CANDIDATE", "reviewed_at_utc": now, "human_approval": False, "condition_source_suggestions": suggestions, "review_basis": ["query semantics and required-condition mapping", "policy source text and version validity at evaluation_at", "evidence IDs and judgments", "SINGLE/MULTI strategy and tool boundary", "template/evidence-combination split isolation"], "technical_findings": [], "human_attention": "PENDING_REVIEW" if unresolved else "HUMAN_CONFIRMATION_REQUIRED", "human_attention_reason": case.get("unscorable_reason") if unresolved else "人工需确认内容、来源建议及隔日盲审；AI审查不等于正式签署。", "decision": "AI_AUDITED_CANDIDATE_NOT_APPROVED"}
        evidence_check = all(eid in policy_ids for eid in case.get("acceptable_evidence_ids", []) + case.get("possible_evidence_ids", []) + case.get("hard_negative_evidence_ids", []))
        eval_at = datetime.fromisoformat(case["evaluation_at"].replace("Z", "+00:00"))
        active_check = True
        for eid in case.get("acceptable_evidence_ids", []) + case.get("hard_negative_evidence_ids", []):
            doc = policy_by_id.get(eid)
            if doc is None or not (datetime.fromisoformat(doc["effective_from"].replace("Z", "+00:00")) <= eval_at and (doc["effective_to"] is None or eval_at < datetime.fromisoformat(doc["effective_to"].replace("Z", "+00:00")))):
                active_check = False
        condition_texts = {x["text"] for x in case["required_conditions"]}
        judgment_texts = {x["condition"] for x in case.get("evidence_judgments", [])}
        cases_audit.append({"case_id": case["case_id"], "template_group": case["template_group"], "dataset_split": case["dataset_split"], "expected_strategy": case["expected_strategy"], "scoreability": case["scoreability"], "review_status": case["review_status"], "technical_status": case["technical_review"]["status"], "source_type_suggestions": suggestions, "checks": {"condition_judgment_coverage": condition_texts == judgment_texts, "all_evidence_ids_exist": evidence_check, "gold_and_hard_negative_active_at_evaluation_time": active_check, "source_suggestion_covers_conditions": set(suggestions) == {x["condition_id"] for x in case["required_conditions"]}, "valid_strategy_enum": case["expected_strategy"] in ("SINGLE", "MULTI"), "human_status_pending": case["review_status"] == "PENDING_REVIEW"}, "findings": changed_by_id(case["case_id"], changed), "human_attention": case["technical_review"]["human_attention_reason"]})
        detail_rows.append({"case_id": case["case_id"], "template_group": case["template_group"], "dataset_split": case["dataset_split"], "user_query": case["user_query"], "expected_strategy": case["expected_strategy"], "scoreability": case["scoreability"], "acceptable_evidence_ids": json.dumps(case["acceptable_evidence_ids"], ensure_ascii=False), "condition_source_suggestions": json.dumps(suggestions, ensure_ascii=False), "technical_status": "AI_AUDITED_CANDIDATE", "human_attention": case["technical_review"]["human_attention"], "review_status": "PENDING_REVIEW", "technical_note": local_reason})

    for case in payload["cases"]:
        case.pop("_final_source_suggestions", None)
        case.pop("_final_revision_reason", None)

    # Add final version provenance while keeping all samples formally pending.
    payload["dataset_version"] = VERSION
    payload["status"] = "CANDIDATE_PENDING_HUMAN_REVIEW_NOT_FROZEN"
    payload["technical_review_status"] = "AI_AUDITED_CANDIDATE"
    payload["technical_review_notice"] = "All 112 rows received AI technical review. This is not a human signature, APPROVED status, blind review, or test freeze."
    payload.setdefault("annotation_revision_history", []).append({"revision_id": "m0-policy-final-r7", "event_type": "FINAL_AI_TECHNICAL_REVIEW", "created_at_utc": now, "source_dataset_version": "policy-retrieval-v2-candidate-20261019-r3", "source_dataset_sha256": digest(SOURCE.read_bytes()), "human_approval": False, "frozen": False, "changed_case_count": len(changed), "reviewed_case_count": len(cases_audit), "split_moves": {"validation": sorted(to_validation), "test_candidate": sorted(to_test)}, "reason": "Apply user-approved technical dispositions, correct condition provenance and strategy over-labeling, and assign complete evidence bundles to final candidate splits."})
    out_bytes = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    out_hash = digest(out_bytes)
    OUT.write_bytes(out_bytes)

    with SOURCE_SUGGESTIONS.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(source_rows[0]))
        writer.writeheader(); writer.writerows(source_rows)

    split_combos: dict[tuple[str, ...], set[str]] = defaultdict(set)
    group_splits: dict[str, set[str]] = defaultdict(set)
    combo_to_groups: dict[tuple[str, ...], set[str]] = defaultdict(set)
    split_cases = defaultdict(list)
    for case in payload["cases"]:
        split = case["dataset_split"]
        split_cases[split].append(case)
        group_splits[case["template_group"]].add(split)
        ids = tuple(sorted(case.get("acceptable_evidence_ids") or case.get("possible_evidence_ids", [])))
        if ids: split_combos[ids].add(split); combo_to_groups[ids].add(case["template_group"])
    leaks = [{"evidence_ids": list(k), "splits": sorted(v), "template_groups": sorted(combo_to_groups[k])} for k,v in split_combos.items() if len(v)>1]
    group_leaks = [{"template_group": k, "splits": sorted(v)} for k,v in group_splits.items() if len(v)>1]
    split_summary = {s: {"cases": len(rows), "template_groups": len({x["template_group"] for x in rows}), "strategies": dict(Counter(x["expected_strategy"] for x in rows)), "scorable_strategies": dict(Counter(x["expected_strategy"] for x in rows if x["scoreability"] == "SCORABLE_CANDIDATE")), "unscorable_cases": sum(x["scoreability"] != "SCORABLE_CANDIDATE" for x in rows), "hard_negative_cases": sum(bool(x.get("hard_negative_evidence_ids")) for x in rows)} for s,rows in split_cases.items()}
    split_report = {"report_type": "final_policy_dataset_split", "dataset_version": VERSION, "dataset_sha256": out_hash, "split_summary": split_summary, "template_group_leakage": group_leaks, "policy_combination_leakage": leaks, "moves": {"dev_to_validation": sorted(to_validation), "dev_to_test_candidate": sorted(to_test)}, "validation_has_scorable_multi": split_summary["validation"]["scorable_strategies"].get("MULTI",0)>0, "test_candidate_used_for_tuning": False, "status": "PASS" if not group_leaks and not leaks and split_summary["validation"]["scorable_strategies"].get("MULTI",0)>0 else "FAIL", "note": "All split changes are candidate assignments only; test_candidate is not frozen and must not be used for tuning."}
    SPLIT_JSON.write_text(json.dumps(split_report, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")

    checks_ok = all(all(x["checks"].values()) for x in cases_audit)
    audit_report = {"report_type":"final_policy_dataset_ai_technical_audit","dataset_version":VERSION,"dataset_sha256":out_hash,"source_dataset_version":"policy-retrieval-v2-candidate-20261019-r3","source_dataset_sha256":digest(source_bytes),"created_at_utc":now,"case_count":len(cases_audit),"template_group_count":len({x['template_group'] for x in cases_audit}),"ai_audited_candidate_count":len(cases_audit),"formal_human_approved_count":0,"review_status_counts":dict(Counter(x["review_status"] for x in cases_audit)),"human_attention_case_count":sum(x["scoreability"] != "SCORABLE_CANDIDATE" for x in cases_audit),"changed_case_count":len(changed),"changed_cases":changed,"source_suggestion_count":len(source_rows),"source_suggestion_counts":dict(Counter(x["suggested_source_type"] for x in source_rows)),"condition_judgment_mismatches":[x['case_id'] for x in cases_audit if not x['checks']['condition_judgment_coverage']],"evidence_id_failures":[x['case_id'] for x in cases_audit if not x['checks']['all_evidence_ids_exist']],"invalid_strategies":[x['case_id'] for x in cases_audit if not x['checks']['valid_strategy_enum']],"case_reviews":cases_audit,"status":"PASS_TECHNICAL_AUDIT_NO_HUMAN_APPROVAL" if checks_ok else "FAIL","notice":"AI review is a technical candidate audit only. It is not a human annotation, signature, overnight blind review, approval, or freeze."}
    AUDIT_JSON.write_text(json.dumps(audit_report,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    with DETAIL_CSV.open("x",encoding="utf-8-sig",newline="") as f:
        writer=csv.DictWriter(f,fieldnames=list(detail_rows[0])); writer.writeheader(); writer.writerows(detail_rows)
    summary = f"""# M0 政策检索最终技术审查摘要（r7 候选）

- 数据集：`{VERSION}`；SHA-256：`{out_hash}`；112 条、28 个模板组；全部 `PENDING_REVIEW`，人工批准 0，未冻结。
- 技术审查：112/112 条标记 `AI_AUDITED_CANDIDATE`。逐题核查明细见 `policy-retrieval-final-audit-r7.csv`，结构化审查记录见 `docs/probes/policy-retrieval-final-technical-audit-r7.json`。来源建议均为非权威，待人工确认。
- 明确修订：高风险 8 题及四个指定组完成条件来源、证据归因与结论复核；多个退货/退款、取消场景逐题降为 SINGLE，仅当问题确实跨独立政策规则时保留 MULTI。
- 路由：`cancel_open_refund_conflict` 为 SINGLE 3、MULTI 1；`multi_clause_cancel_conflict` 为 SINGLE 2、MULTI 2；`cancel_paid_refund` 为 SINGLE 3、MULTI 1；`multi_clause_cancel_refund` 为 SINGLE 2、MULTI 2。
- 划分：取消场景因修订后多个模板组共享单条取消政策证据，完整取消证据连通组整体移入 test_candidate；`delivery_policy_limits` 完整组移入 validation，提供可评分 MULTI，退款单证据组留在 dev。分布与隔离见 `docs/probes/policy-retrieval-final-split-r7.json`。
- 仍需用户正式确认：所有 112 条来源类型和金标、8 条不可评分样本的独立澄清/证据不足评分口径，以及最终 split 设计。8 条不可评分样本继续排除普通检索 Recall/MRR。
- 人工审核 CSV 和 28 组政策原文材料由 `scripts/policy_review.py prepare --dataset eval/dataset/policy-retrieval-v2-final-candidate-20261019-r7.json --review-tag r11` 生成。只有真实审核者填写身份、时间、依据后才可导入；隔日盲审需实际等待并由用户执行。
"""
    SUMMARY_MD.write_text(summary,encoding="utf-8")
    print(json.dumps({"dataset":str(OUT),"sha256":out_hash,"cases":len(cases_audit),"changed":len(changed),"split_status":split_report["status"],"splits":split_summary,"audit_status":audit_report["status"],"review_status_counts":audit_report["review_status_counts"],"outputs":[str(SOURCE_SUGGESTIONS),str(AUDIT_JSON),str(SPLIT_JSON),str(DETAIL_CSV),str(SUMMARY_MD)]},ensure_ascii=False,indent=2))
    return 0 if audit_report["status"].startswith("PASS") and split_report["status"]=="PASS" else 1


def changed_by_id(case_id: str, changed: list[dict]) -> list[dict]:
    return [x for x in changed if x["case_id"] == case_id]


if __name__ == "__main__":
    raise SystemExit(main())
