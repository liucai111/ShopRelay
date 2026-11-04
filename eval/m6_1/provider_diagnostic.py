"""Safe metadata classification for the one-shot DeepSeek diagnostic response."""
from __future__ import annotations

from typing import Any


NORMAL_FINISH_REASONS = {"stop"}
PROBE_MESSAGE = "Reply with only OK."


def build_provider_probe_parameters(*, model: str) -> dict:
    """Fixed harmless request shape; callers cannot override capability or budget fields."""
    if model != "deepseek-flash":
        raise ValueError("PROVIDER_PROBE_MODEL_NOT_ALLOWLISTED")
    return {
        "model": model,
        "messages": [{"role": "user", "content": PROBE_MESSAGE}],
        "max_tokens": 64,
        "stream": False,
        "extra_body": {"thinking": {"type": "disabled"}},
    }


def classify_chat_completion(response: Any) -> dict:
    """Return bounded response metadata; never expose reasoning or tool payloads."""
    choices = getattr(response, "choices", None)
    if not isinstance(choices, (list, tuple)):
        return {
            "classification": "RESPONSE_CONTRACT_ERROR",
            "choices_count": None,
            "finish_reasons": [],
            "content_field_states": [],
            "content_lengths": [],
            "diagnostic_text": None,
            "reasoning_content_present": [],
            "reasoning_content_lengths": [],
            "tool_calls_present": [],
            "usage": _usage(getattr(response, "usage", None)),
            "response_model": _safe_text(getattr(response, "model", None), 128),
        }

    finish_reasons: list[str | None] = []
    content_states: list[str] = []
    content_lengths: list[int | None] = []
    reasoning_present: list[bool] = []
    reasoning_lengths: list[int | None] = []
    tool_calls_present: list[bool] = []
    diagnostic_text = None
    for choice in choices:
        finish = getattr(choice, "finish_reason", None)
        finish_reasons.append(_safe_text(finish, 64) if finish is not None else None)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None) if message is not None else None
        if message is None:
            content_states.append("MESSAGE_MISSING")
            content_lengths.append(None)
        elif content is None:
            content_states.append("NULL")
            content_lengths.append(None)
        elif isinstance(content, str):
            content_states.append("EMPTY" if not content else "NONEMPTY")
            content_lengths.append(len(content))
            if diagnostic_text is None and content:
                diagnostic_text = content[:96]
        else:
            content_states.append("WRONG_TYPE")
            content_lengths.append(None)
        reasoning = getattr(message, "reasoning_content", None) if message is not None else None
        reasoning_present.append(reasoning is not None)
        reasoning_lengths.append(len(reasoning) if isinstance(reasoning, str) else None)
        calls = getattr(message, "tool_calls", None) if message is not None else None
        tool_calls_present.append(bool(calls))

    usage = _usage(getattr(response, "usage", None))
    any_length = any(reason == "length" for reason in finish_reasons)
    normal_nonempty = any(reason in NORMAL_FINISH_REASONS and state == "NONEMPTY"
        for reason, state in zip(finish_reasons, content_states))
    if any_length:
        classification = "RESPONSE_RECEIVED_LENGTH_TRUNCATED"
    elif normal_nonempty and usage["status"] == "RECORDED":
        classification = "NORMAL_NONEMPTY_COMPLETION"
    elif not choices:
        classification = "RESPONSE_CONTRACT_ERROR"
    else:
        classification = "RESPONSE_RECEIVED_EMPTY_OR_NONSTANDARD_CONTENT"
    return {
        "classification": classification,
        "choices_count": len(choices),
        "finish_reasons": finish_reasons,
        "content_field_states": content_states,
        "content_lengths": content_lengths,
        "diagnostic_text": diagnostic_text,
        "reasoning_content_present": reasoning_present,
        "reasoning_content_lengths": reasoning_lengths,
        "tool_calls_present": tool_calls_present,
        "usage": usage,
        "response_model": _safe_text(getattr(response, "model", None), 128),
    }


def _usage(value: Any) -> dict:
    if value is None:
        return {"status": "UNKNOWN", "prompt_tokens": None,
            "completion_tokens": None, "total_tokens": None}
    prompt = getattr(value, "prompt_tokens", None)
    completion = getattr(value, "completion_tokens", None)
    total = getattr(value, "total_tokens", None)
    numbers = (prompt, completion, total)
    if any(isinstance(item, bool) or not isinstance(item, int) or item < 0 for item in numbers):
        return {"status": "UNKNOWN", "prompt_tokens": None,
            "completion_tokens": None, "total_tokens": None}
    return {"status": "RECORDED", "prompt_tokens": prompt,
        "completion_tokens": completion, "total_tokens": total}


def _safe_text(value: Any, limit: int) -> str | None:
    if not isinstance(value, str):
        return None
    return "".join(ch for ch in value[:limit] if ch.isprintable())
