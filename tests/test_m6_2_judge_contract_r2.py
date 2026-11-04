import json
from types import SimpleNamespace

import pytest

from eval.m6_2.judge_batch import execute_samples
from eval.m6_2.judge_contract import (
    DIMENSIONS, build_messages, create_judge_completion, parse_response, response_schema,
)


def payload():
    return {"question": "Is this claim supported?", "answer_or_claim": "The bound fact is 12 units.",
        "applicable_sources": [{"source_ref": "doc-a", "text": "The bound fact is 12 units."}],
        "rubric": {"scope": "claim only", "instruction": "Do not extend the claim."},
        "scoring_target": "Only assess this claim."}


def valid_answer(ref="doc-a", decision="PARTIAL"):
    return {"decision": decision,
        "dimension_scores": {key: "PARTIAL" for key in DIMENSIONS},
        "rationale": "The statement is partly supported.", "evidence_refs": [ref], "confidence": 0.71}


class CapturingClient:
    def __init__(self, content, usage=(30, 20, 50)):
        self.calls = []
        self.content = content
        self.usage = usage
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        p, c, t = self.usage
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self.content), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=p, completion_tokens=c, total_tokens=t), model="deepseek-flash")


def test_actual_send_path_includes_contract_target_source_and_quote_scope():
    client = CapturingClient(json.dumps(valid_answer(), separators=(",", ":")))
    create_judge_completion(client, "deepseek-flash", payload())
    assert len(client.calls) == 1
    call = client.calls[0]
    system, user = call["messages"]
    assert "Return exactly one JSON object" in system["content"]
    assert '"required":["decision","dimension_scores","rationale","evidence_refs","confidence"]' in system["content"]
    assert '"enum":["doc-a"]' in system["content"]
    assert "Only assess this claim." in user["content"]
    assert "doc-a" in user["content"] and "The bound fact is 12 units." in user["content"]
    assert call["max_tokens"] == 768 and call["stream"] is False
    assert "response_format" not in call


def test_schema_is_single_source_used_for_prompt_and_validator():
    schema = response_schema(["doc-a"])
    assert set(schema["properties"]["dimension_scores"]["required"]) == set(DIMENSIONS)
    assert schema["additionalProperties"] is False
    ok, error = parse_response(json.dumps(valid_answer()), {"doc-a"})[0:2]
    assert ok == "VALID" and error == valid_answer()


@pytest.mark.parametrize("bad", [
    {k: v for k, v in valid_answer().items() if k != "confidence"},
    {**valid_answer(), "decision": "MAYBE"},
    {**valid_answer(), "evidence_refs": ["other-doc"]},
    {**valid_answer(), "dimension_scores": {"task_fit": "PARTIAL"}},
    {**valid_answer(), "extra": "drift"},
])
def test_field_drift_enum_scope_and_missing_dimensions_rejected(bad):
    status, _, errors = parse_response(json.dumps(bad), {"doc-a"})
    assert status == "INVALID_SCHEMA"
    assert errors


def test_missing_schema_payload_field_fails_before_transport():
    client = CapturingClient("{}")
    bad = payload(); del bad["scoring_target"]
    with pytest.raises(ValueError, match="invalid blinded Judge payload"):
        create_judge_completion(client, "deepseek-flash", bad)
    assert client.calls == []


def test_truncated_json_rejected_and_partial_remains_distinct():
    assert parse_response('{"decision":"PASS"', {"doc-a"})[0] == "INVALID_JSON"
    assert parse_response(json.dumps(valid_answer(decision="PARTIAL")), {"doc-a"})[1]["decision"] == "PARTIAL"
    assert parse_response(json.dumps(valid_answer(decision="INSUFFICIENT_EVIDENCE")), {"doc-a"})[1]["decision"] == "INSUFFICIENT_EVIDENCE"


def test_first_sample_format_failure_consumes_once_and_blocks_next_sample():
    samples = [{"calibration_sample_id": "first", "judge_payload": payload()},
        {"calibration_sample_id": "second", "judge_payload": payload()}]
    calls, reservations, settlements = [], [], []
    def send(_):
        calls.append(1)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"score":5}'), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=4, total_tokens=14), model="deepseek-flash")
    outcome = execute_samples(samples, send=send, reserve_before_send=reservations.append,
        settle_after_response=lambda sid, raw, result: settlements.append((sid, result["status"])))
    assert len(calls) == len(reservations) == len(settlements) == 1
    assert reservations == ["first"] and settlements == [("first", "INVALID_SCHEMA")]
    assert outcome["stop_reason"] == "FORMAT_FAILURE" and outcome["remaining_samples"] == ["second"]


def test_usage_unknown_or_inconsistent_blocks_next_sample_after_attempt_is_settled():
    samples = [{"calibration_sample_id": "first", "judge_payload": payload()},
        {"calibration_sample_id": "second", "judge_payload": payload()}]
    sent, reserved, settled = [], [], []
    def send(_):
        sent.append(True)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(valid_answer())), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=True, completion_tokens=20, total_tokens=21), model="deepseek-flash")
    outcome = execute_samples(samples, send=send, reserve_before_send=reserved.append,
        settle_after_response=lambda sid, raw, result: settled.append((sid, result["usage"])))
    assert len(sent) == len(reserved) == len(settled) == 1
    assert outcome["stop_reason"] == "REQUEST_OR_USAGE_UNKNOWN"
    assert outcome["remaining_samples"] == ["second"]
