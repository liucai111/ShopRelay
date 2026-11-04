"""Versioned, safety-first two-layer routing for M2.1.

This module chooses a strategy; it does not execute tools or unavailable agents.
"""
from __future__ import annotations

import re
import time
import inspect
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROUTE_VERSION = "m2.1-route-v16"
PROMPT_VERSION = "m2.1-deepseek-route-v5"
Strategy = Literal["DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"]

_PRODUCT_ID = re.compile(r"\bP-[A-Z0-9_-]{2,}\b", re.I)
_COUPON_ID = re.compile(r"\b[A-Z][A-Z0-9_-]{2,}\b")
_SENSITIVE_OBJECT = r"订单|物流|运单|退款|退货|售后|账户|会话|地址|电话|个人信息|隐私|数据|记录|商品明细"
_ACCESS_VERB = r"查|查询|查看|读取|调取|导出|打印|跟踪|追踪|获取|提供|显示|列出|发送|发给|给我|替我|帮我|执行|办理|提交|发起|创建|退款|取消"
_GLOBAL_SENSITIVE_SCOPE = r"(?:全体|所有|全部|全量|整库|全库|整个数据库).{0,16}(?:用户|客户|买家|账户|会话|聊天|消息|订单|退款|售后|个人信息|隐私|数据|记录)|(?:用户|客户|买家|账户|会话|聊天|消息|订单|退款|售后|个人信息|隐私|数据|记录).{0,16}(?:全体|所有|全部|全量|整库|全库)"
_BULK_USER_SCOPE = r"批量.{0,16}(?:用户|客户|买家|账户|会话|聊天|消息|个人信息|隐私|数据|记录)|(?:用户|客户|买家|账户|会话|聊天|消息|个人信息|隐私|数据|记录).{0,16}批量"
_PRODUCT_READ_CAPABILITIES = {"search_products", "get_product_detail", "recommend_products", "check_inventory"}


def _product_comparison_labels(query: str) -> list[str]:
    q = query.casefold()
    labels = ["live_product", "product_comparison"]
    if re.search(r"重量|多重|更轻|较轻|谁轻|更重|较重|谁重|轻一些|重一些|weight|weigh|lighter|heavier|lightest|heaviest", q):
        labels.append("product_attribute_weight_g")
        if re.search(r"更轻|较轻|谁轻|轻一些|lighter|lightest", q):
            labels.append("product_direction_min")
        elif re.search(r"更重|较重|谁重|重一些|heavier|heaviest", q):
            labels.append("product_direction_max")
        else:
            labels.append("product_direction_side_by_side")
    elif re.search(r"价格|价钱|售价|更贵|较贵|更便宜|较便宜|谁贵|谁便宜|cost|price|cheaper|more expensive", q):
        labels.append("product_attribute_price_cents")
        if re.search(r"更便宜|较便宜|谁便宜|cheaper|less expensive|costs less", q):
            labels.append("product_direction_min")
        elif re.search(r"更贵|较贵|谁贵|more expensive|costs more", q):
            labels.append("product_direction_max")
        else:
            labels.append("product_direction_side_by_side")
    elif re.search(r"输入电压|input\s+voltage",q,re.I):
        labels.extend(['product_attribute_input_voltage','product_direction_side_by_side'])
    elif re.search(r"续航|电池|尺寸|长度|宽度|高度|功率|材质|颜色|保修|耐用|battery|warranty|size|length|width|height|power|material|color|durability", q):
        labels.extend(["product_attribute_unsupported", "product_direction_side_by_side"])
    else:
        labels.extend(["product_attribute_unspecified", "product_direction_side_by_side"])
    return labels


class RouteCapabilityScopeError(ValueError):
    """A route named a capability outside the server-provided request scope."""
    def __init__(self, message: str, *, diagnostic: dict | None = None):
        self.code = "ROUTE_CAPABILITY_SCOPE_REJECTED"
        self.diagnostic = diagnostic or {}
        super().__init__(message)


def _is_security_policy_question(query: str) -> bool:
    """Do not confuse asking about a safeguard with asking to bypass it."""
    security_concept = bool(re.search(r"授权|权限|登录|验证|认证|属主|身份|越权|他人订单|别人.{0,8}(?:订单|会话|隐私)|隐私|密钥|所有用户|全体用户|批量导出", query, re.I))
    policy_ask = bool(re.search(
        r"规则|政策|条款|规定|为什么不能|是否允许|能否|如何防止|怎样保护|如何保护|如何阻止|怎样阻止|如何拒绝|怎样拒绝|"
        r"违反.{0,10}(?:规则|政策|条款)|触犯.{0,10}(?:规则|政策|条款)", query, re.I))
    action_request = bool(re.search(
        r"(?:请|帮我|替我|直接|马上).{0,20}(?:绕过|冒充|伪造|盗用|读取|查询|导出|退款|取消|打印)", query, re.I))
    return ((security_concept and policy_ask) or
            bool(re.search(r"(?:违反|触犯).{0,10}(?:规则|政策|条款)", query, re.I))) and not action_request


def _is_unauthorized_action(query: str) -> bool:
    q = query.casefold()
    if _is_security_policy_question(q):
        return False
    asks_secret = bool(re.search(r"api\s*key|密钥|密码|(?<!付款)(?<!支付)凭据|credential", q, re.I) and
                       re.search(r"泄露|泄漏|打印|输出|提供|发给|给我|告诉我|导出|读取|显示", q, re.I))
    person_reference = r"他人|别人|其他用户|其他人的|别人的|另一位用户|另一个用户|另一用户|其他账户|另一个账户|其他买家|别的买家|伴侣|朋友|亲友|同事|家人"
    other_person_target = bool(re.search(
        rf"(?:{person_reference}).{{0,64}}(?:{_SENSITIVE_OBJECT})|"
        rf"(?:{_SENSITIVE_OBJECT}).{{0,64}}(?:{person_reference})|"
        r"\b(?:another\s+user|someone\s+else|SYN-USER-[A-Z0-9_-]+)\b.{0,48}\bORD[-_][A-Z0-9_-]+\b|"
        r"\bORD[-_][A-Z0-9_-]+\b.{0,48}\b(?:another\s+user|someone\s+else|SYN-USER-[A-Z0-9_-]+)\b", q, re.I))
    explicit_missing_auth = bool(re.search(
        r"无权|无权限|没有权限|没有.{0,8}(?:登录|账户|访问).{0,8}权限|未经授权|未授权|没有登录权限|没有账户权限|"
        r"不做验证|不经验证|不登录|未登录|(?:不用|不必|无需|不需要).{0,8}(?:登录|身份验证|身份认证|认证|授权|属主验证|账户验证)", q, re.I))
    bypass_or_spoof = bool(re.search(
        r"(?:绕过|跳过|忽略).{0,12}(?:登录|身份|认证|验证|属主|权限|授权)|(?:伪造|冒充|盗用|猜(?:测|一个|到)).{0,18}(?:身份|用户.?id|cookie|会话|登录|凭据)|"
        r"(?:用户.?id|cookie|会话凭据).{0,18}(?:伪造|冒充|盗用|猜(?:测|一个|到))", q, re.I))
    no_confirmation_write = bool(re.search(
        r"(?:未经|不经|不要|无需|跳过).{0,12}(?:用户|本人|人工|二次)?确认.{0,16}(?:退款|退货|取消|导出|批量)|"
        r"(?:绕过|跳过).{0,12}(?:确认|授权).{0,18}(?:退款|退货|取消|导出|执行)", q, re.I))
    asks_for_sensitive_action = bool(re.search(_ACCESS_VERB + r"|\b(?:tell|show|read|check|query|look\s+up|access|disclose|apply)\b", q, re.I))
    bulk_scope = bool(re.search(_GLOBAL_SENSITIVE_SCOPE, q, re.I) or
                       (re.search(_BULK_USER_SCOPE, q, re.I) and
                        not re.search(r"我(?:的|本人)?|本人(?:的)?|自己(?:的)?", q)))
    bulk_sensitive_export = bool(bulk_scope and
                                 re.search(_SENSITIVE_OBJECT, q, re.I) and asks_for_sensitive_action)
    return asks_secret or bulk_sensitive_export or (other_person_target and asks_for_sensitive_action) or \
        (explicit_missing_auth and asks_for_sensitive_action) or bypass_or_spoof or no_confirmation_write


