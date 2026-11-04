"""Build a reviewable, unfrozen policy retrieval candidate set from policy text."""
from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "knowledge/policies.json"
OUT = ROOT / "eval/dataset/policy-retrieval-v2-candidate.json"
CSV_OUT = ROOT / "eval/review/policy-retrieval-v2-review.csv"

# Each question is deliberately authored as a separate candidate. Variants in a
# group share one split; they must never be scattered across splits.
GROUPS = [
    ("return_basic_current", "test_candidate", "direct_policy", False, ["policies/return-standard@2026-07#no-reason"], ["普通商品范围", "签收后七天内", "商品未影响二次销售"], "符合条件时可提交无理由退货申请，提交不等于批准。", [
        "普通商品收货后七天之内且没有影响二次销售，能不能走无理由退货？", "签收未满七天的普通商品，保持完好时是否可以申请无理由退货？", "我买的不是定制品也不是食品，签收六天、未拆坏，政策允许申请退吗？", "一般商品刚签收几天，外观和二次销售状态都没受影响，可以申请七天无理由吗？"]),
    ("return_custom_exception", "test_candidate", "category_exception", True, ["policies/return-standard@2026-07#no-reason"], ["商品为定制品", "请求无理由退货", "品类例外优先于七天时限"], "定制商品不适用无理由退货；不能仅凭七天时限判定可申请。", [
        "定制的纪念品刚收到三天，东西完好，可以按七天无理由退吗？", "虽然还在签收后一周内，但定做的商品不想要了能无理由申请吗？", "刻字定制品符合时间和完好条件，品类限制是否仍会挡住无理由退货？", "个性化定制商品没有质量问题，七天期限内能不能申请普通无理由退货？"]),
    ("return_food_exception", "test_candidate", "category_exception", True, ["policies/return-standard@2026-07#no-reason"], ["商品为食品", "请求无理由退货", "品类例外独立于签收时间"], "食品不适用无理由退货。", [
        "食品签收两天、包装没有明显破损，可以不说明原因退吗？", "买的是零食，仍在七天之内，能照普通商品的规则申请无理由退货吗？", "食品类商品符合未影响二次销售的描述，是否因此就能无理由退？", "对于已签收的食品，普通七天退货时限是否适用？"]),
    ("return_quality_conditions", "validation", "multi_condition_policy", True, ["policies/return-standard@2026-07#quality"], ["存在质量问题", "签收不超过30天", "仅可提交审核申请，不承诺批准"], "符合30天窗口可提交整单质量问题退货申请，由人工审核。", [
        "定制电器签收二十多天后发现故障，还能提交质量问题退货审核吗？", "食品有质量问题且收货不到30天，政策允许申请退货吗，能保证通过吗？", "普通商品超过七天但未满三十天出现质量问题，处理方式是什么？", "商品签收第29天才发现质量问题，是否还能申请整单退货，结果会自动批准吗？"]),
    ("return_quality_expired", "validation", "time_boundary", True, ["policies/return-standard@2026-07#quality"], ["质量问题", "签收已超过30天", "当前条款要求转人工核对保修"], "超过30天不能按该条款直接判定可申请质量退货，应转人工核对保修。", [
        "签收一个多月才发现故障，质量问题退货政策还能直接适用吗？", "商品第31天出现质量问题，是否仍可按30天退货流程提交？", "发现瑕疵时已经过了签收后三十天，应继续申请退货还是找人工核保修？", "超过质量问题三十天窗口后，模拟政策要求客服如何处理？"]),
    ("refund_pre_ship", "dev", "refund_state", False, ["policies/refund@2026-01#before-shipping"], ["已付款", "尚未发货", "整单退款申请不代表真实到账"], "可以申请整单退款；申请不等于退款到账。", [
        "订单已经付款但仓库还没发出，能申请整单退款吗？", "已支付的订单状态仍是未发货，政策允许提出全额退款申请吗？", "付款完成、物流尚无发货记录时，退款申请可以提交到哪一步？", "尚未发货的已付款订单能不能申请整单退，是否等同于钱已退回？"]),
    ("refund_delivered_return", "dev", "multi_clause", True, ["policies/refund@2026-01#before-shipping", "policies/return-standard@2026-07#quality"], ["订单已签收", "退货申请已获批准", "之后才可申请退款"], "已签收订单需先有已批准的退货申请，再申请退款。", [
        "签收的商品有质量问题，退货申请获批之后能否再申请退款？", "包裹已送达且售后退货审核通过，退款步骤是否符合政策？", "商品已签收但退货还在人工审核，是否已经满足退款前置条件？", "已批准的退货单和已签收状态同时存在时，退款资格如何判断？"]),
    ("refund_shipped_unreceived", "dev", "state_exception", True, ["policies/refund@2026-01#before-shipping"], ["已发货但未签收", "不同于未发货退款", "默认转人工"], "已发货未签收默认转人工，不能套用未发货退款条件。", [
        "物流显示已发出但我还没签收，能直接按未发货订单申请退款吗？", "包裹在运输途中，未送到买家手上，退款应自动提交还是转人工？", "订单已经出库揽收但没有签收，这种状态是否满足未发货退款政策？", "已发运未妥投的订单，政策对退款申请有什么特别处理？"]),
    ("refund_open_uniqueness", "dev", "uniqueness", True, ["policies/refund@2026-01#uniqueness"], ["同一订单已有开放整单退款申请", "变更退款原因", "仍不可重复提交"], "同订单最多一个开放整单退款申请；修改原因不产生第二个名额。", [
        "我的整单退款还在处理中，改一个理由能再开一笔吗？", "相同订单有未结束的退款申请，换成质量原因是否可以重复提交？", "退款申请尚未关闭，只修改申请说明能否生成第二个开放申请？", "同一订单的退款理由不同，是否能绕过开放申请唯一限制？"]),
    ("refund_partial_unsupported", "dev", "unsupported_scope", False, ["policies/refund@2026-01#uniqueness"], ["用户要求部分退款", "模拟政策只说明整单申请且部分退款暂不支持"], "部分退款暂不支持；需说明限制并转人工核对，而非伪造适用政策。", [
        "订单里只想退其中一件的金额，当前模拟政策能办理部分退款吗？", "同单买了多件，我只申请其中一件退款是否属于已支持范围？", "可不可以把整单退款拆成一个商品的小额退款申请？", "我的诉求是按商品行退款，不退整笔订单，政策是否支持？"]),
    ("cancel_unshipped", "dev", "cancellation", True, ["policies/cancel@2026-01#eligible"], ["订单尚未发货", "请求取消", "用户确认是实际动作前提"], "未发货订单可请求取消；执行前需要用户确认。", [
        "订单还没发出，我想取消；政策是不是允许提出申请？", "未发货的订单可以取消吗，是否还需要我确认一次？", "付款后尚未发货，取消规则和确认要求分别是什么？", "商品还在待发货状态，能否取消订单而不是申请退货？"]),
    ("cancel_shipped", "dev", "cancellation_exception", False, ["policies/cancel@2026-01#eligible"], ["订单已发货", "请求取消", "已发货不可取消"], "已发货订单不能取消，应走退货或人工处理。", [
        "物流已经揽收了，我现在还能取消订单吗？", "订单从仓库发出以后，取消按钮是否还符合模拟政策？", "已发货的商品不想要了，应该继续取消还是咨询退货流程？", "包裹在途中，能否按未发货订单那样撤销？"]),
    ("cancel_paid_refund", "dev", "multi_clause", True, ["policies/cancel@2026-01#eligible", "policies/refund@2026-01#before-shipping"], ["订单未发货", "订单已经付款", "取消与整单退款同事务关联", "用户确认后执行"], "已付款未发货订单可请求取消；取消时应同步创建整单退款申请并先确认。", [
        "已付款但未发货的订单要取消，退款申请是不是也要一起建立？", "我确认想取消一个已付款的待发货订单，政策要求同时处理什么？", "订单已支付且尚未出库，取消成功是否自动意味着钱到账？", "取消已付款订单时，退款申请与人工确认之间是什么关系？"]),
    ("cancel_open_refund_conflict", "dev", "multi_clause", True, ["policies/cancel@2026-01#eligible", "policies/refund@2026-01#uniqueness"], ["已有开放退款申请", "订单未发货", "取消与新退款申请不得冲突"], "已有开放退款申请时不得继续取消订单。", [
        "待发货订单已有一笔开放退款，我又提出取消，是否还能继续执行？", "退款申请处理中时再取消同一订单，会不会违反政策？", "同一订单已存在未结束的退款申请，取消操作是否应被拒绝？", "订单未发货但退款已经在办，系统可否再创建取消和另一笔退款？"]),
    ("delivery_policy_limits", "dev", "evidence_boundary", True, ["policies/delivery@2026-01#tracking", "policies/return-standard@2026-07#no-reason"], ["政策规定物流事实来源", "退货期限从签收起算", "文档不能证明具体订单签收时间"], "需查询实时物流事件才能证明签收日期；政策文档只给出期限规则。", [
        "政策里写了七天退货，能否据此证明我的订单昨天签收并仍在期限内？", "我想核算签收后退货窗口，但手里只有政策，缺哪项订单事实？", "普通商品无理由期限如何算，以及具体包裹签收时间应从哪里核实？", "政策条文可以替代物流查询来确认签收日吗，退货时间又从何时起算？"]),
    ("coupon_paid_version", "test_candidate", "version_condition", True, ["policies/coupon@2026-01#validity"], ["订单已付款", "优惠券资格按付款时有效版本", "还要核对订单使用记录"], "已付款订单按付款时有效政策和实际使用记录核对优惠券。", [
        "订单支付后再咨询优惠券，应该按付款当时规则还是今天规则核对？", "已经付款的订单优惠券是否仅看当前政策，还是要回到支付时版本？", "核对已支付订单能否使用某券，需要同时查当时规则和什么记录？", "付款完成后政策版本变化，优惠券适用条件按哪个时点判断？"]),
    ("coupon_unpaid_querytime", "test_candidate", "version_condition", False, ["policies/coupon@2026-01#validity"], ["订单尚未付款", "仅作咨询说明", "未付款按查询时版本"], "未付款订单咨询优惠券时说明查询时规则，不追溯为订单权益。", [
        "还没下单付款，只是问现在优惠券怎么用，按哪个时间的规则回答？", "未付款前咨询券门槛，是否应该采用目前生效的版本？", "我尚未支付，今天查询优惠券的适用范围应取哪个时点？", "没有付款记录的优惠券问题可以直接按下单日政策推定吗？"]),
    ("warranty_general", "validation", "direct_policy", False, ["policies/warranty@2026-01#general"], ["普通电器", "付款起一年", "耗材和人为损坏例外"], "普通电器可在付款起一年内提交保修咨询；耗材与人为损坏不在模拟范围。", [
        "普通电器从付款起保修多久，人为摔坏是否也包含？", "电器保修期按付款日算吗，耗材能不能按同一规则保修？", "模拟保修政策对普通电器、耗材和人为损坏分别怎么规定？", "如果不是人为损坏，普通电器在付款一年内能提交保修咨询吗？"]),
    ("warranty_specific_missing", "validation", "insufficient_evidence", True, ["policies/warranty@2026-01#general"], ["问题要求具体型号/部件承诺", "通用条款不足以确认", "应转人工核对"], "缺少具体商品条款时转人工核对，不能从通用政策推断型号承诺。", [
        "这款型号X的电池到底保几年？通用电器条款能确定吗？", "某品牌配件的保修年限没有单独条款，客服可以承诺两年吗？", "页面未提供我这款商品的具体保修条款，能否仅凭普通电器规则确认？", "耗材的专属质保期限在知识库中缺失时，应给确定期限还是转人工？"]),
    ("delivery_fact_boundary", "test_candidate", "high_lexical_hard_negative", False, ["policies/delivery@2026-01#tracking"], ["用户问具体发货/签收事实", "知识库不能证明订单动态状态"], "必须查询物流事件；政策文本不能证明某订单已发货、签收或预计送达。", [
        "物流政策有写时效，能证明我的包裹今天已经送达吗？", "想知道订单是否已发货，搜政策条款是否足够？", "客服能不能从知识库断定我的包裹几号签收？", "关于物流进度的规定是否等于这张运单的实时状态？"]),
    ("return_old_version", "validation", "historical_version", False, ["policies/return-standard@2026-01#no-reason"], ["评估时间在2026-07-01之前", "查询旧版退货条款", "不得混用现行版"], "在2026-07-01前按旧版条款检索；不能以当前版本替代。", [
        "按2026年6月30日当时的规则，普通商品七天无理由条款是什么？", "查询六月底有效的旧版退货政策，不要引用七月新版本。", "我需要核对2026-06-30仍生效的普通商品退货条款。", "政策变更日前一天的退货问答应匹配哪个版本？"]),
    ("return_exact_boundary", "test_candidate", "version_boundary", True, ["policies/return-standard@2026-07#no-reason"], ["时点恰为2026-07-01T00:00:00Z", "effective_from包含", "旧版effective_to不包含"], "边界时刻由2026-07版生效，旧版不再适用。", [
        "在UTC时间2026-07-01零点整查询，退货规则用旧版还是新版本？", "恰好到七月一日生效边界时，哪份退货政策可用于检索？", "effective_to为7月1日00:00且effective_from同一时刻，边界归哪个版本？", "变更瞬间发起的政策查询，是否仍能命中截至该时刻结束的旧版？"]),
    ("refund_reason_hard_negative", "dev", "high_lexical_hard_negative", False, ["policies/refund@2026-01#uniqueness"], ["原因不同", "同订单任一时刻最多一个开放整单退款"], "不同原因不改变开放退款唯一约束。", [
        "同一订单退货理由换了，是否就能新建第二个退款申请？", "原因不同的两笔整单退款，能同时保持开放状态吗？", "客户修改退款原因后，原开放申请还没结束，可否另外提交？", "退款诉求内容变化是否会解除同一订单的开放申请限制？"]),
    ("multi_clause_return_refund", "dev", "multi_clause", True, ["policies/return-standard@2026-07#quality", "policies/refund@2026-01#before-shipping"], ["商品已签收", "质量问题在30天内", "退货须获批后才能申请退款"], "先依据质量问题条款提交并获批退货，再按退款条款申请退款。", [
        "签收20天的商品发生故障，若退货审核通过，之后退款还需要满足什么顺序？", "质量问题在三十天内提出且货物已送达，退款能否绕过退货批准？", "已签收的瑕疵商品申请退货与退款，两个政策条件如何衔接？", "质量退货处于审核阶段时，是否已经达到已签收订单的退款前置条件？"]),
    ("multi_clause_cancel_refund", "dev", "multi_clause", True, ["policies/cancel@2026-01#eligible", "policies/refund@2026-01#before-shipping"], ["订单已付未发货", "用户请求取消", "同时创建退款申请", "确认前不得执行"], "须取得用户确认；已付款取消与退款申请在同一操作关联，且不代表到账。", [
        "一笔已付待发货订单要取消，是否需确认并同步建退款申请？", "取消已付款未发货订单后，退款会自动到账吗，系统应先做什么？", "我要求撤销尚未发货的已付订单，取消规则与退款规则如何共同适用？", "已付款状态下申请取消，是否可以不建退款申请直接完成？"]),
    ("multi_clause_cancel_conflict", "dev", "multi_clause", True, ["policies/cancel@2026-01#eligible", "policies/refund@2026-01#uniqueness"], ["订单待发货", "已有开放退款申请", "不得通过取消产生第二个开放退款"], "开放退款申请存在时不得取消该订单。", [
        "待发货订单已有开放退款，现在还要求取消，能不能继续？", "已有退款处理中的订单，取消动作是否会触发状态冲突？", "如果取消会新增退款但该订单已有开放申请，政策要求如何判定？", "同订单已经有未结退款记录，是否允许再通过取消产生另一个？"]),
    ("multi_clause_category_time", "test_candidate", "multi_condition_policy", True, ["policies/return-standard@2026-07#no-reason"], ["商品类型为食品或定制品", "签收未满七天", "品类例外覆盖时间条件"], "即使在七天内，食品/定制品仍不适用无理由退货。", [
        "定制商品签收两天且保持完好，时间条件能否覆盖定制例外？", "食品仍在签收后七天内，是否因为时间满足就能无理由退？", "无理由申请同时满足七天时限但商品是定做的，结论是什么？", "定制或食品在期限和完好条件满足时，品类限制是否仍有效？"]),
    ("ambiguous_paid_policy_version", "test_candidate", "conflict_or_clarification", False, ["policies/return-standard@2026-01#no-reason", "policies/return-standard@2026-07#no-reason"], ["请求追溯已付款订单权益", "未提供付款时间", "旧版与新版可能适用"], "需先询问付款时间，再选择付款时生效版本；当前证据不足以断定适用版本。", [
        "我以前买的普通商品能否无理由退？没记付款日期，直接套现行规则可以吗？", "订单已付款但我没提供日期，客服怎样确认该用哪版退货政策？", "要追溯判断这单退货权益，付款时点未知时能不能先给确定结论？", "购买发生在政策变更前还是之后不清楚，应先引用旧版还是新版？"]),
]

