"""Bounded, evidence-first multi-hop policy retrieval for M2.3."""
from __future__ import annotations

import re
import json
import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from app.m0.config import load_settings
from app.m2.retrieval import RetrievalUnavailable, RetrievalView


class Condition(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=500)
    kind: Literal["USER_PROVIDED_FACT", "POLICY_RULE", "BUSINESS_TOOL_FACT", "MISSING_INFORMATION", "POLICY_ASPECT", "UNKNOWN"]
    required: bool
    source_excerpt: str | None = Field(default=None, max_length=300)
    source_evidence_id: str | None = Field(default=None, max_length=200)
    source_type: Literal["USER_PROVIDED", "POLICY", "BUSINESS_TOOL", "UNKNOWN"] | None = None
    rule_role: Literal["CORE_RULE", "NEGATIVE_BOUNDARY", "SUPPLEMENTAL_EXPLANATION"] = "CORE_RULE"
    depends_on: list[str] = Field(default_factory=list, max_length=12)
    policy_stages: list[Literal["RETURN_SUBMISSION", "RETURN_REVIEW", "RETURN_APPROVAL",
                                "RETURN_COMPLETION", "REFUND_APPLICATION", "REFUND_APPROVAL", "REFUND_SETTLEMENT"]] = Field(
                                    default_factory=list, max_length=6)
    negative_policy_stages: list[Literal["RETURN_SUBMISSION", "RETURN_REVIEW", "RETURN_APPROVAL",
                                         "RETURN_COMPLETION", "REFUND_APPLICATION", "REFUND_APPROVAL", "REFUND_SETTLEMENT"]] = Field(
                                             default_factory=list, max_length=6)

    @model_validator(mode="after")
    def source_matches_kind(self):
        expected = {"USER_PROVIDED_FACT": "USER_PROVIDED", "POLICY_RULE": "POLICY",
                    "BUSINESS_TOOL_FACT": "BUSINESS_TOOL", "MISSING_INFORMATION": "UNKNOWN",
                    "POLICY_ASPECT": "UNKNOWN", "UNKNOWN": "UNKNOWN"}
        expected_source = expected.get(self.kind)
        if self.source_type is None and expected_source:
            self.source_type = expected_source
        if expected_source and self.source_type != expected_source:
            raise ValueError("condition source_type conflicts with kind")
        if self.kind == "UNKNOWN" and self.source_type != "UNKNOWN":
            raise ValueError("unknown condition must use UNKNOWN source_type")
        return self


_STAGE_PATTERNS = {
    "RETURN_SUBMISSION": (r"提交.{0,8}退货申请", r"申请退货"),
    # A generic human review/transfer is not necessarily a return review. Keep
    # the lifecycle label anchored to an explicit return/quality context.
    "RETURN_REVIEW": (r"退货申请.{0,10}人工审核", r"人工审核.{0,10}退货申请",
                      r"退货.{0,8}人工审核", r"质量问题.{0,8}人工审核",
                      r"退货申请.{0,6}审核", r"审核.{0,6}退货申请", r"退货审核", r"人工审核"),
    "RETURN_APPROVAL": (r"退货申请.{0,6}(?:获批|批准|通过)", r"(?:获批|批准|通过).{0,6}退货申请",
                        r"退货.{0,3}(?:获批|批准)"),
    "RETURN_COMPLETION": (r"退货.{0,4}(?:完成|完毕)", r"(?:完成|完毕).{0,4}退货", r"退货流程.{0,4}完成",
                          r"(?:商品|货物).{0,4}寄回"),
    "REFUND_APPLICATION": (r"申请.{0,4}退款", r"退款申请", r"(?i:\brefund\s+applications?\b)"),
    "REFUND_APPROVAL": (r"退款(?:申请)?(?:已经|已)(?:获批|批准|通过)", r"(?:已批准|已获批).{0,3}退款"),
    "REFUND_SETTLEMENT": (r"(?:退款|款项).{0,4}到账", r"到账"),
}
_PHASE_ORDER = {"RETURN_SUBMISSION": 10, "RETURN_REVIEW": 20, "RETURN_APPROVAL": 30,
                "RETURN_COMPLETION": 40, "REFUND_APPLICATION": 50, "REFUND_APPROVAL":55,"REFUND_SETTLEMENT": 60}


def _policy_stages(text: str) -> set[str]:
    value = text or ""
    stages = set()
    for stage, patterns in _STAGE_PATTERNS.items():
        matches = [pattern for pattern in patterns if re.search(pattern, value)]
        if not matches:
            continue
        if stage == "RETURN_REVIEW" and matches == [r"人工审核"]:
            # A bare review mention is only a return lifecycle stage when the
            # same sentence connects it to a return/quality application.
            clauses = re.split(r"[。！？!?；;]", value)
            if not any("人工审核" in clause and re.search(r"退货|质量问题", clause)
                       for clause in clauses):
                continue
        stages.add(stage)
    # “不保证批准” only identifies the return-approval phase when the same
    # statement actually concerns a return; generic approval disclaimers do not.
    if ("RETURN_APPROVAL" not in stages and re.search(r"(?:不保证|不承诺).{0,5}(?:获批|批准|通过)", value)
            and re.search(r"退货", value)):
        stages.add("RETURN_APPROVAL")
    return stages


def _evidence_policy_stages(row: dict) -> set[str]:
    title = row.get("title", "") or ""
    content = row.get("content", "") or ""
    stages = _policy_stages(_evidence_stage_text(row))
    # Some policy chunks omit the subject from the clause text while the
    # versioned policy title supplies it (e.g. a return policy's review rule).
    if re.search(r"退货", title) and re.search(r"人工审核", content):
        stages.add("RETURN_REVIEW")
    if (re.search(r"退货", title) and
            re.search(r"(?:不保证|不承诺).{0,5}(?:获批|批准|通过)", content)):
        stages.add("RETURN_APPROVAL")
    return stages


def _evidence_stage_text(row: dict) -> str:
    """Resolve an elided application subject only from this retrieved record's topic."""
    title = row.get("title", "") or ""
    content = row.get("content", "") or ""
    topic = " ".join(str(row.get(key) or "") for key in ("title", "policy_id", "chunk_id"))
    if re.search(r"\brefund\b|退款", topic, re.I):
        content = re.sub(r"(?<!\w)(?<!refund )applications?\s+(?=may\s+be\s+reviewed\b)",
                         "refund application ", content, flags=re.I)
    return title + "。" + content


def _asserted_policy_stages(text: str) -> set[str]:
    """Lifecycle stages positively asserted by a clause, excluding negated caveats."""
    stages = set()
    for clause in re.split(r"(?<=[。！？!?；;])", text or ""):
        if re.search(r"不代表|不等于|并非", clause):
            continue
        for stage in _STAGE_PATTERNS:
            polarities = _stage_polarities(clause, stage)
            if False in polarities or not polarities:
                if any(re.search(pattern, clause) for pattern in _STAGE_PATTERNS[stage]):
                    stages.add(stage)
    return stages


def _negative_policy_stages(text: str) -> set[str]:
    return {stage for stage in _STAGE_PATTERNS if True in _stage_polarities(text, stage)}


def _asks_boundary(question: str, aspect_text: str, condition: Condition) -> bool:
    text = question + " " + aspect_text
    negative_stages = set(condition.negative_policy_stages) or _negative_policy_stages(condition.text)
    if "REFUND_SETTLEMENT" in negative_stages:
        return bool(re.search(r"到账|收到款|款项|结算", text))
    if "RETURN_APPROVAL" in negative_stages:
        return bool(re.search(r"是否.{0,5}(?:获批|批准|通过)|能否.{0,5}(?:获批|批准|通过)|"
                              r"审核.{0,6}(?:通过|批准)|保证.{0,5}(?:通过|批准)|等于.{0,5}(?:获批|批准)", text))
    return False


def _atomic_policy_conditions(evidence: dict[str, dict]) -> list[Condition]:
    """Create candidate policy rules only from exact, applicable retrieved policy excerpts."""
    rows: list[Condition] = []
    seen: set[tuple[str, str]] = set()
    for evidence_id, row in sorted(evidence.items()):
        content = row.get("content") or ""
        # Policy chunks are source clauses; split compound prose at sentence/semicolon
        # boundaries so a negative settlement caveat cannot contaminate a core rule.
        atoms = re.split(r"(?<=[。！？!?；;])|\r?\n", content)
        for atom in atoms:
            excerpt = atom.strip(" \t\r\n。！？!?；;")
            if not excerpt or (evidence_id, excerpt) in seen:
                continue
            seen.add((evidence_id, excerpt))
            role = ("NEGATIVE_BOUNDARY" if re.search(r"不代表|不等于|并非", excerpt)
                    else "SUPPLEMENTAL_EXPLANATION" if re.search(r"(?:说明|例如|补充)$", excerpt)
                    else "CORE_RULE")
            identity = hashlib.sha256((evidence_id + "\0" + excerpt).encode("utf-8")).hexdigest()[:16]
            rows.append(Condition(
                condition_id=f"policy-{identity}", text=excerpt, kind="POLICY_RULE", required=False,
                source_excerpt=excerpt, source_evidence_id=evidence_id, source_type="POLICY", rule_role=role,
                policy_stages=sorted((_policy_stages(excerpt) if role == "NEGATIVE_BOUNDARY"
                                      else _asserted_policy_stages(excerpt)),
                                     key=lambda stage: _PHASE_ORDER[stage]),
                negative_policy_stages=sorted(_negative_policy_stages(excerpt), key=lambda stage: _PHASE_ORDER[stage])))
    return rows


def _aspect_placeholders(aspects: list["RequestedAspect"]) -> list[Condition]:
    return [Condition(condition_id=_aspect_condition_id(aspect.aspect_id), text=aspect.text,
                      kind="POLICY_ASPECT", source_type="UNKNOWN", required=True)
            for aspect in aspects]


def _aspect_condition_id(aspect_id: str) -> str:
    return "aspect-" + hashlib.sha256(aspect_id.encode("utf-8")).hexdigest()[:12]


_UNREQUESTED_POLICY_QUALIFIERS = (
    ("UNREQUESTED_SHIPMENT_BRANCH", ("已付款且未发货", "已发货未签收", "发货未签收"),
     ("未发货", "已发货未签收", "发货未签收", "物流", "签收前", "签收后")),
    ("UNREQUESTED_RETURN_EXCEPTION", ("定制商品", "食品", "无理由退货", "二次销售"),
     ("定制商品", "定制", "食品", "无理由", "二次销售")),
    ("UNREQUESTED_EXPIRED_WARRANTY", ("超过30天", "转人工核对保修", "保修"),
     ("超过30天", "30天后", "逾期", "保修", "质保")),
    ("UNREQUESTED_REFUND_CONCURRENCY", ("一个开放的整单退款申请", "仅允许一个开放"),
     ("重复申请", "同时申请", "多个退款", "开放退款", "未完成的退款申请")),
    ("UNREQUESTED_CANCEL_FLOW", ("订单取消", "取消订单", "申请取消", "请求取消", "取消前", "取消时须", "不得取消", "不能取消"),
     ("取消", "撤销订单", "取消订单")),
    ("UNREQUESTED_RETURN_TIME_WINDOW", ("7×24小时", "7*24小时", "24小时内"),
     ("7天", "七天", "24小时", "无理由", "普通商品")),
    ("UNREQUESTED_PARTIAL_REFUND", ("部分退款", "部分退"),
     ("部分退款", "部分退", "退一部分")),
    ("UNREQUESTED_REFUND_SETTLEMENT", ("真实到账", "款项到账", "退款到账"),
     ("到账", "收到款", "结算", "款项")),
)


def _broad_policy_scope(text: str) -> bool:
    return bool(re.search(r"全部|所有|完整|全面|总体|有哪些|所有条件|整个.{0,4}政策|政策.{0,4}全貌", text or ""))


def _unrequested_qualifier_codes(rule_text: str, question: str, aspect_text: str = "",
                                 source_evidence_id: str | None = None) -> list[str]:
    """Return specific policy facets present in a rule but absent from a narrow request."""
    query = f"{question} {aspect_text}"
    if _broad_policy_scope(query):
        return []
    codes = [code for code, rule_markers, query_markers in _UNREQUESTED_POLICY_QUALIFIERS
             if any(marker in rule_text for marker in rule_markers)
             and not any(marker in query for marker in query_markers)]
    # Some compact policy atoms inherit their topic only from the source clause ID.
    # Treat that as source metadata so an adjacent no-reason clause is not generalized
    # to a quality-return request merely because both concern returns.
    if (source_evidence_id and "#no-reason" in source_evidence_id
            and not any(term in query for term in ("无理由", "不影响二次销售", "七天", "7天", "24小时"))):
        codes.append("UNREQUESTED_NO_REASON_RETURN_RULE")
    return codes


def _apply_aspect_rule_map(conditions: list[Condition], aspects: list["RequestedAspect"],
                           grade: "GradePayload", question: str) -> list[dict]:
    """Separate necessary policy rules from merely relevant/supporting clauses.

    Grade output can suggest necessity, but the executor rejects ungrounded anchors,
    validates explicit dependency references and demotes clearly unasked policy facets
    to supporting evidence. Supporting rules never raise the sufficiency threshold.
    """
    expected = {aspect.aspect_id for aspect in aspects}
    mappings = grade.aspect_rule_map
    actual = [mapping.aspect_id for mapping in mappings]
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError("grade aspect_rule_map must classify every requested aspect exactly once")
    by_id = {item.condition_id: item for item in conditions}
    required_rules: set[str] = set()
    empty_aspects: set[str] = set()
    decisions: list[dict] = []
    assignments_by_id: dict[str, list[tuple[str, RequiredRuleAssignment]]] = {}
    aspect_by_id = {aspect.aspect_id: aspect for aspect in aspects}
    for mapping in mappings:
        aspect = aspect_by_id[mapping.aspect_id]
        if ((mapping.required_rules or mapping.supporting_rule_ids) and
                (not aspect.query_anchor or aspect.query_anchor not in question)):
            raise ValueError("requested aspect query_anchor must be an exact substring of the user question")
        aspect_required: set[str] = set()
        aspect_supporting: set[str] = set(mapping.supporting_rule_ids)
        aspect_assignment_ids = [item.rule_id for item in mapping.required_rules]
        if len(aspect_assignment_ids) != len(set(aspect_assignment_ids)):
            raise ValueError("an aspect cannot list the same required rule more than once")
        if set(aspect_assignment_ids) & aspect_supporting:
            raise ValueError("a rule cannot be required and supporting for the same requested aspect")
        for assignment in mapping.required_rules:
            condition_id = assignment.rule_id
            candidate = by_id.get(condition_id)
            if candidate is None or candidate.kind != "POLICY_RULE":
                raise ValueError("grade mapped a requested aspect to a non-evidence policy rule")
            if candidate.rule_role == "SUPPLEMENTAL_EXPLANATION":
                raise ValueError("grade mapped an aspect to a supplemental explanation")
            if assignment.query_anchor != aspect.query_anchor:
                raise ValueError("required rule query_anchor must match its grounded requested aspect")
            if candidate.rule_role == "NEGATIVE_BOUNDARY" and not _asks_boundary(question, aspect.text, candidate):
                raise ValueError("grade promoted an unrequested negative policy boundary to a required rule")
            assignments_by_id.setdefault(condition_id, []).append((mapping.aspect_id, assignment))
            blockers = _unrequested_qualifier_codes(candidate.text, question, aspect.text,
                                                     candidate.source_evidence_id)
            if blockers:
                decisions.append({"condition_id": condition_id, "model_basis": assignment.basis,
                    "decision": "SUPPORTING", "reason_codes": blockers,
                    "aspect_id": mapping.aspect_id, "query_anchor": assignment.query_anchor,
                    "basis": "specific policy facet is outside the narrow user request"})
            else:
                required_rules.add(condition_id)
                aspect_required.add(condition_id)
                decisions.append({"condition_id": condition_id, "model_basis": assignment.basis,
                    "decision": "REQUIRED", "reason_codes": [], "aspect_id": mapping.aspect_id,
                    "query_anchor": assignment.query_anchor,
                    "basis": assignment.rationale})
        for condition_id in mapping.supporting_rule_ids:
            candidate = by_id.get(condition_id)
            if candidate is None or candidate.kind != "POLICY_RULE":
                raise ValueError("grade mapped a supporting clause to a non-evidence policy rule")
            decisions.append({"condition_id": condition_id, "model_basis": "SUPPORTING",
                "decision": "SUPPORTING", "reason_codes": ["EXPLICIT_SUPPORTING"],
                "aspect_id": mapping.aspect_id, "query_anchor": aspect.query_anchor,
                "basis": "Grade identified relevant context that is not needed to answer this aspect"})
        if not aspect_required:
            empty_aspects.add(mapping.aspect_id)
    for rule_id, aspect_assignments in assignments_by_id.items():
        dependent = by_id[rule_id]
        dependencies = set(dependent.depends_on)
        for aspect_id, assignment in aspect_assignments:
            if assignment.basis == "POLICY_DEPENDENCY":
                if rule_id not in required_rules:
                    decisions.append({"condition_id": rule_id, "model_basis": "POLICY_DEPENDENCY",
                        "decision": "SUPPORTING", "reason_codes": [
                            "DEPENDENCY_IGNORED_FOR_SUPPORTING_RULE"],
                        "aspect_id": aspect_id, "query_anchor": assignment.query_anchor,
                        "basis": "the scoped rule is not required, so its proposed dependency cannot raise the answer gate"})
                    continue
                dependency = assignment.depends_on_rule_id
                if not dependency:
                    # If the same rule was independently grounded as directly asked
                    # for another aspect, normalize this redundant role label only.
                    # The rule remains required; no missing dependency is inferred.
                    independently_direct = any(
                        other_aspect != aspect_id and other.basis == "DIRECTLY_ASKED"
                        for other_aspect, other in aspect_assignments)
                    if independently_direct:
                        decisions.append({"condition_id": rule_id, "model_basis": "POLICY_DEPENDENCY",
                            "decision": "REQUIRED", "reason_codes": [
                                "REDUNDANT_DEPENDENCY_CLASSIFICATION_NORMALIZED"],
                            "aspect_id": aspect_id,
                            "query_anchor": assignment.query_anchor,
                            "basis": "rule remains directly required in another grounded aspect; this missing edge was not inferred"})
                        continue
                    raise ValueError("policy dependency must identify a prerequisite rule")
                if (dependency not in assignments_by_id or dependency not in required_rules
                        or dependency == assignment.rule_id):
                    raise ValueError("policy dependency must reference a distinct required rule in this grade")
                prerequisite = by_id[dependency]
                if not dependent.policy_stages or not prerequisite.policy_stages:
                    raise ValueError("policy dependency requires explicit lifecycle stages on both rules")
                if max(_PHASE_ORDER[item] for item in prerequisite.policy_stages) > min(
                        _PHASE_ORDER[item] for item in dependent.policy_stages):
                    raise ValueError("policy dependency contradicts policy lifecycle order")
                dependencies.add(dependency)
            elif assignment.depends_on_rule_id is not None:
                raise ValueError("directly asked rule must not declare a dependency")
        dependent.depends_on = sorted(dependencies)
    for condition in conditions:
        if condition.kind == "POLICY_RULE":
            condition.required = condition.condition_id in required_rules
        elif condition.kind == "POLICY_ASPECT":
            aspect_id = next((aspect.aspect_id for aspect in aspects
                              if condition.condition_id == _aspect_condition_id(aspect.aspect_id)), None)
            condition.required = aspect_id in empty_aspects
    classified = {item["condition_id"] for item in decisions}
    for condition in conditions:
        if condition.kind == "POLICY_RULE" and condition.condition_id not in classified:
            decisions.append({"condition_id": condition.condition_id, "model_basis": "UNMAPPED",
                "decision": "SUPPORTING", "reason_codes": ["NOT_MAPPED_AS_REQUIRED"],
                "query_anchor": None,
                "basis": "candidate rule was not identified as necessary for any requested aspect"})
    return decisions


def _required_answer_evidence(required_conditions: list[Condition], evidence: list[dict],
                              coverage: dict) -> tuple[list[dict], list[dict]]:
    """Build a minimal answer context from evidence that actually supports required rules."""
    by_id = {row.get("chunk_id"): row for row in evidence}
    excerpts_by_id: dict[str, list[str]] = {}
    audit = []
    for condition in required_conditions:
        item_coverage = coverage.get(condition.condition_id)
        accepted_ids = set(item_coverage.supports if item_coverage else [])
        source_id = condition.source_evidence_id
        if source_id not in accepted_ids:
            audit.append({"condition_id": condition.condition_id, "status": "MISSING_SUPPORTING_EVIDENCE",
                          "source_evidence_id": source_id})
            continue
        row = by_id.get(source_id)
        excerpt = condition.source_excerpt
        if not row or not excerpt or excerpt not in (row.get("content") or ""):
            audit.append({"condition_id": condition.condition_id, "status": "SOURCE_EXCERPT_NOT_IN_CURRENT_EVIDENCE",
                          "source_evidence_id": source_id})
            continue
        excerpts_by_id.setdefault(source_id, [])
        if excerpt not in excerpts_by_id[source_id]:
            excerpts_by_id[source_id].append(excerpt)
        audit.append({"condition_id": condition.condition_id, "status": "REQUIRED_EXCERPT_INCLUDED",
                      "source_evidence_id": source_id})
    scoped = []
    for evidence_id, excerpts in sorted(excerpts_by_id.items()):
        row = by_id[evidence_id]
        scoped.append({key: row.get(key) for key in
                       ("chunk_id", "title", "version", "effective_from", "effective_to")}
                      | {"content": "；".join(excerpts)})
    return scoped, audit


