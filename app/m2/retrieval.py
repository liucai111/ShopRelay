"""Versioned policy retrieval with explicit sparse/dense degradation semantics."""
from __future__ import annotations

import json
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
import numpy as np

from app.m0.bm25 import BM25
from app.m0.config import LOCAL_CONFIG, parse_env_file
from app.m0.data import ROOT, digest, ingest, policy_at

SMALL_REV = "7999e1d3359715c523056ef9478215996d62a620"
RERANKER_REV = "2cfc18c9415c912f9d8155881c133215df768a70"
SMALL_DIR = ROOT / ".cache/huggingface/hub/models--BAAI--bge-small-zh-v1.5/snapshots" / SMALL_REV
RERANKER_DIR = ROOT / ".cache/huggingface/hub/models--BAAI--bge-reranker-base/snapshots" / RERANKER_REV
MODES = {"bm25", "vector", "rrf", "rrf_reranker", "hybrid_rerank"}
BM25_CACHE_MAX_ENTRIES = 32


@dataclass(frozen=True)
class RetrievalView:
    """One immutable knowledge/index/time view shared by every hop in a run."""
    index: dict
    fingerprint: str
    evaluation_at: str


def config_from_environment(environ=None, local_path: Path = LOCAL_CONFIG) -> "RetrievalConfig":
    env=os.environ if environ is None else environ
    try: local=parse_env_file(local_path.read_text(encoding="utf-8"))
    except FileNotFoundError: local={}
    def get(name,default): return env[name] if name in env else local.get(name,default)
    return RetrievalConfig(version=get("SHOPRELAY_RETRIEVAL_CONFIG_VERSION","m2-r-v1"),
        mode=get("SHOPRELAY_RETRIEVAL_MODE","bm25"),
        sparse_top_k=int(get("SHOPRELAY_RETRIEVAL_SPARSE_TOP_K","5")),
        dense_top_k=int(get("SHOPRELAY_RETRIEVAL_DENSE_TOP_K","5")),
        output_top_k=int(get("SHOPRELAY_RETRIEVAL_OUTPUT_TOP_K","5")),
        rrf_constant=int(get("SHOPRELAY_RETRIEVAL_RRF_CONSTANT","60")),
        rerank_candidate_limit=int(get("SHOPRELAY_RETRIEVAL_RERANK_CANDIDATE_LIMIT","10")),
        timeout_seconds=float(get("SHOPRELAY_RETRIEVAL_TIMEOUT_SECONDS","8")),
        min_vector_score=float(get("SHOPRELAY_RETRIEVAL_MIN_COSINE_SCORE","0.20")),
        qdrant_mode=get("SHOPRELAY_QDRANT_MODE","local"),qdrant_url=get("QDRANT_URL","") or None)


@dataclass(frozen=True)
class RetrievalConfig:
    version: str = "m2-r-v1"
    mode: str = "bm25"
    sparse_top_k: int = 5
    dense_top_k: int = 5
    output_top_k: int = 5
    rrf_constant: int = 60
    rerank_candidate_limit: int = 10
    timeout_seconds: float = 8.0
    min_vector_score: float = 0.20
    qdrant_mode: str = "local"
    qdrant_url: str | None = None

    def __post_init__(self):
        if self.mode not in MODES: raise ValueError("unsupported retrieval mode")
        if min(self.sparse_top_k, self.dense_top_k, self.output_top_k, self.rrf_constant,
               self.rerank_candidate_limit) < 1 or self.timeout_seconds <= 0:
            raise ValueError("retrieval limits must be positive")
        if not -1 <= self.min_vector_score <= 1: raise ValueError("min_vector_score must be in [-1,1]")
        if self.qdrant_mode not in {"local", "service"}: raise ValueError("invalid Qdrant mode")
        if self.qdrant_mode == "service" and not self.qdrant_url: raise ValueError("QDRANT_URL required for service mode")

    @property
    def config_hash(self) -> str: return digest(asdict(self))


class RetrievalUnavailable(RuntimeError):
    def __init__(self, code: str, detail: str):
        self.code, self.detail = code, detail
        super().__init__(code)


