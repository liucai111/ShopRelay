"""Close the R9 attempt that failed locally before Run creation; never replay it."""
from __future__ import annotations
import json, sys
from datetime import datetime, timezone
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from eval.m6_1.personal_dev_batch_m62 import DurableAttemptJournal, TaskQuotaLedger
from eval.m6_1.personal_dev_database import PersonalDevDatabase

PKG = ROOT / "docs/eval/m6.2-r324-40case-personal-dev-batch-r9-20261029"
AUTH = PKG / "authorization-r9-0E30E94C060A45C9.json"
STATE = PKG / "execution-state-r9-0E30E94C060A45C9.json"
JOURNAL = PKG / "execution-journal-r9-0E30E94C060A45C9.jsonl"
TASK = PKG / "task-cumulative-ledger-r1.json"
REPORT = PKG / "reports/pre-run-local-failure-closeout-r1.json"

def main() -> None:
    auth = json.loads(AUTH.read_text(encoding="utf-8"))
    state = json.loads(STATE.read_text(encoding="utf-8"))
    rows = [json.loads(line) for line in JOURNAL.read_text(encoding="utf-8").splitlines() if line.strip()]
    case_id = "D1-POLICY-018"
    if (REPORT.exists() or auth.get("status") != "ACTIVE" or
            state.get("authorization_id") != auth.get("authorization_id") or
            state.get("attempt_count", {}).get(case_id) != 1 or state.get("case_state", {}).get(case_id) != "ATTEMPT_RESERVED" or
            any(row.get("event") == "RUN_CREATED" for row in rows) or
            not any(row.get("event") == "CASE_ATTEMPT_RESERVED" and row.get("case_id") == case_id for row in rows)):
        raise SystemExit("pre-run failure state is not safe to close")
    target = PersonalDevDatabase(ROOT)
    engine, factory = target.open()
    try:
        with factory() as db:
            from sqlalchemy import inspect, text
            target.check_session(db)
            tables = set(inspect(db.get_bind()).get_table_names())
            runs = db.execute(text("SELECT COUNT(*) FROM runs")).scalar_one() if "runs" in tables else -1
            pending = db.execute(text("SELECT COUNT(*) FROM controlled_pending_actions")).scalar_one() if "controlled_pending_actions" in tables else -1
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
        if target.identity != state.get("database_identity") or runs != 0 or pending != 0 or marker.lease_id is not None or marker.receipt is not None:
            raise SystemExit("database is not verifiably idle; preserve the execution state")
    finally:
        engine.dispose()
    quota = TaskQuotaLedger(TASK, task_id=auth["task_id"], seed_sha256=auth["task_ledger_seed_sha256"])
    qstate = quota.read()
    record = qstate.get("case_records", {}).get(case_id, {})
    if record.get("status") != "ATTEMPT_RESERVED" or qstate["attempts"].get(case_id) != 1:
        raise SystemExit("task-level one-shot reservation not found")
    quota.settle_case(case_id, run_created=False, provider_requests=0, tokens=0,
        local_estimated_usd=0.0, elapsed_seconds=1, status="CLOSED_NO_RUN_PRE_EXECUTION_ERROR")
    at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    journal = DurableAttemptJournal(JOURNAL, batch_id=auth["authorization_id"])
    journal.append("CASE_EXCEPTION_STOP", case_id=case_id, run_created=False, run_ids=[],
        failure_phase="PRE_RUN_LOCAL_SETUP", error_type="NameError", provider_requests=0,
        business_read_calls=0, cleanup_status="NOT_APPLICABLE_NO_RUN",
        task_attempt_consumed=True, replay_prohibited=True)
    journal.append("BATCH_CLOSED", closed_at_utc=at, completed_case_ids=[],
        not_run_case_ids=auth["case_ids"], final_task_ledger=quota.read()["spent"])
    state.update({"case_state": {**state["case_state"], case_id: "CLOSED_NO_RUN_PRE_EXECUTION_ERROR"},
        "cleanup_status": "NOT_APPLICABLE_NO_RUN", "run_created": False,
        "agent_started": False, "provider_requests": 0, "actual_tool_calls": 0,
        "failure_phase": "PRE_RUN_LOCAL_SETUP", "error_type": "NameError",
        "replay_prohibited_case_ids": [case_id], "closed_at_utc": at})
    state_path = STATE.with_suffix(STATE.suffix + ".closed.tmp")
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state_path.replace(STATE)
    auth.update({"status": "CLOSED_STOPPED", "closed_at_utc": at,
        "close_reason": "LOCAL_PRE_RUN_SETUP_FAILURE; one-shot attempt consumed; no Run or external calls"})
    auth_tmp = AUTH.with_suffix(AUTH.suffix + ".closed.tmp")
    auth_tmp.write_text(json.dumps(auth, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    auth_tmp.replace(AUTH)
    journal.release_lock_after_safe_closeout()
    report = {"schema_version": "m62-r9-pre-run-local-failure-closeout-r1",
        "authorization_id": auth["authorization_id"], "case_id": case_id,
        "attempt_consumed": True, "application_run_created": False, "provider_requests": 0,
        "tokens": 0, "local_estimated_usd": 0.0, "runtime_seconds_charged": 1,
        "failure_phase": "PRE_RUN_LOCAL_SETUP", "error_type": "NameError",
        "cleanup_status": "NOT_APPLICABLE_NO_RUN", "personal_database_identity": target.identity,
        "database_idle_verified": True, "authorization_final_status": "CLOSED_STOPPED",
        "no_replay": True, "historical_r8_untouched": True,
        "remaining_r9_case_ids": [x for x in auth["case_ids"] if x != case_id],
        "recorded_at_utc": at}
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"closed": True, "authorization_id": auth["authorization_id"],
        "case_id": case_id, "run_created": False, "provider_requests": 0,
        "task_attempt_consumed": True, "remaining_case_count": 14}, ensure_ascii=False))

if __name__ == "__main__": main()
