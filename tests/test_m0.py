import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from app.m0.baseline import SingleAgentBaseline
from app.m0.bm25 import BM25, tokens
from app.m0.data import ROOT, ingest, policy_at, seed
from app.m0.evaluate import evaluate


@pytest.fixture
def tmp_path():
    # Windows sandbox denies pytest's mode-0700 temporary directories.
    path = ROOT / "data" / "test-tmp" / uuid.uuid4().hex
    path.mkdir(parents=True)
    return path


def test_seed_repeatable_and_owner_rows(tmp_path):
    db = tmp_path / "business.sqlite3"
    first = seed(db)
    second = seed(db)
    assert first["sha256"] == second["sha256"]
    assert second["status"] == "already_applied"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 4
        assert conn.execute("SELECT user_id FROM orders WHERE order_id='ORD-OTHER'").fetchone()[0] == "u_other"
        conn.execute("INSERT INTO refunds VALUES ('R1','ORD-PAID','SUBMITTED',15900,'one','2026-10-18T00:00:00Z')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("INSERT INTO refunds VALUES ('R2','ORD-PAID','APPROVED',15900,'different reason','2026-10-18T00:00:01Z')")
        assert conn.execute("SELECT COUNT(*) FROM refunds").fetchone()[0] == 1


def test_ingest_determinism_and_policy_boundary(tmp_path):
    path = tmp_path / "index.json"
    first = ingest(path)
    contents = path.read_bytes()
    second = ingest(path)
    assert contents == path.read_bytes()
    assert first == second
    chunks = json.loads(contents)["chunks"]
    old = policy_at(chunks, "2026-06-30T23:59:59Z")
    new = policy_at(chunks, "2026-07-01T00:00:00Z")
    assert any(c["chunk_id"] == "policies/return-standard@2026-01#no-reason" for c in old)
    assert not any(c["chunk_id"] == "policies/return-standard@2026-07#no-reason" for c in old)
    assert any(c["chunk_id"] == "policies/return-standard@2026-07#no-reason" for c in new)
    assert not any(c["chunk_id"] == "policies/return-standard@2026-01#no-reason" for c in new)


def test_policy_answer_cites_live_version(tmp_path):
    path = tmp_path / "index.json"
    ingest(path)
    result = SingleAgentBaseline(path).answer("定制商品无理由退货", "2026-10-18T00:00:00Z")
    assert result["status"] == "EXTRACTIVE_EVIDENCE"
    assert result["citations"]
    assert all("@2026-01#" not in c for c in result["citations"] if "return-standard" in c)
    assert result["citations"][0] in result["answer"]


def test_deepseek_provider_failure_returns_explicit_unavailable(tmp_path):
    from types import SimpleNamespace

    path = tmp_path / "index.json"
    ingest(path)

    class FailingCompletions:
        def create(self, **kwargs):
            raise RuntimeError("synthetic provider failure")

    client = SimpleNamespace(chat=SimpleNamespace(completions=FailingCompletions()))
    result = SingleAgentBaseline(path).answer_llm("定制商品质量问题如何申请退货", "2026-10-18T00:00:00Z", client=client)
    assert result["status"] == "MODEL_UNAVAILABLE"
    assert result["citations"] == []
    assert result["error_type"] == "RuntimeError"
    assert "synthetic provider failure" not in json.dumps(result, ensure_ascii=False)


def test_bm25_is_lexical_and_no_hash_semantic_claim():
    assert "退货" in tokens("可以退货吗")
    chunks = [{"chunk_id": "a", "title": "退货", "content": "定制商品不能无理由退货"}, {"chunk_id": "b", "title": "物流", "content": "快递已发货"}]
    assert BM25(chunks).search("定制商品退货")[0]["chunk_id"] == "a"


def test_raw_runs_append_only(tmp_path):
    index = tmp_path / "index.json"
    ingest(index)
    raw_dir = tmp_path / "raw"
    one = evaluate(index, ROOT / "eval/dataset/policy-initial.json", raw_dir)
    two = evaluate(index, ROOT / "eval/dataset/policy-initial.json", raw_dir)
    assert one["run_id"] != two["run_id"]
    assert len(list(raw_dir.glob("*.json"))) == 2
    assert one["metrics"]["n"] == 12