HARD_NEGATIVES = {
    "delivery_fact_boundary": ["policies/return-standard@2026-07#no-reason"],
    "refund_reason_hard_negative": ["policies/return-standard@2026-07#no-reason"],
}
UNSCORABLE_GROUPS = {"warranty_specific_missing", "ambiguous_paid_policy_version"}
UNKNOWN_JUDGMENT_GROUPS = {"ambiguous_paid_policy_version"}
CONDITION_RELATIONS = {
    "ambiguous_paid_policy_version": ["unknown", "unknown", "unknown"],
    "warranty_specific_missing": ["unknown", "supports", "supports"],
}
CONDITION_EVIDENCE = {
    "refund_delivered_return": [["policies/refund@2026-01#before-shipping"], ["policies/refund@2026-01#before-shipping", "policies/return-standard@2026-07#quality"], ["policies/refund@2026-01#before-shipping"]],
    "cancel_paid_refund": [["policies/cancel@2026-01#eligible"], ["policies/refund@2026-01#before-shipping", "policies/cancel@2026-01#eligible"], ["policies/cancel@2026-01#eligible", "policies/refund@2026-01#before-shipping"], ["policies/cancel@2026-01#eligible"]],
    "cancel_open_refund_conflict": [["policies/cancel@2026-01#eligible"], ["policies/refund@2026-01#uniqueness"], ["policies/cancel@2026-01#eligible", "policies/refund@2026-01#uniqueness"]],
    "delivery_policy_limits": [["policies/return-standard@2026-07#no-reason"], ["policies/return-standard@2026-07#no-reason"], ["policies/delivery@2026-01#tracking"]],
    "multi_clause_return_refund": [["policies/return-standard@2026-07#quality"], ["policies/refund@2026-01#before-shipping"], ["policies/return-standard@2026-07#quality", "policies/refund@2026-01#before-shipping"]],
    "multi_clause_cancel_refund": [["policies/cancel@2026-01#eligible"], ["policies/refund@2026-01#before-shipping"], ["policies/cancel@2026-01#eligible"], ["policies/cancel@2026-01#eligible"]],
    "multi_clause_cancel_conflict": [["policies/cancel@2026-01#eligible"], ["policies/refund@2026-01#uniqueness"], ["policies/cancel@2026-01#eligible", "policies/refund@2026-01#uniqueness"]],
    "ambiguous_paid_policy_version": [["policies/return-standard@2026-01#no-reason", "policies/return-standard@2026-07#no-reason"]] * 3,
}