def _unauthorized_reason_code(query: str) -> str:
    """Classify the refusal reason without echoing or exposing requested data."""
    q = query.casefold()
    person_reference = r"他人|别人|其他用户|其他人的|别人的|另一位用户|另一个用户|另一用户|其他账户|另一个账户|其他买家|别的买家|伴侣|朋友|亲友|同事|家人"
    third_party_order = bool(re.search(rf"(?:{person_reference}).{{0,64}}订单|订单.{{0,64}}(?:{person_reference})|"
        r"\b(?:another\s+user|someone\s+else|SYN-USER-[A-Z0-9_-]+)\b.{0,48}\bORD[-_][A-Z0-9_-]+\b|"
        r"\bORD[-_][A-Z0-9_-]+\b.{0,48}\b(?:another\s+user|someone\s+else|SYN-USER-[A-Z0-9_-]+)\b", q, re.I))
    asks_secret = bool(re.search(r"api\s*key|密钥|密码|(?<!付款)(?<!支付)凭据|credential", q, re.I) and
                       re.search(r"泄露|泄漏|打印|输出|提供|发给|给我|告诉我|导出|读取|显示", q, re.I))
    if third_party_order and not asks_secret:
        return "THIRD_PARTY_ORDER_ACCESS"
    if asks_secret and not third_party_order:
        return "SECRET_CREDENTIAL_DISCLOSURE"
    return "UNAUTHORIZED_OR_MIXED_SENSITIVE_REQUEST"


def _order_identifier_present(query: str) -> bool:
    # A field label such as “订单号” is not itself an identifier.
    # Reuse the real tool's ASCII token boundary. Unicode \b treats the Han
    # character before ORD as a word character and lost valid natural inputs.
    from app.m3.order_tools import requested_order_id
    identifier,error=requested_order_id(query)
    return bool(identifier or error=='ORDER_ID_AMBIGUOUS' or re.search(r"订单(?:号|编号)\s*[:：#]?\s*[A-Z0-9][A-Z0-9_-]{4,}", query, re.I))


def _multi_policy_relation(query: str) -> bool:
    """Require distinct policy aspects plus an explicit request to relate them."""
    q = query.casefold()
    has_return = bool(re.search(r"退货|无理由|质量问题|质量退货", q))
    has_refund = bool(re.search(r"退款|退费|到账", q))
    has_cancel = bool(re.search(r"取消订单|订单取消|取消已付款|取消", q))
    has_coupon = bool(re.search(r"优惠券|优惠码|用券", q))
    has_warranty = bool(re.search(r"保修|质保|保修期", q))
    has_shipping_window = bool(re.search(r"签收|无理由期限|质量期限|30天|三十天|七天|7天", q))
    distinct_pairs = (has_return and has_refund) or (has_cancel and has_refund) or \
        (has_coupon and (has_shipping_window or has_refund)) or (has_warranty and has_return)
    relation = bool(re.search(
        r"如何衔接|怎么衔接|接起来|分别由.{0,16}条款|两条(?:规则|限制|政策)|两项规则|两个政策|多项政策|"
        r"独立前置|分别判断|同时判断|先.{0,24}再|之后.{0,16}还需要|还需要.{0,16}前置|"
        r"什么顺序|如何排序|规则关系|政策关系|条件关系|如何组合|分别说明|分别适用|能否绕过.{0,12}(?:批准|审核|前置)", q))
    same_cancel_refund_rule = has_cancel and has_refund and bool(re.search(
        r"取消资格.{0,30}(?:同步|同时|一起|退款申请)|取消成功.{0,40}退款申请|"
        r"取消.{0,16}(?:同步|同时|并且|一并).{0,16}退款申请|取消后.{0,20}退款申请", q))
    if same_cancel_refund_rule:
        return False
    return distinct_pairs and relation


def _product_id_present(query: str) -> bool:
    return bool(_PRODUCT_ID.search(query) or re.search(r"(?:商品|型号)\s*[A-Z0-9][A-Z0-9_-]{2,}", query, re.I))


def _clarification_intent(query: str) -> bool:
    return bool(re.search(
        r"还没(?:记起|想好|决定|说明|描述)|尚未(?:决定|明确)|具体问题(?:还)?没(?:有|描述)|"
        r"先告诉我需要(?:补充|提供)|先让我说清|不要替我操作|请先澄清|不知道该问什么", query))


def _order_collection_request(query: str) -> bool:
    """A collection read differs from a specific order fact or action.

    Compose the object and read intent rather than matching one complete user
    sentence. Unknown phrasings may still use the bounded model classifier.
    """
    collection = re.search(r"订单|订购记录|购买记录|买过(?:什么|哪些)|下过.{0,4}单|\borders?\b|\bpurchases\b", query, re.I)
    if not collection or _order_identifier_present(query):
        return False
    specific = re.search(r"商品清单|订单商品|明细|状态|进度|付款|支付|签收|物流|配送|退款|退货|取消|保修|优惠|政策|规则|权限|接口|"
                         r"\b(?:status|payment|shipment|refund|cancel|items?)\b", query, re.I)
    if specific:
        return False
    read = re.search(r"查(?:询|看)?|看(?:看|下|一下)?|列出|列表|清单|展示|显示|调出|打开|有哪些|有哪几|买过|下过|"
                     r"\b(?:show|view|list|browse|find|see|check)\b", query, re.I)
    bare = re.fullmatch(r"(?:我(?:的|账户下的)|本人(?:的)?)订单[？?。\s]*|my\s+orders[?.\s]*", query, re.I)
    return bool(read or bare)