def _answer_context_notes(question: str, conditions: list[Condition]) -> list[str]:
    notes = []
    facts = [item for item in conditions if item.kind == "USER_PROVIDED_FACT"]
    for fact in facts:
        notes.append(f"用户陈述（未由业务工具核验）：{fact.source_excerpt or fact.text}")
    has_numeric_duration = bool(re.search(r"\d+\s*(?:天|日|小时|周|个月|月)", question or ""))
    for condition in conditions:
        if (condition.kind == "POLICY_RULE" and condition.required
                and re.search(r"\d+\s*(?:天|日|小时|周|个月|月)", condition.text)
                and re.search(r"签收后|发货后|付款后|收货后", condition.text)
                and not has_numeric_duration
                and re.search(r"签收|瑕疵|质量问题|故障|订单|商品", question or "")):
            notes.append("政策规定了从具体业务事件起算的时间窗口，但问题未提供事件间隔；只能解释规则，不得判断个案满足该期限。")
            break
    if any(condition.kind == "POLICY_RULE" and condition.required and
           {"RETURN_SUBMISSION", "RETURN_REVIEW"}.issubset(set(condition.policy_stages))
           for condition in conditions):
        notes.append("政策规定退货申请需人工审核；说明审核结果尚未确定，不得把提交申请说成已获批或承诺必然获批。")
    return notes


def _required_answer_issues(answer: str, question: str, required_conditions: list[Condition],
                            context_notes: list[str]) -> list[dict]:
    issues = []
    asserted = _asserted_policy_stages(answer)
    for condition in required_conditions:
        expected_stages = set(condition.policy_stages)
        if condition.rule_role == "NEGATIVE_BOUNDARY":
            missing_stages = sorted((set(condition.negative_policy_stages) or expected_stages) -
                                    {stage for stage in expected_stages if True in _stage_polarities(answer, stage)},
                                    key=lambda stage: _PHASE_ORDER[stage])
        else:
            missing_stages = sorted(expected_stages - asserted, key=lambda stage: _PHASE_ORDER[stage])
        if missing_stages:
            issues.append({"code": "ANSWER_REQUIRED_POLICY_STAGE_OMITTED",
                           "condition_id": condition.condition_id, "missing_stages": missing_stages,
                           "source_evidence_id": condition.source_evidence_id})
        for amount, unit in re.findall(r"(\d+)\s*(天|日|小时|周|个月|月)", condition.text):
            if not re.search(rf"{re.escape(amount)}\s*{re.escape(unit)}", answer or ""):
                issues.append({"code": "ANSWER_REQUIRED_POLICY_WINDOW_OMITTED",
                               "condition_id": condition.condition_id, "window": f"{amount}{unit}"})
        if {"RETURN_SUBMISSION", "RETURN_REVIEW"}.issubset(expected_stages):
            outcome_uncertain = bool(re.search(r"(?:是否获批|是否批准|审批结果).{0,16}(?:需|待|仍要|尚需|尚待).{0,10}(?:人工)?审核|"
                                               r"(?:人工审核|审核).{0,14}(?:结果|是否获批).{0,12}(?:尚未确定|未确定|仍需确认)",
                                               answer or ""))
            outcome_uncertain = outcome_uncertain or bool(re.search(r"不保证.{0,8}(?:获批|批准)", answer or ""))
            if not outcome_uncertain:
                issues.append({"code": "ANSWER_RETURN_REVIEW_OUTCOME_UNCLEAR",
                               "condition_id": condition.condition_id})
    needs_window_caveat = any("事件间隔" in note for note in context_notes)
    if needs_window_caveat:
        has_caveat = bool(re.search(r"(?:未提供|没有提供|未说明|没有说明).{0,16}(?:时长|间隔|多久)|"
                                    r"(?:不能|无法|尚不能|不足以).{0,12}(?:判断|确定|确认).{0,18}(?:期限|时限|个案|订单|符合)|"
                                    r"不能据此(?:判断|确定|确认)", answer or ""))
        if not has_caveat:
            issues.append({"code": "ANSWER_MISSING_UNVERIFIED_TIME_WINDOW_CAVEAT"})
        if re.search(r"(?:该订单|您的订单|此订单|该商品).{0,12}(?:符合|满足|已在).{0,8}(?:期限|30天|窗口)|"
                     r"(?:确定|一定|肯定).{0,8}(?:可以|能够).{0,6}(?:退货|退款)", answer or ""):
            issues.append({"code": "ANSWER_UNSUPPORTED_SPECIFIC_ELIGIBILITY_CLAIM"})
    for code in _unrequested_qualifier_codes(answer or "", question):
        issues.append({"code": "ANSWER_UNREQUESTED_POLICY_DETAIL", "facet": code})
    return issues


def _canonicalize_grade_missing_aspect_ids(grade: "GradePayload", conditions: list[Condition],
                                           aspects: list["RequestedAspect"]) -> list[dict]:
    """Map only exact supplied aspect IDs to their server-created placeholder condition IDs.

    This repairs a known model aliasing error without treating an unresolved aspect as covered.
    Unknown IDs remain unchanged and are rejected by the strict evidence-coverage validator.
    """
    condition_ids = {item.condition_id for item in conditions}
    aliases = {aspect.aspect_id: _aspect_condition_id(aspect.aspect_id)
               for aspect in aspects if _aspect_condition_id(aspect.aspect_id) in condition_ids}
    normalizations = []
    canonical = []
    for value in grade.grade.missing_aspect:
        replacement = value if value in condition_ids else aliases.get(value, value)
        if replacement != value:
            normalizations.append({"field": "grade.missing_aspect", "from": value, "to": replacement,
                "basis": "exact requested_aspect_id mapped to its required server-created POLICY_ASPECT placeholder"})
        canonical.append(replacement)
    grade.grade.missing_aspect = canonical
    return normalizations


def _remove_policy_aspect_judgments(grade: "GradePayload", conditions: list[Condition]) -> list[dict]:
    """Drop redundant LLM judgments on server-created aspect placeholders.

    A placeholder is not policy evidence. Ignoring this duplicate claim cannot satisfy
    coverage; the mapped required POLICY_RULEs still need their own evidence judgments.
    Judgments on user/tool facts remain for the strict coverage validator to reject.
    """
    by_id = {condition.condition_id: condition for condition in conditions}
    kept = []
    audit = []
    for judgment in grade.judgments:
        target = by_id.get(judgment.condition_id)
        if target is not None and target.kind == "POLICY_ASPECT":
            audit.append({"condition_id": judgment.condition_id,
                "evidence_id": judgment.evidence_id,
                "decision": "IGNORED_NON_RULE_JUDGMENT_NO_COVERAGE_EFFECT",
                "reason": "POLICY_ASPECT is a routing placeholder, not a policy evidence condition"})
        else:
            kept.append(judgment)
    grade.judgments = kept
    return audit


def _stage_polarity_assertions(text: str, stage: str, *, scope_commas: bool = False) -> list[tuple[bool, str]]:
    """Return negation polarity and its local clause for lifecycle mentions."""
    found: list[tuple[bool, str]] = []
    value=text or ''
    if scope_commas:
        # Contrast terminates a negation's reach even without punctuation.
        # “不但” is additive, not a denial/contrast boundary.
        value=re.sub(r'(?<!不)(?:但是|但|然而|不过)', '；',value)
    marks = "。！？!?；;，,：:\n\r" if scope_commas else "。！？!?；;"
    for pattern in _STAGE_PATTERNS[stage]:
        for match in re.finditer(pattern, value):
            punctuation = max((value.rfind(mark, 0, match.start())
                               for mark in marks), default=-1)
            clause_start = punctuation + 1
            clause_end = min([pos for mark in marks
                               if (pos := value.find(mark, match.end())) >= 0] or [len(value)])
            clause = value[clause_start:clause_end]
            prefix = value[max(clause_start, match.start() - 40):match.start()]
            matched = value[match.start():match.end()]
            # A relation pattern that crosses a comma/line boundary combines
            # separate propositions and cannot safely carry polarity.
            if scope_commas and re.search(r"[，,：:\n\r]", matched):
                continue
            # Questions/uncertainty about a lifecycle stage are not positive or
            # negative policy assertions (e.g. “whether funds actually arrived”).
            if re.search(r"是否|能否|可否|能不能|无法确认|无法判断|尚不清楚|以.{0,8}为准", clause):
                continue
            # Negation must govern the lifecycle phrase itself. A nearby `未`
            # can negate an attribute such as “未规定审核时限”, not the review
            # or approval stage; a broad look-behind caused false conflicts in
            # answers that correctly distinguished approval from completion.
            if scope_commas:
                prefix_tail = prefix[-12:]
                directly_negated = bool(re.search(
                    r"(?:不代表|不等于|并非|不能|不可|不应|不必|无需)(?:已有|已|直接|申请|提交|进行|经过|完成|获批|批准|通过)?$|"
                    r"(?:尚未|未)(?:进行|经过|完成|提交|获批|批准|通过|申请)?$",
                    prefix_tail))
                directly_negated = directly_negated or bool(re.search(
                    r"(?i:\b(?:no|not|without)\s*)$", prefix))
                denied_after = bool(re.match(r"(?i:\s+(?:cannot|must\s+not|may\s+not|are\s+not\s+allowed)\b)",
                    value[match.end():clause_end]))
                negated_inside = bool(re.search(
                    r"(?:不代表|不等于|并非|不能|不可|不应|不必|尚未|未).{0,4}"
                    r"(?:获批|批准|通过|完成|提交|申请|到账)", matched))
                polarity = directly_negated or negated_inside or denied_after
            else:
                # Retain the historical condition-audit polarity contract;
                # the answer-specific scoping change must not alter Grade data.
                polarity = bool(re.search(r"不代表|不等于|并非|不能|不可|不应|尚未|未|不", prefix) or
                                re.match(r"不保证|不承诺|不代表|不等于|并非", matched))
            found.append((polarity, clause))
    return found


def _stage_polarities(text: str, stage: str, *, scope_commas: bool = False) -> set[bool]:
    return {polarity for polarity, _ in _stage_polarity_assertions(text, stage, scope_commas=scope_commas)}


def _scoped_expired_return_boundary(answer: str, cited_evidence: list[dict]) -> bool:
    """Recognize a negative return-window statement scoped to a sourced >N boundary."""
    for amount, unit in re.findall(r"超过\s*(\d+)\s*(天|日|小时|周|个月|月)", answer or ""):
        answer_clause = next((clause for clause in re.split(r"[。！？!?；;]", answer or "")
                              if re.search(rf"超过\s*{re.escape(amount)}\s*{re.escape(unit)}", clause)), "")
        denies_submission = bool(re.search(r"(?:不能|不可|不应|无法|不再).{0,24}(?:提交|申请).{0,8}退货|"
                                           r"(?:转人工|人工核对).{0,12}(?:保修|核实)", answer_clause))
        asserts_submission = bool(re.search(r"(?:仍可|仍能|可以|可).{0,12}(?:提交|申请).{0,8}退货", answer_clause))
        if not denies_submission or asserts_submission:
            continue
        for row in cited_evidence:
            source = (row.get("content") or "")
            if (re.search(rf"超过\s*{re.escape(amount)}\s*{re.escape(unit)}", source) and
                    re.search(r"人工核对|转人工|保修", source)):
                return True
    return False


def _condition_semantic_issues(question: str, request_mode: str, conditions: list[Condition],
                               evidence: dict[str, dict] | None = None) -> list[dict]:
    """Conservative checks for duplicated context, invented workflow phases, and unsupported anchors."""
    issues: list[dict] = []
    seen_exact: dict[str, str] = {}
    by_id = {item.condition_id: item for item in conditions}
    for item in conditions:
        canonical = re.sub(r"[\W_]+", "", item.text).casefold()
        if canonical in seen_exact and item.required and by_id[seen_exact[canonical]].required:
            issues.append({"code": "DUPLICATE_REQUIRED_CONDITION", "condition_id": item.condition_id,
                           "related_condition_id": seen_exact[canonical],
                           "basis": "required conditions have identical normalized text"})
        else:
            seen_exact[canonical] = item.condition_id

        declared = set(item.policy_stages)
        declared_negative = set(item.negative_policy_stages)
        inferred = _asserted_policy_stages(item.text)
        inferred_negative = _negative_policy_stages(item.text)
        if ((declared or declared_negative or item.source_evidence_id) and
                (not inferred.issubset(declared) or not inferred_negative.issubset(declared_negative))):
            issues.append({"code": "POLICY_STAGE_DECLARATION_MISMATCH", "condition_id": item.condition_id,
                           "basis": "positive and negative lifecycle mentions must be declared separately"})
        stages = declared | inferred
        if item.kind == "POLICY_RULE" and item.source_evidence_id:
            source = (evidence or {}).get(item.source_evidence_id)
            if (source is None or not item.source_excerpt or
                    item.source_excerpt not in (source.get("content") or "") or
                    item.text != item.source_excerpt):
                issues.append({"code": "POLICY_RULE_SOURCE_MISMATCH", "condition_id": item.condition_id,
                               "basis": "atomic policy rule is not an exact excerpt of its cited evidence"})
        if (item.kind == "POLICY_RULE" and item.required and
                item.rule_role == "NEGATIVE_BOUNDARY" and
                not _asks_boundary(question, "", item)):
            issues.append({"code": "UNREQUESTED_NEGATIVE_BOUNDARY_REQUIRED", "condition_id": item.condition_id,
                           "basis": "a negative policy boundary is required only when the user asks about that boundary"})
        if item.kind == "POLICY_RULE" and item.required:
            # A scenario fact may be used as context, but cannot be promoted into a policy
            # requirement merely by restating it as a prerequisite.
            fact_restated = (request_mode == "POLICY_EXPLANATION" and
                ("事实前提" in item.text or "情景前提" in item.text) and
                any(term in question and term in item.text for term in ("已签收", "签收", "瑕疵", "质量问题")))
            has_independent_rule_anchor = bool(
                re.search(r"\d+\s*(?:天|日|小时|周|个月|月)", item.text) or
                re.search(r"提交.{0,8}退货申请|人工审核|退货申请.{0,6}(?:获批|批准|通过|审核)|"
                          r"(?:获批|批准).{0,6}退货申请|已签收订单.{0,12}申请退款", item.text))
            if fact_restated and not has_independent_rule_anchor:
                issues.append({"code": "USER_CONTEXT_PROMOTED_TO_POLICY", "condition_id": item.condition_id,
                               "basis": "condition restates a question-supplied scenario fact without an independent policy rule anchor"})
        for dependency_id in item.depends_on:
            dependency = by_id.get(dependency_id)
            if dependency:
                prior = _asserted_policy_stages(dependency.text) | set(dependency.policy_stages)
                current = stages
                if prior and current and max(_PHASE_ORDER[s] for s in prior) > min(_PHASE_ORDER[s] for s in current):
                    issues.append({"code": "POLICY_DEPENDENCY_ORDER_CONFLICT", "condition_id": item.condition_id,
                                   "related_condition_id": dependency_id,
                                   "basis": "dependency reverses the policy lifecycle order"})

        if item.required and item.kind == "POLICY_RULE" and evidence:
            mentioned_stages = stages | declared_negative | inferred_negative
            for stage in mentioned_stages:
                stage_evidence = [row for row in evidence.values()
                                  if stage in _evidence_policy_stages(row)]
                condition_polarities = _stage_polarities(item.text, stage)
                evidence_polarities = set().union(*(_stage_polarities(
                    (row.get("title", "") or "") + "。" + (row.get("content", "") or ""), stage)
                    for row in stage_evidence)) if stage_evidence else set()
                if not stage_evidence:
                    issues.append({"code": "UNSUPPORTED_POLICY_PHASE", "condition_id": item.condition_id,
                                   "policy_stage": stage,
                                   "basis": "no retrieved applicable policy evidence states this workflow phase"})
                elif condition_polarities and evidence_polarities and not condition_polarities.issubset(evidence_polarities):
                    issues.append({"code": "POLICY_PHASE_POLARITY_MISMATCH", "condition_id": item.condition_id,
                                   "policy_stage": stage,
                                   "basis": "condition asserts the opposite polarity from retrieved policy evidence"})
            for amount, unit in re.findall(r"(\d+)\s*(天|日|小时|周|个月|月)", item.text):
                if not any(re.search(rf"{re.escape(amount)}\s*{re.escape(unit)}", row.get("content", ""))
                           for row in evidence.values()):
                    issues.append({"code": "UNSUPPORTED_POLICY_WINDOW", "condition_id": item.condition_id,
                                   "window": f"{amount}{unit}",
                                   "basis": "no retrieved applicable policy evidence states this time window"})

    if evidence and request_mode == "POLICY_EXPLANATION":
        query_has_return_refund_relation = ("退货" in question and "退款" in question and
            bool(re.search(r"衔接|先后|顺序|如何.{0,4}(?:退货|退款)|条件.{0,4}如何", question)))
        required = [item for item in conditions if item.required and item.kind == "POLICY_RULE"]
        if query_has_return_refund_relation:
            approval_rule_evidence = [row for row in evidence.values()
                if {"RETURN_APPROVAL", "REFUND_APPLICATION"}.issubset(
                    _policy_stages(row.get("content", "")) | _policy_stages(row.get("title", "")))]
            if approval_rule_evidence and not any(
                    {"RETURN_APPROVAL", "REFUND_APPLICATION"}.issubset(
                        _policy_stages(item.text) | set(item.policy_stages)) for item in required):
                issues.append({"code": "REQUIRED_POLICY_RELATION_OMITTED",
                               "policy_stages": ["RETURN_APPROVAL", "REFUND_APPLICATION"],
                               "basis": "question asks the return/refund relationship and the applicable policy makes approval a prerequisite to refund application"})
            quality_query = bool(re.search(r"瑕疵|质量问题|质量退货", question))
            quality_window_evidence = [row for row in evidence.values()
                if re.search(r"\d+\s*(?:天|日)", row.get("content", "")) and
                   re.search(r"提交.{0,8}退货申请|退货申请.{0,6}审核|人工审核", row.get("content", "")) and
                   re.search(r"质量|瑕疵", row.get("content", ""))]
            if quality_query and quality_window_evidence:
                for row in quality_window_evidence:
                    windows = re.findall(r"(\d+)\s*(天|日)", row.get("content", ""))
                    covered = any(
                        any(re.search(rf"{re.escape(amount)}\s*{re.escape(unit)}", item.text)
                            for amount, unit in windows) and
                        bool(_policy_stages(item.text) & {"RETURN_SUBMISSION", "RETURN_REVIEW"})
                        for item in required)
                    if not covered:
                        issues.append({"code": "REQUIRED_POLICY_WINDOW_OMITTED",
                                       "policy_stages": ["RETURN_SUBMISSION", "RETURN_REVIEW"],
                                       "basis": "quality-return question requires the applicable quality-return time window and review rule to be represented"})

    return issues


_POLICY_WINDOW_RE = re.compile(
    r"(?<![A-Za-z0-9_零〇一二两三四五六七八九十百千万亿-])"
    r"(?P<amount>\d{1,4}\s*[×*]\s*\d{1,4}|\d+|[一二两三四五六七八九]百(?:零[一二两三四五六七八九]|[一二两三四五六七八九]?十[一二两三四五六七八九]?)?"
    r"|[一二两三四五六七八九]?十[一二两三四五六七八九]?|[零一二两三四五六七八九])"
    r"\s*[-\u2010-\u2015]?\s*(?P<unit>days?|d|天|日|hours?|h|小时|weeks?|w|周|months?|mos?|mo|个月|月|years?|yrs?|y|年)(?![A-Za-z0-9_])",
    re.IGNORECASE)


def _policy_amount(value: str) -> int:
    """Bounded Chinese integer duration notation, shared by source and answer."""
    if value.isdecimal():
        return int(value)
    factors = re.fullmatch(r'(\d{1,4})\s*[×*]\s*(\d{1,4})', value)
    if factors:
        return int(factors[1]) * int(factors[2])
    digits = {letter: number for number, letter in enumerate("零一二三四五六七八九")}
    digits["两"] = 2
    total, digit = 0, 0
    for letter in value:
        if letter in digits:
            digit = digits[letter]
        else:
            total += (digit or 1) * {"十": 10, "百": 100}[letter]
            digit = 0
    return total + digit
_POLICY_CALENDAR_DATE_RE = re.compile(
    r"[一二三四五六七八九十]{1,3}月[一二三四五六七八九十]{1,3}日|"
    r"(?<!\d)(?:(?:19|20)\d{2}\s*年\s*)?\d{1,2}\s*月\s*\d{1,2}\s*日(?!\d)|"
    r"(?<!\d)(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}(?!\d)|"
    r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+\d{1,2}"
    r"(?:st|nd|rd|th)?(?:,?\s+(?:19|20)\d{2})?\b|"
    r"\b\d{1,2}(?:st|nd|rd|th)?\s+(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|"
    r"jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
    r"(?:,?\s+(?:19|20)\d{2})?\b", re.IGNORECASE)


def _policy_duration_matches(text: str):
    """Find durations but exclude numeric fragments that form calendar dates."""
    value = text or ""
    date_spans = [(match.start(), match.end()) for match in _POLICY_CALENDAR_DATE_RE.finditer(value)]
    return [match for match in _POLICY_WINDOW_RE.finditer(value)
        if not any(match.start() < end and start < match.end() for start, end in date_spans)]
_POLICY_WINDOW_UNITS = {
    "d": "day", "day": "day", "days": "day", "天": "day", "日": "day",
    "h": "hour", "hour": "hour", "hours": "hour", "小时": "hour",
    "w": "week", "week": "week", "weeks": "week", "周": "week",
    "mo": "month", "mos": "month", "month": "month", "months": "month", "月": "month", "个月": "month",
    "y": "year", "yr": "year", "yrs": "year", "year": "year", "years": "year", "年": "year",
}
_POLICY_SCOPE_STOP = {
    "a", "an", "the", "this", "that", "two", "same", "thing", "about", "fictional",
    "policy", "policies", "rule", "rules", "source", "states", "says", "say", "under",
    "expires", "expire", "expired", "expiration", "after", "within", "for", "from",
    "of", "in", "on", "is", "are", "does", "not", "never",
    "将在", "将于", "会在", "会于", "将", "会", "称", "说", "规定", "指出", "规则",
    "period", "window", "期限", "期", "窗口",
}

