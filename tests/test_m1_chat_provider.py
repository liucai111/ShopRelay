from types import SimpleNamespace

import pytest

from app.m0.config import load_settings
from app.m1.chat import ChatFailure, DeepSeekProvider


class FakeCompletions:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = 0
        self.last_kwargs = None

    def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.error:
            raise self.error
        return self.response


def completion(content, finish_reason="stop", usage=None):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=content))], model="deepseek-flash",
        usage=SimpleNamespace(model_dump=lambda: usage) if usage else None)
    return response


def provider_for(response):
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(response=response)))
    settings = load_settings(environ={"DEEPSEEK_API_KEY": "unit-test-secret", "DEEPSEEK_MODEL": "deepseek-flash",
                                     "SHOPRELAY_DEEPSEEK_EVIDENCE_SUFFICIENCY_THINKING": "disabled",
                                     "SHOPRELAY_DEEPSEEK_EVIDENCE_SUFFICIENCY_MAX_TOKENS": "3072",
                                     "SHOPRELAY_DEEPSEEK_EVIDENCE_SUFFICIENCY_TIMEOUT_SECONDS": "30"},
                             local_values={}, require_api_key=True)
    return DeepSeekProvider(settings=settings, client=client)


def test_deepseek_provider_validates_json_and_citations_and_estimates_usage():
    provider = provider_for(completion('{"answer":"可申请退货。","citations":["p/one"]}', usage={
        "prompt_tokens": 100, "completion_tokens": 20, "prompt_tokens_details": {"cached_tokens": 10}}))
    result = provider.answer("质量问题能否退货", [{"chunk_id": "p/one", "content": "政策"}])
    assert result["citations"] == ["p/one"] and result["model_id"] == "deepseek-flash"
    assert result["raw_content"] == '{"answer":"可申请退货。","citations":["p/one"]}'
    assert result["prompt_version"] == "m1-policy-answer-v1"
    assert result["estimated_cost_usd"] == pytest.approx((90 * 0.30 + 10 * .006 + 20 * 1.2) / 1_000_000)


def test_policy_answer_serializes_only_retrieved_source_version_metadata_to_provider():
    import json
    provider = provider_for(completion('{"answer":"The cited policy is version 7.","citations":["policy/a"]}'))
    result = provider.answer("What does this policy say?", [{
        "chunk_id": "policy/a", "title": "Policy A", "content": "Applies from 2026-01-01.",
        "version": "7", "effective_from": "2026-01-01T00:00:00Z", "effective_to": None,
        "knowledge_version": "kb-r2", "index_fingerprint": "idx-abc", "evaluation_at": "2026-04-01T00:00:00Z",
        "policy_document_version": "7", "source_type": "POLICY_DOCUMENT",
        "secret_unapproved_field": "must not be sent"}])
    sent = json.loads(provider.client.chat.completions.last_kwargs["messages"][1]["content"])
    source = sent["evidence"][0]
    assert result["citations"] == ["policy/a"]
    assert source["effective_from"] == "2026-01-01T00:00:00Z"
    assert source["effective_to"] is None
    assert source["knowledge_version"] == "kb-r2"
    assert source["index_fingerprint"] == "idx-abc"
    assert source["evaluation_at"] == "2026-04-01T00:00:00Z"
    assert source["policy_document_version"] == "7"
    assert "secret_unapproved_field" not in source
    assert "不自动等同订单送达日期适用条件" in provider.client.chat.completions.last_kwargs["messages"][0]["content"]


def test_route_schema_failure_keeps_bounded_field_diagnostic_and_usage_without_payload():
    from app.m1.routing import decide_route
    bad_payload = '{"strategy":"AGENT","sub_strategy":"BM25","intent_labels":[],"safety_decision":"ALLOW","reason_code":"x","needs_clarification":false,"clarification_question":null,"required_capabilities":[]}'
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(response=completion(
        bad_payload, usage={"prompt_tokens": 55, "completion_tokens": 12, "total_tokens": 67,
            "prompt_tokens_details": {"cached_tokens": 0}}))))
    settings = load_settings(environ={"DEEPSEEK_API_KEY": "unit-test-secret", "DEEPSEEK_MODEL": "deepseek-flash"},
        local_values={}, require_api_key=True)
    provider = DeepSeekProvider(settings=settings, client=client)
    route = decide_route("Can you explain this unusual request?", classifier=provider)
    assert route.reason_code == "LLM_CLASSIFICATION_FALLBACK"
    assert route.usage == {"prompt_tokens": 55, "completion_tokens": 12, "total_tokens": 67}
    diagnostic = route.provider_diagnostic
    assert diagnostic["failure_phase"] == "RESPONSE_PARSE_OR_CONTRACT_FAILURE"
    assert diagnostic["validation_issues"]
    assert all(set(row) == {"location", "type"} for row in diagnostic["validation_issues"])
    assert "input" not in str(diagnostic).casefold()
    assert bad_payload not in str(route.model_dump())