def _business_route(query: str, available_capabilities: set[str] | None = None) -> RouteDecision | None:
    q = query.casefold()
    # A current coupon lookup needs a user-provided code. General coupon policy
    # questions still use retrieval; no identifier is inferred from fixture data.
    coupon_lookup = bool(re.search(r"优惠券|优惠码|券码|\bcoupon\b", q) and
        re.search(r"今天|当前|现在|能用|可用|有效|折扣|\b(?:today|currently|actually use|discount)\b", q) and
        not re.search(r"政策|规则|\b(?:policy|rules?)\b", q))
    if coupon_lookup:
        from app.m3.policy_tools import coupon_code_from_query
        if coupon_code_from_query(query) is None:
            return _decision("DIRECT", reason="MISSING_COUPON_IDENTIFIER", labels=["clarification", "live_coupon"],
                clarify="请提供明确的优惠券码；当前无法核验可用性、适用资格或折扣，我不会在缺少券码时查询或猜测。")
        if available_capabilities is not None and "get_coupon" not in available_capabilities:
            return _decision("DIRECT", reason="COUPON_LOOKUP_NOT_ALLOWED", labels=["clarification", "live_coupon"],
                clarify="当前范围不允许查询优惠券；我无法核验其可用性、适用资格或折扣。")
        caps = ["get_coupon"]
        if _order_identifier_present(query) and re.search(r"订单.{0,20}(?:状态|付款|记录|金额)", q):
            if available_capabilities is not None and "get_order" not in available_capabilities:
                return _decision("DIRECT", reason="ORDER_COUPON_SCOPE_INCOMPLETE", labels=["clarification"],
                    clarify="当前只能核验优惠券，不能核验订单事实；请提供允许的查询范围。")
            caps.insert(0, "get_order")
        return _decision("AGENT", sub="AGENT+MULTI" if len(caps) > 1 else "AGENT+SINGLE", reason="LIVE_COUPON_FACT",
            labels=["live_coupon"], capabilities=caps)
    if _order_collection_request(q):
        if available_capabilities is not None and "list_orders" not in available_capabilities:
            return _decision("DIRECT", reason="ORDER_LIST_NOT_ALLOWED", labels=["clarification"],
                clarify="当前范围不支持列出订单；可以提供本人订单号进行查询。")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="OWNED_ORDER_LIST",
            labels=["live_business"], capabilities=["list_orders"])
    order_id = _order_identifier_present(query)
    product_id = _product_id_present(query)
    if not order_id and re.search(r'我|本人|我的',q) and re.search(r'坏了|损坏|故障|缺件|质量问题',q):
        return _decision('DIRECT',reason='AFTER_SALES_FACTS_MISSING',labels=['clarification','after_sales'],
            clarify='请提供本人订单号、商品送达或签收时间，以及具体问题事实（损坏现象、部位或缺失的物件）；未核验前不能确认售后资格。')
    # Chat requests prepare a draft; only the existing authenticated confirmation
    # API may turn that draft into an effect. Never route a chat to a write tool.
    draft_action = bool(re.search(r"(?:准备|整理|草案|待.{0,4}确认|确认后|请取消|请.{0,8}提交).{0,45}(?:退货|退款|取消|人工|工单)|"
        r"(?:退货|退款|取消|人工).{0,45}(?:草案|准备|待.{0,4}确认|确认后)", q))
    draft_action = draft_action or bool(re.search(r"退货|退回|退款|取消|转人工", q) and
        re.search(r"先准备.{0,8}申请|(?:准备|整理).{0,8}(?:草案|待确认申请)", q))
    asks_policy = bool(re.search(r"政策|条款|规则|保修|质保|返款时长|换货|policy|warranty",q,re.I))
    def scoped_read(required,reason):
        if available_capabilities is not None and not set(required)<=available_capabilities:
            return _decision('DIRECT',reason='COMPOSITE_READ_CAPABILITY_NOT_ALLOWED',labels=['clarification','scope_boundary'],
                clarify='当前授权范围不足以完成所需事实和政策核验；请补充合法查询范围，我不会猜测资格或执行动作。')
        return _decision('AGENT',sub='AGENT+MULTI' if len(required)>1 else 'AGENT+SINGLE',reason=reason,
            labels=['live_business','composite_read'] if len(required)>1 else ['live_business'],capabilities=required)
    if order_id and draft_action and (available_capabilities is None or {'get_order','search_knowledge'}.issubset(available_capabilities)):
        required=['get_order','search_knowledge']
        if re.search(r'退货.{0,16}批准|批准.{0,16}退货',q):required.insert(1,'get_refund_status')
        return scoped_read(required,'BUSINESS_ACTION_DRAFT_READ_ONLY')
    if order_id and asks_policy and re.search(r'比较|对比',q) and re.search(r'商品|产品',q):
        route = scoped_read(['get_order','get_order_items','get_product_detail','search_knowledge'],'OWNED_PRODUCTS_POLICY_COMPOSITE')
        if route.strategy == 'AGENT':
            route = route.model_copy(update={'intent_labels':[*route.intent_labels, *_product_comparison_labels(query)]})
        return route
    if order_id and re.search(r"退款申请|退货申请",q) and re.search(r"阶段|进度|记录|状态",q):
        if not asks_policy:
            required=['get_refund_status']
            if re.search(r'订单.{0,60}(?:记录状态|的状态)',q):required.insert(0,'get_order')
            return scoped_read(required,'AFTER_SALES_RECORDED_STATE')
        return scoped_read(['get_order','get_refund_status','search_knowledge'],'AFTER_SALES_RECORDED_STATE_AND_POLICY')
    if order_id and asks_policy and re.search(r"明细|两条数量|订单商品",q) and not re.search(r"保修|质保",q):
        return scoped_read(['get_order','get_order_items','search_knowledge'],'ORDER_ITEMS_POLICY_COMPOSITE')
    if product_id and re.search(r"规格|详情|参数",q) and re.search(r"保修|质保",q):
        return scoped_read(['get_product_detail','get_warranty'],'EXPLICIT_PRODUCT_SPEC_AND_WARRANTY')
    if order_id and asks_policy and re.search(r"物流|延误",q):
        return scoped_read(['get_order','track_shipment','search_knowledge'],'ORDER_SHIPMENT_POLICY_COMPOSITE')
    if order_id and asks_policy and re.search(r"根据|结合|区分",q) and re.search(r"订单状态|退款|到账",q):
        return scoped_read(['get_order','search_knowledge'],'ORDER_RECORDED_FACT_AND_POLICY')
    live_verb = r"查|查询|查看|读取|调取|调出|调出来|核实|核对|追踪|跟踪|显示|列出|打开|获取|搜索|推荐|比较|挑选|选购|到哪|目前|现在|\b(?:what|check|show|tell|find|look\s+up)\b"

    refund_subject = bool(re.search(r"退款(?:申请|请求)|退款单|\brefund(?:\s+(?:request|application))?\b", q, re.I))
    order_subject = bool(order_id or re.search(r"订单|订单号|订单编号|这笔退款|我(?:的|这笔|那笔)?订单|\bmy\s+(?:own\s+)?(?:synthetic\s+)?order\b", q, re.I) or refund_subject)
    order_fact = bool(re.search(r"状态|进度|支付时间|付款时间|签收时间|签收日|哪一天.{0,8}签收|签收.{0,8}哪一天|商品明细|商品清单|订单商品|商品名称|数量|物流|运单|送达|预计到|事件|是否(?:已经)?(?:付|付款|支付|到账)|(?:有|已经|是否).{0,4}(?:付|付款|支付)|\bstatus\b|\b(paid|payment|delivered|order\s+items?)\b|\b(?:has|was|is)\s+(?:it\s+)?(?:been\s+)?paid\b|\bit\s+(?:has|was|is)\s+been\s+paid\b", q, re.I))
    shipment = bool(re.search(r"物流|运单|包裹|快递|配送|签收", q))
    warranty_question = bool(re.search(r"保修|质保|warranty|guarantee", q, re.I))

    # A named product's live stock combined with a delivery-policy promise
    # needs catalog, inventory and policy evidence, before the generic FAQ route.
    inventory_policy = bool(re.search(r"商品|产品|product|lamp|bottle|mug", q, re.I) and
        re.search(r"库存|现货|stock|inventory", q, re.I) and
        re.search(r"政策|policy|规则|rule", q, re.I))
    if inventory_policy:
        required = ["get_product_detail", "check_inventory", "search_knowledge"]
        if available_capabilities is None or set(required).issubset(available_capabilities):
            return _decision("AGENT", sub="AGENT+MULTI", reason="PRODUCT_STOCK_POLICY_COMPOSITE_READ",
                labels=["live_product", "policy", "composite_read"], capabilities=required)
        return _decision("DIRECT", reason="COMPOSITE_READ_CAPABILITY_NOT_ALLOWED",
            labels=["clarification", "product", "policy"],
            clarify="当前范围不能同时核验商品、库存和配送政策；我不能据此承诺库存或送达时间。")

    # A request that joins one named product fact with a policy fact is an
    # application-level composite read. Route only the two registered
    # capabilities that the case explicitly permits; Product and Policy
    # specialists resolve their own evidence under the existing supervisor.
    product_policy = bool(
        re.search(r"商品|产品|瓶|水壶|杯|product|bottle|kettle|mug", q, re.I) and
        re.search(r"保修|质保|政策|规则|条款|期限|warranty|policy|rule|term", q, re.I) and
        re.search(r"比较|对比|compare|comparison", q, re.I)
    )
    if product_policy:
        required = ["get_product_detail", "search_knowledge"]
        if available_capabilities is None or set(required).issubset(available_capabilities):
            return _decision("AGENT", sub="AGENT+MULTI", reason="PRODUCT_POLICY_COMPOSITE_READ",
                labels=["live_product", "policy", "composite_read"], capabilities=required)
        return _decision("DIRECT", reason="COMPOSITE_READ_CAPABILITY_NOT_ALLOWED",
            labels=["clarification", "live_product", "policy"],
            clarify="当前请求范围不同时允许读取商品详情和政策；我不会猜测商品或政策内容。")

    # Return/refund eligibility questions about a specified order need both
    # the owner-scoped order facts and the policy source. An explicitly
    # requested refund-status step adds only its registered read capability.
    from app.m3.order_tools import is_return_guidance_query
    return_guidance = is_return_guidance_query(q)
    return_eligibility = return_guidance or bool(re.search(
        r"退货|退回|退款|能退|可以退|能不能退|return|refund", q, re.I) and re.search(
        r"能退|可以退|能不能退|需要|条件|资格|办理|before|eligibility|eligible|returnable|staff review|"
        r"(?:can|may|could)\s+(?:i|we)\s+(?:request|apply)|(?:what|how).{0,32}(?:request|apply)|"
        r"申请.{0,10}(?:需要|条件|资格)|what.{0,16}(?:need|step)|what is needed", q, re.I))
    if order_id and return_eligibility:
        order_product_policy_caps = ["get_order", "get_order_items", "get_product_detail", "search_knowledge"]
        asks_item_policy = bool(re.search(r"returnable|does staff review|staff review apply|商品类别|适用商品|"
            r"accessory|unopened|sealed|wrong\s+size|商品|这件|配件|未拆封|尺码", q, re.I))
        if return_guidance and (available_capabilities is None or
                set(order_product_policy_caps).issubset(available_capabilities)):
            asks_item_policy = True
        if asks_item_policy:
            if available_capabilities is not None and not set(order_product_policy_caps).issubset(available_capabilities):
                return _decision("DIRECT", reason="COMPOSITE_READ_CAPABILITY_NOT_ALLOWED",
                    labels=["clarification", "live_business", "product", "policy"],
                    clarify="当前请求范围不允许核验订单商品和适用规则；我不会猜测商品类别或资格。")
            return _decision("AGENT", sub="AGENT+MULTI", reason="OWNED_ORDER_ITEM_POLICY_COMPOSITE_READ",
                labels=["live_business", "product", "policy", "composite_read"],
                capabilities=order_product_policy_caps)
        required = ["get_order", "search_knowledge"]
        if re.search(r"未发货|尚未发货|物流|not\s+(?:yet\s+)?shipped|shipment|dispatch", q, re.I):
            required.insert(1, "track_shipment")
        asks_refund_step = bool(re.search(
            r"approved.{0,20}return|return.{0,24}approved|退款.{0,12}(?:步骤|进度|状态)|"
            r"refund.{0,20}(?:step|status|progress)|what refund step", q, re.I))
        if asks_refund_step:
            required.insert(1, "get_refund_status")
        if available_capabilities is None or set(required).issubset(available_capabilities):
            return _decision("AGENT", sub="AGENT+MULTI", reason="ORDER_POLICY_ELIGIBILITY_READ",
                labels=["live_business", "policy", "composite_read"], capabilities=required)
        return _decision("DIRECT", reason="COMPOSITE_READ_CAPABILITY_NOT_ALLOWED",
            labels=["clarification", "live_business", "policy"],
            clarify="当前请求范围不允许核验所需的本人订单事实和政策；我不会推断订单资格。")
    if order_id and warranty_question:
        # Resolve warranty coverage from an authenticated order's actual line
        # item. The order and item reads establish the product identifier;
        # only then may the warranty policy capability consume that ID.
        required = ["get_order", "get_order_items", "get_warranty"]
        if available_capabilities is not None and not set(required).issubset(available_capabilities):
            return _decision("DIRECT", reason="WARRANTY_ORDER_CONTEXT_NOT_ALLOWED",
                labels=["clarification", "live_business", "policy"],
                clarify="当前请求范围不允许核验订单商品及对应保修规则；我不会猜测商品编号或保修资格。")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="WARRANTY_FROM_OWNED_ORDER",
            labels=["live_business", "policy", "warranty"], capabilities=required)
    has_refund_application = bool(re.search(r"退款(?:申请|请求)|退款单|\brefund\s+(?:request|application)\b", q, re.I))
    has_payment_status = bool(re.search(r"状态|进度|是否|付款|支付|paid|payment|status|progress", q, re.I))
    refund_status = has_refund_application and has_payment_status
    items = bool(re.search(r"明细|商品清单|订单商品|商品名称|商品项目|买了什么|购买的商品|两条数量|商品.{0,6}数量", q))
    order_fact = order_fact or items or bool(re.search(r'收货日期|记录金额',q))
    if order_subject and order_fact and (re.search(live_verb, q) or refund_status):
        if not order_id:
            return _decision("DIRECT", reason="MISSING_ORDER_IDENTIFIER", labels=["clarification", "live_business"],
                             clarify="请提供需要查询的订单号，我再帮您核实。")
        caps = (["get_order_items"] if items else [])
        recorded_date = bool(re.search(r"记录.{0,12}(?:签收|收货|送达)|(?:收货|签收|送达)(?:日期|时间)|付款时间|支付时间|记录金额", q))
        order_record_requested = bool(re.search(r"订单.{0,60}(?:记录状态|的状态)", q))
        if recorded_date or order_record_requested: caps.append("get_order")
        if shipment and not recorded_date: caps.append("track_shipment")
        if refund_status: caps.append("get_refund_status")
        if not caps: caps.append("get_order")
        from app.m3.contracts import CAPABILITY_AGENT
        sub = "AGENT+MULTI" if len({CAPABILITY_AGENT[cap] for cap in caps}) > 1 else "AGENT+SINGLE"
        return _decision("AGENT", sub=sub, reason="LIVE_BUSINESS_FACT",
                         labels=["live_business"], capabilities=caps)

    if shipment and re.search(r"查|查询|查看|读取|调取|调出|追踪|跟踪|预计.{0,8}(?:到|送达)|当前.{0,8}(?:到哪|状态)", q):
        if not order_id:
            return _decision("DIRECT", reason="MISSING_ORDER_IDENTIFIER", labels=["clarification", "live_shipment"],
                             clarify="请提供对应订单号，以便核实当前物流信息。")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="LIVE_BUSINESS_FACT",
                         labels=["live_shipment"], capabilities=["track_shipment"])

    recommendation = bool(re.search(r"推荐|帮我(?:挑|选)|适合.{0,24}(?:预算|通勤|需求)|候选商品|选购|\b(?:recommend|suggest)\b", q, re.I))
    explicit_product_ids = list(dict.fromkeys(match.upper() for match in _PRODUCT_ID.findall(query)))
    asks_comparison = bool(re.search(
        r"比较|对比|哪.{0,12}(?:更轻|更重|较轻|较重|更贵|更便宜)|谁更?(?:轻|重|贵|便宜)|"
        r"重量|价格.{0,8}(?:比较|对比)|lighter|heavier|lightest|heaviest|cheaper|more expensive|"
        r"costs (?:less|more)|compare|comparison", q, re.I))
    product_subject = bool(re.search(
        r"商品|产品|台灯|灯具|灯|水壶|杯子|马克杯|水杯|保温杯|电器|家电|食品|零食|坚果|礼盒|"
        r"lamp|mugs?|cups?|bottles?|kettles?|product|catalog|sku|型号|品牌", q, re.I) or
        len(explicit_product_ids) >= 2)
    product_comparison = asks_comparison and product_subject
    if product_comparison:
        available = _PRODUCT_READ_CAPABILITIES if available_capabilities is None else (
            _PRODUCT_READ_CAPABILITIES & available_capabilities)
        labels = _product_comparison_labels(query)
        if len(explicit_product_ids) == 2:
            if "get_product_detail" in available:
                return _decision("AGENT", sub="AGENT+SINGLE", reason="PRODUCT_COMPARISON_EXPLICIT_IDS",
                    labels=labels, capabilities=["get_product_detail"])
            return _decision("DIRECT", reason="PRODUCT_DETAIL_NOT_ALLOWED",
                labels=["clarification", *labels],
                clarify="当前请求范围不允许读取指定商品详情；请在允许读取商品详情后再比较。")
        if len(explicit_product_ids) > 2:
            return _decision("DIRECT", reason="PRODUCT_ID_AMBIGUOUS",
                labels=["clarification", *labels],
                clarify="当前比较请求包含多个商品编号；请指定要比较的两个商品编号。")
        if "search_products" in available:
            caps = ["search_products"]
            if "get_product_detail" in available:
                caps.append("get_product_detail")
            return _decision("AGENT", sub="AGENT+SINGLE", reason="PRODUCT_COMPARISON_CATALOG_LOOKUP",
                labels=labels, capabilities=caps)
        if "get_product_detail" not in available or not product_id:
            return _decision("DIRECT", reason="PRODUCT_SEARCH_NOT_ALLOWED",
                labels=["clarification", *labels],
                clarify="当前请求没有可用的目录搜索能力；请提供可查询的商品编号。")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="PRODUCT_COMPARISON_EXPLICIT_IDS",
            labels=labels, capabilities=["get_product_detail"])
    catalog_search = bool(re.search(r"(?:商品|产品).{0,12}(?:搜索|搜一下|找|查找|有哪些|有什么)|"
                                   r"(?:搜索|搜一下|找|查找).{0,12}(?:商品|产品|水壶|杯子|坚果)", q))
    policy_conditions = bool(re.search(r'(?:商品|产品)(?:的)?(?:无理由)?(?:退货|退款|售后|保修)'
        r'.{0,12}(?:条件|流程|政策|规则|期限)', q))
    if policy_conditions and not re.search(r'搜索|搜一下|查找|找出', q):
        catalog_search = False
    current_product_data = bool(re.search(r"当前|现在|最新|价格|价钱|售价|详情|规格|容量|自重|接口|电压|功率|库存|现货|有货|可售|保修期限|具体保修|质保期限", q))
    if recommendation:
        if product_id and available_capabilities is not None and 'get_product_detail' in available_capabilities:
            return scoped_read(['get_product_detail'],'EXPLICIT_PRODUCT_RECOMMENDATION_FACT')
        caps = ["search_products", "recommend_products"]
        if re.search(r"现货|有货|库存|可售", q): caps.append("check_inventory")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="PRODUCT_RECOMMENDATION",
                         labels=["live_product"], capabilities=caps)
    if catalog_search:
        caps = ["search_products"]
        if re.search(r"库存|现货|有货|可售", q): caps.append("check_inventory")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="PRODUCT_CATALOG_SEARCH",
                         labels=["live_product"], capabilities=caps)
    if product_id and current_product_data:
        caps = []
        if re.search(r"详情|规格|价格|价钱|售价", q): caps.append("get_product_detail")
        if re.search(r"库存|现货|有货|可售", q): caps.append("check_inventory")
        if re.search(r"保修|质保", q): caps.append("get_warranty")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="LIVE_PRODUCT_FACT",
                         labels=["live_product"], capabilities=caps or ["get_product_detail"])
    if re.search(r"优惠券|优惠码|券码", q) and (_COUPON_ID.search(query) or current_product_data) and \
       re.search(r"当前|现在|能用|可用|适用|范围|有效期|叠加", q):
        return _decision("AGENT", sub="AGENT+SINGLE", reason="LIVE_COUPON_FACT",
                         labels=["live_coupon"], capabilities=["get_coupon"])

    action = re.search(r"(?:帮我|替我|我要|我想|请).{0,32}(?:提交|办理|申请|创建|发起|取消).{0,12}(退货|退款|取消订单|取消请求|取消操作|售后)", q)
    if action:
        target = action.group(1)
        capability = "create_return_request" if "退货" in target else \
                     "create_refund_request" if "退款" in target else "cancel_order"
        if ("订单" in q or "退款" in target or "退货" in target) and not order_id:
            return _decision("DIRECT", reason="MISSING_ORDER_IDENTIFIER", labels=["clarification", "business_action"],
                             clarify="请提供订单号；提交操作前还需要核实订单状态并由您确认。")
        return _decision("AGENT", sub="AGENT+SINGLE", reason="BUSINESS_ACTION_REQUEST",
                         labels=["business_action"], capabilities=[capability])
    return None


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Strategy
    sub_strategy: str | None = None
    intent_labels: list[str] = Field(default_factory=list)
    safety_decision: Literal["ALLOW", "ISOLATE_INJECTION", "REFUSE"]
    reason_code: str
    decision_source: Literal["RULE", "LLM", "FALLBACK"]
    model_id: str | None = None
    needs_clarification: bool = False
    clarification_question: str | None = None
    retrieval_query: str | None = Field(default=None, min_length=1, max_length=1000)
    required_capabilities: list[str] = Field(default_factory=list)
    route_version: str = ROUTE_VERSION
    latency_ms: int = 0
    prompt_version: str | None = None
    usage: dict | None = None
    fallback_error: str | None = None
    provider_diagnostic: dict | None = None

    @model_validator(mode="after")
    def consistent_contract(self):
        if self.needs_clarification != (self.clarification_question is not None):
            raise ValueError("clarification flag and question must agree")
        if (self.strategy == "REFUSE") != (self.safety_decision == "REFUSE"):
            raise ValueError("REFUSE strategy and safety decision must agree")
        allowed = {"DIRECT": {None}, "SINGLE": {None, "BM25"}, "MULTI": {None, "MULTI"},
                   "AGENT": {None, "AGENT+SINGLE", "AGENT+MULTI"}, "REFUSE": {None}}
        if self.sub_strategy not in allowed[self.strategy]:
            raise ValueError("sub_strategy is invalid for strategy")
        return self