# A small, general vocabulary for matching the same common policy subjects when
# an answer changes language. This normalizes entities; it does not encode a
# case answer, policy outcome, precedence rule, or fixture value.
_POLICY_SUBJECT_ALIASES = (
    (re.compile(r"\bgift\s+cards?\b|礼品卡|礼物卡", re.I), "giftcard"),
    (re.compile(r"\bsealed\s+(?:items?|products?|goods)\b|密封商品|密封货品", re.I), "sealeditem"),
    (re.compile(r"\bopened\s+(?:items?|products?|goods)\b|已拆封商品|已开封商品", re.I), "openeditem"),
    (re.compile(r"\bfoods?\b|食品", re.I), "food"),
    (re.compile(r"\borders?\b|订单", re.I), "order"),
    (re.compile(r"\breturns?\b|退货", re.I), "return"),
    (re.compile(r"\brefunds?\b|退款", re.I), "refund"),
    (re.compile(r"\bwarrant(?:y|ies)\b|保修|质保", re.I), "warranty"),
    (re.compile(r"\bshipments?\b|物流|配送", re.I), "shipment"),
    (re.compile(r"\bproducts?\b|商品|产品", re.I), "product"),
)


_POLICY_SOURCE_LABEL_RE = re.compile(
    r"\b(?:fictional\s+)?(?:rule|policy|source)\s+(?!says?\b|runs?\b|states?\b|applies?\b|requires?\b)[a-z0-9][a-z0-9_-]*(?=\s|[:：]|$|[\u3400-\u9fff])"
    r"|\b(?:[a-z]|[a-z0-9_-]*[-_0-9][a-z0-9_-]+)\s+(?:rule|policy|source)(?=\s|[:：]|$|[\u3400-\u9fff])"
    r"|\b[a-z0-9][a-z0-9_-]*\s*(?:规则|政策|来源)(?=\s|[:：]|$|[\u3400-\u9fff])"
    r"|(?:虚构)?(?:规则|政策|来源)\s*(?:[A-Za-z0-9_-]+|[一二三四五六七八九十]+)"
    r"(?=\s|[:：]|$|[\u3400-\u9fff])", re.IGNORECASE)
_POLICY_PREDICATE_LEAD_RE = re.compile(
    r"^(?:expires?|expire|does\s+not\s+expire|do\s+not\s+expire|never\s+expire|"
    r"is\s+valid|validity|within\b|after\b|in\s+\d|"
    r"(?:在|于)?\s*\d+\s*(?:days?|d|天|日|hours?|h|小时|weeks?|w|周|months?|mos?|mo|个月|月|years?|yrs?|y|年)|"
    r"过期|到期|失效|不(?:会|再)?过期)", re.IGNORECASE)
_POLICY_ASSERTION_CUE_RE = re.compile(
    r"\b(?:expires?|expire|within|validity|valid\s+for|after)\b|"
    r"过期|到期|失效|有效期|不(?:会|再)?过期", re.IGNORECASE)
_POLICY_TIME_ANCHOR_RE = re.compile(
    r"(?:签收|收货|下单|付款|支付|发货|交付|收到|购买|激活|提交|申请|确认|创建|注册|开通)后")


def _policy_time_anchor_before(text: str) -> str | None:
    anchors = list(_POLICY_TIME_ANCHOR_RE.finditer(text or ""))
    return anchors[-1].group() if anchors else None


def _policy_assertion_clauses(text: str) -> list[str]:
    """Split narrative into local assertions, leaving conditional tails attached."""
    result: list[str] = []
    sentence_re = r"(?<=[。！？!?;；])|(?<=\.)\s+(?=[A-Z])|[\r\n]+"
    for sentence in re.split(sentence_re, text or ""):
        if not sentence.strip():
            continue
        contrasted = re.split(
            r"(?:(?<=[,，。；;\s])(?:而(?!是)|但是|然而|但|不过)(?!(?:它)?(?:仅|只)适用于)(?=\s|[\u3400-\u9fff])|"
            r"\b(?:whereas|while|but)\b)", sentence, flags=re.I)
        for part in contrasted:
            # A colon is a clause boundary unless the right side starts with a
            # predicate whose grammatical subject is immediately on its left.
            colon_atoms: list[str] = []
            cursor = 0
            for match in re.finditer(r"[:：]", part):
                left = part[cursor:match.start()]
                right = part[match.end():]
                lead = re.match(r"\s*([^:：]*)", right)
                right_head = lead.group(1).strip() if lead else ""
                if _POLICY_PREDICATE_LEAD_RE.search(right_head) or re.match(
                        r"(?:自|从|以|不按|按(?:送达|交付)|申请窗口(?:自|从)|签收后|收货后|付款后|支付后|在确认.{0,12}前)", right_head):
                    continue
                if left.strip():
                    colon_atoms.append(left)
                cursor = match.end()
            tail = part[cursor:]
            if tail.strip():
                colon_atoms.append(tail)
            if not colon_atoms:
                colon_atoms = [part]

            for atom in colon_atoms:
                # Attribution labels start a new local scope. This prevents a
                # conflict preface or a preceding source from becoming subject.
                starts = [m.start() for m in _POLICY_SOURCE_LABEL_RE.finditer(atom) if m.start() > 0]
                if starts:
                    chunks: list[str] = []
                    begin = 0
                    for offset in starts:
                        chunks.append(atom[begin:offset])
                        begin = offset
                    chunks.append(atom[begin:])
                else:
                    chunks = [atom]

                for chunk in chunks:
                    comma_parts = re.split(r"([,，])", chunk)
                    current = ""
                    for index in range(0, len(comma_parts), 2):
                        next_part = comma_parts[index]
                        starts_new_fact = bool(
                            current and ((_POLICY_ASSERTION_CUE_RE.search(current) and
                            _POLICY_ASSERTION_CUE_RE.search(next_part)) or
                            (_policy_duration_matches(current) and _policy_duration_matches(next_part) and
                             not re.match(r"\s*(?:和|与|及|and\b|通常(?:不能|不得)再按)",next_part,re.I))) and
                            not re.match(r"\s*(?:if|when|unless|only\s+if|如果|若|除非|仅当)(?:\s|$)", next_part, re.I))
                        if starts_new_fact:
                            result.append(current)
                            current = next_part
                        else:
                            separator = comma_parts[index - 1] if index else ""
                            current += separator + next_part
                    if current.strip():
                        result.append(current)
    # A condition-only continuation qualifies the immediately preceding rule;
    # it does not introduce a second object or duration.
    joined: list[str] = []
    for item in result:
        if joined and (re.match(r"\s*(?:且)?(?:须|需|仅限|仅适用于)", item) and not _policy_duration_matches(item)
                or re.match(r"\s*该条(?:款)?仅适用于",item)
                or (len(_policy_duration_matches(joined[-1]))==1 and not _policy_duration_matches(item)
                    and re.match(r'\s*(?:它|该窗口|该期限|此窗口)(?:是)?(?:自|从|以)',item))
                or (not _policy_duration_matches(joined[-1]) and len(_policy_duration_matches(item))==1
                    and re.search(r'适用对象是|(?:规定|规定的是).{0,30}申请窗口',joined[-1])
                    and re.match(r'\s*(?:须|需|从|自|窗口)',item))):
            joined[-1] += item
        else:
            joined.append(item)
    return [item.strip() for item in joined if item.strip()]


def _policy_scope_tokens(value: str, *, limit: int | None = 4) -> tuple[str, ...]:
    """Normalize a locally scoped subject, without retaining attribution or predicate debris."""
    text = (value or "").casefold().replace("-", " ")
    text = _POLICY_SOURCE_LABEL_RE.sub(" ", text)
    text = re.sub(r"\b(?:according\s+to|policy|rule|source)\s+[a-z0-9_-]+\b", " ", text)
    text = re.sub(r"(?:称|说|规定|指出|表明|说明)", " ", text)
    # These are grammatical links between a subject and its predicate, not
    # scope tokens. They are trimmed only after assertion boundaries are found.
    text = re.sub(r"(?:\s|^)(?:在|于|将于|将|会在|会于)\s*$", " ", text)
    for pattern, canonical in _POLICY_SUBJECT_ALIASES:
        text = pattern.sub(f" {canonical} ", text)
    words = re.findall(r"[a-z][a-z0-9]*|[\u3400-\u9fff]+", text)
    normalized = []
    for word in words:
        if word in _POLICY_SCOPE_STOP:
            continue
        if word.endswith("s") and len(word) > 3:
            word = word[:-1]
        normalized.append(word)
    return tuple(normalized if limit is None else normalized[-limit:])


def _policy_subject_is_pronominal(subject: tuple[str, ...]) -> bool:
    return bool(subject) and set(subject).issubset({
        "it", "its", "they", "them", "this", "that", "these", "those",
        "其", "该项", "该商品", "这个", "这些"})


def _policy_expiry_facts(text: str) -> list[dict]:
    """Extract bounded expiry predicates from independent local assertions."""
    facts = []
    for clause in _policy_assertion_clauses(text):
        negative_expiry = re.search(
            r"\b(?:does\s+not|doesn't|never|no\s+longer)\s+expire\b|\bnon[- ]?expiring\b|"
            r"不(?:会|再)?过期|永不过期|不过期", clause, re.I)
        if negative_expiry:
            subject = clause[:negative_expiry.start()]
            facts.append({"kind": "NO_EXPIRY", "subject": _policy_scope_tokens(subject)})
        for match in _policy_duration_matches(clause):
            local_clause, local_start, local_end = _window_clause_context(
                clause, match.start(), match.end())
            relation = _window_relation(local_clause, local_start, local_end)
            if _parenthetical_window_repeats_local_assertion(clause, match, relation):
                continue
            before, after = local_clause[:local_start], local_clause[local_end:]
            expiry_before = re.search(
                r"(?P<subject>[^,;：:。！？!?]{1,100}?)\s+(?:expires?|expire)\s+(?:after|in)\s*$",
                before, re.I)
            expiry_after = re.match(r"^\s*(?:后|以后)\s*(?:过期|到期|失效)", after)
            if not expiry_before and not expiry_after:
                continue
            subject = expiry_before.group("subject") if expiry_before else before
            if not expiry_before:
                subject = re.sub(r"(?:在|于)\s*$", "", subject.strip())
            facts.append({"kind": "EXPIRES_AFTER", "amount": _policy_amount(match.group("amount")),
                "unit": _POLICY_WINDOW_UNITS[match.group("unit").casefold()],
                "subject": _policy_scope_tokens(subject)})
    return facts


def _answer_marks_policy_conflict(text: str) -> bool:
    """Require an affirmative local conflict assertion, not a negated keyword."""
    affirmative_difference = re.compile(
        r"并不相同|并非一致|并不一致|不相同|不一致|不同|not\s+the\s+same|different", re.I)
    conflict_marker = re.compile(
        r"冲突|矛盾|conflicts?|contradict(?:s|ed|ory|ion)?|disagree(?:s|d|ment)?", re.I)
    explicit_no_conflict = re.compile(
        r"(?:不存在|没有|并无|无|no\s+(?:actual\s+)?|there\s+is\s+no\s+)"
        r"(?:任何|any\s+)?(?:冲突|矛盾|差异|conflict|contradiction|disagreement)", re.I)
    for clause in _policy_assertion_clauses(text):
        # Negated conflict terms are removed locally; an independent affirmative
        # contrast elsewhere in this clause can still establish disagreement.
        remaining = explicit_no_conflict.sub(" ", clause)
        if affirmative_difference.search(remaining):
            return True
        for marker in conflict_marker.finditer(remaining):
            prefix = remaining[:marker.start()]
            if not re.search(
                    r"(?:并不|不|并非|没有|未|无|does\s+not|do\s+not|doesn't|don't|cannot|can't|not|no)\s*$",
                    prefix, re.I):
                return True
    return False


def _window_relation(clause: str, start: int, end: int) -> str:
    before, after = clause[:start], clause[end:]
    if re.search(r"(?:超过|多于|大于|more\s+than|greater\s+than|over)\s*$", before, re.I):
        return "EXCEEDS"
    if re.search(r"(?:expires?|expire)\s+(?:after|in)\s*$", before, re.I) or re.match(r"\s*(?:后|以后)\s*(?:过期|到期|失效)", after):
        return "EXPIRES_AFTER"
    if re.search(r"(?:within|no later than)\s*$", before, re.I) or re.match(r"\s*内", after):
        return "WITHIN"
    if re.search(r"(?:valid\s+for|validity(?:\s+period)?\s*(?:is|of)?|effective\s+for)\s*$", before, re.I) or re.search(r"有效期\s*(?:为|是)?\s*$", before):
        return "VALID_FOR"
    if re.search(r"(?:after|following)\s*$", before, re.I) or re.match(r"\s*后", after):
        return "AFTER_EVENT"
    return "UNSCOPED"


def _window_subject(clause: str, start: int, relation: str) -> tuple[str, ...]:
    before = clause[:start]
    # A leading attribution is not the object of the duration that follows.
    # Strip it before tokenization, retaining the explicitly named local noun.
    before = re.sub(r"^[\u3400-\u9fff]{1,8}政策(?:另|还)?(?:规定|指出|说明)[，,:：]?\s*", "", before)
    # Discourse attribution is outside the local noun phrase.  Keep modifiers
    # on the entity, removing only the explicit evidence preface.
    before = re.sub(r"^\s*(?:根据|依据|按照|按)(?:本轮|上述|当前)?(?:的)?(?:虚构)?(?:政策|条款|规则)[，,:：]\s*", "", before)
    before = re.sub(r"^\s*(?:另需注意[，,:：]?|另)\s*", "", before)
    # A service's booking period and the booking period name the same event;
    # product/material/category modifiers remain intact.
    before = re.sub(r"服务的(?=预约)", "", before)
    if relation == "WITHIN":
        before = re.sub(r"^(?:退货|退款)申请(?:需|须|应)(?:在)?(?=商品|产品|食品|密封|已拆封)", "", before)
        before = re.sub(r"退货(?:申请)?(?:还|通常)?(?:须|应)?(?:在|条款为)(?=.{0,8}送达后)", "", before)
    before = re.sub(r"^(?:退货|退款|保修)方面[，,]?\s*", "", before)
    # Separate the local entity from an event used as the clock origin.
    origin = re.search(r"(?:自|从)(?:经核实的|已核验的|记录的|已记录的)?"
        r"(?:购买|激活|签收|送达|支付|付款|下单|注册)(?:日期|之日|时间|日)?(?:起|后)\s*$", before)
    if origin:
        before = before[:origin.start()]
    before = re.sub(r"^\s*(?:根据|依据|按照)(?:本轮|上述|当前)?(?:的)?(?:证据|来源)[^，,:：]{0,40}[，,:：]\s*", "", before)
    before = re.sub(r"^\s*(?:according\s+to|based\s+on)\s+[^,;]{0,60}(?:evidence|policy|source)[^,;]{0,30},\s*", "", before, flags=re.I)
    before = re.sub(r"^\s*(?:虚构|fictional\b)\s*", "", before, flags=re.I)
    before = re.sub(r"(?:期限|时长|期)?(?:为|是)\s*$|\s+(?:lasts?|is|of)\s*$", "", before, flags=re.I)
    if relation == "VALID_FOR":
        before = re.sub(r"有效(?:期)?\s*$", "", before)
        before = re.sub(r"的\s*$", "", before)
        before = re.sub(r"\s+(?:is\s+)?(?:valid(?:ity(?:\s+period)?)?|effective)(?:\s+(?:is|of))?(?:\s+for)?\s*$", "", before, flags=re.I)
    if relation == "UNSCOPED":
        # In a coordinated list such as "10天与30天", the text before the
        # second value is another duration, not an entity subject.  Do not
        # turn the unit plus conjunction ("天与") into a guessed subject.
        prior_windows = _policy_duration_matches(before)
        if prior_windows:
            prior = prior_windows[-1]
            bridge = before[prior.end():]
            if re.fullmatch(r"\s*(?:与|和|及|、|,|，|and|&)\s*", bridge, re.I):
                return ()
    if relation == "EXPIRES_AFTER":
        expiry_before = re.search(
            r"(?P<subject>[^,;：:。！？!?]{1,100}?)\s+(?:expires?|expire)\s+(?:after|in)\s*$",
            before, re.I)
        if expiry_before:
            return _policy_scope_tokens(expiry_before.group("subject"))
        # In Chinese expiry constructions the locative marker is attached to
        # the noun ("礼品卡在 12 个月后过期"). Remove it from the local
        # subject span before normalizing the entity.
        before = re.sub(r"(?:在|于)\s*$", "", before.strip())
    temporal_anchor = _policy_time_anchor_before(before)
    if temporal_anchor and relation in {"WITHIN", "EXCEEDS", "AFTER_EVENT"}:
        return _policy_scope_tokens(temporal_anchor)
    # Local clause segmentation has already removed preceding discourse and
    # source assertions; normalization now removes only attribution/grammar.
    return _policy_scope_tokens(before)


def _window_start_event(clause: str, start: int, end: int) -> tuple[str, ...]:
    """Bounded event/verification scope; durations are never calendar dates."""
    before, after = clause[:start], clause[end:]
    prefix = re.search(r"(?:自|从)(?:经核实的|已核验的|未核实的|记录的|已记录的)?"
        r"(?:购买|激活|签收|送达|支付|付款|下单|注册)(?:日期|之日|时间|日)?(?:起|后)\s*$", before)
    suffix = re.match(r"\s*(?:from|after|following)\s+(?:(?:the|recorded|verified|unverified)\s+)*"
        r"(?:purchase|activation|delivery|receipt|payment|registration|order)(?:\s+date)?\b", after, re.I)
    value = prefix.group() if prefix else suffix.group() if suffix else ""
    if not value:
        return ()
    events = ((r"购买|\bpurchase\b", "purchase"), (r"激活|\bactivation\b", "activation"),
        (r"签收|送达|\b(?:delivery|receipt)\b", "delivery"),
        (r"支付|付款|\bpayment\b", "payment"), (r"注册|\bregistration\b", "registration"),
        (r"下单|\border\b", "order"))
    event = next(name for pattern, name in events if re.search(pattern, value, re.I))
    qualifiers = []
    if re.search(r"记录|\brecorded\b", value, re.I): qualifiers.append("recorded")
    if re.search(r"未核实|\bunverified\b", value, re.I): qualifiers.append("unverified")
    elif re.search(r"核实|核验|\bverified\b", value, re.I): qualifiers.append("verified")
    return (event, *qualifiers)


def _source_window_facts(row: dict) -> list[dict]:
    facts = _policy_window_facts(row.get("content") or "")
    metadata = " ".join(str(row.get(key) or "") for key in ("title", "policy_id", "chunk_id"))
    topics = {canonical for pattern, canonical in _POLICY_SUBJECT_ALIASES
              if canonical in {"warranty", "giftcard", "return", "refund"} and pattern.search(metadata)}
    for fact in facts:
        if not fact["subject"] and len(topics) == 1:
            fact["subject"] = (next(iter(topics)),)
    return facts


def _eligibility_requirements(text: str) -> list[set[str]]:
    """Check explicitly asserted prerequisite lists, not an unknown personal entitlement."""
    matches = re.finditer(r"(?:eligibility|coverage|资格)[^.;。；!?！？]{0,24}?"
        r"(?:requires?|需要|需)([^.;。；!?！？]+)", text or "", re.I)
    facts = []
    for match in matches:
        value = match.group(1)
        tokens = {name for pattern, name in ((r"购买|\bpurchase\b", "purchase"),
            (r"激活|\bactivation\b", "activation"), (r"注册|\bregistration\b", "registration"),
            (r"核验|核实|\bverified\b", "verified")) if re.search(pattern, value, re.I)}
        if re.search(r"未核实|无需|无须|不需要|\bunverified\b|\bwithout\b", match.group(), re.I): tokens.add("negated")
        if tokens: facts.append(tokens)
    return facts


def _coordinated_window_subject(clause: str, first_start: int) -> tuple[str, ...]:
    """Keep the explicit shared scope of a coordinated duration list.

    For ``...分别规定密封商品退货期为10天和30天`` the phrase after
    ``分别规定`` is the asserted object.  For ``密封商品退货期分别为10天和
    30天`` the phrase before ``分别`` is the object.  This helper does not
    assign values to individual sources; it only preserves a shared local
    subject, so changed objects remain detectable by the normal source match.
    """
    prefix = (clause or "")[:first_start].strip()
    if not prefix:
        return ()
    marker = re.search(r"(?:分别\s*(?:规定|说明|写明|注明|列为)|respectively\s+(?:state|states|specify|specifies|list|lists))\s*", prefix, re.I)
    if marker:
        subject_text = prefix[marker.end():]
    else:
        suffix = re.search(r"(?:分别|respectively)\s*$", prefix, re.I)
        subject_text = prefix[:suffix.start()] if suffix else prefix
    subject_text = re.sub(r"\s*(?:为|是|:|：)\s*$", "", subject_text).strip()
    return _policy_scope_tokens(subject_text)


def _policy_condition_tokens(value: str) -> tuple[str, ...]:
    text = (value or "").casefold()
    text = re.sub(r"\b(?:not|never|no|without)\b|未|不|没有|无|并非", " negated ", text)
    for pattern, canonical in _POLICY_SUBJECT_ALIASES:
        text = pattern.sub(f" {canonical} ", text)
    words = re.findall(r"[a-z][a-z0-9]*|\d+|[\u3400-\u9fff]+", text)
    ignored = {"if", "only", "when", "unless", "is", "are", "was", "were", "be", "the", "a", "an",
               "已", "的", "则", "才", "时", "情况下", "仅当", "如果", "若", "除非"}
    return tuple(word for word in words if word not in ignored)