def canonical_hash(data: object) -> str:
    blob = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def assert_outputs_new(*paths: Path) -> None:
    existing = [str(path) for path in paths if path.exists()]
    if existing:
        raise FileExistsError("refusing to overwrite candidate/review files; choose a new versioned destination: " + ", ".join(existing))


def build() -> dict:
    # Candidate JSON and its review sheet are evidence artifacts. Never replace either
    # on a repeat run, especially after a reviewer has edited the CSV.
    assert_outputs_new(OUT, CSV_OUT)
    policies = json.loads(SOURCE.read_text(encoding="utf-8"))
    knowledge = {}
    for doc in policies["documents"]:
        for clause in doc["clauses"]:
            cid = f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"
            knowledge[cid] = {"version": doc["version"], "effective_from": doc["effective_from"], "effective_to": doc["effective_to"]}
    cases = []
    for group_id, split, category, composite, evidence, conditions, conclusion, questions in GROUPS:
        if len(questions) != 4:
            raise ValueError(f"{group_id}: expected four questions")
        for variant, question in enumerate(questions, 1):
            case_id = f"pr2-{group_id}-{variant:02d}"
            eval_at = "2026-06-30T23:59:59Z" if group_id == "return_old_version" else "2026-07-01T00:00:00Z" if group_id == "return_exact_boundary" else "2026-10-18T00:00:00Z"
            evidence_judgments = []
            mapped = CONDITION_EVIDENCE.get(group_id, [list(evidence) for _ in conditions])
            if len(mapped) != len(conditions):
                raise ValueError(f"{group_id}: condition/evidence mapping length mismatch")
            relations = CONDITION_RELATIONS.get(group_id, ["supports"] * len(conditions))
            if len(relations) != len(conditions):
                raise ValueError(f"{group_id}: condition/relation mapping length mismatch")
            for condition, condition_evidence, relation in zip(conditions, mapped, relations):
                evidence_judgments.append({"condition": condition, "evidence_ids": condition_evidence, "relation": relation})
            scoreability = "UNSCORABLE_CANDIDATE" if group_id in UNSCORABLE_GROUPS else "SCORABLE_CANDIDATE"
            reason = ("具体型号/部件条款不在现有政策库；人工确认是否应按证据不足/转人工评价。" if group_id == "warranty_specific_missing" else "付款日期缺失，无法确定已付款订单适用的历史政策版本；应先澄清。") if scoreability.startswith("UNSCORABLE") else None
            versions = sorted({knowledge[e]["version"] for e in evidence})
            gold_evidence = [] if group_id in UNKNOWN_JUDGMENT_GROUPS else evidence
            policy_combo = sorted(gold_evidence or (evidence if group_id in UNKNOWN_JUDGMENT_GROUPS else []))
            combo_id = "pc-" + hashlib.sha256("|".join(policy_combo).encode("utf-8")).hexdigest()[:12]
            cases.append({
                "case_id": case_id, "source": "CODEX_AUTHORED_UNREVIEWED_CANDIDATE", "authoring_note": "候选问题和金标由助手依据现有模拟政策起草，未由人类审核；不可视作金标。",
                "business_scenario": category, "template_group": group_id, "group_id": group_id, "variant": variant,
                "policy_combination_group": combo_id,
                "dataset_split": split, "test_candidate_only": split == "test_candidate", "frozen": False,
                "user_query": question, "user_id": None, "session_history": [], "order_snapshot_id": None,
                "knowledge_version": policies["knowledge_version"], "policy_version": versions[0] if len(versions) == 1 else versions,
                "evaluation_at": eval_at, "required_conditions": [{"condition_id": f"c{i+1}", "text": c, "required": True} for i, c in enumerate(conditions)],
                "acceptable_evidence_ids": gold_evidence, "possible_evidence_ids": evidence if group_id in UNKNOWN_JUDGMENT_GROUPS else [],
                "hard_negative_evidence_ids": HARD_NEGATIVES.get(group_id, []),
                "evidence_judgments": evidence_judgments,
                "expected_conclusion": conclusion, "expected_intent": "policy_question", "expected_strategy": "MULTI" if len(evidence) > 1 else "SINGLE",
                "expected_tool_calls": [], "expected_business_effect": None, "critical_facts": [conclusion],
                "scoreability": scoreability, "scorable": scoreability == "SCORABLE_CANDIDATE", "unscorable_reason": reason,
                "annotator": None, "review_status": "PENDING_REVIEW", "reviewer": None, "reviewed_at": None,
                "review_notes": "", "review_history": [], "tags": [category] + (["compound_reasoning"] if composite and group_id not in UNSCORABLE_GROUPS else []),
            })
    payload = {
        "dataset_version": "policy-retrieval-v2-candidate-20261018-r2",
        "status": "CANDIDATE_PENDING_HUMAN_REVIEW_NOT_FROZEN",
        "note": "112 条候选样本覆盖现有模拟政策的10个版本化chunk；全部由助手起草并待人工审核，问题数量不增加政策知识覆盖。test_candidate 是候选冻结集，不代表已冻结或可用于正式放行。12 条 policy-initial-v1-unfrozen 另存，不计入本集合。",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_policy_file": "knowledge/policies.json", "source_policy_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
        "source_policy_canonical_sha256": canonical_hash(policies),
        "knowledge_version": policies["knowledge_version"], "schema_version": "policy-retrieval-case-v2",
        "split_policy": "split is assigned per template_group and exact acceptable-evidence policy-combination group; all variants and identical positive evidence combinations remain together. dev/validation/test_candidate groups are disjoint.",
        "review_policy": "Do not change review_status to APPROVED until a human has verified query meaning, applicable version, all required conditions, evidence IDs, conclusion, and scoreability. No blind review is claimed.",
        "group_count": len(GROUPS), "case_count": len(cases), "cases": cases,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("x", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    CSV_OUT.parent.mkdir(parents=True, exist_ok=True)
    fields = ["case_id", "dataset_split", "template_group", "business_scenario", "tags", "user_query", "policy_version", "evaluation_at", "required_conditions", "acceptable_evidence_ids", "possible_evidence_ids", "hard_negative_evidence_ids", "evidence_judgments", "expected_conclusion", "expected_strategy", "scoreability", "unscorable_reason", "review_status", "review_notes"]
    with CSV_OUT.open("x", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for case in cases:
            writer.writerow({k: json.dumps(case[k], ensure_ascii=False) if isinstance(case[k], (list, dict)) else case[k] for k in fields})
    print(f"wrote {OUT.relative_to(ROOT)} and {CSV_OUT.relative_to(ROOT)}: {len(cases)} candidates")


if __name__ == "__main__":
    build()
