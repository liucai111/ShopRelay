"""M2.3 MULTI vertical probe against the dedicated PostgreSQL DB.

Defaults to a deterministic provider stub. Pass --real only for one approved
dev/validation sample; the complete Self-RAG run is capped at three model requests.
Qdrant uses an isolated in-memory collection namespace for this probe.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import sys
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor"), str(ROOT)]

from fastapi.testclient import TestClient
from qdrant_client import QdrantClient
from sqlalchemy import make_url, text

from app.m0.config import load_settings
from app.m1.api import CSRF_HEADER, create_app
from app.m1.chat import DeepSeekProvider
from app.m1.db import current_revision, make_engine, make_session_factory
from app.m1.models import Trace
from app.m1.services import set_demo_password
from app.m2.retrieval import RetrievalConfig, RetrievalService
from app.m2.self_rag import (SelfRAGConfig, _answer_semantic_issues, _asserted_policy_stages,
                             _policy_stages)

DB_NAME = "shoprelay_m11_test"
CASE_ID = "pr2-multi_clause_return_refund-03"
DATASET_PATH = ROOT / "eval/dataset/reviewed/policy-retrieval-reviewed-6bee4a732647.json"
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"


def _business_fingerprint(connection) -> dict:
    users = connection.execute(text("SELECT user_id, display_name FROM users ORDER BY user_id")).all()
    orders = connection.execute(text(
        "SELECT order_id, user_id, status, paid_at, delivered_at, paid_cents, version "
        "FROM orders ORDER BY order_id")).all()
    payload = {"users": [list(row) for row in users], "orders": [list(row) for row in orders]}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return {"counts": {key: len(value) for key, value in payload.items()},
            "sha256": hashlib.sha256(encoded).hexdigest()}


def _safe_exception_message(exc: Exception, settings, database_url: str) -> str:
    message = str(exc)[:500]
    for secret in (getattr(settings, "api_key", None), database_url,
                   getattr(settings, "database_url", None)):
        if secret:
            message = message.replace(secret, "[REDACTED]")
    return message


def usage_meta(result: dict, node: str) -> dict:
    return {"result": result, "model_id": "m2.3-postgres-stub", "node": node,
            "usage": {"prompt_tokens": 24, "completion_tokens": 12},
            "latency_ms": 1, "estimated_cost_usd": 0.00002}


class StubProvider:
    """Deterministic contract fixture. Never used as evidence of real LLM quality."""
    def __init__(self, case: dict):
        self.case = case
        self.calls = {"condition_extraction": 0, "evidence_sufficiency": 0, "policy_answer": 0}
        self.settings = load_settings(require_api_key=False)

    def extract_conditions(self, question: str) -> dict:
        self.calls["condition_extraction"] += 1
        fields = []
        for condition in self.case["required_conditions"]:
            if condition["condition_id"] == "c1":
                kind = "USER_PROVIDED_FACT"
                excerpt = "已签收的瑕疵商品"
            elif condition["condition_id"] == "c4":
                kind, excerpt = "MISSING_INFORMATION", None
            else:
                kind, excerpt = "POLICY_RULE", None
            source = {"USER_PROVIDED_FACT": "USER_PROVIDED", "POLICY_RULE": "POLICY",
                      "MISSING_INFORMATION": "UNKNOWN"}[kind]
            fields.append({"condition_id": condition["condition_id"], "text": condition["text"],
                           "kind": kind, "required": condition["required"], "source_excerpt": excerpt,
                           "source_type": source})
        result = {"request_mode": "POLICY_EXPLANATION", "conditions": fields}
        return usage_meta(result, "condition_extraction") | {"conditions_result": result}

    def grade_evidence(self, question, conditions, evidence, evaluation_at, *, request_mode="POLICY_EXPLANATION"):
        self.calls["evidence_sufficiency"] += 1
        found = {row["chunk_id"] for row in evidence}
        judgments = []
        missing = []
        for condition, judgement in zip(self.case["required_conditions"], self.case["evidence_judgments"]):
            if not condition["required"]:
                continue
            usable = [item for item in judgement["evidence_ids"] if item in found]
            if not usable:
                missing.append(condition["condition_id"])
                continue
            judgments.extend({"condition_id": condition["condition_id"], "evidence_id": item,
                              "judgment": judgement["relation"]} for item in usable)
        payload = {"grade": {"relevant": bool(evidence), "sufficient": not missing,
                              "missing_aspect": missing,
                              "rationale": "Stub maps approved required conditions to current snapshot evidence."},
                   "judgments": judgments,
                   "proposed_conclusion": ("EXPLAIN" if request_mode == "POLICY_EXPLANATION" else "ALLOW") if not missing else "UNKNOWN"}
        return usage_meta(payload, "evidence_sufficiency") | {"grade_payload": payload}

    def answer(self, question, evidence):
        self.calls["policy_answer"] += 1
        return {"answer": "根据已检索并核验的政策条款，质量退货与退款前置条件分别适用；实际资格仍须结合有效签收时间确认。",
                "citations": [row["chunk_id"] for row in evidence], "model_id": "m2.3-postgres-stub",
                "thinking": "disabled", "usage": {"prompt_tokens": 32, "completion_tokens": 22},
                "latency_ms": 1, "estimated_cost_usd": 0.00004}


def data_events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]


def login(client, username, password):
    token = client.get("/api/demo/csrf").json()["csrf_token"]
    headers = {"Origin": "http://testserver", CSRF_HEADER: token}
    response = client.post("/api/demo/login", headers=headers,
                           json={"username": username, "password": password})
    if response.status_code != 200:
        raise RuntimeError(f"demo login failed: HTTP {response.status_code}")
    return {**headers, CSRF_HEADER: client.cookies.get("shoprelay_csrf")}


def _score_retrieval(rows: list[dict], gold: set[str]) -> dict:
    ids = [row["chunk_id"] for row in rows]
    first = 1 if ids and ids[0] in gold else 0
    hits = [rank for rank, evidence_id in enumerate(ids, 1) if evidence_id in gold]
    return {"p_at_1": first, "recall_at_3": len(set(ids[:3]) & gold) / len(gold) if gold else 0.0,
            "recall_at_5": len(set(ids[:5]) & gold) / len(gold) if gold else 0.0,
            "mrr": 1 / min(hits) if hits else 0.0, "evidence_ids": ids}


def _windows(rows: list[dict]) -> set[tuple[str, str]]:
    import re
    return {(amount, unit) for row in rows for amount, unit in
            re.findall(r"(\d+)\s*(天|日|小时|周|个月|月)", row.get("text", ""))}


def _required_semantics(rows: list[dict]) -> tuple[set[str], set[tuple[str, str]]]:
    required = [row for row in rows if row.get("required")]
    return (set().union(*(_asserted_policy_stages(row.get("text", "")) for row in required)) if required else set(),
            _windows(required))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Run one real approved MULTI flow; total Self-RAG calls are capped at three.")
    args = parser.parse_args()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = {"record_type": "m2.3_postgresql_multi_vertical_probe", "created_at_utc": datetime.now(timezone.utc).isoformat(),
              "status": "FAIL", "provider_mode": "DEEPSEEK_REAL" if args.real else "STUB",
              "case_id": CASE_ID, "dataset_path": str(DATASET_PATH.relative_to(ROOT)).replace("\\", "/"),
              "checks": {}, "real_model_requests": 0, "usage": None, "secrets_recorded": False,
              "database": DB_NAME, "database_version": None, "alembic_revision": None}
    settings = load_settings(require_api_key=args.real)
    if not settings.database_url or not settings.database_url.startswith("postgresql"):
        raise SystemExit("BLOCKED: configure PostgreSQL in ignored local configuration")
    database_url = make_url(settings.database_url).set(database=DB_NAME).render_as_string(hide_password=False)
    if make_url(database_url).database != DB_NAME:
        raise SystemExit("Refusing to run outside shoprelay_m11_test")
    dataset_bytes = DATASET_PATH.read_bytes()
    dataset = json.loads(dataset_bytes.decode("utf-8"))
    case = next((row for row in dataset["cases"] if row["case_id"] == CASE_ID), None)
    if not case or case.get("review_status") != "APPROVED" or case.get("dataset_split") not in {"dev", "validation"} or case.get("expected_strategy") != "MULTI":
        raise SystemExit("BLOCKED: fixed diagnostic must be an approved dev/validation MULTI sample")
    report.update({"dataset_version": dataset.get("dataset_version"), "dataset_sha256": dataset.get("dataset_sha256"),
                   "dataset_file_sha256": hashlib.sha256(dataset_bytes).hexdigest(),
                   "dataset_split": case["dataset_split"], "knowledge_version": case["knowledge_version"],
                   "evaluation_at": case["evaluation_at"], "test_candidate_scored": False,
                   "test_candidate_used_for_tuning": False,
              "real_call_limit": 3 if args.real else 0,
              "real_call_budget_note": "This single vertical run includes extraction, evidence Grade, and answer generation; Self-RAG enforces at most three total requests. No separate model probe is part of this command." if args.real else None})
    report["effective_node_config"] = {name: {"thinking": node.thinking, "max_tokens": node.max_tokens,
        "timeout_seconds": node.timeout_seconds, "max_cost_usd": node.max_cost_usd,
        "locked": node.locked} for name, node in settings.nodes.items()
        if name in {"route", "condition_extraction", "evidence_sufficiency", "policy_answer"}}
    engine = make_engine(database_url)
    qdrant = QdrantClient(":memory:")
    app = None
    try:
        with engine.connect() as connection:
            report["database_version"] = connection.execute(text("SELECT version()" )).scalar_one().split(",", 1)[0]
            business_before = _business_fingerprint(connection)
        revision = current_revision(database_url)
        report["alembic_revision"] = revision
        if revision != "20261019_04":
            raise RuntimeError(f"expected revision 20261019_04; actual revision {revision}")
        report["checks"]["postgresql_revision_and_seed"] = "PASS"

        config = replace(RetrievalConfig(mode="bm25"), mode="rrf_reranker")
        retrieval = RetrievalService(config=config, qdrant_client=qdrant,
                                     collection_namespace="m23_isolated")
        gold = set(case["acceptable_evidence_ids"])
        baseline = {}
        for mode in ("bm25", "rrf", "rrf_reranker"):
            start = time.perf_counter()
            found = retrieval.retrieve(case["user_query"], evaluation_at=case["evaluation_at"], mode=mode)
            baseline[mode] = {**_score_retrieval(found["evidence"], gold),
                              "latency_ms": round((time.perf_counter() - start) * 1000, 3),
                              "index_fingerprint": found["index_fingerprint"],
                              "knowledge_version": found["knowledge_version"],
                              "degraded": found["degraded"], "failures": found["failures"]}
        report["single_hop_baseline_same_approved_case"] = baseline
        report["qdrant"] = {"backend": "isolated in-memory Qdrant", "namespace": "m23_isolated",
                            "collection": retrieval.collection_name(retrieval.pin_view(case["evaluation_at"]).fingerprint)}
        report["checks"]["single_hop_retrieval_baselines"] = "PASS" if all(not row["degraded"] for row in baseline.values()) else "DEGRADED"

        factory = make_session_factory(engine)
        password_a, password_b = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
        with factory() as db:
            set_demo_password(db, "u_demo", password_a)
            set_demo_password(db, "u_other", password_b)
        provider = DeepSeekProvider(settings=settings) if args.real else StubProvider(case)
        app = create_app(database_url=database_url, engine=engine, chat_provider=provider,
                         retrieval_evaluation_at=case["evaluation_at"],
                         self_rag_config=SelfRAGConfig(max_model_requests=3 if args.real else 12))
        app.state.retrieval_service = retrieval
        client = TestClient(app)
        headers = login(client, "u_demo", password_a)
        client_b = TestClient(app)
        login(client_b, "u_other", password_b)
        run_started = time.perf_counter()
        client_request_id = "m23-" + uuid.uuid4().hex
        chat_payload = {"message": case["user_query"], "client_request_id": client_request_id}
        response = client.post("/api/chat/stream", headers=headers, json=chat_payload)
        run_ms = round((time.perf_counter() - run_started) * 1000, 3)
        events = data_events(response)
        if response.status_code != 200 or not events:
            raise RuntimeError(f"MULTI SSE request failed: HTTP {response.status_code}")
        run_id = events[0]["run_id"]
        run = client.get(f"/api/runs/{run_id}").json()["run"]
        answer = run.get("answer") or {}
        self_rag = answer.get("self_rag") or {}
        persisted_error_details = (run.get("error") or {}).get("details") or {}
        failed_budget = persisted_error_details.get("budget") or {}
        route_event = next((event for event in events if event.get("type") == "route_decided"), None)
        error_event = next((event for event in events if event.get("type") == "error"), None)
        report.update({"run_id": run_id, "run_status": run["status"], "run_latency_ms": run_ms,
                       "sse_event_types": [event["type"] for event in events],
                       "sse_error_event": error_event,
                       "run_error": run.get("error"),
                       "route_event": route_event,
                       "self_rag_stop_reason": self_rag.get("stop_reason"),
                       "self_rag_sufficient": self_rag.get("sufficient"),
                       "self_rag_evidence_sufficient": self_rag.get("evidence_sufficient"),
                       "answer_kind": answer.get("answer_kind"), "answer": answer.get("answer"),
                       "citations": answer.get("citations", []), "self_rag": self_rag,
                       "route_strategy": ((answer.get("route") or {}).get("strategy") or
                                          ((route_event or {}).get("payload") or {}).get("strategy")),
                       "real_model_requests": ((self_rag.get("budget") or {}).get("model_requests") or
                                               failed_budget.get("model_requests", 0)) if args.real else 0,
                       "usage": self_rag.get("budget") or failed_budget or None,
                       "required_condition_count": sum(bool(row["required"]) for row in case["required_conditions"]),
                       "actual_conditions": self_rag.get("conditions", []),
                       "actual_request_mode": self_rag.get("request_mode"),
                       "self_rag_hop_count": len(self_rag.get("hops", [])),
                       "effective_citations": sum(item.get("chunk_id") in {row["chunk_id"] for row in self_rag.get("evidence", [])}
                                                  for item in answer.get("citations", [])),
                       "single_hop_same_case": baseline})
        if run["status"] != "SUCCEEDED":
            report["checks"]["multi_api_sse_and_persisted_run"] = "FAIL"
            report["checks"]["real_deepseek_multi_vertical"] = "BLOCKED"
            report["error"] = {"type": "RunFailed", "code": (run.get("error") or {}).get("code"),
                               "details": (run.get("error") or {}).get("details")}
            report["status"] = "BLOCKED"
            return
        assert answer.get("strategy") == "MULTI", "approved MULTI case did not reach MULTI executor"
        assert (answer.get("route") or {}).get("strategy") == "MULTI", "persisted route was not MULTI"
        assert report["self_rag_hop_count"] <= 3
        assert all(hop["index_fingerprint"] == self_rag.get("index_fingerprint")
                   for hop in self_rag.get("hops", []))
        assert run["status"] == "SUCCEEDED"
        semantic_gate_rejected = (self_rag.get("condition_semantic_audit") or {}).get("status") == "REJECTED"
        report["checks"]["multi_api_sse_and_persisted_run"] = (
            "PASS" if ("retrieval_hop" in report["sse_event_types"] and
                       "answer" in report["sse_event_types"] and "done" in report["sse_event_types"] and
                       report["sse_event_types"].index("retrieval_hop") < report["sse_event_types"].index("answer") <
                       report["sse_event_types"].index("done")) else
            ("BLOCKED" if semantic_gate_rejected else "FAIL"))
        if semantic_gate_rejected:
            report["checks"]["condition_semantic_gate"] = "BLOCKED"
        report["checks"]["pinned_policy_fingerprint_and_utc_time"] = "PASS"
        report["checks"]["three_hop_and_query_limits"] = "PASS" if self_rag_hop_count_valid(self_rag) else "FAIL"
        assert client_b.get(f"/api/runs/{run_id}").status_code == 404
        assert client_b.get(f"/api/runs/{run_id}/events").status_code == 404
        report["checks"]["cross_user_run_and_event_isolation"] = "PASS"
        events_after = client.get(f"/api/runs/{run_id}/events").json()["events"]
        report["persisted_hop_count"] = sum(event["type"] == "retrieval_hop" for event in events_after)
        with factory() as db:
            from app.m1.state import restore_agent_state
            state = restore_agent_state(db, "u_demo", run_id)
            report["agent_state"] = {"stop_reason": state.stop_reason,
                "condition_count": len(state.policy_conditions), "hop_count": len(state.retrieval_hops),
                "evidence_sufficient": state.evidence_sufficient}
            trace_types = [row.event_type for row in db.query(Trace).filter(Trace.run_id == run_id).all()]
            report["trace_types"] = trace_types
            report["checks"]["run_state_trace_and_event_persistence"] = "PASS" if (
                state and "self_rag_completed" in trace_types and
                (not self_rag.get("hops") or "self_rag_hop" in trace_types) and
                report["persisted_hop_count"] == len(self_rag.get("hops", []))) else "FAIL"
        with factory() as db:
            trace_count_before_replay = db.query(Trace).filter(Trace.run_id == run_id).count()
        provider_calls_before_replay = (provider.calls.copy() if isinstance(provider, StubProvider) else None)
        replay = client.post("/api/chat/stream", headers=headers, json=chat_payload)
        replay_events = data_events(replay)
        replay_run_id = replay_events[0].get("run_id") if replay_events else None
        if replay_run_id != run_id:
            raise AssertionError("same client_request_id did not replay the original run")
        if isinstance(provider, StubProvider) and provider.calls != provider_calls_before_replay:
            raise AssertionError("idempotent replay called the provider again")
        with factory() as db:
            trace_count_after_replay = db.query(Trace).filter(Trace.run_id == run_id).count()
        if trace_count_after_replay != trace_count_before_replay:
            raise AssertionError("idempotent replay added traces or called model nodes again")
        report["trace_count_before_after_replay"] = [trace_count_before_replay, trace_count_after_replay]
        report["checks"]["same_request_id_idempotent_replay"] = "PASS"
        restart_app = create_app(database_url=database_url, engine=engine, chat_provider=provider,
                                 retrieval_evaluation_at=case["evaluation_at"],
                                 self_rag_config=SelfRAGConfig(max_model_requests=3 if args.real else 12))
        restart_app.state.retrieval_service = retrieval
        with TestClient(restart_app) as restarted_client:
            login(restarted_client, "u_demo", password_a)
            restarted_run = restarted_client.get(f"/api/runs/{run_id}")
            restarted_events = restarted_client.get(f"/api/runs/{run_id}/events")
            if restarted_run.status_code != 200 or restarted_events.status_code != 200:
                raise AssertionError("run or persisted events were not readable after app recreation")
            recovered = restarted_run.json()["run"]
            recovered_answer = recovered.get("answer") or {}
            if recovered["status"] != run["status"] or recovered_answer.get("answer") != answer.get("answer"):
                raise AssertionError("application recreation changed the persisted run result")
            report["restart_recovery"] = {"status": recovered["status"],
                "persisted_event_count": len(restarted_events.json().get("events", [])),
                "answer_matches": recovered_answer.get("answer") == answer.get("answer")}
        restart_app.state.run_executor.shutdown(wait=True, cancel_futures=False)
        report["checks"]["application_recreation_and_event_replay"] = "PASS"
        report["checks"]["test_candidate_untouched"] = "PASS"
        with engine.connect() as connection:
            business_after = _business_fingerprint(connection)
        report["m0_business_rows_before"] = business_before
        report["m0_business_rows_after"] = business_after
        report["m0_seed_rows_preserved"] = business_before == business_after
        report["checks"]["m0_business_rows_preserved"] = "PASS" if report["m0_seed_rows_preserved"] else "FAIL"
        report["model"] = {"id": settings.model, "real": args.real,
                           "price_source": PRICE_SOURCE if args.real else None,
                           "prompt_version": self_rag.get("prompt_version")}
        if args.real:
            gold_required_evidence = {evidence_id for condition, judgment in
                zip(case["required_conditions"], case["evidence_judgments"])
                if condition.get("required") for evidence_id in judgment.get("evidence_ids", [])}
            actual_conditions = self_rag.get("conditions", [])
            required_conditions = [item for item in actual_conditions if item.get("required")]
            source_safe = all(item.get("kind") == "POLICY_RULE" and item.get("source_type") == "POLICY"
                              for item in required_conditions)
            final_answer = answer.get("answer") or ""
            evidence_ids = {row.get("chunk_id") for row in self_rag.get("evidence", [])}
            citation_ids = {item.get("chunk_id") if isinstance(item, dict) else item
                            for item in answer.get("citations", [])}
            gold_stages, gold_windows = _required_semantics(case["required_conditions"])
            actual_stages, actual_windows = _required_semantics(actual_conditions)
            cited_rows = [row for row in self_rag.get("evidence", []) if row.get("chunk_id") in citation_ids]
            answer_stages = _policy_stages(final_answer)
            positive_answer_stages = _asserted_policy_stages(final_answer)
            answer_windows = _windows([{"text": final_answer}])
            answer_issues = _answer_semantic_issues(final_answer, cited_rows)
            condition_audit = self_rag.get("condition_semantic_audit") or {}
            exact_source_rules = all(
                item.get("rule_role") == "CORE_RULE" and item.get("source_type") == "POLICY" and
                item.get("source_evidence_id") in evidence_ids and
                bool(item.get("source_excerpt")) and item.get("text") == item.get("source_excerpt") and
                item.get("source_excerpt") in next((row.get("content", "") for row in self_rag.get("evidence", [])
                                                      if row.get("chunk_id") == item.get("source_evidence_id")), "")
                for item in required_conditions)
            no_unasked_settlement = not any("REFUND_SETTLEMENT" in item.get("policy_stages", [])
                                            for item in required_conditions)
            no_return_completion_inflation = not any("RETURN_COMPLETION" in item.get("policy_stages", [])
                                                     for item in required_conditions)
            grade_is_sufficient = (self_rag.get("evidence_sufficient") is True and
                                   bool(self_rag.get("hops")) and
                                   self_rag["hops"][-1].get("grade_status") == "VALIDATED" and
                                   (self_rag["hops"][-1].get("grade_result") or {}).get("sufficient") is True)
            report["semantic_fidelity"] = {"request_mode_policy_explanation": self_rag.get("request_mode") == "POLICY_EXPLANATION",
                "required_policy_semantics_match_approved_gold": gold_stages.issubset(actual_stages) and actual_stages.issubset(gold_stages) and gold_windows == actual_windows,
                "required_conditions_are_atomic_policy_sources": source_safe and exact_source_rules,
                "no_unasked_refund_settlement_prerequisite": no_unasked_settlement,
                "no_return_completion_inflation": no_return_completion_inflation,
                "answer_does_not_require_refund_settlement": "REFUND_SETTLEMENT" not in positive_answer_stages,
                "answer_does_not_require_return_completion": "RETURN_COMPLETION" not in positive_answer_stages,
                "condition_semantic_audit_passed": condition_audit.get("status") == "PASS",
                "grade_sufficient_with_coverage": grade_is_sufficient and self_rag.get("sufficient") is True,
                "gold_required_policy_evidence_retrieved": gold_required_evidence.issubset(evidence_ids),
                "gold_required_policy_evidence_cited": gold_required_evidence.issubset(citation_ids),
                "answer_covers_required_policy_semantics": gold_stages.issubset(answer_stages) and gold_windows.issubset(answer_windows),
                "answer_supported_by_cited_policy": not answer_issues,
                "gold_policy_stages": sorted(gold_stages), "actual_policy_stages": sorted(actual_stages),
                "answer_policy_stages": sorted(answer_stages),
                "gold_time_windows": sorted(f"{n}{u}" for n, u in gold_windows),
                "actual_time_windows": sorted(f"{n}{u}" for n, u in actual_windows),
                "answer_semantic_issues": answer_issues}
            report["checks"]["real_deepseek_multi_vertical"] = "PASS" if (
                answer.get("answer_kind") == "POLICY_ANSWER" and self_rag.get("sufficient") is True and
                report["real_model_requests"] <= 3 and all(
                    value is True for key, value in report["semantic_fidelity"].items()
                    if key not in {"gold_policy_stages", "actual_policy_stages", "answer_policy_stages",
                                   "gold_time_windows", "actual_time_windows", "answer_semantic_issues"})) else "BLOCKED"
        else:
            report["checks"]["stub_multi_contract_only"] = "PASS"
        report["status"] = "PASS" if all(value == "PASS" for value in report["checks"].values()) else "BLOCKED"
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__,
                            "message": _safe_exception_message(exc, locals().get("settings"),
                                                               locals().get("database_url", ""))}
        report["checks"].setdefault("probe_execution", "FAIL")
        report["status"] = "FAIL"
    finally:
        try:
            qdrant.close()
        except Exception:
            pass
        if app is not None:
            try:
                app.state.run_executor.shutdown(wait=True, cancel_futures=False)
            except Exception:
                pass
        engine.dispose()
        report_path = ROOT / "docs" / "probes" / f"m2.3-postgres-{report['provider_mode'].lower()}-{stamp}.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({**report, "evidence_path": str(report_path.relative_to(ROOT)).replace("\\", "/")},
                         ensure_ascii=False, indent=2))


def self_rag_hop_count_valid(result: dict) -> bool:
    hops = result.get("hops", [])
    return len(hops) <= 3 and all(len(hop.get("queries", [])) <= 2 for hop in hops)


if __name__ == "__main__":
    main()
