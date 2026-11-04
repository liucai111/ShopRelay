"""D1 personal Dev coordinator. Offline fixture work is separate from execution.

Synthetic read adapters remain owner-scoped, and no expected outcome drives routing.
The separate personal diagnostic authorization never changes formal Registry policy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import uuid

from sqlalchemy import delete, select, text

from app.m1.models import (ConversationSession, GraphExecutionRecord, Message, Order,
    PendingAction, Run, RunEvent, SessionMemorySummary, Shipment, Trace, User, ObservabilityRecord)
from app.m3.order_tools import SqlAlchemyOrderReadRepository
from .contracts import EvalCase, Verdict, redact, sha256_json
from .personal_dev_database import PersonalDevDatabase
from .shipment_fixture_reset import load_bundle_shipments, cleanup_bundle_shipments
from .fixture_read_models import (READ_TOOLS, fact_rows, create_projection_table, FactStore,
    FixtureOrderRepository, FixtureProductRepository, FixtureAfterSalesRepository, FixtureCouponRepository,
    fixture_retrieval, ReadAllowance)

OWNER_MAP = {"SYN-USER-A": "d1-personal-a", "SYN-USER-B": "d1-personal-b"}
SUPPORTED_TOOLS = READ_TOOLS
OS_ISOLATION_STATUS = Verdict.NOT_APPLICABLE.value


def implementation_digest(root: Path) -> str:
    paths = ("eval/m6_1/personal_dev.py", "eval/m6_1/personal_dev_database.py",
        "eval/m6_1/personal_dev_runtime.py", "eval/m6_1/shipment_fixture_reset.py",
        "eval/m6_1/shipment_fixture_loader.py", "app/orchestration/runtime.py",
        "app/m1/models.py", "app/m1/services.py", "scripts/run_m6_1_d1_personal_dev.py",
        "app/m0/config.py", "app/m1/chat.py", "app/m1/routing.py", "app/m1/chat_responses.py",
        "app/m2/policy_execution.py", "app/orchestration/adapters.py", "app/orchestration/graph.py",
        "app/orchestration/state.py", "app/orchestration/checkpointer.py",
        "app/orchestration/observability.py", "app/orchestration/persistence.py", "app/m5/memory.py")
    paths += ("eval/m6_1/fixture_read_models.py", "app/m3/agents.py", "app/m3/policy_tools.py",
        "app/m3/order_tools.py", "app/m3/product_tools.py", "app/m2/retrieval.py")
    paths += ("eval/m6_1/personal_dev_authorization.py", "eval/m6_1/budget.py", "eval/m6_1/store.py")
    paths += ("app/m1/chat_execution.py",)
    paths += ("eval/m6_1/personal_dev_diagnostic_checks.py",)
    paths += ("scripts/check_m6_1_d1_diagnostic.py",)
    paths += ("scripts/recompute_m6_1_d1_diagnostic_r7.py",)
    paths += ("eval/m6_1/personal_dev_scoring.py", "eval/m6_1/contracts.py")
    paths += ("eval/m6_1/personal_dev_budget_carryforward.py",)
    # Bind user-facing result projection and specialist dispatch/audit transport;
    # both are part of the personal D1 execution and evidence contract.
    paths += ("app/m1/api.py", "app/m3/graph.py")
    paths += ("eval/m6_1/provider_diagnostic.py", "scripts/run_m6_1_d1_provider_diagnostic_r6.py")
    paths += ("eval/m6_1/personal_smoke.py", "scripts/run_m6_1_d1_personal_smoke.py")
    # M6.2 exact-subset coordinator is a runtime dependency when the dedicated
    # personal Dev batch CLI is used; include both in future frozen digests.
    paths += ("eval/m6_1/personal_dev_batch_m62.py", "scripts/run_m6_2_r324_personal_dev_batch.py",
        "scripts/run_m6_2_r324_personal_dev_batch_r5.py", "scripts/prepare_m6_2_r324_batch_r5.py",
        "scripts/run_m6_2_r324_personal_dev_batch_r6.py",
        "scripts/run_m6_2_r324_personal_dev_batch_r7.py",
        "app/m3/after_sales_tools.py")
    return sha256_json({p: hashlib.sha256((root / p).read_bytes()).hexdigest() for p in paths})


def create_budget_ledger(target, permit, limits, settings):
    """Lock an honest PERSONAL_DEV config, reusing the existing ledger/store.

    This does not call the B-only preregistration function or claim a Dev Smoke
    experiment. Formal and personal diagnostic grants use separate entry checks.
    """
    from .contracts import EvalExperiment
    from .budget import BudgetLedger
    from .store import ResultStore
    config = {"mode": "PERSONAL_DEV", "asset_sha256": permit["asset_sha256"],
        "case_ids": permit["case_ids"], "database_identity": target.identity,
        "code_sha256": permit["code_sha256"], "budget": limits.to_dict(),
        "model": settings.model, "os_isolation": OS_ISOLATION_STATUS}
    if permit.get("authorization_type") == "PERSONAL_DEV_AGENT_EVAL":
        config["mode"] = "PERSONAL_DEV_AGENT_EVAL"
    elif permit.get("authorization_type") == "PERSONAL_DEV_TECHNICAL_SMOKE":
        config["mode"] = "PERSONAL_DEV_TECHNICAL_SMOKE"
    tool_permissions = tuple(sorted(permit.get("allowed_tool_capabilities", SUPPORTED_TOOLS)))
    if not set(tool_permissions).issubset(SUPPORTED_TOOLS):
        raise D1PreflightError("BUDGET_EXPERIMENT_TOOL_SCOPE_INVALID")
    store = ResultStore(target.root / "data/eval/d1-personal-budget.sqlite3")
    experiment = EvalExperiment(experiment_id=permit["authorization_id"], config_snapshot=config,
        config_sha256=sha256_json(config), git_commit=permit["git_commit"], knowledge_index_version=None,
        model_versions={"provider": settings.model}, prompt_versions={"application": "EXISTING_ROUTE_PROMPT"},
        strategy=config["mode"], tool_permissions=tool_permissions, budget=limits.to_dict(),
        data_version=permit.get("data_version", "m6.1-d1-dev-draft-r3.2.4"),
        scoring_version=("PERSONAL_DEV_DIAGNOSTIC" if config["mode"] == "PERSONAL_DEV_AGENT_EVAL" else "NOT_GRADED"))
    store.lock_experiment(experiment, datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    return BudgetLedger(store, experiment.experiment_id, limits)


class D1PreflightError(ValueError):
    pass


def validate_execution_budget(limits, message):
    if (limits.max_retries != 0 or limits.max_concurrent_trials != 1 or not 0 <= limits.tool_calls <= 32 or
            limits.max_calls_per_trial != limits.tool_calls or not 1 <= limits.provider_calls <= 8 or
            limits.embedding_calls != 0 or limits.judge_calls != 0):
        raise D1PreflightError("SINGLE_CASE_READ_ONLY_ENVELOPE_REQUIRED")
    if limits.total_tokens <= 4096 + len(message.encode("utf-8")) or limits.estimated_usd <= 0:
        raise D1PreflightError("BUDGET_INSUFFICIENT_BEFORE_RUN_CLAIM")


def plan_case(bundle: dict, case_id: str) -> dict:
    if bundle.get("split") != "dev":
        raise D1PreflightError("PERSONAL_DEV_SPLIT_REQUIRED")
    matches = [c for c in bundle["cases"] if c["case_id"] == case_id]
    if len(matches) != 1:
        raise D1PreflightError("EXACT_CASE_ID_REQUIRED")
    case = matches[0]
    EvalCase.from_dict(case)
    if case["split"] != "dev" or case["data_version"] != bundle["version"]:
        raise D1PreflightError("CASE_VERSION_OR_SPLIT_MISMATCH")
    return plan_readonly_case(bundle, case, OWNER_MAP)


def plan_readonly_case(bundle: dict, case: dict, owner_map: dict[str, str]) -> dict:
    """Plan source-preserving fixtures; callers validate asset/version at their boundary."""
    case_id = case["case_id"]
    message = case["input"].get("user_query")
    alias = case["input"].get("user_id")
    if not isinstance(message, str) or not message.strip() or alias not in owner_map:
        raise D1PreflightError("MESSAGE_OR_owner_mapPING_MISSING")
    calls = case["input"].get("expected_tool_calls", [])
    if any(c.get("mode") != "read_only" or c.get("tool_id") not in SUPPORTED_TOOLS for c in calls):
        raise D1PreflightError("FIXTURE_CAPABILITY_NOT_IMPLEMENTED: read-only contracts only")
    try:
        facts = fact_rows(bundle, case, owner_map)
    except (ValueError, KeyError) as exc:
        raise D1PreflightError(str(exc)) from exc
    # Missing read arguments are left to the genuine application route so it can
    # ask the user to clarify or return UNKNOWN. Never synthesize those facts.
    rows = [json.loads(r["payload"]) for r in facts if r["kind"] == "orders_and_snapshots"]
    if len({r["order_id"] for r in rows}) != len(rows):
        raise D1PreflightError("EXACT_ORDER_FIXTURES_REQUIRED")
    orders = []
    for row in rows:
        status = row.get("status", row.get("order_status"))
        if (status is not None and not isinstance(status, str) or
                row.get("paid_cents") is not None and (isinstance(row["paid_cents"], bool) or not isinstance(row["paid_cents"], int))):
            raise D1PreflightError("ORDER_FACT_TYPE_INVALID")
        if "status" in row and "order_status" in row and row["status"] != row["order_status"]:
            raise D1PreflightError("ORDER_STATUS_CONFLICT")
        if row.get("owner_id") not in owner_map:
            raise D1PreflightError("ORDER_OWNER_ALIAS_UNKNOWN")
        if status is not None and row.get("paid_cents") is not None:
            orders.append({"order_id": row["order_id"], "user_id": owner_map[row["owner_id"]],
                "status": status, "paid_cents": row["paid_cents"], "paid_at": row.get("paid_at"),
                "delivered_at": row.get("delivered_at"), "version": row.get("version", 1)})
    order_ids = {r["order_id"] for r in rows}
    shipments = [r for r in bundle["fixtures"]["shipments"] if r["order_id"] in order_ids]
    if any(r["order_id"] not in {o["order_id"] for o in orders} for r in shipments):
        raise D1PreflightError("SHIPMENT_REQUIRES_COMPLETE_ORDER_FK_NO_SENTINEL")
    scoped = {"fixtures": {"users": bundle["fixtures"]["users"],
        "orders_and_snapshots": rows, "shipments": shipments}}
    return {"case_id": case_id, "message": message, "owner_id": owner_map[alias],
        "orders": orders, "bundle": scoped, "data_version": bundle["version"], "facts": facts,
        "order_ids": sorted(order_ids), "case": case,
        "owner_map": owner_map, "knowledge_version": bundle["fixtures"].get("knowledge_version", "SYNTHETIC-PROBE"),
        "evaluation_at": bundle.get("scenario_clock_utc", datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))}


@dataclass
class FixtureReceipt:
    lease_id: str
    database_identity: str
    user_ids: tuple[str, ...]
    order_ids: tuple[str, ...]
    shipment_receipt: object
    session_id: str | None = None
    run_id: str | None = None
    core_order_ids: tuple[str, ...] = ()
    fact_receipts: tuple[dict, ...] = ()


class D1Coordinator:
    def __init__(self, target: PersonalDevDatabase, factory):
        self.target, self.factory = target, factory
        with factory() as db:
            target.check_session(db)

    def prepare(self, plan: dict) -> FixtureReceipt:
        lease = uuid.uuid4().hex
        self.target.acquire(self.factory, lease)
        users = tuple(sorted({plan["owner_id"], *(r["user_id"] for r in plan["orders"]),
            *(r["owner_id"] for r in plan.get("facts", []) if r["owner_id"])}))
        orders = tuple(plan.get("order_ids", [r["order_id"] for r in plan["orders"]]))
        core_orders = tuple(r["order_id"] for r in plan["orders"])
        try:
            with self.factory() as db, db.begin():
                # SQLite's legacy driver does not BEGIN for SELECT; a first
                # SAVEPOINT would otherwise commit independently of rollback.
                if db.get_bind().dialect.name == "sqlite":
                    db.connection().exec_driver_sql("BEGIN IMMEDIATE")
                self.target.check_session(db)
                create_projection_table(db)
                if (db.execute(select(User.user_id).where(User.user_id.in_(users))).first() or
                        db.execute(select(Order.order_id).where(Order.order_id.in_(orders))).first()):
                    raise D1PreflightError("FIXTURE_ID_COLLISION_NO_OVERWRITE")
                db.add_all([User(user_id=u, display_name="D1 synthetic personal Dev owner") for u in users])
                db.flush()
                db.add_all([Order(**r) for r in plan["orders"]])
                db.flush()
                # Force a real outer SQLite transaction before nested savepoints.
                shipment = load_bundle_shipments(db, plan["bundle"], owner_id_map=plan.get("owner_map", OWNER_MAP),
                    probe_id=lease[:24], personal_database=self.target)
                for fact in plan.get("facts", []):
                    db.execute(text("INSERT INTO d1_fixture_facts "
                        "(lease_id,kind,source_id,resource_id,owner_id,payload,digest) "
                        "VALUES (:lease_id,:kind,:source_id,:resource_id,:owner_id,:payload,:digest)"),
                        dict(fact, lease_id=lease))
                receipt = FixtureReceipt(lease, self.target.identity, users, orders, shipment,
                    core_order_ids=core_orders, fact_receipts=tuple(plan.get("facts", [])))
                db.execute(text("UPDATE d1_personal_target SET receipt=:receipt WHERE lease_id=:lease"),
                    {"receipt": json.dumps(asdict(receipt)), "lease": lease})
            return receipt
        except Exception:
            self.target.release(self.factory, lease)
            raise

    def _check_receipt(self, db, receipt):
        self.target.check_session(db)
        row = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
        if receipt.database_identity != self.target.identity or row.lease_id != receipt.lease_id:
            raise D1PreflightError("RECEIPT_DATABASE_OR_LEASE_MISMATCH")
        if json.loads(row.receipt) != json.loads(json.dumps(asdict(receipt))):
            raise D1PreflightError("RECEIPT_CONTENT_MISMATCH")

    def cleanup(self, receipt: FixtureReceipt) -> None:
        with self.factory() as db, db.begin():
            if db.get_bind().dialect.name == "sqlite":
                db.connection().exec_driver_sql("BEGIN IMMEDIATE")
            self._check_receipt(db, receipt)
            if receipt.session_id:
                runs = db.execute(select(Run).where(Run.session_id == receipt.session_id)).scalars().all()
                if len(runs) != 1 or runs[0].run_id != receipt.run_id or runs[0].status not in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}:
                    raise D1PreflightError("NONTERMINAL_OR_UNKNOWN_RUN_RETAINED")
                session = db.get(ConversationSession, receipt.session_id)
                if session is None or session.user_id != runs[0].user_id or runs[0].user_id not in receipt.user_ids:
                    raise D1PreflightError("SESSION_OR_RUN_OWNER_CHANGED")
                if db.execute(select(PendingAction.action_id).where(PendingAction.session_id == receipt.session_id)).first():
                    raise D1PreflightError("PENDING_ACTION_RETAINED")
                for model in (Trace, ObservabilityRecord, RunEvent, GraphExecutionRecord):
                    db.execute(delete(model).where(model.run_id == receipt.run_id))
                db.execute(delete(SessionMemorySummary).where(SessionMemorySummary.session_id == receipt.session_id))
                db.execute(delete(Message).where(Message.session_id == receipt.session_id))
                db.execute(delete(Run).where(Run.run_id == receipt.run_id))
                db.execute(delete(ConversationSession).where(ConversationSession.session_id == receipt.session_id))
            cleanup_bundle_shipments(db, receipt.shipment_receipt, personal_database=self.target)
            actual = [dict(r._mapping) for r in db.execute(text("SELECT kind,source_id,resource_id,owner_id,payload,digest "
                "FROM d1_fixture_facts WHERE lease_id=:lease ORDER BY source_id"), {"lease": receipt.lease_id})]
            if actual != sorted(list(receipt.fact_receipts), key=lambda r: r["source_id"]):
                raise D1PreflightError("FIXTURE_FACT_RECEIPT_MISMATCH")
            db.execute(text("DELETE FROM d1_fixture_facts WHERE lease_id=:lease"), {"lease": receipt.lease_id})
            # A foreign/new row on one of our orders blocks cleanup; do not erase it.
            if db.execute(select(Shipment.shipment_id).where(Shipment.order_id.in_(receipt.order_ids))).first():
                raise D1PreflightError("UNRECEIPTED_SHIPMENT_RETAINED")
            rows = db.execute(select(Order).where(Order.order_id.in_(receipt.order_ids))).scalars().all()
            if {r.order_id for r in rows} != set(receipt.core_order_ids) or any(r.user_id not in receipt.user_ids for r in rows):
                raise D1PreflightError("PRELOADED_ORDER_SCOPE_CHANGED")
            for row in rows:
                db.delete(row)
            db.flush()
            for user_id in receipt.user_ids:
                db.delete(db.get(User, user_id))
            db.flush()  # Unexpected dependencies roll back the entire cleanup.
            released = db.execute(text("UPDATE d1_personal_target SET lease_id=NULL,receipt=NULL "
                "WHERE profile=:profile AND identity=:identity AND lease_id=:lease"),
                {"profile": self.target.profile, "identity": receipt.database_identity, "lease": receipt.lease_id})
            if released.rowcount != 1:
                raise D1PreflightError("CLEANUP_ATOMIC_LEASE_RELEASE_MISMATCH")

    def diagnostic_cleanup_evidence(self, receipt: FixtureReceipt, *, cleaned: bool) -> dict:
        """Inspect only IDs in this receipt; never enumerate unrelated rows."""
        with self.factory() as db:
            self.target.check_session(db)
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            pending = 0
            if receipt.session_id:
                pending = len(db.execute(select(PendingAction.action_id).where(
                    PendingAction.session_id == receipt.session_id)).all())
            residual = 0
            for model, column, values in (
                    (User, User.user_id, receipt.user_ids),
                    (Order, Order.order_id, receipt.order_ids),
                    (Shipment, Shipment.order_id, receipt.order_ids)):
                if values:
                    residual += len(db.execute(select(column).where(column.in_(values))).all())
            if receipt.lease_id:
                residual += int(db.execute(text("SELECT count(*) FROM d1_fixture_facts WHERE lease_id=:lease"),
                    {"lease": receipt.lease_id}).scalar_one())
            if receipt.session_id:
                residual += len(db.execute(select(ConversationSession.session_id).where(
                    ConversationSession.session_id == receipt.session_id)).all())
                residual += len(db.execute(select(Run.run_id).where(Run.run_id == receipt.run_id)).all())
            released = marker.lease_id is None and marker.receipt is None
            return {"pending_actions": pending,
                "cleanup_status": "CLEANED_AND_RELEASED" if cleaned and released else "NOT_CLEANED_OR_LEASE_RETAINED",
                "residual_case_rows": residual,
                "lease_released": released,
                "lease_id": receipt.lease_id,
                "database_identity": self.target.identity}

    def diagnostic_cleanup_safety(self, receipt: FixtureReceipt | None, archive_path: Path) -> dict:
        """Separate diagnostic failure from whether an exact receipt can safely be cleaned."""
        if receipt is None or not archive_path.is_file():
            return {"safe_to_cleanup": False, "reason": "RECEIPT_OR_ARCHIVE_MISSING"}
        try:
            archived = json.loads(archive_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"safe_to_cleanup": False, "reason": "ARCHIVE_UNREADABLE"}
        outer = archived.get("result") if isinstance(archived, dict) else None
        outer = outer if isinstance(outer, dict) else {}
        observations = outer.get("observations") if isinstance(outer.get("observations"), dict) else {}
        run_rows = observations.get("runs") if isinstance(observations.get("runs"), list) else []
        event_rows = observations.get("run_events") if isinstance(observations.get("run_events"), list) else []
        matches = [row for row in run_rows if isinstance(row, dict) and row.get("run_id") == receipt.run_id]
        archive_terminal = outer.get("status") in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}
        if (outer.get("run_id") != receipt.run_id or not archive_terminal or len(matches) != 1 or
                not isinstance(event_rows, list)):
            return {"safe_to_cleanup": False, "reason": "ARCHIVE_RUN_STATE_OR_EVIDENCE_INCOMPLETE"}
        try:
            with self.factory() as db:
                self.target.check_session(db)
                self._check_receipt(db, receipt)
                runs = db.execute(select(Run).where(Run.session_id == receipt.session_id)).scalars().all()
                if (len(runs) != 1 or runs[0].run_id != receipt.run_id or
                        runs[0].status not in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}):
                    return {"safe_to_cleanup": False, "reason": "DATABASE_RUN_NOT_TERMINAL_OR_AMBIGUOUS"}
                session = db.get(ConversationSession, receipt.session_id)
                if session is None or session.user_id != runs[0].user_id or runs[0].user_id not in receipt.user_ids:
                    return {"safe_to_cleanup": False, "reason": "RUN_OWNER_OR_SESSION_MISMATCH"}
                pending = db.execute(select(PendingAction.action_id).where(
                    PendingAction.session_id == receipt.session_id)).first()
                if pending:
                    return {"safe_to_cleanup": False, "reason": "PENDING_ACTION_PRESENT"}
        except Exception as exc:
            return {"safe_to_cleanup": False, "reason": "RECEIPT_OR_DATABASE_CHECK_FAILED",
                    "error_type": type(exc).__name__}
        return {"safe_to_cleanup": True, "reason": "TERMINAL_RUN_ARCHIVED_RECEIPT_MATCHES_NO_PENDING_ACTION",
                "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                "run_id": receipt.run_id, "database_identity": self.target.identity}

    def offline_query(self, bundle, case_id):
        """Fixed repository probe through the real lifecycle; never calls an Agent."""
        plan = plan_case(bundle, case_id)
        receipt = self.prepare(plan)
        try:
            repo = self.repositories(plan, receipt)["order"]
            return {order: repo.track_shipment(order, plan["owner_id"]) for order in receipt.order_ids}
        finally:
            self.cleanup(receipt)

    def repositories(self, plan, receipt, maximum=32):
        from app.m3.policy_tools import PolicyReadService
        store = FactStore(self.factory, self.target, receipt.lease_id, ReadAllowance(maximum))
        retrieval = fixture_retrieval(store, plan["knowledge_version"], plan["evaluation_at"])
        return {"order": FixtureOrderRepository(store), "product": FixtureProductRepository(store),
            "after_sales": FixtureAfterSalesRepository(store), "coupon": FixtureCouponRepository(store),
            "policy": PolicyReadService(retrieval, evaluation_at=plan["evaluation_at"]),
            "retrieval": retrieval, "store": store}

    def validate_admission(self, *, bundle, case_id, asset, grant, permit, ledger, settings):
        """Full runtime admission without fixture writes, Run creation or SDK."""
        if (asset.split != "dev" or grant is None or not grant.valid_for(asset) or
                not permit or permit.get("actor_type") != "CHAT_USER_SELF_CONFIRMED"):
            raise D1PreflightError("REGISTRY_GRANT_AND_EXPLICIT_RUNTIME_PERMIT_REQUIRED")
        if (permit.get("asset_sha256") != asset.sha256 or permit.get("case_ids") != [case_id] or
                permit.get("database_identity") != self.target.identity or
                permit.get("code_sha256") != implementation_digest(self.target.root) or
                permit.get("model") != settings.model or permit.get("provider_base_url") != settings.base_url or
                permit.get("budget_sha256") != sha256_json(ledger.limits.to_dict()) or
                datetime.fromisoformat(permit["expires_at_utc"].replace("Z", "+00:00")) <= datetime.now(timezone.utc)):
            raise D1PreflightError("RUNTIME_PERMIT_SCOPE_MISMATCH")
        raw_path = (self.target.root / asset.source_path).resolve()
        allowed_path = self.target.root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
        if raw_path != allowed_path or hashlib.sha256(raw_path.read_bytes()).hexdigest() != asset.sha256:
            raise D1PreflightError("EXACT_D1_DEV_ASSET_HASH_REQUIRED")
        if json.loads(raw_path.read_text(encoding="utf-8")) != bundle or asset.version != bundle["version"]:
            raise D1PreflightError("BUNDLE_DIFFERS_FROM_REGISTERED_ASSET")
        limits = ledger.limits
        plan = plan_case(bundle, case_id)
        validate_execution_budget(limits, plan["message"])
        if ledger.snapshot()["events"]:
            raise D1PreflightError("AUTHORIZATION_LEDGER_ALREADY_USED_NO_REPLAY")
        if not settings.api_key.strip():
            raise D1PreflightError("PROVIDER_CREDENTIAL_CONFIGURATION_REQUIRED")
        return plan

    def validate_personal_diagnostic_admission(self, *, bundle, case_id, authorization,
                                               limits, settings, code_revision, budget_path, authorization_path):
        """Validate one allowlisted diagnostic profile without changing formal Registry semantics."""
        from .personal_dev_authorization import (
            AUTHORIZED_ASSET_PATH, AUTHORIZED_ASSET_SHA256, AUTHORIZED_VERSION,
            load_price_evidence, personal_diagnostic_profile,
            validate_personal_authorization, validate_personal_budget,
        )
        profile = personal_diagnostic_profile(
            case_id, diagnostic_round_id=authorization.get("diagnostic_round_id"))
        if profile.check_rules_path:
            from .personal_dev_diagnostic_checks import require_diagnostic_predecessor
            require_diagnostic_predecessor(self.target.root, case_id=case_id,
                profile=profile, authorization=authorization)
        expected_authorization_path = self.target.root / profile.authorization_path
        authorization_path = Path(authorization_path)
        try:
            expected_auth_resolved = expected_authorization_path.resolve(strict=True)
            supplied_auth_resolved = authorization_path.resolve(strict=True)
        except OSError as exc:
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_AUTHORIZATION_PATH_MISSING") from exc
        if (expected_auth_resolved != expected_authorization_path.absolute() or
                supplied_auth_resolved != expected_auth_resolved):
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_AUTHORIZATION_PATH_MISMATCH")
        authorization_bytes = authorization_path.read_bytes()
        try:
            authorization_from_file = json.loads(authorization_bytes.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_AUTHORIZATION_FILE_INVALID") from exc
        if authorization_from_file != authorization:
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_AUTHORIZATION_FILE_CONTENT_MISMATCH")
        expected_budget_path = self.target.root / profile.budget_path
        budget_path = Path(budget_path)
        try:
            expected_budget_resolved = expected_budget_path.resolve(strict=False)
            supplied_budget_resolved = budget_path.resolve(strict=True)
        except OSError as exc:
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_BUDGET_PATH_MISSING") from exc
        if (expected_budget_resolved != expected_budget_path.absolute() or
                supplied_budget_resolved != expected_budget_resolved):
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_BUDGET_PATH_MISMATCH")
        price_path = self.target.root / profile.pricing_path
        pricing_record, pricing_sha256 = load_price_evidence(price_path,
            model=settings.model, endpoint=settings.base_url)
        budget_config = json.loads(budget_path.read_text(encoding="utf-8"))
        budget_file_sha256 = hashlib.sha256(budget_path.read_bytes()).hexdigest()
        raw_path = (self.target.root / AUTHORIZED_ASSET_PATH).resolve()
        expected_path = self.target.root / AUTHORIZED_ASSET_PATH
        if (raw_path != expected_path or not raw_path.is_file() or
                hashlib.sha256(raw_path.read_bytes()).hexdigest() != AUTHORIZED_ASSET_SHA256):
            raise D1PreflightError("EXACT_D1_DEV_ASSET_HASH_REQUIRED")
        raw_bundle = json.loads(raw_path.read_text(encoding="utf-8"))
        if raw_bundle != bundle or bundle.get("version") != AUTHORIZED_VERSION:
            raise D1PreflightError("BUNDLE_DIFFERS_FROM_FIXED_D1_DEV_ASSET")
        if bundle.get("split") != "dev" or bundle.get("asset_id") != "m6.1-d1-synthetic-dev-draft":
            raise D1PreflightError("PERSONAL_DEV_ASSET_SCOPE_MISMATCH")
        plan = plan_case(bundle, case_id)
        declared = plan["case"]["input"].get("expected_tool_calls", [])
        allowed = {row.get("tool_id") for row in declared if row.get("mode") == "read_only"}
        if allowed != set(profile.allowed_tools):
            raise D1PreflightError("PERSONAL_AUTHORIZATION_CASE_CAPABILITY_MISMATCH")
        check_rules_sha256 = None
        if profile.check_rules_path:
            from .personal_dev_diagnostic_checks import load_locked_rules
            rules_path = self.target.root / profile.check_rules_path
            try:
                if rules_path.resolve(strict=True) != rules_path.absolute() or not rules_path.is_file():
                    raise D1PreflightError("PERSONAL_DIAGNOSTIC_CHECK_RULES_PATH_INVALID")
                _rules, check_rules_sha256 = load_locked_rules(rules_path, case_id=case_id)
            except OSError as exc:
                raise D1PreflightError("PERSONAL_DIAGNOSTIC_CHECK_RULES_MISSING") from exc
        validate_personal_budget(limits, plan["message"], budget_config=budget_config,
            profile=profile)
        if limits.to_dict() != budget_config:
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_LOADED_BUDGET_MISMATCH")
        validate_personal_authorization(authorization,
            case_id=case_id, asset_id=bundle["asset_id"], data_version=bundle["version"],
            asset_path=AUTHORIZED_ASSET_PATH, asset_sha256=AUTHORIZED_ASSET_SHA256,
            code_sha256=implementation_digest(self.target.root), code_revision=code_revision,
            database_identity=self.target.identity, model=settings.model, endpoint=settings.base_url,
            budget=budget_config, budget_file_sha256_value=budget_file_sha256,
            pricing_sha256_value=pricing_sha256, check_rules_sha256_value=check_rules_sha256,
            profile=profile)
        if not settings.api_key.strip():
            raise D1PreflightError("PROVIDER_CREDENTIAL_CONFIGURATION_REQUIRED")

        # Preflight reads use a fresh receipt/repository and are recorded separately
        # from the one-shot execution budget. No Run, model or external network.
        receipt = self.prepare(plan)
        preflight = {"kind": profile.preflight_kind, "receipt_lease_id": receipt.lease_id,
            "database_identity": receipt.database_identity, "read_calls": 0,
            "cleanup_status": "NOT_ATTEMPTED"}
        try:
            reads = self.repositories(plan, receipt, maximum=limits.tool_calls)
            if profile.preflight_kind == "policy":
                result = reads["policy"].search(plan["message"], mode="BM25",
                    result_ref="personal-diagnostic-preflight")
                evidence = result.get("evidence") or []
                if (result.get("knowledge_version") != plan["knowledge_version"] or
                        "syn-policy-return-window" not in {row.get("chunk_id") for row in evidence}):
                    raise D1PreflightError("FIXTURE_POLICY_EVIDENCE_NOT_READABLE")
                if result.get("degraded") or result.get("failures"):
                    raise D1PreflightError("FIXTURE_POLICY_RETRIEVAL_DEGRADED")
                preflight.update({"knowledge_version": result.get("knowledge_version"),
                    "evidence_refs": [row.get("chunk_id") for row in evidence]})
            elif profile.preflight_kind == "product":
                from app.m3.product_tools import parse_product_request_many
                _explicit_ids, filters, _wants_stock = parse_product_request_many(plan["message"])
                repository = reads["product"]
                filters = repository.resolve_product_filters(plan["message"], filters)
                result = repository.search_products(filters)
                candidates = result.get("products") or []
                if result.get("source") != "shoprelay_product_catalog" or not candidates:
                    raise D1PreflightError("FIXTURE_PRODUCT_CANDIDATES_NOT_READABLE")
                if result.get("truncated") or result.get("count") != len(candidates):
                    raise D1PreflightError("FIXTURE_PRODUCT_SEARCH_SCOPE_INCOMPLETE")
                if len(candidates) < 2 or len(candidates) + 1 > limits.tool_calls:
                    raise D1PreflightError("FIXTURE_PRODUCT_CASE_EXCEEDS_READ_BUDGET")
                details = []
                for candidate in candidates:
                    detail = repository.get_product_detail(candidate["product_id"])
                    product = detail.get("product") or {}
                    weight = (product.get("recorded_attributes") or {}).get("weight_g")
                    if (detail.get("availability") != "AVAILABLE" or
                            detail.get("source") != "shoprelay_product_catalog" or
                            isinstance(weight, bool) or not isinstance(weight, (int, float))):
                        raise D1PreflightError("FIXTURE_PRODUCT_DETAIL_OR_WEIGHT_NOT_READABLE")
                    details.append({"product_id": product.get("product_id"),
                        "fixture_provenance": product.get("fixture_provenance"), "weight_g": weight})
                preflight.update({"search_source": result["source"], "candidate_count": len(candidates),
                    "candidate_product_ids": [row["product_id"] for row in candidates],
                    "detail_evidence": details})
            elif profile.preflight_kind == "order":
                if len(plan.get("order_ids", [])) != 1 or len(plan.get("facts", [])) != 1:
                    raise D1PreflightError("FIXTURE_ORDER_PREFLIGHT_SCOPE_INVALID")
                fact = plan["facts"][0]
                if fact.get("kind") != "orders_and_snapshots" or fact.get("owner_id") != plan["owner_id"]:
                    raise D1PreflightError("FIXTURE_ORDER_OWNER_RECEIPT_MISMATCH")
                result = reads["order"].get_order(plan["order_ids"][0], plan["owner_id"])
                expected_fact = json.loads(fact["payload"])
                if (result.get("source") != "business_database" or
                        result.get("order_id") != plan["order_ids"][0] or
                        result.get("fixture_provenance") != fact["source_id"] or
                        result.get("status") != expected_fact.get("status", expected_fact.get("order_status"))):
                    raise D1PreflightError("FIXTURE_ORDER_RECORD_NOT_READABLE_OR_OWNER_MISMATCH")
                preflight.update({"order_id": result["order_id"],
                    "fixture_provenance": result["fixture_provenance"],
                    "stored_status": result.get("status"),
                    "unknown_fields": result.get("unknown_fields", []),
                    "business_source": result["source"]})
            elif profile.preflight_kind == "crossagent_warranty":
                order_facts = [row for row in plan.get("facts", []) if row.get("kind") == "orders_and_snapshots"]
                item_facts = [row for row in plan.get("facts", []) if row.get("kind") == "order_items"]
                policy_facts = [row for row in plan.get("facts", []) if row.get("kind") == "policies"]
                if (case_id != "D1-CROSSAGENT-030" or len(plan.get("order_ids", [])) != 1 or
                        len(order_facts) != 1 or len(item_facts) != 1 or len(policy_facts) != 1 or
                        order_facts[0].get("owner_id") != plan.get("owner_id")):
                    raise D1PreflightError("CROSSAGENT_WARRANTY_PREFLIGHT_SCOPE_INVALID")
                order_id = plan["order_ids"][0]
                order_result = reads["order"].get_order(order_id, plan["owner_id"])
                item_result = reads["order"].get_order_items(order_id, plan["owner_id"])
                expected_order = json.loads(order_facts[0]["payload"])
                expected_item = json.loads(item_facts[0]["payload"])
                expected_product_id = __import__("eval.m6_1.fixture_read_models", fromlist=["product_id"]).product_id(
                    expected_item["product_id"])
                items = item_result.get("items") or []
                if (order_result.get("source") != "business_database" or order_result.get("order_id") != order_id or
                        order_result.get("fixture_provenance") != order_facts[0]["source_id"] or
                        order_result.get("status") != expected_order.get("order_status") or
                        item_result.get("source") != "business_database" or item_result.get("order_id") != order_id or
                        len(items) != 1 or items[0].get("fixture_provenance") != item_facts[0]["source_id"] or
                        items[0].get("product_id") != expected_product_id or
                        items[0].get("quantity") != expected_item.get("quantity")):
                    raise D1PreflightError("CROSSAGENT_ORDER_OWNER_OR_PRODUCT_MAPPING_NOT_READABLE")
                policy_result = reads["policy"].search(plan["message"], mode="BM25",
                    result_ref="crossagent030-preflight-policy")
                policy_fact = json.loads(policy_facts[0]["payload"])
                policy_rows = policy_result.get("evidence") or []
                policy = next((row for row in policy_rows if row.get("chunk_id") == policy_fact.get("evidence_id")), None)
                if (not policy or policy.get("version") != policy_fact.get("version") or
                        policy_result.get("knowledge_version") != plan["knowledge_version"] or
                        policy.get("knowledge_version") != plan["knowledge_version"] or
                        policy.get("effective_from") is None or policy.get("effective_to") is None or
                        policy_result.get("degraded") or policy_result.get("failures")):
                    raise D1PreflightError("CROSSAGENT_WARRANTY_POLICY_EVIDENCE_NOT_READABLE")
                preflight.update({"order_id": order_id, "owner_id": plan["owner_id"],
                    "order_source_ref": order_result["fixture_provenance"],
                    "order_item_source_ref": items[0]["fixture_provenance"],
                    "product_id": items[0]["product_id"], "product_fixture_ref": items[0].get("product_fixture_provenance"),
                    "warranty_policy_ref": policy.get("chunk_id"), "policy_version": policy.get("version"),
                    "policy_effective_from": policy.get("effective_from"),
                    "policy_effective_to": policy.get("effective_to"),
                    "knowledge_version": policy_result["knowledge_version"],
                    "index_fingerprint": policy_result["index_fingerprint"],
                    "evaluation_at": policy_result["evaluation_at"],
                    "eligibility_precondition_missing": expected_order.get("activation_record") is None})
            elif profile.preflight_kind == "none":
                if profile.allowed_tools or plan.get("facts") or plan.get("order_ids"):
                    raise D1PreflightError("NO_TOOL_PROFILE_HAS_BUSINESS_FACT_SCOPE")
                if reads["store"].allowance.calls != 0:
                    raise D1PreflightError("NO_TOOL_PREFLIGHT_MUST_NOT_QUERY_BUSINESS_FACTS")
            else:
                raise D1PreflightError("PERSONAL_DIAGNOSTIC_PREFLIGHT_KIND_UNSUPPORTED")
            preflight["read_calls"] = reads["store"].allowance.calls
        finally:
            self.cleanup(receipt)
            preflight["cleanup_status"] = "RECEIPT_CLEANED_AND_LEASE_RELEASED"
        plan["_personal_admission_evidence"] = preflight
        plan["_personal_admission_binding"] = {"case_id": case_id,
            "authorization_id": authorization["authorization_id"],
            "code_sha256": implementation_digest(self.target.root), "database_identity": self.target.identity,
            "asset_sha256": hashlib.sha256(raw_path.read_bytes()).hexdigest(),
            "authorization_file_sha256": hashlib.sha256(authorization_bytes).hexdigest(),
            "budget_file_sha256": budget_file_sha256, "pricing_sha256": pricing_sha256,
            "check_rules_sha256": check_rules_sha256}
        return plan

    def execute_personal_diagnostic(self, *, bundle, case_id, authorization, limits,
                                    settings, code_revision, budget_path, authorization_path):
        """Consume one file authorization and execute exactly one validated case."""
        from .personal_dev_authorization import (
            claim_personal_diagnostic_execution, close_personal_diagnostic_execution,
        )
        plan = self.validate_personal_diagnostic_admission(bundle=bundle, case_id=case_id,
            authorization=authorization, limits=limits, settings=settings, code_revision=code_revision,
            budget_path=budget_path, authorization_path=authorization_path)
        from .personal_dev_authorization import file_sha256
        binding = plan["_personal_admission_binding"]
        from .personal_dev_authorization import personal_diagnostic_profile
        profile = personal_diagnostic_profile(
            case_id, diagnostic_round_id=authorization.get("diagnostic_round_id"))
        current_rules_hash = (file_sha256(self.target.root / profile.check_rules_path)
            if profile.check_rules_path else None)
        import subprocess
        current_revision = subprocess.check_output(["git", "rev-parse", "HEAD"],
            cwd=self.target.root, text=True).strip()
        if (file_sha256(Path(authorization_path)) != binding["authorization_file_sha256"] or
                file_sha256(Path(budget_path)) != binding["budget_file_sha256"] or
                file_sha256(self.target.root / profile.pricing_path) != binding["pricing_sha256"] or
                current_rules_hash != binding.get("check_rules_sha256") or
                hashlib.sha256((self.target.root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json").read_bytes()).hexdigest() != binding["asset_sha256"] or
                implementation_digest(self.target.root) != binding["code_sha256"] or
                current_revision != code_revision or self.target.identity != binding["database_identity"]):
            raise D1PreflightError("PERSONAL_DIAGNOSTIC_BINDING_CHANGED_AFTER_PREFLIGHT")
        with self.factory() as db:
            self.target.check_session(db)
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            if marker.lease_id is not None or marker.receipt is not None:
                raise D1PreflightError("PERSONAL_DATABASE_LEASE_OR_RECEIPT_STILL_ACTIVE")
        state_path = claim_personal_diagnostic_execution(root=self.target.root,
            authorization_path=authorization_path, authorization=authorization)
        permit = {"authorization_id": authorization["authorization_id"],
            "asset_sha256": authorization["asset_sha256"], "case_ids": authorization["case_ids"],
            "database_identity": authorization["database_identity"],
            "code_sha256": authorization["code_sha256"], "git_commit": authorization["code_revision"],
            "data_version": authorization["data_version"],
            "authorization_type": authorization["authorization_type"]}
        ledger = None
        try:
            ledger = create_budget_ledger(self.target, permit, limits, settings)
            if ledger.snapshot()["events"]:
                raise D1PreflightError("AUTHORIZATION_LEDGER_ALREADY_USED_NO_REPLAY")
            result = self._execute_validated_plan(plan=plan, case_id=case_id, ledger=ledger, settings=settings,
                defer_cleanup=bool(profile.check_rules_path))
            budget_snapshot = ledger.snapshot()
            receipt = result.pop("_diagnostic_receipt", None)
            diagnostic_report_path = None
            diagnostic_report_sha256 = None
            if profile.check_rules_path:
                from .personal_dev_diagnostic_checks import (
                    DiagnosticCheckError, evaluate_diagnostic_record,
                    fail_closed_diagnostic_report, load_locked_rules,
                    validate_diagnostic_check_result,
                )
                rules = None
                rules_sha = None
                rules_error = None
                try:
                    rules, rules_sha = load_locked_rules(self.target.root / profile.check_rules_path, case_id=case_id)
                    if rules_sha != binding.get("check_rules_sha256"):
                        rules_error = "PERSONAL_DIAGNOSTIC_CHECK_RULES_CHANGED_DURING_EXECUTION"
                except Exception as exc:
                    rules_error = f"CHECK_RULES_UNAVAILABLE:{type(exc).__name__}"
                archive_path = self.target.root / result["evidence_path"]
                archived = json.loads(archive_path.read_text(encoding="utf-8"))
                report_dir = self.target.root / (profile.report_dir or
                    "docs/eval/m6.1-d1-diagnostic-closure-r2/reports")
                report_dir.mkdir(parents=True, exist_ok=True)
                report_suffix = profile.report_suffix or "r2"
                preliminary_path = report_dir / f"{case_id}-pre-cleanup-check-{report_suffix}.json"
                try:
                    if rules is None or rules_error:
                        raise DiagnosticCheckError(rules_error or "CHECK_RULES_UNAVAILABLE")
                    preliminary = evaluate_diagnostic_record(case_id=case_id, archived=archived, plan=plan,
                        locked_rules=rules, budget_snapshot=budget_snapshot, cleanup_evidence=None)
                except Exception as exc:
                    preliminary = fail_closed_diagnostic_report(case_id=case_id,
                        reason="CHECKER_EXCEPTION_BEFORE_CLEANUP", error_type=type(exc).__name__)
                preliminary_doc = {"schema_version": "2.0", "phase": "PRE_CLEANUP_DIAGNOSTIC_ONLY",
                    "classification": "PROVISIONAL_DEV_CHECKS", "reference_review_status": "reference_unreviewed",
                    "grading_status": "NOT_GRADED", "case_id": case_id,
                    "diagnostic_round_id": profile.round_id,
                    "rules_sha256": rules_sha,
                    "archive_path": result["evidence_path"],
                    "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                    "checks": preliminary}
                with preliminary_path.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(preliminary_doc, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                cleanup_safety = self.diagnostic_cleanup_safety(receipt, archive_path)
                cleanup_error = None
                cleaned = False
                if cleanup_safety.get("safe_to_cleanup") is True:
                    try:
                        self.cleanup(receipt)
                        cleaned = True
                    except Exception as exc:
                        cleanup_error = f"{type(exc).__name__}: {exc}"
                try:
                    after_cleanup = self.diagnostic_cleanup_evidence(receipt, cleaned=cleaned)
                except Exception as exc:
                    after_cleanup = {"cleanup_status": "UNKNOWN", "lease_released": False,
                        "pending_actions": "UNKNOWN", "residual_case_rows": "UNKNOWN",
                        "lease_id": getattr(receipt, "lease_id", None),
                        "database_identity": self.target.identity,
                        "cleanup_inspection_error": type(exc).__name__}
                if not cleanup_safety.get("safe_to_cleanup"):
                    after_cleanup["cleanup_status"] = "RETAINED_UNSAFE_STATE"
                    after_cleanup["cleanup_reason"] = cleanup_safety.get("reason")
                try:
                    if rules is None or rules_error:
                        raise DiagnosticCheckError(rules_error or "CHECK_RULES_UNAVAILABLE")
                    final_checks = evaluate_diagnostic_record(case_id=case_id, archived=archived, plan=plan,
                        locked_rules=rules, budget_snapshot=budget_snapshot, cleanup_evidence=after_cleanup)
                except Exception as exc:
                    final_checks = fail_closed_diagnostic_report(case_id=case_id,
                        reason="CHECKER_EXCEPTION_AFTER_CLEANUP", error_type=type(exc).__name__)
                if rules_error:
                    final_checks.setdefault("check_items", []).append({"id": "checker_integrity",
                        "status": "UNKNOWN", "reason": rules_error, "blocking": True,
                        "category": "checker_integrity"})
                    final_checks.setdefault("decision", {})["outcome"] = "STOP"
                    final_checks["decision"].setdefault("blocking_items", []).append("checker_integrity")
                    final_checks["decision"].setdefault("reasons", []).append("checker_integrity:UNKNOWN")
                    final_checks["stop_reasons"] = final_checks["decision"]["reasons"]
                if cleanup_error or not cleanup_safety.get("safe_to_cleanup"):
                    final_checks.setdefault("check_items", []).append({"id": "cleanup_safety_gate",
                        "status": "FAIL" if cleanup_error else "UNKNOWN",
                        "reason": cleanup_error or cleanup_safety.get("reason"),
                        "blocking": True, "category": "cleanup_checks"})
                    final_checks.setdefault("decision", {})["outcome"] = "STOP"
                    final_checks["decision"].setdefault("blocking_items", []).append("cleanup_safety_gate")
                    final_checks["decision"].setdefault("reasons", []).append(
                        "cleanup_safety_gate:" + ("FAIL" if cleanup_error else "UNKNOWN"))
                    final_checks["stop_reasons"] = final_checks["decision"]["reasons"]
                if rules is not None:
                    try:
                        validate_diagnostic_check_result(final_checks,
                            decision_policy=rules["decision_policy"])
                    except Exception as exc:
                        final_checks = fail_closed_diagnostic_report(case_id=case_id,
                            reason="CHECK_RESULT_INCOMPLETE_OR_INCONSISTENT",
                            error_type=type(exc).__name__)
                diagnostic_report_path = report_dir / f"{case_id}-diagnostic-check-{report_suffix}.json"
                diagnostic_doc = {"schema_version": "2.0", "phase": "EXECUTION_CLOSEOUT",
                    "classification": "PROVISIONAL_DEV_CHECKS",
                    "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
                    "case_id": case_id, "authorization_id": authorization.get("authorization_id"),
                    "diagnostic_round_id": profile.round_id,
                    "code_sha256": binding["code_sha256"],
                    "database_identity": binding["database_identity"],
                    "asset_sha256": binding["asset_sha256"],
                    "rules_sha256": rules_sha,
                    "archive_path": result["evidence_path"],
                    "archive_sha256": hashlib.sha256(archive_path.read_bytes()).hexdigest(),
                    "budget_snapshot": budget_snapshot,
                    "pre_cleanup_report": preliminary_path.relative_to(self.target.root).as_posix(),
                    "cleanup_safety": cleanup_safety, "cleanup_evidence": after_cleanup,
                    "cleanup_error": cleanup_error, "checks": final_checks}
                with diagnostic_report_path.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(diagnostic_doc, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                diagnostic_report_sha256 = hashlib.sha256(diagnostic_report_path.read_bytes()).hexdigest()
                result["diagnostic_check_report"] = diagnostic_report_path.relative_to(self.target.root).as_posix()
                result["diagnostic_check_report_sha256"] = diagnostic_report_sha256
                result["diagnostic_checks"] = final_checks
                result["cleanup_status"] = after_cleanup["cleanup_status"]
            events = (result.get("observations") or {}).get("run_events") or []
            agent_started = any(row.get("event_type") == "agent_started" for row in events)
            run_status = result.get("status", "UNKNOWN")
            run_terminal = run_status in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}
            with self.factory() as db:
                self.target.check_session(db)
                marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            cleanup_status = result.get("cleanup_status") or ("CLEANED_AND_RELEASED" if marker.lease_id is None and marker.receipt is None
                else "NOT_CLEANED_OR_LEASE_RETAINED")
            from .personal_dev_diagnostic_checks import diagnostic_cli_projection
            diagnostic_projection = diagnostic_cli_projection(self.target.root,
                result.get("diagnostic_check_report"), result.get("diagnostic_check_report_sha256"))
            if diagnostic_projection.get("diagnostic_report_integrity") != "VERIFIED":
                diagnostic_projection["cleanup_status"] = cleanup_status
            checker_decision = diagnostic_projection.get("diagnostic_decision", "STOP")
            final_status = ("CLOSED_COMPLETED" if run_terminal and cleanup_status == "CLEANED_AND_RELEASED" and checker_decision == "CONTINUE"
                else "CLOSED_STOPPED")
            outcome = {"case_id": case_id, "run_id": result.get("run_id"), "run_status": run_status,
                "agent_started": agent_started, "provider_requests": budget_snapshot.get("provider_requests"),
                "budget_ledger": budget_snapshot, "preflight_evidence": plan.get("_personal_admission_evidence"),
                "cleanup_status": cleanup_status, "result_archive": result.get("evidence_path"),
                "result_archive_sha256": result.get("evidence_sha256"), "diagnostic_check_report": result.get("diagnostic_check_report"),
                "diagnostic_check_report_sha256": result.get("diagnostic_check_report_sha256"),
                "diagnostic_decision": checker_decision,
                "blocking_item_count": diagnostic_projection.get("blocking_item_count", 1),
                "review_item_count": diagnostic_projection.get("review_item_count", 0),
                "diagnostic_report_integrity": diagnostic_projection.get("diagnostic_report_integrity", "FAIL"),
                "diagnostic_report_error": diagnostic_projection.get("diagnostic_report_error"),
                "diagnostic_checks": diagnostic_projection,
                "grading_status": "NOT_GRADED"}
            close_personal_diagnostic_execution(state_path=state_path, authorization_path=authorization_path,
                authorization=authorization, status=final_status, outcome=outcome)
            return {"status": run_status, "authorization_status": final_status, **outcome,
                "result": result.get("result")}
        except Exception as exc:
            budget_snapshot = ledger.snapshot() if ledger is not None else None
            active = self.inspect_active_run_evidence()
            outcome = {"case_id": case_id, "run_status": active.get("run_status", "UNKNOWN"),
                "agent_started": active.get("agent_started", False), "run_created": active.get("run_created", False),
                "provider_requests": (budget_snapshot or {}).get("provider_requests", "UNKNOWN"),
                "budget_ledger": budget_snapshot, "preflight_evidence": plan.get("_personal_admission_evidence"),
                "cleanup_status": "RETAINED_FOR_INVESTIGATION" if active.get("lease_active") else "NO_RUN_OR_LEASE_CONFIRMED",
                "failure_type": type(exc).__name__, "grading_status": "NOT_GRADED"}
            try:
                close_personal_diagnostic_execution(state_path=state_path, authorization_path=authorization_path,
                    authorization=authorization, status="CLOSED_STOPPED", outcome=outcome)
                authorization_status = "CLOSED_STOPPED"
            except Exception:
                authorization_status = "IN_USE_CLOSE_WRITE_FAILED"
            return {"status": "EXECUTION_STOPPED", "authorization_status": authorization_status, **outcome}

    def validate_personal_batch_admission(self, *, bundle, authorization, budget_config,
                                          budget_file, settings, code_revision):
        """Validate the exact r3.2.4 40-case personal batch before any fixture or Run."""
        from .personal_dev_authorization import (load_price_evidence,
            validate_personal_batch_authorization, validate_personal_budget)
        from .budget import BudgetLimits
        asset_path = self.target.root / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
        expected_sha = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
        if (hashlib.sha256(asset_path.read_bytes()).hexdigest() != expected_sha or
                json.loads(asset_path.read_text(encoding="utf-8")) != bundle or
                bundle.get("version") != "m6.1-d1-dev-draft-r3.2.4" or bundle.get("split") != "dev"):
            raise D1PreflightError("EXACT_R324_DEV_ASSET_REQUIRED")
        case_ids = [case["case_id"] for case in bundle.get("cases", [])]
        if len(case_ids) != 40 or len(set(case_ids)) != 40:
            raise D1PreflightError("EXACT_40_UNIQUE_CASES_REQUIRED")
        config = budget_config
        per_case = config.get("per_case", {})
        batch = config.get("batch", {})
        expected_per_case = {"provider_calls": 4, "tool_calls": 4, "total_tokens": 120000,
            "estimated_usd": 0.18, "max_duration_seconds": 180, "max_concurrent_trials": 1,
            "max_retries": 0, "embedding_calls": 0, "judge_calls": 0}
        expected_batch = {"provider_calls": 160, "tool_calls": 160, "total_tokens": 4800000,
            "estimated_usd": 7.20, "max_duration_seconds": 180, "max_concurrent_trials": 1,
            "max_retries": 0, "embedding_calls": 0, "judge_calls": 0}
        batch_root = (self.target.root / "docs/eval/personal-dev-batches").resolve()
        budget_path = budget_file.resolve()
        if (budget_path.name != "budget.json" or budget_path.parent.parent != batch_root or
                budget_path.parent.name != authorization.get("authorization_id")):
            raise D1PreflightError("PERSONAL_BATCH_BUDGET_PATH_MISMATCH")
        if (config.get("case_count") != 40 or config.get("provider_hard_spend_cap") is not False or
                per_case != expected_per_case or batch != expected_batch):
            raise D1PreflightError("PERSONAL_BATCH_BUDGET_SCOPE_MISMATCH")
        from .personal_dev_budget_carryforward import (ReadOnlyBudgetStore,
            inspect_closed_batch_carryforward, SOURCE_EXPERIMENT_ID)
        closed_source_auth = self.target.root / "docs/eval/personal-dev-batches" / SOURCE_EXPERIMENT_ID / "authorization.json"
        prior_auth_path = self.target.root / "docs/eval/m6.1-d1-personal-dev-preparation-r2/22_personal-dev-batch-authorization-r1.json"
        try:
            source_auth = json.loads(closed_source_auth.read_text(encoding="utf-8"))
            prior_auth = json.loads(prior_auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise D1PreflightError("CLOSED_BATCH_AUTHORIZATION_EVIDENCE_UNAVAILABLE") from exc
        if (source_auth.get("authorization_id") != SOURCE_EXPERIMENT_ID or
                source_auth.get("status") != "CLOSED_COMPLETED" or
                prior_auth.get("authorization_id") != "M61D1-BATCH-R324-d60e1f72d7dc468d886b37aa72c84ac5" or
                prior_auth.get("status") != "CLOSED_STOPPED"):
            raise D1PreflightError("CLOSED_BATCH_AUTHORIZATION_REPLAY_FORBIDDEN")
        carryforward = inspect_closed_batch_carryforward(ReadOnlyBudgetStore(
            self.target.root / "data/eval/d1-personal-budget.sqlite3"), case_ids)
        expected_carry = {"source_experiment_id": SOURCE_EXPERIMENT_ID,
            "provider_calls": carryforward["provider_calls"],
            "conservative_tokens": carryforward["conservative_tokens"],
            "conservative_usd": carryforward["conservative_usd"],
            "actual_tokens": "UNKNOWN" if carryforward["unknown_actual_tokens"] else "RECORDED",
            "actual_billed_usd": "UNKNOWN", "included_once": True}
        if config.get("carry_forward") != expected_carry:
            raise D1PreflightError("PERSONAL_BATCH_CARRY_FORWARD_SCOPE_MISMATCH")
        case_limits = BudgetLimits(**{**per_case, "max_calls_per_trial": 4})
        global_limits = BudgetLimits(**{**batch, "max_calls_per_trial": 4})
        price_path = self.target.root / "docs/eval/m6.1-d1-personal-dev-preparation-r2/11_official-pricing-evidence-2026-10-26.json"
        _, price_sha = load_price_evidence(price_path, model=settings.model, endpoint=settings.base_url)
        validate_personal_batch_authorization(authorization, case_ids=case_ids,
            asset_sha256=expected_sha, code_sha256=implementation_digest(self.target.root),
            code_revision=code_revision, database_identity=self.target.identity,
            model=settings.model, endpoint=settings.base_url, budget=config,
            budget_file_sha256_value=hashlib.sha256(budget_file.read_bytes()).hexdigest(),
            pricing_sha256_value=price_sha,
            expected_budget_ref=budget_file.resolve().relative_to(self.target.root).as_posix())
        if not settings.api_key.strip():
            raise D1PreflightError("PROVIDER_CREDENTIAL_CONFIGURATION_REQUIRED")
        plans = [plan_case(bundle, case_id) for case_id in case_ids]
        for plan in plans:
            validate_personal_budget(case_limits, plan["message"], budget_config=case_limits.to_dict())
            declared = plan["case"]["input"].get("expected_tool_calls", [])
            if any(row.get("mode") != "read_only" or row.get("tool_id") not in SUPPORTED_TOOLS for row in declared):
                raise D1PreflightError(f"NON_READ_ONLY_CAPABILITY_FORBIDDEN:{plan['case_id']}")
        with self.factory() as db:
            self.target.check_session(db)
            target = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            if target.lease_id is not None or target.receipt is not None:
                raise D1PreflightError("PERSONAL_DATABASE_HAS_ACTIVE_LEASE_OR_RECEIPT")
            for table in (User, Order, Shipment, Run, PendingAction):
                if db.execute(select(table).limit(1)).first():
                    raise D1PreflightError(f"PERSONAL_DATABASE_NOT_EMPTY:{table.__tablename__}")
            if db.execute(text("SELECT 1 FROM d1_fixture_facts LIMIT 1")).first():
                raise D1PreflightError("PERSONAL_DATABASE_NOT_EMPTY:d1_fixture_facts")
        return {"case_ids": case_ids, "plans": plans, "case_limits": case_limits,
                "global_limits": global_limits, "carryforward": carryforward}

    def execute_personal_batch(self, *, bundle, authorization, budget_config, budget_file,
                               settings, code_revision):
        """Execute the user-confirmed batch serially, with one durable global ledger."""
        import time
        from datetime import datetime, timezone
        from .budget import BudgetStop
        from .personal_dev_runtime import PerCaseBudgetLedger
        admission = self.validate_personal_batch_admission(bundle=bundle,
            authorization=authorization, budget_config=budget_config, budget_file=budget_file,
            settings=settings, code_revision=code_revision)
        permit = {"authorization_id": authorization["authorization_id"],
            "asset_sha256": authorization["asset_sha256"], "case_ids": admission["case_ids"],
            "database_identity": authorization["database_identity"], "code_sha256": authorization["code_sha256"],
            "git_commit": authorization["code_revision"], "data_version": authorization["data_version"],
            "authorization_type": authorization["authorization_type"]}
        ledger = create_budget_ledger(self.target, permit, admission["global_limits"], settings)
        if ledger.snapshot()["events"]:
            raise D1PreflightError("PERSONAL_BATCH_LEDGER_ALREADY_USED_NO_REPLAY")
        batch_dir = budget_file.parent
        if not (batch_dir / "authorization.json").is_file():
            raise D1PreflightError("PERSONAL_BATCH_AUTHORIZATION_FILE_MISSING")
        from .personal_dev_budget_carryforward import apply_carryforward
        apply_carryforward(ledger, admission["carryforward"])
        journal = batch_dir / "batch.jsonl"
        started = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        records = []
        stopped = False
        consecutive_provider_errors = 0
        with journal.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps({"event": "BATCH_STARTED", "started_at_utc": started,
                "authorization_id": authorization["authorization_id"], "case_ids": admission["case_ids"],
                "asset_sha256": authorization["asset_sha256"], "code_sha256": authorization["code_sha256"],
                "database_identity": authorization["database_identity"], "budget": budget_config,
                "grading_status": "PERSONAL_DEV_ONLY"}, ensure_ascii=False) + "\n")
            stream.flush()
            for index, plan in enumerate(admission["plans"]):
                case_id = plan["case_id"]
                t0 = time.monotonic()
                try:
                    per_case_ledger = PerCaseBudgetLedger(ledger, case_id, admission["case_limits"])
                    result = self._execute_validated_plan(plan=plan, case_id=case_id,
                        ledger=per_case_ledger, settings=settings)
                    trial_budget = ledger.trial_snapshot(case_id)
                    from .personal_dev_scoring import diagnose_personal_d1_run
                    diagnostics = diagnose_personal_d1_run(case_id=case_id,
                        expected_tool_calls=plan["case"]["input"].get("expected_tool_calls", []),
                        result=result, trial_budget=trial_budget)
                    from .personal_dev_scoring import classify_personal_d1_execution
                    classification = classify_personal_d1_execution(diagnostics, result)
                    row = {"case_id": case_id, "status": classification["execution_status"],
                        "execution_status": classification["execution_status"],
                        "run_status": diagnostics.get("run_status", "UNKNOWN"),
                        "agent_started": diagnostics["agent_started"],
                        "run_created": bool(result.get("run_id")), "result": result,
                        "classification": classification,
                        "input": plan["case"]["input"],
                        "wall_duration_seconds": round(time.monotonic() - t0, 3),
                        "trial_budget": trial_budget, "diagnostics": diagnostics,
                        "scoring": {"task_outcome": "UNKNOWN", "deterministic_score": "UNKNOWN",
                            "semantic_score": "UNKNOWN", "reason": "Evidence-only real D1 diagnostics are not a task-quality scoring rubric; no synthetic harness grader applied."}}
                    records.append(row)
                    if result["status"] not in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}:
                        stopped = True
                        row["safety_stop_reason"] = "Run did not reach an approved safe terminal state."
                    provider_error = diagnostics.get("provider_error") or ""
                    consecutive_provider_errors = consecutive_provider_errors + 1 if "APIConnectionError" in provider_error else 0
                    if index == 0 and not diagnostics["agent_started"]:
                        stopped = True
                        row["safety_stop_reason"] = "FIRST_CASE_DID_NOT_START_AGENT; pilot gate not satisfied."
                    elif consecutive_provider_errors >= 2:
                        stopped = True
                        row["safety_stop_reason"] = "CONSECUTIVE_PROVIDER_CONNECTION_ERRORS; stopping remaining cases."
                except BudgetStop as exc:
                    row = {"case_id": case_id, "status": "BUDGET_STOP", "agent_started": False,
                        "run_created": False, "reason": str(exc), "wall_duration_seconds": round(time.monotonic()-t0, 3)}
                    records.append(row); stopped = True
                except Exception as exc:
                    persisted = self.inspect_active_run_evidence()
                    row = {"case_id": case_id, "status": "EXECUTION_ERROR", "execution_status": "EXECUTION_ERROR",
                        "agent_started": persisted["agent_started"],
                        "run_created": persisted["run_created"], "persisted_run_status": persisted["run_status"],
                        "reason": f"{type(exc).__name__}: {exc}",
                        "wall_duration_seconds": round(time.monotonic()-t0, 3)}
                    records.append(row); stopped = True
                stream.write(json.dumps({"event": "CASE_RESULT", **row}, ensure_ascii=False, default=str) + "\n")
                stream.flush()
                if stopped:
                    for remaining in admission["case_ids"][index+1:]:
                        pending = {"event": "CASE_RESULT", "case_id": remaining,
                            "status": "BUDGET_STOP" if row["status"] == "BUDGET_STOP" else "NOT_RUN",
                            "agent_started": False, "run_created": False,
                            "reason": "Batch stopped after budget or unresolved execution state."}
                        records.append(pending); stream.write(json.dumps(pending) + "\n")
                    break
            ended = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            from collections import Counter
            from .aggregate import percentile
            status_counts = dict(sorted(Counter(row["status"] for row in records).items()))
            executed = [row for row in records if row.get("agent_started") is True]
            latencies = [row["wall_duration_seconds"] * 1000 for row in records
                if isinstance(row.get("wall_duration_seconds"), (int, float))]
            budget_snapshot = ledger.snapshot()
            batch_status = "BATCH_STOPPED" if stopped else "BATCH_COMPLETED"
            terminal_rows = [row for row in records if row.get("run_status") in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}]
            failed_count = sum(row.get("run_status") in {"FAILED", "MODEL_UNAVAILABLE"} or
                row.get("status") == "EXECUTION_ERROR"
                for row in records)
            summary = {"event": batch_status, "batch_status": batch_status, "started_at_utc": started,
                "ended_at_utc": ended, "records": records, "ledger": budget_snapshot,
                "counts": {"requested": 40, "recorded": len(records), "agent_started": len(executed),
                    "runs_created": sum(bool(row.get("run_created")) for row in records),
                    "terminal_runs": len(terminal_rows),
                    "completed": sum(row.get("agent_started") is True and row.get("run_status") == "SUCCEEDED" for row in records),
                    "succeeded": sum(row.get("agent_started") is True and row.get("run_status") == "SUCCEEDED" for row in records),
                    "failed": failed_count,
                    "non_agent_route_terminal": sum(row.get("status") == "NON_AGENT_ROUTE_TERMINAL_UNSCORED"
                        for row in records),
                    "unscorable": sum(str(row.get("status", "")).startswith("UNSCORABLE") for row in records), "not_run": sum(
                        row["status"] in {"NOT_RUN", "BUDGET_STOP"} for row in records),
                    "status_counts": status_counts},
                "task_success_rate": {"numerator": 0, "denominator": 0, "rate": None,
                    "unknown_count": len(executed), "status": "UNKNOWN_NO_REAL_D1_GRADER"},
                "fact_correctness": "UNKNOWN", "evidence_support": "UNKNOWN",
                "tool_execution_correctness": "UNKNOWN",
                "latency_ms": {"sample_count": len(latencies), "p50": percentile(latencies, .5),
                    "p95": percentile(latencies, .95), "method": "linear interpolation of wall-clock per-case duration"},
                "all_cases_recorded": len(records) == 40, "grading_status": "PERSONAL_DEV_ONLY",
                "formal_gold_or_frozen_claim": False,
                "limitations": ["The personal D1 adapter reports execution evidence only; unsupported quality dimensions stay UNKNOWN.",
                    "Local estimated cost is not provider billing; billed amount remains UNKNOWN unless reported by provider."]}
            stream.write(json.dumps(summary, ensure_ascii=False, default=str) + "\n")
            stream.flush()
        return {"batch_dir": str(batch_dir), "summary": summary}

    def execute_authorized(self, *, bundle, case_id, asset, grant, permit, ledger, settings):
        """Real application entry; only after full admission. Never scores Gold."""
        plan = self.validate_admission(bundle=bundle, case_id=case_id, asset=asset, grant=grant,
            permit=permit, ledger=ledger, settings=settings)
        return self._execute_validated_plan(plan=plan, case_id=case_id, ledger=ledger, settings=settings)

    def inspect_active_run_evidence(self) -> dict:
        """Read the exact personal target receipt after an exception; never replay or clean it."""
        with self.factory() as db:
            self.target.check_session(db)
            row = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            if not row.lease_id:
                return {"agent_started": False, "run_created": False, "run_status": "UNKNOWN",
                    "lease_active": False}
            if not row.receipt:
                return {"agent_started": False, "run_created": False, "run_status": "UNKNOWN",
                    "lease_active": True}
            try:
                receipt = json.loads(row.receipt)
                run_id = receipt.get("run_id")
                if not run_id:
                    return {"agent_started": False, "run_created": False, "run_status": "UNKNOWN",
                        "lease_active": True}
                run = db.get(Run, run_id)
                session_runs = db.execute(select(Run).where(
                    Run.session_id == receipt.get("session_id"))).scalars().all()
                events = db.execute(select(RunEvent).where(RunEvent.run_id == run_id)).scalars().all()
                return {"agent_started": any(event.event_type == "agent_started" for event in events),
                    "run_created": run is not None, "run_status": run.status if run else "UNKNOWN",
                    "lease_active": True, "run_id": run_id, "lease_id": receipt.get("lease_id"),
                    "database_identity": receipt.get("database_identity"),
                    "event_count": len(events), "run_count": len(session_runs)}
            except (TypeError, ValueError, KeyError):
                return {"agent_started": False, "run_created": False, "run_status": "UNKNOWN",
                    "lease_active": True}

    def capture_active_run_after_exception(self, *, case_id: str, user_query: str,
            error_type: str) -> dict:
        """Persist an exact DB snapshot after an exception; never replay or infer missing results."""
        with self.factory() as db:
            self.target.check_session(db)
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
            if not marker.lease_id or not marker.receipt:
                return {"captured": False, "reason": "ACTIVE_RECEIPT_MISSING"}
            try:
                raw_receipt = json.loads(marker.receipt)
                if (raw_receipt.get("lease_id") != marker.lease_id or
                        raw_receipt.get("database_identity") != self.target.identity):
                    return {"captured": False, "reason": "ACTIVE_RECEIPT_IDENTITY_MISMATCH"}
                receipt = FixtureReceipt(lease_id=raw_receipt["lease_id"],
                    database_identity=raw_receipt["database_identity"],
                    user_ids=tuple(raw_receipt.get("user_ids", [])),
                    order_ids=tuple(raw_receipt.get("order_ids", [])),
                    shipment_receipt=raw_receipt.get("shipment_receipt"),
                    session_id=raw_receipt.get("session_id"), run_id=raw_receipt.get("run_id"),
                    core_order_ids=tuple(raw_receipt.get("core_order_ids", [])),
                    fact_receipts=tuple(raw_receipt.get("fact_receipts", [])))
                self._check_receipt(db, receipt)
            except Exception as exc:
                return {"captured": False, "reason": "ACTIVE_RECEIPT_INVALID",
                    "error_type": type(exc).__name__}

            base = self.target.root / "docs/eval/personal-dev-runs" / f"{receipt.lease_id}.json"
            recovery = base.with_name(base.stem + ".recovery.json")
            selected = base if base.is_file() else recovery if recovery.is_file() else None
            run = db.get(Run, receipt.run_id) if receipt.run_id else None
            session_runs = (db.execute(select(Run).where(Run.session_id == receipt.session_id)
                .order_by(Run.created_at)).scalars().all() if receipt.session_id else [])
            run_rows = [{column.name: getattr(row, column.name) for column in Run.__table__.columns}
                for row in session_runs]
            events = (db.execute(select(RunEvent).where(RunEvent.run_id == receipt.run_id)
                .order_by(RunEvent.seq)).scalars().all() if receipt.run_id else [])
            traces = (db.execute(select(Trace).where(Trace.run_id == receipt.run_id)
                .order_by(Trace.created_at)).scalars().all() if receipt.run_id else [])
            messages = (db.execute(select(Message).where(Message.session_id == receipt.session_id)
                .order_by(Message.sequence)).scalars().all() if receipt.session_id else [])
            graph_rows = (db.execute(select(GraphExecutionRecord).where(
                GraphExecutionRecord.run_id == receipt.run_id)).scalars().all() if receipt.run_id else [])
            observability = (db.execute(select(ObservabilityRecord).where(
                ObservabilityRecord.run_id == receipt.run_id)).scalars().all() if receipt.run_id else [])
            if selected is None:
                persisted = None
                if run is not None and run.result_json:
                    try:
                        persisted = json.loads(run.result_json)
                    except (TypeError, json.JSONDecodeError):
                        persisted = None
                tables = {"runs": run_rows,
                    "run_events": [{column.name: getattr(row, column.name) for column in RunEvent.__table__.columns}
                        for row in events],
                    "traces": [{column.name: getattr(row, column.name) for column in Trace.__table__.columns}
                        for row in traces],
                    "messages": [{column.name: getattr(row, column.name) for column in Message.__table__.columns}
                        for row in messages],
                    "graph_executions": [{column.name: getattr(row, column.name) for column in GraphExecutionRecord.__table__.columns}
                        for row in graph_rows],
                    "observability_records": [{column.name: getattr(row, column.name) for column in ObservabilityRecord.__table__.columns}
                        for row in observability]}
                archived = {"case_id": case_id, "user_query": user_query,
                    "receipt": asdict(receipt), "result": {"status": run.status if run else "UNKNOWN",
                        "run_id": receipt.run_id, "result": persisted,
                        "failure_code": run.error_code if run else None, "observations": tables,
                        "checkpoint_mode": "CAPTURED_FROM_DATABASE_AFTER_EXCEPTION",
                        "agent_quality_verdict": "NOT_GRADED"},
                    "recovery": {"classification": "EXECUTION_EXCEPTION", "error_type": error_type,
                        "snapshot_source": "EXACT_PERSONAL_DATABASE_RECEIPT_AND_RUN"},
                    "grading_status": "NOT_GRADED", "os_isolation": OS_ISOLATION_STATUS}
                selected = recovery
                selected.parent.mkdir(parents=True, exist_ok=True)
                with selected.open("x", encoding="utf-8", newline="\n") as stream:
                    json.dump(redact(archived), stream, ensure_ascii=False, indent=2, default=str)
                    stream.write("\n")
            return {"captured": True, "receipt": receipt, "evidence_path": selected.relative_to(
                self.target.root).as_posix(), "run_id": receipt.run_id,
                "run_status": run.status if run else "UNKNOWN",
                "agent_started": any(event.event_type == "agent_started" for event in events),
                "event_count": len(events), "trace_count": len(traces), "run_count": len(run_rows),
                "archive_sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
                "database_identity": self.target.identity}

    def _execute_validated_plan(self, *, plan, case_id, ledger, settings, defer_cleanup=False):
        limits = ledger.limits
        # Reserve before preload; a failed preflight cannot trigger external calls.
        reservation = ledger.reserve(trial_id=case_id, category="tool", calls=limits.tool_calls) if limits.tool_calls else None
        settled = False
        try:
            receipt = self.prepare(plan)
        except Exception:
            if reservation:
                ledger.settle(reservation, status="RELEASE", actual_calls=0, actual_tokens=0, estimated_usd=0.0)
            raise
        result = None
        output = None
        try:
            from .personal_dev_runtime import execute_application_case
            result = execute_application_case(self, plan, receipt, ledger, settings)
            output = self.target.root / "docs/eval/personal-dev-runs" / f"{receipt.lease_id}.json"
            output.parent.mkdir(parents=True, exist_ok=True)
            result["evidence_path"] = output.relative_to(self.target.root).as_posix()
            with output.open("x", encoding="utf-8") as stream:
                json.dump(redact({"case_id": case_id, "user_query": plan["message"],
                    "receipt": asdict(receipt), "result": result,
                    "grading_status": "NOT_GRADED", "os_isolation": OS_ISOLATION_STATUS}), stream, ensure_ascii=False, indent=2, default=str)
            result["evidence_sha256"] = hashlib.sha256(output.read_bytes()).hexdigest()
            if reservation:
                ledger.settle(reservation, status="COMMIT", actual_calls=result.get("read_calls", limits.tool_calls), actual_tokens=0, estimated_usd=0.0,
                    call_count_status="RECORDED")
            settled = True
            if result["status"] in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"} and not defer_cleanup:
                self.cleanup(receipt)
            if defer_cleanup:
                result["_diagnostic_receipt"] = receipt
            return result
        except Exception as exc:
            # If a terminal Run was persisted but normal serialization failed,
            # attempt one evidence-only recovery archive. Never re-call the app.
            if receipt is not None and result is not None and output is not None:
                try:
                    recovery = output.with_name(output.stem + ".recovery.json")
                    payload = {"case_id": case_id, "user_query": plan["message"],
                        "receipt": asdict(receipt), "result": result,
                        "archive_recovery_error": f"{type(exc).__name__}: {exc}",
                        "grading_status": "NOT_GRADED", "os_isolation": OS_ISOLATION_STATUS}
                    safe_payload = redact(json.loads(json.dumps(payload, ensure_ascii=False, default=str)))
                    with recovery.open("x", encoding="utf-8") as stream:
                        json.dump(safe_payload, stream, ensure_ascii=False, indent=2, default=str)
                except Exception:
                    # The receipt/lease remains durable; no cleanup or replay.
                    pass
            # May follow a provider call or committed Run. Keep durable receipt
            # and exclusive lease for investigation; never reset or replay.
            if not settled and reservation:
                ledger.settle(reservation, status="COMMIT", actual_calls=limits.tool_calls, actual_tokens=None, estimated_usd=None,
                    call_count_status="CONSERVATIVE_UPPER_BOUND")
            raise
