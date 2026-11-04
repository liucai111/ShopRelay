"""One policy-answering agent with one BM25 retrieval call and cited extractive output."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .bm25 import BM25
from .config import create_deepseek_client, load_settings
from .data import policy_at


class SingleAgentBaseline:
    def __init__(self, index_path: Path):
        self.index = json.loads(index_path.read_text(encoding="utf-8"))

    def answer(self, query: str, evaluation_at: str, top_k: int = 5) -> dict:
        eligible = policy_at(self.index["chunks"], evaluation_at)
        evidence = BM25(eligible).search(query, top_k)
        if not evidence:
            return {"status": "INSUFFICIENT_EVIDENCE", "answer": "未找到可核对的政策条款，请转人工核实。", "citations": [], "knowledge_version": self.index["knowledge_version"], "generation_mode": "extractive_offline"}
        best = evidence[0]
        return {"status": "EXTRACTIVE_EVIDENCE", "answer": f"相关模拟政策原文：{best['content']} [{best['chunk_id']}]。请结合订单实时状态核对，不代表已批准申请。", "citations": [e["chunk_id"] for e in evidence], "knowledge_version": self.index["knowledge_version"], "generation_mode": "extractive_offline"}

    def answer_llm(self, query: str, evaluation_at: str, top_k: int = 5, client=None) -> dict:
        """One DeepSeek call after BM25; refusal is explicit on model or citation failure."""
        evidence = BM25(policy_at(self.index["chunks"], evaluation_at)).search(query, top_k)
        if not evidence:
            return self.answer(query, evaluation_at, top_k)
        settings = load_settings(require_api_key=client is None)
        node = settings.nodes["policy_answer"]
        if client is None:
            client = create_deepseek_client(settings, node)
        model = settings.model
        context = [{"chunk_id": c["chunk_id"], "content": c["content"]} for c in evidence]
        started = time.perf_counter()
        try:
            response = client.chat.completions.create(model=model, messages=[{"role": "system", "content": "你是模拟商家政策单 Agent 基线。只用给定政策证据回答。输出 json 对象，格式为 {\"answer\":\"...\",\"citations\":[\"chunk_id\"]}。不知道就说证据不足；不得编造订单事实或批准结果。"}, {"role": "user", "content": json.dumps({"question": query, "evidence": context}, ensure_ascii=False)}], response_format={"type": "json_object"}, extra_body={"thinking": {"type": node.thinking}}, max_tokens=node.max_tokens)
            payload = json.loads(response.choices[0].message.content or "")
            answer = payload["answer"]
            citations = payload["citations"]
            allowed = {c["chunk_id"] for c in evidence}
            if not isinstance(answer, str) or not answer.strip() or not isinstance(citations, list) or not citations or not all(isinstance(c, str) and c in allowed for c in citations):
                raise ValueError("invalid model answer or citations")
            return {"status": "MODEL_ANSWER_UNREVIEWED", "answer": answer, "citations": citations, "retrieved": [c["chunk_id"] for c in evidence], "knowledge_version": self.index["knowledge_version"], "generation_mode": f"deepseek_{node.thinking}", "model": response.model, "config_source": {"api_key": settings.api_key_source, "base_url": settings.base_url_source, "model": settings.model_source}, "usage": response.usage.model_dump() if response.usage else None, "latency_ms": round((time.perf_counter() - started) * 1000)}
        except Exception as exc:
            return {"status": "MODEL_UNAVAILABLE", "answer": "模型回答未通过或服务暂不可用，请转人工核对。", "citations": [], "retrieved": [c["chunk_id"] for c in evidence], "knowledge_version": self.index["knowledge_version"], "generation_mode": f"deepseek_{node.thinking}", "model": model, "error_type": type(exc).__name__, "latency_ms": round((time.perf_counter() - started) * 1000)}
