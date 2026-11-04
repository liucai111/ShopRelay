"""Short-lived authenticated UI harness for the M5.4-B2 Q1 read-only runs.

This runner never creates or resets accounts. The existing owner logs in through
the normal UI. It pins all DB work to shoprelay_m11_test and uses the project's
deterministic local answer stub; retrieval itself remains real.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / ".vendor" / "g2"), str(ROOT / ".vendor"), str(ROOT)]

from scripts.g2_test_database import resolve_test_database_url


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--retrieval-mode", choices=("bm25", "rrf_reranker"), required=True)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    database_url = resolve_test_database_url()
    if database_url.database != "shoprelay_m11_test" or database_url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise SystemExit("REFUSING_NON_TEST_OR_NON_LOOPBACK_DATABASE")

    os.environ["DATABASE_URL"] = database_url.render_as_string(hide_password=False)
    os.environ["SHOPRELAY_ORCHESTRATOR"] = "langgraph"
    os.environ["SHOPRELAY_M3_SUPERVISOR_ENABLED"] = "true"
    os.environ["SHOPRELAY_RETRIEVAL_MODE"] = args.retrieval_mode
    os.environ["SHOPRELAY_QDRANT_MODE"] = "local"
    os.environ["SHOPRELAY_GRAPH_VERSION"] = "m5.4-b2-q1"
    os.environ["SHOPRELAY_EXECUTION_VERSION"] = "m5.4-b2-q1-browser"
    os.environ["SHOPRELAY_RETRIEVAL_PREWARM"] = "false"

    from scripts.probe_m5_4_a_browser_server import DeterministicEvidenceStub
    from app.m1.api import create_app
    from app.orchestration.adapters import make_legacy_services

    provider = DeterministicEvidenceStub()
    app = create_app(database_url=database_url.render_as_string(hide_password=False),
                     orchestrator_mode="langgraph", chat_provider=provider,
                     observability_admin_user_ids=None)
    app.state.graph_services = make_legacy_services(
        provider=provider,
        retrieval_service=app.state.retrieval_service,
        index_path=app.state.policy_index_path,
        evaluation_at=None,
    )
    print({"database": database_url.database, "retrieval_mode": args.retrieval_mode,
           "orchestrator": "langgraph", "provider": "deterministic_local_stub",
           "prewarm": False, "business_write_flags": "default_closed",
           "admin_allowlist": "loaded_from_existing_process_settings"}, flush=True)

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
