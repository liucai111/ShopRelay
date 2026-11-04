"""Owner-independent public catalog readers backed by the shared business DB."""
from __future__ import annotations

import re
import json
import math
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.m1.models import Inventory, Product
from .order_tools import ToolExecutionError

_PRODUCT_ID = re.compile(r"(?<![A-Za-z0-9])P-[A-Z0-9_-]{2,48}(?![A-Za-z0-9])", re.I)
_CATEGORY_ALIASES = {
    "appliance": ("电器", "家电", "水壶", "电热水壶"),
    "custom": ("定制", "马克杯", "杯子"),
    "food": ("食品", "零食", "坚果", "礼盒"),
}
_CATEGORY_LABELS = {"appliance": "家电", "custom": "定制商品", "food": "食品"}


def _catalog_search_evidence(search: dict[str, Any], result_ref: str) -> dict[str, Any]:
    """A catalog result reference is business evidence, never a document citation."""
    return {"kind": "catalog_search_result", "source": search.get("source"),
        "tool": "search_products", "result_ref": result_ref,
        "facts": [{"field": "matched_count", "value": search.get("count", len(search.get("products", [])))}],
        "scope_complete": not bool(search.get("truncated"))}


@dataclass(frozen=True)
class ProductFilters:
    keyword: str = ""
    category: str | None = None
    max_price_cents: int | None = None
    limit: int = 10

    def validate(self) -> "ProductFilters":
        if len(self.keyword) > 80 or len(self.keyword.strip()) > 80:
            raise ToolExecutionError("PRODUCT_QUERY_INVALID")
        if self.category is not None and self.category not in _CATEGORY_LABELS:
            raise ToolExecutionError("PRODUCT_CATEGORY_UNSUPPORTED")
        if self.max_price_cents is not None and not 0 <= self.max_price_cents <= 100_000_000:
            raise ToolExecutionError("PRODUCT_FILTER_INVALID")
        if not 1 <= self.limit <= 20:
            raise ToolExecutionError("PRODUCT_LIMIT_INVALID")
        return self


def parse_product_request_many(query: str) -> tuple[list[str], ProductFilters, bool]:
    """Extract bounded catalog filters; query remains ephemeral and untrusted."""
    if len(query) > 20_000:
        raise ToolExecutionError("PRODUCT_QUERY_INVALID")
    ids = list(dict.fromkeys(match.upper() for match in _PRODUCT_ID.findall(query)))
    category = next((key for key, aliases in _CATEGORY_ALIASES.items()
                     if any(alias in query for alias in aliases)), None)
    budget_match = re.search(r"(?:不超过|低于|小于)\s*(\d{1,7})\s*(?:元|块)?|"
        r"预算(?:不超过|最多)?\s*(\d{1,7})\s*(?:元|块)?(?:以内|以下)?|"
        r"(\d{1,7})\s*(?:元|块)(?:以内|以下)", query)
    budget_value = next((group for group in budget_match.groups() if group), None) if budget_match else None
    max_price = int(budget_value) * 100 if budget_value else None
    cleaned = _PRODUCT_ID.sub(" ", query)
    cleaned = re.sub(r"(?:不超过|低于|小于)\s*\d{1,7}\s*(?:元|块)?|"
                     r"预算(?:不超过|最多)?\s*\d{1,7}\s*(?:元|块)?(?:以内|以下)?|"
                     r"\d{1,7}\s*(?:元|块)(?:以内|以下)", " ", cleaned)
    cleaned = re.sub(r"推荐|帮我|请|搜索|搜一下|查找|查一下|查询|查看|商品|产品|详情|介绍|信息|当前|现在|库存|有货|现货|可售|价格|价钱|售价|多少钱|一款|一个|几款|的|一下|哪些|有什么|有哪些|能否|是否|和|并|且|\s+", " ", cleaned)
    named = re.search(r'(?:明确)?(?:商品|产品)(?:为|是|叫作|名称为)[：:\s]*([^（）()，。；;]{1,80})',query)
    keyword = named.group(1).strip() if named else cleaned.strip(" ，,。！？?！")[:80]
    in_stock_required = bool(re.search(r"现货|有货|库存充足|可售", query))
    return ids, ProductFilters(keyword=keyword, category=category,
        max_price_cents=max_price).validate(), in_stock_required