def test_route_response_summary_is_bound_to_local_semantic_contract_failure():
    from app.m1.routing import decide_route
    bad_payload = {"strategy": "DIRECT", "sub_strategy": None, "intent_labels": ["uncertain"],
        "safety_decision": "ALLOW", "reason_code": "model_choice", "needs_clarification": False,
        "clarification_question": None, "required_capabilities": ["search_knowledge"]}
    response = completion(__import__("json").dumps(bad_payload), usage={
        "prompt_tokens": 70, "completion_tokens": 11, "total_tokens": 81})
    provider = provider_for(response)
    route = decide_route("An ambiguous question that needs classification", classifier=provider)
    assert route.decision_source == "FALLBACK"
    assert route.fallback_error == "DIRECT_REFUSE_CAPABILITY_MISMATCH:RouteClassifierContractError"
    diagnostic = route.provider_diagnostic
    assert diagnostic["failure_phase"] == "CLASSIFIER_SEMANTIC_CONTRACT"
    assert diagnostic["route_stage"] == "CLASSIFIER_SEMANTIC_CONTRACT"
    assert diagnostic["route_error_code"] == "DIRECT_REFUSE_CAPABILITY_MISMATCH"
    assert diagnostic["field_path"] == "required_capabilities"
    summary = diagnostic["response_summary"]
    assert summary["provider_response_received"] is True
    assert summary["choice_count"] == 1 and summary["finish_reason"] == "stop"
    assert summary["content_present"] is True and summary["content_length"] > 0
    assert summary["reasoning_content_present"] is False
    assert summary["tool_calls_present"] is False
    assert summary["usage"] == {"prompt_tokens": 70, "completion_tokens": 11, "total_tokens": 81}
    assert "model_choice" not in str(diagnostic)


def test_condition_extraction_uses_structured_contract_and_checks_user_fact_excerpt():
    payload = {"request_mode": "POLICY_EXPLANATION", "analysis_version": "request_intent_v1",
        "requested_aspects": [{"aspect_id": "return-policy", "text": "已签收问题的退货规则",
                               "query_anchor": "已签收的瑕疵商品能否退货"}], "conditions": [
        {"condition_id": "order-state", "text": "商品已签收", "kind": "USER_PROVIDED_FACT",
         "required": False, "source_excerpt": "已签收"},
    ]}
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(
        response=completion(__import__("json").dumps(payload), usage={"prompt_tokens": 120, "completion_tokens": 32}))))
    settings = load_settings(environ={"DEEPSEEK_API_KEY": "unit-test-secret", "DEEPSEEK_MODEL": "deepseek-flash"},
                             local_values={}, require_api_key=True)
    provider = DeepSeekProvider(settings=settings, client=client)
    result = provider.extract_conditions("已签收的瑕疵商品能否退货？")
    assert len(result["conditions_result"]["conditions"]) == 1
    assert result["prompt_version"] == "m2.4-request-intent-scope-v3"
    assert result["conditions_result"]["conditions"][0]["policy_stages"] == []
    assert result["raw_content"] == __import__("json").dumps(payload)
    request = client.chat.completions.last_kwargs
    assert 'POLICY_EXPLANATION' in request["messages"][0]["content"]
    assert 'ELIGIBILITY_DECISION' in request["messages"][0]["content"]
    assert "USER_PROVIDED_FACT" in request["messages"][0]["content"]
    assert "required=false" in request["messages"][0]["content"]
    assert "query_anchor" in request["messages"][0]["content"]

    bad = payload | {"conditions": [payload["conditions"][0] | {"source_excerpt": "未在问题中出现"}]}
    provider = provider_for(completion(__import__("json").dumps(bad)))
    with pytest.raises(ChatFailure, match="CONDITION_SOURCE_INVALID"):
        provider.extract_conditions("已签收的瑕疵商品能否退货？")

    from app.m2.self_rag import ConditionsResult
    with pytest.raises(ValueError, match="pre-retrieval analysis cannot create policy or business-tool facts"):
        ConditionsResult.model_validate({"request_mode": "POLICY_EXPLANATION", "analysis_version": "request_intent_v1",
            "requested_aspects": [{"aspect_id": "return", "text": "退货规则",
                                   "query_anchor": "退货规则"}], "conditions": [
                {"condition_id": "invented", "text": "获批后申请退款", "kind": "POLICY_RULE", "required": True}]})


