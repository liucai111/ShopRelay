import json
import shutil
import tempfile
from pathlib import Path

import pytest
from qdrant_client import QdrantClient

from app.m2.retrieval import IndexRegistry, RetrievalConfig, RetrievalService, RetrievalUnavailable, config_from_environment


@pytest.fixture
def tmp_path(tmp_path_factory):
    # Reuse the project's accessible test factory on Windows; tempfile.mkdtemp
    # can create a directory that this sandbox cannot subsequently write into.
    # Cleanup remains restricted to this fresh test directory.
    path=tmp_path_factory.mktemp("shoprelay-m22-test")
    try: yield path
    finally: shutil.rmtree(path,ignore_errors=True)


class TinyEncoder:
    def get_sentence_embedding_dimension(self): return 3
    def encode(self, texts, **kwargs):
        rows=[]
        for text in texts:
            low=text.lower()
            rows.append([1.0 if any(w in low for w in ("退款","refund")) else 0.0,
                         1.0 if any(w in low for w in ("退货","return")) else 0.0,
                         1.0 if any(w in low for w in ("物流","签收","delivery")) else 0.0])
        import numpy as np
        return np.asarray(rows,dtype="float32")


class TinyRanker:
    def predict(self, pairs, **kwargs):
        return [2.0 if "退款" in doc else 1.0 for _,doc in pairs]


class BrokenRanker:
    def predict(self,*args,**kwargs): raise RuntimeError("injected reranker failure")


def qclient(tmp_path): return QdrantClient(":memory:")


def test_four_retrieval_modes_emit_rank_provenance_and_stable_rrf(tmp_path):
    q=qclient(tmp_path)
    service=RetrievalService(config=RetrievalConfig(mode="bm25", output_top_k=5),
        encoder=TinyEncoder(),ranker=TinyRanker(),qdrant_client=q)
    query="退款条件与退货申请"
    bm=service.retrieve(query,mode="bm25")
    dense=service.retrieve(query,mode="vector")
    fused=service.retrieve(query,mode="rrf")
    ranked=service.retrieve(query,mode="rrf_reranker")
    assert bm["evidence"] and bm["evidence"][0]["score_type"]=="bm25_raw"
    assert dense["evidence"] and dense["evidence"][0]["score_type"]=="cosine_similarity"
    assert all("channel_ranks" in row and row["score_type"]=="rrf" for row in fused["evidence"])
    assert ranked["models"]["reranker"]
    assert ranked["diagnostics"]["rrf_fusion_ms"] >= 0
    assert ranked["diagnostics"]["embedding_model_cache_hit"] is True
    assert ranked["diagnostics"]["reranker_model_cache_hit"] is True
    assert ranked["diagnostics"]["qdrant_client_cache_hit"] is True
    assert ranked["diagnostics"]["embedding_query_ms"] >= 0
    assert ranked["diagnostics"]["qdrant_query_ms"] >= 0
    assert [r["chunk_id"] for r in fused["evidence"]] == [r["chunk_id"] for r in service.retrieve(query,mode="rrf")["evidence"]]
    assert len({r["chunk_id"] for r in fused["evidence"]}) == len(fused["evidence"])
    q.close()


def test_warmup_reuses_models_and_only_validates_existing_vector_collection(tmp_path):
    client = qclient(tmp_path)
    service = RetrievalService(config=RetrievalConfig(mode="vector"), encoder=TinyEncoder(),
                               ranker=TinyRanker(), qdrant_client=client)
    try:
        service.retrieve("退货政策", mode="vector")
        result = service.warmup()
        assert result["status"] == "ready"
        assert result["vector_count"] == result["expected_vector_count"]
        assert result["warmup_total_ms"] >= 0
        assert service._warmup_result == result

        missing = RetrievalService(config=RetrievalConfig(mode="vector"), encoder=TinyEncoder(),
                                   ranker=TinyRanker(), qdrant_client=qclient(tmp_path))
        with pytest.raises(RetrievalUnavailable) as exc:
            missing.warmup()
        assert exc.value.code == "VECTOR_SNAPSHOT_UNAVAILABLE"
        assert missing.client.collection_exists(missing.collection_name(missing.pin_view().fingerprint)) is False
        missing.client.close()
    finally:
        client.close()


def test_retrieval_config_process_environment_overrides_local_file(tmp_path):
    local=tmp_path/".env.m0.local"
    local.write_text("SHOPRELAY_RETRIEVAL_MODE=rrf\nSHOPRELAY_RETRIEVAL_SPARSE_TOP_K=7\nQDRANT_URL=http://127.0.0.1:6333\nSHOPRELAY_QDRANT_MODE=service\n",encoding="utf-8")
    config=config_from_environment({"SHOPRELAY_RETRIEVAL_MODE":"vector"},local)
    assert config.mode=="vector" and config.sparse_top_k==7
    assert config.qdrant_mode=="service" and config.qdrant_url=="http://127.0.0.1:6333"
    assert config.config_hash==config_from_environment({"SHOPRELAY_RETRIEVAL_MODE":"vector"},local).config_hash


