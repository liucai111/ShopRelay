"""Single source of truth for the M6.2 Judge request and response contract."""
from __future__ import annotations

import json
from typing import Any

DECISIONS = ("PASS", "PARTIAL", "FAIL", "INSUFFICIENT_EVIDENCE", "UNSCORABLE")
DIMENSION_VALUES = (*DECISIONS, "NOT_APPLICABLE")
DIMENSIONS = (
    "task_fit",
    "source_support",
    "conditions_and_scope",
    "safety_and_non_disclosure",
    "evidence_sufficiency",
)
PAYLOAD_KEYS = {"question", "answer_or_claim", "applicable_sources", "rubric", "scoring_target"}


def response_schema(allowed_source_refs: list[str]) -> dict[str, Any]:
    """Return a complete JSON Schema; cited refs are scoped to this payload."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "ShopRelay M6.2 Judge response",
        "type": "object",
        "required": ["decision", "dimension_scores", "rationale", "evidence_refs", "confidence"],
        "additionalProperties": False,
        "properties": {
            "decision": {"type": "string", "enum": list(DECISIONS)},
            "dimension_scores": {
                "type": "object",
                "required": list(DIMENSIONS),
                "additionalProperties": False,
                "properties": {name: {"type": "string", "enum": list(DIMENSION_VALUES)} for name in DIMENSIONS},
            },
            "rationale": {"type": "string", "maxLength": 900},
            "evidence_refs": {"type": "array", "minItems": 1, "uniqueItems": True,
                "items": {"type": "string", "enum": list(allowed_source_refs)}},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        },
    }


def contract_artifact() -> dict[str, Any]:
    """Versioned schema contract descriptor; dynamic source enums are injected per request."""
    return {"schema_version": "shoprelay-m6.2-judge-response-contract-v2",
        "decisions": list(DECISIONS), "dimensions": list(DIMENSIONS),
        "dimension_values": list(DIMENSION_VALUES),
        "required_root_fields": ["decision", "dimension_scores", "rationale", "evidence_refs", "confidence"],
        "additional_root_fields": False, "rationale_max_characters": 900,
        "confidence_range": [0, 1], "evidence_refs_must_be_supplied_source_refs": True,
        "dynamic_schema_builder": "response_schema(allowed_source_refs) from eval.m6_2.judge_contract"}


def validate_payload(payload: object) -> tuple[bool, list[str], list[str]]:
    errors: list[str] = []
    if not isinstance(payload, dict) or set(payload) != PAYLOAD_KEYS:
        return False, ["payload_fields_mismatch"], []
    for key in ("question", "answer_or_claim", "scoring_target"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            errors.append(f"invalid_{key}")
    sources = payload.get("applicable_sources")
    if not isinstance(sources, list):
        errors.append("invalid_applicable_sources")
        sources = []
    refs: list[str] = []
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {"source_ref", "text"} or
                not isinstance(source.get("source_ref"), str) or not source["source_ref"].strip() or
                not isinstance(source.get("text"), str) or not source["text"].strip()):
            errors.append("invalid_source_record")
        else:
            refs.append(source["source_ref"])
    if len(refs) != len(set(refs)):
        errors.append("duplicate_source_ref")
    if not refs:
        errors.append("empty_applicable_sources")
    if not isinstance(payload.get("rubric"), dict):
        errors.append("invalid_rubric")
    return not errors, errors, refs


def build_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    valid, errors, refs = validate_payload(payload)
    if not valid:
        raise ValueError("invalid blinded Judge payload: " + ",".join(errors))
    schema = response_schema(refs)
    system = (
        "You are a blinded evaluation assistant. Score only the supplied scoring target. "
        "Do not use outside knowledge. Do not infer execution, tools, delivery, side effects, or hidden labels. "
        "Return exactly one JSON object, with no Markdown or text before/after it. Follow this full output contract exactly:\n"
        + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        + "\nFor PARTIAL, preserve PARTIAL; do not map it to PASS or FAIL. Use INSUFFICIENT_EVIDENCE when supplied evidence cannot establish support or contradiction. "
        "Every evidence_refs entry must be copied exactly from an applicable_sources.source_ref in the user message."
    )
    user = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def validate_response(value: object, allowed_source_refs: set[str]) -> tuple[bool, list[str]]:
    errors: list[str] = []
    if not isinstance(value, dict):
        return False, ["root_not_object"]
    required = {"decision", "dimension_scores", "rationale", "evidence_refs", "confidence"}
    if set(value) != required:
        errors.append("root_fields_mismatch")
    if value.get("decision") not in DECISIONS:
        errors.append("invalid_decision")
    dimensions = value.get("dimension_scores")
    if not isinstance(dimensions, dict) or set(dimensions) != set(DIMENSIONS):
        errors.append("dimension_fields_mismatch")
    elif any(item not in DIMENSION_VALUES for item in dimensions.values()):
        errors.append("invalid_dimension_value")
    rationale = value.get("rationale")
    if not isinstance(rationale, str) or len(rationale) > 900:
        errors.append("invalid_rationale")
    refs = value.get("evidence_refs")
    if not isinstance(refs, list) or not refs or any(not isinstance(item, str) for item in refs):
        errors.append("invalid_evidence_refs")
    elif len(refs) != len(set(refs)) or not set(refs).issubset(allowed_source_refs):
        errors.append("evidence_ref_out_of_scope_or_duplicate")
    confidence = value.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1:
        errors.append("invalid_confidence")
    return not errors, errors


def parse_response(content: object, allowed_source_refs: set[str]) -> tuple[str, dict | None, list[str]]:
    if not isinstance(content, str) or not content.strip():
        return "EMPTY_CONTENT", None, ["empty_content"]
    try:
        value = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return "INVALID_JSON", None, ["invalid_or_truncated_json"]
    valid, errors = validate_response(value, allowed_source_refs)
    return ("VALID", value, []) if valid else ("INVALID_SCHEMA", value if isinstance(value, dict) else None, errors)


def create_judge_completion(client, model: str, payload: dict[str, Any], max_tokens: int = 768):
    """Actual request path shared by live runner and capturing offline tests.

    No provider-specific response_format is assumed. The complete contract is
    always delivered in the system message and validated locally.
    """
    messages = build_messages(payload)
    return client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0,
        max_tokens=max_tokens,
        stream=False,
        extra_body={"thinking": {"type": "disabled"}},
    )