def test_m2r_aspect_planner_has_small_policy_free_schema_and_validates_anchors():
    import json
    payload = {"request_mode": "POLICY_EXPLANATION", "requested_aspects": [
        {"aspect_id": "return-window", "text": "质量退货的时限", "query_anchor": "质量退货"},
        {"aspect_id": "refund-prerequisite", "text": "退款申请前置条件", "query_anchor": "退款"}],
        "user_provided_facts": [{"text": "商品存在瑕疵", "source_excerpt": "商品有瑕疵"}]}
    provider = provider_for(completion(json.dumps(payload, ensure_ascii=False), usage={
        "prompt_tokens": 90, "completion_tokens": 45}))
    result = provider.plan_policy_aspects("商品有瑕疵，质量退货和退款如何衔接？")
    assert result["plan_result"]["request_mode"] == "POLICY_EXPLANATION"
    assert result["prompt_version"] == "m2-r-policy-aspect-plan-v1"
    prompt = provider.client.chat.completions.last_kwargs["messages"][0]["content"]
    assert "requested_aspects" in prompt and "POLICY_RULE" not in prompt
    assert "Grade" not in prompt and "sufficiency" not in prompt

    payload["user_provided_facts"][0]["source_excerpt"] = "用户说商品已签收"
    provider = provider_for(completion(json.dumps(payload, ensure_ascii=False), usage={
        "prompt_tokens": 90, "completion_tokens": 45}))
    with pytest.raises(ChatFailure, match="POLICY_ASPECT_PLAN_INVALID"):
        provider.plan_policy_aspects("商品有瑕疵，质量退货和退款如何衔接？")


def test_historical_delivery_policy_extraction_is_canonicalized_without_claiming_order_eligibility():
    import json
    from pathlib import Path
    from app.m2.self_rag import ConditionsResult

    record = json.loads((Path(__file__).parents[1] /
        "eval/raw/m2.4-self-rag-recheck-20261020T063455Z-046a1927.json").read_text(encoding="utf-8"))
    case_row = next(row for row in record["results"] if row.get("case_id") == "pr2-delivery_policy_limits-01")
    raw_payload = json.loads(case_row["condition_extraction_raw_output"])
    validated = ConditionsResult.model_validate(raw_payload)
    assert validated.conditions[0].source_type == "USER_PROVIDED"
    assert validated.conditions[1].required and validated.conditions[1].source_type == "UNKNOWN"
    assert validated.conditions[2].source_type == "UNKNOWN"

    # The same missing fact is not a prerequisite for merely explaining policy.
    raw_payload["request_mode"] = "POLICY_EXPLANATION"
    with pytest.raises(ValueError, match="policy explanation cannot require missing personal order information"):
        ConditionsResult.model_validate(raw_payload)
    raw_payload["conditions"][1]["required"] = False
    assert ConditionsResult.model_validate(raw_payload).conditions[1].required is False


def test_grade_node_uses_bounded_nonthinking_structured_configuration():
    from app.m2.self_rag import GradePayload
    payload = {"grade": {"relevant": True, "sufficient": True, "missing_aspect": [], "rationale": "所问政策规则有明确条款。"},
               "judgments": [{"condition_id": "c1", "evidence_id": "p/1", "judgment": "supports"}],
               "aspect_rule_map": [{"aspect_id": "refund-prerequisite", "required_rules": [{
                   "rule_id": "c1", "basis": "DIRECTLY_ASKED", "query_anchor": "政策如何衔接",
                   "rationale": "回答用户明确询问的规则"}], "supporting_rule_ids": []}],
               "proposed_conclusion": "EXPLAIN"}
    provider = provider_for(completion(__import__("json").dumps(payload)))
    evidence = [{"chunk_id": "p/1", "version": "v1", "effective_from": "2026-01-01T00:00:00Z",
                 "effective_to": None, "title": "Rule", "content": "所问一般政策规则。"}]
    result = provider.grade_evidence("政策如何衔接？", [{"condition_id": "c1", "kind": "POLICY_RULE",
        "source_type": "POLICY", "text": "规则", "required": True}], evidence,
        "2026-10-20T00:00:00Z", request_mode="POLICY_EXPLANATION",
        requested_aspects=[{"aspect_id": "refund-prerequisite", "text": "退款前置条件",
                            "query_anchor": "政策如何衔接"}])
    request = provider.client.chat.completions.last_kwargs
    assert result["prompt_version"] == "m2.4-evidence-grade-scope-v3"
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["max_tokens"] == 3072
    assert "POLICY_EXPLANATION" in request["messages"][0]["content"]
    assert "judgments 只能列 POLICY_RULE" in request["messages"][0]["content"]
    assert "签收后7×24小时内" in request["messages"][0]["content"]
    assert "supporting_rule_ids" in request["messages"][0]["content"]
    assert GradePayload.model_validate(result["grade_payload"]).proposed_conclusion == "EXPLAIN"