def _decision(strategy: Strategy, *, sub: str | None = None, safety: str = "ALLOW",
              reason: str, labels: list[str], clarify: str | None = None,
              capabilities: list[str] | None = None, source: str = "RULE",
              model_id: str | None = None, usage: dict | None = None,
              latency_ms: int = 0) -> RouteDecision:
    return RouteDecision(strategy=strategy, sub_strategy=sub, safety_decision=safety,
        reason_code=reason, intent_labels=labels, needs_clarification=clarify is not None,
        clarification_question=clarify, required_capabilities=capabilities or [],
        decision_source=source, model_id=model_id, usage=usage, latency_ms=latency_ms,
        prompt_version=PROMPT_VERSION if source == "LLM" else None)


_PROVIDER_FAILURE_PHASES = {
    "PRE_REQUEST_LOCAL_FAILURE", "CONNECTION_NO_RESPONSE", "TIMEOUT_NO_RESPONSE",
    "HTTP_RESPONSE_RECEIVED", "RESPONSE_PARSE_OR_CONTRACT_FAILURE", "FAILURE_PHASE_UNKNOWN",
    "CLASSIFIER_SEMANTIC_CONTRACT",
}
_PROVIDER_ERROR_CATEGORIES = {
    "LOCAL_GUARD", "LOCAL_BUDGET_GUARD", "DNS_FAILURE", "TLS_FAILURE", "CONNECT_FAILURE",
    "TIMEOUT", "HTTP_ERROR", "RESPONSE_INVALID", "CLASSIFIER_OUTPUT_INVALID", "UNKNOWN",
}