def test_rrf_degrades_to_bm25_and_all_channels_failure_is_explicit(tmp_path,monkeypatch):
    service=RetrievalService(config=RetrievalConfig(mode="rrf"), encoder=TinyEncoder(),
        qdrant_client=object())
    result=service.retrieve("退款政策",mode="rrf")
    assert result["degraded"] and result["failures"] and result["evidence"]
    empty_fallback=RetrievalService(config=RetrievalConfig(mode="rrf"),encoder=TinyEncoder(),qdrant_client=object())
    empty=empty_fallback.retrieve("unrelated query")
    assert empty["degraded"] and empty["evidence"]==[]
    broken=RetrievalService(config=RetrievalConfig(mode="vector"),encoder=TinyEncoder(),qdrant_client=object())
    with pytest.raises(RetrievalUnavailable): broken.retrieve("退款政策")
    import app.m2.retrieval as module
    class BrokenBM25:
        def __init__(self,*args,**kwargs): raise RuntimeError("injected BM25 failure")
    monkeypatch.setattr(module,"BM25",BrokenBM25)
    all_broken=RetrievalService(config=RetrievalConfig(mode="rrf"),encoder=TinyEncoder(),qdrant_client=object())
    with pytest.raises(RetrievalUnavailable): all_broken.retrieve("退款政策")


def test_reranker_failure_returns_real_fused_candidates_with_degraded_trace(tmp_path):
    client=qclient(tmp_path)
    try:
        service=RetrievalService(config=RetrievalConfig(mode="rrf_reranker"),encoder=TinyEncoder(),ranker=BrokenRanker(),qdrant_client=client)
        result=service.retrieve("退款政策")
        assert result["degraded"] and result["diagnostics"]["reranker_skipped"]
        assert result["evidence"] and all(row["score_type"]=="rrf" for row in result["evidence"])
    finally: client.close()


def test_irrelevant_zero_similarity_vector_query_returns_no_evidence(tmp_path):
    client=qclient(tmp_path)
    try:
        service=RetrievalService(config=RetrievalConfig(mode="vector"),encoder=TinyEncoder(),qdrant_client=client)
        result=service.retrieve("unrelated query")
        assert result["evidence"]==[]
    finally: client.close()


def test_embedding_dimension_mismatch_and_missing_snapshot_fail_closed(tmp_path):
    client=qclient(tmp_path)
    initial=RetrievalService(config=RetrievalConfig(mode="vector"),encoder=TinyEncoder(),qdrant_client=client)
    initial.retrieve("退款政策")
    class WrongDimension(TinyEncoder):
        def get_sentence_embedding_dimension(self): return 4
    mismatch=RetrievalService(config=RetrievalConfig(mode="vector"),encoder=WrongDimension(),qdrant_client=client)
    with pytest.raises(RetrievalUnavailable) as exc: mismatch.retrieve("退款政策")
    assert exc.value.code=="EMBEDDING_DIMENSION_MISMATCH"
    registry=IndexRegistry(tmp_path/"registry",vector_builder=lambda index,fp:None,vector_validator=lambda index,fp:None)
    with pytest.raises(RetrievalUnavailable): registry.activate("missing-fingerprint")
    client.close()


def test_versioned_index_build_failure_keeps_pointer_and_rollback(tmp_path):
    policies=Path("knowledge/policies.json")
    registry=IndexRegistry(tmp_path / "registry",vector_builder=lambda index,fp: None,vector_validator=lambda index,fp: None)
    first=registry.build(policies)
    assert registry.current()[1] == first
    altered=json.loads(policies.read_text(encoding="utf-8")); altered["knowledge_version"]="test-next"
    altered_path=tmp_path / "policies-next.json"; altered_path.write_text(json.dumps(altered,ensure_ascii=False),encoding="utf-8")
    with pytest.raises(RuntimeError): registry.build(altered_path,fail_before_switch=True)
    assert registry.current()[1] == first
    second=registry.build(altered_path)
    assert second != first and registry.current()[0]["knowledge_version"]=="test-next"
    registry.activate(first)
    assert registry.current()[1] == first


def test_expired_chunks_never_returned_by_bm25(tmp_path):
    service=RetrievalService(config=RetrievalConfig(mode="bm25"))
    result=service.retrieve("质量问题退货",evaluation_at="2026-08-01T00:00:00Z")
    assert all(x["effective_to"] is None or "2026-08-01T00:00:00Z" < x["effective_to"] for x in result["evidence"])


def test_bm25_cache_reuses_same_policy_view_across_live_timestamps():
    service = RetrievalService(config=RetrievalConfig(mode="bm25"))
    for number in range(80):
        at = f"2026-10-19T00:00:00.{number:06d}Z"
        service.retrieve("质量问题如何退货", evaluation_at=at)
    assert len(service._bm25_cache) == 1
    key = next(iter(service._bm25_cache))
    assert key[0] and key[1]


def test_bm25_exception_without_detail_maps_to_bm25_unavailable(monkeypatch):
    import app.m2.retrieval as module
    class BrokenBM25:
        def __init__(self, *args, **kwargs):
            raise RuntimeError("injected failure without a detail attribute")
    monkeypatch.setattr(module, "BM25", BrokenBM25)
    service = RetrievalService(config=RetrievalConfig(mode="bm25"))
    with pytest.raises(RetrievalUnavailable) as exc:
        service.retrieve("质量问题如何退货")
    assert exc.value.code == "BM25_UNAVAILABLE"
    assert exc.value.detail == "RuntimeError"


def test_vector_snapshot_contains_history_but_filters_by_requested_effective_time(tmp_path):
    client=qclient(tmp_path)
    service=RetrievalService(config=RetrievalConfig(mode="vector"),encoder=TinyEncoder(),qdrant_client=client,
                             collection_namespace="m23_test")
    old=service.retrieve("退货政策质量问题",evaluation_at="2026-02-01T00:00:00Z")
    current=service.retrieve("退货政策质量问题",evaluation_at="2026-08-01T00:00:00Z")
    old_id="policies/return-standard@2026-01#quality"
    assert old_id in [x["chunk_id"] for x in old["evidence"]]
    assert old_id not in [x["chunk_id"] for x in current["evidence"]]
    assert client.count("m23_test_"+old["index_fingerprint"][:24],exact=True).count == 10
    client.close()
