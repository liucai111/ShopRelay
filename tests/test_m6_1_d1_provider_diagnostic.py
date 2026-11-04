from types import SimpleNamespace

import pytest

from eval.m6_1.provider_diagnostic import (
    build_provider_probe_parameters,
    classify_chat_completion,
)
from scripts.run_m6_1_d1_provider_diagnostic_r6 import _proxy_check


def _response(*, finish="stop", content="OK", reasoning=None, tool_calls=None, usage=None):
    message = SimpleNamespace(content=content, reasoning_content=reasoning, tool_calls=tool_calls)
    choice = SimpleNamespace(finish_reason=finish, message=message)
    return SimpleNamespace(choices=[choice], model="deepseek-flash", usage=usage)


def test_probe_request_disables_thinking_and_uses_bounded_output():
    request = build_provider_probe_parameters(model="deepseek-flash")
    assert request["max_tokens"] == 64
    assert request["stream"] is False
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["messages"] == [{"role": "user", "content": "Reply with only OK."}]
    with pytest.raises(ValueError, match="NOT_ALLOWLISTED"):
        build_provider_probe_parameters(model="other-model")


def test_probe_classifies_normal_nonempty_completion_and_redacts_reasoning_body():
    result = classify_chat_completion(_response(
        content="OK", reasoning="do not preserve this", usage=SimpleNamespace(
            prompt_tokens=4, completion_tokens=1, total_tokens=5)))
    assert result["classification"] == "NORMAL_NONEMPTY_COMPLETION"
    assert result["choices_count"] == 1
    assert result["finish_reasons"] == ["stop"]
    assert result["content_field_states"] == ["NONEMPTY"]
    assert result["content_lengths"] == [2]
    assert result["diagnostic_text"] == "OK"
    assert result["reasoning_content_present"] == [True]
    assert result["reasoning_content_lengths"] == [20]
    assert "do not preserve this" not in str(result)
    assert result["tool_calls_present"] == [False]
    assert result["usage"]["total_tokens"] == 5


def test_probe_classifies_length_truncation_even_if_partial_content_exists():
    result = classify_chat_completion(_response(finish="length", content="O",
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1, total_tokens=5)))
    assert result["classification"] == "RESPONSE_RECEIVED_LENGTH_TRUNCATED"


@pytest.mark.parametrize("content,state", [(None, "NULL"), ("", "EMPTY")])
def test_probe_does_not_treat_empty_content_as_usable(content, state):
    result = classify_chat_completion(_response(content=content,
        usage=SimpleNamespace(prompt_tokens=4, completion_tokens=1, total_tokens=5)))
    assert result["classification"] == "RESPONSE_RECEIVED_EMPTY_OR_NONSTANDARD_CONTENT"
    assert result["content_field_states"] == [state]


def test_probe_keeps_missing_usage_unknown_and_contract_errors_distinct():
    missing_usage = classify_chat_completion(_response(content="OK", usage=None))
    assert missing_usage["usage"]["status"] == "UNKNOWN"
    assert missing_usage["classification"] == "RESPONSE_RECEIVED_EMPTY_OR_NONSTANDARD_CONTENT"
    malformed = classify_chat_completion(SimpleNamespace(choices=None, model="deepseek-flash", usage=None))
    assert malformed["classification"] == "RESPONSE_CONTRACT_ERROR"


def test_probe_proxy_check_confirms_7892_and_no_proxy_does_not_bypass(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:7892")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    result = _proxy_check()
    assert result["httpx_selected"] == {"scheme": "http", "host": "127.0.0.1", "port": 7892}
    assert result["no_proxy_bypasses_provider"] is False
    assert all(value == "http://127.0.0.1:7892" for value in result["variables"].values())


def test_probe_proxy_check_rejects_no_proxy_provider_bypass(monkeypatch):
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:7892")
    monkeypatch.setenv("NO_PROXY", "api.deepseek.com")
    monkeypatch.setenv("no_proxy", "api.deepseek.com")
    with pytest.raises(RuntimeError, match="NO_PROXY_BYPASSES_PROVIDER"):
        _proxy_check()
