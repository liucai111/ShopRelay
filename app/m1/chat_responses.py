"""Pure response helpers shared by the legacy executor and G1 adapters."""
from __future__ import annotations


def direct_response(route: dict, *, latency_ms: int = 0) -> dict:
    answer = route.get("clarification_question") or "你好，请告诉我你需要查询或了解的内容。"
    return {"answer": answer, "answer_kind": "DIRECT", "citations": [], "latency_ms": latency_ms}


def refusal_response(*, reason_code: str | None = None, latency_ms: int = 0) -> dict:
    if reason_code == "THIRD_PARTY_ORDER_ACCESS":
        answer = ("出于隐私保护，我不能查询、确认或披露他人订单信息。"
                  "请使用已登录并完成本人身份验证的账户查询自己的订单。")
    elif reason_code == "SECRET_CREDENTIAL_DISCLOSURE":
        answer = "出于安全要求，我不能披露密钥、密码或其他敏感凭据。"
    elif reason_code == "UNAUTHORIZED_OR_MIXED_SENSITIVE_REQUEST":
        answer = "出于隐私与安全要求，我不能协助访问未经授权的信息或处理混合的敏感请求。"
    elif reason_code == "UNSAFE_REQUEST_NOT_ISOLATABLE":
        answer = "我不能按这项请求处理；请改为提出不要求绕过保护措施或泄露敏感信息的正常任务。"
    else:
        answer = "我不能协助执行这项受限请求。"
    return {"answer": answer, "answer_kind": "REFUSAL", "citations": [], "latency_ms": latency_ms}
