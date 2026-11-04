"""Narrow, separate user-confirmed authorization for non-scoring personal D1 runs."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from .contracts import sha256_json


AUTHORIZATION_TYPE = "PERSONAL_DEV_DIAGNOSTIC_NONSCORING"
AUTHORIZED_CASE = "D1-POLICY-013"
AUTHORIZED_ASSET_PATH = "eval/m6_1/drafts/m6.1-d1-synthetic-dev-r3.2.4.json"
AUTHORIZED_VERSION = "m6.1-d1-dev-draft-r3.2.4"
AUTHORIZED_ASSET_SHA256 = "8a95e5db1264b0ed85b669c05ad5a698f217d6967b6a5bd6488959d4a7029969"
PRICE_SOURCE = "https://api-docs.deepseek.com/quick_start/pricing/"
BATCH_AUTHORIZATION_TYPE = "PERSONAL_DEV_AGENT_EVAL"
DIAGNOSTIC_ROUND_ID = "M61D1-THREECASE-R3-LIVE-20261027T0634Z"
DIAGNOSTIC_ROUND_ROOT = "docs/eval/d1-diagnostic-r3-20261027"
PROVIDER_RETRY_ROUND_ID = "M61D1-POLICY013-PROVIDER-RETRY-R5-20261027"
PROVIDER_RETRY_ROUND_ROOT = "docs/eval/d1-diagnostic-r5-policy013-20261027"
PROVIDER_PROBE_ROUND_ID = "M61D1-POLICY013-PROVIDER-PROBE-R6-20261027T0840Z"
PROVIDER_PROBE_ROUND_ROOT = "docs/eval/d1-diagnostic-r6-policy013-20261027"
ORDER_DIAGNOSTIC_ROUND_ID = "M61D1-ORDERS-R7-20261027T0920Z"
ORDER_DIAGNOSTIC_ROUND_ROOT = "docs/eval/d1-diagnostic-r7-orders-20261027"
CROSSAGENT030_ROUND_ID = "M62-CA030-MULTI-R1-20261029"
CROSSAGENT030_ROUND_ROOT = "docs/eval/m6.2-crossagent030-r1-20261029"


class PersonalAuthorizationError(ValueError):
    """Closed failure for invalid, expired, or out-of-scope personal authorizations."""


@dataclass(frozen=True)
class PersonalDiagnosticProfile:
    case_id: str
    authorization_path: str
    budget_path: str
    pricing_path: str
    execution_state_path: str
    allowed_tools: frozenset[str]
    preflight_kind: str
    check_rules_path: str | None = None
    report_dir: str | None = None
    round_id: str | None = None
    report_suffix: str | None = None
    budget_limits: dict | None = None
    predecessor_case_id: str | None = None
    predecessor_round_id: str | None = None
    predecessor_report_path: str | None = None
    predecessor_report_sha256: str | None = None
    predecessor_archive_path: str | None = None
    predecessor_archive_sha256: str | None = None
    predecessor_authorization_path: str | None = None
    predecessor_authorization_sha256: str | None = None
    predecessor_state_path: str | None = None
    predecessor_state_sha256: str | None = None
    predecessor_independent_report_path: str | None = None
    predecessor_independent_report_sha256: str | None = None
    predecessor_rules_sha256: str | None = None
    predecessor_manifest_path: str | None = None
    predecessor_manifest_sha256: str | None = None
    predecessor_closeout_path: str | None = None
    predecessor_closeout_sha256: str | None = None
    predecessor_closeout_markdown_path: str | None = None
    predecessor_closeout_markdown_sha256: str | None = None
    predecessor_code_sha256: str | None = None
    predecessor_checker_version: str | None = None


PERSONAL_DIAGNOSTIC_PROFILES = {
    "D1-POLICY-013": PersonalDiagnosticProfile(
        case_id="D1-POLICY-013",
        authorization_path=f"{DIAGNOSTIC_ROUND_ROOT}/D1-POLICY-013-authorization-r3.json",
        budget_path=f"{DIAGNOSTIC_ROUND_ROOT}/D1-POLICY-013-budget-r3.json",
        pricing_path=f"{DIAGNOSTIC_ROUND_ROOT}/official-pricing-evidence-r3.json",
        execution_state_path=f"{DIAGNOSTIC_ROUND_ROOT}/state/D1-POLICY-013-execution-state-r3.json",
        allowed_tools=frozenset({"search_knowledge"}),
        preflight_kind="policy",
        check_rules_path=f"{DIAGNOSTIC_ROUND_ROOT}/locked-check-rules-r3.json",
        report_dir=f"{DIAGNOSTIC_ROUND_ROOT}/reports",
        round_id=DIAGNOSTIC_ROUND_ID,
        report_suffix="r3",
    ),
    "D1-PRODUCT-001": PersonalDiagnosticProfile(
        case_id="D1-PRODUCT-001",
        authorization_path="docs/eval/m6.1-d1-personal-dev-preparation-r2/15_d1-product-001-authorization-r1.json",
        budget_path="docs/eval/m6.1-d1-personal-dev-preparation-r2/14_d1-product-001-budget-r1.json",
        pricing_path="docs/eval/m6.1-d1-personal-dev-preparation-r2/23_official-pricing-evidence-2026-10-27-r1.json",
        execution_state_path="docs/eval/m6.1-d1-personal-dev-preparation-r2/16_d1-product-001-execution-state-r1.json",
        allowed_tools=frozenset({"search_products", "get_product_detail"}),
        preflight_kind="product",
    ),
    "D1-ORDER-007": PersonalDiagnosticProfile(
        case_id="D1-ORDER-007",
        authorization_path=f"{DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-007-authorization-r3.json",
        budget_path=f"{DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-007-budget-r3.json",
        pricing_path=f"{DIAGNOSTIC_ROUND_ROOT}/official-pricing-evidence-r3.json",
        execution_state_path=f"{DIAGNOSTIC_ROUND_ROOT}/state/D1-ORDER-007-execution-state-r3.json",
        allowed_tools=frozenset({"get_order"}),
        preflight_kind="order",
        check_rules_path=f"{DIAGNOSTIC_ROUND_ROOT}/locked-check-rules-r3.json",
        report_dir=f"{DIAGNOSTIC_ROUND_ROOT}/reports",
        round_id=DIAGNOSTIC_ROUND_ID,
        report_suffix="r3",
    ),
    "D1-ORDER-009": PersonalDiagnosticProfile(
        case_id="D1-ORDER-009",
        authorization_path=f"{DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-009-authorization-r3.json",
        budget_path=f"{DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-009-budget-r3.json",
        pricing_path=f"{DIAGNOSTIC_ROUND_ROOT}/official-pricing-evidence-r3.json",
        execution_state_path=f"{DIAGNOSTIC_ROUND_ROOT}/state/D1-ORDER-009-execution-state-r3.json",
        allowed_tools=frozenset(),
        preflight_kind="none",
        check_rules_path=f"{DIAGNOSTIC_ROUND_ROOT}/locked-check-rules-r3.json",
        report_dir=f"{DIAGNOSTIC_ROUND_ROOT}/reports",
        round_id=DIAGNOSTIC_ROUND_ID,
        report_suffix="r3",
    ),
}


PERSONAL_DIAGNOSTIC_ROUND_PROFILES = {
    ("D1-CROSSAGENT-030", CROSSAGENT030_ROUND_ID): PersonalDiagnosticProfile(
        case_id="D1-CROSSAGENT-030",
        authorization_path=f"{CROSSAGENT030_ROUND_ROOT}/authorization.json",
        budget_path=f"{CROSSAGENT030_ROUND_ROOT}/budget.json",
        pricing_path="docs/eval/m6.2-outcome-pilot-r2-20261028/official-pricing-evidence-m6.2-r1.json",
        execution_state_path=f"{CROSSAGENT030_ROUND_ROOT}/state/execution-state.json",
        allowed_tools=frozenset({"get_order", "get_order_items", "get_warranty"}),
        preflight_kind="crossagent_warranty",
        check_rules_path=f"{CROSSAGENT030_ROUND_ROOT}/locked-check-rules.json",
        report_dir=f"{CROSSAGENT030_ROUND_ROOT}/reports",
        round_id=CROSSAGENT030_ROUND_ID,
        report_suffix="r1",
        budget_limits={"provider_calls": 6, "tool_calls": 3, "total_tokens": 120000,
            "estimated_usd": 0.18, "max_duration_seconds": 180,
            "max_concurrent_trials": 1, "max_retries": 0,
            "embedding_calls": 0, "judge_calls": 0,
            "max_calls_per_trial": 3},
    ),
    ("D1-POLICY-013", PROVIDER_RETRY_ROUND_ID): PersonalDiagnosticProfile(
        case_id="D1-POLICY-013",
        authorization_path=f"{PROVIDER_RETRY_ROUND_ROOT}/D1-POLICY-013-authorization-r5.json",
        budget_path=f"{PROVIDER_RETRY_ROUND_ROOT}/D1-POLICY-013-budget-r5.json",
        pricing_path=f"{DIAGNOSTIC_ROUND_ROOT}/official-pricing-evidence-r3.json",
        execution_state_path=f"{PROVIDER_RETRY_ROUND_ROOT}/state/D1-POLICY-013-execution-state-r5.json",
        allowed_tools=frozenset({"search_knowledge"}),
        preflight_kind="policy",
        check_rules_path=f"{DIAGNOSTIC_ROUND_ROOT}/locked-check-rules-r3.json",
        report_dir=f"{PROVIDER_RETRY_ROUND_ROOT}/reports",
        round_id=PROVIDER_RETRY_ROUND_ID,
        report_suffix="r5",
        # One reserved diagnostic call consumes at most 8,192 tokens and
        # USD 0.0098304 from the user's total envelope. This profile is the
        # remaining case-run budget; total caps therefore remain unchanged.
        budget_limits={
            "provider_calls": 3, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 111808, "estimated_usd": 0.1701696,
            "tool_calls": 4, "max_calls_per_trial": 4,
            "max_concurrent_trials": 1, "max_duration_seconds": 180,
            "max_retries": 0,
        },
    ),
    ("D1-POLICY-013", PROVIDER_PROBE_ROUND_ID): PersonalDiagnosticProfile(
        case_id="D1-POLICY-013",
        authorization_path=f"{PROVIDER_PROBE_ROUND_ROOT}/D1-POLICY-013-authorization-r6.json",
        budget_path=f"{PROVIDER_PROBE_ROUND_ROOT}/D1-POLICY-013-budget-r6.json",
        pricing_path=f"{PROVIDER_PROBE_ROUND_ROOT}/official-pricing-evidence-r6.json",
        execution_state_path=f"{PROVIDER_PROBE_ROUND_ROOT}/state/D1-POLICY-013-execution-state-r6.json",
        allowed_tools=frozenset({"search_knowledge"}),
        preflight_kind="policy",
        check_rules_path=f"{DIAGNOSTIC_ROUND_ROOT}/locked-check-rules-r3.json",
        report_dir=f"{PROVIDER_PROBE_ROUND_ROOT}/reports",
        round_id=PROVIDER_PROBE_ROUND_ID,
        report_suffix="r6",
        # The single registered probe reserves 8,192 tokens and USD 0.0098304;
        # this is the exact remaining share of the user's per-round envelope.
        budget_limits={
            "provider_calls": 3, "embedding_calls": 0, "judge_calls": 0,
            "total_tokens": 111808, "estimated_usd": 0.1701696,
            "tool_calls": 4, "max_calls_per_trial": 4,
            "max_concurrent_trials": 1, "max_duration_seconds": 180,
            "max_retries": 0,
        },
    ),
    ("D1-ORDER-007", ORDER_DIAGNOSTIC_ROUND_ID): PersonalDiagnosticProfile(
        case_id="D1-ORDER-007",
        authorization_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-007-authorization-r7.json",
        budget_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-007-budget-r7.json",
        pricing_path=f"{PROVIDER_PROBE_ROUND_ROOT}/official-pricing-evidence-r6.json",
        execution_state_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/state/D1-ORDER-007-execution-state-r7.json",
        allowed_tools=frozenset({"get_order"}),
        preflight_kind="order",
        check_rules_path=f"{DIAGNOSTIC_ROUND_ROOT}/locked-check-rules-r3.json",
        report_dir=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/reports",
        round_id=ORDER_DIAGNOSTIC_ROUND_ID,
        report_suffix="r7",
        predecessor_case_id="D1-POLICY-013",
        predecessor_round_id=PROVIDER_PROBE_ROUND_ID,
        predecessor_report_path=f"{PROVIDER_PROBE_ROUND_ROOT}/reports/D1-POLICY-013-diagnostic-check-r6.json",
        predecessor_report_sha256="4fb21290a964fd65f7656634a89deb305afb15ea55a9b36a000f0659c15bc463",
        predecessor_archive_path="docs/eval/personal-dev-runs/66c785b79d01425e85f4d5eba1dfebe9.json",
        predecessor_archive_sha256="eeecf3a5b314513fc06b4bdc2ab075cbbf35cd6b45ea2c5fdb2a90b120e44582",
        predecessor_authorization_path=f"{PROVIDER_PROBE_ROUND_ROOT}/D1-POLICY-013-authorization-r6.json",
        predecessor_authorization_sha256="0f9c9dca4d5b7dc8e74f1758c7b8555344f47c78f61029f77f492421f11a0fcc",
        predecessor_state_path=f"{PROVIDER_PROBE_ROUND_ROOT}/state/D1-POLICY-013-execution-state-r6.json",
        predecessor_state_sha256="c27cc0f05fa9e2685789cd4e73430e169a4d8460d2dc56cd2e668bfa57e10177",
        predecessor_independent_report_path=f"{PROVIDER_PROBE_ROUND_ROOT}/reports/D1-POLICY-013-independent-check-r6.json",
        predecessor_independent_report_sha256="bf11fd420c871c3e72ba096a289fb886e36fd1cfe96cbe4556e24a922879e1ae",
        predecessor_rules_sha256="34f38e0266c955b0aa37bfd3a64f3611dcd588940143ce87044223e07e9c9a30",
        predecessor_manifest_path=f"{PROVIDER_PROBE_ROUND_ROOT}/SHA256SUMS-r6.md",
        predecessor_manifest_sha256="87633492964013669571d8c53520819757c5204bdae72a8dd62fa4f07899d03e",
        predecessor_closeout_path=f"{PROVIDER_PROBE_ROUND_ROOT}/closeout-r6.json",
        predecessor_closeout_sha256="c5028bbd845827c6d611f6c07a216bcd08cf6c7456b1a748c0a10293bfb7c29b",
        predecessor_closeout_markdown_path=f"{PROVIDER_PROBE_ROUND_ROOT}/closeout-r6.md",
        predecessor_closeout_markdown_sha256="e43eb4b64f4bb7d21f23491a91093a2beab7aec3ce04c790179692b291106863",
        predecessor_code_sha256="336c8b10927352d35d69c940eeb3bc69817bc1f5584678fa66309f19293602da",
        predecessor_checker_version="m6.1-d1-diagnostic-checker-r3",
    ),
    ("D1-ORDER-009", ORDER_DIAGNOSTIC_ROUND_ID): PersonalDiagnosticProfile(
        case_id="D1-ORDER-009",
        authorization_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-009-authorization-r7.json",
        budget_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-009-budget-r7.json",
        pricing_path=f"{PROVIDER_PROBE_ROUND_ROOT}/official-pricing-evidence-r6.json",
        execution_state_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/state/D1-ORDER-009-execution-state-r7.json",
        allowed_tools=frozenset(),
        preflight_kind="none",
        check_rules_path=f"{DIAGNOSTIC_ROUND_ROOT}/locked-check-rules-r3.json",
        report_dir=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/reports",
        round_id=ORDER_DIAGNOSTIC_ROUND_ID,
        report_suffix="r7",
        predecessor_case_id="D1-ORDER-007",
        predecessor_round_id=ORDER_DIAGNOSTIC_ROUND_ID,
        predecessor_report_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/reports/D1-ORDER-007-diagnostic-check-r7.json",
        predecessor_authorization_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/D1-ORDER-007-authorization-r7.json",
        predecessor_state_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/state/D1-ORDER-007-execution-state-r7.json",
        predecessor_independent_report_path=f"{ORDER_DIAGNOSTIC_ROUND_ROOT}/reports/D1-ORDER-007-independent-check-r7.json",
        predecessor_rules_sha256="34f38e0266c955b0aa37bfd3a64f3611dcd588940143ce87044223e07e9c9a30",
        predecessor_checker_version="m6.1-d1-diagnostic-checker-r4",
    ),
}


def personal_diagnostic_profile(case_id: str, *, diagnostic_round_id: str | None = None) -> PersonalDiagnosticProfile:
    if diagnostic_round_id is not None:
        round_profile = PERSONAL_DIAGNOSTIC_ROUND_PROFILES.get((case_id, diagnostic_round_id))
        if round_profile is not None:
            return round_profile
    profile = PERSONAL_DIAGNOSTIC_PROFILES.get(case_id)
    if profile is None:
        raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_CASE_NOT_ALLOWLISTED")
    if diagnostic_round_id is not None and profile.round_id != diagnostic_round_id:
        raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_ROUND_NOT_ALLOWLISTED")
    return profile


def validate_personal_batch_authorization(authorization: dict, *, case_ids: list[str],
        asset_sha256: str, code_sha256: str, code_revision: str, database_identity: str,
        model: str, endpoint: str, budget: dict, budget_file_sha256_value: str,
        pricing_sha256_value: str, expected_budget_ref: str | None = None,
        now: datetime | None = None) -> None:
    """Validate a one-shot user-confirmed batch without touching formal Registry."""
    if not isinstance(authorization, dict) or authorization.get("schema_version") != "1.0":
        raise PersonalAuthorizationError("PERSONAL_BATCH_AUTHORIZATION_SCHEMA_INVALID")
    if authorization.get("authorization_type") != BATCH_AUTHORIZATION_TYPE:
        raise PersonalAuthorizationError("PERSONAL_BATCH_AUTHORIZATION_TYPE_MISMATCH")
    if authorization.get("status") != "USER_CONFIRMED":
        raise PersonalAuthorizationError("PERSONAL_BATCH_USER_CONFIRMATION_REQUIRED")
    if (authorization.get("purpose") != "One-time 40-case personal Dev Agent evaluation; not a formal Gold, Frozen Test, or M2 result." or
            authorization.get("result_scope") != "PERSONAL_DEV_ONLY" or
            authorization.get("grading_status") != "PERSONAL_DEV_ONLY" or
            authorization.get("data_version") != "m6.1-d1-dev-draft-r3.2.4" or
            authorization.get("asset_path") != AUTHORIZED_ASSET_PATH or
            authorization.get("asset_id") != "m6.1-d1-synthetic-dev-draft" or
            authorization.get("budget_ref") != (expected_budget_ref or
                "docs/eval/m6.1-d1-personal-dev-preparation-r2/21_personal-dev-batch-budget-r1.json")):
        raise PersonalAuthorizationError("PERSONAL_BATCH_PURPOSE_OR_ASSET_MISMATCH")
    if (authorization.get("authorized_by") != "CHAT_USER_SELF_CONFIRMED" or
            authorization.get("actor_type") != "CHAT_USER_SELF_CONFIRMED"):
        raise PersonalAuthorizationError("PERSONAL_BATCH_ACTOR_MISMATCH")
    if not isinstance(authorization.get("authorization_id"), str) or not authorization["authorization_id"].strip():
        raise PersonalAuthorizationError("PERSONAL_BATCH_ID_REQUIRED")
    if authorization.get("case_ids") != case_ids or len(case_ids) != 40 or len(set(case_ids)) != 40:
        raise PersonalAuthorizationError("EXACT_40_CASE_ALLOWLIST_REQUIRED")
    for key, actual in (("asset_sha256", asset_sha256), ("code_sha256", code_sha256),
            ("code_revision", code_revision), ("database_identity", database_identity),
            ("model", model), ("provider_endpoint", endpoint),
            ("pricing_sha256", pricing_sha256_value),
            ("budget_file_sha256", budget_file_sha256_value)):
        if not isinstance(actual, str) or not actual or authorization.get(key) != actual:
            raise PersonalAuthorizationError(f"PERSONAL_BATCH_SCOPE_MISMATCH:{key}")
    if authorization.get("budget") != budget or authorization.get("budget_sha256") != sha256_json(budget):
        raise PersonalAuthorizationError("PERSONAL_BATCH_BUDGET_SCOPE_MISMATCH")
    if authorization.get("protected_assets_allowed") is not False or authorization.get("formal_gates_changed") is not False:
        raise PersonalAuthorizationError("PERSONAL_BATCH_PROTECTED_SCOPE_FORBIDDEN")
    confirmed = _utc(authorization.get("confirmed_at_utc"), "confirmed_at_utc")
    expires = _utc(authorization.get("expires_at_utc"), "expires_at_utc")
    now = now or datetime.now(timezone.utc)
    if confirmed > now or expires <= now or expires <= confirmed:
        raise PersonalAuthorizationError("PERSONAL_BATCH_EXPIRED_OR_INVALID")
    if expires - confirmed > timedelta(hours=4):
        raise PersonalAuthorizationError("PERSONAL_BATCH_VALIDITY_EXCEEDS_4H")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_json_durable(path: Path, payload: dict, *, exclusive: bool = False) -> None:
    mode = "x" if exclusive else "w"
    with path.open(mode, encoding="utf-8", newline="\n") as stream:
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def claim_personal_diagnostic_execution(*, root: Path, authorization_path: Path,
                                        authorization: dict) -> Path:
    """Durably consume the one-shot file authorization before creating a Run."""
    if not isinstance(authorization, dict) or authorization.get("status") != "USER_CONFIRMED":
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_ALREADY_USED_OR_NOT_CONFIRMED")
    case_ids = authorization.get("case_ids")
    if not isinstance(case_ids, list) or len(case_ids) != 1 or not isinstance(case_ids[0], str):
        raise PersonalAuthorizationError("SINGLE_ALLOWLISTED_CASE_REQUIRED")
    profile = personal_diagnostic_profile(
        case_ids[0], diagnostic_round_id=authorization.get("diagnostic_round_id"))
    if profile.round_id and authorization.get("diagnostic_round_id") != profile.round_id:
        raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_ROUND_MISMATCH")
    expected_auth = (root / profile.authorization_path).resolve(strict=True)
    expected_state = (root / profile.execution_state_path).resolve(strict=False)
    if authorization_path.resolve(strict=True) != expected_auth or expected_auth != (root / profile.authorization_path).absolute():
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_PATH_MISMATCH")
    raw = authorization_path.read_bytes()
    if json.loads(raw.decode("utf-8")) != authorization:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_FILE_CONTENT_CHANGED")
    if expected_state.exists():
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_ALREADY_USED_NO_REPLAY")
    root_resolved = Path(root).resolve(strict=True)
    if not expected_state.is_relative_to(root_resolved):
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_STATE_PATH_ESCAPES_ROOT")
    expected_state.parent.mkdir(parents=True, exist_ok=True)
    if expected_state.parent.resolve(strict=True) != expected_state.parent:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_STATE_PATH_ALIAS_REJECTED")
    state = {"schema_version": "1.0", "authorization_id": authorization.get("authorization_id"),
        "diagnostic_round_id": authorization.get("diagnostic_round_id"),
        "case_ids": case_ids, "status": "IN_USE", "claimed_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "authorization_file_sha256": hashlib.sha256(raw).hexdigest()}
    _write_json_durable(expected_state, state, exclusive=True)
    authorization["status"] = "IN_USE"
    authorization["execution_started_at_utc"] = state["claimed_at_utc"]
    _write_json_durable(authorization_path, authorization)
    return expected_state


def close_personal_diagnostic_execution(*, state_path: Path, authorization_path: Path,
                                        authorization: dict, status: str, outcome: dict) -> None:
    """Close the one-shot state even after a failed/unknown run; never reopen it."""
    if status not in {"CLOSED_COMPLETED", "CLOSED_STOPPED"}:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_TERMINAL_STATUS_INVALID")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if (state.get("authorization_id") != authorization.get("authorization_id") or
            state.get("status") != "IN_USE"):
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_STATE_MISMATCH")
    closed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    authorization["status"] = status
    authorization["closed_at_utc"] = closed_at
    authorization["execution_outcome"] = outcome
    _write_json_durable(authorization_path, authorization)
    state["status"] = status
    state["closed_at_utc"] = closed_at
    state["execution_outcome"] = outcome
    _write_json_durable(state_path, state)


def _utc(value, field):
    if not isinstance(value, str) or not value.strip():
        raise PersonalAuthorizationError(f"{field}_REQUIRED")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PersonalAuthorizationError(f"{field}_INVALID") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise PersonalAuthorizationError(f"{field}_TIMEZONE_REQUIRED")
    return parsed.astimezone(timezone.utc)


def pricing_sha256(path: Path) -> str:
    return file_sha256(path)


def validate_official_pricing(record: dict, *, model: str, endpoint: str) -> None:
    expected = {
        "provider": "DeepSeek",
        "api_model_id": "deepseek-flash",
        "served_model_version": "DeepSeek-V4.1-Flash-1017",
        "base_url": "https://api.deepseek.com",
        "currency": "USD",
        "unit": "per_1M_tokens",
        "peak": {"input_cache_miss": 0.30, "input_cache_hit": 0.006, "output": 1.20},
        "off_peak": {"input_cache_miss": 0.15, "input_cache_hit": 0.003, "output": 0.60},
    }
    for key, value in expected.items():
        if record.get(key) != value:
            raise PersonalAuthorizationError(f"OFFICIAL_PRICE_EVIDENCE_MISMATCH:{key}")
    if record.get("source_url") != PRICE_SOURCE or record.get("status") != "OFFICIAL_DOC_VERIFIED":
        raise PersonalAuthorizationError("OFFICIAL_PRICE_SOURCE_UNVERIFIED")
    if model != expected["api_model_id"] or endpoint.rstrip("/") != expected["base_url"]:
        raise PersonalAuthorizationError("MODEL_ENDPOINT_PRICE_BASIS_MISMATCH")
    checked_at = _utc(record.get("checked_at_utc"), "price_checked_at_utc")
    now = datetime.now(timezone.utc)
    if checked_at > now:
        raise PersonalAuthorizationError("PRICE_CHECK_TIME_IN_FUTURE")
    if now - checked_at > timedelta(hours=24):
        raise PersonalAuthorizationError("PRICE_EVIDENCE_STALE_RECHECK_REQUIRED")


def validate_personal_authorization(
    authorization: dict,
    *,
    case_id: str,
    asset_id: str,
    data_version: str,
    asset_path: str,
    asset_sha256: str,
    code_sha256: str,
    code_revision: str,
    database_identity: str,
    model: str,
    endpoint: str,
    budget: dict,
    budget_file_sha256_value: str,
    pricing_sha256_value: str,
    check_rules_sha256_value: str | None = None,
    profile: PersonalDiagnosticProfile | None = None,
    now: datetime | None = None,
) -> None:
    """Validate the full one-case boundary; never mutates Registry or issues a grant."""
    if not isinstance(authorization, dict):
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_OBJECT_REQUIRED")
    if authorization.get("schema_version") != "1.0":
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_SCHEMA_UNSUPPORTED")
    if authorization.get("authorization_type") != AUTHORIZATION_TYPE:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_TYPE_MISMATCH")
    if authorization.get("status") != "USER_CONFIRMED":
        raise PersonalAuthorizationError("FINAL_USER_CONFIRMATION_REQUIRED")
    if authorization.get("purpose") != "Single-case non-scoring personal development diagnostic; all output remains NOT_GRADED.":
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_PURPOSE_MISMATCH")
    if authorization.get("grading_status") != "NOT_GRADED":
        raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_MUST_REMAIN_NOT_GRADED")
    if authorization.get("actor_type") != "CHAT_USER_SELF_CONFIRMED":
        raise PersonalAuthorizationError("USER_CONFIRMATION_ACTOR_TYPE_REQUIRED")
    if not isinstance(authorization.get("authorization_id"), str) or not authorization["authorization_id"].strip():
        raise PersonalAuthorizationError("AUTHORIZATION_ID_REQUIRED")
    if authorization.get("authorized_by") != "CHAT_USER_SELF_CONFIRMED":
        raise PersonalAuthorizationError("USER_CONFIRMATION_IDENTITY_REQUIRED")
    expected_profile = profile or personal_diagnostic_profile(
        case_id, diagnostic_round_id=authorization.get("diagnostic_round_id"))
    if (expected_profile.case_id != case_id or authorization.get("case_ids") != [case_id]):
        raise PersonalAuthorizationError("SINGLE_ALLOWLISTED_CASE_REQUIRED")
    if expected_profile.check_rules_path:
        if authorization.get("allowed_tools") != sorted(expected_profile.allowed_tools):
            raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_CAPABILITIES_MISMATCH")
        if (not isinstance(check_rules_sha256_value, str) or not check_rules_sha256_value or
                authorization.get("check_rules_sha256") != check_rules_sha256_value):
            raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_CHECK_RULES_MISMATCH")
    if expected_profile.round_id:
        if authorization.get("diagnostic_round_id") != expected_profile.round_id:
            raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_ROUND_MISMATCH")
        prior = expected_profile.predecessor_case_id or {
            "D1-ORDER-007": "D1-POLICY-013", "D1-ORDER-009": "D1-ORDER-007"}.get(case_id)
        if prior:
            expected_report = (expected_profile.predecessor_report_path or
                (Path(expected_profile.report_dir) /
                 f"{prior}-diagnostic-check-{expected_profile.report_suffix}.json").as_posix())
            if (authorization.get("predecessor_report_path") != expected_report or
                    not isinstance(authorization.get("predecessor_report_sha256"), str) or
                    len(authorization["predecessor_report_sha256"]) != 64):
                raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_PREDECESSOR_BINDING_REQUIRED")
            try:
                int(authorization["predecessor_report_sha256"], 16)
            except ValueError as exc:
                raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_PREDECESSOR_HASH_INVALID") from exc
            if (expected_profile.predecessor_report_sha256 and
                    authorization.get("predecessor_report_sha256") != expected_profile.predecessor_report_sha256):
                raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_PREDECESSOR_HASH_NOT_ALLOWLISTED")
            if expected_profile.predecessor_case_id:
                expected_code_sha = expected_profile.predecessor_code_sha256 or code_sha256
                if (authorization.get("predecessor_case_id") != prior or
                        authorization.get("predecessor_round_id") != expected_profile.predecessor_round_id or
                        authorization.get("predecessor_code_sha256") != expected_code_sha):
                    raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_PREDECESSOR_METADATA_MISMATCH")
                expected_paths = {
                    "predecessor_report_path": expected_report,
                    "predecessor_authorization_path": expected_profile.predecessor_authorization_path,
                    "predecessor_state_path": expected_profile.predecessor_state_path,
                    "predecessor_independent_report_path": expected_profile.predecessor_independent_report_path,
                }
                for key, expected_path in expected_paths.items():
                    if not expected_path or authorization.get(key) != expected_path:
                        raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_PREDECESSOR_PATH_MISMATCH")
                for key in ("predecessor_archive_path", "predecessor_archive_sha256"):
                    if not isinstance(authorization.get(key), str) or not authorization[key]:
                        raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_PREDECESSOR_ARCHIVE_BINDING_REQUIRED")
                archive_ref = PurePosixPath(authorization["predecessor_archive_path"])
                if (archive_ref.is_absolute() or archive_ref.parts[:3] !=
                        ("docs", "eval", "personal-dev-runs") or len(archive_ref.parts) != 4 or
                        archive_ref.parts[3] in {"", ".", ".."} or archive_ref.suffix != ".json" or
                        "\\" in authorization["predecessor_archive_path"]):
                    raise PersonalAuthorizationError("PERSONAL_DIAGNOSTIC_PREDECESSOR_ARCHIVE_PATH_INVALID")
                hash_expectations = {
                    "predecessor_report_sha256": expected_profile.predecessor_report_sha256,
                    "predecessor_archive_sha256": expected_profile.predecessor_archive_sha256,
                    "predecessor_authorization_sha256": expected_profile.predecessor_authorization_sha256,
                    "predecessor_state_sha256": expected_profile.predecessor_state_sha256,
                    "predecessor_independent_report_sha256": expected_profile.predecessor_independent_report_sha256,
                    "predecessor_rules_sha256": expected_profile.predecessor_rules_sha256,
                }
                for key, pinned in hash_expectations.items():
                    value = authorization.get(key)
                    if (not isinstance(value, str) or len(value) != 64 or
                            any(ch not in "0123456789abcdef" for ch in value.lower()) or
                            (pinned and value != pinned)):
                        raise PersonalAuthorizationError(f"PERSONAL_DIAGNOSTIC_PREDECESSOR_HASH_INVALID:{key}")
                for key in ("predecessor_authorization_sha256", "predecessor_state_sha256",
                            "predecessor_independent_report_sha256", "predecessor_rules_sha256"):
                    try:
                        int(authorization[key], 16)
                    except ValueError as exc:
                        raise PersonalAuthorizationError(f"PERSONAL_DIAGNOSTIC_PREDECESSOR_HASH_INVALID:{key}") from exc
        elif authorization.get("predecessor_report_path") is not None or authorization.get("predecessor_report_sha256") is not None:
            raise PersonalAuthorizationError("FIRST_CASE_MUST_NOT_BIND_PREDECESSOR")
    if (authorization.get("asset_id") != asset_id or asset_id != "m6.1-d1-synthetic-dev-draft" or
            authorization.get("data_version") != data_version or data_version != AUTHORIZED_VERSION or
            authorization.get("asset_path") != asset_path or asset_path != AUTHORIZED_ASSET_PATH or
            authorization.get("asset_sha256") != asset_sha256 or asset_sha256 != AUTHORIZED_ASSET_SHA256):
        raise PersonalAuthorizationError("EXACT_DEV_ASSET_SCOPE_REQUIRED")
    for key, actual in (("code_sha256", code_sha256), ("code_revision", code_revision),
                        ("database_identity", database_identity), ("model", model),
                        ("provider_endpoint", endpoint), ("pricing_sha256", pricing_sha256_value)):
        if not isinstance(actual, str) or not actual or authorization.get(key) != actual:
            raise PersonalAuthorizationError(f"PERSONAL_AUTHORIZATION_SCOPE_MISMATCH:{key}")
    if authorization.get("budget") != budget:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_BUDGET_MISMATCH")
    if authorization.get("budget_sha256") != sha256_json(budget):
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_BUDGET_DIGEST_MISMATCH")
    if authorization.get("budget_file_sha256") != budget_file_sha256_value:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_BUDGET_FILE_MISMATCH")
    bounds = authorization.get("limits")
    caps = expected_profile.budget_limits or {
        "provider_calls": 4, "tool_calls": 0 if not expected_profile.allowed_tools else 4,
        "total_tokens": 120000, "estimated_usd": 0.18,
        "max_duration_seconds": 180, "max_concurrent_trials": 1, "max_retries": 0,
        "embedding_calls": 0, "judge_calls": 0,
    }
    exact_bounds = {"provider_calls": caps["provider_calls"], "tool_calls": caps["tool_calls"],
        "total_tokens": caps["total_tokens"], "estimated_usd": caps["estimated_usd"],
        "timeout_seconds": caps["max_duration_seconds"], "concurrency": caps["max_concurrent_trials"],
        "automatic_retries": caps["max_retries"], "embedding_calls": caps["embedding_calls"],
        "judge_calls": caps["judge_calls"]}
    if bounds != exact_bounds:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_LIMITS_MISMATCH")
    if authorization.get("protected_assets_allowed") is not False or authorization.get("formal_gates_changed") is not False:
        raise PersonalAuthorizationError("PROTECTED_OR_FORMAL_SCOPE_FORBIDDEN")
    confirmed = _utc(authorization.get("confirmed_at_utc"), "confirmed_at_utc")
    expires = _utc(authorization.get("expires_at_utc"), "expires_at_utc")
    now = now or datetime.now(timezone.utc)
    if confirmed > now or expires <= now or expires <= confirmed:
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_EXPIRED_OR_INVALID")
    if expires - confirmed > timedelta(hours=24):
        raise PersonalAuthorizationError("PERSONAL_AUTHORIZATION_VALIDITY_EXCEEDS_24H")


def load_price_evidence(path: Path, *, model: str, endpoint: str) -> tuple[dict, str]:
    record = json.loads(path.read_text(encoding="utf-8"))
    validate_official_pricing(record, model=model, endpoint=endpoint)
    return record, file_sha256(path)


def validate_personal_budget(limits, message: str, *, budget_config: dict,
                             profile: PersonalDiagnosticProfile | None = None) -> None:
    from .personal_dev import D1PreflightError, validate_execution_budget

    try:
        validate_execution_budget(limits, message)
    except D1PreflightError as exc:
        raise PersonalAuthorizationError(str(exc)) from exc
    actual = limits.to_dict()
    if actual != budget_config:
        raise PersonalAuthorizationError("PERSONAL_DEV_BUDGET_CONFIG_MISMATCH")
    expected_tool_calls = 0 if profile is not None and not profile.allowed_tools else 4
    expected = profile.budget_limits if profile is not None and profile.budget_limits is not None else {
        "provider_calls": 4, "embedding_calls": 0, "judge_calls": 0,
        "total_tokens": 120000, "estimated_usd": 0.18, "tool_calls": expected_tool_calls,
        "max_calls_per_trial": expected_tool_calls, "max_concurrent_trials": 1,
        "max_duration_seconds": 180, "max_retries": 0}
    if actual != expected:
        raise PersonalAuthorizationError("PERSONAL_DEV_BUDGET_LIMITS_INVALID")
