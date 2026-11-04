"""Chinese jieba-tokenized BM25 baseline, not semantic search."""
from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter

import jieba


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).lower()
    return [part.strip() for part in jieba.lcut(text, cut_all=False) if re.fullmatch(r"[a-z0-9]+|[\u4e00-\u9fff]+", part.strip())]


class BM25:
    def __init__(self, chunks: list[dict], k1: float = 1.5, b: float = 0.75):
        self.chunks = chunks
        self.k1 = k1
        self.b = b
        self.freq = [Counter(tokens(c["title"] + " " + c["content"])) for c in chunks]
        self.lengths = [sum(f.values()) for f in self.freq]
        self.avgdl = sum(self.lengths) / max(1, len(chunks))
        self.df = Counter(term for freq in self.freq for term in freq)

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not 1 <= top_k <= 100:
            raise ValueError("top_k out of range")
        terms = set(tokens(query))
        n = len(self.chunks)
        scored = []
        for chunk, freq, length in zip(self.chunks, self.freq, self.lengths):
            score = 0.0
            for term in terms:
                if term not in freq:
                    continue
                idf = math.log(1 + (n - self.df[term] + 0.5) / (self.df[term] + 0.5))
                tf = freq[term]
                score += idf * tf * (self.k1 + 1) / (tf + self.k1 * (1 - self.b + self.b * length / max(self.avgdl, 1)))
            if score > 0:
                scored.append({**chunk, "score": round(score, 8)})
        return sorted(scored, key=lambda c: (-c["score"], c["chunk_id"]))[:top_k]