def _window_condition(clause: str) -> tuple[str, ...]:
    # Implicit Chinese prerequisites ("普通商品...未影响二次销售可申请")
    # and their explicit "如果是..." paraphrases describe the same scope.
    # Normalize only this bounded grammar; unknown qualifiers retain the legacy
    # conservative tokens rather than being discarded with a broad stop list.
    area = re.split(r'(?:可以|可|能够|能|应当|应|必须|须)(?:在|于)?', clause, maxsplit=1)[0]
    # A mention under "regardless/even if" does not assert the positive
    # prerequisite. Keep its explicit scope instead of matching a noun inside it.
    if re.search(r'无论|不管|即使|哪怕|\bregardless\b|\beven\s+if\b', area, re.I):
        return _policy_condition_tokens(area)
    atoms = []
    # Mandatory qualifiers can follow the permission predicate. They remain
    # part of this local assertion, rather than being erased at “可申请”.
    if re.search(r'(?:未|没有|不)影响二次销售',clause):atoms.append('resale_unaffected')
    if re.search(r'保留完整附件',clause):atoms.append('complete_accessories')
    if re.search(r'(?:无需|无须|不必|不需要)保留完整附件',clause):atoms.append('negated_accessories')
    for pattern, atom in ((r'普通商品', 'ordinary_product'),
            # Normalize a local existence predicate with its own polarity. Do
            # not discard arbitrary residual qualifiers or negate the entire
            # sentence: the following policy assertion may have another scope.
            (r'(?:不存在|没有|无|未出现|并非|不是)(?:质量问题|质量退货|质量瑕疵)', 'quality_issue_absent'),
            (r'(?:存在|出现|发生|确有|有)?(?:质量问题|质量退货|质量瑕疵)', 'quality_issue'),
            (r'(?:未|没有|不)影响二次销售', 'resale_unaffected')):
        if re.search(pattern, area):
            atoms.append(atom)
            area = re.sub(pattern, ' ', area)
    if atoms:
        for window in reversed(_policy_duration_matches(area)):
            area = area[:window.start()] + ' ' + area[window.end():]
        area = re.sub(r'签收后|收货后|如果|若|那么|则|是|且|在|内|的|时|[\s、，,:：;；。]+', '', area)
        explicit_guard = re.search(r'如果|若|除非|仅当|前提是|仅限|针对|\b(?:if|unless|provided)\b', clause, re.I)
        if not area or not explicit_guard:
            return tuple(sorted(set(atoms)))
    match = re.search(
        r"\b(?:provided\s+that|as\s+long\s+as|only\s+if|only\s+for|if|when|unless)\s+([^,.;!?]+)|"
        r"(?:前提是|仅当|如果|若|除非|仅限|针对)([^，。；！？]+)",
        clause or "", re.I)
    if not match:
        return ()
    value = next((part for part in match.groups() if part), "")
    return _policy_condition_tokens(value)


def _window_citation_anchors(clause: str, start: int, cited_ids) -> tuple[str, ...]:
    """Return exact cited IDs immediately attached to a bare numeric window.

    This links an unscoped number to one cited record without asserting that a
    relation such as WITHIN is equivalent to EXPIRES_AFTER. The caller only
    uses this anchor for UNSCOPED references and still requires one unique
    matching amount, unit, and condition in that exact record.
    """
    anchors = []
    for evidence_id in cited_ids:
        if not isinstance(evidence_id, str) or not evidence_id:
            continue
        for match in re.finditer(re.escape(evidence_id), clause, re.I):
            if match.end() > start:
                continue
            bridge = clause[match.end():start]
            if len(bridge) <= 16 and re.fullmatch(
                    r"\s*(?:(?:is|are|was|were|=|为|是)\s*)?", bridge, re.I):
                anchors.append(evidence_id)
    return tuple(dict.fromkeys(anchors))


def _explicit_source_scope(clause: str, cited_ids) -> tuple[str, ...]:
    """Resolve local source labels only against the selected current-run IDs."""
    ids = tuple(cited_ids)
    found = [key for key in ids if re.search(re.escape(key), clause, re.I)]
    for label in re.finditer(r'\b(?:fictional\s+)?(?:rule|policy|source)\s+([a-z0-9_-]+)\b|\b(DOC-\d+)\b', clause, re.I):
        token = next(value for value in label.groups() if value).casefold()
        matches = [key for key in ids if key.casefold() == token or key.casefold().endswith('-' + token)]
        if len(matches) == 1:
            found.extend(matches)
    return tuple(dict.fromkeys(found))


def _parenthetical_spans(text: str) -> list[tuple[int, int]]:
    stack: list[tuple[str, int]] = []
    pairs = {"(": ")", "（": "）", "[": "]", "［": "］"}
    spans: list[tuple[int, int]] = []
    for offset, char in enumerate(text):
        if char in pairs:
            stack.append((char, offset))
        elif stack and char == pairs[stack[-1][0]]:
            _opening, start = stack.pop()
            spans.append((start, offset))
    return sorted(spans)


def _window_clause_context(clause: str, start: int, end: int) -> tuple[str, int, int]:
    """Return local predicate text while keeping attached condition clauses."""
    spans = _parenthetical_spans(clause)
    enclosing = next(((left, right) for left, right in spans
        if left < start < right and end <= right), None)
    if enclosing is not None:
        left, right = enclosing
        return clause[left + 1:right], start - left - 1, end - left - 1

    condition_lead = re.compile(
        r"^\s*(?:if\b|only\s+if\b|when\b|unless\b|provided\s+that\b|as\s+long\s+as\b|"
        r"如果|若|仅当|除非|前提是|针对)", re.I)
    chars = list(clause)
    for left, right in spans:
        inner = clause[left + 1:right]
        if condition_lead.search(inner):
            continue
        windows = _policy_duration_matches(inner)
        if windows and any(_window_relation(inner, item.start(), item.end()) != "UNSCOPED"
                for item in windows):
            for offset in range(left, right + 1):
                chars[offset] = " "
    return "".join(chars), start, end


def _parenthetical_window_repeats_local_assertion(clause: str, match: re.Match,
        relation: str) -> bool:
    """Recognize a parenthetical translation only when it restates one adjacent fact.

    Subject elision is inherited from the immediately preceding assertion only
    when amount, unit, predicate relation, condition, and any explicit subject
    in the parenthesis agree exactly. A changed object or condition remains a
    separate assertion and is validated normally.
    """
    spans = _parenthetical_spans(clause)
    enclosing = next(((left, right) for left, right in spans
        if left < match.start() < right and match.end() <= right), None)
    if enclosing is None:
        return False

    opening, closing = enclosing
    prefix = clause[:opening].rstrip()
    inner = clause[opening + 1:closing]
    inner_matches = _policy_duration_matches(inner)
    if not prefix or len(inner_matches) != 1:
        return False
    inner_match = inner_matches[0]
    inner_relation = _window_relation(inner, inner_match.start(), inner_match.end())
    inner_subject = _window_subject(inner, inner_match.start(), inner_relation)
    inner_condition = _window_condition(inner)
    current_clause, _current_start, _current_end = _window_clause_context(
        clause, match.start(), match.end())
    current = (_policy_amount(match.group("amount")),
        _POLICY_WINDOW_UNITS[match.group("unit").casefold()], relation,
        _window_condition(current_clause))
    translated = (_policy_amount(inner_match.group("amount")),
        _POLICY_WINDOW_UNITS[inner_match.group("unit").casefold()], inner_relation,
        inner_condition)
    if current != translated:
        return False

    adjacent = _policy_window_facts(prefix)
    if len(adjacent) != 1:
        return False
    source = adjacent[0]
    same_fact = (source["amount"], source["unit"], source["relation"], source["condition"]) == current
    same_scope = bool(source["subject"]) and (not inner_subject or inner_subject == source["subject"])
    return same_fact and same_scope


def _is_uncertain_window_reference(clause: str, start: int, end: int) -> bool:
    """A bare duration mention under explicit uncertainty is not a policy assertion."""
    before = (clause or "")[:start]
    around = (clause or "")[max(0, start - 24):min(len(clause or ""), end + 28)]
    uncertainty = re.search(
        r"cannot\s+(?:confirm|determine|verify)|can't\s+(?:confirm|determine)|"
        r"\bunknown\b|\bunclear\b|\bnot\s+enough\s+information\b|"
        r"无法(?:据此)?(?:确认|确定|核实)|不能(?:据此)?(?:确认|确定|核实)|"
        r"尚不能确认|无法判断|不清楚|未能确认|未能核实|(?:本|该|这笔)(?:订单|单|申请)是否(?:已)?在", before, re.I)
    window_reference = re.search(r"\bwindow\b|\btime\s+window\b|窗口|期限", around, re.I)
    eligibility_reference = re.search(
        r"qualif(?:y|ies)|meet(?:s)?|satisf(?:y|ies)|符合|满足|达到", around, re.I)
    membership_question=bool(re.search(r'(?:本|该|这笔)(?:订单|单|申请)是否(?:已)?在\s*$',before) and re.match(r'期限内',clause[end:]))
    return bool(uncertainty and window_reference and (eligibility_reference or membership_question))


_LOCAL_CLOCK_RE = re.compile(
    r"(?:自|从|以)?[‘“\"'「『]?(?P<qualifier>有据的?|记录的?|已记录的?|经核实的?|未核实的?)?"
    r"(?P<event>服务登记|登记|注册|购买|激活|付款|支付|送达|交付|领取记录产生)"
    r"(?:事件|日期|时间|之日|日)?(?:后)?[’”\"'」』]?(?:起算|起|后)(?:[，,]?共)?"
    r"(?:保修(?:期)?(?:为|是)?)?\s*$")
_LOCAL_POST_CLOCK_RE = re.compile(
    r"(?:自|从|以)[‘“\"'「『]?(?P<qualifier>有据的?|记录的?|已记录的?|经核实的?|未核实的?)?"
    r"(?P<event>服务登记|登记|注册|购买|激活|付款|支付|送达|交付|领取记录产生)"
    r"(?:事件|日期|时间|之日|日)?[’”\"'」』]?(?:为起算点|起算|起|后)")


def _local_window_scope(clause: str, start: int, end: int) -> dict:
    """Resolve explicit local noun/clock/condition grammar, never from expected evidence.

    Unknown noun constructions keep the conservative legacy parse. Reference-only
    mentions are distinguished from complete rules; omitted prerequisites in a
    positive rule are never filled from a source.
    """
    before, after = clause[:start], clause[end:]
    # A preceding duration plus comma cannot be part of this explicit noun.
    # Preserve conditions in the full clause; trim only the local noun prefix.
    if _policy_duration_matches(before) and re.search(r'[,，、][^,，、]+$',before):
        before=re.split(r'[,，、]',before)[-1]
    before=re.sub(r'^\s*(?:该|本|此)?(?:条款|政策|来源)(?:明确)?(?:写了|写明|规定|指出)[：:]?\s*','',before)
    forbidden_transfer = re.search(r'(?:不能|不得)(?:套用|移用)([^，,；;]+?)的?\s*$',before)
    if forbidden_transfer:
        return {'subject':_policy_scope_tokens(forbidden_transfer.group(1).rstrip('的')),
                'relation':'NOT_TRANSFERABLE','condition':_window_condition(clause)}
    prefix_clock = _LOCAL_CLOCK_RE.search(before)
    # A postposed clock qualifies this duration only, within the same local
    # assertion; it never inherits a clock from another value or source.
    suffix_clock = _LOCAL_POST_CLOCK_RE.search(after)
    clock = prefix_clock or suffix_clock
    noun = before[:prefix_clock.start()] if prefix_clock else before
    # “X不按A计算，而是按B” retains X as the subject of B. The
    # rejected clock A is not inherited; the local positive clock is parsed below.
    noun = re.sub(r'[:：]?不按[^，,。；;]{1,32}[，,]而是按\s*$', '', noun)
    if prefix_clock and re.search(r'[,，][^,，]+$',noun):noun=re.split(r'[,，]',noun)[-1]
    noun = re.sub(r'的(?:核验|适用|起算)?(?:节点|窗口|期限)(?:是|为)\s*$', '', noun)
    noun = re.sub(r'^是(?=商品|产品)', '', noun)
    noun=re.sub(r'[:：]?以[“「\"]?(?:送达|交付|购买|付款)[”」\"]?为起点[，,]?\s*$','',noun)
    noun = re.sub(r'^\s*(?:按|根据|依据)(?:本轮|当前|上述)?(?:虚构)?(?:政策|条款)[，,：:]\s*','',noun)
    noun = re.sub(r"^\s*\d+[.)、]\s*", "", noun)
    noun = re.sub(r"(?:的)?保修期(?:是|为)?\s*$", "", noun)
    noun = re.sub(r"(?:[:：]|期限(?:是|为)?|期(?:是|为)|(?:是|为|按|适用))\s*$", "", noun).strip(' ‘“\"')
    identity_tail = re.match(r'[\s’”\"\']*(?:条款|窗口)?(?:属于|是指)([^，,。；;]+)', after)
    named_scope = before + (' ' + identity_tail.group(1) if identity_tail else '')
    post_subject = re.match(r'\s*窗口(?:仅限|仅适用于)([^，,；;。]+)',after)
    if post_subject:named_scope+=' '+post_subject.group(1)
    subject: tuple[str, ...] = ()
    # These are business entities, analogous to the bilingual vocabulary above.
    # Their values, clocks, conditions and resource IDs remain independent checks.
    if re.search(r"商业(?:服务)?手续费\s*(?:的)?减免|独立商业渠道的服务手续费减免|\bcommercial service fee waiver\b", named_scope, re.I):
        subject = ('commercial_service_fee_waiver',)
    elif (re.search(r"独立渠道(?:商用)?条款|\bindependent channel (?:rule|clause)\b", named_scope, re.I)
            or (re.search(r'另有(?:一条)?条款',before) and re.search(r'不同渠道',clause))
            or (identity_tail and re.fullmatch(r"(?:另一|一个|本|该)?独立渠道", identity_tail.group(1).strip()))):
        subject = ('independent_channel_rule',)
    elif re.search(r"保修|质保|warranty", before, re.I):
        resource = re.search(r"(?:商品|产品|product)\s+([A-Za-z][A-Za-z0-9_-]+)", clause, re.I)
        if resource is None:
            resource = re.search(r"(?:商品|产品)([A-Za-z][A-Za-z0-9_-]+)", clause)
        if resource:
            subject = ('warranty', 'resource:' + resource.group(1).casefold())
        elif clock and re.search(r'商品|产品|\bproducts?\b',noun,re.I):
            subject = _policy_scope_tokens(noun)
    elif re.search(r"刻字(?:订单|服务)|\bengraving (?:order|service)\b", noun,re.I):
        subject = ('engraving_order',)
    elif re.search(r"寄存柜", noun):
        subject = ('locker_rental',)
    elif clock and noun and re.search(r'期限|本体|保修|为|是|适用',before) and not re.search(r"条款|规则|政策|提到|写成|根据|依据",noun):
        # A local, explicit noun plus a clock is a complete scope. Keep the
        # noun and clock independently; never infer a noun from source values.
        subject = _policy_scope_tokens(noun)
    if not subject:
        return {}
    result: dict = {'subject': subject}
    if clock:
        events = {'服务登记': 'service_registration', '登记': 'registration', '注册': 'registration',
            '购买': 'purchase', '激活': 'activation', '付款': 'payment', '支付': 'payment',
            '送达': 'delivery', '交付': 'delivery', '领取记录产生': 'collection_record'}
        event = [events[clock.group('event')]]
        qualifier = clock.group('qualifier') or ''
        if '记录' in qualifier: event.append('recorded')
        if '未核实' in qualifier: event.append('unverified')
        elif '核实' in qualifier: event.append('verified')
        result['start_event'] = tuple(event)
        if subject[0] == 'warranty': result['relation'] = 'VALID_FOR'
        elif subject[0] == 'engraving_order': result['relation'] = 'WITHIN' if re.match(r'\s*内', after) else 'AFTER_EVENT'
        elif subject[0] == 'commercial_service_fee_waiver': result['relation'] = 'WITHIN'
        elif subject[0] in {'independent_channel_rule','locker_rental'}: result['relation'] = 'RUNS_FOR'
        else: result['relation'] = 'VALID_FOR' if re.search(r'保修',before) else _window_relation(clause,start,end)
        if (event[0]=='delivery' and re.search(r'按(?:送达|交付)后',before)
                and re.match(r'[的和、，]?.{0,12}条件.{0,8}核验',after)):
            result['relation']='WITHIN'
    else:
        english_clock = re.search(r'\b(?:from|after)\s+(?:the\s+)?(service registration|registration|activation|purchase|payment|delivery)\b',
            before + ' ' + after, re.I)
        if english_clock:
            result['start_event'] = (english_clock.group(1).casefold().replace(' ', '_'),)
            parsed_relation=_window_relation(clause,start,end)
            result['relation'] = ('WITHIN' if subject[0] == 'commercial_service_fee_waiver' else
                parsed_relation if parsed_relation!='UNSCOPED' else 'RUNS_FOR')
    explicit_relation = _window_relation(clause, start, end)
    if explicit_relation in {'EXPIRES_AFTER', 'EXCEEDS', 'AFTER_EVENT'}:
        result['relation'] = explicit_relation
    elif explicit_relation == 'WITHIN' and subject[0] == 'independent_channel_rule':
        result['relation'] = 'WITHIN'
    new_entitlement = re.search(r'可(?:以)?(?:申请)?(退货|退款|保修)', after)
    if new_entitlement and subject[0] in {'independent_channel_rule', 'commercial_service_fee_waiver'}:
        result['subject'] = (*subject, 'asserted_entitlement:' + new_entitlement.group(1))
    # Parse bounded conjunctions, preserving invalid/negated prerequisites.
    conditions = []
    if re.search(r'商业使用|商业用途|商用条款|\bcommercial use\b', clause, re.I): conditions.append('commercial_use')
    if re.search(r'个人使用|\bpersonal use\b', clause, re.I): conditions.append('personal_use')
    if re.search(r'无效服务凭证|\binvalid service evidence\b', clause, re.I): conditions.append('invalid_service_evidence')
    elif re.search(r'有效服务凭证|\bvalid service evidence\b', clause, re.I): conditions.append('valid_service_evidence')
    if re.search(r'(?:仅限|限)非耗材主体', clause): conditions.append('non_consumable_body')
    if re.search(r'确认图稿(?:之)?前|图稿确认(?:之)?前|\bbefore (?:the )?artwork confirmation\b', clause,re.I): conditions.append('before_artwork_confirmation')
    if re.search(r'(?:无需|无须|不需要|不适用于).{0,8}(?:商业使用|有效服务凭证|非耗材主体)|非商业使用|'
            r'\b(?:without|not required)\b.{0,20}(?:commercial use|valid service evidence)',clause,re.I):
        conditions.append('negated_condition')
    condition_tail = re.search(r'(?:仅适用于|且须|须为|须具备|仅限|only (?:if|for))([^，,。；;！？!?.]+)', clause, re.I)
    if condition_tail:
        residue = condition_tail.group(1)
        residue=re.sub(r'用于商业用途','商业使用',residue)
        residue=re.sub(r'^有(?=商业使用|商业用途|有效服务凭证)','',residue)
        if subject==('commercial_service_fee_waiver',):
            residue=re.sub(r'^商业服务手续费减免(?:申请)?', '', residue)
        residue = re.sub(r'(?:具有|有)(?=有效服务凭证)', '', residue)
        residue = re.sub(r'[（(](?:[A-Za-z0-9_-]+)[）)]', '', residue)
        for pattern in (r'商业使用|商业用途|个人使用|(?:无效|有效)服务凭证|非耗材主体',
                r'\b(?:commercial use|personal use|(?:invalid|valid) service evidence)\b',
                r'在(?:确认图稿(?:之)?前|图稿确认(?:之)?前)(?:提出|申请)?|确认图稿(?:之)?前|图稿确认(?:之)?前',
                r'同时满足|同时|且|并持|并具备|并|及|和|具有|具备|持有|持|有(?=有效服务凭证)|的商品|为|\band\b'):
            residue = re.sub(pattern, '', residue, flags=re.I)
        if residue.strip(' ：:、（）()'):
            conditions.append('unparsed_condition:' + residue.strip())
    result['condition'] = tuple(sorted(set(conditions)))
    # A lexical reference is not a license to assert an unqualified entitlement.
    reference = bool(re.search(r'涉及|提到|该条针对', clause) and re.search(r'等内容|等不同规则|并非同一事项', clause)
        and not re.search(r'可申请|可以申请|符合资格|适用本订单', clause))
    reference=reference or (explicit_relation=='UNSCOPED' and not clock
        and re.search(r'(?:看|核对).{0,40}窗口',clause)
        and not re.search(r'可申请|可以申请|符合资格|适用本订单',clause))
    if (reference or (identity_tail and explicit_relation == 'UNSCOPED'
            and not re.search(r'可申请|可以申请|符合资格|适用(?:于|本订单)|期限为|起算', clause))):
        result['scope'] = 'REFERENCE_ONLY'
    return result


