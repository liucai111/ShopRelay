"""Transactional, append-only SQLite store for synthetic M6.1-B experiments."""
from __future__ import annotations

from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator

from .contracts import EvalExperiment, EvalOutcome, EvalTrial, canonical_json, redact


class StoreError(RuntimeError):
    pass


class ResultStore:
    """WAL-backed store. Raw trial payloads and scores are insert-only records."""

    def __init__(self, path: Path, *, timeout_seconds: float = 15.0) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout_seconds = timeout_seconds
        with closing(self.connect()) as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                PRAGMA foreign_keys=ON;
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    config_sha256 TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    locked_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS trial_events (
                    event_id TEXT PRIMARY KEY,
                    trial_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_trial_events_trial ON trial_events(trial_id, created_at_utc);
                CREATE TABLE IF NOT EXISTS trials (
                    trial_id TEXT PRIMARY KEY,
                    case_id TEXT NOT NULL,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    lifecycle_status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS raw_results (
                    trial_id TEXT PRIMARY KEY REFERENCES trials(trial_id),
                    payload_json TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS outcomes (
                    outcome_id TEXT PRIMARY KEY,
                    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
                    rubric_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS score_records (
                    score_id TEXT PRIMARY KEY,
                    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
                    grader_id TEXT NOT NULL,
                    grader_version TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_tasks (
                    review_id TEXT PRIMARY KEY,
                    trial_id TEXT NOT NULL REFERENCES trials(trial_id),
                    status TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS review_decisions (
                    decision_id TEXT PRIMARY KEY,
                    review_id TEXT NOT NULL UNIQUE REFERENCES review_tasks(review_id),
                    reviewer_identity TEXT NOT NULL,
                    reviewer_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_limits (
                    experiment_id TEXT PRIMARY KEY REFERENCES experiments(experiment_id),
                    payload_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS budget_events (
                    event_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    reservation_id TEXT,
                    event_type TEXT NOT NULL,
                    category TEXT NOT NULL,
                    calls INTEGER NOT NULL,
                    tokens INTEGER,
                    estimated_usd REAL,
                    billed_usd REAL,
                    payload_json TEXT NOT NULL,
                    created_at_utc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_budget_events_experiment ON budget_events(experiment_id, event_type);
                CREATE TABLE IF NOT EXISTS store_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
            """)
            connection.execute("INSERT OR IGNORE INTO store_meta(key,value) VALUES('schema_version','1')")
            schema_version = connection.execute("SELECT value FROM store_meta WHERE key='schema_version'").fetchone()[0]
            if schema_version != "1":
                raise StoreError("unsupported result store schema version")
            for table in ("experiments", "trial_events", "trials", "raw_results", "outcomes", "score_records", "review_tasks", "review_decisions", "budget_limits", "budget_events"):
                for operation in ("UPDATE", "DELETE"):
                    trigger = f"deny_{table}_{operation.lower()}"
                    connection.execute(f"CREATE TRIGGER IF NOT EXISTS {trigger} BEFORE {operation} ON {table} BEGIN SELECT RAISE(ABORT, 'append-only table'); END")

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=self.timeout_seconds, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={int(self.timeout_seconds * 1000)}")
        return connection

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def lock_experiment(self, experiment: EvalExperiment, locked_at_utc: str) -> None:
        payload = canonical_json(experiment.to_dict())
        with self.transaction() as db:
            row = db.execute("SELECT config_sha256,payload_json FROM experiments WHERE experiment_id=?", (experiment.experiment_id,)).fetchone()
            if row:
                if row["config_sha256"] != experiment.config_sha256 or row["payload_json"] != payload:
                    raise StoreError("experiment is immutable; configuration changed; create a new experiment_id")
                return
            db.execute("INSERT INTO experiments VALUES(?,?,?,?)", (experiment.experiment_id, experiment.config_sha256, payload, locked_at_utc))

    def add_trial_event(self, event_id: str, trial_id: str, event_type: str, payload: dict[str, Any], at: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO trial_events VALUES(?,?,?,?,?)", (event_id, trial_id, event_type, canonical_json(payload), at))

    def append_trial(self, trial: EvalTrial, at: str) -> None:
        payload = canonical_json(trial.to_dict())
        from .contracts import sha256_json
        with self.transaction() as db:
            db.execute("INSERT INTO trials VALUES(?,?,?,?,?,?,?)", (trial.trial_id, trial.case_id, trial.experiment_id, trial.lifecycle_status, payload, sha256_json(trial.to_dict()), at))

    def append_trial_bundle(self, *, trial: EvalTrial, raw_result: dict[str, Any], outcome: EvalOutcome,
                            scores: list[dict[str, Any]], at: str, terminal_event: dict[str, Any],
                            review_task: dict[str, Any] | None = None) -> None:
        """Atomically append immutable execution, raw result, score and terminal event records."""
        from .contracts import sha256_json
        trial_payload = canonical_json(trial.to_dict())
        raw_clean = redact(raw_result)
        with self.transaction() as db:
            db.execute("INSERT INTO trials VALUES(?,?,?,?,?,?,?)", (trial.trial_id, trial.case_id, trial.experiment_id,
                trial.lifecycle_status, trial_payload, sha256_json(trial.to_dict()), at))
            db.execute("INSERT INTO raw_results VALUES(?,?,?,?)", (trial.trial_id, canonical_json(raw_clean), sha256_json(raw_clean), at))
            db.execute("INSERT INTO outcomes VALUES(?,?,?,?,?)", (outcome.outcome_id, outcome.trial_id, outcome.rubric_version,
                canonical_json(outcome.to_dict()), outcome.created_at_utc))
            for score in scores:
                required = {"score_id", "trial_id", "grader_id", "grader_version"}
                if not required <= set(score):
                    raise StoreError("score record missing required identity fields")
                db.execute("INSERT INTO score_records VALUES(?,?,?,?,?,?)", (score["score_id"], score["trial_id"], score["grader_id"],
                    score["grader_version"], canonical_json(score), at))
            if review_task is not None:
                db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,?)", (review_task["review_id"], review_task["trial_id"],
                    review_task["status"], canonical_json(review_task), at))
            db.execute("INSERT INTO trial_events VALUES(?,?,?,?,?)", (terminal_event["event_id"], trial.trial_id,
                terminal_event["event_type"], canonical_json(terminal_event["payload"]), terminal_event["created_at_utc"]))

    def append_raw_result(self, trial_id: str, result: dict[str, Any], at: str) -> None:
        from .contracts import sha256_json
        clean = redact(result)
        with self.transaction() as db:
            db.execute("INSERT INTO raw_results VALUES(?,?,?,?)", (trial_id, canonical_json(clean), sha256_json(clean), at))

    def raw_result(self, trial_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT payload_json FROM raw_results WHERE trial_id=?", (trial_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def append_outcome(self, outcome: EvalOutcome) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO outcomes VALUES(?,?,?,?,?)", (outcome.outcome_id, outcome.trial_id, outcome.rubric_version, canonical_json(outcome.to_dict()), outcome.created_at_utc))

    def append_score(self, score: dict[str, Any], at: str) -> None:
        required = {"score_id", "trial_id", "grader_id", "grader_version"}
        if not required <= set(score):
            raise StoreError("score record missing required identity fields")
        with self.transaction() as db:
            db.execute("INSERT INTO score_records VALUES(?,?,?,?,?,?)", (score["score_id"], score["trial_id"], score["grader_id"], score["grader_version"], canonical_json(score), at))

    def create_review_task(self, review: dict[str, Any], at: str) -> None:
        with self.transaction() as db:
            db.execute("INSERT INTO review_tasks VALUES(?,?,?,?,?)", (review["review_id"], review["trial_id"], review["status"], canonical_json(review), at))

    def append_review_decision(self, decision: dict[str, Any]) -> None:
        if decision.get("reviewer_type") not in {"HUMAN", "SYNTHETIC_NOT_HUMAN"}:
            raise StoreError("reviewer_type must explicitly identify human or synthetic reviewer")
        with self.transaction() as db:
            row = db.execute("SELECT status FROM review_tasks WHERE review_id=?", (decision["review_id"],)).fetchone()
            if not row:
                raise StoreError("review task not found")
            if row["status"] != "PENDING_HUMAN_REVIEW":
                raise StoreError("review task is not pending")
            db.execute("INSERT INTO review_decisions VALUES(?,?,?,?,?,?)", (decision["decision_id"], decision["review_id"], decision["reviewer_identity"], decision["reviewer_type"], canonical_json(decision), decision["created_at_utc"]))

    def review_status(self, review_id: str) -> str | None:
        with closing(self.connect()) as db:
            task = db.execute("SELECT status FROM review_tasks WHERE review_id=?", (review_id,)).fetchone()
            if task is None:
                return None
            decision = db.execute("SELECT 1 FROM review_decisions WHERE review_id=?", (review_id,)).fetchone()
        return "REVIEWED" if decision else task["status"]

    def trial(self, trial_id: str) -> dict[str, Any] | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT payload_json FROM trials WHERE trial_id=?", (trial_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def trial_event_types(self, trial_id: str) -> list[str]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT event_type FROM trial_events WHERE trial_id=? ORDER BY rowid", (trial_id,)).fetchall()
        return [row[0] for row in rows]

    def trials(self, experiment_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT payload_json FROM trials WHERE experiment_id=? ORDER BY created_at_utc,trial_id", (experiment_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def outcomes(self, trial_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT payload_json FROM outcomes WHERE trial_id=? ORDER BY created_at_utc,outcome_id", (trial_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def outcomes_for_experiment(self, experiment_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT payload_json FROM outcomes WHERE trial_id IN (SELECT trial_id FROM trials WHERE experiment_id=?) ORDER BY created_at_utc,outcome_id", (experiment_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def reviews_for_experiment(self, experiment_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT review_id,trial_id,status,payload_json FROM review_tasks WHERE trial_id IN (SELECT trial_id FROM trials WHERE experiment_id=?) ORDER BY created_at_utc,review_id", (experiment_id,)).fetchall()
            records = []
            for row in rows:
                decision = db.execute("SELECT payload_json FROM review_decisions WHERE review_id=?", (row["review_id"],)).fetchone()
                records.append({"task":json.loads(row["payload_json"]), "status":"REVIEWED" if decision else row["status"],
                                "decision":json.loads(decision[0]) if decision else None})
        return records

    def trial_events(self, trial_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT event_id,event_type,payload_json,created_at_utc FROM trial_events WHERE trial_id=? ORDER BY rowid", (trial_id,)).fetchall()
        return [{"event_id":row["event_id"],"event_type":row["event_type"],"payload":json.loads(row["payload_json"]),"created_at_utc":row["created_at_utc"]} for row in rows]

    def scores(self, experiment_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT payload_json FROM score_records WHERE trial_id IN (SELECT trial_id FROM trials WHERE experiment_id=?) ORDER BY created_at_utc,score_id", (experiment_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def budget_snapshot(self, experiment_id: str) -> list[dict[str, Any]]:
        with closing(self.connect()) as db:
            rows = db.execute("SELECT event_type,category,calls,tokens,estimated_usd,billed_usd,payload_json,reservation_id FROM budget_events WHERE experiment_id=? ORDER BY rowid", (experiment_id,)).fetchall()
        return [dict(row) for row in rows]

    def budget_limits(self, experiment_id: str) -> dict[str, Any] | None:
        """Return the immutable experiment budget definition, if one exists."""
        with closing(self.connect()) as db:
            row = db.execute("SELECT payload_json FROM budget_limits WHERE experiment_id=?",
                             (experiment_id,)).fetchone()
        if row is None:
            return None
        value = json.loads(row[0])
        return value if isinstance(value, dict) else None