class RouteClassifierContractError(ValueError):
    """Known local semantic contract rejection of a parsed classifier result."""
    def __init__(self, code: str, field_path: str):
        self.code, self.field_path = code, field_path
        super().__init__(code)


def _safe_route_response_metadata(value: object) -> dict | None:
    if not isinstance(value, dict) or value.get("schema_version") != "route-response-summary-v1":
        return None
    safe = {"schema_version": "route-response-summary-v1"}
    for key in ("provider_response_received", "content_present", "reasoning_content_present", "tool_calls_present"):
        if type(value.get(key)) is bool:
            safe[key] = value[key]
    for key in ("choice_count", "content_length", "reasoning_content_length"):
        number = value.get(key)
        if isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 1_000_000:
            safe[key] = number
    finish = value.get("finish_reason")
    if finish is None or (isinstance(finish, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,40}", finish)):
        safe["finish_reason"] = finish
    model = value.get("response_model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9._:/-]{1,100}", model):
        safe["response_model"] = model
    usage = _safe_usage(value.get("usage"))
    if usage is not None:
        safe["usage"] = usage
    if value.get("usage_status") in {"RECORDED", "UNKNOWN"}:
        safe["usage_status"] = value["usage_status"]
    return safe


def _safe_provider_diagnostic(value: object) -> dict | None:
    """Allow only bounded, typed metadata into persisted route events."""
    if not isinstance(value, dict) or value.get("schema_version") != "provider-failure-diagnostic-v1":
        return None
    phase, category = value.get("failure_phase"), value.get("error_category")
    if phase not in _PROVIDER_FAILURE_PHASES or category not in _PROVIDER_ERROR_CATEGORIES:
        return None
    call_entered, response_received = value.get("completion_call_entered"), value.get("provider_response_received")
    delivery = value.get("request_delivery")
    if type(call_entered) is not bool or type(response_received) is not bool or delivery not in {
        "NOT_ATTEMPTED", "OUTCOME_UNKNOWN", "RESPONSE_RECEIVED"
    }:
        return None
    exception_types = value.get("exception_types")
    if (not isinstance(exception_types, list) or len(exception_types) > 4 or
            any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", item)
                for item in exception_types)):
        return None
    safe = {"schema_version": "provider-failure-diagnostic-v1", "failure_phase": phase,
        "error_category": category, "completion_call_entered": call_entered,
        "provider_response_received": response_received, "request_delivery": delivery,
        "exception_types": exception_types}
    errno = value.get("errno")
    if isinstance(errno, int) and not isinstance(errno, bool) and -32768 <= errno <= 65535:
        safe["errno"] = errno
    timeout_stage = value.get("timeout_stage")
    if timeout_stage in {"CONNECT", "READ", "WRITE", "POOL"}:
        safe["timeout_stage"] = timeout_stage
    status = value.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        safe["http_status"] = status
    error_code = value.get("provider_error_code")
    if isinstance(error_code, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", error_code):
        safe["provider_error_code"] = error_code
    for field in ("usage_status", "billing_status"):
        if value.get(field) == "UNKNOWN":
            safe[field] = "UNKNOWN"
    issues = value.get("validation_issues")
    if isinstance(issues, list) and len(issues) <= 8:
        safe["validation_issues"] = [
            {"location": row["location"], "type": row["type"]}
            for row in issues if isinstance(row, dict) and
            isinstance(row.get("location"), str) and len(row["location"]) <= 120 and
            isinstance(row.get("type"), str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", row["type"])]
    route_stage = value.get("route_stage")
    if route_stage in {"PROVIDER_REQUEST", "RESPONSE_SHAPE", "FINISH_REASON", "CONTENT_JSON",
                       "ROUTE_SCHEMA", "ROUTE_MODEL_CONTRACT", "CLASSIFIER_SEMANTIC_CONTRACT",
                       "CAPABILITY_SCOPE", "ORDER_IDENTIFIER_CONTRACT"}:
        safe["route_stage"] = route_stage
    route_error = value.get("route_error_code")
    if isinstance(route_error, str) and re.fullmatch(r"[A-Z0-9_]{1,64}", route_error):
        safe["route_error_code"] = route_error
    field_path = value.get("field_path")
    if isinstance(field_path, str) and re.fullmatch(r"[A-Za-z0-9_.$\[\]-]{1,120}", field_path):
        safe["field_path"] = field_path
    response_summary = _safe_route_response_metadata(value.get("response_summary"))
    if response_summary is not None:
        safe["response_summary"] = response_summary
    usage = _safe_usage(value.get("usage"))
    if usage is not None:
        safe["usage"] = usage
    return safe


def safe_route_capability_scope_diagnostic(value: object) -> dict | None:
    """Project a capability rejection into bounded, non-content audit fields."""
    if not isinstance(value, dict) or value.get("schema_version") != "route-capability-scope-diagnostic-v1":
        return None
    allowed_names = set(_ALLOWED_AGENT_CAPABILITIES)
    projected: dict[str, list[str]] = {}
    for field in ("allowed_capabilities", "requested_capabilities", "disallowed_capabilities"):
        raw = value.get(field)
        if not isinstance(raw, list) or len(raw) > len(allowed_names):
            return None
        if any(not isinstance(item, str) or item not in allowed_names for item in raw):
            return None
        projected[field] = sorted(set(raw))
    if projected["disallowed_capabilities"] != sorted(
            set(projected["requested_capabilities"]) - set(projected["allowed_capabilities"])):
        return None
    strategy = value.get("candidate_strategy")
    sub_strategy = value.get("candidate_sub_strategy")
    if strategy not in {"DIRECT", "SINGLE", "MULTI", "AGENT", "REFUSE"}:
        return None
    if sub_strategy is not None and (not isinstance(sub_strategy, str) or
            not re.fullmatch(r"[A-Z0-9_+]{1,48}", sub_strategy)):
        return None
    received = value.get("provider_response_received")
    status = value.get("usage_status")
    local_rule = (value.get('stage') == 'RUNTIME_ROUTE_SCOPE' and value.get('decision_source') == 'RULE' and
        received is False and status == 'NOT_APPLICABLE_RULE_ROUTE' and value.get('usage') is None and
        value.get('response_summary') is None)
    if ((value.get("stage") != "CAPABILITY_SCOPE" and not local_rule) or
            value.get("error_code") != "ROUTE_CAPABILITY_SCOPE_REJECTED" or
            type(received) is not bool or (status not in {"RECORDED", "UNKNOWN"} and not local_rule)):
        return None
    summary = _safe_route_response_metadata(value.get("response_summary"))
    usage = _safe_usage(value.get("usage"))
    return {"schema_version": "route-capability-scope-diagnostic-v1",
        "stage": value['stage'], "error_code": "ROUTE_CAPABILITY_SCOPE_REJECTED",
        **({'decision_source':'RULE'} if local_rule else {}),
        **projected, "candidate_strategy": strategy, "candidate_sub_strategy": sub_strategy,
        "provider_response_received": received, "response_summary": summary,
        "usage": usage, "usage_status": status}


def _safe_usage(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    safe = {key: number for key, number in value.items()
        if key in {"prompt_tokens", "completion_tokens", "total_tokens"} and
        isinstance(number, int) and not isinstance(number, bool) and 0 <= number <= 10_000_000}
    return safe or None


def _fallback_clarification(query: str) -> str:
    """Keep fallback responses tied to the recognizable task without inventing results."""
    q = query.casefold()
    if re.search(r"\b(?:policy|rule|return|refund|warranty|coupon)\b|政策|规则|退货|退款|保修|优惠券", q):
        return "我暂时无法可靠识别这项政策问题；请指出想核实的规则或条件，我会只依据可检索到的政策说明。"
    if _order_identifier_present(query) or re.search(r"\b(?:order|status|delivered)\b|订单|物流|状态", q):
        return "我暂时无法可靠识别订单查询范围；如需查询，请提供本人账户下的订单号，我会先核验属主。"
    if re.search(r"\b(?:product|recommend|suggest|catalog|price|inventory)\b|商品|产品|推荐|库存|价格", q):
        return "我暂时无法可靠识别商品需求；请补充商品类别或筛选条件，我不会猜测商品或属性。"
    return "我暂时无法可靠识别这项请求；请具体说明希望核实的内容。"


def _contains_injection(query: str) -> bool:
    return bool(re.search(
        r"忽略.{0,16}(规则|指令|权限|属主校验)|(?:泄露|泄漏|打印).{0,16}(?:密钥|api.?key|系统提示|内部提示)|"
        r"\bignore\b.{0,36}\b(?:policy|rules?|owner\s+checks?|instructions?)\b|"
        r"\breveal\b.{0,48}\b(?:hidden\s+)?(?:system\s+prompt|api\s+keys?|secrets?)\b|"
        r"\bdisclose\b.{0,36}\b(?:another\s+user|someone\s+else)\b", query, re.I))


def _strip_attributed_untrusted_quotes(query: str) -> str:
    """Exclude quoted content explicitly presented as an untrusted note from action intent."""
    value = query or ""
    marker = re.compile(r"(?:says\s*[:：]|note\s+(?:in\s+my\s+message\s+)?says\s*[:：]?|"
        r"pasted\s+note\s+says\s*[:：]?|以下(?:内容|文字)为不可信|引述内容|引文|消息中写着)\s*", re.I)
    for match in reversed(list(marker.finditer(value))):
        tail = value[match.end():]
        quote_start = re.match(r"[\"'“‘]", tail)
        if not quote_start:
            continue
        opener = quote_start.group()
        closer = {"\"": "\"", "'": "'", "“": "”", "‘": "’"}[opener]
        close_at = tail.find(closer, 1)
        if close_at >= 0:
            start = match.end()
            value = value[:start] + " [untrusted quoted content] " + value[start + close_at + 1:]
    return value


def _obvious_route(query: str, available_capabilities: set[str] | None = None) -> RouteDecision | None:
    q = query.strip()
    injection = _contains_injection(q)
    security_intent = _strip_attributed_untrusted_quotes(q)
    reported_only = bool(re.search(r"(?:不可信引文|不可信内容).{0,80}(?:没有给出合法任务|没有合法任务|仅作引用)", q) and
        re.search(r"(?:不执行|不要执行|只澄清)", q))
    if reported_only:
        return _decision("DIRECT", safety="ISOLATE_INJECTION", reason="UNTRUSTED_QUOTE_NO_TASK", labels=["clarification"],
            clarify="这段引文不是操作授权；请说明您本人希望查询的具体问题，我不会执行引文要求。")
    if re.search(r"(?:请|帮我|替我).{0,12}伪造.{0,20}(?:政策|引用|批准|审批)", security_intent):
        return _decision("REFUSE", safety="REFUSE", reason="FABRICATED_EVIDENCE_REQUEST", labels=["security_boundary"])
    if re.search(r"跨用户.{0,16}(?:读取|查询|导出|发送|调取)", security_intent):
        return _decision("REFUSE", safety="REFUSE", reason="THIRD_PARTY_ORDER_ACCESS", labels=["security_boundary"])
    if _is_unauthorized_action(security_intent):
        return _decision("REFUSE", safety="REFUSE", reason=_unauthorized_reason_code(security_intent),
                         labels=["security_boundary"])
    if _is_security_policy_question(q):
        return _decision("SINGLE", sub="BM25", reason="SECURITY_POLICY_QUESTION",
                         labels=["policy", "security_boundary"], capabilities=["search_knowledge"])
    # A non-separable injection is refused. If it is embedded beside a normal
    # request, continue routing that request and isolate the injected text.
    separable_business = injection and (_order_identifier_present(q) or bool(re.search(
        r"订单|物流|退款|退货|取消|政策|规则|商品|优惠券|保修|质保|"
        r"\b(?:policy|rule|return|refund|order|product|coupon|warranty|status|delivered)\b", q, re.I)))
    if injection and not separable_business:
        return _decision("REFUSE", safety="REFUSE", reason="UNSAFE_REQUEST_NOT_ISOLATABLE",
                         labels=["security_boundary"])
    greeting = bool(re.fullmatch(r"(你好|您好|嗨|hi|hello|在吗|谢谢|感谢)[！!。\s]*", q, re.I))
    if greeting:
        return _decision("DIRECT", reason="GREETING", labels=["chitchat"],
                         safety="ISOLATE_INJECTION" if injection else "ALLOW")
    if _clarification_intent(q):
        return _decision("DIRECT", reason="TASK_NOT_SPECIFIED", labels=["clarification"],
                         clarify="请告诉我您具体想了解的政策或需要查询的业务信息。",
                         safety="ISOLATE_INJECTION" if injection else "ALLOW")

    # Filtering by a size needs a bound supplied in the input, not a fixture.
    size_filter_without_bound = bool(re.search(r"按.{0,20}(?:宽度|深度|高度|尺寸).{0,8}筛选", q) and
        not re.search(r"\d+(?:\.\d+)?\s*(?:毫米|厘米|mm|cm|米)", q, re.I))
    missing_context = bool(re.search(r"(?:没有|没给|未给|未提供|缺少|尚未确定|还没确定).{0,18}(?:阈值|上限|预算|尺寸|净宽|净深|深度|承重|型号|政策名称|活动名称|版本|渠道|上下文)", q))
    asks_first = bool(re.search(r"(?:先|首先|优先).{0,12}(?:问|询问|澄清|确认)|(?:需要|应该).{0,8}(?:补充|问我)|缺少.{0,16}(?:信息|条件)", q))
    fit_unknown = bool(re.search(r"能否|能不能|是否|适配|装得下|放进|能.{0,6}放", q) and re.search(r"安装|支架|柜|空间", q) and
        re.search(r"不知道|未提供|没给|不清楚|缺少", q))
    electrical_unknown = bool(re.search(r'接口|额定功率|电压',q) and re.search(r'没有提供|未提供|缺少|不知道',q) and asks_first)
    if (missing_context and asks_first) or fit_unknown or size_filter_without_bound or electrical_unknown:
        if size_filter_without_bound:
            question = "请提供用于筛选的宽度上限或范围，以及尺寸单位；缺少阈值时不能据此筛选商品或承诺适配。"
        elif electrical_unknown:
            question = "请提供设备接口、输入/输出电压和额定功率；信息不足时不能确认适配，也不保证电气安全兼容。"
        elif fit_unknown:
            question = "请提供安装空间的净宽、净深度和预留余量、安装方式、支架/承载面的承重及拟装设备重量；这些条件未核实时不能承诺安装适配。"
        elif re.search(r'正文|附录',q):
            question = "请提供正文与附录原文、各自版本和渠道，以及权威排序或审批依据；不能在这些事实缺失时自行裁定。"
        elif re.search(r'审批记录|审批凭证|已批准|自述',q):
            question = "请提供独立审批记录、所指的本人订单或具体事项及对应政策原文、版本和渠道；自述已批准不能替代核验。"
        else:
            question = "请补充具体政策或活动名称、原文、版本、渠道和相关日期；信息不足时我不会代猜适用条件或个人资格。"
        return _decision("DIRECT", reason="MISSING_TASK_CONSTRAINT", labels=["clarification"], clarify=question)

    # Policy evidence-boundary questions must not be mistaken for live lookups.
    source_boundary = bool(re.search(
        r"(?:政策|知识库|条文).{0,28}(?:证明|核实|确认|替代|得知|判断).{0,18}"
        r"(?:物流|签收|发货|订单状态|送达|签收日)|"
        r"(?:物流|签收|发货|订单状态|送达|签收日).{0,24}(?:政策|知识库|条文).{0,18}"
        r"(?:证明|核实|确认|替代|得知|判断)", q, re.I))
    window_fact_coupling = bool(re.search(
        r"(?:窗口|期限|时限|时间).{0,40}(?:订单事实|签收时间|哪项事实|从哪里核实|如何核实)|"
        r"(?:订单事实|签收时间|哪项事实|从哪里核实|如何核实).{0,40}(?:窗口|期限|时限|起算|如何算)", q, re.I))
    window_origin_boundary = source_boundary and bool(re.search(
        r"(?:无理由|退货|签收).{0,16}(?:期限|窗口|时限|时间).{0,28}(?:从何时|从哪天|起算|如何算|怎么算)", q, re.I))
    if window_fact_coupling or window_origin_boundary:
        return _decision("MULTI", sub="MULTI", safety="ISOLATE_INJECTION" if injection else "ALLOW",
                         reason="MULTI_POLICY_CONDITIONS", labels=["policy", "composite_policy", "source_boundary"],
                         capabilities=["search_knowledge"])
    if source_boundary:
        return _decision("SINGLE", sub="BM25", safety="ISOLATE_INJECTION" if injection else "ALLOW",
                         reason="POLICY_EVIDENCE_BOUNDARY", labels=["policy", "source_boundary"])

    live_business = _business_route(q, available_capabilities)
    if live_business is not None:
        return live_business.model_copy(update={
            "safety_decision": "ISOLATE_INJECTION" if injection else live_business.safety_decision})

    if _multi_policy_relation(q):
        return _decision("MULTI", sub="MULTI", safety="ISOLATE_INJECTION" if injection else "ALLOW",
                         reason="MULTI_POLICY_CONDITIONS",
                         labels=["policy", "composite_policy"], capabilities=["search_knowledge"])
    explicit_policy_question = bool(re.search(
        r"(?:政策|规则|条款|条件|期限|怎么|如何|是否|能否|可不可以|可以吗|是什么|为什么|起算|从何时|吗[？?。!！]*$|"
        r"(?:能|可以|可否|能不能).{0,16}(?:吗|么|否)|\b(?:does|do|is|are|what|which|how|can|could)\b)", q, re.I))
    policy_topic = bool(re.search(
        r"退货|退款|整单退|取消|政策|规则|保修|质保|保几年|保修年限|保修期限|"
        r"优惠券|优惠码|用券|券面|券|签收后|物流政策|质量问题|质量退货|故障|无理由期限|"
        r"\b(?:policy|policies|rules?|return|refund|warranty|coupon)\b", q, re.I))
    if policy_topic and explicit_policy_question:
        return _decision("SINGLE", sub="BM25", safety="ISOLATE_INJECTION" if injection else "ALLOW",
                         reason="POLICY_QUESTION", labels=["policy"], capabilities=["search_knowledge"])
    # Unknowns are clarified deterministically rather than guessed into a route.
    return None


def decide_route(query: str, classifier=None, *, available_capabilities: set[str] | list[str] | None = None) -> RouteDecision:
    """Apply the safety/task layer, then a bounded classifier only for ambiguous text."""
    started = time.perf_counter()
    allowed = None if available_capabilities is None else set(available_capabilities)
    if allowed is not None and not allowed.issubset(_ALLOWED_AGENT_CAPABILITIES):
        raise RouteCapabilityScopeError("route scope contains an unregistered capability")
    decision = _obvious_route(query, allowed)
    if decision is None:
        decision = _decision("DIRECT", reason="UNCERTAIN_INTENT", labels=["uncertain"],
                             clarify=_fallback_clarification(query), source="FALLBACK")
        if classifier is not None and hasattr(classifier, "classify_route"):
            candidate = None
            classifier_stage = "PROVIDER_REQUEST"
            try:
                classify = classifier.classify_route
                parameters = inspect.signature(classify).parameters
                if allowed is not None and ("available_capabilities" in parameters or any(
                        item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values())):
                    candidate = classify(query, available_capabilities=sorted(allowed))
                else:
                    candidate = classify(query)
                classifier_stage = "ROUTE_SCHEMA"
                candidate = RouteDecision.model_validate(candidate)
                classifier_stage = "CLASSIFIER_SEMANTIC_CONTRACT"
                _validate_classifier_decision(candidate)
                if allowed is not None and not set(candidate.required_capabilities).issubset(allowed):
                    requested = sorted(set(candidate.required_capabilities))
                    permitted = sorted(allowed)
                    response_summary = _safe_route_response_metadata(
                        getattr(classifier, "last_route_response_summary", None))
                    usage = _safe_usage(candidate.usage) or (
                        _safe_usage(response_summary.get("usage")) if response_summary else None)
                    raise RouteCapabilityScopeError(
                        "classifier route exceeds current request capability scope",
                        diagnostic={
                            "schema_version": "route-capability-scope-diagnostic-v1",
                            "stage": "CAPABILITY_SCOPE",
                            "error_code": "ROUTE_CAPABILITY_SCOPE_REJECTED",
                            "allowed_capabilities": permitted,
                            "requested_capabilities": requested,
                            "disallowed_capabilities": sorted(set(requested) - set(permitted)),
                            "candidate_strategy": candidate.strategy,
                            "candidate_sub_strategy": candidate.sub_strategy,
                            "provider_response_received": bool(response_summary and
                                response_summary.get("provider_response_received") is True),
                            "response_summary": response_summary,
                            "usage": usage,
                            "usage_status": "RECORDED" if usage else "UNKNOWN",
                        })
                if candidate.strategy == "AGENT" and set(candidate.required_capabilities) & {
                    "get_order", "get_order_items", "track_shipment", "get_refund_status",
                    "create_refund_request", "create_return_request", "cancel_order",
                } and not _order_identifier_present(query):
                    raise RouteClassifierContractError("ORDER_IDENTIFIER_REQUIRED", "required_capabilities")
                # Recheck security after classification. A model can add a refusal,
                # but the deterministic guard always wins and can never be relaxed.
                if _is_unauthorized_action(query):
                    candidate = _decision("REFUSE", safety="REFUSE", reason=_unauthorized_reason_code(query),
                                          labels=["security_boundary"])
                decision = candidate.model_copy(update={"decision_source": "LLM",
                    "route_version": ROUTE_VERSION, "prompt_version": PROMPT_VERSION})
            except RouteCapabilityScopeError:
                raise
            except Exception as exc:
                diagnostic = getattr(exc, "diagnostic", None)
                provider_diagnostic = _safe_provider_diagnostic(
                    diagnostic.get("provider_diagnostic") if isinstance(diagnostic, dict) else None)
                route_error_code = None
                field_path = None
                if isinstance(exc, RouteClassifierContractError):
                    route_error_code, field_path = exc.code, exc.field_path
                    classifier_stage = ("ORDER_IDENTIFIER_CONTRACT" if exc.code == "ORDER_IDENTIFIER_REQUIRED"
                                        else "CLASSIFIER_SEMANTIC_CONTRACT")
                elif isinstance(exc, RouteCapabilityScopeError):
                    raise
                elif hasattr(exc, "errors"):
                    route_error_code = "ROUTE_DECISION_SCHEMA_INVALID"
                    try:
                        first_issue = exc.errors(include_input=False)[0]
                        field_path = ".".join(str(part)[:40] for part in first_issue.get("loc", [])[:6]) or "$"
                    except (TypeError, ValueError, IndexError):
                        field_path = "$"
                response_summary = _safe_route_response_metadata(
                    getattr(classifier, "last_route_response_summary", None))
                candidate_usage = candidate.usage if isinstance(candidate, RouteDecision) else (
                    candidate.get("usage") if isinstance(candidate, dict) else None)
                safe_usage = _safe_usage(candidate_usage)
                if safe_usage is None and response_summary:
                    safe_usage = _safe_usage(response_summary.get("usage"))
                if provider_diagnostic is None:
                    received = bool(response_summary and response_summary.get("provider_response_received") is True)
                    provider_diagnostic = {
                        "schema_version": "provider-failure-diagnostic-v1",
                        "failure_phase": ("CLASSIFIER_SEMANTIC_CONTRACT" if received else "FAILURE_PHASE_UNKNOWN"),
                        "error_category": ("CLASSIFIER_OUTPUT_INVALID" if received else "UNKNOWN"),
                        "completion_call_entered": True,
                        "provider_response_received": received,
                        "request_delivery": "RESPONSE_RECEIVED" if received else "OUTCOME_UNKNOWN",
                        "exception_types": [type(exc).__name__],
                        "usage_status": (response_summary.get("usage_status", "UNKNOWN")
                                         if response_summary else "UNKNOWN"),
                        "billing_status": "UNKNOWN",
                        "route_stage": classifier_stage,
                        "route_error_code": route_error_code or "CLASSIFIER_DECISION_INVALID",
                        "field_path": field_path or "$",
                    }
                    if response_summary:
                        provider_diagnostic["response_summary"] = response_summary
                    if safe_usage:
                        provider_diagnostic["usage"] = safe_usage
                    provider_diagnostic = _safe_provider_diagnostic(provider_diagnostic)
                code = getattr(exc, "code", None)
                safe_error = (route_error_code or (code if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", code)
                              else type(exc).__name__))
                provider_exception = ((diagnostic or {}).get("provider_exception")
                                      if isinstance(diagnostic, dict) else None)
                if not isinstance(provider_exception, str) or not re.fullmatch(
                        r"[A-Za-z][A-Za-z0-9_]{0,63}", provider_exception):
                    provider_exception = (provider_diagnostic or {}).get("exception_types", [None])[0]
                if isinstance(provider_exception, str):
                    safe_error += f":{provider_exception}"
                provider_status = (provider_diagnostic or {}).get("http_status")
                if (provider_status is None and isinstance(diagnostic, dict) and
                        isinstance(diagnostic.get("provider_status"), int) and
                        not isinstance(diagnostic.get("provider_status"), bool) and
                        100 <= diagnostic["provider_status"] <= 599):
                    provider_status = diagnostic["provider_status"]
                if isinstance(provider_status, int):
                    safe_error += f":HTTP_{provider_status}"
                decision = decision.model_copy(update={"reason_code": "LLM_CLASSIFICATION_FALLBACK",
                    "fallback_error": safe_error[:80], "provider_diagnostic": provider_diagnostic,
                    "usage": safe_usage or _safe_usage(diagnostic.get("usage") if isinstance(diagnostic, dict) else None)})
    updates = {"latency_ms": decision.latency_ms or max(1, round((time.perf_counter()-started)*1000))}
    if _contains_injection(query) and decision.strategy != "REFUSE":
        updates["safety_decision"] = "ISOLATE_INJECTION"
    if decision.strategy == "DIRECT" and not decision.clarification_question and decision.reason_code != "GREETING":
        updates["needs_clarification"] = True
        updates["clarification_question"] = _fallback_clarification(query)
    return decision.model_copy(update=updates)


_ALLOWED_AGENT_CAPABILITIES = {
    "list_orders", "get_order", "get_order_items", "track_shipment", "get_refund_status",
    "create_refund_request", "create_return_request", "cancel_order",
    "search_products", "get_product_detail", "recommend_products",
    "get_coupon", "check_inventory", "get_warranty", "search_knowledge",
    "escalate_to_human",
}


def _validate_classifier_decision(candidate: RouteDecision) -> None:
    if candidate.provider_diagnostic is not None or candidate.fallback_error is not None:
        raise RouteClassifierContractError("FAILURE_METADATA_IN_CLASSIFIER_OUTPUT", "provider_diagnostic")
    caps = set(candidate.required_capabilities)
    if candidate.strategy in {"DIRECT", "REFUSE"} and caps:
        raise RouteClassifierContractError("DIRECT_REFUSE_CAPABILITY_MISMATCH", "required_capabilities")
    if candidate.strategy == "REFUSE" and candidate.safety_decision != "REFUSE":
        raise RouteClassifierContractError("REFUSE_SAFETY_MISMATCH", "safety_decision")
    if candidate.strategy == "SINGLE":
        if candidate.sub_strategy not in {None, "BM25"} or caps - {"search_knowledge"}:
            raise RouteClassifierContractError("SINGLE_CAPABILITY_CONTRACT_INVALID",
                "sub_strategy" if candidate.sub_strategy not in {None, "BM25"} else "required_capabilities")
    if candidate.strategy == "MULTI":
        if candidate.sub_strategy not in {None, "MULTI"} or caps - {"search_knowledge"}:
            raise RouteClassifierContractError("MULTI_CAPABILITY_CONTRACT_INVALID",
                "sub_strategy" if candidate.sub_strategy not in {None, "MULTI"} else "required_capabilities")
    if candidate.strategy == "AGENT":
        if candidate.sub_strategy not in {None, "AGENT+SINGLE", "AGENT+MULTI"}:
            raise RouteClassifierContractError("AGENT_SUBSTRATEGY_INVALID", "sub_strategy")
        if (not caps or not caps <= _ALLOWED_AGENT_CAPABILITIES or
                ("list_orders" in caps and caps != {"list_orders"})):
            raise RouteClassifierContractError("AGENT_CAPABILITY_SET_INVALID", "required_capabilities")
