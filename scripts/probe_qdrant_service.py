"""Compare Qdrant service CRUD against the local client interface."""
from __future__ import annotations

import json
import os
import subprocess
import uuid
from importlib.metadata import version
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]


def run() -> dict:
    url = os.getenv("QDRANT_URL")
    if not url:
        return {"status": "blocked", "reason": "QDRANT_URL absent; no service connection attempted"}
    endpoint = urlparse(url)
    if endpoint.scheme != "http" or endpoint.hostname not in {"127.0.0.1", "localhost"} or endpoint.port != 6333:
        return {"status": "blocked", "reason": "restart persistence probe is restricted to the local ShopRelay Qdrant endpoint on port 6333"}
    try:
        from qdrant_client import QdrantClient, models
    except ImportError:
        return {"status": "blocked", "reason": "qdrant-client absent"}
    name = "shoprelay_m0_probe_" + uuid.uuid4().hex[:12]
    client = QdrantClient(url=url, timeout=4)
    local = QdrantClient(":memory:")
    try:
        vector = [1.0, 0.0]
        payload = {"chunk_id": "synthetic"}
        local.create_collection(name, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
        local.upsert(name, points=[models.PointStruct(id=1, vector=vector, payload=payload)])
        local_found = local.retrieve(name, ids=[1], with_payload=True)
        local_query = local.query_points(name, query=vector, limit=1).points

        client.create_collection(name, vectors_config=models.VectorParams(size=2, distance=models.Distance.COSINE))
        client.upsert(name, points=[models.PointStruct(id=1, vector=vector, payload=payload)])
        found = client.retrieve(name, ids=[1], with_payload=True)
        queried = client.query_points(name, query=vector, limit=1).points

        # Restart only this Compose project's Qdrant service, then read the marker back.
        restarted = subprocess.run(
            ["docker", "compose", "-f", str(ROOT / "compose.m0.yaml"), "restart", "qdrant"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        if restarted.returncode != 0:
            return {"status": "failed", "stage": "service_restart", "error_type": "ComposeRestartFailed"}
        client.close()
        client = QdrantClient(url=url, timeout=4)
        persisted = client.retrieve(name, ids=[1], with_payload=True)
        persisted_query = client.query_points(name, query=vector, limit=1).points
        client.delete(name, points_selector=models.PointIdsList(points=[1]))
        deleted = client.retrieve(name, ids=[1])
        same_result = (
            len(found) == len(local_found) == 1
            and found[0].id == local_found[0].id
            and found[0].payload == local_found[0].payload == payload
            and len(queried) == len(local_query) == 1
            and queried[0].id == local_query[0].id == 1
            and queried[0].score == local_query[0].score
        )
        persisted_ok = len(persisted) == 1 and persisted[0].payload == payload and len(persisted_query) == 1 and persisted_query[0].id == 1
        ok = same_result and persisted_ok and len(deleted) == 0
        return {
            "status": "ok" if ok else "failed",
            "retrieved": len(found),
            "query_top_id": queried[0].id if queried else None,
            "same_result_as_in_memory_local": same_result,
            "persisted_after_compose_restart": persisted_ok,
            "deleted": len(deleted) == 0,
            "qdrant_client_version": version("qdrant-client"),
        }
    except Exception as exc:
        return {"status": "failed", "error_type": type(exc).__name__}
    finally:
        try:
            client.delete_collection(name)
        except Exception:
            pass
        client.close()
        local.close()


def main() -> None:
    result = {"timestamp_utc": datetime.now(timezone.utc).isoformat(), "probe": "qdrant_service_m0", "result": run()}
    path = ROOT / "docs/probes" / f"qdrant-service-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as file:
        json.dump(result, file, ensure_ascii=False, indent=2)
    print(json.dumps({"path": str(path), **result}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
