"""Explicit, repeatable M0 seed and policy ingest. No startup side effects."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def canonical_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def digest(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).split())


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(db_path: Path) -> None:
    with connect(db_path) as conn:
        conn.executescript((ROOT / "migrations/0001_m0.sql").read_text(encoding="utf-8"))


def seed(db_path: Path, seed_path: Path = ROOT / "data/seed.json") -> dict:
    payload = json.loads(seed_path.read_text(encoding="utf-8"))
    sha = digest(payload)
    migrate(db_path)
    with connect(db_path) as conn:
        row = conn.execute("SELECT version,sha256 FROM seed_versions WHERE kind='business'").fetchone()
        if row:
            if (row["version"], row["sha256"]) != (payload["snapshot_id"], sha):
                raise ValueError("existing business seed differs; use a new database")
            return {"snapshot_id": row["version"], "sha256": sha, "status": "already_applied"}
        for user in payload["users"]:
            conn.execute("INSERT INTO users VALUES (?,?)", (user["user_id"], user["display_name"]))
        for product in payload["products"]:
            conn.execute("INSERT INTO products VALUES (?,?,?,?)", (product["product_id"], product["name"], product["category"], product["price_cents"]))
            conn.execute("INSERT INTO inventory(product_id,available_qty) VALUES (?,?)", (product["product_id"], product["available_qty"]))
        for order in payload["orders"]:
            conn.execute("INSERT INTO orders VALUES (?,?,?,?,?,?,?)", tuple(order[k] for k in ("order_id", "user_id", "status", "paid_at", "delivered_at", "paid_cents", "version")))
            conn.execute("INSERT INTO order_items VALUES (?,?,?,?)", (order["order_id"], order["product_id"], order["quantity"], order["paid_cents"]))
        conn.execute("INSERT INTO seed_versions VALUES (?,?,?,?)", ("business", payload["snapshot_id"], sha, utc_now()))
    return {"snapshot_id": payload["snapshot_id"], "sha256": sha, "status": "applied"}


def ingest(index_path: Path, policy_path: Path = ROOT / "knowledge/policies.json") -> dict:
    payload = json.loads(policy_path.read_text(encoding="utf-8"))
    chunks = []
    seen = set()
    for doc in payload["documents"]:
        for clause in doc["clauses"]:
            chunk_id = f"policies/{doc['policy_id']}@{doc['version']}#{clause['clause_id']}"
            if chunk_id in seen:
                raise ValueError(f"duplicate chunk: {chunk_id}")
            seen.add(chunk_id)
            chunks.append({"chunk_id": chunk_id, "policy_id": doc["policy_id"], "version": doc["version"], "title": doc["title"], "clause_id": clause["clause_id"], "effective_from": doc["effective_from"], "effective_to": doc["effective_to"], "content": normalize(clause["text"])})
    index = {"knowledge_version": payload["knowledge_version"], "source_sha256": digest(payload), "chunks": sorted(chunks, key=lambda item: item["chunk_id"])}
    index_path.parent.mkdir(parents=True, exist_ok=True)
    temp = index_path.with_suffix(".tmp")
    temp.write_text(json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(index_path)
    return {"knowledge_version": index["knowledge_version"], "source_sha256": index["source_sha256"], "chunks": len(chunks)}


def policy_at(chunks: list[dict], evaluation_at: str) -> list[dict]:
    if not evaluation_at.endswith("Z"):
        raise ValueError("evaluation_at must be UTC ISO 8601 Z")
    return [c for c in chunks if c["effective_from"] <= evaluation_at and (c["effective_to"] is None or evaluation_at < c["effective_to"])]