def _policy_window_facts(text: str, *, cited_ids=None) -> list[dict]:
    # Quotes around an event are typography, not an omitted clock or subject.
    text=re.sub(r'[“「\"]((?:送达|交付|购买|付款)(?:后)?)[”」\"]',r'\1',text)
    text=re.sub(r'[“「\"]((?:送达|交付|购买|付款)后[的]?\s*[零〇一二两三四五六七八九十百\d]+\s*(?:小时|个月|天|日|周|月))[”」\"]',r'\1',text)
    text=re.sub(r'((?:送达|交付|购买|付款)后)的(?=\s*[零〇一二两三四五六七八九十百\d]+)',r'\1',text)
    facts = []
    previous_time_anchor: tuple[str, ...] = ()
    previous_window_condition: tuple[str, ...] = ()
    for clause in _policy_assertion_clauses(text):
        clause_windows = _policy_duration_matches(clause)
        coordinated_window_list = (len(clause_windows) > 1 and
            bool(re.search(r"分别", clause)) and
            bool(re.search(r"(?:和|与|及|以及|、)", clause)))
        coordinated_subject = (_coordinated_window_subject(clause, clause_windows[0].start())
            if coordinated_window_list else ())
        for match in clause_windows:
            local_clause, local_start, local_end = _window_clause_context(
                clause, match.start(), match.end())
            relation = _window_relation(local_clause, local_start, local_end)
            # Explicit denial that a calendar boundary is a duration is not a
            # positive time-window claim. Bound the denial to this local clause;
            # an independent entitlement or changed number remains checked.
            before,after=local_clause[:local_start],local_clause[local_end:]
            if (re.search(r'(?:日历边界|日历日期|日期节点|calendar date).{0,20}(?:不是|不等于|并不代表|不代表|不表示|is not|does not equal)',before,re.I)
                    and re.search(r'持续时长|duration',after,re.I)
                    and not re.search(r'可申请|可以申请|符合资格|eligible',local_clause,re.I)):
                continue
            if _parenthetical_window_repeats_local_assertion(clause, match, relation):
                continue
            if relation == "UNSCOPED" and _is_uncertain_window_reference(
                    local_clause, local_start, local_end):
                continue
            subject = _window_subject(local_clause, local_start, relation)
            if re.fullmatch(r'(?:而|另一条)?[“”「」\s]*送达后[“”「」\s]*',local_clause[:local_start]):
                subject=('送达后',)
            anchored_ids = _window_citation_anchors(local_clause, local_start, cited_ids) if cited_ids is not None else ()
            # A sentence that reports multiple durations as a coordinated
            # list (“the excerpts respectively state … 10 days and 30 days”)
            # does not by itself map each value to a specific cited source.
            # Keep the values unassigned; later support requires every exact
            # value/subject/condition to exist in the current cited set.
            if coordinated_window_list and relation == "UNSCOPED":
                # A coordinated list does not identify which source owns each
                # value, but it does retain an explicitly named shared object.
                # Do not erase that scope: doing so would let an answer about
                # another object pass as unresolved rather than be rejected.
                subject = coordinated_subject
                anchored_ids = ()
            # Explicit, current-run source IDs immediately attached to a value
            # provide attribution. Remove the ID from the entity scope; without
            # such an anchor, a coordinated value list remains unassigned.
            if relation == "UNSCOPED" and len(anchored_ids) == 1:
                subject = ()
            anchor_text = _policy_time_anchor_before(local_clause[:local_start])
            if anchor_text:
                previous_time_anchor = _policy_scope_tokens(anchor_text)
            elif relation == "EXCEEDS" and previous_time_anchor:
                # A comparative boundary in the immediately following policy
                # clause may elide the event anchor stated in the preceding
                # window (for example, within N days after receipt; over N days).
                subject = previous_time_anchor
            elif relation != "EXCEEDS":
                previous_time_anchor = ()
            fact = {"amount": _policy_amount(match.group("amount")),
                "unit": _POLICY_WINDOW_UNITS[match.group("unit").casefold()],
                "raw_unit": match.group("unit"),
                "relation": relation, "subject": subject,
                "condition": _window_condition(local_clause)}
            if relation == 'EXCEEDS' and previous_time_anchor and not fact['condition']:
                fact['condition'] = previous_window_condition
            elif relation == 'WITHIN':
                previous_window_condition = fact['condition']
            elif relation != 'EXCEEDS':
                previous_window_condition = ()
            origin = _window_start_event(local_clause, local_start, local_end)
            if origin:
                fact["start_event"] = origin
            # A follow-up membership question may elide the prerequisite of
            # an already stated, exactly matching local rule. It cannot invent
            # a new value, unit, event, relation, subject or explicit condition.
            membership_prefix = (bool(anchor_text) and bool(re.search(
                rf'(?:您|你)?是否(?:已经)?(?:在|于)?\s*{re.escape(anchor_text)}\s*$',
                local_clause[:local_start])))
            if (not fact['condition'] and membership_prefix
                    and not re.search(r'可(?:以)?(?:提交|申请)|符合资格|有权|eligible', local_clause, re.I)):
                prior = [item for item in facts if all(item.get(key) == fact.get(key)
                    for key in ('amount', 'unit', 'relation', 'subject', 'start_event'))]
                if prior and all(item['condition'] == prior[0]['condition'] for item in prior):
                    fact['condition'] = prior[0]['condition']
            # Retain the outer noun and attached tails for parenthetical clocks.
            fact.update(_local_window_scope(clause, match.start(), match.end()))
            if 'product' in fact['subject']:
                qualifiers=set(_window_condition(clause)) & {'ordinary_product','quality_issue','quality_issue_absent',
                    'resale_unaffected','complete_accessories','negated_accessories'}
                fact['condition']=tuple(sorted(set(fact['condition'])|qualifiers))
            if fact.get('start_event')==('delivery',) and fact['subject']==('product',) and fact['relation']=='WITHIN':
                fact['subject']=('product','送达后');fact.pop('start_event')
            if fact.get('start_event') == ('delivery',) and fact['subject'] == ('product','本体'):
                # A locally stated inspection condition can follow its window.
                # Do not inherit it from another clause/source or another object.
                if re.search(r'(?:商品)?完好(?:的)?条件',clause):
                    fact['condition']=tuple(sorted(set(fact['condition'])|{'intact'}))
                if re.search(r'核验|核验节点',clause) and fact['relation']=='UNSCOPED':
                    fact['relation']='WITHIN'
                if re.search(r'核验节点是|为期限',clause) and not re.search(r'可申请|可以申请|符合资格',clause):
                    fact['scope']='REFERENCE_ONLY'
            if (fact.get('start_event')==('delivery',) and fact['subject']==('product','return')
                    and re.search(r'(?:^|[:：])\s*商品退货适用送达后',clause)):
                # Same generic return-window grammar as “商品送达后N天可退货”.
                # Explicit food/sealed/opened objects do not match this noun.
                fact['subject']=('product','送达后');fact['relation']='WITHIN';fact.pop('start_event')
            if re.search(r'你问到的|您问到的',clause) and re.search(r'只作为对照|仅作对照',clause):
                quoted=re.search(r'[“「]([^”」]+)[”」]',clause)
                if quoted:
                    quoted_subject=re.sub(r'[零〇一二两三四五六七八九十百\d]+\s*(?:个月|月|天|日|小时).*$', '',quoted.group(1))
                    noun_tokens=_policy_scope_tokens(quoted_subject)
                    prior=[f for f in facts if _same_policy_duration(f,fact)
                        and tuple(t for t in f['subject'] if t!='product')==noun_tokens]
                    if len(prior)==1:
                        fact['subject']=prior[0]['subject'];fact['scope']='REFERENCE_ONLY'
            # A comparison referring back to a named rule is not a fresh,
            # unqualified entitlement; its value and explicit object still
            # have to match exactly one of the current cited source rules.
            reference=re.search(r'(?:与|和)([^，,；;。]+?)的\s*$',before)
            if reference and re.match(r'\s*规则(?:并)?(?:不相同|不同|不一致)',after):
                fact={'amount':fact['amount'],'unit':fact['unit'],'raw_unit':fact['raw_unit'],
                    'relation':'UNSCOPED','subject':_policy_scope_tokens(reference.group(1)),
                    'condition':(),'scope':'REFERENCE_ONLY'}
            quoted_clock=re.search(r'期限是[“\"](?:送达|交付)后\s*$',before)
            if quoted_clock:
                fact['subject']=('送达后',);fact['relation']='UNSCOPED';fact.pop('start_event',None)
            # An explicitly marked later summary can inherit one already stated
            # assertion. A new value, event, object or prerequisite cannot do so.
            summary = re.match(r'\s*(?:因此(?:该|是)|\d+\s*(?:天|小时|个月)手续费减免窗口专属)', clause)
            if summary and relation == 'UNSCOPED' and not origin:
                antecedents = [prior for prior in facts if prior['amount'] == fact['amount']
                    and prior['unit'] == fact['unit'] and prior.get('start_event')
                    and (not fact['subject'] or fact['subject'] == prior['subject']
                         or fact['subject'] in {('因此',), ('因此该',)})
                    and not fact['condition']]
                unique = {repr((prior['subject'], prior['start_event'], prior['condition'], prior['relation'])): prior
                    for prior in antecedents}
                if len(unique) == 1:
                    prior = next(iter(unique.values()))
                    for key in ('subject', 'start_event', 'condition', 'relation'):
                        fact[key] = prior[key]
            if cited_ids is not None:
                fact["_cited_source_ids"] = anchored_ids or _explicit_source_scope(clause, cited_ids)
            facts.append(fact)
    # A one-window answer may put its conditions in a separate labelled row.
    # Never inherit proximity-based scope between several policy windows.
    if len(facts) == 1:
        # A following mandatory qualifier belongs to this sole rule, not to an
        # unrelated window. Multiple-window paragraphs never inherit by proximity.
        mandatory = re.search(r'[；;]\s*须([^。；;]+)',text)
        if mandatory:
            extra=[]
            if re.search(r'未影响二次销售',mandatory.group(1)):extra.append('resale_unaffected')
            if re.search(r'保留完整附件',mandatory.group(1)):extra.append('complete_accessories')
            facts[0]['condition']=tuple(sorted(set(facts[0]['condition'])|set(extra)))
        # A single explicit rule may state its prerequisite in the following
        # labelled sentence; multiple windows never inherit by proximity.
        prerequisite=re.search(r'(?:另一个仍需满足的前提|必要前提|前提条件)是[：:]?([^。]+)',text)
        if prerequisite and re.search(r'确认图稿(?:之)?前|图稿确认(?:之)?前',prerequisite.group(1)):
            facts[0]['condition']=tuple(sorted(set(facts[0]['condition'])|{'before_artwork_confirmation'}))
        scopes = re.findall(r'(?:商品(?:范围|状态)|适用条件|前提条件|同时满足[^:：。]{0,12}条件)'
            r'\s*[:：]([^\n。]+)', text)
        if len(scopes) == 1:
            conditions = _window_condition(scopes[0])
            if conditions:
                facts[0]['condition'] = tuple(sorted(set(facts[0]['condition']) | set(conditions)))
    return facts

def _same_policy_duration(left: dict, right: dict) -> bool:
    """Exact elapsed-unit equivalence; calendar months/years are never guessed."""
    hours = {'hour': 1, 'day': 24, 'week': 168}
    if left['unit'] in hours and right['unit'] in hours:
        return left['amount'] * hours[left['unit']] == right['amount'] * hours[right['unit']]
    return (left['amount'], left['unit']) == (right['amount'], right['unit'])


def _answer_semantic_issues(answer: str, cited_evidence: list[dict], question: str = "") -> list[dict]:
    """Reject policy assertions whose amount, unit, relation, subject, or condition is unsupported."""
    cited = {row.get("chunk_id"): row for row in cited_evidence}
    issues = []
    # Unsupported/ambiguous number notation is not a supported scalar. Never
    # silently read only the last digit or let an unparsed assertion pass.
    parsed_spans={(m.start(),m.end()) for m in _policy_duration_matches(answer)}
    date_spans=[(m.start(),m.end()) for m in _POLICY_CALENDAR_DATE_RE.finditer(answer)]
    for match in re.finditer(r"(?<![零〇一二两三四五六七八九十百千万亿])"
            r"[零〇一二两三四五六七八九十百千万亿]+\s*(?:小时|个月|天|日|周|月|年)",answer):
        if (match.start(),match.end()) in parsed_spans:
            continue
        if any(match.start()<end and start<match.end() for start,end in date_spans):
            continue
        issues.append({'code':'ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED','window':match.group(),
            'basis':'duration number notation is ambiguous or outside the bounded parser'})
    question_stages = _policy_stages(question)
    for stage in _policy_stages(answer):
        # A lifecycle state explicitly supplied by the user is context, not a
        # policy assertion. It must still be handled as unverified user input.
        if stage in question_stages:
            continue
        assertions = _stage_polarity_assertions(answer, stage, scope_commas=True)
        non_implication_only = bool(assertions) and all(
            re.search(r"不代表|不等于", clause) and
            any(re.search(pattern, clause) for pattern in _STAGE_PATTERNS[stage])
            for _, clause in assertions)
        supporting = [row for row in cited.values() if stage in _evidence_policy_stages(row)]
        if not supporting:
            # Explicitly distinguishing two workflow states (for example,
            # “approval does not mean return completion”) is not a policy
            # assertion that the source must enumerate as a separate phase.
            reference_only = bool(assertions) and all(
                re.search(r'其他条款|另.{0,5}条款',clause) and
                re.search(r'不是|并非|不能.{0,4}套用|不适用于',clause) and
                not re.search(r'可申请|可以申请|已经批准|已批准|符合资格',clause)
                for _,clause in assertions)
            sentences=[s for s in re.split(r'[。！？!?；;]',answer)
                if any(re.search(pattern,s) for pattern in _STAGE_PATTERNS[stage])]
            reference_only = reference_only or bool(sentences) and all(
                re.search(r'其他条款.{0,180}(?:均不是|并不属于).{0,30}(?:窗口|规则)',s)
                and not re.search(r'可申请|可以申请|已经批准|已批准|符合资格',s)
                for s in sentences)
            # Non-commitment is not a positive claim of lifecycle progress.
            # Inspect every local assertion: a later affirmative stage still
            # requires independent support and cannot hide behind this denial.
            non_commitment = bool(assertions) and all(re.search(
                r'不能(?:视为|据此保证|直接承诺|保证)|不涉及.{0,20}结论|'
                r'无法.{0,20}(?:核验|执行)|不(?:直接)?(?:承诺|保证)',clause)
                for _,clause in assertions)
            if non_implication_only or reference_only or non_commitment:
                continue
            issues.append({"code": "ANSWER_UNSUPPORTED_POLICY_PHASE", "policy_stage": stage,
                           "basis": "cited policy does not state this workflow phase"})
            continue
        answer_polarities = {polarity for polarity, clause in assertions
                             if not (re.search(r"不代表|不等于", clause) and
                                     any(re.search(pattern, clause) for pattern in _STAGE_PATTERNS[stage]))
                             and not (stage == "RETURN_APPROVAL" and
                                      _states_approval_prerequisite_denial(clause))}
        source_polarities = set().union(*(_stage_polarities(
            _evidence_stage_text(row), stage,
            scope_commas=True)
            for row in supporting))
        mismatched = answer_polarities - source_polarities
        if (mismatched and not (stage == "RETURN_SUBMISSION" and
                                _scoped_expired_return_boundary(answer, list(cited.values())))):
            issues.append({"code": "ANSWER_POLICY_PHASE_POLARITY_MISMATCH", "policy_stage": stage,
                           "basis": "answer reverses the cited policy's positive/negative statement"})
    # A policy listing workflow stages does not establish that this user's
    # payment or approval happened. Business completion belongs to tool facts.
    for stage in ('REFUND_SETTLEMENT','REFUND_APPROVAL','RETURN_APPROVAL'):
        for _,clause in _stage_polarity_assertions(answer,stage,scope_commas=True):
            if (re.search(r'(?:退款|款项|退货)(?:已经|已)(?:到账|批准|完成)',clause)
                    and not re.search(r'不能视为|不代表|不等于|尚未|未|不得',clause)):
                issues.append({'code':'ANSWER_UNSUPPORTED_POLICY_COMPLETION','policy_stage':stage,
                    'basis':'policy lifecycle rules alone do not prove completed personal business facts'})
    # Do not transfer a settlement disclaimer scoped to a refund application
    # onto a return application. A citation may contain both workflow stages;
    # that does not make every cross-stage implication in the answer supported.
    clauses = re.split(r"[。！？!?；;，,]", answer or "")
    for clause in clauses:
        if not re.search(r"(?:申请退货|退货申请|提交.{0,6}退货申请|退货单|退货流程|退货获批|退货审核)", clause):
            continue
        if not re.search(r"到账|入账|收到款|收款|结算", clause):
            continue
        explicitly_refund_scoped = bool(re.search(
            r"(?:退款申请|申请.{0,4}退款).{0,18}(?:到账|入账|收到款|收款|结算)|"
            r"(?:到账|入账|收到款|收款|结算).{0,18}(?:退款申请|申请.{0,4}退款)", clause))
        return_terms = r"(?:申请退货|退货申请|提交.{0,6}退货申请|退货单|退货流程|退货获批|退货审核)"
        settlement_terms = r"(?:到账|入账|收到款|收款|结算)"
        sourced_return_settlement = any(
            re.search(rf"{return_terms}.{{0,32}}{settlement_terms}|{settlement_terms}.{{0,32}}{return_terms}",
                      clause)
            for row in cited.values()
            for clause in re.split(r"[。！？!?；;]", row.get("content") or ""))
        if not explicitly_refund_scoped and not sourced_return_settlement:
            issues.append({"code": "ANSWER_UNSUPPORTED_CROSS_STAGE_SCOPE",
                           "policy_stages": ["RETURN_SUBMISSION", "REFUND_SETTLEMENT"],
                           "basis": "evidence states settlement behavior for refund applications, not return applications"})
    for amount, unit in re.findall(r"超过\s*(\d+)\s*(天|日|小时|周|个月|月)", answer or ""):
        clauses = []
        for clause in re.split(r"[。！？!?；;]", answer or ""):
            boundary = re.search(rf"超过\s*{re.escape(amount)}\s*{re.escape(unit)}", clause)
            if boundary:
                # A permitted earlier window is not a claim about this later
                # boundary. Inspect only the assertion beginning at "超过N".
                clauses.append(clause[boundary.start():])
        if any(re.search(r"(?:仍可|仍能|可以|可).{0,12}(?:提交|申请).{0,8}退货", clause)
               for clause in clauses):
            contradicts_boundary = any(
                re.search(rf"超过\s*{re.escape(amount)}\s*{re.escape(unit)}", row.get("content") or "") and
                re.search(r"(?:需|应|须).{0,12}(?:转人工|人工核对).{0,12}(?:保修|核实)|(?:转人工|人工核对).{0,12}(?:保修|核实)",
                          row.get("content") or "")
                for row in cited.values())
            if contradicts_boundary:
                issues.append({"code": "ANSWER_POLICY_TIME_BOUNDARY_CONTRADICTION",
                               "window": f">{amount}{unit}",
                               "basis": "answer permits return submission beyond an explicit policy boundary that routes the case to manual warranty review"})
    # A question is a scope context, not a candidate subject phrase. Keep all
    # normalized terms so a long prompt does not discard its relevant entity.
    question_tokens = set(_policy_scope_tokens(question, limit=None))
    evidence_windows = [(row, fact) for row in cited.values()
        for fact in _source_window_facts(row)]
    # A duration-free affirmative expiry statement can still reverse a cited
    # non-expiry rule. Keep the exact local subject; uncertainty is not an
    # affirmative expiry assertion and never inherits a subject here.
    non_expiring_subjects = {tuple(f['subject']) for row in cited.values()
        for f in _policy_expiry_facts(row.get('content') or '') if f['kind']=='NO_EXPIRY'}
    for clause in _policy_assertion_clauses(answer):
        source_scope = _explicit_source_scope(clause, cited)
        if source_scope:
            for expiry in _policy_expiry_facts(clause):
                support = [source_expiry for source_id in source_scope
                    for source_expiry in _policy_expiry_facts(cited[source_id].get('content') or '')
                    if source_expiry == expiry]
                if not support:
                    issues.append({'code': 'ANSWER_UNSUPPORTED_POLICY_ATTRIBUTION',
                        'evidence_ids': list(source_scope),
                        'basis': 'the explicit source label does not support this expiry predicate and subject'})
        if re.search(r"不(?:会|再)?过期|永不过期|does\s+not\s+expire|never\s+expire",clause,re.I):
            continue
        positive = re.search(r"^(.*?)\s*(?:会|将)?过期[。.!]?\s*$|^(.*?)\s+expires?[.!]?\s*$",clause,re.I)
        if positive:
            scope=_policy_scope_tokens(next(part for part in positive.groups() if part is not None))
            if scope in non_expiring_subjects:
                issues.append({'code':'ANSWER_POLICY_EXPIRY_POLARITY_MISMATCH',
                    'basis':'an affirmative expiry assertion reverses a cited non-expiry rule for the same subject'})
    answer_window_facts = _policy_window_facts(answer, cited_ids=cited.keys())
    aggregate_value_list = (len(answer_window_facts) > 1 and
        all(fact.get("relation") == "UNSCOPED" and not fact.get("_cited_source_ids")
            for fact in answer_window_facts))
    for fact in answer_window_facts:
        # The user's explicit object may scope an elliptical answer, just as
        # the subject inheritance below does. It cannot override a competing
        # object or supply a missing resale/eligibility prerequisite.
        if ('resale_unaffected' in fact['condition'] and
                'ordinary_product' not in fact['condition'] and
                re.search(r'普通商品', question) and
                not re.search(r'(?:如果是|针对|仅限|适用于)(?:食品|定制商品|已拆封商品)|'
                    r'(?:食品|定制商品|已拆封商品)[^，。；]{0,12}(?:可|能)提交', answer)):
            fact['condition'] = tuple(sorted((*fact['condition'], 'ordinary_product')))
        anchored = fact.get('_cited_source_ids', ())
        local_evidence = [(row, source_fact) for row, source_fact in evidence_windows
            if not anchored or row['chunk_id'] in anchored]
        subject = tuple(fact.get("subject") or ())
        if subject==('送达后',) and re.search(r'商品',question) and not re.search(r'食品|密封商品|已拆封商品',question):
            # A bare event phrase may omit only the generic product explicitly
            # requested. Never replace an explicit competing object.
            local=[sf['subject'] for _,sf in local_evidence if _same_policy_duration(sf,fact)
                and sf['subject']==('product','送达后')]
            if local:subject=('product','送达后')
        if fact.get('scope') == 'REFERENCE_ONLY':
            references = [source_fact for _row, source_fact in local_evidence
                if _same_policy_duration(source_fact, fact)
                and source_fact['subject'] == subject
                and (not fact.get('start_event') or fact['start_event'] == source_fact.get('start_event'))
                and set(fact['condition']).issubset(source_fact['condition'])]
            if references and len({repr((f['amount'],f['unit'],f['relation'],f['subject'],
                    f.get('start_event'),f['condition'])) for f in references}) == 1:
                continue
        if not subject or _policy_subject_is_pronominal(subject):
            # An omitted subject may inherit the question's scope only when the
            # cited source independently shares that scope.
            matched_subjects = [tuple(row_fact.get("subject") or ()) for _row, row_fact in evidence_windows
                if set(row_fact.get("subject") or ()) & question_tokens]
            if matched_subjects and all(item == matched_subjects[0] for item in matched_subjects):
                subject = matched_subjects[0]
            elif any(row_fact.get("subject") for _row, row_fact in evidence_windows):
                issues.append({"code": "ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED",
                    "window": f"{fact['amount']}{fact.get('raw_unit', fact['unit'])}",
                    "basis": "the assertion has no safely inheritable subject scope"})
                continue
        matching = []
        if aggregate_value_list:
            # A value-only comparison can be supported by the cited evidence
            # set without inventing which source the answer paired with each
            # value. Require the value, inherited subject, and condition to
            # occur in current-run evidence; relation remains unasserted.
            value_support = [row for row, source_fact in evidence_windows
                if _same_policy_duration(source_fact, fact) and
                source_fact.get("start_event", ()) == fact.get("start_event", ()) and
                tuple(source_fact.get("subject") or ()) == subject and
                source_fact.get("condition", ()) == fact.get("condition", ())]
            if value_support:
                continue
            same_value = [source_fact for _row, source_fact in evidence_windows
                if _same_policy_duration(source_fact, fact)]
            if same_value and (any(tuple(item.get("subject") or ()) != subject for item in same_value) or
                    any(item.get("condition", ()) != fact.get("condition", ()) for item in same_value)):
                issues.append({"code": "ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED",
                    "window": f"{fact['amount']}{fact.get('raw_unit', fact['unit'])}",
                    "basis": "the value appears in cited evidence, but its subject or condition cannot be established"})
                continue
        for row, source_fact in local_evidence:
            source_subject = tuple(source_fact.get("subject") or ())
            # A missing object is not a wildcard: it cannot support an assertion
            # about a different explicit object. The question-scope inheritance
            # above is the only allowed way to fill an omitted answer subject.
            subject_matches = subject == source_subject
            if (_same_policy_duration(source_fact, fact) and
                    source_fact["relation"] == fact["relation"] and subject_matches and
                    source_fact.get("start_event", ()) == fact.get("start_event", ()) and
                    source_fact.get("condition", ()) == fact.get("condition", ())):
                matching.append(row)
        if not matching and fact["relation"] == "UNSCOPED":
            # A bare later reference such as “the 30-day window” does not
            # restate a different rule relation. Accept it only when this exact
            # duration, subject, and condition already has a cited source rule;
            # an explicit conflicting relation remains rejected above.
            matching = [row for row, source_fact in local_evidence
                if _same_policy_duration(source_fact, fact) and
                tuple(source_fact.get("subject") or ()) == subject and
                source_fact.get("start_event", ()) == fact.get("start_event", ()) and
                source_fact.get("condition", ()) == fact.get("condition", ())]
            if not matching:
                # A bare numeric summary may be linked only to a single exact
                # cited evidence ID immediately attached to that number. Its
                # relation remains unspecified; explicit relation mismatches
                # are never normalized away.
                anchored_ids = tuple(fact.get("_cited_source_ids") or ())
                if len(anchored_ids) == 1 and anchored_ids[0] in cited:
                    anchored_row = cited[anchored_ids[0]]
                    anchored_facts = [source_fact for source_fact in
                        _source_window_facts(anchored_row)
                        if _same_policy_duration(source_fact, fact) and
                        source_fact.get("start_event", ()) == fact.get("start_event", ()) and
                        source_fact.get("condition", ()) == fact.get("condition", ())]
                    if len(anchored_facts) == 1:
                        matching = [(anchored_row, anchored_facts[0])]
        if not matching:
            same_amount_unit = [source for _row, source in evidence_windows
                if _same_policy_duration(source, fact)]
            code = "ANSWER_UNSUPPORTED_POLICY_WINDOW"
            basis = "cited policy does not support the same amount, unit, relation, subject, and condition"
            if same_amount_unit and any(source["relation"] != fact["relation"] for source in same_amount_unit):
                missing_origin = any(source.get('start_event') and
                    source.get('start_event') != fact.get('start_event') for source in same_amount_unit)
                if missing_origin:
                    basis = 'the asserted duration omits or changes the explicit source clock origin'
                elif fact['relation']=='UNSCOPED' or any(source['relation']=='UNSCOPED' for source in same_amount_unit):
                    code = 'ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED'
                    basis = 'the duration is present, but a local relation is not reliably parsed'
                else:
                    basis = "the duration is attached to a different policy relation"
            elif same_amount_unit and subject and all(source.get("subject") and
                    tuple(subject) != tuple(source.get("subject") or ()) for source in same_amount_unit):
                known_objects={canonical for _pattern,canonical in _POLICY_SUBJECT_ALIASES}
                answer_objects=set(subject)&known_objects
                definitely_different=bool(answer_objects) and all(
                    bool(set(source['subject'])&known_objects) and
                    not answer_objects.intersection(source['subject']) for source in same_amount_unit)
                if definitely_different:
                    basis = "the cited duration belongs to a different subject"
                else:
                    code = 'ANSWER_POLICY_WINDOW_SCOPE_UNRESOLVED'
                    basis = 'the duration is present, but differing local subject tokens do not prove a different object'
            elif same_amount_unit and any(source.get("condition", ()) != fact.get("condition", ())
                    for source in same_amount_unit):
                basis = "the cited duration has a different condition"
            issues.append({"code": code, "window": f"{fact['amount']}{fact.get('raw_unit', fact['unit'])}", "basis": basis})

    prerequisites = [item for row in cited.values() for item in _eligibility_requirements(row.get("content") or "")]
    for item in _eligibility_requirements(answer):
        if prerequisites and item not in prerequisites:
            issues.append({"code": "ANSWER_UNSUPPORTED_ELIGIBILITY_CONDITIONS",
                "basis": "the asserted prerequisite list differs from the cited policy"})

    # Conflicting cited expiry statements must be reported as a conflict. A
    # response cannot silently elevate one source over another without an
    # explicit authority rule in the evidence.
    expiry_facts = [(row.get("chunk_id"), fact) for row in cited.values()
        for fact in _policy_expiry_facts(row.get("content") or "")]
    for index, (left_id, left) in enumerate(expiry_facts):
        for right_id, right in expiry_facts[index + 1:]:
            if left_id == right_id or left.get("subject") != right.get("subject"):
                continue
            conflict = ((left["kind"] == "NO_EXPIRY" and right["kind"] == "EXPIRES_AFTER") or
                        (right["kind"] == "NO_EXPIRY" and left["kind"] == "EXPIRES_AFTER"))
            if not conflict:
                continue
            answer_expiry = _policy_expiry_facts(answer)
            answer_kinds = set()
            for fact in answer_expiry:
                answer_subject = tuple(fact.get("subject") or ())
                if answer_subject == tuple(left.get("subject") or ()):
                    answer_kinds.add(fact["kind"])
                elif (not answer_subject and left.get("subject") == right.get("subject") and
                      set(left.get("subject") or ()) & question_tokens):
                    # A subject may be omitted only when the question and both
                    # conflicting source facts independently establish one
                    # identical scope.
                    answer_kinds.add(fact["kind"])
            text_lower = (answer or "").casefold()
            priority_claim = _has_unsupported_policy_priority_claim(text_lower)
            conflict_marked = _answer_marks_policy_conflict(answer)
            if priority_claim:
                issues.append({"code": "ANSWER_UNSUPPORTED_POLICY_PRIORITY",
                    "evidence_ids": [left_id, right_id],
                    "basis": "no cited evidence establishes precedence between conflicting policies"})
            elif not (conflict_marked and {"NO_EXPIRY", "EXPIRES_AFTER"}.issubset(answer_kinds)):
                issues.append({"code": "ANSWER_UNACKNOWLEDGED_POLICY_CONFLICT",
                    "evidence_ids": [left_id, right_id],
                    "basis": "cited policy sources conflict but the answer does not state both positions and preserve the conflict"})
    # Conflicting time windows with the same subject, conditions, and effective
    # interval do not establish authority by themselves. This applies to any
    # policy window; it is not tied to a case ID or a particular return period.
    cited_rows = list(cited.values())
    for index, left_row in enumerate(cited_rows):
        for right_row in cited_rows[index + 1:]:
            same_interval = (left_row.get("effective_from") == right_row.get("effective_from") and
                left_row.get("effective_to") == right_row.get("effective_to") and
                left_row.get("effective_from") is not None)
            if not same_interval:
                continue
            left_windows = _policy_window_facts(left_row.get("content") or "")
            right_windows = _policy_window_facts(right_row.get("content") or "")
            conflict = any(
                tuple(left.get("subject") or ()) == tuple(right.get("subject") or ()) and
                tuple(left.get("condition") or ()) == tuple(right.get("condition") or ()) and
                (left.get("amount"), left.get("unit"), left.get("relation")) !=
                    (right.get("amount"), right.get("unit"), right.get("relation"))
                for left in left_windows for right in right_windows)
            if not conflict:
                continue
            evidence_text = " ".join(str(row.get("content") or "") for row in (left_row, right_row))
            explicit_authority = bool(re.search(
                r"supersed(?:es|ed|ing)?|replaces?\s+(?:the\s+)?(?:prior|previous|older)|"
                r"takes?\s+precedence|overrides?|取代|优先于|以.{0,24}为准",
                evidence_text, re.I))
            if not explicit_authority and _has_unsupported_policy_priority_claim(answer):
                issues.append({"code": "ANSWER_UNSUPPORTED_POLICY_PRIORITY",
                    "evidence_ids": [left_row.get("chunk_id"), right_row.get("chunk_id")],
                    "basis": "cited policy windows conflict over the same effective interval and establish no authority"})
            elif _claims_order_date_selects_policy(answer):
                issues.append({"code": "ANSWER_UNSUPPORTED_POLICY_PRIORITY",
                    "evidence_ids": [left_row.get("chunk_id"), right_row.get("chunk_id")],
                    "basis": "order date does not establish precedence between conflicting policies with the same effective interval"})
    # Distinct channel clocks may both be true. A later universal summary must
    # not erase that distinction even when it avoids the word “priority”.
    if (re.search(r'独立渠道|不同渠道', ' '.join(row.get('content') or '' for row in cited.values()))
            and re.search(r'(?:通用答案|统一起算点|一律从|所有渠道均从)',answer)):
        events={fact.get('start_event') for row in cited.values()
                for fact in _source_window_facts(row) if fact.get('start_event')}
        # Some clauses state the clock without repeating a duration.
        clocks=' '.join(row.get('content') or '' for row in cited.values())
        if len(events)>1 or (re.search(r'购买日期',clocks) and re.search(r'登记事件起算',clocks)):
            issues.append({'code':'ANSWER_POLICY_CHANNEL_SCOPE_UNRESOLVED',
                'basis':'a universal clock summary erases distinct channel-scoped rules'})
    return issues