def parse_product_request(query: str) -> tuple[str | None, ProductFilters, bool]:
    """Compatibility parser for capabilities that require at most one product."""
    ids, filters, in_stock_required = parse_product_request_many(query)
    if len(ids) > 1:
        raise ToolExecutionError("PRODUCT_ID_AMBIGUOUS")
    return (ids[0] if ids else None), filters, in_stock_required


class ProductReadRepository:
    """Allowlisted read methods only; no SQL fragments or caller-supplied columns."""
    def __init__(self, factory: sessionmaker) -> None:
        self.factory = factory

    @staticmethod
    def _row(row: Product) -> dict[str, Any]:
        return {"product_id": row.product_id, "name": row.name, "category": row.category,
                "price_cents": row.price_cents, "description": None,
                "description_status": "NOT_STORED"}

    def search_products(self, filters: ProductFilters) -> dict[str, Any]:
        filters.validate()
        stmt = select(Product)
        if filters.keyword:
            pattern = filters.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{pattern}%"
            stmt = stmt.where(or_(Product.name.ilike(pattern, escape="\\"),
                                  Product.category.ilike(pattern, escape="\\")))
        if filters.category:
            stmt = stmt.where(Product.category == filters.category)
        if filters.max_price_cents is not None:
            stmt = stmt.where(Product.price_cents <= filters.max_price_cents)
        count_stmt = select(func.count()).select_from(stmt.order_by(None).subquery())
        stmt = stmt.order_by(Product.product_id).limit(filters.limit)
        with self.factory() as db:
            total_count = int(db.execute(count_stmt).scalar_one())
            rows = db.execute(stmt).scalars().all()
        return {"availability": "AVAILABLE", "products": [self._row(row) for row in rows],
                "count": total_count, "truncated": total_count > len(rows),
                "source": "shoprelay_product_catalog"}

    def get_product_detail(self, product_id: str) -> dict[str, Any]:
        _validate_product_id(product_id)
        with self.factory() as db:
            row = db.get(Product, product_id)
        if row is None:
            return {"availability": "NOT_FOUND", "product_id": product_id,
                    "error_code": "PRODUCT_NOT_FOUND", "source": "shoprelay_product_catalog"}
        return {"availability": "AVAILABLE", "product": self._row(row),
                "source": "shoprelay_product_catalog"}

    def check_inventory(self, product_id: str | None, filters: ProductFilters | None = None) -> dict[str, Any]:
        if product_id is not None:
            _validate_product_id(product_id)
        filters = (filters or ProductFilters(limit=20)).validate()
        with self.factory() as db:
            if product_id is not None:
                product = db.get(Product, product_id)
                if product is None:
                    return {"availability": "NOT_FOUND", "product_id": product_id,
                            "error_code": "PRODUCT_NOT_FOUND", "source": "shoprelay_inventory"}
                rows = db.execute(select(Product, Inventory).outerjoin(
                    Inventory, Inventory.product_id == Product.product_id).where(
                    Product.product_id == product_id)).all()
            else:
                rows = self._filtered_product_inventory(db, filters)
        if not rows:
            return {"availability": "AVAILABLE", "records": [], "count": 0,
                    "source": "shoprelay_inventory"}
        records = []
        for product, row in rows:
            if row is None:
                records.append({"availability": "UNAVAILABLE", "product_id": product.product_id,
                    "error_code": "INVENTORY_RECORD_MISSING"})
                continue
            if row.available_qty < row.reserved_qty:
                raise ToolExecutionError("INVENTORY_DATA_INVALID")
            sellable = row.available_qty - row.reserved_qty
            # The existing snapshot has no observed_at timestamp, so its
            # quantities cannot establish current sellability.
            records.append({"availability": "UNAVAILABLE", "product_id": product.product_id,
                "available_qty": row.available_qty, "reserved_qty": row.reserved_qty,
                "snapshot_sellable_qty": sellable, "snapshot_is_available": sellable > 0,
                "error_code": "INVENTORY_FRESHNESS_UNVERIFIED",
                "version": row.version, "observation_time": None,
                "freshness": "DATABASE_SNAPSHOT_NO_OBSERVED_AT"})
        top_availability = (records[0]["availability"] if product_id is not None else
                            "UNAVAILABLE" if any(r["availability"] == "UNAVAILABLE" for r in records)
                            else "AVAILABLE")
        return {"availability": top_availability, "product_id": product_id,
                "records": records, "count": len(records),
                "error_code": records[0].get("error_code") if product_id else
                    next((r.get("error_code") for r in records if r.get("error_code")), None),
                "source": "shoprelay_inventory"}

    @staticmethod
    def _filtered_product_inventory(db: Session, filters: ProductFilters):
        stmt = select(Product, Inventory).outerjoin(Inventory, Inventory.product_id == Product.product_id)
        if filters.keyword:
            pattern = filters.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{pattern}%"
            stmt = stmt.where(or_(Product.name.ilike(pattern, escape="\\"),
                                  Product.category.ilike(pattern, escape="\\")))
        if filters.category:
            stmt = stmt.where(Product.category == filters.category)
        if filters.max_price_cents is not None:
            stmt = stmt.where(Product.price_cents <= filters.max_price_cents)
        return db.execute(stmt.order_by(Product.product_id).limit(filters.limit)).all()

    def recommend_products(self, filters: ProductFilters, *, require_inventory: bool = False) -> dict[str, Any]:
        filters.validate()
        stmt = select(Product, Inventory).outerjoin(Inventory, Inventory.product_id == Product.product_id)
        if filters.keyword:
            pattern = filters.keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            pattern = f"%{pattern}%"
            stmt = stmt.where(or_(Product.name.ilike(pattern, escape="\\"),
                                  Product.category.ilike(pattern, escape="\\")))
        if filters.category:
            stmt = stmt.where(Product.category == filters.category)
        # Keep over-budget alternatives so the result can label them PARTIAL_MATCH.
        stmt = stmt.order_by(Product.price_cents, Product.product_id).limit(filters.limit)
        with self.factory() as db:
            rows = db.execute(stmt).all()
        candidates = []
        for product, inventory in rows:
            reasons = []
            if filters.category:
                reasons.append(f"类别匹配：{_CATEGORY_LABELS[filters.category]}")
            if filters.keyword:
                reasons.append("名称或类别包含请求关键词")
            within_budget = filters.max_price_cents is None or product.price_cents <= filters.max_price_cents
            if filters.max_price_cents is not None:
                reasons.append(("目录价格符合预算" if within_budget else "目录价格超过预算") +
                               f"（上限 {filters.max_price_cents} 分）")
            if require_inventory:
                if inventory is None:
                    status = "UNCONFIRMED" if within_budget else "PARTIAL_MATCH"
                    reasons.append("缺少库存记录，不能确认有货")
                elif inventory.available_qty < inventory.reserved_qty:
                    raise ToolExecutionError("INVENTORY_DATA_INVALID")
                else:
                    status = "UNCONFIRMED" if within_budget else "PARTIAL_MATCH"
                    reasons.append("库存数据没有观测时间，无法验证当前可售状态")
            else:
                status = "FULL_MATCH" if within_budget else "PARTIAL_MATCH"
                reasons.append("匹配本次可验证的商品条件")
            candidates.append({"product": self._row(product), "match_status": status,
                               "match_reasons": reasons,
                               "inventory_version": inventory.version if inventory else None})
        return {"availability": "AVAILABLE", "recommendations": candidates,
                "count": len(candidates), "source": "shoprelay_product_catalog"}