def test_evidence_grade_rejects_unknown_evidence_and_truncated_json():
    from app.m2.self_rag import GradePayload
    good = {"grade": {"relevant": True, "sufficient": True, "missing_aspect": [], "rationale": "规则适用。"},
            "judgments": [{"condition_id": "c1", "evidence_id": "p/1", "judgment": "supports"}],
            "proposed_conclusion": "EXPLAIN"}
    provider = provider_for(completion(__import__("json").dumps(good)))
    response = provider._structured_node("evidence_sufficiency", "test-grade-v1", "grade",
                                         {"question": "q"})
    assert GradePayload.model_validate(response["result"]).proposed_conclusion == "EXPLAIN"
    provider = provider_for(completion('{"grade":', "length", usage={"prompt_tokens": 1000, "completion_tokens": 3072}))
    with pytest.raises(ChatFailure, match="STRUCTURED_NODE_INVALID_RESPONSE") as raised:
        provider._structured_node("evidence_sufficiency", "test-grade-v1", "grade", {"question": "q"})
    assert raised.value.diagnostic["finish_reason"] == "length"
    assert raised.value.diagnostic["usage"]["completion_tokens"] == 3072


@pytest.mark.parametrize("content,finish", [('{"answer":"x","citations":["not-retrieved"]}', "stop"),
                                               ('{"answer":"truncated"', "length"),
                                               ('not json', "stop")])
def test_deepseek_provider_rejects_invalid_structure_or_truncation(content, finish):
    provider = provider_for(completion(content, finish))
    with pytest.raises(ChatFailure, match="MODEL_INVALID_RESPONSE"):
        provider.answer("question", [{"chunk_id": "p/one", "content": "policy"}])


@pytest.mark.parametrize("error_type,expected,retryable", [
    ("connection", "MODEL_UNAVAILABLE", True),
    ("timeout", "MODEL_UNAVAILABLE", True),
    ("authentication", "MODEL_AUTHENTICATION_FAILED", False),
    ("rate_limit", "MODEL_RATE_LIMITED", True),
])
def test_deepseek_provider_maps_transport_and_service_failures(error_type, expected, retryable):
    import httpx
    from openai import APIConnectionError, APITimeoutError, AuthenticationError, RateLimitError
    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    if error_type == "connection":
        error = APIConnectionError(request=request)
    elif error_type == "timeout":
        error = APITimeoutError(request=request)
    elif error_type == "authentication":
        error = AuthenticationError(message="redacted test", response=httpx.Response(401, request=request), body={})
    else:
        error = RateLimitError(message="redacted test", response=httpx.Response(429, request=request), body={})
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(error=error)))
    settings = load_settings(environ={"DEEPSEEK_API_KEY": "test", "DEEPSEEK_MODEL": "deepseek-flash"},
                             local_values={}, require_api_key=True)
    provider = DeepSeekProvider(settings=settings, client=client)
    with pytest.raises(ChatFailure) as raised:
        provider.answer("question", [{"chunk_id": "p/one", "content": "policy"}])
    assert raised.value.code == expected
    assert raised.value.retryable is retryable
    assert "redacted test" not in str(raised.value.diagnostic)


def test_route_classifier_records_sanitized_connection_chain_and_unknown_usage():
    import json
    import httpx
    import socket
    from openai import APIConnectionError

    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    connect = httpx.ConnectError("secret-proxy-password should never persist", request=request)
    connect.__cause__ = socket.gaierror(-2, "private-dns-name.invalid")
    error = APIConnectionError(request=request)
    error.__cause__ = connect
    client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(error=error)))
    settings = load_settings(environ={"DEEPSEEK_API_KEY": "test-secret", "DEEPSEEK_MODEL": "deepseek-flash"},
                             local_values={}, require_api_key=True)
    provider = DeepSeekProvider(settings=settings, client=client)

    with pytest.raises(ChatFailure) as raised:
        provider.classify_route("请说明一般政策", available_capabilities=["search_knowledge"])

    diagnostic = raised.value.diagnostic["provider_diagnostic"]
    assert diagnostic["failure_phase"] == "CONNECTION_NO_RESPONSE"
    assert diagnostic["error_category"] == "DNS_FAILURE"
    assert diagnostic["provider_response_received"] is False
    assert diagnostic["request_delivery"] == "OUTCOME_UNKNOWN"
    assert diagnostic["exception_types"] == ["APIConnectionError", "ConnectError", "gaierror"]
    assert diagnostic["errno"] == -2
    assert diagnostic["usage_status"] == "UNKNOWN"
    assert diagnostic["billing_status"] == "UNKNOWN"
    serialized = json.dumps(raised.value.diagnostic)
    assert "secret-proxy-password" not in serialized
    assert "private-dns-name.invalid" not in serialized
    assert "test-secret" not in serialized