def _has_unsupported_policy_priority_claim(text: str) -> bool:
    """Find affirmative priority claims, excluding locally negated/unknown ones."""
    priority = re.compile(
        r"优先(?:适用|执行|级)?|以.{0,24}为准|权威(?:性|版本|来源)?|"
        r"authorit(?:y|ative)(?:\s+(?:version|source))?|takes?\s+precedence|"
        r"overrides?|prevails?|has\s+priority|controls?", re.I)
    negative_prefix = re.compile(
        r"(?:无法|不能|不应|不宜|不可以|不确定|未能|尚不能|不足以(?:判定|确认|确定|认定|说明)?|cannot|can't|unable\s+to|unclear).{0,28}$|"
        r"(?:请|请补充|需要|请求|询问|ask|please|request).{0,28}$|"
        r"(?:未提供|未说明|未规定|未明确|未指定|未标明|未确立|未建立|未记载|未给出|"
        r"没有提供|没有给出|没有说明|没有规定|没有明确|没有依据|尚无依据|缺少|缺乏|不存在|并无).{0,18}$|"
        r"\b(?:no|not|never|without)\s*$", re.I)
    negative_suffix = re.compile(
        r"^\s*(?:未知|不明|未(?:提供|说明|规定|明确|指定|确立|建立|记载|给出)|"
        r"is\s+not\s+(?:provided|specified|established|stated|defined)|"
        r"(?:not|never)\s+(?:provided|specified|established|stated|defined)|"
        r"unknown|unspecified|unclear)\b", re.I)
    # Keep source-label phrases such as "rule A" intact (they can be the
    # object of an affirmative claim), while splitting sentence and contrast
    # boundaries so a preceding negative assertion cannot mask what follows.
    sentence_parts = re.split(r"(?<=[。！？!?;；])|[\r\n]+", text or "")
    clauses = []
    for sentence in sentence_parts:
        clauses.extend(re.split(
            r"(?:(?<=[,，。；;\s])(?:而|但是|然而|但|不过)(?=\s|[\u3400-\u9fff])|"
            r"\b(?:whereas|while|but)\b)", sentence, flags=re.I))
    for clause in clauses:
        for match in priority.finditer(clause):
            prefix = clause[max(0, match.start() - 64):match.start()]
            suffix = clause[match.end():match.end() + 36]
            if negative_prefix.search(prefix) or negative_suffix.search(suffix):
                continue
            if re.search(r"\bno\s+(?:(?:version|source|policy|rule)\s+){0,2}(?:is\s+)?$",
                    prefix, re.I):
                continue
            return True
    return False


def _claims_order_date_selects_policy(text: str) -> bool:
    for clause in re.split(r"[。！？!?；;]", text or ""):
        if re.search(r"无法|不能|不应|不可以|不能据|不代表|cannot|can't|should not|does not|not enough|cannot determine", clause, re.I):
            continue
        has_order_date = bool(re.search(r"下单(?:时间|日期)|订单日期|下单时|order\s+date|date\s+of\s+(?:the\s+)?order", clause, re.I))
        selects_policy = bool(re.search(r"(?:因此|所以|据此|按此|适用|决定|确定|选择|authoritative|controls?|selects?|determines?)", clause, re.I))
        has_version = bool(re.search(r"版本|政策|规则|来源|version|policy|rule|source", clause, re.I))
        if has_order_date and selects_policy and has_version:
            return True
    return False


def _states_approval_prerequisite_denial(clause: str) -> bool:
    """Recognize a conditional denial that follows from an approval prerequisite.

    A statement such as “未获批准的退货申请不能作为退款申请依据” is not the
    reverse of a rule requiring approval before refund application. The older
    phase-only polarity check treated both as an unconditional negative claim.
    This helper only filters that conditional form; an unconditional reversal
    such as “无需获批也可以申请退款” remains subject to the mismatch check.
    """
    return bool(re.search(
        r"(?:未|尚未|没有)(?:获得?|获批|批准|通过).{0,20}"
        r"(?:不能|不可|无法|不符合|不满足).{0,24}"
        r"(?:作为.{0,6}(?:退款申请|申请退款).{0,6}(?:依据|条件)|"
        r"(?:申请退款|退款申请).{0,8}(?:依据|条件))",
        clause))