def _validate_product_id(product_id: str) -> None:
    if not isinstance(product_id, str) or not re.fullmatch(r"P-[A-Z0-9_-]{2,48}", product_id):
        raise ToolExecutionError("PRODUCT_ID_INVALID")


def build_product_business_result(supervisor_result: dict[str, Any], *, route: dict[str, Any],
                                  result_store: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    allowed = {"search_products", "get_product_detail", "recommend_products", "check_inventory"}
    capabilities = route.get("required_capabilities")
    if not isinstance(capabilities, list) or not capabilities or not set(capabilities).issubset(allowed):
        return None
    handoffs = supervisor_result.get("handoffs") or []
    if supervisor_result.get("supervisor_status") != "COMPLETED" or \
       supervisor_result.get("terminal_status") != "RUNNING" or len(handoffs) != 1:
        return None
    handoff = handoffs[0]
    if handoff.get("target_agent") != "product" or handoff.get("status") != "COMPLETED" or \
       sorted(handoff.get("required_capabilities") or []) != sorted(capabilities):
        return None
    agent_result = handoff.get("result") or {}
    if agent_result.get("source_agent") != "product" or agent_result.get("status") != "COMPLETED":
        return None
    if agent_result.get("missing_information") == ["PRODUCT_CANDIDATES_NOT_FOUND"]:
        refs = agent_result.get("tool_result_refs") or []
        search_status = next((row for row in supervisor_result.get("tool_execution_status", [])
            if row.get("capability") == "search_products"), None)
        search = result_store.get(refs[0]) if len(refs) == 1 else None
        if (not search_status or not search_status.get("executed") or
                not isinstance(search, dict) or search.get("source") != "shoprelay_product_catalog" or
                search.get("products") != []):
            return None
        return {"answer": "目录检索没有找到匹配的商品记录；请补充商品类别、品牌或型号后再查。",
            "answer_kind": "CLARIFICATION_REQUIRED", "business_results": {"search_products": search},
            "tool_references": [{"tool": "search_products", "source": search["source"], "result_ref": refs[0]}],
            "business_evidence": [_catalog_search_evidence(search, refs[0])],
            "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "latency_ms": sum(float(row.get("duration_ms", 0)) for row in supervisor_result.get("tool_execution_status", [])
                if row.get("executed"))}
    if agent_result.get("missing_information") == ["PRODUCT_DETAIL_READ_BUDGET_EXHAUSTED"]:
        refs = agent_result.get("tool_result_refs") or []
        search_status = next((row for row in supervisor_result.get("tool_execution_status", [])
            if row.get("capability") == "search_products"), None)
        search = result_store.get(refs[0]) if len(refs) == 1 else None
        if (not search_status or not search_status.get("executed") or not isinstance(search, dict) or
                search.get("source") != "shoprelay_product_catalog"):
            return None
        return {"answer": "已找到目录候选，但本次只读工具预算不足以读取商品详情；没有据此进行属性比较。",
            "answer_kind": "CLARIFICATION_REQUIRED", "business_results": {"search_products": search},
            "tool_references": [{"tool": "search_products", "source": search["source"], "result_ref": refs[0]}],
            "business_evidence": [_catalog_search_evidence(search, refs[0])],
            "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "latency_ms": sum(float(row.get("duration_ms", 0)) for row in supervisor_result.get("tool_execution_status", [])
                if row.get("executed"))}
    if (agent_result.get("missing_information") == ["PRODUCT_CANDIDATES_TRUNCATED"] and
            not any(row.get("capability") == "get_product_detail" and row.get("executed")
                    for row in supervisor_result.get("tool_execution_status", []))):
        refs = agent_result.get("tool_result_refs") or []
        search_status = next((row for row in supervisor_result.get("tool_execution_status", [])
            if row.get("capability") == "search_products"), None)
        search = next((result_store.get(ref) for ref in refs if isinstance(result_store.get(ref), dict)
            and result_store[ref].get("source") == "shoprelay_product_catalog"), None)
        if not search_status or not search_status.get("executed") or not isinstance(search, dict):
            return None
        return {"answer": "目录已返回候选，但只读工具预算没有剩余详情读取额度；未对候选属性作比较。",
            "answer_kind": "CLARIFICATION_REQUIRED", "business_results": {"search_products": search},
            "tool_references": [{"tool": "search_products", "source": search["source"],
                "result_ref": next(ref for ref in refs if result_store.get(ref) is search)}],
            "business_evidence": [_catalog_search_evidence(search,
                next(ref for ref in refs if result_store.get(ref) is search))],
            "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "latency_ms": sum(float(row.get("duration_ms", 0)) for row in supervisor_result.get("tool_execution_status", [])
                if row.get("executed"))}
    if agent_result.get("missing_information") == ["PRODUCT_ID_REQUIRED"] and not agent_result.get("tool_result_refs"):
        return {"answer": "请提供商品编号，以便查询该商品详情或库存。",
                "answer_kind": "CLARIFICATION_REQUIRED", "business_results": {},
                "tool_references": [],
                "tool_execution_status": supervisor_result.get("tool_execution_status", []),
                "latency_ms": 0}
    if agent_result.get("missing_information") == ["PRODUCT_ID_AMBIGUOUS"] and not agent_result.get("tool_result_refs"):
        return {"answer": "请求中存在多个可能的商品对象；如需比较，请明确指定要比较的两个商品编号或补充具体型号。",
                "answer_kind": "CLARIFICATION_REQUIRED", "business_results": {},
                "tool_references": [],
                "tool_execution_status": supervisor_result.get("tool_execution_status", []),
                "latency_ms": 0}
    refs = agent_result.get("tool_result_refs") or []
    statuses = {item.get("capability"): item for item in supervisor_result.get("tool_execution_status", [])}
    if len(refs) < len(capabilities) or len(refs) != len(set(refs)):
        return None
    results: dict[str, dict[str, Any]] = {}
    references = []
    position = 0
    details = []
    detail_refs: list[str] = []
    for capability in sorted(capabilities):
        status = statuses.get(capability)
        if not status or not status.get("executed") or status.get("status") not in {"COMPLETED", "UNAVAILABLE", "NOT_FOUND"}:
            return None
        found = []
        found_refs = []
        while position < len(refs):
            ref = refs[position]
            value = result_store.get(ref)
            if not isinstance(value, dict) or value.get("source") not in {"shoprelay_product_catalog", "shoprelay_inventory"}:
                return None
            if capability == "get_product_detail" and not ("product" in value or value.get("availability") == "NOT_FOUND"):
                break
            found.append(value)
            found_refs.append(ref)
            references.append({"tool": capability, "source": value["source"], "result_ref": ref})
            position += 1
            if capability != "get_product_detail":
                break
        if not found:
            return None
        if capability == "get_product_detail":
            details = found
            detail_refs = found_refs
        results[capability] = found[0] if len(found) == 1 else {"records": found, "availability": "AVAILABLE"}
    if position != len(refs):
        return None
    comparison_requested = "product_comparison" in (route.get("intent_labels") or [])
    phrases: list[str] = []
    for detail in details:
        if comparison_requested:
            continue
        if detail["availability"] == "NOT_FOUND":
            phrases.append(f"未找到商品 {detail['product_id']}。")
        else:
            product = detail["product"]
            phrases.append(f"{product['name']}（商品编号 {product['product_id']}），类别：{product['category'] or '未知'}；目录记录价格：{str(product['price_cents']) + ' 分' if product['price_cents'] is not None else '未知'}。")
            if product.get("recorded_attributes"):
                phrases.append("已记录属性：" + json.dumps(product["recorded_attributes"], ensure_ascii=False, sort_keys=True) + "。")
            phrases.append("商品描述未存储在当前数据源中。")
    search = results.get("search_products")
    if search and not comparison_requested:
        products = search.get("products", [])
        phrases.append(f"商品检索本次展示 {len(products)} 条，匹配总数 {search.get('count', len(products))} 条" +
            ("，该结果页已截断" if search.get("truncated") else "") + "；记录为：" + ("；".join(
            f"{item['name']}（{item['product_id']}，{item['category'] or '未知类别'}，目录价格 {str(item['price_cents']) + ' 分' if item['price_cents'] is not None else '未知'}）"
            for item in products) if products else "没有匹配的目录记录") + "。")
    inventory = results.get("check_inventory")
    if inventory:
        if inventory["availability"] == "NOT_FOUND":
            phrases.append(f"未找到商品 {inventory['product_id']}，没有可查询的库存。")
        elif inventory.get("product_id"):
            row = next(iter(inventory.get("records", [])), {})
            if row.get("snapshot_sellable_qty") is not None:
                quantities='、'.join(f"{label} {row[key]} 件" for key,label in
                    (('available_qty','在库'),('reserved_qty','预留'),('snapshot_sellable_qty','快照可售')) if row.get(key) is not None)
                phrases.append(f"商品 {inventory['product_id']} 的数据库库存快照记录{quantities}；来源 shoprelay_inventory，但没有观测时间，无法确认当前库存。")
            else:
                phrases.append(f"商品 {inventory['product_id']} 缺少可用库存记录，当前无法确认是否有货。")
        elif inventory.get("records"):
            phrases.append("库存查询结果：" + "；".join(
                f"{row['product_id']}：" + (f"记录快照可售 {row['snapshot_sellable_qty']} 件，但没有观测时间，无法确认当前库存"
                                            if row.get("snapshot_sellable_qty") is not None
                                            else "缺少库存记录，无法确认") for row in inventory["records"]) + "。")
        else:
            phrases.append("没有匹配的库存数据，当前无法确认是否有货。")
    recommendations = results.get("recommend_products")
    if recommendations:
        rows = recommendations.get("recommendations", [])
        phrases.append("推荐结果：" + ("；".join(
            f"{row['product']['name']}（{row['product']['product_id']}，{row['match_status']}：{ '、'.join(row['match_reasons']) }）"
            for row in rows) if rows else "没有符合已验证条件的目录候选") + "。")
        for row in rows:
            if row["product"].get("recorded_attributes"):
                phrases.append(row["product"]["name"] + "的已记录属性：" + json.dumps(
                    row["product"]["recorded_attributes"], ensure_ascii=False, sort_keys=True) + "。")
    comparison = None
    if comparison_requested:
        labels = set(route.get("intent_labels") or [])
        attribute = ("weight_g" if "product_attribute_weight_g" in labels else
            "price_cents" if "product_attribute_price_cents" in labels else
            "input_voltage" if "product_attribute_input_voltage" in labels else None)
        direction = ("MIN" if "product_direction_min" in labels else
            "MAX" if "product_direction_max" in labels else "SIDE_BY_SIDE")
        evidence = [detail.get("product", {}).get("fixture_provenance") for detail in details
            if isinstance(detail.get("product"), dict) and detail["product"].get("fixture_provenance")]
        products = [detail.get("product") for detail in details if detail.get("availability") == "AVAILABLE"
            and isinstance(detail.get("product"), dict)]
        missing_products = [detail.get("product_id", "unknown product") for detail in details
            if detail.get("availability") != "AVAILABLE"]
        available_fields = set.intersection(*(set(row.get("recorded_attributes", {})) | {"price_cents"}
            for row in products)) if products else set()
        scope_incomplete = bool(search and (search.get("truncated") or
            len(details) < len(search.get("products", [])) or
            len(details) < int(search.get("count", len(search.get("products", []))))))
        if agent_result.get("missing_information") == ["PRODUCT_CANDIDATES_TRUNCATED"]:
            scope_incomplete = True
        comparison = {"attribute": attribute, "direction": direction, "evidence_ids": evidence}
        if "product_attribute_unsupported" in labels:
            comparison.update(status="UNSUPPORTED_REQUESTED_ATTRIBUTE", requested_attribute="not_supported")
            phrases.append("当前请求的比较属性没有对应的受支持可靠字段；不能用重量或价格替代该属性的结论。请确认属性或提供可核验来源。")
        elif attribute is None:
            field_names = {"weight_g": "重量", "price_cents": "目录价格"}
            supported = sorted(field_names[key] for key in available_fields if key in field_names)
            comparison.update(status="COMPARISON_ATTRIBUTE_NOT_SPECIFIED",
                supported_attributes=supported)
            phrases.append("我可以依据目录记录比较" + ("、".join(supported) if supported else "的字段有限") +
                "；请说明要比较的具体属性。")
        elif len(products) + len(missing_products) < 2:
            comparison.update(status="INSUFFICIENT_CANDIDATES", candidate_count=len(products) + len(missing_products))
            phrases.append("当前只取得一个或更少的商品详情，无法完成两款商品比较；请补充另一个商品编号或更具体的检索条件。")
        elif missing_products:
            comparison.update(status="PRODUCT_DETAILS_NOT_FOUND", missing_product_ids=missing_products)
            phrases.append("有指定商品编号未找到对应目录记录，无法对指定商品完成比较；请核对商品编号。")
        else:
            values = []
            missing_attribute = []
            for product in products:
                value = product.get("price_cents") if attribute == "price_cents" else (
                    product.get("recorded_attributes", {}).get(attribute))
                valid = (isinstance(value, int) and not isinstance(value, bool) and value >= 0) if attribute == "price_cents" else (
                    bool(isinstance(value,str) and value.strip()) if attribute=='input_voltage' else
                    isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0)
                if valid:
                    values.append({"name": product["name"], "product_id": product["product_id"],
                        "value": value, "fixture_provenance": product.get("fixture_provenance")})
                else:
                    missing_attribute.append(product["product_id"])
            if missing_attribute:
                comparison.update(status="MISSING_COMPARISON_ATTRIBUTE", missing_attribute_products=missing_attribute)
                label = "重量" if attribute == "weight_g" else "目录价格"
                phrases.append(f"商品详情中缺少可核验的{label}字段，涉及商品 {', '.join(missing_attribute)}；无法可靠比较。")
            elif scope_incomplete:
                chosen = (min if direction == "MIN" else max)(row["value"] for row in values) if direction != "SIDE_BY_SIDE" else None
                observed = [row for row in values if row["value"] == chosen] if chosen is not None else values
                comparison.update(status="CANDIDATE_SCOPE_INCOMPLETE", observed_products=observed,
                    checked_candidate_count=len(values), matched_candidate_count=search.get("count") if search else None)
                unit = "g" if attribute == "weight_g" else "分"
                if direction == "SIDE_BY_SIDE":
                    rendered = "；".join(f"{row['name']}：{row['value']} {unit}" for row in values)
                    phrases.append(f"本次预算内已读取 {len(values)} 个候选的{('重量' if attribute == 'weight_g' else '目录价格')}：{rendered}。检索范围不完整，不能据此作全候选结论。")
                else:
                    phrases.append(f"本次只在已读取的 {len(values)} 个候选中，{observed[0]['name']}的记录{('重量' if attribute == 'weight_g' else '目录价格')}为 {chosen} {unit}，但候选范围不完整，不能据此判断全部匹配商品。")
            else:
                chosen = (min if direction == "MIN" else max)(row["value"] for row in values) if direction != "SIDE_BY_SIDE" else None
                winners = [row for row in values if row["value"] == chosen] if chosen is not None else []
                comparison.update(status="COMPARED", products=values)
                unit = "g" if attribute == "weight_g" else "分"
                label = "重量" if attribute == "weight_g" else "目录价格"
                if len({row["value"] for row in values}) == 1:
                    comparison["tie"] = True
                    if attribute == "weight_g":
                        comparison["weight_difference_g"] = 0
                    phrases.append(f"这些商品的目录记录{label}相同，均为 {values[0]['value']} {unit}。")
                elif direction == "SIDE_BY_SIDE":
                    rendered = "；".join(f"{row['name']}：{row['value']} {unit}" for row in values)
                    phrases.append(f"按目录记录{label}逐项为：{rendered}；这里仅列出记录值，没有推定最轻或最低价商品。")
                elif len(winners) > 1:
                    comparison["tie"] = True
                    phrases.append(f"按目录记录{label}，目标极值并列：" + "、".join(row["name"] for row in winners) + f"（{chosen} {unit}）。")
                else:
                    comparison["winner"] = winners[0]
                    if attribute == "weight_g":
                        outcome = "更轻" if direction == "MIN" else "更重"
                        if direction == "MIN":
                            comparison["lighter_product"] = {**winners[0], "weight_g": winners[0]["value"]}
                        elif direction == "MAX":
                            comparison["heavier_product"] = {**winners[0], "weight_g": winners[0]["value"]}
                        if attribute == "weight_g" and len(values) == 2:
                            comparison["weight_difference_g"] = abs(values[0]["value"] - values[1]["value"])
                    else:
                        outcome = "更便宜" if direction == "MIN" else "更贵"
                    phrases.append(f"按目录记录{label}，{winners[0]['name']}{outcome}（{chosen} {unit}）。")
    if not phrases:
        return None
    durations = [float(row.get("duration_ms", 0)) for row in supervisor_result.get("tool_execution_status", [])
                 if row.get("executed")]
    answer_kind = "CLARIFICATION_REQUIRED" if comparison and comparison.get("status") != "COMPARED" else "BUSINESS_FACTS"
    business_evidence = []
    comparison_attribute = comparison.get("attribute") if comparison else None
    for result_ref, detail in zip(detail_refs, details):
        product = detail.get("product") if isinstance(detail, dict) else None
        if detail.get("availability") != "AVAILABLE" or not isinstance(product, dict):
            if isinstance(detail, dict) and detail.get("availability") == "NOT_FOUND":
                business_evidence.append({"kind": "catalog_product_lookup", "source": detail.get("source"),
                    "tool": "get_product_detail", "result_ref": result_ref,
                    "resource": {"type": "product", "id": detail.get("product_id")},
                    "availability": "NOT_FOUND"})
            continue
        facts = []
        recorded = product.get("recorded_attributes") if isinstance(product.get("recorded_attributes"), dict) else {}
        fields = [comparison_attribute] if comparison_attribute else ["weight_g", "price_cents"]
        for field_name in fields:
            value = product.get("price_cents") if field_name == "price_cents" else recorded.get(field_name)
            if (isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)) or (field_name=='input_voltage' and isinstance(value,str) and value.strip()):
                facts.append({"field": field_name, "value": value,
                    "unit": "g" if field_name == "weight_g" else "minor_currency_unit" if field_name == "price_cents" else None})
        business_evidence.append({"kind": "catalog_product_fact", "source": detail.get("source"),
            "tool": "get_product_detail", "result_ref": result_ref,
            "resource": {"type": "product", "id": product.get("product_id"), "name": product.get("name")},
            "facts": facts, "provenance_ref": product.get("fixture_provenance")})
    if search and "search_products" in results:
        search_ref = next((item["result_ref"] for item in references
            if item.get("tool") == "search_products"), None)
        if search_ref:
            business_evidence.append(_catalog_search_evidence(search, search_ref))
    if comparison_requested:
        attribute = comparison.get("attribute") if comparison else None
        label = "重量" if attribute == "weight_g" else "目录价格" if attribute == "price_cents" else '输入电压' if attribute=='input_voltage' else "请求的比较属性"
        unit = " g" if attribute == "weight_g" else " 分" if attribute == "price_cents" else ""
        fact_rows = []
        for item in business_evidence:
            if item.get("kind") != "catalog_product_fact":
                continue
            fact = next((row for row in item.get("facts", []) if row.get("field") == attribute), None)
            if fact is not None:
                fact_rows.append((item, fact))
        if fact_rows:
            rendered = "；".join(
                f"{item['resource']['name']}（{item['resource']['id']}）：{fact['value']}{unit}"
                for item, fact in fact_rows)
            refs_text = "、".join(item["result_ref"] for item, _ in fact_rows)
            evidence_sentence = (
                f"依据商品目录的 get_product_detail 记录，{rendered}；工具结果引用：{refs_text}。"
            )
            difference = comparison.get("weight_difference_g") if comparison else None
            if attribute == "weight_g" and isinstance(difference, (int, float)):
                evidence_sentence += f"两条重量记录相差 {difference} g。"
            if comparison and comparison.get("status") == "CANDIDATE_SCOPE_INCOMPLETE":
                evidence_sentence += "本次只说明已读取的候选，检索范围不完整，不能据此作全目录结论。"
            phrases.append(evidence_sentence)
        elif business_evidence:
            phrases.append("商品目录工具记录未提供可用于本次比较的详情属性；无法用其他属性替代该结论。")
    if comparison is not None:
        results["product_comparison"] = comparison
    return {"answer": "".join(phrases), "answer_kind": answer_kind,
            "business_results": results, "tool_references": references,
            "business_evidence": business_evidence,
            "tool_execution_status": supervisor_result.get("tool_execution_status", []),
            "latency_ms": round(sum(durations), 3)}
