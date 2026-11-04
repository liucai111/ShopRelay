"""Fixed-profile personal D1 technical Smoke; never changes formal gates."""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PROXY = "http://127.0.0.1:7892"
for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
    os.environ[key] = PROXY


def configure_proxy() -> dict:
    # Preserve unrelated bypasses, but remove the Provider host and wildcard bypasses.
    for upper, lower in (("NO_PROXY", "no_proxy"),):
        entries = [x.strip() for x in (os.environ.get(upper) or os.environ.get(lower) or "").split(",") if x.strip()]
        kept = [x for x in entries if x != "*" and x.lstrip(".").casefold() not in {"deepseek.com", "api.deepseek.com"}]
        value = ",".join(kept)
        os.environ[upper] = value
        os.environ[lower] = value
    import httpx
    proxies = httpx._utils.get_environment_proxies()
    selected = proxies.get("https://")
    if selected != PROXY:
        raise RuntimeError("HTTPX_PROVIDER_PROXY_NOT_SELECTED")
    return {"proxy_host": "127.0.0.1", "proxy_port": 7892,
        "httpx_https_proxy_selected": True, "provider_host_bypassed": False}


configure_proxy()
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

from app.m0.config import load_settings
from app.m3.product_tools import ProductFilters, parse_product_request
from eval.m6_1.budget import BudgetLedger, BudgetLimits, BudgetStop
from eval.m6_1.personal_dev import (D1Coordinator, D1PreflightError, OS_ISOLATION_STATUS,
    create_budget_ledger, implementation_digest, plan_case, validate_execution_budget)
from eval.m6_1.personal_dev_authorization import load_price_evidence
from eval.m6_1.personal_dev_database import PersonalDevDatabase
from eval.m6_1.personal_dev_runtime import PerCaseBudgetLedger
from eval.m6_1.contracts import sha256_json
from eval.m6_1.personal_smoke import (SMOKE_AUTHORIZATION_TYPE, SMOKE_DATASET_PATH,
    SMOKE_DATASET_SHA256, SMOKE_DATASET_VERSION, SMOKE_LABELS,
    SmokeAdmissionError, budget_contract, evaluate_smoke_case,
    locked_rules, validate_smoke_bindings, smoke_profile, SMOKE_NINE_PROFILE_ID,
    SMOKE_EIGHT_PROFILE_ID, SMOKE_EIGHT_R2_PROFILE_ID, POLICY014_SAFETY038_PROFILE_ID,
    POLICY014_SAFETY038_R2_PROFILE_ID,
    POLICY014_SAFETY038_R3_PROFILE_ID,
    SAFETY038_R4_PROFILE_ID,
    SAFETY038_SCORING_PROFILE_ID,
    M62_PILOT_PROFILE_ID,
    M62_PILOT_R2_PROFILE_ID, M62_PILOT_FIVE_R1_PROFILE_ID,
    M62_PILOT_FIVE_R2_PROFILE_ID,
    runtime_dependency_binding, archive_activity_violations,
    SMOKE_FAILURE_CLASSIFICATION_VERSION)
from eval.m6_1.store import ResultStore


PRICE_PATH = ROOT / "docs/eval/d1-diagnostic-r6-policy013-20261027/official-pricing-evidence-r6.json"
M62_PILOT_PRICE_PATH = ROOT / "docs/eval/m6.2-outcome-pilot-r2-20261028/official-pricing-evidence-m6.2-r1.json"
SMOKE_PARENT = ROOT / "docs/eval/personal-dev-smokes"
NINE_PREFLIGHT_ROOT = ROOT / "docs/eval/personal-dev-smokes-preflight/M61D1-SMOKE-PREFLIGHT-BLOCKED-c0491e08f63b47bca738b6c92a4b1334"
NINE_PREFLIGHT_SHA256 = "8e32ebceca14516f193577e6d6ab4943d681a64b7847b369635f441564f50673"
NINE_CLOSEOUT_R2_SHA256 = "8631028c232781247f012e166293fc23034bb1f12226067016b92946fb5e98fa"
PRIOR_EIGHT_SMOKE_ID = "M61D1-SMOKE-R324-8CASE-699805785072420889281243a85b617b"
PRIOR_EIGHT_CODE_SHA256 = "710f36e1bad4d59d2ea2c962c220062865e5bf4eb5f3c8d622d5ccea921cee14"
PRIOR_EIGHT_RECON_SHA256 = "721637d6b61d9fdc38ebbc5854767286210d280be96efdbc809ffbf82ba67c9e"
PRIOR_EIGHT_CASE_ARCHIVES = {
    "D1-PRODUCT-001": ("docs/eval/personal-dev-runs/0abcbfb83fd2466fb0987ab438ff8fde.json",
        "fecd813523c59c9b46fabada0c2d26c03ad34e944ce64924cd702c7e5c601e57"),
    "D1-PRODUCT-004": ("docs/eval/personal-dev-runs/4c368dd014244240b366e5033bac8a10.json",
        "a3f560e9f6e9f66668dea8fb4edcb1debf2363a93a3a2a7624188418c4bea565")}
NINE_CASE_IDS = ("D1-PRODUCT-001", "D1-PRODUCT-004", "D1-ORDER-007", "D1-ORDER-009",
    "D1-ORDER-011", "D1-POLICY-013", "D1-POLICY-014", "D1-CROSSAGENT-029", "D1-SAFETY-038")
SAFETY038_ATTEMPT_SCOPE = "CHAT_USER_SELF_CONFIRMED-SAFETY038-20261028-r1"
SAFETY038_ATTEMPT_PACKAGE = ROOT / "docs/eval/d1-safety038-single-run-r4-20261028"
SAFETY038_ATTEMPT_LEDGER = SAFETY038_ATTEMPT_PACKAGE / "attempt-ledger-r1.jsonl"
SAFETY038_ATTEMPT_CLAIM = SAFETY038_ATTEMPT_PACKAGE / "attempt-r1.claim"
SAFETY038_SCORING_ATTEMPT_SCOPE = "CHAT_USER_SELF_CONFIRMED-SAFETY038-SCORING-CLOSEOUT-20261028-r1"
SAFETY038_SCORING_ATTEMPT_PACKAGE = ROOT / "docs/eval/m6.1-smoke-scoring-closeout-r1-20261028"
SAFETY038_SCORING_ATTEMPT_LEDGER = SAFETY038_SCORING_ATTEMPT_PACKAGE / "attempt-ledger-r1.jsonl"
SAFETY038_SCORING_ATTEMPT_CLAIM = SAFETY038_SCORING_ATTEMPT_PACKAGE / "attempt-r1.claim"


def _safety038_attempt_binding(profile_id: str) -> tuple[str, Path, Path]:
    if profile_id == SAFETY038_R4_PROFILE_ID:
        return SAFETY038_ATTEMPT_SCOPE, SAFETY038_ATTEMPT_LEDGER, SAFETY038_ATTEMPT_CLAIM
    if profile_id == SAFETY038_SCORING_PROFILE_ID:
        return SAFETY038_SCORING_ATTEMPT_SCOPE, SAFETY038_SCORING_ATTEMPT_LEDGER, SAFETY038_SCORING_ATTEMPT_CLAIM
    raise SmokeAdmissionError("SAFETY038_ATTEMPT_PROFILE_NOT_ALLOWLISTED")


def _scope_transition(profile_id: str) -> dict | None:
    if profile_id == SMOKE_NINE_PROFILE_ID:
        return None
    # This profile is a new, independent two-case acceptance package. It does
    # not consume the old eight-case batch or any replay report as a runtime
    # predecessor; its own manifest, rules, authorization and journal bind it.
    if profile_id in {POLICY014_SAFETY038_PROFILE_ID, POLICY014_SAFETY038_R2_PROFILE_ID,
            POLICY014_SAFETY038_R3_PROFILE_ID, SAFETY038_R4_PROFILE_ID,
            SAFETY038_SCORING_PROFILE_ID, M62_PILOT_PROFILE_ID, M62_PILOT_R2_PROFILE_ID,
            M62_PILOT_FIVE_R1_PROFILE_ID, M62_PILOT_FIVE_R2_PROFILE_ID}:
        return None
    if profile_id not in {SMOKE_EIGHT_PROFILE_ID, SMOKE_EIGHT_R2_PROFILE_ID}:
        raise SmokeAdmissionError("SMOKE_PROFILE_NOT_ALLOWLISTED")
    preflight_path = NINE_PREFLIGHT_ROOT / "preflight.json"
    closeout_path = NINE_PREFLIGHT_ROOT / "closeout-r2.json"
    if (_sha(preflight_path) != NINE_PREFLIGHT_SHA256 or _sha(closeout_path) != NINE_CLOSEOUT_R2_SHA256):
        raise SmokeAdmissionError("SOURCE_NINE_CASE_PREFLIGHT_BINDING_MISMATCH")
    preflight, closeout = _read_json(preflight_path), _read_json(closeout_path)
    if (preflight.get("status") != "BLOCKED" or preflight.get("authorization_created") is not False or
            preflight.get("run_created") is not False or preflight.get("provider_requests") != 0 or
            preflight.get("case_order") != list(NINE_CASE_IDS) or
            preflight.get("blocking_cases") != ["D1-CROSSAGENT-029"] or
            closeout.get("preflight", {}).get("preflight_pass") != 8 or
            closeout.get("preflight", {}).get("preflight_fail") != 1 or
            closeout.get("execution", {}).get("not_run_case_ids") != list(NINE_CASE_IDS)):
        raise SmokeAdmissionError("SOURCE_NINE_CASE_PREFLIGHT_CONTENT_MISMATCH")
    prior_root = SMOKE_PARENT / PRIOR_EIGHT_SMOKE_ID
    prior_closeout_path = prior_root / "reports/closeout.json"
    prior_reconciliation_path = prior_root / "reports/stop-reconciliation-r1.json"
    prior_manifest = _read_json(prior_root / "manifest.json")
    prior_rules = _read_json(prior_root / "locked-rules.json")
    prior_auth = _read_json(prior_root / "authorization.json")
    prior_closeout = _read_json(prior_closeout_path)
    prior_reconciliation = _read_json(prior_reconciliation_path)
    prior_ledger = _read_json(prior_root / "reports/batch-budget-final.json")
    if (_sha(prior_reconciliation_path) != PRIOR_EIGHT_RECON_SHA256 or
            prior_manifest.get("code_sha256") != PRIOR_EIGHT_CODE_SHA256 or
            prior_manifest.get("dataset_sha256") != SMOKE_DATASET_SHA256 or
            prior_manifest.get("profile_id") != "eight-case-r1" or
            prior_manifest.get("case_order") != list(smoke_profile(SMOKE_EIGHT_PROFILE_ID).cases) or
            prior_rules.get("case_order") != list(smoke_profile(SMOKE_EIGHT_PROFILE_ID).cases) or
            prior_auth.get("status") != "CLOSED_STOPPED" or
            prior_closeout.get("status") != "SMOKE_STOPPED" or
            prior_closeout.get("requested_case_count") != 8 or
            prior_closeout.get("executed_case_count") != 2 or
            prior_closeout.get("not_run_count") != 6 or
            prior_closeout.get("not_run_case_ids") != list(smoke_profile(SMOKE_EIGHT_PROFILE_ID).cases[2:]) or
            prior_reconciliation.get("counts", {}).get("attempted") != 2 or
            prior_reconciliation.get("counts", {}).get("not_run") != 6 or
            prior_reconciliation.get("budget", {}).get("complete") is not True or
            prior_reconciliation.get("budget", {}).get("settled") is not True or
            prior_reconciliation.get("budget", {}).get("provider_requests") != 1 or
            prior_reconciliation.get("budget", {}).get("read_only_calls") != 4 or
            prior_reconciliation.get("budget", {}).get("tokens") != 823 or
            prior_ledger.get("calls_by_category", {}).get("provider") != 1 or
            prior_ledger.get("calls_by_category", {}).get("tool") != 4 or
            prior_ledger.get("tokens") != 823):
        raise SmokeAdmissionError("SOURCE_PRIOR_EIGHT_CASE_EXECUTION_BINDING_MISMATCH")
    prior_checksums = {}
    for line in (prior_root / "SHA256SUMS.md").read_text(encoding="utf-8").splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            prior_checksums[parts[1].strip()] = parts[0].strip().lower()
    for relative, expected_sha in (("manifest.json", None), ("locked-rules.json", None),
            ("authorization.json", None), ("reports/closeout.json", "72b7d93fdd0f13ebb45c0b3e84f11f9eb50ab7584c76bafe54712e1c187c457e"),
            ("reports/batch-budget-final.json", "e23016450f818ccc0b8b99654a7f238dc0ca251cf24af4eec50d33729ace595e")):
        actual_sha = _sha(prior_root / relative)
        if expected_sha and actual_sha != expected_sha:
            raise SmokeAdmissionError(f"SOURCE_PRIOR_EIGHT_CASE_HASH_MISMATCH:{relative}")
        if prior_checksums.get(relative) != actual_sha:
            raise SmokeAdmissionError(f"SOURCE_PRIOR_EIGHT_CASE_MANIFEST_MISMATCH:{relative}")
    for case_id, (relative, expected_sha) in PRIOR_EIGHT_CASE_ARCHIVES.items():
        if _sha(ROOT / relative) != expected_sha:
            raise SmokeAdmissionError(f"SOURCE_PRIOR_CASE_ARCHIVE_HASH_MISMATCH:{case_id}")
        source_hash = next((row.get("sha256") for row in prior_reconciliation.get("source_sha256", [])
            if row.get("path") == relative), None)
        if source_hash != expected_sha:
            raise SmokeAdmissionError(f"SOURCE_PRIOR_CASE_ARCHIVE_RECONCILIATION_MISMATCH:{case_id}")
    return {"source_preflight_path": preflight_path.relative_to(ROOT).as_posix(),
        "source_preflight_sha256": NINE_PREFLIGHT_SHA256,
        "source_closeout_path": closeout_path.relative_to(ROOT).as_posix(),
        "source_closeout_sha256": NINE_CLOSEOUT_R2_SHA256,
        "original_nine_case_order": list(NINE_CASE_IDS),
        "original_nine_preflight": {"pass": 8, "fail": 1,
            "blocking_case": "D1-CROSSAGENT-029", "authorization_created": False,
            "real_cases_executed": 0, "all_case_statuses": "NOT_RUN"},
        "new_scope_profile_id": profile_id,
        "new_fixed_case_ids": list(smoke_profile(profile_id).cases),
        "prior_eight_case_batch": {"smoke_id": PRIOR_EIGHT_SMOKE_ID,
            "manifest_path": (prior_root / "manifest.json").relative_to(ROOT).as_posix(),
            "manifest_sha256": _sha(prior_root / "manifest.json"),
            "original_closeout_path": prior_closeout_path.relative_to(ROOT).as_posix(),
            "original_closeout_sha256": _sha(prior_closeout_path),
            "reconciliation_path": prior_reconciliation_path.relative_to(ROOT).as_posix(),
            "reconciliation_sha256": _sha(prior_reconciliation_path),
            "prior_code_sha256": PRIOR_EIGHT_CODE_SHA256,
            "authorization_status": "CLOSED_STOPPED",
            "attempted_cases": ["D1-PRODUCT-001", "D1-PRODUCT-004"],
            "not_run_case_ids": list(smoke_profile(SMOKE_EIGHT_PROFILE_ID).cases[2:]),
            "derived_settled_ledger": {"provider_requests": 1, "read_only_calls": 4,
                "tokens": 823, "local_estimated_usd": prior_reconciliation.get("budget", {}).get("local_estimated_usd"),
                "supplier_billing": "UNKNOWN"}},
        "excluded_case": {"case_id": "D1-CROSSAGENT-029", "status": "HELD_NOT_RUN",
            "reason_code": "CROSSAGENT_PRODUCT_IDENTIFIER_UNRESOLVED_BY_ALLOWED_CAPABILITIES"},
        "coverage_not_included": "Cross-agent product/inventory/policy composition; this eight-case run does not cover it."}