@pytest.mark.parametrize(("error", "phase", "category", "response", "timeout_stage"), [
    ("tls", "CONNECTION_NO_RESPONSE", "TLS_FAILURE", False, None),
    ("connect_timeout", "TIMEOUT_NO_RESPONSE", "TIMEOUT", False, "CONNECT"),
    ("http_503", "HTTP_RESPONSE_RECEIVED", "HTTP_ERROR", True, None),
])
def test_route_classifier_diagnostics_distinguish_tls_timeout_and_http(error, phase, category, response, timeout_stage):
    import httpx
    import ssl
    from openai import APIConnectionError, APIStatusError, APITimeoutError

    request = httpx.Request("POST", "https://api.deepseek.com/chat/completions")
    if error == "tls":
        cause = httpx.ConnectError("credentials=hidden", request=request)
        cause.__cause__ = ssl.SSLError("certificate details hidden")
        failure = APIConnectionError(request=request)
        failure.__cause__ = cause
    elif error == "connect_timeout":
        failure = APITimeoutError(request=request)
        failure.__cause__ = httpx.ConnectTimeout("private host", request=request)
    else:
        failure = APIStatusError(message="response body secret", response=httpx.Response(
            503, request=request, json={"key": "hidden"}), body={"key": "hidden"})
    settings = load_settings(environ={"DEEPSEEK_API_KEY": "test-secret"}, local_values={}, require_api_key=True)
    provider = DeepSeekProvider(settings=settings,
        client=SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions(error=failure))))

    with pytest.raises(ChatFailure) as raised:
        provider.classify_route("synthetic route diagnostic", available_capabilities=["search_knowledge"])
    diagnostic = raised.value.diagnostic["provider_diagnostic"]
    assert diagnostic["failure_phase"] == phase
    assert diagnostic["error_category"] == category
    assert diagnostic["provider_response_received"] is response
    assert diagnostic["timeout_stage"] == timeout_stage
    assert diagnostic["usage_status"] == "UNKNOWN"
    assert diagnostic["billing_status"] == "UNKNOWN"
    assert "hidden" not in str(diagnostic)


def test_route_classifier_parse_failure_is_not_a_connection_failure_and_model_direct_is_preserved():
    import json

    settings = load_settings(environ={"DEEPSEEK_API_KEY": "test-secret"}, local_values={}, require_api_key=True)
    provider = DeepSeekProvider(settings=settings, client=SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions(response=completion("not-json")))))
    with pytest.raises(ChatFailure) as raised:
        provider.classify_route("ambiguous synthetic query", available_capabilities=["search_knowledge"])
    diagnostic = raised.value.diagnostic["provider_diagnostic"]
    assert diagnostic["failure_phase"] == "RESPONSE_PARSE_OR_CONTRACT_FAILURE"
    assert diagnostic["error_category"] == "RESPONSE_INVALID"
    assert diagnostic["provider_response_received"] is True

    direct = {"strategy": "DIRECT", "sub_strategy": None, "intent_labels": ["chitchat"],
        "safety_decision": "ALLOW", "reason_code": "GREETING", "needs_clarification": False,
        "clarification_question": None, "required_capabilities": []}
    provider = DeepSeekProvider(settings=settings, client=SimpleNamespace(chat=SimpleNamespace(
        completions=FakeCompletions(response=completion(json.dumps(direct))))))
    result = provider.classify_route("你好", available_capabilities=["search_knowledge"])
    assert result["strategy"] == "DIRECT"
    assert result["decision_source"] == "LLM"
    assert result["usage"] is None
    assert result.get("provider_diagnostic") is None


def test_real_provider_client_has_implicit_retries_disabled():
    from app.m0.config import create_deepseek_client

    settings = load_settings(environ={"DEEPSEEK_API_KEY": "test-secret"}, local_values={}, require_api_key=True)
    client = create_deepseek_client(settings, settings.nodes["route"])
    try:
        assert client.max_retries == 0
    finally:
        client.close()