class RequestedAspect(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspect_id: str = Field(min_length=1, max_length=64)
    text: str = Field(min_length=1, max_length=300)
    # Optional only for reading pre-v3 historical payloads. Live extraction and
    # Grade mapping enforce a non-empty exact question substring.
    query_anchor: str | None = Field(default=None, max_length=160)


class RequiredRuleAssignment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rule_id: str = Field(min_length=1, max_length=64)
    basis: Literal["DIRECTLY_ASKED", "POLICY_DEPENDENCY"]
    query_anchor: str = Field(min_length=1, max_length=160)
    rationale: str = Field(min_length=1, max_length=300)
    depends_on_rule_id: str | None = Field(default=None, max_length=64)


class AspectRuleMapping(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aspect_id: str = Field(min_length=1, max_length=64)
    required_rules: list[RequiredRuleAssignment] = Field(default_factory=list, max_length=12)
    supporting_rule_ids: list[str] = Field(default_factory=list, max_length=12)


class ConditionsResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_mode: Literal["POLICY_EXPLANATION", "ELIGIBILITY_DECISION", "ACTION_EXECUTION"]
    analysis_version: Literal["legacy_preconditions", "request_intent_v1"] = "legacy_preconditions"
    requested_aspects: list[RequestedAspect] = Field(default_factory=list, max_length=12)
    conditions: list[Condition] = Field(default_factory=list, max_length=12)

    @model_validator(mode="after")
    def unique_ids(self):
        ids = [condition.condition_id for condition in self.conditions]
        if len(ids) != len(set(ids)):
            raise ValueError("condition_id values must be unique")
        if any(dependency not in ids or dependency == condition.condition_id
               for condition in self.conditions for dependency in condition.depends_on):
            raise ValueError("condition dependencies must reference another existing condition")
        aspect_ids = [aspect.aspect_id for aspect in self.requested_aspects]
        if len(aspect_ids) != len(set(aspect_ids)):
            raise ValueError("requested aspect IDs must be unique")
        if self.analysis_version == "request_intent_v1":
            if any(condition.kind not in {"USER_PROVIDED_FACT", "MISSING_INFORMATION", "UNKNOWN"}
                   for condition in self.conditions):
                raise ValueError("pre-retrieval analysis cannot create policy or business-tool facts")
            if any(condition.required for condition in self.conditions
                   if condition.kind in {"USER_PROVIDED_FACT", "UNKNOWN"}):
                raise ValueError("user-provided and unresolved context facts cannot be required evidence")
            if self.request_mode == "POLICY_EXPLANATION" and any(
                    condition.kind == "MISSING_INFORMATION" and condition.required
                    for condition in self.conditions):
                raise ValueError("policy explanation cannot require missing personal order information")
            if any(condition.kind == "MISSING_INFORMATION" and condition.source_type != "UNKNOWN"
                   for condition in self.conditions):
                raise ValueError("missing information must have UNKNOWN source")
        if self.analysis_version == "request_intent_v1" and not self.requested_aspects:
            raise ValueError("request intent must identify at least one requested aspect")
        return self


class GradeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relevant: bool
    sufficient: bool
    missing_aspect: list[str] = Field(default_factory=list, max_length=12)
    rationale: str = Field(min_length=1, max_length=1000)


class EvidenceJudgment(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str
    evidence_id: str
    judgment: Literal["supports", "refutes", "unknown"]


class GradePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    grade: GradeResult
    judgments: list[EvidenceJudgment] = Field(default_factory=list, max_length=120)
    aspect_rule_map: list[AspectRuleMapping] = Field(default_factory=list, max_length=12)
    proposed_conclusion: Literal["ALLOW", "DENY", "EXPLAIN", "CONFLICT", "UNKNOWN"] = "UNKNOWN"


def _upgrade_historical_grade_payload_for_replay(payload: dict) -> tuple[dict, list[dict]]:
    """Read the retired broad `rule_ids` format as relevance only, never as necessity.

    This adapter is for offline historical replay. Live provider validation remains
    strict and rejects the retired field, preserving the original response separately.
    """
    upgraded = dict(payload)
    mappings = []
    audit = []
    for item in payload.get("aspect_rule_map", []):
        if not isinstance(item, dict):
            mappings.append(item)
            continue
        mapped = dict(item)
        legacy = mapped.pop("rule_ids", None)
        if legacy is not None:
            if mapped.get("required_rules"):
                raise ValueError("historical payload mixes retired rule_ids with current required_rules")
            supporting = list(mapped.get("supporting_rule_ids") or [])
            supporting.extend(value for value in legacy if value not in supporting)
            mapped["supporting_rule_ids"] = supporting
            audit.append({"aspect_id": mapped.get("aspect_id"),
                          "legacy_rule_ids_count": len(legacy),
                          "decision": "REPLAY_AS_SUPPORTING_ONLY",
                          "basis": "retired schema did not distinguish relevance from necessity"})
        mappings.append(mapped)
    upgraded["aspect_rule_map"] = mappings
    return upgraded, audit


class GradeValidationError(ValueError):
    """A structurally valid grade that violates evidence/source invariants."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class ConditionCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    condition_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    supports: list[str] = Field(default_factory=list)
    refutes: list[str] = Field(default_factory=list)
    unknown: list[str] = Field(default_factory=list)
    applicable_version: list[str] = Field(default_factory=list)


StopReason = Literal[
    "SUFFICIENT_EVIDENCE", "SUFFICIENT_REFUTATION", "MAX_HOPS_REACHED",
    "NO_NEW_EVIDENCE", "UNKNOWN_GRADE", "BUDGET_EXCEEDED",
    "RETRIEVAL_UNAVAILABLE", "USER_CLARIFICATION_REQUIRED", "CONDITION_EXTRACTION_FAILED",
    "GENERATION_UNAVAILABLE",
]


@dataclass(frozen=True)
class SelfRAGConfig:
    max_hops: int = 3
    max_queries: int = 6
    max_queries_per_hop: int = 2
    max_model_requests: int = 12
    max_total_tokens: int = 12288
    max_cost_usd: float = 0.10
    max_wall_seconds: float = 105.0

    def __post_init__(self):
        if not 1 <= self.max_hops <= 3:
            raise ValueError("max_hops must be between 1 and 3")
        if not 1 <= self.max_queries_per_hop <= 2 or self.max_queries < self.max_hops:
            raise ValueError("query limits exceed the M2.3 contract")
        if min(self.max_model_requests, self.max_total_tokens) < 1 or self.max_cost_usd <= 0 or self.max_wall_seconds <= 0:
            raise ValueError("Self-RAG budgets must be positive")


def self_rag_config_from_environment(environ=None) -> SelfRAGConfig:
    import os
    env = os.environ if environ is None else environ
    defaults = SelfRAGConfig()
    return SelfRAGConfig(
        max_hops=int(env.get("SHOPRELAY_MAX_RETRIEVAL_HOPS", defaults.max_hops)),
        max_queries=int(env.get("SHOPRELAY_MAX_RETRIEVAL_QUERIES", defaults.max_queries)),
        max_queries_per_hop=int(env.get("SHOPRELAY_MAX_RETRIEVAL_QUERIES_PER_HOP", defaults.max_queries_per_hop)),
        max_model_requests=int(env.get("SHOPRELAY_MAX_MODEL_REQUESTS", defaults.max_model_requests)),
        max_total_tokens=int(env.get("SHOPRELAY_MAX_MODEL_TOKENS_PER_CHAT", defaults.max_total_tokens)),
        max_cost_usd=float(env.get("SHOPRELAY_MAX_ESTIMATED_COST_USD_PER_CHAT", defaults.max_cost_usd)),
        max_wall_seconds=float(env.get("SHOPRELAY_MAX_CHAT_WALL_SECONDS", defaults.max_wall_seconds)),
    )


def _cost_for_usage(usage: dict | None) -> float | None:
    if not isinstance(usage, dict):
        return None
    try:
        input_tokens = int(usage["prompt_tokens"])
        output_tokens = int(usage["completion_tokens"])
    except (KeyError, TypeError, ValueError):
        return None
    details = usage.get("prompt_tokens_details") or {}
    cache_hit = min(input_tokens, int(details.get("cached_tokens") or details.get("cache_hit_tokens") or
                                        usage.get("prompt_cache_hit_tokens") or usage.get("cache_hit_tokens") or 0))
    return round(((input_tokens - cache_hit) * .30 + cache_hit * .006 + output_tokens * 1.20) / 1_000_000, 9)


@dataclass
class BudgetLedger:
    config: SelfRAGConfig
    started: float = field(default_factory=time.monotonic)
    requests: int = 0
    queries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    usage_unknown: bool = False
    calls: list[dict] = field(default_factory=list)

    def can_query(self) -> bool:
        return (self.queries < self.config.max_queries and
                time.monotonic() - self.started < self.config.max_wall_seconds)

    def before_model_call(self, node, input_text: str) -> tuple[bool, str | None, float]:
        estimated_input = max(1, len(input_text.encode("utf-8")) // 2)
        estimated_cost = _cost_for_usage({"prompt_tokens": estimated_input, "completion_tokens": node.max_tokens}) or 0.0
        if self.requests >= self.config.max_model_requests:
            return False, "model_request_limit", estimated_cost
        if self.input_tokens + self.output_tokens + estimated_input + node.max_tokens > self.config.max_total_tokens:
            return False, "token_limit", estimated_cost
        if estimated_cost > node.max_cost_usd:
            return False, "node_cost_limit", estimated_cost
        if self.cost_usd + estimated_cost > self.config.max_cost_usd:
            return False, "chat_cost_limit", estimated_cost
        if time.monotonic() - self.started + node.timeout_seconds > self.config.max_wall_seconds:
            return False, "wall_clock_limit", estimated_cost
        if self.usage_unknown:
            return False, "previous_usage_unknown", estimated_cost
        self.requests += 1
        return True, None, estimated_cost

    def record_call(self, node: str, response: dict, fallback_cost: float) -> None:
        usage = response.get("usage") if isinstance(response, dict) else None
        try:
            input_tokens = int(usage["prompt_tokens"])
            output_tokens = int(usage["completion_tokens"])
            cost = response.get("estimated_cost_usd")
            if cost is None:
                cost = _cost_for_usage(usage)
            if cost is None:
                raise ValueError("missing usage cost")
            cost = float(cost)
            self.input_tokens += input_tokens
            self.output_tokens += output_tokens
        except (TypeError, ValueError, KeyError):
            input_tokens = output_tokens = None
            cost = fallback_cost
            self.usage_unknown = True
        latency = int(response.get("latency_ms") or 0) if isinstance(response, dict) else 0
        self.latency_ms += latency
        self.cost_usd += cost
        self.calls.append({"node": node, "model_id": response.get("model_id"),
                           "status": "COMPLETED" if response.get("usage") else "USAGE_UNKNOWN",
                           "prompt_tokens": input_tokens, "completion_tokens": output_tokens,
                           "latency_ms": latency, "estimated_cost_usd": cost,
                           "finish_reason": response.get("finish_reason"),
                           "prompt_version": response.get("prompt_version"),
                           "thinking": response.get("thinking"),
                           "max_tokens": response.get("max_tokens"),
                           "timeout_seconds": response.get("timeout_seconds"),
                           "cost_is_estimated": True})

    def record_failed_call(self, node: str, estimated_cost: float, error_code: str,
                           diagnostic: dict | None = None) -> None:
        usage = (diagnostic or {}).get("usage") if isinstance(diagnostic, dict) else None
        actual_estimate = _cost_for_usage(usage) if isinstance(usage, dict) else None
        cost = actual_estimate if actual_estimate is not None else estimated_cost
        latency = int((diagnostic or {}).get("latency_ms") or 0) if isinstance(diagnostic, dict) else 0
        if actual_estimate is not None:
            self.input_tokens += int(usage.get("prompt_tokens", 0))
            self.output_tokens += int(usage.get("completion_tokens", 0))
        else:
            self.usage_unknown = True
        self.cost_usd += cost
        self.latency_ms += latency
        self.calls.append({"node": node, "model_id": None,
                           "status": "FAILED" if actual_estimate is not None else "FAILED_USAGE_UNKNOWN",
                           "prompt_tokens": int(usage.get("prompt_tokens", 0)) if isinstance(usage, dict) and actual_estimate is not None else None,
                           "completion_tokens": int(usage.get("completion_tokens", 0)) if isinstance(usage, dict) and actual_estimate is not None else None,
                           "latency_ms": latency, "estimated_cost_usd": cost, "error_code": error_code,
                           "finish_reason": (diagnostic or {}).get("finish_reason") if isinstance(diagnostic, dict) else None,
                           "cost_is_estimated": True})

    def as_dict(self) -> dict:
        return {"limits": {"max_model_requests": self.config.max_model_requests,
                            "max_total_tokens": self.config.max_total_tokens,
                            "max_retrieval_hops": self.config.max_hops,
                            "max_retrieval_queries": self.config.max_queries,
                            "max_retrieval_queries_per_hop": self.config.max_queries_per_hop,
                            "max_estimated_cost_usd": self.config.max_cost_usd,
                            "max_wall_seconds": self.config.max_wall_seconds},
                "model_requests": self.requests, "retrieval_queries": self.queries,
                "input_tokens": self.input_tokens if not self.usage_unknown else None,
                "output_tokens": self.output_tokens if not self.usage_unknown else None,
                "tokens_unknown": self.usage_unknown, "estimated_cost_usd": round(self.cost_usd, 9),
                "cost_basis": "DeepSeek peak-rate estimate using returned usage; failed/missing usage uses conservative preflight estimate; not an invoice",
                "price_source": "https://api-docs.deepseek.com/quick_start/pricing/",
                "latency_ms": self.latency_ms, "calls": self.calls,
                "elapsed_ms": round((time.monotonic() - self.started) * 1000)}


def _valid_at(evidence: dict, at: str) -> bool:
    try:
        instant = datetime.fromisoformat(at.replace("Z", "+00:00")).astimezone(timezone.utc)
        start = datetime.fromisoformat(evidence["effective_from"].replace("Z", "+00:00")).astimezone(timezone.utc)
        end_text = evidence.get("effective_to")
        end = datetime.fromisoformat(end_text.replace("Z", "+00:00")).astimezone(timezone.utc) if end_text else None
        return start <= instant and (end is None or instant < end)
    except (KeyError, TypeError, ValueError):
        return False


class MultiHopRetrievalService:
    """Three-hop Self-RAG with validated condition coverage and bounded model/query use."""
    PROMPT_VERSION = "m2.4-q3-required-scope-v1"

    def __init__(self, retrieval, provider, *, config: SelfRAGConfig | None = None,
                 on_hop: Callable[[dict], None] | None = None,
                 on_trace: Callable[[str, dict], None] | None = None):
        self.retrieval = retrieval
        self.provider = provider
        self.config = config or self_rag_config_from_environment()
        self.on_hop = on_hop
        self.on_trace = on_trace

    def _node(self, name: str):
        settings = getattr(self.provider, "settings", None)
        if settings is None:
            settings = load_settings(require_api_key=False)
        return settings.nodes[name]

    def _clarify(self, missing: list[str], reason: str) -> str:
        if reason == "CONDITION_EXTRACTION_FAILED":
            return ("我暂时无法可靠解析您要询问的条件，因此没有判断具体订单是否符合资格，也没有执行任何操作。"
                    "请换一种说法说明您想了解政策规则，还是核实某笔订单；具体订单状态需以授权业务查询为准。")
        details = "；".join(missing[:4])
        if details:
            return f"目前无法核实全部必要条件（{details}），因此不能给出确定政策结论。请补充相关信息，或转人工核实。"
        return "目前无法确认该政策结论，未检索到足以覆盖全部必要条件的有效依据。请补充信息或转人工核实。"

    def run(self, question: str, *, mode: str | None = None,
            route_usage: dict | None = None, route_model_id: str | None = None,
            route_latency_ms: int = 0, route_error_code: str | None = None,
            evaluation_at: str | None = None) -> dict:
        ledger = BudgetLedger(self.config)
        if route_error_code:
            ledger.requests = 1
            ledger.record_failed_call("route", .01, route_error_code)
        elif route_usage is not None or route_model_id is not None:
            route_call = {"usage": route_usage, "model_id": route_model_id, "latency_ms": route_latency_ms}
            ledger.requests = 1
            ledger.record_call("route", route_call, 0.0)
        view: RetrievalView = self.retrieval.pin_view(evaluation_at)
        condition_node = self._node("condition_extraction")
        extraction_input = question
        allowed, limit_reason, estimate = ledger.before_model_call(condition_node, extraction_input)
        if not allowed:
            return self._finish(question, view, [], [], {}, None, "BUDGET_EXCEEDED", [], ledger, limit_reason)
        try:
            extracted = self.provider.extract_conditions(question)
            ledger.record_call("condition_extraction", extracted, estimate)
            condition_result = ConditionsResult.model_validate(extracted.get("conditions_result", extracted))
            raw_conditions = (extracted.get("result") or extracted.get("conditions_result") or {}).get("conditions", [])
            condition_normalizations = []
            for item in condition_result.conditions:
                if not item.policy_stages:
                    item.policy_stages = sorted(_policy_stages(item.text),
                                                key=lambda stage: _PHASE_ORDER[stage])
            for raw, normalized in zip(raw_conditions, condition_result.conditions):
                for field_name in ("source_type", "depends_on", "policy_stages"):
                    normalized_value = getattr(normalized, field_name)
                    raw_value = raw.get(field_name)
                    if (field_name not in raw or raw_value != normalized_value) and normalized_value not in (None, []):
                        condition_normalizations.append({"condition_id": normalized.condition_id,
                            "field": field_name, "from": raw_value if field_name in raw else "omitted",
                            "to": normalized_value,
                            "basis": "canonical source derived from validated condition kind" if field_name == "source_type"
                                     else ("schema default empty dependency list" if field_name == "depends_on"
                                           else "normalized from lifecycle stages explicitly stated in condition text")})
            if any(condition.kind == "USER_PROVIDED_FACT" and
                   (not condition.source_excerpt or condition.source_excerpt not in question)
                   for condition in condition_result.conditions):
                raise ValueError("user fact source excerpt is not present in original query")
        except Exception as exc:
            code = getattr(exc, "code", None) or type(exc).__name__
            if ledger.requests > len(ledger.calls):
                ledger.record_failed_call("condition_extraction", estimate, code)
            result = self._finish(question, view, [], [], {}, None, "CONDITION_EXTRACTION_FAILED", [], ledger, code)
            diagnostic = getattr(exc, "diagnostic", None)
            if isinstance(diagnostic, dict):
                result["condition_extraction_error"] = {key: diagnostic[key] for key in
                    ("node", "provider_exception", "validation_errors", "raw_content", "finish_reason",
                     "usage", "latency_ms", "thinking", "max_tokens", "timeout_seconds") if key in diagnostic}
                result["condition_extraction_raw_output"] = diagnostic.get("raw_content")
            return result

        conditions = condition_result.conditions
        request_mode = condition_result.request_mode
        request_intent_v1 = condition_result.analysis_version == "request_intent_v1"
        requested_aspects = condition_result.requested_aspects
        condition_semantic_issues = ([] if request_intent_v1 else
                                     _condition_semantic_issues(question, request_mode, conditions))
        if condition_semantic_issues:
            result = self._finish(question, view, [item.model_dump() for item in conditions], [], {}, None,
                                  "UNKNOWN_GRADE", [], ledger, "condition semantic validation failed",
                                  [item.text for item in conditions if item.required and item.kind != "USER_PROVIDED_FACT"])
            result["request_mode"] = request_mode
            result["condition_extraction_raw_output"] = extracted.get("raw_content")
            result["condition_normalizations"] = condition_normalizations
            result["condition_semantic_audit"] = {"status": "REJECTED", "issues": condition_semantic_issues}
            return result
        condition_semantic_audit = {"status": "PASS", "issues": []}
        if not request_intent_v1 and request_mode == "POLICY_EXPLANATION" and any(
                item.required and item.kind in {"MISSING_INFORMATION", "BUSINESS_TOOL_FACT"}
                for item in conditions):
            missing = [item.text for item in conditions if item.required and
                       item.kind in {"MISSING_INFORMATION", "BUSINESS_TOOL_FACT"}]
            result = self._finish(question, view, [item.model_dump() for item in conditions], [], {}, None,
                                  "USER_CLARIFICATION_REQUIRED", [], ledger,
                                  "policy explanation cannot require unprovided order/tool facts", missing)
            result["request_mode"] = request_mode
            result["condition_extraction_raw_output"] = extracted.get("raw_content")
            result["condition_normalizations"] = condition_normalizations
            return result
        if not request_intent_v1 and request_mode == "ELIGIBILITY_DECISION" and any(
                item.required and item.kind in {"MISSING_INFORMATION", "BUSINESS_TOOL_FACT"}
                for item in conditions):
            missing = [item.text for item in conditions if item.required and
                       item.kind in {"MISSING_INFORMATION", "BUSINESS_TOOL_FACT"}]
            result = self._finish(question, view, [item.model_dump() for item in conditions], [], {}, None,
                                  "USER_CLARIFICATION_REQUIRED", [], ledger,
                                  "M2.3 cannot verify required live order facts", missing)
            result["request_mode"] = request_mode
            result["condition_extraction_raw_output"] = extracted.get("raw_content")
            result["condition_normalizations"] = condition_normalizations
            return result
        if request_mode == "ACTION_EXECUTION":
            result = self._finish(question, view, [item.model_dump() for item in conditions], [], {}, None,
                                  "USER_CLARIFICATION_REQUIRED", [], ledger,
                                  "business write execution is not available in M2.3")
            result["request_mode"] = request_mode
            result["condition_extraction_raw_output"] = extracted.get("raw_content")
            result["condition_normalizations"] = condition_normalizations
            return result
        condition_ids = {condition.condition_id for condition in conditions}
        evidence_by_id: dict[str, dict] = {}
        seen_queries: set[str] = set()
        previous_hop_ids: set[str] | None = None
        hops: list[dict] = []
        grade_payload: GradePayload | None = None
        grade_error_detail = None
        coverage: dict[str, ConditionCoverage] = {
            condition.condition_id: ConditionCoverage(condition_id=condition.condition_id)
            for condition in conditions
        }
        final_reason: StopReason = "MAX_HOPS_REACHED"
        final_missing = [condition.text for condition in conditions if condition.required]
        proposed_conclusion = "UNKNOWN"
        grade_identifier_normalizations = []
        grade_payload_sanitizations = []
        condition_scope_audit = []
        sufficiency_audit = []

        for hop_number in range(1, self.config.max_hops + 1):
            if hop_number == 1:
                queries = [question]
            else:
                missing_ids = set()
                if grade_payload is not None:
                    missing_ids = set(grade_payload.grade.missing_aspect)
                    if not missing_ids:
                        missing_ids = {item.condition_id for item in coverage.values()
                                       if not (item.supports or item.refutes)}
                else:
                    missing_ids = {condition.condition_id for condition in conditions
                                   if condition.required and condition.kind != "USER_PROVIDED_FACT"}
                targets = [condition for condition in conditions if condition.condition_id in missing_ids and
                           condition.kind in {"POLICY_RULE", "POLICY_ASPECT", "MISSING_INFORMATION"}]
                if any(condition.kind == "MISSING_INFORMATION" for condition in targets):
                    final_reason = "USER_CLARIFICATION_REQUIRED"
                    final_missing = [condition.text for condition in targets]
                    break
                # One focused query can cover up to two outstanding rules. A later
                # hop narrows to the still-missing subset, producing a new query.
                focused = targets[:self.config.max_queries_per_hop]
                queries = [f"{question}\n只核查这些必要政策条件：" + "；".join(item.text for item in focused)] if focused else []
            normalized_queries = []
            for query in queries[:self.config.max_queries_per_hop]:
                normalized = re.sub(r"\s+", " ", query).strip().casefold()
                if normalized and normalized not in seen_queries:
                    seen_queries.add(normalized)
                    normalized_queries.append(query)
            if not normalized_queries:
                final_reason = ("UNKNOWN_GRADE" if hops and hops[-1].get("grade_status") == "UNKNOWN_GRADE"
                                else "NO_NEW_EVIDENCE")
                break
            if len(normalized_queries) > self.config.max_queries_per_hop:
                normalized_queries = normalized_queries[:self.config.max_queries_per_hop]

            previous_ids = set(evidence_by_id)
            query_records = []
            retrieval_failures = []
            for query in normalized_queries:
                if not ledger.can_query():
                    final_reason = "BUDGET_EXCEEDED"
                    break
                ledger.queries += 1
                try:
                    found = self.retrieval.retrieve(query, mode=mode, view=view)
                    if found.get("index_fingerprint") != view.fingerprint or found.get("knowledge_version") != view.index["knowledge_version"]:
                        raise RetrievalUnavailable("KNOWLEDGE_VIEW_CHANGED", "retrieval result did not match pinned snapshot")
                    if found.get("mode") and mode and found["mode"] != mode:
                        raise RetrievalUnavailable("RETRIEVAL_MODE_CHANGED", "retrieval mode changed during run")
                    for row in found.get("evidence", []):
                        if row.get("index_fingerprint") != view.fingerprint or not _valid_at(row, view.evaluation_at):
                            continue
                        evidence_by_id.setdefault(row["chunk_id"], row)
                    query_records.append({"query": query, "evidence_ids": [row["chunk_id"] for row in found.get("evidence", [])
                                                                            if row.get("index_fingerprint") == view.fingerprint and _valid_at(row, view.evaluation_at)],
                                          "mode": found.get("mode"), "latency_ms": found.get("latency_ms", 0),
                                          "degraded": found.get("degraded", False),
                                          "failures": found.get("failures", [])})
                except RetrievalUnavailable as exc:
                    retrieval_failures.append({"code": exc.code, "detail": exc.detail})
                    query_records.append({"query": query, "evidence_ids": [], "mode": mode,
                                          "latency_ms": 0, "degraded": True,
                                          "failures": [{"code": exc.code, "detail": exc.detail}]})
            if final_reason == "BUDGET_EXCEEDED":
                break
            hop_ids = set(evidence_by_id) - previous_ids
            current_ids = set(evidence_by_id)
            if retrieval_failures and not query_records or retrieval_failures and all(not row.get("evidence_ids") for row in query_records):
                # Allow an independently successful second query, but do not grade an all-failed hop.
                if all(row.get("failures") for row in query_records):
                    final_reason = "RETRIEVAL_UNAVAILABLE"
                    hops.append({"hop": hop_number, "queries": query_records, "new_evidence_ids": [],
                                 "evidence_ids": sorted(current_ids), "index_fingerprint": view.fingerprint,
                                 "knowledge_version": view.index["knowledge_version"], "grade_status": "NOT_RUN",
                                 "stop_reason": final_reason})
                    if self.on_hop:
                        self.on_hop(hops[-1])
                    break
            if previous_hop_ids is not None and current_ids == previous_hop_ids:
                final_reason = "NO_NEW_EVIDENCE"
                hops.append({"hop": hop_number, "queries": query_records, "new_evidence_ids": [],
                             "evidence_ids": sorted(current_ids), "index_fingerprint": view.fingerprint,
                             "knowledge_version": view.index["knowledge_version"], "grade_status": "NOT_RUN",
                             "stop_reason": final_reason})
                if self.on_hop:
                    self.on_hop(hops[-1])
                break
            previous_hop_ids = current_ids

            if request_intent_v1:
                policy_candidates = _atomic_policy_conditions(evidence_by_id)
                conditions = condition_result.conditions + policy_candidates + _aspect_placeholders(requested_aspects)
                coverage = {condition.condition_id: ConditionCoverage(condition_id=condition.condition_id)
                            for condition in conditions}
            evidence_semantic_issues = ([] if request_intent_v1 else _condition_semantic_issues(
                question, request_mode, conditions, evidence_by_id))
            if evidence_semantic_issues:
                condition_semantic_audit = {"status": "REJECTED", "issues": evidence_semantic_issues,
                                            "evidence_ids": sorted(evidence_by_id)}
                final_reason = "UNKNOWN_GRADE"
                grade_error_detail = {"code": "CONDITION_EVIDENCE_SEMANTIC_MISMATCH",
                                      "issues": evidence_semantic_issues}
                hops.append({"hop": hop_number, "queries": query_records,
                             "new_evidence_ids": sorted(hop_ids), "evidence_ids": sorted(current_ids),
                             "index_fingerprint": view.fingerprint,
                             "knowledge_version": view.index["knowledge_version"],
                             "grade_status": "SEMANTIC_REJECTED", "semantic_issues": evidence_semantic_issues,
                             "stop_reason": final_reason})
                if self.on_hop:
                    self.on_hop(hops[-1])
                break

            grade_node = self._node("evidence_sufficiency")
            # Avoid sending source excerpts and lifecycle metadata twice: policy
            # clauses already appear verbatim in the evidence list. This keeps a
            # later hop inside the existing run token budget without changing it.
            compact_conditions = [{key: value for key, value in item.model_dump().items()
                                   if key in {"condition_id", "text", "kind", "required", "source_type",
                                              "source_evidence_id", "rule_role", "depends_on"}}
                                  for item in conditions]
            grade_context = {"question": question, "conditions": compact_conditions,
                             "evidence": [{"chunk_id": item["chunk_id"], "version": item["version"],
                                           "effective_from": item["effective_from"], "effective_to": item.get("effective_to"),
                                           "content": item["content"]} for item in evidence_by_id.values()],
                             "evaluation_at": view.evaluation_at,
                             "requested_aspects": [item.model_dump() for item in requested_aspects]}
            allowed, limit_reason, estimate = ledger.before_model_call(grade_node, str(grade_context))
            if not allowed:
                final_reason = "BUDGET_EXCEEDED"
                hops.append({"hop": hop_number, "queries": query_records, "new_evidence_ids": sorted(hop_ids),
                             "evidence_ids": sorted(current_ids), "index_fingerprint": view.fingerprint,
                             "knowledge_version": view.index["knowledge_version"], "grade_status": "NOT_RUN",
                             "stop_reason": final_reason, "budget_reason": limit_reason})
                if self.on_hop:
                    self.on_hop(hops[-1])
                break
            graded = None
            grade_validation_stage = "provider"
            grade_raw_output = None
            grade_finish_reason = None
            hop_grade_normalizations = []
            try:
                grade_args = {"request_mode": request_mode}
                if request_intent_v1:
                    grade_args["requested_aspects"] = [item.model_dump() for item in requested_aspects]
                graded = self.provider.grade_evidence(question, [item.model_dump() for item in conditions],
                    list(evidence_by_id.values()), view.evaluation_at, **grade_args)
                ledger.record_call("evidence_sufficiency", graded, estimate)
                grade_raw_output = graded.get("raw_content")
                grade_finish_reason = graded.get("finish_reason")
                grade_validation_stage = "schema"
                grade_payload = GradePayload.model_validate(graded.get("grade_payload", graded))
                if request_intent_v1:
                    aspect_judgment_audit = _remove_policy_aspect_judgments(grade_payload, conditions)
                    grade_payload_sanitizations.extend({"hop": hop_number, **item}
                                                       for item in aspect_judgment_audit)
                    grade_validation_stage = "aspect_rule_map"
                    scope_decisions = _apply_aspect_rule_map(conditions, requested_aspects, grade_payload, question)
                    condition_scope_audit.extend({"hop": hop_number, **item} for item in scope_decisions)
                    hop_grade_normalizations = _canonicalize_grade_missing_aspect_ids(
                        grade_payload, conditions, requested_aspects)
                    grade_identifier_normalizations.extend({"hop": hop_number, **item}
                                                           for item in hop_grade_normalizations)
                    grade_validation_stage = "condition_semantics"
                    evidence_semantic_issues = _condition_semantic_issues(
                        question, request_mode, conditions, evidence_by_id)
                    if evidence_semantic_issues:
                        condition_semantic_audit = {"status": "REJECTED", "issues": evidence_semantic_issues,
                                                    "evidence_ids": sorted(evidence_by_id)}
                        grade_payload.grade.sufficient = False
                        final_reason = "UNKNOWN_GRADE"
                        grade_error_detail = {"code": "CONDITION_EVIDENCE_SEMANTIC_MISMATCH",
                                              "issues": evidence_semantic_issues}
                        hops.append({"hop": hop_number, "queries": query_records,
                                     "new_evidence_ids": sorted(hop_ids), "evidence_ids": sorted(current_ids),
                                     "index_fingerprint": view.fingerprint,
                                     "knowledge_version": view.index["knowledge_version"],
                                     "grade_status": "SEMANTIC_REJECTED", "semantic_issues": evidence_semantic_issues,
                                     "stop_reason": final_reason})
                        if self.on_hop:
                            self.on_hop(hops[-1])
                        break
                grade_validation_stage = "evidence_coverage"
                coverage, final_missing, coverage_ok, conflict = self._coverage(
                    conditions, grade_payload, evidence_by_id, view)
                proposed_conclusion = grade_payload.proposed_conclusion
                valid_conclusion = (grade_payload.proposed_conclusion == "EXPLAIN" if request_mode == "POLICY_EXPLANATION"
                                    else grade_payload.proposed_conclusion in {"ALLOW", "DENY"})
                provider_sufficient = grade_payload.grade.sufficient
                server_sufficient = (grade_payload.grade.relevant and coverage_ok and not conflict and
                                     bool(evidence_by_id) and valid_conclusion)
                sufficient = (server_sufficient if request_intent_v1
                              else server_sufficient and provider_sufficient)
                sufficiency_audit.append({"hop": hop_number,
                    "provider_sufficient": provider_sufficient,
                    "programmatic_required_coverage_sufficient": server_sufficient,
                    "effective_sufficient": sufficient,
                    "basis": ("request-intent v1: only required rules and required aspect placeholders set the gate; "
                              "supporting clauses do not" if request_intent_v1 else
                              "legacy condition contract retains provider sufficiency veto")})
                if not coverage_ok or conflict:
                    grade_payload.grade.sufficient = False
                if sufficient:
                    final_reason = ("SUFFICIENT_REFUTATION" if proposed_conclusion == "DENY" and
                                    any(item.refutes for item in coverage.values()) else "SUFFICIENT_EVIDENCE")
                else:
                    final_reason = "MAX_HOPS_REACHED" if hop_number == self.config.max_hops else "MAX_HOPS_REACHED"
            except Exception as exc:
                code = getattr(exc, "code", None) or type(exc).__name__
                diagnostic = getattr(exc, "diagnostic", None)
                if isinstance(diagnostic, dict):
                    safe_keys = {"node", "provider_exception", "finish_reason", "response_chars", "raw_content",
                                 "max_tokens", "validation_errors", "provider_status", "thinking",
                                 "timeout_seconds", "latency_ms", "usage"}
                    grade_error_detail = {key: diagnostic[key] for key in safe_keys if key in diagnostic}
                else:
                    grade_error_detail = {"provider_exception": type(exc).__name__}
                grade_error_detail["code"] = str(code)[:80]
                grade_error_detail["validation_stage"] = grade_validation_stage
                if not isinstance(diagnostic, dict) and str(exc):
                    grade_error_detail["safe_message"] = str(exc)[:240]
                if isinstance(graded, dict):
                    grade_raw_output = graded.get("raw_content")
                    grade_finish_reason = graded.get("finish_reason")
                if ledger.requests > len(ledger.calls):
                    ledger.record_failed_call("evidence_sufficiency", estimate, code, diagnostic)
                grade_payload = None
                if grade_finish_reason is None:
                    grade_finish_reason = (diagnostic or {}).get("finish_reason") if isinstance(diagnostic, dict) else None
                coverage = self._unknown_coverage(conditions, evidence_by_id)
                final_missing = [condition.text for condition in conditions if condition.required and condition.kind != "USER_PROVIDED_FACT"]
                final_reason = "UNKNOWN_GRADE"
                sufficient = False
                proposed_conclusion = "UNKNOWN"

            hop_record = {"hop": hop_number, "queries": query_records,
                          "new_evidence_ids": sorted(hop_ids), "evidence_ids": sorted(current_ids),
                          "index_fingerprint": view.fingerprint,
                          "knowledge_version": view.index["knowledge_version"],
                          "retrieval_mode": mode or getattr(getattr(self.retrieval, "config", None), "mode", "bm25"),
                          "degraded": any(row.get("degraded") for row in query_records),
                          "grade_status": "VALIDATED" if grade_payload is not None else "UNKNOWN_GRADE",
                          "grade_result": grade_payload.grade.model_dump() if grade_payload else None,
                          "grade_provider_sufficient": grade_payload.grade.sufficient if grade_payload else None,
                          "sufficiency_audit": sufficiency_audit[-1] if sufficiency_audit else None,
                          "grade_raw_output": grade_raw_output,
                          "grade_identifier_normalizations": hop_grade_normalizations,
                          "grade_payload_sanitizations": aspect_judgment_audit if request_intent_v1 else [],
                          "grade_finish_reason": grade_finish_reason,
                          "grade_error": grade_error_detail,
                          "grade_validation_stage": grade_validation_stage,
                          "coverage": {key: item.model_dump() for key, item in coverage.items()},
                          "sufficient": bool(sufficient), "stop_reason": final_reason}
            hops.append(hop_record)
            if self.on_hop:
                self.on_hop(hop_record)
            if sufficient:
                break
            if final_reason == "UNKNOWN_GRADE" and hop_number < self.config.max_hops:
                if ledger.usage_unknown:
                    break
                continue
            if any(condition.required and condition.kind == "MISSING_INFORMATION" for condition in conditions):
                final_reason = "USER_CLARIFICATION_REQUIRED"
                final_missing = [condition.text for condition in conditions
                                 if condition.required and condition.kind == "MISSING_INFORMATION"]
                break
            if hop_number == self.config.max_hops:
                final_reason = "UNKNOWN_GRADE" if grade_payload is None else "MAX_HOPS_REACHED"
                break

        evidence, final_rerank = self._global_rank(question, evidence_by_id, mode)
        evidence_was_sufficient = final_reason in {"SUFFICIENT_EVIDENCE", "SUFFICIENT_REFUTATION"}
        completion = None
        generation_error = None
        answer_issues: list[dict] = []
        answer_evidence_audit = []
        answer_context_notes = []
        if final_reason in {"SUFFICIENT_EVIDENCE", "SUFFICIENT_REFUTATION"}:
            answer_node = self._node("policy_answer")
            required_policy_conditions = [item for item in conditions
                                          if item.kind == "POLICY_RULE" and item.required]
            answer_evidence, answer_evidence_audit = _required_answer_evidence(
                required_policy_conditions, evidence, coverage)
            answer_context_notes = _answer_context_notes(question, conditions)
            answer_context = {"question": question,
                "required_policy_rules": [item.model_dump() for item in required_policy_conditions],
                "context_notes": answer_context_notes,
                "evidence": answer_evidence if request_intent_v1 else evidence}
            allowed, limit_reason, estimate = ledger.before_model_call(answer_node, str(answer_context))
            if not allowed:
                final_reason = "BUDGET_EXCEEDED"
                final_missing = ["预算不足以生成并核验最终答复"]
            else:
                try:
                    requirement_answerer = getattr(self.provider, "answer_with_requirements", None)
                    if request_intent_v1 and callable(requirement_answerer):
                        completion = requirement_answerer(question, answer_evidence,
                            required_conditions=[item.model_dump() for item in required_policy_conditions],
                            context_notes=answer_context_notes)
                    else:
                        completion = self.provider.answer(question, evidence)
                    ledger.record_call("policy_answer", completion, estimate)
                    cited_source = answer_evidence if request_intent_v1 else evidence
                    allowed_ids = {row["chunk_id"] for row in cited_source}
                    if (not isinstance(completion, dict) or not isinstance(completion.get("answer"), str)
                            or not completion["answer"].strip() or not isinstance(completion.get("citations"), list)
                            or not completion["citations"] or any(cid not in allowed_ids for cid in completion["citations"])):
                        raise ValueError("final answer citations do not match accumulated evidence")
                    cited_rows = [row for row in cited_source if row["chunk_id"] in set(completion["citations"])]
                    answer_issues = _answer_semantic_issues(completion["answer"], cited_rows, question)
                    if request_intent_v1:
                        answer_issues.extend(_required_answer_issues(completion["answer"], question,
                            required_policy_conditions, answer_context_notes))
                    if answer_issues:
                        raise ValueError("final answer failed policy lifecycle/time-window semantic validation: " +
                                         json.dumps(answer_issues, ensure_ascii=False))
                except Exception as exc:
                    code = getattr(exc, "code", None) or type(exc).__name__
                    if ledger.requests > len(ledger.calls):
                        ledger.record_failed_call("policy_answer", estimate, code)
                    generation_error = {"code": code, "exception_type": type(exc).__name__}
                    # Preserve enough bounded, sanitized evidence to diagnose a failed
                    # answer generation. The answer itself remains unset on failure.
                    source_diagnostic = getattr(exc, "diagnostic", None)
                    if not isinstance(source_diagnostic, dict):
                        source_diagnostic = {}
                    generated_usage = completion.get("usage") if isinstance(completion, dict) else None
                    diagnostic_usage = source_diagnostic.get("usage", generated_usage)
                    usage_counts = ({"prompt_units": diagnostic_usage.get("prompt_tokens"),
                                     "completion_units": diagnostic_usage.get("completion_tokens"),
                                     "total_units": diagnostic_usage.get("total_tokens")}
                                    if isinstance(diagnostic_usage, dict) else None)
                    raw_content = source_diagnostic.get("raw_content")
                    if not isinstance(raw_content, str) and isinstance(completion, dict):
                        raw_content = completion.get("raw_content")
                    node_config = self._node("policy_answer")
                    generation_error["diagnostic"] = {
                        "node": "policy_answer",
                        "provider_exception": source_diagnostic.get("provider_exception", type(exc).__name__),
                        "failure_reason": str(exc)[:500],
                        "finish_reason": source_diagnostic.get("finish_reason",
                            completion.get("finish_reason") if isinstance(completion, dict) else None),
                        "response_chars": source_diagnostic.get("response_chars",
                            len(raw_content) if isinstance(raw_content, str) else None),
                        "raw_content": raw_content[:6000] if isinstance(raw_content, str) else None,
                        # safe_trace_json intentionally redacts any key containing
                        # "token"; use explicit unit counts so diagnostics retain
                        # non-secret accounting metadata without weakening redaction.
                        "usage_counts": usage_counts,
                        "cost_estimate_usd_peak_rate": (diagnostic_usage.get("cost_estimate_usd_peak_rate")
                            if isinstance(diagnostic_usage, dict) else None),
                        "latency_ms": source_diagnostic.get("latency_ms",
                            completion.get("latency_ms") if isinstance(completion, dict) else None),
                        "model_id": source_diagnostic.get("model_id",
                            completion.get("model_id") if isinstance(completion, dict) else None),
                        "max_tokens": source_diagnostic.get("max_tokens",
                            completion.get("max_tokens", node_config.max_tokens) if isinstance(completion, dict)
                            else node_config.max_tokens),
                        "timeout_seconds": source_diagnostic.get("timeout_seconds", node_config.timeout_seconds),
                        "thinking": source_diagnostic.get("thinking", node_config.thinking),
                        "prompt_version": source_diagnostic.get("prompt_version",
                            completion.get("prompt_version") if isinstance(completion, dict) else None),
                        "validation_errors": answer_issues or source_diagnostic.get("validation_errors", []) or
                            [{"code": code, "exception_type": type(exc).__name__}],
                    }
                    if request_intent_v1:
                        generation_error["answer_evidence_audit"] = answer_evidence_audit
                    if answer_issues:
                        generation_error["semantic_issues"] = answer_issues
                    final_reason = "GENERATION_UNAVAILABLE"
        if final_reason not in {"SUFFICIENT_EVIDENCE", "SUFFICIENT_REFUTATION"}:
            final_missing = final_missing or [condition.text for condition in conditions
                                               if condition.required and condition.kind != "USER_PROVIDED_FACT"]
        result = self._finish(question, view, [item.model_dump() for item in conditions], hops,
                              {key: value.model_dump() for key, value in coverage.items()},
                              grade_payload.model_dump() if grade_payload else None, final_reason,
                              evidence, ledger, None, final_missing, proposed_conclusion)
        result["request_mode"] = request_mode
        result["condition_extraction_raw_output"] = extracted.get("raw_content")
        result["condition_normalizations"] = condition_normalizations
        result["grade_identifier_normalizations"] = grade_identifier_normalizations
        result["grade_payload_sanitizations"] = grade_payload_sanitizations
        result["condition_scope_audit"] = condition_scope_audit
        result["sufficiency_audit"] = sufficiency_audit
        result["answer_evidence_audit"] = answer_evidence_audit
        result["answer_context_notes"] = answer_context_notes
        result["condition_semantic_audit"] = condition_semantic_audit
        result["condition_analysis_version"] = condition_result.analysis_version
        result["requested_aspects"] = [item.model_dump() for item in requested_aspects]
        if request_intent_v1:
            policy_rules = [item for item in conditions if item.kind == "POLICY_RULE"]
            result["policy_rule_construction"] = {
                "method": "exact_applicable_policy_sentence_atoms_v1",
                "candidate_rule_count": len(policy_rules),
                "required_rule_count": sum(item.required for item in policy_rules),
                "all_source_excerpts_verified": all(
                    item.source_evidence_id in evidence_by_id and item.source_excerpt in
                    (evidence_by_id[item.source_evidence_id].get("content") or "")
                    for item in policy_rules),
            }
        result["final_rerank"] = final_rerank
        result["evidence_sufficient"] = evidence_was_sufficient or final_reason == "GENERATION_UNAVAILABLE"
        result["sufficient"] = final_reason in {"SUFFICIENT_EVIDENCE", "SUFFICIENT_REFUTATION"}
        result["completion"] = completion
        result["generation_error"] = generation_error
        return result

    def _coverage(self, conditions, grade: GradePayload, evidence_by_id: dict, view: RetrievalView):
        by_condition = {condition.condition_id: {"evidence_ids": [], "supports": [], "refutes": [], "unknown": [], "versions": []}
                        for condition in conditions}
        for judgment in grade.judgments:
            if judgment.condition_id not in by_condition or judgment.evidence_id not in evidence_by_id:
                raise GradeValidationError("GRADE_REFERENCE_UNKNOWN", "grade references unknown condition or evidence")
            condition = next(item for item in conditions if item.condition_id == judgment.condition_id)
            evidence = evidence_by_id[judgment.evidence_id]
            if condition.kind != "POLICY_RULE" or not _valid_at(evidence, view.evaluation_at):
                raise GradeValidationError("GRADE_JUDGMENT_SOURCE_MISMATCH",
                    f"judgment for non-policy condition {condition.condition_id} cannot be supported by policy evidence")
            target = by_condition[judgment.condition_id]
            target["evidence_ids"].append(judgment.evidence_id)
            target[judgment.judgment].append(judgment.evidence_id)
            target["versions"].append(evidence["version"])
        coverage = {}
        missing = []
        complete = True
        conflict = False
        for condition in conditions:
            raw = by_condition[condition.condition_id]
            if condition.kind == "USER_PROVIDED_FACT":
                raw["supports"].append("USER_PROVIDED")
            elif condition.kind == "MISSING_INFORMATION":
                raw["unknown"].append("MISSING_INFORMATION")
            elif condition.kind == "POLICY_ASPECT":
                raw["unknown"].append("MISSING_POLICY_EVIDENCE")
            supports = sorted(set(raw["supports"]))
            refutes = sorted(set(raw["refutes"]))
            unknown = sorted(set(raw["unknown"]))
            is_conflict = bool(raw["evidence_ids"] and supports and refutes)
            conflict = conflict or is_conflict
            item_complete = (condition.kind == "USER_PROVIDED_FACT" or
                             (condition.kind == "POLICY_RULE" and bool(raw["evidence_ids"]) and bool(supports or refutes) and not is_conflict))
            if condition.kind == "MISSING_INFORMATION":
                item_complete = False
            if condition.kind == "POLICY_ASPECT":
                item_complete = False
            if condition.required and not item_complete:
                complete = False
                missing.append(condition.text)
            coverage[condition.condition_id] = ConditionCoverage(
                condition_id=condition.condition_id,
                evidence_ids=sorted(set(raw["evidence_ids"])), supports=supports,
                refutes=refutes, unknown=unknown,
                applicable_version=sorted(set(raw["versions"])),
            )
        # LLM cannot claim sufficiency if its missing list contradicts complete coverage,
        # or if the coverage map omits a necessary rule. Conditions without judgments are unknown.
        for condition in conditions:
            if condition.kind == "POLICY_RULE" and not by_condition[condition.condition_id]["evidence_ids"]:
                coverage[condition.condition_id].unknown.append("NO_APPLICABLE_EVIDENCE")
        if grade.grade.missing_aspect:
            missing_ids = set(grade.grade.missing_aspect)
            known_ids = {condition.condition_id for condition in conditions}
            if not missing_ids.issubset(known_ids):
                raise ValueError("grade missing_aspect includes unknown condition_id")
            for condition in conditions:
                if condition.condition_id in missing_ids and condition.required:
                    complete = False
                    if condition.text not in missing:
                        missing.append(condition.text)
        return coverage, missing, complete, conflict

    @staticmethod
    def _unknown_coverage(conditions, evidence):
        result = {}
        for condition in conditions:
            evidence_ids = []
            if condition.kind == "POLICY_RULE":
                evidence_ids = sorted(evidence)
            supports = ["USER_PROVIDED"] if condition.kind == "USER_PROVIDED_FACT" else []
            result[condition.condition_id] = ConditionCoverage(
                condition_id=condition.condition_id, evidence_ids=evidence_ids,
                supports=supports, unknown=["UNKNOWN_GRADE"], applicable_version=[])
        return result

    def _global_rank(self, question, evidence_by_id, mode):
        if not evidence_by_id:
            return [], {"reranked": False, "degraded": False, "reason": "no_accumulated_evidence"}
        rows = list(evidence_by_id.values())
        try:
            ranked, info = self.retrieval.rerank_candidates(question, rows, mode=mode)
            return ranked, info
        except (AttributeError, TypeError):
            rows.sort(key=lambda row: (row.get("rank", 10**9), row["chunk_id"]))
            return rows[:5], {"reranked": False, "degraded": True, "reason": "global_rerank_unavailable"}

    def _finish(self, question, view, conditions, hops, coverage, grade, reason, evidence,
                ledger, detail, missing=None, proposed_conclusion="UNKNOWN"):
        result = {"conditions": conditions, "hops": hops, "condition_coverage": coverage,
                  "grade_result": grade, "stop_reason": reason,
                  "sufficient": reason in {"SUFFICIENT_EVIDENCE", "SUFFICIENT_REFUTATION"},
                  "proposed_conclusion": proposed_conclusion,
                  "missing_aspect": missing or [], "evidence": evidence,
                  "knowledge_version": view.index["knowledge_version"],
                  "index_fingerprint": view.fingerprint, "evaluation_at": view.evaluation_at,
                  "retrieval_mode": getattr(getattr(self.retrieval, "config", None), "mode", "bm25"),
                  "retrieval_config_hash": getattr(getattr(self.retrieval, "config", None), "config_hash", None),
                  "retrieval_config_version": getattr(getattr(self.retrieval, "config", None), "version", None),
                  "prompt_version": self.PROMPT_VERSION, "budget": ledger.as_dict()}
        if detail:
            result["diagnostic_code"] = str(detail)[:80]
        result["answer_kind"] = ("POLICY_ANSWER" if result["sufficient"] else
                                  "GENERATION_UNAVAILABLE" if reason == "GENERATION_UNAVAILABLE" else
                                  "CLARIFICATION_REQUIRED")
        result["answer"] = ("" if result["sufficient"] or reason == "GENERATION_UNAVAILABLE" else
                            self._clarify(result["missing_aspect"], reason))
        if self.on_trace:
            self.on_trace("self_rag_completed", {"stop_reason": reason, "sufficient": result["sufficient"],
                "hop_count": len(hops), "evidence_count": len(evidence), "index_fingerprint": view.fingerprint,
                "knowledge_version": view.index["knowledge_version"], "model_requests": ledger.requests,
                "retrieval_queries": ledger.queries, "diagnostic_code": result.get("diagnostic_code")})
        return result