def _json_bytes(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _write_new(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def _write_json(path: Path, value: dict) -> None:
    with path.open("wb") as stream:
        stream.write(_json_bytes(value))
        stream.flush()
        os.fsync(stream.fileno())


def _append_jsonl(path: Path, value: dict, *, create: bool = False) -> None:
    mode = "x" if create else "a"
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _attempt_ledger_rows(*, initialize: bool = False,
        profile_id: str = SAFETY038_R4_PROFILE_ID) -> list[dict]:
    scope_id, path, _claim_path = _safety038_attempt_binding(profile_id)
    if not path.exists():
        if not initialize:
            raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_MISSING")
        path.parent.mkdir(parents=True, exist_ok=True)
        genesis = {"schema_version": "personal-dev-instruction-attempt-ledger-v1",
            "scope_id": scope_id, "event": "SCOPE_OPENED",
            "allowed_case_ids": ["D1-SAFETY-038"], "max_real_runs": 1,
            "budget_reservation": {"provider_calls": 4, "policy_reads": 1,
                "tokens": 120000, "estimated_usd": 0.18},
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "previous_hash": None}
        genesis["event_hash"] = sha256_json(genesis)
        _append_jsonl(path, genesis, create=True)
    rows = []
    previous = None
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        try:
            row = json.loads(line)
        except (TypeError, ValueError) as exc:
            raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_CORRUPT") from exc
        if not isinstance(row, dict) or row.get("scope_id") != scope_id or row.get("previous_hash") != previous:
            raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_CHAIN_INVALID")
        recorded = row.get("event_hash")
        payload = {key: value for key, value in row.items() if key != "event_hash"}
        if recorded != sha256_json(payload):
            raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_HASH_INVALID")
        if index == 0 and row.get("event") != "SCOPE_OPENED":
            raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_GENESIS_INVALID")
        rows.append(row)
        previous = recorded
    if not rows:
        raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_EMPTY")
    return rows


def _append_attempt_event(event: str, *, profile_id: str = SAFETY038_R4_PROFILE_ID, **fields) -> dict:
    scope_id, path, _claim_path = _safety038_attempt_binding(profile_id)
    rows = _attempt_ledger_rows(profile_id=profile_id)
    payload = {"schema_version": "personal-dev-instruction-attempt-ledger-v1",
        "scope_id": scope_id, "event": event,
        "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "previous_hash": rows[-1]["event_hash"], **fields}
    payload["event_hash"] = sha256_json(payload)
    _append_jsonl(path, payload)
    return payload


def _check_safety038_attempt_available(*, claim: bool = False, smoke_id: str | None = None,
        manifest: dict | None = None, profile_id: str = SAFETY038_R4_PROFILE_ID) -> dict:
    scope_id, ledger_path, claim_path = _safety038_attempt_binding(profile_id)
    rows = _attempt_ledger_rows(initialize=claim, profile_id=profile_id)
    claims = [row for row in rows if row.get("event") == "ATTEMPT_CLAIMED"]
    if len(claims) > 1:
        raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_MULTIPLE_CLAIMS")
    if claim:
        if len(claims) != 0 or claim_path.exists() or not smoke_id or not isinstance(manifest, dict):
            raise SmokeAdmissionError("SAFETY038_INSTRUCTION_ATTEMPT_ALREADY_CONSUMED")
        if (manifest.get("profile_id") != profile_id or
                manifest.get("case_order") != ["D1-SAFETY-038"] or
                manifest.get("dataset_sha256") != SMOKE_DATASET_SHA256):
            raise SmokeAdmissionError("SAFETY038_ATTEMPT_SCOPE_BINDING_MISMATCH")
        claim_payload = {"scope_id": scope_id, "case_id": "D1-SAFETY-038",
            "smoke_id": smoke_id, "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "reserved_provider_calls": 4, "reserved_policy_reads": 1,
            "reserved_tokens": 120000, "reserved_estimated_usd": 0.18}
        with claim_path.open("x", encoding="utf-8", newline="\n") as marker:
            marker.write(json.dumps(claim_payload, sort_keys=True) + "\n")
            marker.flush()
            os.fsync(marker.fileno())
        return _append_attempt_event("ATTEMPT_CLAIMED", profile_id=profile_id,
            case_id="D1-SAFETY-038", smoke_id=smoke_id,
            manifest_sha256=_sha(SMOKE_PARENT / smoke_id / "manifest.json"),
            reserved_provider_calls=4, reserved_policy_reads=1,
            reserved_tokens=120000, reserved_estimated_usd=0.18,
            real_run_count=1, consumed_before_run_creation=True)
    if claims or claim_path.exists():
        raise SmokeAdmissionError("SAFETY038_INSTRUCTION_ATTEMPT_ALREADY_CONSUMED")
    return {"available": True, "claims": 0, "scope_id": scope_id,
        "ledger_sha256": _sha(ledger_path), "genesis_hash": rows[0]["event_hash"]}


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise SmokeAdmissionError(f"JSON_OBJECT_REQUIRED:{path.name}")
    return value


def _case_capabilities(case: dict) -> list[str]:
    rows = case["input"].get("expected_tool_calls", [])
    if any(row.get("mode") != "read_only" for row in rows):
        raise SmokeAdmissionError(f"NON_READ_ONLY_CAPABILITY:{case['case_id']}")
    return sorted(row["tool_id"] for row in rows)


def _case_input_tool_contracts(bundle: dict, profile_id: str) -> list[dict]:
    """Check that required tool arguments can be derived from the actual user input.

    Fixture provenance is deliberately not used to manufacture a runtime argument.
    """
    profile = smoke_profile(profile_id)
    cases = {row["case_id"]: row for row in bundle.get("cases", [])}
    findings = []
    for case_id in profile.cases:
        case = cases.get(case_id)
        if case is None:
            findings.append({"case_id": case_id, "status": "FAIL", "reason_code": "CASE_NOT_IN_DATASET"})
            continue
        query = case.get("input", {}).get("user_query", "")
        capabilities = _case_capabilities(case)
        if "get_product_detail" in capabilities:
            product_id, _filters, _stock = parse_product_request(query)
            if not product_id and "search_products" not in capabilities:
                findings.append({"case_id": case_id, "status": "FAIL",
                    "reason_code": "TOOL_ARGUMENT_NOT_DERIVABLE_FROM_USER_INPUT",
                    "tool": "get_product_detail", "required_argument": "product_id",
                    "argument_derived": False, "alternative_search_capability_allowed": False,
                    "fixture_refs_not_used_as_arguments": True})
                continue
        if "get_order" in capabilities and not any(token in query for token in ("ORD-",)):
            findings.append({"case_id": case_id, "status": "FAIL",
                "reason_code": "TOOL_ARGUMENT_NOT_DERIVABLE_FROM_USER_INPUT",
                "tool": "get_order", "required_argument": "order_id", "argument_derived": False})
            continue
        findings.append({"case_id": case_id, "status": "PASS",
            "reason_code": "REQUIRED_READ_ARGUMENTS_DERIVABLE_OR_NO_BUSINESS_TOOL"})
    return findings


def _check_database_empty(target: PersonalDevDatabase, factory) -> None:
    from sqlalchemy import select, text
    from app.m1.models import PendingAction, Run, Order, Shipment, User
    with factory() as db:
        target.check_session(db)
        marker = db.execute(text("SELECT lease_id,receipt FROM d1_personal_target")).one()
        if marker.lease_id is not None or marker.receipt is not None:
            raise D1PreflightError("SMOKE_DATABASE_HAS_ACTIVE_LEASE_OR_RECEIPT")
        for model in (User, Order, Shipment, Run, PendingAction):
            if db.execute(select(model).limit(1)).first():
                raise D1PreflightError(f"SMOKE_DATABASE_NOT_EMPTY:{model.__tablename__}")
        if db.execute(text("SELECT 1 FROM d1_fixture_facts LIMIT 1")).first():
            raise D1PreflightError("SMOKE_DATABASE_NOT_EMPTY:d1_fixture_facts")


def _check_no_existing_reservations(path: Path) -> None:
    if not path.is_file():
        raise D1PreflightError("PERSONAL_BUDGET_LEDGER_MISSING")
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        rows = [dict(row) for row in db.execute(
            "SELECT event_type,reservation_id,category,calls,tokens,estimated_usd,billed_usd,payload_json "
            "FROM budget_events ORDER BY rowid")]
    try:
        states = BudgetLedger._derive_states(rows)
    except Exception as exc:
        raise D1PreflightError("PERSONAL_BUDGET_LEDGER_INTEGRITY_UNKNOWN") from exc
    if any(row.get("state") == "HELD" for row in states.values()):
        raise D1PreflightError("PERSONAL_BUDGET_LEDGER_HAS_UNSETTLED_RESERVATION")


def _preflight_read_case(coordinator, plan: dict, profile_id: str = SMOKE_NINE_PROFILE_ID) -> dict:
    profile = smoke_profile(profile_id)
    case_id = plan["case_id"]
    allowed = set(_case_capabilities(plan["case"]))
    if allowed == set():
        if case_id not in {"D1-ORDER-009", "D1-SAFETY-037"} or plan.get("facts") or plan.get("order_ids"):
            raise SmokeAdmissionError("ZERO_READ_CASE_HAS_TARGET_FACTS")
        return {"status": "PASS", "business_reads": 0, "target_lookup": "NONE"}
    receipt = coordinator.prepare(plan)
    observations = []
    try:
        # Admission reads are separately recorded and do not consume the case trial cap.
        repos = coordinator.repositories(plan, receipt, maximum=profile.tool_caps[case_id] + 8)
        products = [row["resource_id"] for row in plan["facts"] if row["kind"] == "products"]
        product_sources = {row["resource_id"]: row["source_id"] for row in plan["facts"] if row["kind"] == "products"}
        if case_id == "D1-PRODUCT-002":
            product_id = products[0]
            detail = repos["product"].get_product_detail(product_id)
            attrs = detail.get("product", {}).get("recorded_attributes", {})
            if (detail.get("availability") != "AVAILABLE" or
                    detail.get("product", {}).get("fixture_provenance") != product_sources[product_id] or
                    attrs.get("capacity_ml") != 320):
                raise SmokeAdmissionError("PRODUCT_CAPACITY_FIXTURE_NOT_READABLE")
            observations.append({"capability": "get_product_detail", "source": detail["source"],
                "fixture_provenance": detail["product"].get("fixture_provenance"),
                "capacity_ml": attrs.get("capacity_ml")})
        elif case_id == "D1-PRODUCT-003":
            from app.m3.product_tools import ProductFilters
            found = repos["product"].search_products(ProductFilters(keyword="Organizer", limit=10))
            recommendation = repos["product"].recommend_products(ProductFilters(keyword="Organizer", limit=10))
            product_id = products[0]
            products_found = [row for row in found.get("products", [])
                if row.get("fixture_provenance") == product_sources[product_id]]
            recs = [row for row in recommendation.get("recommendations", [])
                if row.get("product", {}).get("fixture_provenance") == product_sources[product_id]]
            if (found.get("truncated") or len(products_found) != 1 or len(recs) != 1 or
                    products_found[0].get("recorded_attributes", {}).get("depth_cm") != 12 or
                    products_found[0].get("recorded_attributes", {}).get("price_credits") != 18):
                raise SmokeAdmissionError("PRODUCT_RECOMMENDATION_FIXTURE_NOT_READABLE")
            observations.extend([{"capability": "search_products", "source": found["source"],
                    "fixture_provenance": product_sources[product_id], "count": found.get("count")},
                {"capability": "recommend_products", "source": recommendation["source"],
                    "fixture_provenance": product_sources[product_id], "count": recommendation.get("count")}])
        elif case_id == "D1-PRODUCT-001":
            found = repos["product"].search_products(ProductFilters(limit=20))
            if found.get("truncated") or found.get("count") != len(products) or found.get("count") < 2:
                raise SmokeAdmissionError("PRODUCT_FIXTURE_SEARCH_INCOMPLETE")
            observations.append({"capability": "search_products", "source": found["source"], "count": found["count"]})
            for product_id in products:
                detail = repos["product"].get_product_detail(product_id)
                if detail.get("availability") != "AVAILABLE":
                    raise SmokeAdmissionError("PRODUCT_FIXTURE_DETAIL_UNAVAILABLE")
                observations.append({"capability": "get_product_detail", "source": detail["source"],
                    "product_fixture_provenance": detail["product"].get("fixture_provenance")})
        elif case_id == "D1-PRODUCT-004":
            result = repos["product"].check_inventory(products[0])
            if result.get("source") != "shoprelay_inventory" or not result.get("records"):
                raise SmokeAdmissionError("INVENTORY_FIXTURE_UNAVAILABLE")
            observations.append({"capability": "check_inventory", "source": result["source"],
                "records": len(result["records"]), "freshness": result["records"][0].get("freshness")})
        elif case_id == "D1-ORDER-007":
            result = repos["order"].get_order(plan["order_ids"][0], plan["owner_id"])
            if result.get("order_id") != plan["order_ids"][0]:
                raise SmokeAdmissionError("ORDER_FIXTURE_EXACT_ID_MISMATCH")
            observations.append({"capability": "get_order", "source": result["source"],
                "order_id": result["order_id"], "fixture_provenance": result.get("fixture_provenance")})
        elif case_id == "D1-ORDER-011":
            result = repos["order"].get_order_items(plan["order_ids"][0], plan["owner_id"])
            if len(result.get("items", [])) != 2:
                raise SmokeAdmissionError("ORDER_ITEMS_FIXTURE_COUNT_MISMATCH")
            observations.append({"capability": "get_order_items", "source": result["source"],
                "order_id": result["order_id"], "item_count": len(result["items"])})
        elif case_id in {"D1-POLICY-013", "D1-POLICY-014", "D1-SAFETY-038",
                "D1-POLICY-017", "D1-POLICY-018"}:
            result = repos["policy"].search(plan["message"], result_ref=f"smoke-preflight:{case_id}")
            expected = set(plan["case"]["input"].get("acceptable_evidence_ids", []))
            seen = {row.get("chunk_id") for row in result.get("evidence", [])}
            if not expected.issubset(seen):
                raise SmokeAdmissionError("POLICY_FIXTURE_REQUIRED_EVIDENCE_MISSING")
            observations.append({"capability": "search_knowledge", "source": result["source"],
                "evidence_ids": sorted(seen & expected), "knowledge_version": result["knowledge_version"],
                "index_fingerprint": result["index_fingerprint"]})
        elif case_id == "D1-SAFETY-036":
            order_id = plan["order_ids"][0]
            result = repos["order"].get_order(order_id, plan["owner_id"])
            receipt_source_ids = {row.get("source_id") for row in plan.get("facts", [])
                if row.get("kind") == "orders_and_snapshots"}
            owner_mapping_matches = any(row.get("kind") == "orders_and_snapshots" and
                row.get("owner_id") == plan.get("owner_id") for row in plan.get("facts", []))
            if (result.get("order_id") != order_id or result.get("fixture_provenance") not in receipt_source_ids or
                    not receipt_source_ids or not owner_mapping_matches):
                raise SmokeAdmissionError("SAFETY_OWN_ORDER_FIXTURE_NOT_READABLE")
            observations.append({"capability": "get_order", "source": result["source"],
                "order_id": result["order_id"], "fixture_provenance": result.get("fixture_provenance"),
                "owner_mapping_validated": True})
        elif case_id == "D1-CROSSAGENT-029" and profile.profile_id == SMOKE_NINE_PROFILE_ID:
            from app.m3.product_tools import parse_product_request_many
            explicit_ids, _filters, _wants_stock = parse_product_request_many(plan["message"])
            product_ids = list(explicit_ids)
            if not product_ids:
                product_ids = repos["product"].resolve_product_references(plan["message"])
            resolved_id = product_ids[0] if len(product_ids) == 1 else None
            if not resolved_id:
                resolved_id = repos["product"].resolve_product_reference(plan["message"])
            if not resolved_id:
                raise SmokeAdmissionError("CROSSAGENT_PRODUCT_IDENTIFIER_UNRESOLVED_BY_ALLOWED_CAPABILITIES")
            if resolved_id != products[0]:
                raise SmokeAdmissionError("CROSSAGENT_PRODUCT_IDENTIFIER_NOT_BOUND_TO_CASE_FIXTURE")
            detail = repos["product"].get_product_detail(resolved_id)
            inventory_fact = next(row for row in plan["facts"] if row["kind"] == "inventory")
            inventory = repos["product"].check_inventory(resolved_id)
            policy = repos["policy"].search(plan["message"], result_ref=f"smoke-preflight:{case_id}")
            if (detail.get("availability") != "AVAILABLE" or not inventory.get("records") or
                    not policy.get("evidence")):
                raise SmokeAdmissionError("CROSSAGENT_PRECHECK_FACT_SOURCE_MISSING")
            observations.extend([{"capability": "get_product_detail", "source": detail["source"],
                    "resolved_product_id": resolved_id},
                {"capability": "check_inventory", "source": inventory["source"],
                    "fixture_provenance": inventory["records"][0].get("fixture_provenance"),
                    "freshness": inventory["records"][0].get("freshness")},
                {"capability": "search_knowledge", "source": policy["source"],
                    "evidence_ids": [row.get("chunk_id") for row in policy.get("evidence", [])]}])
        else:
            raise SmokeAdmissionError(f"SMOKE_PREFLIGHT_CASE_UNIMPLEMENTED:{case_id}")
        return {"status": "PASS", "business_reads": repos["store"].allowance.calls,
            "observations": observations}
    finally:
        coordinator.cleanup(receipt)
        evidence = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=True)
        if (evidence.get("cleanup_status") != "CLEANED_AND_RELEASED" or
                evidence.get("residual_case_rows") != 0 or evidence.get("pending_actions") != 0):
            raise D1PreflightError(f"SMOKE_PREFLIGHT_CLEANUP_FAILED:{case_id}")


def _pricing_path(profile_id: str) -> Path:
    if profile_id in {M62_PILOT_PROFILE_ID, M62_PILOT_R2_PROFILE_ID,
            M62_PILOT_FIVE_R1_PROFILE_ID, M62_PILOT_FIVE_R2_PROFILE_ID}:
        return M62_PILOT_PRICE_PATH
    return PRICE_PATH


def _runtime_context(*, require_api_key: bool, profile_id: str = SMOKE_NINE_PROFILE_ID):
    asset_path = ROOT / SMOKE_DATASET_PATH
    asset_bytes = asset_path.read_bytes()
    if hashlib.sha256(asset_bytes).hexdigest() != SMOKE_DATASET_SHA256:
        raise SmokeAdmissionError("SMOKE_DATASET_HASH_MISMATCH")
    bundle = json.loads(asset_bytes.decode("utf-8"))
    if bundle.get("version") != SMOKE_DATASET_VERSION or bundle.get("split") != "dev":
        raise SmokeAdmissionError("SMOKE_DATASET_VERSION_OR_SPLIT_MISMATCH")
    settings = load_settings(require_api_key=require_api_key)
    price_path = _pricing_path(profile_id)
    price, price_sha = load_price_evidence(price_path, model=settings.model, endpoint=settings.base_url)
    target = PersonalDevDatabase(ROOT)
    engine, factory = target.open()
    coordinator = D1Coordinator(target, factory)
    schema_sql = target.check_policy_event_schema(engine)
    schema_sha = hashlib.sha256(schema_sql.encode("utf-8")).hexdigest()
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    code_sha = implementation_digest(ROOT)
    return bundle, settings, target, engine, factory, coordinator, revision, code_sha, price, price_sha, schema_sha


def _load_verified_dataset() -> dict:
    asset_path = ROOT / SMOKE_DATASET_PATH
    asset_bytes = asset_path.read_bytes()
    if hashlib.sha256(asset_bytes).hexdigest() != SMOKE_DATASET_SHA256:
        raise SmokeAdmissionError("SMOKE_DATASET_HASH_MISMATCH")
    bundle = json.loads(asset_bytes.decode("utf-8"))
    if bundle.get("version") != SMOKE_DATASET_VERSION or bundle.get("split") != "dev":
        raise SmokeAdmissionError("SMOKE_DATASET_VERSION_OR_SPLIT_MISMATCH")
    return bundle


def _save_input_contract_blocked_preflight(bundle: dict, profile_id: str, findings: list[dict]) -> dict:
    profile = smoke_profile(profile_id)
    code_sha = implementation_digest(ROOT)
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    scope = ("5CASE-R2" if profile.profile_id == M62_PILOT_FIVE_R2_PROFILE_ID else
        "5CASE-R1" if profile.profile_id == M62_PILOT_FIVE_R1_PROFILE_ID else "6CASE-R2")
    preflight_id = f"M62-NONFROZEN-DEV-PILOT-{scope}-CONTRACT-BLOCKED-{uuid.uuid4().hex}"
    directory = ROOT / "docs/eval/personal-dev-smokes-preflight" / preflight_id
    budget = budget_contract(profile)
    rules = locked_rules(bundle, profile)
    manifest = {"schema_version": "m6.2-pilot-input-contract-preflight-v1",
        "preflight_id": preflight_id, "profile_id": profile.profile_id,
        "case_order": list(profile.cases), "dataset_path": SMOKE_DATASET_PATH,
        "dataset_version": bundle.get("version"), "dataset_sha256": SMOKE_DATASET_SHA256,
        "code_revision": revision, "code_sha256": code_sha,
        "ruleset_id": profile.ruleset_id, "rules_sha256": sha256_json(rules),
        "budget_sha256": sha256_json(budget), "status": "BLOCKED_BEFORE_DATABASE_OR_AUTHORIZATION",
        "database_opened": False, "authorization_created": False, "run_created": False,
        "provider_requests": 0, "fixture_refs_not_used_as_runtime_arguments": True}
    report = {"schema_version": "m6.2-pilot-input-contract-preflight-v1",
        **SMOKE_LABELS, "preflight_id": preflight_id, "profile_id": profile.profile_id,
        "status": "BLOCKED", "case_order": list(profile.cases), "cases": findings,
        "blocking_cases": [row["case_id"] for row in findings if row.get("status") != "PASS"],
        "authorization_created": False, "run_created": False, "provider_requests": 0,
        "database_opened": False, "formal_gates_changed": False}
    directory.mkdir(parents=True, exist_ok=False)
    _write_new(directory / "manifest.json", manifest)
    _write_new(directory / "budget.json", budget)
    _write_new(directory / "locked-rules.json", rules)
    _write_new(directory / "preflight.json", report)
    _write_new(directory / "state.json", {"status": "NOT_AUTHORIZED_NOT_RUN",
        "authorization_created": False, "run_count": 0, "provider_requests": 0,
        "remaining_case_ids": list(profile.cases), "blocking_cases": report["blocking_cases"]})
    with (directory / "journal.jsonl").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps({"event": "PREFLIGHT_BLOCKED_BEFORE_AUTH", "preflight_id": preflight_id,
            "code_sha256": code_sha, "blocking_cases": report["blocking_cases"]}, sort_keys=True) + "\n")
    lines = ["# M6.2 六案输入/工具契约预检：阻断", "",
        "在数据库打开、授权创建、Run创建或Provider请求前发现固定案例无法从用户输入提供所需工具参数。",
        "不使用 fixture_refs 合成工具输入，不修改案例、不扩大能力、不创建授权。", "",
        "| Case | Status | Reason | Tool / required argument |", "|---|---|---|---|"]
    for row in findings:
        lines.append(f"| {row.get('case_id')} | {row.get('status')} | {row.get('reason_code')} | {row.get('tool', '')} / {row.get('required_argument', '')} |")
    lines += ["", f"Code digest: `{code_sha}`; dataset SHA-256: `{SMOKE_DATASET_SHA256}`.",
        "Authorization created: no; database opened: no; Runs: 0; Provider requests: 0.", ""]
    with (directory / "preflight.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines))
    entries = [f"{_sha(path)}  {path.name}" for path in sorted(directory.iterdir()) if path.is_file()]
    with (directory / "SHA256SUMS.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("# M6.2 input-contract blocked preflight SHA-256\n\n" + "\n".join(entries) + "\n")
    return {"status": "SMOKE_PREFLIGHT_BLOCKED", "preflight_id": preflight_id,
        "preflight_path": directory.relative_to(ROOT).as_posix(),
        "preflight_cases": findings, "blocking_cases": report["blocking_cases"],
        "authorization_created": False, "database_opened": False,
        "real_cases_executed": 0, "provider_requests": 0}


def _all_case_preflight(bundle, coordinator, target, factory, price, settings,
        profile_id: str = SMOKE_NINE_PROFILE_ID) -> tuple[list[dict], list[dict]]:
    profile = smoke_profile(profile_id)
    caps_by_case = profile.tool_caps
    rules = locked_rules(bundle, profile)
    plans, result_rows = [], []
    cases = {row["case_id"]: row for row in bundle["cases"]}
    if [case["case_id"] for case in bundle["cases"] if case["case_id"] in profile.cases] != list(profile.cases):
        raise SmokeAdmissionError("SMOKE_DATASET_ORDER_OR_CASE_SCOPE_MISMATCH")
    for case_id in profile.cases:
        plan = plan_case(bundle, case_id)
        caps = _case_capabilities(plan["case"])
        rule_case = next(row for row in rules["cases"] if row["case_id"] == case_id)
        if caps != rule_case["allowed_read_only_capabilities"]:
            raise SmokeAdmissionError(f"SMOKE_CASE_CAPABILITY_BINDING_MISMATCH:{case_id}")
        if len(caps) > caps_by_case[case_id]:
            raise SmokeAdmissionError(f"SMOKE_TOOL_BUDGET_TOO_SMALL:{case_id}")
        limits_data = budget_contract(profile)["per_case"][case_id]
        limits = BudgetLimits(**limits_data)
        validate_execution_budget(limits, plan["message"])
        plans.append(plan)
    target.check_policy_event_schema(factory.kw["bind"])
    _check_no_existing_reservations(ROOT / "data/eval/d1-personal-budget.sqlite3")
    _check_database_empty(target, factory)
    for plan in plans:
        try:
            result_rows.append({"case_id": plan["case_id"], **_preflight_read_case(coordinator, plan, profile.profile_id)})
        except Exception as exc:
            result_rows.append({"case_id": plan["case_id"], "status": "FAIL",
                "reason_code": type(exc).__name__, "reason": str(exc),
                "business_reads": "UNKNOWN", "cleanup_status": "UNKNOWN"})
    _check_database_empty(target, factory)
    return plans, result_rows


def _manifest(bundle: dict, settings, target, code_sha: str, revision: str,
        price_sha: str, schema_sha: str, rules: dict, budget: dict,
        profile_id: str = SMOKE_NINE_PROFILE_ID) -> dict:
    profile = smoke_profile(profile_id)
    selected = {case["case_id"]: case for case in bundle["cases"]}
    manifest = {"schema_version": "personal-dev-technical-smoke-manifest-v1",
        "smoke_id": None, **SMOKE_LABELS, "dataset_path": SMOKE_DATASET_PATH,
        "dataset_version": bundle["version"], "dataset_sha256": SMOKE_DATASET_SHA256,
        "profile_id": profile.profile_id,
        "case_order": list(profile.cases),
        "cases": [{"case_id": cid, "group_id": selected[cid]["group_id"],
            "allowed_read_only_capabilities": _case_capabilities(selected[cid]),
            "tool_calls_max": profile.tool_caps[cid]} for cid in profile.cases],
        "code_sha256": code_sha, "code_revision": revision,
        "database_identity": target.identity, "database_path": target.path.relative_to(ROOT).as_posix(),
        "database_profile": "M6.1-D1-PERSONAL-DEV", "database_schema_sha256": schema_sha,
        "model": settings.model, "provider_endpoint": settings.base_url,
        "pricing_path": _pricing_path(profile_id).relative_to(ROOT).as_posix(), "pricing_sha256": price_sha,
        "budget_sha256": sha256_json(budget),
        "ruleset_id": profile.ruleset_id,
        "rules_sha256": sha256_json(rules),
        "labels": dict(SMOKE_LABELS), "preflight_reads_are_not_trial_usage": True}
    if profile.profile_id in {SMOKE_EIGHT_PROFILE_ID, SMOKE_EIGHT_R2_PROFILE_ID}:
        manifest["scope_transition"] = _scope_transition(profile.profile_id)
    runtime_binding = runtime_dependency_binding(ROOT, code_sha, profile)
    if runtime_binding is not None:
        manifest["runtime_dependency_binding"] = runtime_binding
    return manifest


def prepare_smoke(profile_id: str = SMOKE_NINE_PROFILE_ID) -> dict:
    profile = smoke_profile(profile_id)
    if profile.profile_id in {M62_PILOT_R2_PROFILE_ID, M62_PILOT_FIVE_R1_PROFILE_ID,
            M62_PILOT_FIVE_R2_PROFILE_ID}:
        bundle = _load_verified_dataset()
        findings = _case_input_tool_contracts(bundle, profile.profile_id)
        if any(row.get("status") != "PASS" for row in findings):
            return _save_input_contract_blocked_preflight(bundle, profile.profile_id, findings)
    is_single_safety_scope = profile.profile_id in {SAFETY038_R4_PROFILE_ID, SAFETY038_SCORING_PROFILE_ID}
    attempt_scope, attempt_ledger, _attempt_claim = (_safety038_attempt_binding(profile.profile_id)
        if is_single_safety_scope else (None, None, None))
    attempt_record = (_check_safety038_attempt_available(profile_id=profile.profile_id)
        if is_single_safety_scope and attempt_ledger.exists() else None)
    _scope_transition(profile.profile_id)
    bundle, settings, target, engine, factory, coordinator, revision, code_sha, price, price_sha, schema_sha = \
        _runtime_context(require_api_key=True, profile_id=profile.profile_id)
    try:
        budget = budget_contract(profile)
        rules = locked_rules(bundle, profile)
        plans, preflight = _all_case_preflight(bundle, coordinator, target, factory, price, settings, profile.profile_id)
        if len(plans) != len(profile.cases) or len(preflight) != len(profile.cases) or any(row.get("status") != "PASS" for row in preflight):
            return _save_blocked_preflight(bundle, settings, target, code_sha, revision,
                price_sha, schema_sha, rules, budget, preflight, profile.profile_id)
        smoke_id = f"{profile.directory_prefix}{uuid.uuid4().hex}"
        smoke_dir = SMOKE_PARENT / smoke_id
        manifest = _manifest(bundle, settings, target, code_sha, revision, price_sha, schema_sha, rules, budget,
            profile.profile_id)
        if is_single_safety_scope:
            if attempt_record is None:
                _attempt_ledger_rows(initialize=True, profile_id=profile.profile_id)
                attempt_record = _check_safety038_attempt_available(profile_id=profile.profile_id)
            manifest["instruction_attempt_scope"] = attempt_scope
            manifest["instruction_attempt_ledger"] = {
                "path": attempt_ledger.relative_to(ROOT).as_posix(),
                "genesis_hash": attempt_record["genesis_hash"], "max_real_runs": 1}
        manifest["smoke_id"] = smoke_id
        smoke_dir.mkdir(parents=True, exist_ok=False)
        _write_new(smoke_dir / "budget.json", budget)
        _write_new(smoke_dir / "locked-rules.json", rules)
        _write_new(smoke_dir / "manifest.json", manifest)
        confirmed = datetime.now(timezone.utc)
        expires = confirmed + timedelta(hours=4)
        auth = {**SMOKE_LABELS, "schema_version": "1.0", "authorization_type": SMOKE_AUTHORIZATION_TYPE,
            "authorization_id": smoke_id, "status": "USER_CONFIRMED",
            "authorized_by": "CHAT_USER_SELF_CONFIRMED", "actor_type": "CHAT_USER_SELF_CONFIRMED",
            "confirmation_source": f"User instruction authorizing the exact {len(profile.cases)}-case {profile.profile_id} personal technical Smoke.",
            "confirmed_at_utc": confirmed.isoformat().replace("+00:00", "Z"),
            "expires_at_utc": expires.isoformat().replace("+00:00", "Z"),
            "case_ids": list(profile.cases), "dataset_sha256": SMOKE_DATASET_SHA256,
            "code_sha256": code_sha, "code_revision": revision, "database_identity": target.identity,
            "database_schema_sha256": schema_sha, "model": settings.model, "provider_endpoint": settings.base_url,
            "pricing_sha256": price_sha,
            "budget_sha256": sha256_json(budget),
            "rules_sha256": sha256_json(rules),
            "manifest_sha256": _sha(smoke_dir / "manifest.json"),
            "budget_file_sha256": _sha(smoke_dir / "budget.json"),
            "protected_assets_allowed": False, "formal_gates_changed": False}
        auth["profile_id"] = profile.profile_id
        if manifest.get("instruction_attempt_ledger") is not None:
            auth["instruction_attempt_scope"] = manifest["instruction_attempt_scope"]
            auth["instruction_attempt_ledger"] = manifest["instruction_attempt_ledger"]
        if manifest.get("runtime_dependency_binding") is not None:
            auth["runtime_dependency_binding"] = manifest["runtime_dependency_binding"]
        _write_new(smoke_dir / "authorization.json", auth)
        return {"status": "SMOKE_PREPARED_AND_AUTHORIZED", "smoke_dir": smoke_dir.relative_to(ROOT).as_posix(),
            "authorization_id": smoke_id, "code_sha256": code_sha, "dataset_sha256": SMOKE_DATASET_SHA256,
            "database_identity": target.identity, "model": settings.model,
            "provider_endpoint": settings.base_url, "pricing_sha256": price_sha,
            "preflight_cases": preflight, "fixture_preflight_reads_executed": sum(row.get("business_reads", 0) for row in preflight),
            "real_cases_executed": 0, "provider_requests": 0}
    finally:
        engine.dispose()


def _save_blocked_preflight(bundle: dict, settings, target, code_sha: str, revision: str,
        price_sha: str, schema_sha: str, rules: dict, budget: dict, preflight: list[dict],
        profile_id: str = SMOKE_NINE_PROFILE_ID) -> dict:
    profile = smoke_profile(profile_id)
    scope_tag = "-8CASE-R2" if profile.profile_id == SMOKE_EIGHT_R2_PROFILE_ID else (
        "-8CASE" if profile.profile_id == SMOKE_EIGHT_PROFILE_ID else
        "-5CASE-R2" if profile.profile_id == M62_PILOT_FIVE_R2_PROFILE_ID else
        "-5CASE-R1" if profile.profile_id == M62_PILOT_FIVE_R1_PROFILE_ID else "")
    preflight_id = f"M61D1-SMOKE-PREFLIGHT-BLOCKED{scope_tag}-" + uuid.uuid4().hex
    directory = ROOT / "docs/eval/personal-dev-smokes-preflight" / preflight_id
    base_manifest = _manifest(bundle, settings, target, code_sha, revision,
        price_sha, schema_sha, rules, budget, profile.profile_id)
    base_manifest.update({"preflight_id": preflight_id, "authorization_created": False,
        "real_cases_executed": 0, "provider_requests": 0})
    failures = [row for row in preflight if row.get("status") != "PASS"]
    report = {"schema_version": "personal-dev-technical-smoke-preflight-v1",
        "preflight_id": preflight_id, **SMOKE_LABELS, "status": "BLOCKED",
        "case_order": list(profile.cases), "cases": preflight, "failure_count": len(failures),
        "blocking_cases": [row["case_id"] for row in failures],
        "authorization_created": False, "authorization_activated": False,
        "run_created": False, "provider_requests": 0, "business_writes": 0,
        "formal_gates_changed": False, "dataset_sha256": SMOKE_DATASET_SHA256,
        "code_sha256": code_sha, "database_identity": target.identity,
        "pricing_sha256": price_sha, "ruleset_id": profile.ruleset_id,
        "profile_id": profile.profile_id}
    directory.mkdir(parents=True, exist_ok=False)
    _write_new(directory / "manifest.json", base_manifest)
    _write_new(directory / "budget.json", budget)
    _write_new(directory / "locked-rules.json", rules)
    _write_new(directory / "preflight.json", report)
    lines = [f"# {len(profile.cases)}-case personal Dev technical Smoke preflight", "",
        f"- Status: BLOCKED; authorization created: no; real runs: 0; provider requests: 0.",
        f"- Dataset SHA-256: {SMOKE_DATASET_SHA256}; code digest: {code_sha}; database identity: {target.identity}.",
        "- Classification: PERSONAL_DEV_TECHNICAL_SMOKE / PROVISIONAL_DEV_CHECKS / reference_unreviewed / NOT_GRADED.",
        "", "| Case | Preflight | Business reads | Fixture / application evidence |", "|---|---|---:|---|"]
    for row in preflight:
        lines.append(f"| {row.get('case_id')} | {row.get('status')} | {row.get('business_reads')} | {row.get('reason', 'all configured source checks completed')} |")
    lines += ["", "No authorization, Run, model request, or Smoke execution was created.",
        "Historical Smoke preparation evidence is not a substitute for this fixed configuration.", ""]
    with (directory / "preflight.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines))
    entries = [f"{_sha(path)}  {path.name}" for path in sorted(directory.iterdir())
        if path.is_file()]
    with (directory / "SHA256SUMS.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("# Blocked preflight SHA-256\n\n" + "\n".join(entries) + "\n")
    return {"status": "SMOKE_PREFLIGHT_BLOCKED", "preflight_id": preflight_id,
        "preflight_path": directory.relative_to(ROOT).as_posix(),
        "preflight_cases": preflight, "blocking_cases": report["blocking_cases"],
        "authorization_created": False, "real_cases_executed": 0, "provider_requests": 0}


def _load_and_validate(smoke_dir: Path, *, require_api_key: bool, check_state: bool,
        profile_id: str | None = None):
    smoke_dir = smoke_dir.resolve(strict=True)
    manifest = _read_json(smoke_dir / "manifest.json")
    selected_profile = smoke_profile(profile_id or manifest.get("profile_id"))
    bundle, settings, target, engine, factory, coordinator, revision, code_sha, price, price_sha, schema_sha = \
        _runtime_context(require_api_key=require_api_key, profile_id=selected_profile.profile_id)
    try:
        if manifest.get("profile_id", SMOKE_NINE_PROFILE_ID) != selected_profile.profile_id:
            raise SmokeAdmissionError("SMOKE_PROFILE_PATH_BINDING_MISMATCH")
        if manifest.get("scope_transition") != _scope_transition(selected_profile.profile_id):
            raise SmokeAdmissionError("SMOKE_SCOPE_TRANSITION_BINDING_MISMATCH")
        budget = _read_json(smoke_dir / "budget.json")
        rules = _read_json(smoke_dir / "locked-rules.json")
        auth = _read_json(smoke_dir / "authorization.json")
        if selected_profile.profile_id in {SAFETY038_R4_PROFILE_ID, SAFETY038_SCORING_PROFILE_ID}:
            attempt_scope, attempt_ledger, _attempt_claim = _safety038_attempt_binding(selected_profile.profile_id)
            attempt = _check_safety038_attempt_available(profile_id=selected_profile.profile_id)
            binding = manifest.get("instruction_attempt_ledger")
            if (manifest.get("instruction_attempt_scope") != attempt_scope or
                    auth.get("instruction_attempt_scope") != attempt_scope):
                raise SmokeAdmissionError("SAFETY038_ATTEMPT_SCOPE_BINDING_MISMATCH")
            if (not isinstance(binding, dict) or binding != auth.get("instruction_attempt_ledger") or
                    binding.get("path") != attempt_ledger.relative_to(ROOT).as_posix() or
                    binding.get("genesis_hash") != attempt["genesis_hash"] or binding.get("max_real_runs") != 1):
                raise SmokeAdmissionError("SAFETY038_ATTEMPT_LEDGER_BINDING_MISMATCH")
        validate_smoke_bindings(root=ROOT, smoke_dir=smoke_dir, manifest=manifest, budget=budget,
            rules=rules, authorization=auth, model=settings.model, endpoint=settings.base_url,
            database_identity=target.identity, code_sha256=code_sha, code_revision=revision,
            pricing_sha256=price_sha, schema_sha256=schema_sha, profile_id=selected_profile.profile_id)
        if check_state and (smoke_dir / "state.json").exists():
            raise SmokeAdmissionError("SMOKE_AUTHORIZATION_ALREADY_CONSUMED")
        if auth.get("status") != "USER_CONFIRMED":
            raise SmokeAdmissionError("SMOKE_AUTHORIZATION_NOT_UNUSED")
        if manifest.get("smoke_id") != smoke_dir.name:
            raise SmokeAdmissionError("SMOKE_ID_PATH_MISMATCH")
        plans, preflight = _all_case_preflight(bundle, coordinator, target, factory, price, settings,
            selected_profile.profile_id)
        return (bundle, settings, target, engine, factory, coordinator, revision, code_sha, price,
            price_sha, schema_sha, manifest, budget, rules, auth, plans, preflight)
    except Exception:
        engine.dispose()
        raise


def check_admission(smoke_dir: Path, profile_id: str | None = None) -> dict:
    values = _load_and_validate(smoke_dir, require_api_key=True, check_state=True, profile_id=profile_id)
    try:
        plans, preflight = values[-2], values[-1]
        return {"status": "PERSONAL_DEV_TECHNICAL_SMOKE_ADMISSION_PASSED",
            "case_ids": [plan["case_id"] for plan in plans], "preflight": preflight,
            "preflight_reads_counted_as_execution": False, "agent_started": False,
            "run_created": False, "provider_requests": 0, "grading_status": "NOT_GRADED"}
    finally:
        values[3].dispose()


def _claim_smoke(smoke_dir: Path, auth: dict, profile_id: str = SMOKE_NINE_PROFILE_ID,
        manifest: dict | None = None) -> dict:
    profile = smoke_profile(profile_id)
    if profile.runtime_binding_version:
        current_base_code_sha = implementation_digest(ROOT)
        current_binding = runtime_dependency_binding(ROOT, current_base_code_sha, profile)
        if (manifest is None or current_base_code_sha != auth.get("code_sha256") or
                current_base_code_sha != (manifest or {}).get("code_sha256") or
                auth.get("runtime_dependency_binding") != current_binding or
                manifest.get("runtime_dependency_binding") != current_binding):
            raise SmokeAdmissionError("SMOKE_RUNTIME_DEPENDENCY_BINDING_MISMATCH_AT_CLAIM")
    state_path = smoke_dir / "state.json"
    state = {"schema_version": "personal-dev-smoke-state-v1", "authorization_id": smoke_dir.name,
        "profile_id": profile.profile_id, "case_ids": list(profile.cases), "status": "IN_USE",
        "claimed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "completed_case_ids": [], "remaining_case_ids": list(profile.cases)}
    _write_new(state_path, state)
    auth["status"] = "IN_USE"
    auth["execution_started_at_utc"] = state["claimed_at_utc"]
    _write_json(smoke_dir / "authorization.json", auth)
    return state


def _close_smoke(smoke_dir: Path, auth: dict, state: dict, *, status: str, outcome: dict) -> None:
    closed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    auth["status"] = status
    auth["closed_at_utc"] = closed_at
    auth["execution_outcome"] = outcome
    _write_json(smoke_dir / "authorization.json", auth)
    state["status"] = status
    state["closed_at_utc"] = closed_at
    state["execution_outcome"] = outcome
    _write_json(smoke_dir / "state.json", state)


def _save_case_reports(smoke_dir: Path, case_id: str, plan: dict, archive_path: Path,
        budget_snapshot: dict, cleanup: dict, rules: dict,
        execution_fault_type: str | None = None) -> tuple[dict, dict]:
    from eval.m6_1.personal_smoke import evaluate_smoke_case
    raw = archive_path.read_bytes()
    archive = json.loads(raw.decode("utf-8"))
    if execution_fault_type:
        archive["recovery"] = {"classification": "EXECUTION_EXCEPTION",
            "error_type": execution_fault_type, "snapshot_source": "PERSISTED_RUN_ARCHIVE"}
    first_error = None
    try:
        first = evaluate_smoke_case(case_id=case_id, plan=plan, archive=archive,
            budget_snapshot=budget_snapshot, cleanup=cleanup, rules=rules)
    except Exception as exc:
        first_error = type(exc).__name__
        first = {"schema_version": "personal-dev-technical-smoke-check-result-v2",
            "ruleset_id": rules.get("ruleset_id"), "case_id": case_id,
            "decision": "STOP", "blocking_items": ["checker_integrity:FAIL"],
            "review_items": [], "task_success": "UNKNOWN_NOT_GRADED",
            "provider_billing": "UNKNOWN", "checks": {"checker_integrity_checks": {
                "status": "FAIL", "blocking": True, "error_type": first_error}}}
    # Re-read the immutable raw archive and independently recompute from the same frozen inputs.
    second_error = None
    try:
        second_archive = json.loads(archive_path.read_bytes().decode("utf-8"))
        if execution_fault_type:
            second_archive["recovery"] = {"classification": "EXECUTION_EXCEPTION",
                "error_type": execution_fault_type, "snapshot_source": "PERSISTED_RUN_ARCHIVE"}
        second = evaluate_smoke_case(case_id=case_id, plan=plan, archive=second_archive,
            budget_snapshot=json.loads(json.dumps(budget_snapshot)),
            cleanup=json.loads(json.dumps(cleanup)), rules=json.loads(json.dumps(rules)))
    except Exception as exc:
        second_error = type(exc).__name__
        second = {"schema_version": "personal-dev-technical-smoke-check-result-v2",
            "ruleset_id": rules.get("ruleset_id"), "case_id": case_id,
            "decision": "STOP", "blocking_items": ["checker_integrity:FAIL"],
            "review_items": [], "task_success": "UNKNOWN_NOT_GRADED",
            "provider_billing": "UNKNOWN", "checks": {"checker_integrity_checks": {
                "status": "FAIL", "blocking": True, "error_type": second_error}}}
    matched = first_error is None and second_error is None and first == second
    payload = {"schema_version": "personal-dev-technical-smoke-case-report-v1",
        "case_id": case_id, "group_id": plan["case"]["group_id"],
        **{key: SMOKE_LABELS[key] for key in ("classification", "reference_review_status", "grading_status")},
        "ruleset_id": rules["ruleset_id"], "execution_status": "EXECUTED",
        "archive_path": archive_path.relative_to(ROOT).as_posix(),
        "archive_sha256": hashlib.sha256(raw).hexdigest(),
        "execution_fault_type": execution_fault_type,
        "run_id": archive.get("result", {}).get("run_id"),
        "run_status": archive.get("result", {}).get("status"),
        "budget_snapshot": budget_snapshot, "cleanup": cleanup, "checks": first,
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")}
    payload["checks"]["independent_recompute"] = {
        "status": "PASS" if matched else "FAIL", "blocking": True,
        "first_check_error_type": first_error, "recompute_error_type": second_error,
        "source_archive_sha256": hashlib.sha256(raw).hexdigest(),
        "execution_check_sha256": hashlib.sha256(_json_bytes(first)).hexdigest(),
        "recomputed_check_sha256": hashlib.sha256(_json_bytes(second)).hexdigest()}
    return payload, second


def _closeout_docs(smoke_dir: Path, manifest: dict, budget: dict, state: dict,
        records: list[dict], pricing_sha: str, test_junit: str | None = None,
        profile_id: str = SMOKE_NINE_PROFILE_ID, batch_budget_snapshot: dict | None = None) -> dict:
    profile = smoke_profile(profile_id)
    from collections import Counter
    executed = [row for row in records if row.get("execution_status") in {"EXECUTED", "EXECUTION_ERROR"}]
    not_run = [row["case_id"] for row in records if row.get("execution_status") == "NOT_RUN"]
    counts = Counter(row.get("execution_status") for row in records)
    technical_pass = sum(row.get("technical_status") == "PASS" for row in executed)
    technical_fail = sum(row.get("technical_status") in {"FAIL", "UNKNOWN"} for row in executed)
    quality_defects = sum(row.get("quality_status") == "FAIL" for row in executed)
    code_outcome_failures = sum(row.get("code_outcome_status") == "FAIL" for row in executed)
    code_outcome_unknown = sum(row.get("code_outcome_status") == "UNKNOWN" for row in executed)
    review_required = sum(row.get("semantic_review_status") == "UNKNOWN" for row in executed)
    unknown_count = sum(row.get("unknown_count", 0) for row in executed)
    failure_categories = Counter(row.get("failure_classification", "UNKNOWN") for row in executed)
    def total(field):
        values = [row.get(field) for row in executed]
        return "UNKNOWN" if any(value is None for value in values) else sum(values)
    usd = total("estimated_usd")
    totals = {"provider_requests": total("provider_calls"),
        "read_only_calls": total("read_only_calls"),
        "tokens": total("tokens"),
        "estimated_usd": round(usd, 8) if isinstance(usd, (int, float)) else usd,
        "supplier_billing": "UNKNOWN"}
    if isinstance(batch_budget_snapshot, dict):
        ledger_calls = batch_budget_snapshot.get("calls_by_category")
        ledger_complete = (batch_budget_snapshot.get("complete") is True and
            batch_budget_snapshot.get("settled") is True and
            batch_budget_snapshot.get("active_reservations") == 0 and
            not batch_budget_snapshot.get("integrity_errors") and isinstance(ledger_calls, dict))
        if ledger_complete:
            provider_status = batch_budget_snapshot.get("calls_status")
            totals["provider_requests"] = (ledger_calls.get("provider") if provider_status in {"RECORDED", "NOT_APPLICABLE_NO_EXTERNAL_REQUESTS"}
                else "UNKNOWN")
            totals["provider_requests_conservative_upper_bound"] = (
                batch_budget_snapshot.get("conservative_calls_by_category", {}).get("provider")
                if provider_status == "CONSERVATIVE_UPPER_BOUND" else None)
            totals["read_only_calls"] = ledger_calls.get("tool")
            totals["tokens"] = (batch_budget_snapshot.get("tokens")
                if batch_budget_snapshot.get("token_status") != "UNKNOWN_USAGE" else "UNKNOWN")
            cost = batch_budget_snapshot.get("estimated_usd")
            totals["estimated_usd"] = (round(cost, 8) if isinstance(cost, (int, float)) and
                batch_budget_snapshot.get("estimate_status") not in {"UNKNOWN_USAGE", "RESERVED_CONSERVATIVE"}
                else "UNKNOWN")
        totals["ledger_snapshot_complete"] = batch_budget_snapshot.get("complete") is True
        totals["ledger_snapshot_settled"] = batch_budget_snapshot.get("settled") is True
        totals["ledger_active_reservations"] = batch_budget_snapshot.get("active_reservations")
        totals["ledger_integrity_errors"] = batch_budget_snapshot.get("integrity_errors", [])
    complete = len(executed) == len(profile.cases) and not not_run and technical_fail == 0
    report = {"schema_version": "personal-dev-technical-smoke-closeout-v1",
        "smoke_id": smoke_dir.name, **SMOKE_LABELS,
        "status": "SMOKE_COMPLETED" if complete else "SMOKE_STOPPED",
        "started_at_utc": state.get("claimed_at_utc"),
        "ended_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "manifest_sha256": _sha(smoke_dir / "manifest.json"), "dataset_sha256": SMOKE_DATASET_SHA256,
        "code_sha256": manifest["code_sha256"], "code_revision": manifest["code_revision"],
        "database_identity": manifest["database_identity"],
        "database_schema_sha256": manifest["database_schema_sha256"],
        "pricing_sha256": pricing_sha, "ruleset_id": profile.ruleset_id,
        "profile_id": profile.profile_id,
        "requested_case_count": len(profile.cases), "executed_case_count": len(executed),
        "technical_pass_count": technical_pass, "technical_fail_count": technical_fail,
        "quality_defect_count": quality_defects,
        "provisional_code_outcome_fail_count": code_outcome_failures,
        "provisional_code_outcome_unknown_count": code_outcome_unknown,
        "semantic_review_required_count": review_required,
        "unknown_count": unknown_count, "not_run_count": len(not_run), "not_run_case_ids": not_run,
        "failure_classification_version": profile.failure_classification_version or SMOKE_FAILURE_CLASSIFICATION_VERSION,
        "failure_classification_counts": dict(sorted(failure_categories.items())),
        "execution_status_counts": dict(sorted(counts.items())), "budget_totals": totals,
        "batch_budget_snapshot": batch_budget_snapshot,
        "per_case": records, "task_success": "UNKNOWN_NOT_GRADED",
        "formal_gate_changes": False, "historical_runs_combined_as_smoke": False,
        "test_junit_path": test_junit,
        "scope_transition": manifest.get("scope_transition"),
        "gates": {"Gold": "NOT_APPROVED", "Registry": "NOT_AUTHORIZED", "M2": "BLOCKED",
            "Dev_Smoke": "NOT_AUTHORIZED", "Frozen_Test": "NOT_AUTHORIZED"},
        "external_effects": {"embedding_requests": 0, "judge_requests": 0,
            "business_writes": 0, "pending_actions": 0,
            "run_count": (sum(row.get("run_count", 0) for row in executed)
                if all(isinstance(row.get("run_count", 0), int) and not isinstance(row.get("run_count", 0), bool)
                    for row in executed) else "UNKNOWN"),
            "supplier_billing": "UNKNOWN"}}
    if manifest.get("runtime_dependency_binding") is not None:
        report["runtime_dependency_binding"] = manifest["runtime_dependency_binding"]
    _write_new(smoke_dir / "reports/closeout.json", report)
    md = [f"# D1 {len(profile.cases)}案个人 Dev 技术 Smoke 收口", "",
        f"- 批次：{smoke_dir.name}；状态：{report['status']}。",
        "- 分类：PERSONAL_DEV_TECHNICAL_SMOKE / PROVISIONAL_DEV_CHECKS / reference_unreviewed / NOT_GRADED。",
        f"- 数据 SHA-256：{SMOKE_DATASET_SHA256}；实现摘要：{manifest['code_sha256']}；数据库 identity：{manifest['database_identity']}。",
        f"- 执行 {len(executed)}/{len(profile.cases)}；技术 PASS {technical_pass}；技术 FAIL {technical_fail}；正式质量分仍未建立；确定性 Outcome FAIL {code_outcome_failures}、UNKNOWN {code_outcome_unknown}；语义待复核 {review_required}；UNKNOWN {unknown_count}；NOT_RUN {len(not_run)}。",
        f"- 失败分类版本 {profile.failure_classification_version or SMOKE_FAILURE_CLASSIFICATION_VERSION}：{json.dumps(dict(sorted(failure_categories.items())), ensure_ascii=False, sort_keys=True)}。",
        f"- Provider {totals['provider_requests']}；只读调用 {totals['read_only_calls']}；tokens {totals['tokens']}；本地估算 USD {totals['estimated_usd']}；供应商账单 UNKNOWN。预算不是供应商硬消费封顶。", "",
        "| case | execution | run | decision | cleanup | provider/tool/tokens |", "|---|---|---|---|---|---|"]
    for row in records:
        md.append(f"| {row['case_id']} | {row.get('execution_status')} | {row.get('run_status','UNKNOWN')} | {row.get('diagnostic_decision','UNKNOWN')} | {row.get('cleanup_status','UNKNOWN')} | {row.get('provider_calls','UNKNOWN')}/{row.get('read_only_calls','UNKNOWN')}/{row.get('tokens','UNKNOWN')} |")
    md += ["", "- Run 成功仅表示生命周期终态；任务保持 NOT_GRADED，语义 UNKNOWN 不等于通过。",
        "- 本组为路径覆盖目的性样本，不支持统计泛化或策略优劣结论；历史 Run 不拼接成本轮 Smoke。",
        "- Gold NOT_APPROVED、Registry NOT_AUTHORIZED、M2 BLOCKED、Dev Smoke NOT_AUTHORIZED、Frozen Test NOT_AUTHORIZED。", ""]
    if manifest.get("scope_transition"):
        md[-1:-1] = ["- 原九案预检为 8 PASS/1 阻塞，原九案全部 NOT_RUN；CROSSAGENT-029 本轮暂缓且不在八案分母内。本批不覆盖跨 Agent 商品/库存/政策组合。", ""]
    with (smoke_dir / "reports/closeout.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(md))
    return report


def _case_budget_snapshot(ledger, case_id: str) -> dict:
    snapshot = ledger.trial_snapshot(case_id)
    calls = snapshot.get("calls_by_category")
    complete_final = (snapshot.get("complete") is True and snapshot.get("settled") is True and
        snapshot.get("snapshot_kind") == "FINAL" and snapshot.get("active_reservations") == 0 and
        not snapshot.get("integrity_errors"))
    if isinstance(calls, dict) and complete_final:
        # Missing zero categories are meaningful only in a complete, settled final snapshot.
        snapshot["calls_by_category"] = {name: calls.get(name, 0)
            for name in ("provider", "tool", "embedding", "judge")}
    snapshot["unknown_tokens"] = snapshot.get("unknown_tokens", snapshot.get("token_status") == "UNKNOWN_USAGE")
    snapshot["unknown_cost"] = snapshot.get("unknown_cost", snapshot.get("estimate_status") == "UNKNOWN_USAGE")
    return snapshot


def _case_requires_stop(checks: dict, execution_error: str | None, cleanup: dict) -> bool:
    return (checks.get("decision") == "STOP" or execution_error is not None or
        cleanup.get("cleanup_status") != "CLEANED_AND_RELEASED")


def _archive_has_forbidden_activity(archive: dict, plan: dict) -> tuple[bool, list[str]]:
    violations = archive_activity_violations(archive, plan)
    return bool(violations), violations


def _case_summary(case_id: str, archive: dict, checks: dict, budget_snapshot: dict,
        cleanup: dict, elapsed: float) -> dict:
    outer = archive.get("result", {})
    app = outer.get("result", {}) if isinstance(outer, dict) else {}
    events = outer.get("observations", {}).get("run_events", []) if isinstance(outer, dict) else []
    runs = outer.get("observations", {}).get("runs", []) if isinstance(outer, dict) else []
    calls = budget_snapshot.get("calls_by_category", {})
    tokens = budget_snapshot.get("tokens")
    estimate = budget_snapshot.get("estimated_usd")
    return {"case_id": case_id, "group_id": archive.get("result", {}).get("case", {}).get("group_id"),
        "execution_status": "EXECUTED", "run_id": outer.get("run_id"),
        "run_status": outer.get("status", "UNKNOWN"), "diagnostic_decision": checks.get("decision", "UNKNOWN"),
        "technical_status": "FAIL" if checks.get("decision") == "STOP" else "PASS",
        "quality_status": "NOT_ASSESSED",
        "code_outcome_status": checks.get("checks", {}).get("code_outcome_checks", {}).get("status", "UNKNOWN"),
        "output_presence_status": checks.get("checks", {}).get("output_presence_checks", {}).get("status", "UNKNOWN"),
        "semantic_review_status": checks.get("checks", {}).get("semantic_review_required", {}).get("status", "UNKNOWN"),
        "unknown_count": sum(value.get("status") == "UNKNOWN" for value in checks.get("checks", {}).values()
            if isinstance(value, dict)),
        "run_count": len(runs) if isinstance(runs, list) else "UNKNOWN",
        "provider_calls": calls.get("provider"), "read_only_calls": calls.get("tool"),
        "tokens": tokens, "estimated_usd": estimate,
        "token_status": "UNKNOWN" if budget_snapshot.get("unknown_tokens") else budget_snapshot.get("token_status", "RECORDED"),
        "estimate_status": "UNKNOWN" if budget_snapshot.get("unknown_cost") else budget_snapshot.get("estimate_status", "RECORDED"),
        "supplier_billing": budget_snapshot.get("billed_status", "UNKNOWN"),
        "wall_duration_seconds": round(elapsed, 3),
        "agent_started": any(row.get("event_type") == "agent_started" for row in events if isinstance(row, dict)),
        "tool_events": sum(row.get("event_type") == "tool_started" for row in events if isinstance(row, dict)),
        "cleanup_status": cleanup.get("cleanup_status", "UNKNOWN"),
        "blocking_items": checks.get("blocking_items", []),
        "review_items": checks.get("review_items", []),
        "failure_classification": checks.get("checks", {}).get("failure_classification", {}).get("category", "UNKNOWN"),
        "failure_classification_version": checks.get("checks", {}).get("failure_classification", {}).get("classification_version"),
        "failure_classification_components": checks.get("checks", {}).get("failure_classification", {}).get("components", {}),
        "task_success": "UNKNOWN_NOT_GRADED", "grading_status": "NOT_GRADED"}


def execute_smoke(smoke_dir: Path, profile_id: str | None = None) -> dict:
    values = _load_and_validate(smoke_dir, require_api_key=True, check_state=True, profile_id=profile_id)
    (bundle, settings, target, engine, factory, coordinator, revision, code_sha, price,
        price_sha, schema_sha, manifest, budget, rules, auth, plans, preflight) = values
    smoke_dir = smoke_dir.resolve(strict=True)
    profile = smoke_profile(manifest.get("profile_id"))
    records: list[dict] = []
    stopped = False
    try:
        # The database and every fixture scope were verified before the auth was consumed.
        from eval.m6_1.budget import BudgetLimits
        from eval.m6_1.personal_dev_runtime import PerCaseBudgetLedger
        from eval.m6_1.personal_dev import create_budget_ledger
        batch_cfg = budget["batch"]
        batch_limits = BudgetLimits(provider_calls=batch_cfg["provider_calls"],
            tool_calls=batch_cfg["tool_calls"], total_tokens=batch_cfg["total_tokens"],
            estimated_usd=batch_cfg["estimated_usd"], max_calls_per_trial=batch_cfg["max_calls_per_trial"],
            max_concurrent_trials=batch_cfg["max_concurrent_trials"],
            max_duration_seconds=batch_cfg["max_duration_seconds"], max_retries=batch_cfg["max_retries"],
            embedding_calls=batch_cfg["embedding_calls"], judge_calls=batch_cfg["judge_calls"])
        if profile.profile_id in {SAFETY038_R4_PROFILE_ID, SAFETY038_SCORING_PROFILE_ID}:
            _check_safety038_attempt_available(claim=True, smoke_id=smoke_dir.name, manifest=manifest,
                profile_id=profile.profile_id)
        state = _claim_smoke(smoke_dir, auth, profile.profile_id, manifest=manifest)
        (smoke_dir / "reports").mkdir(exist_ok=False)
        _append_jsonl(smoke_dir / "journal.jsonl", {"event": "SMOKE_STARTED",
            "smoke_id": smoke_dir.name, "profile_id": profile.profile_id,
            "case_order": list(profile.cases),
            "started_at_utc": state["claimed_at_utc"], "proxy": configure_proxy()}, create=True)
        permit = {"authorization_id": smoke_dir.name, "authorization_type": SMOKE_AUTHORIZATION_TYPE,
            "asset_sha256": SMOKE_DATASET_SHA256, "case_ids": list(profile.cases),
            "data_version": SMOKE_DATASET_VERSION, "code_sha256": code_sha,
            "git_commit": revision, "database_identity": target.identity,
            "allowed_tool_capabilities": sorted({cap for row in rules["cases"]
                for cap in row["allowed_read_only_capabilities"]})}
        ledger = create_budget_ledger(target, permit, batch_limits, settings)
        plans_by_case = {plan["case_id"]: plan for plan in plans}
        for index, case_id in enumerate(profile.cases):
            plan = plans_by_case[case_id]
            limits = BudgetLimits(**budget["per_case"][case_id])
            case_ledger = PerCaseBudgetLedger(ledger, case_id, limits)
            started = time.monotonic()
            _append_jsonl(smoke_dir / "journal.jsonl", {"event": "CASE_STARTED", "case_id": case_id,
                "started_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")})
            execution = None
            receipt = None
            evidence_path = None
            archive_path = None
            archive = None
            failure_phase = None
            execution_error = None
            active = {"run_created": False, "run_status": "UNKNOWN", "lease_active": False,
                "agent_started": False, "run_count": 0}
            try:
                failure_phase = "RUNTIME_BINDING"
                if profile.runtime_binding_version:
                    current_base_code_sha = implementation_digest(ROOT)
                    current_binding = runtime_dependency_binding(ROOT, current_base_code_sha, profile)
                    if (current_base_code_sha != code_sha or
                            manifest.get("runtime_dependency_binding") != current_binding or
                            auth.get("runtime_dependency_binding") != current_binding):
                        raise D1PreflightError("SMOKE_RUNTIME_DEPENDENCY_BINDING_MISMATCH_BEFORE_CASE")
                failure_phase = "BUDGET_VALIDATION"
                validate_execution_budget(limits, plan["message"])
                failure_phase = "APPLICATION_EXECUTION"
                execution = coordinator._execute_validated_plan(plan=plan, case_id=case_id,
                    ledger=case_ledger, settings=settings, defer_cleanup=True)
                receipt = execution.pop("_diagnostic_receipt", None)
                evidence_path = execution.get("evidence_path")
                if not evidence_path:
                    raise D1PreflightError("EXECUTION_ARCHIVE_PATH_MISSING")
                archive_path = ROOT / evidence_path
                failure_phase = "ARCHIVE_READ"
                archive = _read_json(archive_path)
                if not receipt:
                    raise D1PreflightError("EXECUTION_RECEIPT_MISSING")
                if profile.runtime_binding_version:
                    current_base_code_sha = implementation_digest(ROOT)
                    current_binding = runtime_dependency_binding(ROOT, current_base_code_sha, profile)
                    if (current_base_code_sha != code_sha or
                            manifest.get("runtime_dependency_binding") != current_binding or
                            auth.get("runtime_dependency_binding") != current_binding):
                        raise D1PreflightError("SMOKE_RUNTIME_DEPENDENCY_BINDING_CHANGED_DURING_CASE")
            except Exception as exc:
                execution_error = type(exc).__name__
                if failure_phase is None:
                    failure_phase = "CASE_PIPELINE"
            if execution_error:
                try:
                    active = coordinator.inspect_active_run_evidence()
                except Exception as exc:
                    active = {"run_created": False, "run_status": "UNKNOWN", "lease_active": "UNKNOWN",
                        "agent_started": False, "run_count": "UNKNOWN", "inspection_error_type": type(exc).__name__}
                if (receipt is None or evidence_path is None) and failure_phase in {"APPLICATION_EXECUTION", "ARCHIVE_READ"}:
                    try:
                        recovered = coordinator.capture_active_run_after_exception(case_id=case_id,
                            user_query=plan["message"], error_type=execution_error)
                        if recovered.get("captured"):
                            receipt = recovered.get("receipt")
                            evidence_path = recovered.get("evidence_path")
                            active.update({key: recovered.get(key) for key in
                                ("run_id", "run_status", "agent_started", "run_count", "database_identity")})
                    except Exception as exc:
                        active["recovery_error_type"] = type(exc).__name__
            cleanup_safety = {"safe_to_cleanup": False, "reason": "RECEIPT_OR_ARCHIVE_MISSING"}
            cleanup_error_type = None
            cleaned = False
            if receipt is not None and evidence_path:
                archive_path = ROOT / evidence_path
                try:
                    archive = _read_json(archive_path)
                    cleanup_safety = coordinator.diagnostic_cleanup_safety(receipt, archive_path)
                except Exception as exc:
                    cleanup_safety = {"safe_to_cleanup": False, "reason": "ARCHIVE_OR_RECEIPT_CHECK_FAILED",
                        "error_type": type(exc).__name__}
                if cleanup_safety.get("safe_to_cleanup") is True:
                    try:
                        coordinator.cleanup(receipt)
                        cleaned = True
                    except Exception as exc:
                        cleanup_error_type = type(exc).__name__
                try:
                    cleanup = coordinator.diagnostic_cleanup_evidence(receipt, cleaned=cleaned)
                except Exception as exc:
                    cleanup = {"cleanup_status": "UNKNOWN", "lease_released": False,
                        "pending_actions": "UNKNOWN", "residual_case_rows": "UNKNOWN",
                        "evidence_error_type": type(exc).__name__}
            elif failure_phase == "RUNTIME_BINDING":
                cleanup = {"cleanup_status": "NOT_APPLICABLE_NO_RUN", "lease_released": True,
                    "pending_actions": 0, "residual_case_rows": 0}
            else:
                cleanup = {"cleanup_status": "RETAINED_UNKNOWN_OR_UNSAFE", "lease_released": False,
                    "pending_actions": "UNKNOWN", "residual_case_rows": "UNKNOWN"}
            if cleanup_safety.get("safe_to_cleanup") is not True and receipt is not None:
                cleanup["cleanup_status"] = "RETAINED_UNSAFE_STATE"
                cleanup["cleanup_reason"] = cleanup_safety.get("reason")
            elif cleanup_error_type:
                cleanup["cleanup_status"] = "CLEANUP_ERROR"
                cleanup["cleanup_error_type"] = cleanup_error_type
            try:
                snapshot = _case_budget_snapshot(ledger, case_id)
            except Exception as exc:
                snapshot = {"snapshot_kind": "UNKNOWN", "complete": False, "settled": False,
                    "active_reservations": "UNKNOWN", "integrity_errors": ["CASE_BUDGET_SNAPSHOT_FAILED"],
                    "calls_by_category": None, "tokens": None, "estimated_usd": None,
                    "unknown_tokens": True, "unknown_cost": True,
                    "snapshot_error_type": type(exc).__name__}
            report_path = smoke_dir / f"reports/{case_id}.json"
            if archive_path is not None and archive_path.is_file():
                try:
                    forbidden, forbidden_reasons = _archive_has_forbidden_activity(archive, plan)
                    case_report, checks = _save_case_reports(smoke_dir, case_id, plan, archive_path,
                        snapshot, cleanup, rules, execution_fault_type=execution_error)
                    _write_new(report_path, case_report)
                except Exception as exc:
                    forbidden_reasons = ["REPORT_PIPELINE_FAILED"]
                    checks = {"decision": "STOP", "blocking_items": ["checker_integrity:FAIL"],
                        "review_items": [], "task_success": "UNKNOWN_NOT_GRADED",
                        "checks": {"checker_integrity_checks": {"status": "FAIL", "blocking": True,
                            "error_type": type(exc).__name__}}}
                    case_report = {"schema_version": "personal-dev-technical-smoke-case-report-v1",
                        "case_id": case_id, "group_id": plan["case"]["group_id"], **SMOKE_LABELS,
                        "execution_status": "EXECUTION_ERROR", "execution_fault_type": execution_error,
                        "archive_path": evidence_path, "archive_sha256": _sha(archive_path),
                        "budget_snapshot": snapshot, "cleanup": cleanup, "checks": checks}
                    if not report_path.exists():
                        _write_new(report_path, case_report)
            else:
                forbidden_reasons = ["ARCHIVE_MISSING_OR_UNREADABLE"]
                checks = {"decision": "STOP", "blocking_items": ["archive_integrity:FAIL"],
                    "review_items": [], "task_success": "UNKNOWN_NOT_GRADED",
                    "checks": {"archive_integrity_checks": {"status": "FAIL", "blocking": True,
                        "execution_error_type": execution_error or "NONE"}}}
                case_report = {"schema_version": "personal-dev-technical-smoke-case-report-v1",
                    "case_id": case_id, "group_id": plan["case"]["group_id"], **SMOKE_LABELS,
                    "execution_status": "EXECUTION_ERROR", "execution_fault_type": execution_error,
                    "archive_path": evidence_path, "budget_snapshot": snapshot,
                    "cleanup": cleanup, "checks": checks}
                _write_new(report_path, case_report)
            if archive is not None:
                row = _case_summary(case_id, archive, checks, snapshot, cleanup, time.monotonic() - started)
            else:
                row = {"case_id": case_id, "run_id": active.get("run_id"),
                    "run_status": active.get("run_status", "UNKNOWN"),
                    "run_count": active.get("run_count", "UNKNOWN"),
                    "agent_started": active.get("agent_started", False),
                    "provider_calls": None, "read_only_calls": None, "tokens": None,
                    "estimated_usd": None, "supplier_billing": "UNKNOWN",
                    "semantic_review_status": "UNKNOWN", "unknown_count": 1,
                    "cleanup_status": cleanup.get("cleanup_status", "UNKNOWN"),
                    "task_success": "UNKNOWN_NOT_GRADED", "grading_status": "NOT_GRADED"}
            row.update({"group_id": plan["case"]["group_id"],
                "execution_status": "EXECUTION_ERROR" if execution_error else "EXECUTED",
                "diagnostic_decision": checks.get("decision", "STOP"),
                "technical_status": "FAIL" if checks.get("decision") == "STOP" or execution_error else "PASS",
                "quality_status": "NOT_ASSESSED",
                "code_outcome_status": checks.get("checks", {}).get("code_outcome_checks", {}).get("status", "UNKNOWN"),
                "output_presence_status": row.get("output_presence_status", "UNKNOWN"),
                "failure_phase": failure_phase, "execution_error_type": execution_error,
                "reason_code": ("EXECUTION_PIPELINE_EXCEPTION" if execution_error else None),
                "cleanup_safety": cleanup_safety, "cleanup_error_type": cleanup_error_type,
                "archive_path": evidence_path, "report_path": f"reports/{case_id}.json",
                "report_sha256": _sha(report_path), "forbidden_activity_violations": forbidden_reasons,
                "wall_duration_seconds": round(time.monotonic() - started, 3)})
            if archive_path is not None and archive_path.is_file():
                row["archive_sha256"] = _sha(archive_path)
            records.append(row)
            state["completed_case_ids"].append(case_id)
            state["remaining_case_ids"] = list(profile.cases[index + 1:])
            _write_json(smoke_dir / "state.json", state)
            _append_jsonl(smoke_dir / "journal.jsonl", {"event": "CASE_STOPPED" if execution_error else "CASE_CLOSED",
                "case_id": case_id, "decision": checks.get("decision"),
                "run_id": row.get("run_id"), "run_status": row.get("run_status"),
                "run_count": row.get("run_count"), "execution_error_type": execution_error,
                "failure_phase": failure_phase, "cleanup_status": row.get("cleanup_status"),
                "cleanup_safety": cleanup_safety,
                "archive_sha256": row.get("archive_sha256"),
                "report_sha256": row.get("report_sha256"),
                "budget_snapshot_kind": snapshot.get("snapshot_kind"),
                "budget_complete": snapshot.get("complete"),
                "budget_settled": snapshot.get("settled")})
            stopped = _case_requires_stop(checks, execution_error, cleanup)
            if stopped:
                for remaining in profile.cases[index + 1:]:
                    row = {"case_id": remaining, "group_id": plans_by_case[remaining]["case"]["group_id"],
                        "execution_status": "NOT_RUN", "diagnostic_decision": "STOPPED_BY_PREVIOUS_CASE",
                        "technical_status": "UNKNOWN", "quality_status": "NOT_APPLICABLE",
                        "semantic_review_status": "NOT_APPLICABLE", "run_id": None,
                        "run_status": "NOT_RUN", "provider_calls": 0, "read_only_calls": 0,
                        "tokens": 0, "estimated_usd": 0.0, "cleanup_status": "NOT_APPLICABLE",
                        "reason": "Earlier case reached STOP or unresolved execution state.",
                        "agent_started": False, "grading_status": "NOT_GRADED"}
                    records.append(row)
                    _append_jsonl(smoke_dir / "journal.jsonl", {"event": "CASE_NOT_RUN", **row})
                break
        try:
            snapshot = ledger.snapshot()
        except Exception as exc:
            snapshot = {"snapshot_kind": "UNKNOWN", "complete": False, "settled": False,
                "active_reservations": "UNKNOWN", "integrity_errors": ["BATCH_BUDGET_SNAPSHOT_FAILED"],
                "calls_by_category": None, "tokens": None, "estimated_usd": None,
                "token_status": "UNKNOWN_USAGE", "estimate_status": "UNKNOWN_USAGE",
                "snapshot_error_type": type(exc).__name__}
        _write_new(smoke_dir / "reports/batch-budget-final.json", snapshot)
        report = _closeout_docs(smoke_dir, manifest, budget, state, records, price_sha,
            profile_id=profile.profile_id, batch_budget_snapshot=snapshot)
        status = "CLOSED_COMPLETED" if report["status"] == "SMOKE_COMPLETED" else "CLOSED_STOPPED"
        state["remaining_case_ids"] = [row["case_id"] for row in records if row.get("execution_status") == "NOT_RUN"]
        _close_smoke(smoke_dir, auth, state, status=status, outcome={
            "closeout_path": "reports/closeout.json", "closeout_sha256": _sha(smoke_dir / "reports/closeout.json"),
            "executed_case_count": report["executed_case_count"], "technical_pass_count": report["technical_pass_count"],
            "technical_fail_count": report["technical_fail_count"], "not_run_count": report["not_run_count"]})
        _append_jsonl(smoke_dir / "journal.jsonl", {"event": "SMOKE_CLOSED", "status": status,
            "closed_at_utc": state["closed_at_utc"], "closeout_sha256": _sha(smoke_dir / "reports/closeout.json")})
        if profile.profile_id in {SAFETY038_R4_PROFILE_ID, SAFETY038_SCORING_PROFILE_ID}:
            _append_attempt_event("ATTEMPT_SETTLED", profile_id=profile.profile_id,
                case_id="D1-SAFETY-038", smoke_id=smoke_dir.name,
                closeout_sha256=_sha(smoke_dir / "reports/closeout.json"), authorization_status=status,
                provider_calls=report["budget_totals"]["provider_requests"],
                policy_reads=report["budget_totals"]["read_only_calls"],
                tokens=report["budget_totals"]["tokens"],
                estimated_usd=report["budget_totals"]["estimated_usd"],
                run_id=next((row.get("run_id") for row in records if row.get("case_id") == "D1-SAFETY-038"), None),
                budget_settled=bool(snapshot.get("settled")))
        _append_progress(report, smoke_dir)
        _write_sha_manifest(smoke_dir)
        return {"status": report["status"], "authorization_status": status,
            "smoke_id": smoke_dir.name, "closeout_path": "reports/closeout.json",
            "case_count": len(records), "executed": report["executed_case_count"],
            "provider_calls": report["budget_totals"]["provider_requests"],
            "read_only_calls": report["budget_totals"]["read_only_calls"],
            "tokens": report["budget_totals"]["tokens"], "agent_started": any(row.get("agent_started") for row in records),
            "grading_status": "NOT_GRADED"}
    finally:
        engine.dispose()


def _append_progress(report: dict, smoke_dir: Path) -> None:
    path = ROOT / "docs/progress.md"
    if not path.is_file():
        return
    line = (f"\n\n## {datetime.now(timezone.utc).strftime('%Y-%m-%d')} personal technical Smoke\n\n"
        f"- {smoke_dir.name}: {report['status']}; executed {report['executed_case_count']}/{report['requested_case_count']}; "
        f"technical PASS {report['technical_pass_count']}, FAIL {report['technical_fail_count']}, "
        f"NOT_RUN {report['not_run_count']}; result remains NOT_GRADED.\n"
        "- Formal Gold/Registry/M2/Dev Smoke/Frozen gates unchanged; purpose-built sample is not statistical evidence.\n")
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)


def _write_sha_manifest(smoke_dir: Path) -> None:
    rows = []
    for path in sorted(smoke_dir.rglob("*")):
        if path.is_file() and path.name != "SHA256SUMS.md":
            rows.append(f"{_sha(path)}  {path.relative_to(smoke_dir).as_posix()}")
    progress = ROOT / "docs/progress.md"
    if progress.is_file():
        rows.append(f"{_sha(progress)}  ../../../progress.md (appended status)")
    manifest = "# Personal Dev Technical Smoke SHA-256\n\n" + "\n".join(rows) + "\n"
    with (smoke_dir / "SHA256SUMS.md").open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description="Exact-profile personal D1 technical Smoke")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--prepare", action="store_true")
    modes.add_argument("--check-admission", action="store_true")
    modes.add_argument("--execute", action="store_true")
    parser.add_argument("--smoke-dir", type=Path)
    parser.add_argument("--profile", choices=(SMOKE_NINE_PROFILE_ID, SMOKE_EIGHT_PROFILE_ID,
        SMOKE_EIGHT_R2_PROFILE_ID, POLICY014_SAFETY038_PROFILE_ID,
        POLICY014_SAFETY038_R2_PROFILE_ID, POLICY014_SAFETY038_R3_PROFILE_ID,
        SAFETY038_R4_PROFILE_ID, SAFETY038_SCORING_PROFILE_ID, M62_PILOT_PROFILE_ID,
        M62_PILOT_R2_PROFILE_ID, M62_PILOT_FIVE_R1_PROFILE_ID, M62_PILOT_FIVE_R2_PROFILE_ID),
        default=SMOKE_NINE_PROFILE_ID)
    args = parser.parse_args()
    if args.prepare:
        if args.smoke_dir:
            raise SmokeAdmissionError("PREPARE_DOES_NOT_ACCEPT_EXISTING_PATH")
        result = prepare_smoke(args.profile)
    else:
        if args.smoke_dir is None:
            raise SmokeAdmissionError("EXACT_SMOKE_DIRECTORY_REQUIRED")
        result = check_admission(args.smoke_dir, args.profile) if args.check_admission else execute_smoke(args.smoke_dir, args.profile)
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
