"""One fixed, pre-reserved DeepSeek response probe for the R6 POLICY-013 round."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / ".vendor", ROOT / ".vendor/g2"):
    sys.path.insert(0, str(path))

ROUND_ID = "M61D1-POLICY013-PROVIDER-PROBE-R6C-20261027T0840Z"
EXPERIMENT_ID = "M61D1-PROVIDER-PROBE-R6C-20261027T0840Z"
PACKAGE = ROOT / "docs/eval/d1-diagnostic-r6-policy013-20261027"
PLAN_PATH = PACKAGE / "provider-diagnostic-plan-r6-attempt3.json"
RESULT_PATH = PACKAGE / "provider-diagnostic-result-r6-attempt3.json"
PROBE_BUDGET_PATH = PACKAGE / "provider-diagnostic-budget-r6-attempt3.json"
PRICE_PATH = PACKAGE / "official-pricing-evidence-r6.json"
ASSET_PATH = ROOT / "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
ASSET_SHA256 = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
RESERVED_TOKENS = 8192
RESERVED_USD = 0.0098304
PROXY = "http://127.0.0.1:7892"
PROXY_VARIABLES = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _proxy_check() -> dict:
    import httpx
    from httpx._utils import URLPattern, get_environment_proxies
    values = {name: __import__("os").environ.get(name) for name in PROXY_VARIABLES}
    if any(value != PROXY for value in values.values()):
        raise RuntimeError("PROCESS_PROXY_BINDING_MISMATCH")
    proxies = get_environment_proxies()
    url = httpx.URL("https://api.deepseek.com/v1/chat/completions")
    matches = sorted(((URLPattern(pattern), proxy) for pattern, proxy in proxies.items()),
        key=lambda item: item[0].priority, reverse=True)
    no_proxy_bypasses = any(proxy is None and pattern.matches(url) for pattern, proxy in matches)
    if no_proxy_bypasses:
        raise RuntimeError("NO_PROXY_BYPASSES_PROVIDER")
    selected = next((proxy for pattern, proxy in matches if pattern.matches(url)), None)
    if selected != PROXY:
        raise RuntimeError("HTTPX_PROXY_SELECTION_MISMATCH")
    parsed = httpx.URL(selected)
    return {"variables": {name: PROXY for name in PROXY_VARIABLES},
        "httpx_selected": {"scheme": parsed.scheme, "host": parsed.host,
            "port": parsed.port}, "no_proxy_bypasses_provider": False,
        "httpx_version": httpx.__version__}


def _validate_local_bindings():
    from app.m0.config import load_settings
    from eval.m6_1.personal_dev import implementation_digest
    from eval.m6_1.personal_dev_authorization import file_sha256, load_price_evidence
    from eval.m6_1.personal_dev_database import PersonalDevDatabase
    from sqlalchemy import select, text
    from app.m1.models import Run, RunEvent, PendingAction, User, Order, Shipment, Product

    settings = load_settings(require_api_key=True)
    if settings.model != "deepseek-flash" or settings.base_url.rstrip("/") != "https://api.deepseek.com":
        raise RuntimeError("MODEL_ENDPOINT_BINDING_MISMATCH")
    dataset_sha = hashlib.sha256(ASSET_PATH.read_bytes()).hexdigest()
    if dataset_sha != ASSET_SHA256:
        raise RuntimeError("D1_ASSET_BINDING_MISMATCH")
    price, price_sha = load_price_evidence(PRICE_PATH, model=settings.model, endpoint=settings.base_url)
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if plan.get("experiment_id") != EXPERIMENT_ID or plan.get("round_id") != ROUND_ID:
        raise RuntimeError("PROBE_PLAN_ID_MISMATCH")
    if plan.get("code_sha256") != implementation_digest(ROOT):
        raise RuntimeError("PROBE_CODE_BINDING_MISMATCH")
    if plan.get("dataset_sha256") != dataset_sha or plan.get("pricing_evidence_sha256") != price_sha:
        raise RuntimeError("PROBE_ASSET_OR_PRICE_BINDING_MISMATCH")
    if plan.get("model") != settings.model or plan.get("provider_endpoint") != settings.base_url:
        raise RuntimeError("PROBE_MODEL_ENDPOINT_BINDING_MISMATCH")
    if plan.get("probe_budget_sha256") != hashlib.sha256(PROBE_BUDGET_PATH.read_bytes()).hexdigest():
        raise RuntimeError("PROBE_BUDGET_BINDING_MISMATCH")
    case_budget_path = PACKAGE / "D1-POLICY-013-budget-r6.json"
    if plan.get("case_budget_sha256") != hashlib.sha256(case_budget_path.read_bytes()).hexdigest():
        raise RuntimeError("CASE_BUDGET_BINDING_MISMATCH")
    from eval.m6_1.provider_diagnostic import build_provider_probe_parameters
    probe_params = build_provider_probe_parameters(model=settings.model)
    params_sha = hashlib.sha256(json.dumps(probe_params, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    if plan.get("probe_request", {}).get("parameters_sha256") != params_sha:
        raise RuntimeError("PROBE_REQUEST_PARAMETERS_MISMATCH")
    database = PersonalDevDatabase(ROOT)
    if database.path.resolve() != database.path.absolute() or not database.path.is_file():
        raise RuntimeError("PERSONAL_DATABASE_PATH_INVALID")
    engine, factory = database.open()
    try:
        with factory() as db:
            database.check_session(db)
            marker = db.execute(text("SELECT profile,identity,path,lease_id,receipt FROM d1_personal_target")).mappings().one()
            counts = {"runs": db.execute(select(Run.run_id).limit(1)).first() is not None,
                "run_events": db.execute(select(RunEvent.event_id).limit(1)).first() is not None,
                "pending_actions": db.execute(select(PendingAction.action_id).limit(1)).first() is not None,
                "users": db.execute(select(User.user_id).limit(1)).first() is not None,
                "orders": db.execute(select(Order.order_id).limit(1)).first() is not None,
                "shipments": db.execute(select(Shipment.shipment_id).limit(1)).first() is not None,
                "products": db.execute(select(Product.product_id).limit(1)).first() is not None}
            if marker["lease_id"] is not None or marker["receipt"] is not None or any(counts.values()):
                raise RuntimeError("PERSONAL_DATABASE_NOT_IDLE_OR_EMPTY")
        return settings, database, price, price_sha, marker["identity"], counts
    finally:
        engine.dispose()


def run_once() -> dict:
    from eval.m6_1.budget import BudgetLimits
    from eval.m6_1.personal_dev import create_budget_ledger, implementation_digest
    from eval.m6_1.provider_diagnostic import (
        PROBE_MESSAGE, build_provider_probe_parameters, classify_chat_completion,
    )
    from app.m0.config import create_deepseek_client

    if RESULT_PATH.exists() or (PACKAGE / "state/D1-POLICY-013-execution-state-r6.json").exists() or (
            PACKAGE / "D1-POLICY-013-authorization-r6.json").exists():
        raise RuntimeError("R6_PROBE_OR_CASE_ALREADY_USED_NO_REPLAY")
    proxy = _proxy_check()
    print(json.dumps({"event": "PROCESS_PROXY_CONFIRMED", "proxy": proxy}, ensure_ascii=False), flush=True)
    settings, database, price, price_sha, identity, db_counts = _validate_local_bindings()
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if plan.get("database_identity") != identity:
        raise RuntimeError("PROBE_DATABASE_IDENTITY_MISMATCH")
    git_revision = __import__("subprocess").check_output(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip()
    if plan.get("git_revision") != git_revision:
        raise RuntimeError("PROBE_GIT_REVISION_MISMATCH")

    limits_data = json.loads(PROBE_BUDGET_PATH.read_text(encoding="utf-8"))
    limits = BudgetLimits(**limits_data)
    permit = {"authorization_id": EXPERIMENT_ID, "asset_sha256": ASSET_SHA256,
        "case_ids": ["D1-POLICY-013"], "database_identity": identity,
        "code_sha256": implementation_digest(ROOT), "git_commit": git_revision,
        "data_version": "m6.1-d1-dev-draft-r3.2.4", "authorization_type": "PERSONAL_DEV_DIAGNOSTIC_NONSCORING"}
    ledger = create_budget_ledger(database, permit, limits, settings)
    if ledger.snapshot()["events"]:
        raise RuntimeError("PROBE_LEDGER_ALREADY_USED_NO_REPLAY")
    reservation = ledger.reserve(trial_id=EXPERIMENT_ID, category="provider", calls=1,
        tokens_reserved=RESERVED_TOKENS, estimated_usd_reserved=RESERVED_USD)
    reserved_at = _now()
    attempted = False
    try:
        route_node = settings.nodes["route"]
        client = create_deepseek_client(settings, route_node)
        attempted = True
        request_parameters = build_provider_probe_parameters(model=settings.model)
        response = client.chat.completions.create(**request_parameters)
        details = classify_chat_completion(response)
        usage = details["usage"]
        if usage["status"] == "RECORDED" and usage["total_tokens"] <= RESERVED_TOKENS:
            estimate = usage["total_tokens"] * price["peak"]["output"] / 1_000_000
            ledger.settle(reservation, status="COMMIT", actual_calls=1,
                actual_tokens=usage["total_tokens"], estimated_usd=estimate,
                call_count_status="RECORDED")
            settle_status = "SETTLED"
        else:
            estimate = None
            ledger.settle(reservation, status="COMMIT", actual_calls=1,
                actual_tokens=None, estimated_usd=None,
                call_count_status="CONSERVATIVE_UPPER_BOUND")
            settle_status = "USAGE_UNKNOWN_OR_EXCEEDS_RESERVATION"
        request_summary = {"model": settings.model, "endpoint_host": "api.deepseek.com",
            "max_tokens": 64, "stream": False,
            "extra_body": {"thinking": {"type": "disabled"}},
            "sdk_max_retries": 0, "message_sha256": hashlib.sha256(PROBE_MESSAGE.encode()).hexdigest()}
        return {"schema_version": "1.0", "experiment_id": EXPERIMENT_ID,
            "round_id": ROUND_ID, "case_id": "D1-POLICY-013",
            "request_attempted": True, "response_received": True,
            "proxy": proxy, "model": settings.model, "endpoint_host": "api.deepseek.com",
            "request_parameters": request_summary,
            "request_parameters_sha256": hashlib.sha256(json.dumps(
                request_summary, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
            "response": details, "classification": details["classification"],
            "reserved_at_utc": reserved_at, "completed_at_utc": _now(),
            "settlement_status": settle_status,
            "local_estimated_usd": estimate if estimate is not None else "UNKNOWN",
            "provider_billed_usd": "UNKNOWN", "budget_ledger": ledger.snapshot(),
            "database_identity": identity, "database_state_at_probe": db_counts,
            "case_authorization_created": False, "case_execution_started": False,
            "retry_count": 0}
    except Exception as exc:
        if attempted:
            ledger.settle(reservation, status="COMMIT", actual_calls=1,
                actual_tokens=None, estimated_usd=None,
                call_count_status="CONSERVATIVE_UPPER_BOUND")
            status = "REQUEST_ATTEMPTED_NO_RESPONSE"
            response_status = None
            try:
                response_status = getattr(getattr(exc, "response", None), "status_code", None)
            except Exception:
                pass
            if response_status is not None:
                status = "HTTP_ERROR_RESPONSE"
        else:
            ledger.settle(reservation, status="RELEASE", actual_calls=0,
                actual_tokens=0, estimated_usd=0.0)
            status = "LOCAL_PRE_REQUEST_FAILURE"
            response_status = None
        chain = []
        current = exc
        while current is not None and len(chain) < 8:
            chain.append(type(current).__name__)
            current = current.__cause__ or current.__context__
        return {"schema_version": "1.0", "experiment_id": EXPERIMENT_ID,
            "round_id": ROUND_ID, "case_id": "D1-POLICY-013",
            "request_attempted": attempted, "response_received": response_status is not None,
            "proxy": proxy, "model": settings.model, "endpoint_host": "api.deepseek.com",
            "classification": status, "exception_type_chain": chain,
            "http_status": response_status, "usage": "UNKNOWN",
            "reserved_at_utc": reserved_at, "completed_at_utc": _now(),
            "settlement_status": "CONSERVATIVE_UPPER_BOUND" if attempted else "RELEASED",
            "local_estimated_usd": "UNKNOWN" if attempted else 0.0,
            "provider_billed_usd": "UNKNOWN", "budget_ledger": ledger.snapshot(),
            "database_identity": identity, "case_authorization_created": False,
            "case_execution_started": False, "retry_count": 0}


def main() -> int:
    PACKAGE.mkdir(parents=True, exist_ok=True)
    safe_failure_codes = {
        "R6_PROBE_OR_CASE_ALREADY_USED_NO_REPLAY", "PROCESS_PROXY_BINDING_MISMATCH",
        "HTTPX_PROXY_SELECTION_MISMATCH", "NO_PROXY_BYPASSES_PROVIDER",
        "MODEL_ENDPOINT_BINDING_MISMATCH", "D1_ASSET_BINDING_MISMATCH",
        "PROBE_PLAN_ID_MISMATCH", "PROBE_CODE_BINDING_MISMATCH",
        "PROBE_ASSET_OR_PRICE_BINDING_MISMATCH", "PROBE_MODEL_ENDPOINT_BINDING_MISMATCH",
        "PROBE_BUDGET_BINDING_MISMATCH", "CASE_BUDGET_BINDING_MISMATCH",
        "PROBE_REQUEST_PARAMETERS_MISMATCH", "PERSONAL_DATABASE_PATH_INVALID",
        "PERSONAL_DATABASE_NOT_IDLE_OR_EMPTY", "PROBE_DATABASE_IDENTITY_MISMATCH",
        "PROBE_GIT_REVISION_MISMATCH", "PROBE_LEDGER_ALREADY_USED_NO_REPLAY",
    }
    try:
        result = run_once()
    except Exception as exc:
        result = {"schema_version": "1.0", "experiment_id": EXPERIMENT_ID,
            "round_id": ROUND_ID, "case_id": "D1-POLICY-013",
            "classification": "LOCAL_PRE_REQUEST_FAILURE",
            "request_attempted": False, "usage": "NOT_APPLICABLE",
            "exception_type_chain": [type(exc).__name__],
            "failure_code": str(exc) if str(exc) in safe_failure_codes else "UNCLASSIFIED_PRE_REQUEST_FAILURE",
            "provider_billed_usd": "NOT_APPLICABLE",
            "case_authorization_created": False, "case_execution_started": False,
            "retry_count": 0}
    with RESULT_PATH.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("classification") == "NORMAL_NONEMPTY_COMPLETION" and result.get(
        "settlement_status") == "SETTLED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