def index_fingerprint(index: dict) -> str:
    return digest({"knowledge_version": index["knowledge_version"], "source_sha256": index.get("source_sha256") or digest(index["chunks"]),
                   "chunks": index["chunks"], "bm25_tokenizer": "jieba-0.42.1-default-no-stopwords-v1",
                   "embedding_model": "BAAI/bge-small-zh-v1.5", "embedding_revision": SMALL_REV,"embedding_dimension":512})


class IndexRegistry:
    """Immutable snapshots and atomic active pointer; failed builds never replace current."""
    def __init__(self, root: Path = ROOT / "data" / "retrieval_indexes", vector_builder=None, vector_validator=None):
        self.root = root
        self.snapshots = root / "snapshots"
        self.pointer = root / "current.json"
        self.vector_builder = vector_builder
        self.vector_validator = vector_validator

    def current(self, fallback: Path = ROOT / "data" / "index.json") -> tuple[dict, str]:
        if self.pointer.exists():
            fp = json.loads(self.pointer.read_text(encoding="utf-8"))["fingerprint"]
            path = self.snapshots / f"{fp}.json"
            if not path.exists(): raise RetrievalUnavailable("INDEX_VERSION_MISSING", "active snapshot missing")
            index = json.loads(path.read_text(encoding="utf-8"))
        else:
            index = json.loads(fallback.read_text(encoding="utf-8"))
        return index, index_fingerprint(index)

    def build(self, policy_path: Path, *, fail_before_switch: bool = False) -> str:
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / "build.tmp.json"
        ingest(temp, policy_path)
        index = json.loads(temp.read_text(encoding="utf-8")); temp.unlink(missing_ok=True)
        ids=[row.get("chunk_id") for row in index.get("chunks",[])]
        if not ids or len(ids)!=len(set(ids)) or any(not row.get("effective_from") or not row.get("version") for row in index["chunks"]):
            raise RetrievalUnavailable("INDEX_VALIDATION_FAILED", "duplicate or incomplete policy chunk metadata")
        fp = index_fingerprint(index)
        snapshot = self.snapshots / f"{fp}.json"
        if not snapshot.exists(): snapshot.write_text(json.dumps(index, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        if self.vector_builder:
            self.vector_builder(index, fp)
            if self.vector_validator: self.vector_validator(index,fp)
        else:
            self._build_vector(index, fp)
            self._verify_vector(index,fp)
        if fail_before_switch: raise RuntimeError("injected validation failure; active version unchanged")
        tmp_pointer = self.root / "current.tmp.json"
        tmp_pointer.write_text(json.dumps({"fingerprint": fp, "knowledge_version": index["knowledge_version"]}), encoding="utf-8")
        tmp_pointer.replace(self.pointer)
        return fp

    @staticmethod
    def _build_vector(index: dict, fingerprint: str) -> None:
        """Build and verify an isolated vector collection before pointer activation."""
        try:
            from sentence_transformers import SentenceTransformer
            from qdrant_client import QdrantClient, models
            if not (SMALL_DIR / "modules.json").exists(): raise FileNotFoundError("cached BGE-small snapshot missing")
            encoder=SentenceTransformer(str(SMALL_DIR),device="cpu",local_files_only=True)
            url=os.getenv("QDRANT_URL")
            if os.getenv("SHOPRELAY_QDRANT_MODE","local")=="service":
                if not url: raise ValueError("QDRANT_URL required")
                client=QdrantClient(url=url,timeout=8)
            else:
                path=ROOT/"data"/"qdrant_retrieval_m2"; path.mkdir(parents=True,exist_ok=True)
                client=QdrantClient(path=str(path),timeout=8)
            name="sr_"+fingerprint[:24]; dim=encoder.get_sentence_embedding_dimension()
            if client.collection_exists(name):
                info=client.get_collection(name)
                if info.config.params.vectors.size!=dim: raise ValueError("existing vector collection dimension mismatch")
            else:
                client.create_collection(name,vectors_config=models.VectorParams(size=dim,distance=models.Distance.COSINE))
                vectors=encoder.encode([x["title"]+" "+x["content"] for x in index["chunks"]],normalize_embeddings=True,convert_to_numpy=True)
                client.upload_points(name,[models.PointStruct(id=i,vector=v.tolist(),payload={
                    "chunk_id":c["chunk_id"],"knowledge_version":index["knowledge_version"],"policy_version":c["version"],
                    "effective_from":c["effective_from"],"effective_to":c["effective_to"],"index_fingerprint":fingerprint})
                    for i,(c,v) in enumerate(zip(index["chunks"],vectors))],wait=True)
            if client.count(name,exact=True).count!=len(index["chunks"]): raise ValueError("vector collection count mismatch")
            client.close()
        except Exception as exc:
            raise RetrievalUnavailable("INDEX_VECTOR_BUILD_FAILED",type(exc).__name__) from exc

    def activate(self, fingerprint: str) -> None:
        path = self.snapshots / f"{fingerprint}.json"
        if not path.exists(): raise RetrievalUnavailable("INDEX_VERSION_MISSING", "snapshot does not exist")
        index = json.loads(path.read_text(encoding="utf-8"))
        if self.vector_validator: self.vector_validator(index,fingerprint)
        else: self._verify_vector(index,fingerprint)
        temp = self.root / "current.tmp.json"
        temp.write_text(json.dumps({"fingerprint": fingerprint, "knowledge_version": index["knowledge_version"]}), encoding="utf-8")
        temp.replace(self.pointer)

    @staticmethod
    def _verify_vector(index: dict, fingerprint: str) -> None:
        try:
            from qdrant_client import QdrantClient
            if os.getenv("SHOPRELAY_QDRANT_MODE","local")=="service":
                client=QdrantClient(url=os.getenv("QDRANT_URL"),timeout=8)
            else: client=QdrantClient(path=str(ROOT/"data"/"qdrant_retrieval_m2"),timeout=8)
            name="sr_"+fingerprint[:24]
            if not client.collection_exists(name) or client.count(name,exact=True).count!=len(index["chunks"]):
                raise ValueError("vector snapshot missing or incomplete")
            points,_=client.scroll(name,limit=len(index["chunks"])+1,with_payload=["chunk_id","index_fingerprint"])
            ids={p.payload.get("chunk_id") for p in points if p.payload.get("index_fingerprint")==fingerprint}
            if ids!={c["chunk_id"] for c in index["chunks"]}: raise ValueError("vector snapshot content mismatch")
            client.close()
        except Exception as exc:
            raise RetrievalUnavailable("INDEX_VECTOR_VALIDATION_FAILED",type(exc).__name__) from exc


class RetrievalService:
    def __init__(self, index_path: Path | None = None, config: RetrievalConfig | None = None,
                 *, encoder=None, ranker=None, qdrant_client=None, registry: IndexRegistry | None = None,
                 collection_namespace: str = "sr"):
        self.index_path = index_path or ROOT / "data" / "index.json"
        self.config = config or config_from_environment()
        self.encoder, self.ranker, self.client = encoder, ranker, qdrant_client
        if not collection_namespace or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for char in collection_namespace):
            raise ValueError("collection_namespace must be a safe Qdrant identifier prefix")
        self.collection_namespace = collection_namespace
        self.registry = registry or IndexRegistry()
        self._lock = threading.RLock()
        self._inference_lock = threading.BoundedSemaphore(1)
        self._index = None
        self._fingerprint = None
        self._bm25_cache = OrderedDict()
        self._warmup_result = None

    def pin_view(self, evaluation_at: str | None = None) -> RetrievalView:
        """Capture the active snapshot once so later index switches cannot affect a run."""
        if self.registry:
            index, fingerprint = self.registry.current(self.index_path)
        else:
            index = json.loads(self.index_path.read_text(encoding="utf-8"))
            fingerprint = index_fingerprint(index)
        at = evaluation_at or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        parsed = datetime.fromisoformat(at.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError("evaluation_at must include a timezone")
        at = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return RetrievalView(index=index, fingerprint=fingerprint, evaluation_at=at)

    def _snapshot(self):
        # A retrieval call pins one immutable index object/fingerprint for all channels.
        if self.registry:
            self._index, self._fingerprint = self.registry.current(self.index_path)
        else:
            self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            self._fingerprint = index_fingerprint(self._index)
        return self._index, self._fingerprint

    def _models(self, need_ranker=False, diagnostics=None):
        with self._lock:
            try:
                if self.encoder is None:
                    import_started = time.perf_counter()
                    started = import_started
                    from sentence_transformers import SentenceTransformer
                    if diagnostics is not None:
                        diagnostics["embedding_dependency_import_ms"] = (time.perf_counter() - import_started) * 1000
                    if not (SMALL_DIR / "modules.json").exists(): raise FileNotFoundError("cached BGE-small snapshot missing")
                    started = time.perf_counter()
                    self.encoder = SentenceTransformer(str(SMALL_DIR), device="cpu", local_files_only=True)
                    if diagnostics is not None:
                        diagnostics["embedding_model_load_ms"] = (time.perf_counter() - started) * 1000
                elif diagnostics is not None:
                    diagnostics["embedding_model_cache_hit"] = True
                if need_ranker and self.ranker is None:
                    import_started = time.perf_counter()
                    started = import_started
                    from sentence_transformers import CrossEncoder
                    if diagnostics is not None:
                        diagnostics["reranker_dependency_import_ms"] = (time.perf_counter() - import_started) * 1000
                    if not (RERANKER_DIR / "model.safetensors").exists(): raise FileNotFoundError("cached reranker snapshot missing")
                    started = time.perf_counter()
                    self.ranker = CrossEncoder(str(RERANKER_DIR), device="cpu", local_files_only=True)
                    if diagnostics is not None:
                        diagnostics["reranker_model_load_ms"] = (time.perf_counter() - started) * 1000
                elif need_ranker and diagnostics is not None:
                    diagnostics["reranker_model_cache_hit"] = True
            except Exception as exc:
                if diagnostics is not None and self.encoder is None:
                    diagnostics.setdefault("embedding_model_load_ms", (time.perf_counter() - started) * 1000)
                elif diagnostics is not None and need_ranker and self.ranker is None:
                    diagnostics.setdefault("reranker_model_load_ms", (time.perf_counter() - started) * 1000)
                raise RetrievalUnavailable("EMBEDDING_UNAVAILABLE" if not need_ranker else "RERANKER_UNAVAILABLE", type(exc).__name__) from exc

    def _client(self, diagnostics=None):
        with self._lock:
            if self.client is not None:
                if diagnostics is not None: diagnostics["qdrant_client_cache_hit"] = True
                return self.client
            started = time.perf_counter()
            try:
                from qdrant_client import QdrantClient
                if self.config.qdrant_mode == "service": self.client = QdrantClient(url=self.config.qdrant_url, timeout=self.config.timeout_seconds)
                else:
                    path = ROOT / "data" / "qdrant_retrieval_m2"
                    path.mkdir(parents=True, exist_ok=True)
                    self.client = QdrantClient(path=str(path), timeout=self.config.timeout_seconds)
                if diagnostics is not None: diagnostics["qdrant_connect_ms"] = (time.perf_counter() - started) * 1000
                return self.client
            except Exception as exc:
                if diagnostics is not None: diagnostics["qdrant_connect_ms"] = (time.perf_counter() - started) * 1000
                raise RetrievalUnavailable("VECTOR_UNAVAILABLE", type(exc).__name__) from exc

    def warmup(self) -> dict:
        """Load locked local models and verify the active vector snapshot without rebuilding it."""
        started = time.perf_counter()
        diagnostics = {}
        self._models(need_ranker=True, diagnostics=diagnostics)
        client = self._client(diagnostics)
        index, fingerprint = self.registry.current(self.index_path) if self.registry else (json.loads(self.index_path.read_text(encoding="utf-8")), None)
        fingerprint = fingerprint or index_fingerprint(index)
        collection = self.collection_name(fingerprint)
        try:
            if not client.collection_exists(collection):
                raise RetrievalUnavailable("VECTOR_SNAPSHOT_UNAVAILABLE", "active vector collection is missing; warmup did not build it")
            info = client.get_collection(collection)
            expected_dim = self.encoder.get_sentence_embedding_dimension()
            if info.config.params.vectors.size != expected_dim:
                raise RetrievalUnavailable("EMBEDDING_DIMENSION_MISMATCH", "collection dimension differs")
            count = client.count(collection, exact=True).count
            if count != len(index["chunks"]):
                raise RetrievalUnavailable("INDEX_VECTOR_INCOMPLETE", "active collection count differs; warmup did not modify it")
        except RetrievalUnavailable:
            raise
        except Exception as exc:
            raise RetrievalUnavailable("VECTOR_UNAVAILABLE", type(exc).__name__) from exc
        diagnostics.update({"collection": collection, "knowledge_version": index["knowledge_version"],
                            "vector_count": count, "expected_vector_count": len(index["chunks"]),
                            "warmup_total_ms": (time.perf_counter() - started) * 1000})
        self._warmup_result = {"status": "ready", **diagnostics}
        return dict(self._warmup_result)

    def _dense(self, query, all_chunks, eligible_chunks, fingerprint, diagnostics=None):
        # Serialize first-time collection creation and CPU inference per app process.
        with self._lock:
            return self._dense_locked(query,all_chunks,eligible_chunks,fingerprint,diagnostics)

    def _dense_locked(self, query, all_chunks, eligible_chunks, fingerprint, diagnostics=None):
        diagnostics = diagnostics if diagnostics is not None else {}
        self._models(diagnostics=diagnostics)
        collection = self.collection_name(fingerprint)
        client = self._client(diagnostics)
        try:
            from qdrant_client import models
            dim = self.encoder.get_sentence_embedding_dimension()
            prepare_started = time.perf_counter()
            if client.collection_exists(collection):
                info = client.get_collection(collection)
                if info.config.params.vectors.size != dim: raise RetrievalUnavailable("EMBEDDING_DIMENSION_MISMATCH", "collection dimension differs")
            else:
                client.create_collection(collection, vectors_config=models.VectorParams(size=dim, distance=models.Distance.COSINE))
            diagnostics["qdrant_collection_ms"] = (time.perf_counter() - prepare_started) * 1000
            scan_started = time.perf_counter()
            existing,_=client.scroll(collection,limit=max(1,len(all_chunks)+1),with_payload=["chunk_id","index_fingerprint"])
            known={p.payload.get("chunk_id") for p in existing if p.payload.get("index_fingerprint")==fingerprint}
            expected={c["chunk_id"] for c in all_chunks}
            if known-expected: raise RetrievalUnavailable("INDEX_VECTOR_VERSION_MISMATCH", "collection contains unexpected chunks")
            diagnostics["qdrant_scan_ms"] = (time.perf_counter() - scan_started) * 1000
            if known != expected:
                encode_started = time.perf_counter()
                with self._inference_lock:
                    vectors = self.encoder.encode([c["title"] + " " + c["content"] for c in all_chunks], normalize_embeddings=True, convert_to_numpy=True)
                diagnostics["embedding_index_build_ms"] = (time.perf_counter() - encode_started) * 1000
                points = [models.PointStruct(id=i, vector=v.tolist(), payload={"chunk_id": c["chunk_id"], "index_fingerprint": fingerprint,
                    "knowledge_version":c.get("knowledge_version", ""),"policy_version":c["version"],"effective_from":c["effective_from"],"effective_to":c["effective_to"]}) for i,(c,v) in enumerate(zip(all_chunks,vectors))]
                upsert_started = time.perf_counter()
                client.upsert(collection, points=points, wait=True)
                diagnostics["qdrant_upsert_ms"] = (time.perf_counter() - upsert_started) * 1000
            if client.count(collection,exact=True).count != len(all_chunks): raise RetrievalUnavailable("INDEX_VECTOR_INCOMPLETE", "collection count differs from immutable snapshot")
            encode_started = time.perf_counter()
            with self._inference_lock:
                qv = self.encoder.encode([query], normalize_embeddings=True, convert_to_numpy=True)[0]
            diagnostics["embedding_query_ms"] = (time.perf_counter() - encode_started) * 1000
            query_started = time.perf_counter()
            found = client.query_points(collection, query=qv.tolist(), limit=min(self.config.dense_top_k, len(eligible_chunks)),
                query_filter=models.Filter(must=[models.FieldCondition(key="chunk_id", match=models.MatchAny(any=[c["chunk_id"] for c in eligible_chunks]))]), with_payload=True).points
            diagnostics["qdrant_query_ms"] = (time.perf_counter() - query_started) * 1000
            by_id = {c["chunk_id"]: c for c in eligible_chunks}
            rows=[{**by_id[p.payload["chunk_id"]], "score": float(p.score), "score_type":"cosine_similarity"}
                  for p in found if p.payload.get("index_fingerprint")==fingerprint and p.payload.get("chunk_id") in by_id
                  and float(p.score)>=self.config.min_vector_score]
            rows.sort(key=lambda r:(-r["score"],r["chunk_id"]))
            return [{**r,"rank":i+1} for i,r in enumerate(rows)]
        except RetrievalUnavailable: raise
        except Exception as exc: raise RetrievalUnavailable("VECTOR_UNAVAILABLE", type(exc).__name__) from exc

    def retrieve(self, query: str, *, evaluation_at: str | None = None, mode: str | None = None,
                 view: RetrievalView | None = None) -> dict:
        mode = mode or self.config.mode
        # `hybrid_rerank` is the online policy strategy name. Keep historical
        # low-level retrieval modes for evaluators and old Run metadata.
        if mode == "hybrid_rerank":
            mode = "rrf_reranker"
        if mode not in MODES: raise ValueError("unsupported retrieval mode")
        started = time.perf_counter()
        pinned = view or self.pin_view(evaluation_at)
        if view is not None and evaluation_at is not None:
            requested = datetime.fromisoformat(evaluation_at.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
            if requested != view.evaluation_at:
                raise ValueError("evaluation_at differs from the pinned retrieval view")
        index, fingerprint, at = pinned.index, pinned.fingerprint, pinned.evaluation_at
        chunks = policy_at(index["chunks"], at)
        channels, failures, diagnostics = {}, [], {}
        if mode in {"bm25", "rrf", "rrf_reranker"}:
            try:
                # Effective-time changes that produce the same chunk set share a cache
                # entry; entries are bounded and remain separated by immutable index.
                key=(fingerprint,tuple(row["chunk_id"] for row in chunks))
                with self._lock:
                    bm=self._bm25_cache.get(key)
                    if bm is None:
                        bm=BM25(chunks); self._bm25_cache[key]=bm
                        while len(self._bm25_cache) > BM25_CACHE_MAX_ENTRIES:
                            self._bm25_cache.popitem(last=False)
                    else:
                        self._bm25_cache.move_to_end(key)
                t=time.perf_counter(); channels["bm25"]=[{**r,"score_type":"bm25_raw","rank":i+1} for i,r in enumerate(bm.search(query,self.config.sparse_top_k))]; diagnostics["bm25_ms"]=(time.perf_counter()-t)*1000
            except Exception as exc: failures.append({"channel":"bm25","error":"BM25_UNAVAILABLE","detail":getattr(exc,"detail",None) or type(exc).__name__})
        if mode in {"vector", "rrf", "rrf_reranker"}:
            try:
                t=time.perf_counter(); channels["vector"]=self._dense(query,index["chunks"],chunks,fingerprint,diagnostics); diagnostics["vector_ms"]=(time.perf_counter()-t)*1000
            except RetrievalUnavailable as exc: failures.append({"channel":"vector","error":exc.code,"detail":exc.detail})
        if mode == "bm25" or (mode in {"rrf","rrf_reranker"} and "vector" not in channels):
            ranked=channels.get("bm25",[])
        elif mode == "vector" or (mode in {"rrf","rrf_reranker"} and "bm25" not in channels):
            ranked=channels.get("vector",[])
        else:
            ranked=[]
            if mode in {"rrf","rrf_reranker"}:
                fusion_started = time.perf_counter()
                maps={k:{r["chunk_id"]:(i+1) for i,r in enumerate(v)} for k,v in channels.items()}
                ids=set().union(*(set(v) for v in maps.values()))
                score={cid:sum(1/(self.config.rrf_constant+rank[cid]) for rank in maps.values() if cid in rank) for cid in ids}
                source={r["chunk_id"]:r for values in channels.values() for r in values}
                ranked=[{**source[cid],"score":score[cid],"score_type":"rrf","channel_ranks":{k:maps[k].get(cid) for k in maps}} for cid in sorted(ids,key=lambda x:(-score[x],x))]
                diagnostics["rrf_fusion_ms"] = (time.perf_counter() - fusion_started) * 1000
            else: ranked=channels.get("vector",[])
        if mode == "rrf_reranker" and ranked:
            before=[r["chunk_id"] for r in ranked[:self.config.rerank_candidate_limit]]
            try:
                self._models(need_ranker=True, diagnostics=diagnostics); candidate=ranked[:self.config.rerank_candidate_limit]
                t=time.perf_counter()
                with self._inference_lock:
                    scores=self.ranker.predict([(query,r["title"]+" "+r["content"]) for r in candidate], batch_size=8)
                if any(not math.isfinite(float(score)) for score in scores): raise ValueError("non-finite reranker score")
                diagnostics["reranker_ms"]=(time.perf_counter()-t)*1000
                ranked=[{**r,"pre_rerank_rank":before.index(r["chunk_id"])+1,"pre_rerank_score":r.get("score"),"score":float(s),"score_type":"cross_encoder"} for s,r in sorted(zip(scores,candidate),key=lambda x:(-float(x[0]),x[1]["chunk_id"]))]+ranked[len(candidate):]
            except Exception as exc:
                failures.append({"channel":"reranker","error":exc.code if isinstance(exc,RetrievalUnavailable) else "RERANKER_INFERENCE_FAILED",
                                 "detail":exc.detail if isinstance(exc,RetrievalUnavailable) else type(exc).__name__}); diagnostics["reranker_skipped"]=True
        if mode in {"rrf","rrf_reranker"} and failures and not channels:
            raise RetrievalUnavailable("RETRIEVAL_UNAVAILABLE", "all retrieval channels failed")
        if mode == "vector" and failures: raise RetrievalUnavailable(failures[-1]["error"], failures[-1]["detail"])
        if mode == "bm25" and failures: raise RetrievalUnavailable("BM25_UNAVAILABLE", failures[-1].get("detail") or failures[-1].get("error") or "BM25 unavailable")
        output=[]
        for pos,row in enumerate(ranked[:self.config.output_top_k],1):
            output.append({**row,"rank":pos,"source":"policy","retrieval_method":mode,"knowledge_version":index["knowledge_version"],
                "index_fingerprint":fingerprint,"policy_version":row["version"],"effective_from":row["effective_from"],"effective_to":row["effective_to"]})
        return {"knowledge_version":index["knowledge_version"],"index_fingerprint":fingerprint,"config_version":self.config.version,
            "config_hash":self.config.config_hash,"mode":mode,"evidence":output,"channels":{k:[r["chunk_id"] for r in v] for k,v in channels.items()},
            "degraded":bool(failures),"failures":failures,"latency_ms":(time.perf_counter()-started)*1000,"diagnostics":diagnostics,
            "models":{"embedding":"BAAI/bge-small-zh-v1.5@"+SMALL_REV if "vector" in channels else None,
                      "reranker":"BAAI/bge-reranker-base@"+RERANKER_REV if mode=="rrf_reranker" and "reranker_ms" in diagnostics else None}}

    def collection_name(self, fingerprint: str) -> str:
        return f"{self.collection_namespace}_{fingerprint[:24]}"

    def rerank_candidates(self, query: str, evidence: list[dict], *, mode: str | None = None) -> tuple[list[dict], dict]:
        """Globally rank already accumulated, version-checked evidence without another retrieval."""
        selected = mode or self.config.mode
        unique = {row["chunk_id"]: row for row in evidence}
        rows = list(unique.values())
        if selected == "rrf_reranker" and rows:
            try:
                self._models(need_ranker=True)
                with self._inference_lock:
                    scores = self.ranker.predict([(query, row["title"] + " " + row["content"]) for row in rows], batch_size=8)
                if len(scores) != len(rows) or any(not math.isfinite(float(score)) for score in scores):
                    raise ValueError("invalid global reranker scores")
                rows = [{**row, "pre_global_score": row.get("score"), "score": float(score),
                         "score_type": "cross_encoder", "global_rank": rank}
                        for rank, (score, row) in enumerate(sorted(zip(scores, rows), key=lambda pair: (-float(pair[0]), pair[1]["chunk_id"])), 1)]
                return rows[:self.config.output_top_k], {"reranked": True, "degraded": False}
            except Exception as exc:
                return rows[:self.config.output_top_k], {"reranked": False, "degraded": True,
                    "failure": exc.code if isinstance(exc, RetrievalUnavailable) else type(exc).__name__}
        rows.sort(key=lambda row: (row.get("rank", 10**9), row["chunk_id"]))
        return [{**row, "global_rank": rank} for rank, row in enumerate(rows[:self.config.output_top_k], 1)], {"reranked": False, "degraded": False}
