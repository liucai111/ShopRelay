"""Actual LangGraph/SQLAlchemy target factory for the existing formal CLI.

It uses the same guarded readonly adapters and exact fixture receipts as Dev,
with a separate marked SQLite target. No expected labels enter routing/model
input or choose the retrieval corpus. There is no test bypass in configuration.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
from pathlib import Path
import time
import uuid

from sqlalchemy import text

from .budget import BudgetLimits
from .contracts import EvalTrial
from .formal_assets import authenticated_subject, confined, raw_hash, write_new
from .grants import formal_case_digest, formal_case_payload, formal_runtime_digest
from .personal_dev import D1Coordinator, FixtureReceipt, plan_readonly_case
from .personal_dev_database import PersonalDevDatabase, FormalPostgresDatabase
from .personal_dev_runtime import PerCaseBudgetLedger, execute_application_case
from .runner import FormalTrialResult

PROFILE = "M6.2-FORMAL-ISOLATED"


def dependency_versions():
    from importlib.metadata import version
    return {name: version(name) for name in ("fastapi", "pydantic", "SQLAlchemy", "langgraph", "langgraph-checkpoint", "openai", "httpx", "cryptography", "numpy", "jieba")}


def application_plan(case):
    value = formal_case_payload(case)
    inp = value["input"]
    # Expected labels never determine runtime scope, IDs, or corpus. All policy
    # clauses in this assigned corpus are loaded, not just relevant/Gold IDs.
    runtime_input = {"user_query": inp["user_query"], "user_id": inp["user_id"],
        "expected_tool_calls": [{"tool_id": t, "mode": "read_only"} for t in inp["allowed_capabilities"]],
        "fixture_refs": list(inp["fixture_refs"])}
    if "owner_interaction" in inp or {"search_knowledge", "get_warranty"} & set(inp["allowed_capabilities"]):
        runtime_input["fixture_refs"] += [p["evidence_id"] for p in inp["fixture"]["policies"]]
    value["input"] = runtime_input
    bundle = {"version": case.data_version, "fixtures": inp["fixture"], "scenario_clock_utc": inp["evaluation_at"]}
    return plan_readonly_case(bundle, value, inp["owner_map"])


def restore_receipt(value):
    from .shipment_fixture_reset import ShipmentFixtureResetReceipt
    from .shipment_fixture_loader import ShipmentLoadReceipt
    value = dict(value)
    ship = value["shipment_receipt"]
    value["shipment_receipt"] = ShipmentFixtureResetReceipt(ship["probe_id"], tuple(ShipmentLoadReceipt(**r) for r in ship["shipment_receipts"]))
    for key in ("user_ids", "order_ids", "core_order_ids", "fact_receipts"):
        value[key] = tuple(value[key])
    return FixtureReceipt(**value)


class MeteredReads:
    """Budget at actual readonly invocation boundaries, not after batch execution."""
    def __init__(self, target, ledger, trial_id, records, *, retrieval=False):
        self.target, self.ledger, self.trial_id, self.records, self.retrieval = target, ledger, trial_id, records, retrieval

    def __getattr__(self, name):
        value = getattr(self.target, name)
        from .fixture_read_models import READ_TOOLS
        if name not in READ_TOOLS and not (self.retrieval and name == "retrieve"):
            return value
        def call(*args, **kwargs):
            held = self.ledger.reserve(trial_id=self.trial_id, category="tool")
            record = {"invocation_id": uuid.uuid4().hex, "category": "policy_retrieval" if self.retrieval else "business_read", "operation": name, "status": "STARTED"}
            from app.m3.order_tools import active_read_audit
            current_audit = active_read_audit()
            if current_audit is not None:
                record["tool_audit"] = current_audit
            self.records.append(record)
            try:
                # Record actual adapter arguments at their responsible boundary.
                # Tool names alone cannot prove two distinct resources were read.
                import inspect
                bound = inspect.signature(value).bind(*args, **kwargs)
                record["arguments"] = json.loads(json.dumps(bound.arguments, default=str))
                result = value(*args, **kwargs)
                record["status"] = "RETURNED"
                if self.retrieval:
                    record["query"] = args[0] if args else kwargs["query"]
                    record["retrieval_result"] = result
                else:
                    record["read_result"] = result
                return result
            except Exception as exc:
                record.update(status="FAILED", error_type=type(exc).__name__)
                raise
            finally:
                self.ledger.settle(held, status="COMMIT", actual_calls=1, actual_tokens=0, estimated_usd=0)
        return call


class FormalApplication:
    def __init__(self, *, config, store, ledger, mode_gate, grant):
        self.config, self.store, self.ledger, self.gate, self.grant = config, store, ledger, mode_gate, grant
        self.root = mode_gate.formal_authority.root
        env = config["experiment_config"]["environment"]
        self.env = env
        self.target = (FormalPostgresDatabase(self.root, env["application_postgres"]) if "application_postgres" in env else
            PersonalDevDatabase(self.root, profile=PROFILE, relative_database=Path(env["application_database"])))
        self.engine, self.session_factory = self.target.open()
        self.coordinator = D1Coordinator(self.target, self.session_factory)
        self.artifacts = confined(self.root, env["application_artifacts"])
        if self.artifacts.resolve() != self.store.path.parent.resolve() / "application":
            raise ValueError("application artifacts must share the approved ledger directory")
        self.artifacts.mkdir(parents=True, exist_ok=True)

    def verify_signing_subject(self):
        return authenticated_subject() == self.env["issuer_subject"]

    def verify_execution_subject(self, expected_subject):
        return authenticated_subject() == expected_subject == self.grant.plan.subject

    def verify_execution_target(self, target):
        with self.session_factory() as db:
            self.target.check_session(db)
            pending = db.execute(text("SELECT count(*) FROM controlled_pending_actions")).scalar_one()
            active = db.execute(text("SELECT count(*) FROM runs WHERE status NOT IN ('SUCCEEDED','FAILED','MODEL_UNAVAILABLE','CANCELLED')")).scalar_one()
            marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
        lease_ok = marker.lease_id is None
        if not lease_ok:
            for trial in self.store.trials(self.grant.plan.experiment_id):
                raw = self.store.raw_result(trial["trial_id"])
                if raw["receipt"]["fixture"] == json.loads(marker.receipt):
                    lease_ok = True
        if self.env.get("simulated_action_tools") and self.target.profile != FormalPostgresDatabase.profile:
            return False
        return target == (self.grant.plan.target_id, self.target.profile, self.target.identity) and not pending and not active and lease_ok

    def admit(self, cases):
        """Receipt-scoped Repository readability, separate from actual Run calls."""
        from .formal_assets import validate_asset
        validate_asset({"schema_version": "formal-owner-action-assets-v2" if any("owner_interaction" in c.input for c in cases) else "formal-readonly-assets-v1", "version": self.grant.plan.asset_version,
            "split": cases[0].split, "cases": [formal_case_payload(c) for c in cases]})
        actions={c.input["owner_interaction"]["tool_id"] for c in cases if "owner_interaction" in c.input}
        if actions:
            from app.m4.actions import server_permission_granted
            if self.target.profile!=FormalPostgresDatabase.profile or actions!=set(self.env["simulated_action_tools"]):
                raise ValueError("exact simulated action scope/target mismatch")
            for c in cases:
                if "owner_interaction" in c.input and not server_permission_granted(c.input["owner_map"][c.input["user_id"]],c.input["owner_interaction"]["tool_id"],"shoprelay_m11_test"):
                    raise PermissionError("fixture owner lacks the existing server-side action grant")
        if {t for case in cases for t in case.input["allowed_capabilities"]} != set(self.config["experiment_config"]["tool_permissions"]):
            raise ValueError("aggregate tool permissions differ from exact per-case scope")
        per_case = [BudgetLimits(**formal_case_payload(c)["expected_outcome"]["task"]["per_case_budget"]) for c in cases]
        metric_mode=self.env.get("retrieval_evaluation_mode","APPLICATION_TRACE_ONLY")
        if metric_mode not in {"APPLICATION_TRACE_ONLY","FORMAL_FULL_QUERY_BM25"}:raise ValueError("unsupported retrieval scoring object")
        if metric_mode=="FORMAL_FULL_QUERY_BM25":
            cohort=set(self.config["experiment_config"]["reporting_cohorts"]["retrieval"])
            for c,limits in zip(cases,per_case):
                if c.case_id in cohort and (limits.tool_calls<1 or not ({"search_knowledge","get_warranty"}&set(c.input["allowed_capabilities"]) or "owner_interaction" in c.input)):
                    raise ValueError("formal retrieval cohort needs a permitted corpus and one separately metered evaluation read")
        for name in ("provider_calls", "tool_calls", "total_tokens", "estimated_usd", "max_duration_seconds"):
            shared_cost = name == "estimated_usd" and self.env.get("budget_pool_policy") == "ACTUAL_STEP_RESERVATIONS_SHARED_COST_V1"
            required = max(getattr(limits, name) for limits in per_case) if shared_cost else sum(getattr(limits, name) for limits in per_case)
            if required > getattr(self.grant.plan.budget, name) + 1e-9:
                raise ValueError("formal batch caps below locked per-case caps: " + name)
        path = self.store.path.parent / ("repository-admission-" + self.grant.plan.config_sha256 + ".json")
        bindings = [[c.case_id, formal_case_digest(c)] for c in cases]
        if path.exists():
            record = json.loads(path.read_text(encoding="utf8"))
            if record["case_bindings"] != bindings or record["database_identity"] != self.target.identity:
                raise ValueError("admission scope/identity drift")
            return record
        # Never load another fixture around an already consumed unresolved Run.
        with self.store.transaction(immediate=False) as db:
            if db.execute("SELECT 1 FROM formal_grant_events WHERE grant_id=? AND event_type='CLAIMED'", (self.grant.grant_id,)).fetchone():
                raise ValueError("admission missing after attempts; reconcile rather than repeat")
        rows = []
        for case in cases:
            plan = application_plan(case)
            receipt = self.coordinator.prepare(plan)
            reads = []
            normalized=False
            try:
                if "owner_interaction" in case.input:
                    from .formal_actions import materialize_action_facts
                    materialize_action_facts(self,case,receipt)
                    normalized=True
                adapters = self.coordinator.repositories(plan, receipt, maximum=8)
                for kind in sorted({r["kind"] for r in plan["facts"]}):
                    values = adapters["store"].rows(kind)
                    reads.append({"kind": kind, "source_ids": [v["source_id"] for v in values]})
            finally:
                if normalized:
                    from .formal_actions import action_snapshot,cleanup_action_facts
                    from .fixture_read_models import product_id
                    products=[product_id(p["id"]) for p in case.input["fixture"].get("products",[])]
                    cleanup_action_facts(self,receipt,{"products":products,"snapshot":action_snapshot(self,receipt,products)})
                self.coordinator.cleanup(receipt)
            evidence = self.coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
            if evidence["residual_case_rows"] or not evidence["lease_released"]:
                raise ValueError("formal admission receipt cleanup incomplete")
            rows.append({"case_id": case.case_id, "repository_reads": reads, "read_count": len(reads),
                "fixture_receipt": asdict(receipt), "cleanup": evidence, "status": "READONLY_FIXTURE_REPOSITORY_VERIFIED_NOT_MODEL_TASK_PASS"})
        record = {"case_bindings": bindings, "database_identity": self.target.identity, "rows": rows,
            "admission_read_count": sum(row["read_count"] for row in rows), "application_run_count": 0,
            "qualification": "Model route/answer are evaluation targets; Repository verification is not an application task success prediction"}
        write_new(path, record)
        return record

    def prepare(self, case, trial_id):
        # Reloaded grant/runtime/state is the actual new trust boundary.
        self.gate.authorize(mode=__import__("eval.m6_1.execution_modes", fromlist=["ExecutionMode"]).ExecutionMode(self.grant.plan.mode), grant=self.grant, case=case, attempt_id=trial_id)
        write_new(self.artifact_path(trial_id,".case.json"), formal_case_payload(case))
        receipt = self.coordinator.prepare(application_plan(case))
        if "owner_interaction" in case.input:
            from .formal_actions import materialize_action_facts
            try: materialize_action_facts(self, case, receipt)
            except Exception:
                self.coordinator.cleanup(receipt)
                raise
        return {"trial_id": trial_id, "fixture": asdict(receipt)}

    def artifact_path(self,trial_id,suffix):
        # Trial IDs remain intact in records. Bounded filenames also leave room
        # for atomic-write staging on Windows; case IDs must not lengthen paths.
        return self.artifacts/(hashlib.sha256(trial_id.encode()).hexdigest()+suffix)

    def preflight(self, *, require_provider_key=True):
        """Actual process/DB/price/settings checks, before grant issue or claims."""
        from app.m0.config import load_settings
        from datetime import datetime, timezone
        import math
        import os
        if self.config["signature_scheme"] != "ED25519_V1":
            raise ValueError("actual formal application requires public verification; legacy HMAC fixtures are test-only")
        settings = load_settings(require_api_key=require_provider_key)
        from urllib.parse import urlsplit
        endpoint = urlsplit(settings.base_url)
        if endpoint.scheme != "https" or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query:
            raise ValueError("formal Provider endpoint requires TLS and no URL credentials")
        asset = self.gate.formal_authority.registry.get(self.grant.plan.asset_id)
        if self.grant.plan.mode=='ENGINEERING_EVAL':
            if self.config.get('engineering_only') is not True or asset.asset_type!='isolated_engineering_fixture' or asset.scoreability!='NOT_SCOREABLE':
                raise ValueError('engineering application requires explicit non-Gold fixture scope')
        elif not asset.historical_source_ref or asset.historical_source_ref not in dict(self.grant.plan.runtime_bindings):
            raise ValueError("review lock/provenance record must be bound in the formal plan")
        if dependency_versions() != self.env["python_dependencies"]:
            raise ValueError("installed runtime dependency versions differ from frozen binding")
        projection = {"model": settings.model, "endpoint": settings.base_url,
            "nodes": {k: asdict(v) for k, v in settings.nodes.items()}}
        if projection != self.env["provider_binding"]:
            raise ValueError("actual Provider node configuration differs from frozen binding")
        price_path = confined(self.root, self.env["price_evidence"]["path"])
        if raw_hash(price_path) != self.env["price_evidence"]["sha256"]:
            raise ValueError("price evidence binding changed")
        price = json.loads(price_path.read_text(encoding="utf8"))
        now = datetime.now(timezone.utc)
        if not datetime.fromisoformat(price["valid_from_utc"]) <= now < datetime.fromisoformat(price["valid_until_utc"]):
            raise ValueError("price evidence outside accepted freshness window")
        rate = price["conservative_usd_per_million"]
        if price["model"] != settings.model or price["endpoint"] != settings.base_url or not price["source_refs"] or isinstance(rate, bool) or not isinstance(rate, (float, int)) or not math.isfinite(rate) or rate <= 0:
            raise ValueError("priced target/rate/source mismatch")
        fixed = "http://127.0.0.1:7892"
        if self.env.get('container_proxy'):
            from .governance import verify_container_process
            verify_container_process(10002)
            if self.env['container_proxy']!='http://proxy:7892':raise ValueError('container proxy binding invalid')
            fixed=self.env['container_proxy']
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            if os.environ.get(name) != fixed: raise ValueError("actual formal process proxy differs from approved proxy")
        if os.environ.get("NO_PROXY", "") or os.environ.get("no_proxy", ""):
            raise ValueError("formal Provider proxy bypass must be empty")
        from httpx._utils import get_environment_proxies
        proxies = get_environment_proxies()
        if proxies.get("https://") != fixed or any(v is None for v in proxies.values()):
            raise ValueError("HTTPX proxy selection differs from approved process configuration")
        with self.session_factory() as db:
            self.target.check_session(db)
            if db.get_bind().dialect.name == "sqlite" and (db.execute(text("PRAGMA integrity_check")).scalar_one() != "ok" or db.execute(text("PRAGMA foreign_key_check")).first()):
                raise ValueError("formal database integrity check failed")
        if self.target.profile == FormalPostgresDatabase.profile:
            from app.orchestration.checkpointer import checkpoint_schema_ready, run_checkpointer_async
            if not run_checkpointer_async(checkpoint_schema_ready(self.target.database_url, settings.checkpoint_schema)):
                raise ValueError("bound PostgreSQL checkpoint schema is not initialized")
        if self.ledger is None:
            if self.store.budget_snapshot(self.grant.plan.experiment_id):
                raise ValueError("unregistered formal ledger contains usage events")
        else:
            snapshot = self.ledger.snapshot()
            if not snapshot["complete"] or not snapshot["settled"] or snapshot["calls_status"] in {"UNKNOWN", "CONSERVATIVE_UPPER_BOUND"}:
                raise ValueError("formal ledger is unresolved")
        return settings, rate

    def execute(self, case, trial_id, receipt, ledger, *, remaining_seconds,evidence_only=False):
        deadline = time.monotonic() + remaining_seconds
        from app.m0.config import load_settings
        from app.m3.policy_tools import PolicyReadService
        from .formal_scoring import evaluate, grade_result
        plan = application_plan(case)
        limits = BudgetLimits(**formal_case_payload(case)["expected_outcome"]["task"]["per_case_budget"])
        per_case = PerCaseBudgetLedger(ledger, trial_id, limits)
        settings, rate = self.preflight()
        held_receipt = restore_receipt(receipt["fixture"])
        records = []
        reads = self.coordinator.repositories(plan, held_receipt, maximum=self.env["repository_read_cap_per_case"])
        reads["retrieval"] = MeteredReads(reads["retrieval"], per_case, trial_id, records, retrieval=True)
        reads["policy"] = PolicyReadService(reads["retrieval"], evaluation_at=plan["evaluation_at"])
        for key in ("order", "product", "after_sales", "coupon", "policy"):
            reads[key] = MeteredReads(reads[key], per_case, trial_id, records)
        started = time.monotonic()
        remaining_for_runtime = deadline - started
        if remaining_for_runtime <= 0:
            from .budget import BudgetStop
            raise BudgetStop("formal deadline exhausted before application Run creation")
        try:
            from app.orchestration.checkpointer import open_checkpointer
            result = execute_application_case(self.coordinator, plan, held_receipt, per_case, settings,
                trial_id=trial_id, remaining_seconds=min(remaining_for_runtime, limits.max_duration_seconds), repositories=reads,
                conservative_usd_per_million=rate,
                agent_architecture=self.env['trial_architectures'][case.case_id] if 'trial_architectures' in self.env else None,
                checkpointer_factory=open_checkpointer if self.target.profile == FormalPostgresDatabase.profile else None)
            action = None
            measurement = None
            if self.env.get("retrieval_evaluation_mode")=="FORMAL_FULL_QUERY_BM25" and case.case_id in self.config["experiment_config"]["reporting_cohorts"]["retrieval"]:
                measured=[]
                known=ledger.trial_snapshot(trial_id)
                if known["complete"] and known["settled"] and not known["unknown_tokens"] and not known["unknown_cost"]:
                    metric_reads=self.coordinator.repositories(plan,held_receipt,maximum=self.env["repository_read_cap_per_case"])
                    MeteredReads(metric_reads["retrieval"],per_case,trial_id,measured,retrieval=True).retrieve(plan["message"],mode="bm25")
                measurement={"mode":"FORMAL_FULL_QUERY_BM25","run_id":result["run_id"],"calls":measured,
                    "status":"RECORDED" if measured else "SKIPPED_ACCOUNTING_UNVERIFIED",
                    "scope":"Independent retriever-module measurement after chat;not fed back to Router/Agent,not an invented application-hop ranking"}
            if "owner_interaction" in case.input:
                from .formal_actions import execute_owner_interaction
                pre=self.artifact_path(trial_id,".chat-before-owner.json")
                write_new(pre,json.loads(json.dumps({"case_id":case.case_id,"result":result,"read_invocations":records,
                    "retrieval_evaluation":measurement,
                    "phase":"CHAT_COMPLETE_OWNER_NOT_YET_COMPLETE","runtime_binding":{"approved":self.grant.plan.execution_implementation_sha256,
                    "observed_at_end":formal_runtime_digest()}},default=str)))
                receipt["archive_path"]=pre.relative_to(self.root).as_posix()
                receipt["owner_interaction_in_progress"]=True
                action = execute_owner_interaction(self, case, held_receipt, result, per_case, trial_id,
                    remaining_seconds=max(0,deadline-time.monotonic()))
                receipt["owner_interaction_in_progress"]=False
        finally:
            receipt["fixture"] = asdict(held_receipt)
        # Receipt now contains actual Run/session IDs; the generic Runner stores
        # this same updated receipt before cleanup and restart recovery.
        receipt["fixture"] = asdict(held_receipt)
        observed_runtime = formal_runtime_digest()
        archive = json.loads(json.dumps({"case_id": case.case_id, "result": result, "read_invocations": records,
            "runtime_binding": {"approved": self.grant.plan.execution_implementation_sha256, "observed_at_end": observed_runtime}}, default=str))
        if action is not None: archive["owner_interaction"] = action
        if measurement is not None: archive["retrieval_evaluation"] = measurement
        archive_path = self.artifact_path(trial_id,".json")
        write_new(archive_path, archive)
        receipt["archive_path"] = archive_path.relative_to(self.root).as_posix()
        usage = ledger.trial_snapshot(trial_id)
        if evidence_only:
            return {"archive":archive,"receipt":receipt,"usage":usage,
                "latency_ms":{"total_ms":(time.monotonic()-started)*1000},"archive_sha256":raw_hash(archive_path)}
        assessment = evaluate(case, archive, usage,allow_known_classifier_rejection=self.env.get('classifier_semantic_failure_policy')=='CASE_FAIL_CONTINUE_WHEN_FULLY_AUDITED')
        budget_events = ledger.store.budget_snapshot(ledger.experiment_id)
        model_calls = tuple(r for r in budget_events if r["category"] == "provider" and r["event_type"] == "COMMIT" and
            any(e["reservation_id"] == r["reservation_id"] and json.loads(e["payload_json"])["trial_id"] == trial_id
                for e in budget_events if e["event_type"] == "RESERVE"))
        trial = EvalTrial(trial_id, case.case_id, self.grant.plan.experiment_id, 1, (result["run_id"],),
            result["status"], model_calls, tuple(records), archive_path.relative_to(self.root).as_posix(),
            {"code": result["failure_code"]} if result["failure_code"] else None, usage,
            {"total_ms": (time.monotonic() - started) * 1000})
        raw = {"application_archive": archive, "code_assessment": assessment,
            "case_sha256": formal_case_digest(case), "case_path": self.artifact_path(trial_id,".case.json").relative_to(self.root).as_posix(),
            "config_sha256": self.grant.plan.config_sha256, "archive_sha256": raw_hash(archive_path),
            "scoring_runtime_sha256": observed_runtime}
        return FormalTrialResult(trial, raw, grade_result(trial_id, assessment), assessment["integrity"], tuple(assessment["blocking_items"]))

    def inspect_failure(self, receipt):
        """Bounded responsible-source inspection; never infer zero Runs from no Trial."""
        from app.m1.models import Run
        from sqlalchemy import select
        held = restore_receipt(receipt["fixture"])
        with self.session_factory() as db:
            self.coordinator._check_receipt(db, held)
            rows = db.execute(select(Run).where(Run.user_id.in_(held.user_ids))).scalars().all()
            inspection = {"run_created": bool(rows), "source": "ACTUAL_MARKED_TARGET_RECEIPT_OWNER_SCOPE",
                "run_ids": [r.run_id for r in rows], "run_statuses": {r.run_id:r.status for r in rows},
                "lease_id": held.lease_id, "database_identity": held.database_identity,
                "cleanup_status": "PRESERVED_EXECUTION_OR_GRADER_EXCEPTION; raw completeness must be reconciled before cleanup"}
        if receipt.get("owner_interaction_in_progress"):
            inspection.update(failure_stage="OWNER_API_OUTCOME_UNVERIFIED",archive_path=receipt["archive_path"],
                cleanup_status="PRESERVED_OWNER_API_MAY_STILL_BE_EXECUTING_NO_REPLAY")
            return inspection
        # If the application archive already completed before its grader failed,
        # stopping and exact cleanup are independent decisions.
        if "archive_path" in receipt:
            path = confined(self.root, receipt["archive_path"])
            archived = json.loads(path.read_text(encoding="utf8"))
            binding = archived["runtime_binding"]
            inspection.update(archive_path=receipt["archive_path"], archive_sha256=raw_hash(path))
            terminal=len(rows)==1 and rows[0].run_id==held.run_id and rows[0].status in {"SUCCEEDED","FAILED","MODEL_UNAVAILABLE"}
            if terminal and binding["approved"] == binding["observed_at_end"] == self.grant.plan.execution_implementation_sha256:
                try:
                    if not self.cleanup(receipt,{"trial_id":receipt["trial_id"]},{"application_archive":archived,"archive_sha256":raw_hash(path)}):return inspection
                except Exception as exc:
                    inspection.update(cleanup_status="PRESERVED_CLEANUP_NOT_VERIFIED",cleanup_error_type=type(exc).__name__)
                    return inspection
                evidence = self.coordinator.diagnostic_cleanup_evidence(held, cleaned=True)
                inspection.update(cleanup_status=evidence["cleanup_status"], cleanup_evidence=evidence)
        return inspection

    def cleanup(self, receipt, trial, raw):
        held = restore_receipt(receipt["fixture"])
        proof_path = self.artifact_path(trial["trial_id"],".cleanup.json")
        # A crash after cleanup but before formal settlement can resume only
        # against this proof plus actual zero residual/lease checks.
        if proof_path.is_file():
            proof = json.loads(proof_path.read_text(encoding="utf8"))
            if proof["receipt"] != receipt or proof["archive_sha256"] != raw["archive_sha256"]:
                raise ValueError("cleanup recovery proof differs from frozen receipt/archive")
            evidence = self.coordinator.diagnostic_cleanup_evidence(held, cleaned=True)
        else:
            path = confined(self.root, receipt["archive_path"])
            if raw_hash(path) != raw["archive_sha256"]:
                return False
            evidence = self.coordinator.diagnostic_cleanup_evidence(held, cleaned=True)
            already_absent = evidence["lease_released"] and evidence["residual_case_rows"] == 0 and evidence["pending_actions"] == 0
            if "owner_interaction" in raw["application_archive"] and not already_absent:
                from .formal_actions import cleanup_action_facts
                cleanup_action_facts(self, held, raw["application_archive"]["owner_interaction"])
            if self.target.profile == FormalPostgresDatabase.profile:
                from app.orchestration.checkpointer import open_checkpointer,run_checkpointer_async
                from app.m0.config import load_settings
                schema=load_settings(require_api_key=False).checkpoint_schema
                async def remove_checkpoint():
                    async with open_checkpointer(self.target.database_url,schema) as saver:
                        config={"configurable":{"thread_id":held.run_id,"checkpoint_ns":""}}
                        saved=await saver.aget_tuple(config)
                        proof=self.artifacts/(held.run_id+".checkpoint-cleanup.json")
                        if saved is None and not proof.exists():raise ValueError("missing durable checkpoint before cleanup")
                        if saved is not None:
                            await saver.adelete_thread(held.run_id)
                            if await saver.aget_tuple(config) is not None:raise ValueError("checkpoint cleanup incomplete")
                            write_new(proof,{"run_id":held.run_id,"checkpoint_id":saved.config["configurable"]["checkpoint_id"],"deleted_exact_thread":True})
                run_checkpointer_async(remove_checkpoint())
            evidence = self.coordinator.diagnostic_cleanup_evidence(held, cleaned=True)
            already_absent = evidence["lease_released"] and evidence["residual_case_rows"] == 0 and evidence["pending_actions"] == 0
            if not already_absent:
                if not self.coordinator.diagnostic_cleanup_safety(held, path)["safe_to_cleanup"]:
                    return False
                self.coordinator.cleanup(held)
                evidence = self.coordinator.diagnostic_cleanup_evidence(held, cleaned=True)
            write_new(proof_path, {"receipt": receipt, "archive_sha256": raw["archive_sha256"], "evidence": evidence})
        return evidence["residual_case_rows"] == 0 and evidence["lease_released"] and evidence["pending_actions"] == 0


def factory(**kwargs):
    if kwargs['config']['experiment_config']['environment'].get('container_endpoint'):
        from .container_boundary import ContainerApplication
        return ContainerApplication(**kwargs)
    return FormalApplication(**kwargs)


def initialize_target(root: Path, relative_database: str):
    """Explicit dedicated new-file provisioning; never relabel an existing DB."""
    target = PersonalDevDatabase(root, profile=PROFILE, relative_database=Path(relative_database))
    if target.path.exists():
        raise ValueError("target already exists; reconcile/read identity rather than reinitialize")
    engine, _factory = target.open(initialize=True)
    engine.dispose()
    return {"profile": PROFILE, "identity": target.identity, "path": relative_database}
