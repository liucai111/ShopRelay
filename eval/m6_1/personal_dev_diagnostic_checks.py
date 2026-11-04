"""Versioned, non-scoring checks for personal D1 diagnostic archives.

Rules are checker-only inputs. They are never passed to the Router, Agent, or tools.
"""
from __future__ import annotations

import hashlib
import json
import math
import numbers
import re
from pathlib import Path
from typing import Any


class DiagnosticCheckError(ValueError):
    pass


CHECKER_VERSION = "m6.1-d1-diagnostic-checker-r4"
THREE_CASES = {"D1-POLICY-013", "D1-ORDER-007", "D1-ORDER-009"}
CROSSAGENT030_CASES = {"D1-CROSSAGENT-030"}
ALLOWED_CASES = THREE_CASES | CROSSAGENT030_CASES
STATUSES = {"PASS", "FAIL", "UNKNOWN", "NOT_APPLICABLE"}
SEQUENCE_ORDER = ("D1-POLICY-013", "D1-ORDER-007", "D1-ORDER-009")


def require_diagnostic_predecessor(root: str | Path, *, case_id: str,
                                   profile=None, authorization: dict | None = None) -> None:
    """Require the profile's exact hash-bound real predecessor before admission."""
    if case_id not in SEQUENCE_ORDER:
        return
    position = SEQUENCE_ORDER.index(case_id)
    if position == 0:
        return
    from .personal_dev_authorization import personal_diagnostic_profile
    profile = profile or personal_diagnostic_profile(case_id)
    if not profile.round_id or not profile.report_dir or not profile.report_suffix:
        raise DiagnosticCheckError("DIAGNOSTIC_PREDECESSOR_ROUND_PROFILE_REQUIRED")
    prior = profile.predecessor_case_id or SEQUENCE_ORDER[position - 1]
    if prior != SEQUENCE_ORDER[position - 1]:
        raise DiagnosticCheckError("DIAGNOSTIC_PREDECESSOR_CASE_NOT_ALLOWLISTED")
    predecessor_round = profile.predecessor_round_id or profile.round_id
    prior_profile = personal_diagnostic_profile(prior, diagnostic_round_id=predecessor_round)
    report_rel = (profile.predecessor_report_path or
        (Path(profile.report_dir) / f"{prior}-diagnostic-check-{profile.report_suffix}.json").as_posix())
    report_path = Path(root) / report_rel
    if (authorization is None or
            authorization.get("predecessor_report_path") != report_rel or
            not isinstance(authorization.get("predecessor_report_sha256"), str)):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_BINDING_REQUIRED:{prior}")
    if (profile.predecessor_report_sha256 and
            authorization.get("predecessor_report_sha256") != profile.predecessor_report_sha256):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_HASH_NOT_ALLOWLISTED:{prior}")
    if profile.predecessor_case_id and (
            authorization.get("predecessor_case_id") != prior or
            authorization.get("predecessor_round_id") != predecessor_round):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_METADATA_MISMATCH:{prior}")
    if (report_path.is_symlink() or not report_path.is_file() or
            report_path.resolve().parent != (Path(root) / prior_profile.report_dir).resolve()):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_REPORT_UNAVAILABLE:{prior}")
    try:
        report_bytes = report_path.read_bytes()
        report = json.loads(report_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_REPORT_UNAVAILABLE:{prior}") from exc
    report_sha = hashlib.sha256(report_bytes).hexdigest()
    if report_sha != authorization.get("predecessor_report_sha256"):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_REPORT_HASH_MISMATCH:{prior}")
    if profile.predecessor_report_sha256 and report_sha != profile.predecessor_report_sha256:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_REPORT_HASH_NOT_ALLOWLISTED:{prior}")
    checks = _dict(report.get("checks"))
    decision = _dict(checks.get("decision"))
    cleanup = _dict(report.get("cleanup_evidence"))
    try:
        rules_path = Path(root) / prior_profile.check_rules_path
        if rules_path.is_symlink():
            raise DiagnosticCheckError("LOCKED_CHECK_RULES_PATH_INVALID")
        rules, rules_sha = load_locked_rules(
            rules_path,
            case_id=prior)
        expected_predecessor_checker = (profile.predecessor_checker_version or CHECKER_VERSION)
        validate_diagnostic_check_result(checks, decision_policy=rules["decision_policy"],
            expected_checker_version=expected_predecessor_checker)
    except DiagnosticCheckError as exc:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_REPORT_INVALID:{prior}") from exc
    if profile.predecessor_rules_sha256 and rules_sha != profile.predecessor_rules_sha256:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_RULES_HASH_MISMATCH:{prior}")
    prior_auth_rel = profile.predecessor_authorization_path or prior_profile.authorization_path
    prior_state_rel = profile.predecessor_state_path or prior_profile.execution_state_path
    prior_auth_path = Path(root) / prior_auth_rel
    prior_state_path = Path(root) / prior_state_rel
    if prior_auth_path.is_symlink() or prior_state_path.is_symlink():
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_TERMINAL_RECORD_PATH_INVALID:{prior}")
    try:
        prior_auth_bytes = prior_auth_path.read_bytes()
        prior_state_bytes = prior_state_path.read_bytes()
        prior_auth = json.loads(prior_auth_bytes.decode("utf-8"))
        prior_state = json.loads(prior_state_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_TERMINAL_RECORD_UNAVAILABLE:{prior}") from exc
    prior_auth_sha = hashlib.sha256(prior_auth_bytes).hexdigest()
    prior_state_sha = hashlib.sha256(prior_state_bytes).hexdigest()
    if (profile.predecessor_authorization_sha256 and
            prior_auth_sha != profile.predecessor_authorization_sha256):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_AUTHORIZATION_HASH_MISMATCH:{prior}")
    if profile.predecessor_state_sha256 and prior_state_sha != profile.predecessor_state_sha256:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_STATE_HASH_MISMATCH:{prior}")
    outcome = _dict(prior_auth.get("execution_outcome"))
    state_outcome = _dict(prior_state.get("execution_outcome"))
    archive_rel = report.get("archive_path")
    if profile.predecessor_archive_path and archive_rel != profile.predecessor_archive_path:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_ARCHIVE_PATH_MISMATCH:{prior}")
    archive_path = Path(root) / archive_rel if isinstance(archive_rel, str) else None
    if (archive_path is None or Path(archive_rel).is_absolute() or archive_path.is_symlink() or
            archive_path.suffix != ".json" or
            archive_path.resolve().parent != (Path(root) / "docs/eval/personal-dev-runs").resolve()):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_ARCHIVE_PATH_INVALID:{prior}")
    try:
        archive_bytes = archive_path.read_bytes()
        archive_sha = hashlib.sha256(archive_bytes).hexdigest()
        archive = json.loads(archive_bytes.decode("utf-8"))
    except OSError as exc:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_ARCHIVE_UNAVAILABLE:{prior}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_ARCHIVE_INVALID:{prior}") from exc
    archived_result = _dict(archive.get("result"))
    archived_app = _dict(archived_result.get("result"))
    observations = _dict(archived_result.get("observations"))
    run_rows = _rows(observations.get("runs"))
    event_rows = _rows(observations.get("run_events"))
    archive_run_id, archive_trace_id, archive_ids_consistent = _run_identity(
        archived_result, archived_app, run_rows, event_rows)
    report_run_id = checks.get("run_id")
    report_trace_id = checks.get("trace_id")
    expected_code_sha = profile.predecessor_code_sha256 or authorization.get("code_sha256")
    independent_rel = (profile.predecessor_independent_report_path or
        (Path(prior_profile.report_dir) /
         f"{prior}-independent-check-{prior_profile.report_suffix}.json").as_posix())
    independent_path = Path(root) / independent_rel
    if independent_path.is_symlink() or not independent_path.is_file() or \
            independent_path.resolve().parent != (Path(root) / prior_profile.report_dir).resolve():
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_INDEPENDENT_CHECK_UNAVAILABLE:{prior}")
    try:
        independent_bytes = independent_path.read_bytes()
        independent = json.loads(independent_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_INDEPENDENT_CHECK_UNAVAILABLE:{prior}") from exc
    independent_sha = hashlib.sha256(independent_bytes).hexdigest()
    if (profile.predecessor_independent_report_sha256 and
            independent_sha != profile.predecessor_independent_report_sha256):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_INDEPENDENT_CHECK_HASH_MISMATCH:{prior}")
    independent_matches = (
        independent.get("classification") == "PROVISIONAL_DEV_CHECKS" and
        independent.get("reference_review_status") == "reference_unreviewed" and
        independent.get("grading_status") == "NOT_GRADED" and
        independent.get("case_id") == prior and
        independent.get("diagnostic_round_id") == prior_profile.round_id and
        independent.get("status") == "RECOMPUTED_CONSISTENT" and
        independent.get("matches_execution_report") is True and
        independent.get("execution_report_path") == report_rel and
        independent.get("execution_report_sha256") == report_sha and
        independent.get("raw_archive_sha256") == archive_sha and
        independent.get("rules_sha256") == rules_sha and
        independent.get("recomputed_checks") == checks
    )
    if predecessor_round == profile.round_id:
        independent_matches = independent_matches and (
            independent.get("code_sha256") == authorization.get("code_sha256") and
            independent.get("database_identity") == authorization.get("database_identity"))
    if not independent_matches:
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_INDEPENDENT_RECOMPUTATION_MISMATCH:{prior}")

    explicit_binding_matches = True
    if profile.predecessor_case_id:
        explicit_binding_matches = (
            authorization.get("predecessor_archive_path") == archive_rel and
            authorization.get("predecessor_archive_sha256") == archive_sha and
            authorization.get("predecessor_authorization_path") == prior_auth_rel and
            authorization.get("predecessor_authorization_sha256") == prior_auth_sha and
            authorization.get("predecessor_state_path") == prior_state_rel and
            authorization.get("predecessor_state_sha256") == prior_state_sha and
            authorization.get("predecessor_independent_report_path") == independent_rel and
            authorization.get("predecessor_independent_report_sha256") == independent_sha and
            authorization.get("predecessor_rules_sha256") == rules_sha and
            authorization.get("predecessor_code_sha256") == expected_code_sha
        )

    if profile.predecessor_manifest_path:
        manifest_rel = profile.predecessor_manifest_path
        manifest_path = Path(root) / manifest_rel
        if manifest_path.is_symlink():
            raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_MANIFEST_PATH_INVALID:{prior}")
        try:
            manifest_bytes = manifest_path.read_bytes()
        except OSError as exc:
            raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_MANIFEST_UNAVAILABLE:{prior}") from exc
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        if manifest_sha != profile.predecessor_manifest_sha256:
            raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_MANIFEST_HASH_MISMATCH:{prior}")
        manifest_entries: dict[str, str] = {}
        for line in manifest_bytes.decode("utf-8").splitlines():
            parts = line.strip().split()
            if len(parts) >= 2 and len(parts[0]) == 64 and re.fullmatch(r"[0-9a-fA-F]{64}", parts[0]):
                manifest_entries[parts[1]] = parts[0].lower()
        expected_manifest_rows = {
            report_rel: profile.predecessor_report_sha256,
            profile.predecessor_archive_path: profile.predecessor_archive_sha256,
            prior_auth_rel: profile.predecessor_authorization_sha256,
            prior_state_rel: profile.predecessor_state_sha256,
            independent_rel: profile.predecessor_independent_report_sha256,
            profile.predecessor_closeout_path: profile.predecessor_closeout_sha256,
            profile.predecessor_closeout_markdown_path: profile.predecessor_closeout_markdown_sha256,
        }
        for rel, expected in expected_manifest_rows.items():
            if rel and expected and manifest_entries.get(rel) != expected:
                raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_MANIFEST_ENTRY_MISMATCH:{prior}")
        closeout_path = Path(root) / profile.predecessor_closeout_path
        closeout_md_path = Path(root) / profile.predecessor_closeout_markdown_path
        if closeout_path.is_symlink() or closeout_md_path.is_symlink():
            raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_CLOSEOUT_PATH_INVALID:{prior}")
        try:
            closeout_bytes = closeout_path.read_bytes()
            closeout_md_bytes = closeout_md_path.read_bytes()
            closeout = json.loads(closeout_bytes.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_CLOSEOUT_UNAVAILABLE:{prior}") from exc
        if (hashlib.sha256(closeout_bytes).hexdigest() != profile.predecessor_closeout_sha256 or
                hashlib.sha256(closeout_md_bytes).hexdigest() != profile.predecessor_closeout_markdown_sha256 or
                closeout.get("authorization", {}).get("status") != "CLOSED_COMPLETED" or
                closeout.get("authorization", {}).get("execution_state_status") != "CLOSED_COMPLETED" or
                closeout.get("authorization", {}).get("replay_prevented_by_state_file") is not True or
                closeout.get("authorization", {}).get("authorization_id") != report.get("authorization_id") or
                closeout.get("diagnostic_checks", {}).get("decision", {}).get("outcome") != "CONTINUE" or
                closeout.get("scope", {}).get("run_id") != report_run_id or
                closeout.get("scope", {}).get("trace_id") != report_trace_id or
                closeout.get("baseline", {}).get("r6_final_implementation_digest") != profile.predecessor_code_sha256 or
                closeout.get("baseline", {}).get("database_identity") != authorization.get("database_identity") or
                closeout.get("scope", {}).get("dataset_sha256") != authorization.get("asset_sha256") or
                closeout.get("case_execution", {}).get("result_archive") != archive_rel or
                closeout.get("case_execution", {}).get("diagnostic_report") != report_rel or
                closeout.get("case_execution", {}).get("status") != "SUCCEEDED" or
                closeout.get("diagnostic_checks", {}).get("recomputation_path") != independent_rel or
                closeout.get("diagnostic_checks", {}).get("recomputation_matches_execution_report") is not True):
            raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_CLOSEOUT_BINDING_MISMATCH:{prior}")

    if (not explicit_binding_matches or
            report.get("phase") != "EXECUTION_CLOSEOUT" or report.get("replay_only") is True or
            report.get("classification") != "PROVISIONAL_DEV_CHECKS" or
            report.get("reference_review_status") != "reference_unreviewed" or
            report.get("grading_status") != "NOT_GRADED" or
            report.get("case_id") != prior or checks.get("checker_version") != CHECKER_VERSION or
            report.get("diagnostic_round_id") != prior_profile.round_id or
            report.get("code_sha256") != expected_code_sha or
            prior_auth.get("code_sha256") != expected_code_sha or
            prior_auth.get("diagnostic_round_id") != prior_profile.round_id or
            profile.predecessor_case_id and (
                prior_auth.get("case_ids") != [prior] or
                prior_auth.get("authorization_type") != "PERSONAL_DEV_DIAGNOSTIC_NONSCORING" or
                prior_auth.get("authorized_by") != "CHAT_USER_SELF_CONFIRMED" or
                prior_auth.get("actor_type") != "CHAT_USER_SELF_CONFIRMED" or
                prior_auth.get("allowed_tools") != sorted(prior_profile.allowed_tools) or
                prior_auth.get("model") != authorization.get("model") or
                prior_auth.get("provider_endpoint") != authorization.get("provider_endpoint") or
                prior_auth.get("pricing_sha256") != authorization.get("pricing_sha256")) or
            report.get("rules_sha256") != rules_sha or
            report.get("authorization_id") != prior_auth.get("authorization_id") or
            prior_auth.get("status") != "CLOSED_COMPLETED" or
            prior_state.get("status") != "CLOSED_COMPLETED" or
            prior_state.get("authorization_id") != prior_auth.get("authorization_id") or
            prior_state.get("diagnostic_round_id") != prior_profile.round_id or
            prior_auth.get("asset_sha256") != authorization.get("asset_sha256") or
            prior_auth.get("database_identity") != authorization.get("database_identity") or
            outcome.get("diagnostic_decision") != "CONTINUE" or
            outcome.get("case_id") != prior or
            outcome.get("diagnostic_check_report") != report_rel or
            outcome.get("diagnostic_check_report_sha256") != report_sha or
            outcome.get("run_id") != checks.get("run_id") or
            outcome.get("run_status") != checks.get("run_status") or
            outcome.get("cleanup_status") != "CLEANED_AND_RELEASED" or
            outcome.get("result_archive") != archive_rel or
            outcome.get("result_archive_sha256") != archive_sha or
            state_outcome.get("diagnostic_check_report_sha256") != report_sha or
            state_outcome.get("diagnostic_check_report") != report_rel or
            state_outcome.get("run_id") != checks.get("run_id") or
            state_outcome.get("diagnostic_decision") != "CONTINUE" or
            state_outcome.get("cleanup_status") != "CLEANED_AND_RELEASED" or
            not isinstance(archive_rel, str) or
            profile.predecessor_archive_sha256 and archive_sha != profile.predecessor_archive_sha256 or
            archive_sha != report.get("archive_sha256") or
            archive_run_id != report_run_id or archive_trace_id != report_trace_id or
            not archive_ids_consistent or len([row for row in run_rows if row.get("run_id") == report_run_id]) != 1 or
            archive.get("case_id") != prior or archived_result.get("run_id") != checks.get("run_id") or
            archived_result.get("status") != checks.get("run_status") or
            decision.get("outcome") != "CONTINUE" or
            cleanup.get("cleanup_status") != "CLEANED_AND_RELEASED" or
            cleanup.get("lease_released") is not True or
            cleanup.get("pending_actions") != 0 or cleanup.get("residual_case_rows") != 0 or
            cleanup.get("database_identity") != authorization.get("database_identity")):
        raise DiagnosticCheckError(f"DIAGNOSTIC_PREDECESSOR_DID_NOT_CONTINUE:{prior}")


def validate_diagnostic_check_result(checks: dict, *, decision_policy: dict,
                                     expected_checker_version: str = CHECKER_VERSION) -> None:
    """Validate completeness, status enums, and exact decision/blocker consistency."""
    if (not isinstance(checks, dict) or checks.get("checker_version") != expected_checker_version or
            checks.get("classification") != "PROVISIONAL_DEV_CHECKS" or
            checks.get("reference_review_status") != "reference_unreviewed" or
            checks.get("grading_status") != "NOT_GRADED" or
            not isinstance(checks.get("check_items"), list) or
            not isinstance(checks.get("decision"), dict) or
            checks.get("task_outcome") != "UNKNOWN"):
        raise DiagnosticCheckError("DIAGNOSTIC_CHECK_REPORT_FIELDS_INCOMPLETE")
    seen: set[str] = set()
    for item in checks["check_items"]:
        if (not isinstance(item, dict) or not isinstance(item.get("id"), str) or
                item["id"] in seen or item.get("status") not in STATUSES or
                not isinstance(item.get("blocking"), bool) or not isinstance(item.get("reason"), str)):
            raise DiagnosticCheckError("DIAGNOSTIC_CHECK_ITEM_INVALID")
        seen.add(item["id"])
    expected = _decision(checks["check_items"], decision_policy)
    actual = checks["decision"]
    if (actual.get("outcome") != expected["outcome"] or
            actual.get("blocking_items") != expected["blocking_items"] or
            actual.get("review_items") != expected["review_items"] or
            actual.get("reasons") != expected["reasons"] or
            checks.get("stop_reasons") != expected["reasons"]):
        raise DiagnosticCheckError("DIAGNOSTIC_DECISION_BLOCKER_REASON_MISMATCH")


def load_locked_rules(path: str | Path, *, case_id: str) -> tuple[dict, str]:
    """Load one versioned fixed-case rule set without widening existing profiles."""
    p = Path(path)
    try:
        raw_bytes = p.read_bytes()
        rules = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DiagnosticCheckError("LOCKED_CHECK_RULES_UNAVAILABLE_OR_INVALID") from exc
    rule_set_id = rules.get("rule_set_id") if isinstance(rules, dict) else None
    expected_cases = (THREE_CASES if rule_set_id == "m6.1-d1-three-case-provisional-checks-r2" else
        CROSSAGENT030_CASES if rule_set_id == "m6.2-crossagent030-provisional-checks-r1" else set())
    if (not isinstance(rules, dict) or rules.get("schema_version") != "2.0" or
            rules.get("classification") != "PROVISIONAL_DEV_CHECKS" or
            rules.get("reference_review_status") != "reference_unreviewed" or
            rules.get("grading_status") != "NOT_GRADED" or
            set(rules.get("cases", {})) != expected_cases or case_id not in expected_cases or
            case_id not in rules["cases"]):
        raise DiagnosticCheckError("LOCKED_CHECK_RULES_SCOPE_INVALID")
    decision_policy = rules.get("decision_policy") or {}
    if (decision_policy.get("schema_version") != "1.0" or
            decision_policy.get("outcomes") != ["CONTINUE", "STOP", "REVIEW_REQUIRED"] or
            decision_policy.get("block_statuses") != ["FAIL", "UNKNOWN"] or
            decision_policy.get("not_applicable_is_missing_evidence") is not False):
        raise DiagnosticCheckError("LOCKED_DECISION_POLICY_INVALID")
    for selected_case, config in rules["cases"].items():
        if (not isinstance(config, dict) or not isinstance(config.get("allowed_business_tools"), list) or
                not isinstance(config.get("allowed_route_strategies"), list) or
                not isinstance(config.get("max_read_only_tool_calls"), int) or
                config["max_read_only_tool_calls"] < 0 or
                not isinstance(config.get("max_provider_requests"), int) or
                config["max_provider_requests"] < 0):
            raise DiagnosticCheckError(f"LOCKED_CASE_RULE_INVALID:{selected_case}")
    return rules, hashlib.sha256(raw_bytes).hexdigest()


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _rows(value: Any) -> list[dict]:
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _json_payload(event: dict) -> dict:
    payload = event.get("payload")
    if isinstance(payload, dict):
        return payload
    raw = event.get("payload_json")
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return {}


def _archive_parts(archived: dict) -> tuple[dict, dict, list[dict], list[dict]]:
    root = _dict(archived)
    outer = _dict(root.get("result"))
    app = _dict(outer.get("result"))
    observations = _dict(outer.get("observations"))
    runs = _rows(observations.get("runs"))
    events = _rows(observations.get("run_events"))
    return outer, app, runs, events


def _check(status: str, reason: str, *, blocking: bool = True, category: str = "") -> dict:
    if status not in STATUSES:
        raise DiagnosticCheckError("CHECK_STATUS_INVALID")
    return {"status": status, "reason": reason, "blocking": bool(blocking), "category": category}


def _event_rows(events: list[dict], event_type: str | None = None) -> list[tuple[dict, dict]]:
    selected = []
    for event in events:
        if event_type and event.get("event_type") != event_type:
            continue
        selected.append((event, _json_payload(event)))
    return selected


def _run_identity(outer: dict, app: dict, runs: list[dict], events: list[dict]) -> tuple[str | None, str | None, bool]:
    run_id = outer.get("run_id")
    if not isinstance(run_id, str):
        run_id = app.get("run_id") if isinstance(app.get("run_id"), str) else None
    trace_id = app.get("trace_id") if isinstance(app.get("trace_id"), str) else None
    for event in events:
        if event.get("run_id") and run_id and event["run_id"] != run_id:
            return run_id, trace_id, False
        if event.get("trace_id") and trace_id and event["trace_id"] != trace_id:
            return run_id, trace_id, False
    return run_id, trace_id, True


def _route_strategy(app: dict, events: list[dict]) -> str | None:
    route = _dict(app.get("route"))
    value = route.get("strategy") or app.get("strategy")
    if isinstance(value, str):
        return value
    for _event, payload in _event_rows(events, "route_decided"):
        if isinstance(payload.get("strategy"), str):
            return payload["strategy"]
    return None


def _route_provider_diagnostic(app: dict, events: list[dict]) -> dict:
    """Separate provider/route transport failures from valid LLM DIRECT decisions."""
    event_routes = [payload for _event, payload in _event_rows(events, "route_decided")]
    route = dict(event_routes[0]) if event_routes else {}
    route.update(_dict(app.get("route")))
    app_route = _dict(app.get("route"))
    if not app_route.get("provider_diagnostic") and event_routes:
        route["provider_diagnostic"] = event_routes[0].get("provider_diagnostic")
    if not app_route.get("fallback_error") and event_routes:
        route["fallback_error"] = event_routes[0].get("fallback_error")
    source = route.get("decision_source")
    strategy = route.get("strategy")
    reason = route.get("reason_code")
    diagnostic = _dict(route.get("provider_diagnostic"))

    if source == "FALLBACK" and reason == "LLM_CLASSIFICATION_FALLBACK":
        phase = diagnostic.get("failure_phase")
        categories = {"LOCAL_GUARD", "LOCAL_BUDGET_GUARD", "DNS_FAILURE", "TLS_FAILURE",
                      "CONNECT_FAILURE", "TIMEOUT", "HTTP_ERROR", "RESPONSE_INVALID", "UNKNOWN"}
        phases = {"PRE_REQUEST_LOCAL_FAILURE", "CONNECTION_NO_RESPONSE", "TIMEOUT_NO_RESPONSE",
                  "HTTP_RESPONSE_RECEIVED", "RESPONSE_PARSE_OR_CONTRACT_FAILURE", "FAILURE_PHASE_UNKNOWN"}
        if (diagnostic.get("schema_version") == "provider-failure-diagnostic-v1" and
                phase in phases and diagnostic.get("error_category") in categories):
            return {
                "classification": "PROVIDER_FAILURE_FALLBACK",
                "source": "persisted_structured_route_diagnostic",
                "failure_phase": phase,
                "error_category": diagnostic["error_category"],
                "completion_call_entered": diagnostic.get("completion_call_entered"),
                "provider_response_received": diagnostic.get("provider_response_received"),
                "request_delivery": diagnostic.get("request_delivery", "UNKNOWN"),
                "exception_types": [x for x in diagnostic.get("exception_types", [])
                                    if isinstance(x, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", x)][:4],
                "errno": (diagnostic.get("errno") if isinstance(diagnostic.get("errno"), int) and
                          not isinstance(diagnostic.get("errno"), bool) and
                          -32768 <= diagnostic.get("errno") <= 65535 else None),
                "timeout_stage": diagnostic.get("timeout_stage"),
                "http_status": diagnostic.get("http_status"),
                "usage_status": diagnostic.get("usage_status", "UNKNOWN"),
                "billing_status": diagnostic.get("billing_status", "UNKNOWN"),
                "fallback_route_strategy": strategy or "UNKNOWN",
                "interpretation": "Fallback route is not a model-returned classification.",
            }

        # Earlier archives stored only a safe exception type in fallback_error.
        # Preserve their limits instead of inventing request-stage or usage data.
        fallback = route.get("fallback_error")
        tokens = fallback.split(":") if isinstance(fallback, str) else []
        if "APIConnectionError" in tokens:
            return {
                "classification": "PROVIDER_CONNECTION_FAILURE_RECORDED",
                "source": "legacy_fallback_exception_type_only",
                "failure_phase": "CONNECTION_NO_RESPONSE",
                "error_category": "CONNECT_FAILURE",
                "completion_call_entered": True,
                "provider_response_received": False,
                "request_delivery": "UNKNOWN",
                "exception_types": ["APIConnectionError"],
                "errno": None,
                "timeout_stage": None,
                "http_status": None,
                "usage_status": "UNKNOWN",
                "billing_status": "UNKNOWN",
                "fallback_route_strategy": strategy or "UNKNOWN",
                "interpretation": "Recorded connection exception; errno and transport stage were not persisted.",
            }
        return {
            "classification": "PROVIDER_FAILURE_RECORDED",
            "source": "legacy_fallback_metadata",
            "failure_phase": "FAILURE_PHASE_UNKNOWN",
            "error_category": "UNKNOWN",
            "completion_call_entered": "UNKNOWN",
            "provider_response_received": "UNKNOWN",
            "request_delivery": "UNKNOWN",
            "exception_types": [], "errno": None, "timeout_stage": None,
            "http_status": None, "usage_status": "UNKNOWN", "billing_status": "UNKNOWN",
            "fallback_route_strategy": strategy or "UNKNOWN",
            "interpretation": "Fallback was recorded; persisted fields do not identify the failure phase.",
        }
    if source == "LLM":
        return {
            "classification": "MODEL_RESPONSE",
            "source": "persisted_route_decision",
            "model_strategy": strategy or "UNKNOWN",
            "provider_response_received": True,
            "usage_status": "RECORDED" if isinstance(route.get("usage"), dict) else "UNKNOWN",
            "billing_status": "UNKNOWN",
            "interpretation": "The strategy is a model response, including when that response is DIRECT.",
        }
    if source == "RULE":
        return {"classification": "DETERMINISTIC_RULE_DECISION", "source": "persisted_route_decision",
                "model_strategy": strategy or "UNKNOWN", "provider_response_received": "NOT_APPLICABLE",
                "usage_status": "NOT_APPLICABLE", "billing_status": "NOT_APPLICABLE"}
    return {"classification": "UNKNOWN", "source": "persisted_route_decision_unavailable",
            "model_strategy": strategy or "UNKNOWN", "provider_response_received": "UNKNOWN",
            "usage_status": "UNKNOWN", "billing_status": "UNKNOWN"}


def _policy_rows(app: dict) -> tuple[list[dict], list[dict], dict, dict]:
    citations = _rows(app.get("citations"))
    evidence = _rows(app.get("evidence"))
    policy = _dict(app.get("policy_retrieval"))
    retrieval = _dict(app.get("retrieval"))
    return citations, evidence, policy, retrieval


def _policy_audit_from_app(app: dict) -> list[dict]:
    return _rows(app.get("policy_retrieval_audit"))


def _policy_event_linkage(events: list[dict], run_id: str | None, trace_id: str | None) -> tuple[str, list[dict]]:
    starts = [p for _e, p in _event_rows(events, "policy_retrieval_started")]
    finishes = [p for _e, p in _event_rows(events, "policy_retrieval_finished")]
    if not starts and not finishes:
        return "UNKNOWN", []
    if len(starts) != len(finishes):
        return "FAIL", finishes
    by_id = {p.get("call_id"): p for p in starts if isinstance(p.get("call_id"), str)}
    if len(by_id) != len(starts):
        return "FAIL", finishes
    for end in finishes:
        call_id = end.get("call_id")
        start = by_id.get(call_id)
        if (not start or not end.get("result_ref") or not end.get("parameter_fingerprint") or
                end.get("status") != "SUCCEEDED" or end.get("executed") is not True or
                start.get("result_ref") != end.get("result_ref") or
                start.get("parameter_fingerprint") != end.get("parameter_fingerprint") or
                start.get("run_id") != run_id or end.get("run_id") != run_id or
                start.get("trace_id") != trace_id or end.get("trace_id") != trace_id):
            return "FAIL", finishes
    return "PASS", finishes


def _policy_fact_checks(app: dict, config: dict) -> tuple[dict[str, dict], list[dict]]:
    expected = config["expected_fixture"]
    citations, evidence, policy, retrieval = _policy_rows(app)
    ids = {row.get("chunk_id") or row.get("evidence_id") or row.get("id") for row in citations + evidence}
    ids.discard(None)
    expected_id = expected["evidence_id"]
    id_present = expected_id in ids
    policy_texts = [str(row.get("content") or row.get("text") or row.get("excerpt") or "")
        for row in citations + evidence
        if (row.get("chunk_id") or row.get("evidence_id") or row.get("id")) == expected_id]
    window_present = any(re.search(
        rf"\b{int(expected['fictional_window_days'])}\s*(?:calendar\s*)?days?\b|{int(expected['fictional_window_days'])}\s*天",
        text, re.IGNORECASE) for text in policy_texts if text)
    kb_versions = {row.get("knowledge_version") for row in citations + evidence if row.get("knowledge_version")}
    kb_version = policy.get("knowledge_version") or retrieval.get("knowledge_version") or app.get("knowledge_version")
    if kb_version:
        kb_versions.add(kb_version)
    doc_versions = set()
    for row in citations + evidence:
        version = row.get("policy_document_version") or row.get("policy_version") or row.get("version")
        if version:
            doc_versions.add(version)
    fingerprints = {row.get("index_fingerprint") for row in citations + evidence if row.get("index_fingerprint")}
    for value in (policy.get("index_fingerprint"), retrieval.get("index_fingerprint")):
        if value:
            fingerprints.add(value)
    eval_at = policy.get("evaluation_at") or retrieval.get("evaluation_at")
    doc_bounds = [row for row in citations + evidence if (row.get("chunk_id") or row.get("id")) == expected_id]
    times_ok = None
    if eval_at and doc_bounds:
        bounds = doc_bounds[0]
        start, end = bounds.get("effective_from"), bounds.get("effective_to")
        times_ok = bool(start and end and str(start) <= str(eval_at) < str(end))
    factual = {
        "fictional_policy_evidence_present": _check("PASS" if id_present and window_present else
            ("FAIL" if id_present and policy_texts else "UNKNOWN"),
            "Expected fixture evidence id and the stated window are checked from the persisted policy citation/evidence projection."),
        "knowledge_version_matches_fixture": _check("PASS" if kb_version == expected["knowledge_version"] and kb_versions <= {expected["knowledge_version"]} else ("FAIL" if kb_version or kb_versions else "UNKNOWN"),
            "Knowledge/index version is checked separately from the policy document version."),
        "policy_document_version_matches_fixture": _check("PASS" if doc_versions == {expected["policy_document_version"]} else ("FAIL" if doc_versions else "UNKNOWN"),
            "Policy document version is checked from citations/evidence, not inferred from the knowledge index."),
        "index_fingerprint_consistent": _check("PASS" if len(fingerprints) == 1 else ("FAIL" if len(fingerprints) > 1 else "UNKNOWN"),
            "Index fingerprint is distinct metadata and must be internally consistent."),
        "evaluation_time_within_policy_effective_window": _check("PASS" if times_ok is True else ("FAIL" if times_ok is False else "UNKNOWN"),
            "The evaluation timestamp is compared with the cited policy document validity interval."),
    }
    return factual, doc_bounds


def _tool_events(events: list[dict]) -> tuple[list[dict], list[dict]]:
    starts, finishes = [], []
    for event, payload in _event_rows(events):
        if event.get("event_type") == "tool_started":
            starts.append(payload)
        elif event.get("event_type") == "tool_finished":
            finishes.append(payload)
    return starts, finishes


def _budget_check(snapshot: dict | None, config: dict, *, observed_tool_calls: int | None,
                  tool_event_status: str) -> dict:
    if not isinstance(snapshot, dict):
        return _check("UNKNOWN", "No persisted budget ledger snapshot supplied.")
    if (snapshot.get("budget_snapshot_schema_version") != "m6.1-budget-snapshot-v2" or
            snapshot.get("complete") is not True or snapshot.get("settled") is not True or
            snapshot.get("snapshot_kind") != "FINAL" or snapshot.get("active_reservations") != 0 or
            snapshot.get("integrity_errors") != []):
        return _check("UNKNOWN", "Budget snapshot is legacy, incomplete, corrupt, or has unsettled reservations.")
    calls_by_category = _dict(snapshot.get("calls_by_category"))
    categories = ("provider", "tool", "embedding", "judge")
    if any(category not in calls_by_category or isinstance(calls_by_category.get(category), bool) or
           not isinstance(calls_by_category.get(category), int) or calls_by_category[category] < 0
           for category in categories):
        return _check("UNKNOWN", "Final snapshot must explicitly record every non-negative call category, including zero.")
    calls = calls_by_category["provider"]
    tool_calls = calls_by_category["tool"]
    tokens = snapshot.get("tokens_conservative_budget_consumption")
    estimated_usd = snapshot.get("estimated_usd_conservative_budget_consumption")
    embedding = calls_by_category["embedding"]
    judge = calls_by_category["judge"]
    numeric = lambda value: isinstance(value, numbers.Real) and not isinstance(value, bool) and math.isfinite(float(value))
    if (not numeric(tokens) or float(tokens) < 0 or not numeric(estimated_usd) or float(estimated_usd) < 0 or
            isinstance(snapshot.get("provider_requests"), bool) or not isinstance(snapshot.get("provider_requests"), int) or
            snapshot.get("provider_requests") != calls or
            isinstance(snapshot.get("embedding_requests"), bool) or not isinstance(snapshot.get("embedding_requests"), int) or
            snapshot.get("embedding_requests") != embedding or
            isinstance(snapshot.get("judge_requests"), bool) or not isinstance(snapshot.get("judge_requests"), int) or
            snapshot.get("judge_requests") != judge):
        return _check("UNKNOWN", "Ledger lacks complete bounded usage counters; missing usage cannot be inferred as zero.")
    if tool_event_status != "PASS" or observed_tool_calls is None:
        return _check("UNKNOWN", "Persisted business-tool event evidence is incomplete or inconsistent with its event pairs.")
    if tool_calls != observed_tool_calls:
        return _check("FAIL", "Committed tool-call count conflicts with persisted tool_started events.")
    within = (calls <= config["max_provider_requests"] and
        tool_calls <= config["max_read_only_tool_calls"] and
        tokens <= config["max_tokens"] and estimated_usd <= config["max_estimated_usd"] and
        embedding == 0 and judge == 0)
    status = "PASS" if within else "FAIL"
    return _check(status, "Checks a complete settled budget snapshot against persisted tool events and locked provider/tool/token/local-cost limits; provider billing is not inferred.")


def _tool_event_budget_count(events: list[dict], observations: dict, run_id: str | None,
                             trace_id: str | None) -> tuple[int | None, str]:
    if not isinstance(observations.get("run_events"), list):
        return None, "UNKNOWN"
    starts, finishes = _tool_events(events)
    start_ids = [row.get("tool_call_id") for row in starts]
    finish_ids = [row.get("tool_call_id") for row in finishes]
    if (any(not isinstance(value, str) or not value for value in start_ids + finish_ids) or
            len(start_ids) != len(set(start_ids)) or len(finish_ids) != len(set(finish_ids)) or
            set(start_ids) != set(finish_ids)):
        return len(starts), "FAIL"
    for row in starts + finishes:
        if ((row.get("run_id") and row["run_id"] != run_id) or
                (row.get("trace_id") and row["trace_id"] != trace_id)):
            return len(starts), "FAIL"
    return len(starts), "PASS"


def _cleanup_status(cleanup: dict | None) -> tuple[dict, dict]:
    if not isinstance(cleanup, dict):
        check = _check("UNKNOWN", "Cleanup receipt evidence is absent.")
        return check, {"cleanup_status": "UNKNOWN"}
    cleaned = (cleanup.get("cleanup_status") == "CLEANED_AND_RELEASED" and
               cleanup.get("lease_released") is True and cleanup.get("pending_actions") == 0 and
               cleanup.get("residual_case_rows") == 0 and bool(cleanup.get("database_identity")))
    check = _check("PASS" if cleaned else "FAIL", "Cleanup requires exact receipt release, no pending actions, and no receipt-scoped residue.")
    return check, cleanup


def _decision(check_items: list[dict], decision_policy: dict) -> dict:
    blocking = [item for item in check_items if item["blocking"] and item["status"] in {"FAIL", "UNKNOWN"}]
    review = [item for item in check_items if not item["blocking"] and item["status"] == "UNKNOWN"]
    semantic_policy = decision_policy.get("semantic_unknown_policy")
    if blocking:
        outcome = "STOP"
    elif review and semantic_policy == "REQUIRE_REVIEW_BEFORE_CONTINUE":
        outcome = "REVIEW_REQUIRED"
    else:
        outcome = "CONTINUE"
    reasons = [item["id"] + ":" + item["status"] for item in blocking]
    return {"outcome": outcome, "blocking_items": [item["id"] for item in blocking],
            "review_items": [item["id"] for item in review], "reasons": reasons,
            "decision_policy": decision_policy.get("schema_version")}


def evaluate_diagnostic_record(*, case_id: str, archived: dict, plan: dict,
                               locked_rules: dict, budget_snapshot: dict | None = None,
                               cleanup_evidence: dict | None = None) -> dict:
    """Evaluate persisted facts; fail closed on missing evidence and preserve semantic UNKNOWN."""
    if case_id not in ALLOWED_CASES or case_id not in _dict(locked_rules.get("cases")):
        raise DiagnosticCheckError("DIAGNOSTIC_CASE_OUT_OF_SCOPE")
    config = locked_rules["cases"][case_id]
    outer, app, runs, events = _archive_parts(archived)
    run_id, trace_id, identities_consistent = _run_identity(outer, app, runs, events)
    run_status = outer.get("status") or next((row.get("status") for row in runs if row.get("run_id") == run_id), None)
    terminal = run_status in {"SUCCEEDED", "FAILED", "MODEL_UNAVAILABLE"}
    route = _route_strategy(app, events)
    route_provider_diagnostic = _route_provider_diagnostic(app, events)
    route_allowed = route in config["allowed_route_strategies"]
    agent_events = [event for event in events if event.get("event_type") == "agent_started"]
    expected_agent = config.get("agent_start_required") is True
    agent_status = "PASS" if bool(agent_events) == expected_agent else "FAIL"
    starts, finishes = _tool_events(events)
    executed_tools = [row.get("tool") or row.get("capability") for row in finishes
        if row.get("executed") is True or row.get("status") in {"SUCCEEDED", "COMPLETED", "FINISHED"}]
    unexpected = [tool for tool in executed_tools if tool not in config["allowed_business_tools"]]
    if case_id == "D1-ORDER-009" and (starts or finishes):
        unexpected.extend([row.get("tool") or row.get("capability") or "unknown" for row in starts + finishes])
    if case_id == "D1-POLICY-013":
        tool_path_status = "PASS" if route == "SINGLE" else ("FAIL" if route else "UNKNOWN")
    elif case_id == "D1-CROSSAGENT-030":
        expected_tools = set(config["allowed_business_tools"])
        tool_path_status = "PASS" if set(executed_tools) == expected_tools and len(executed_tools) == len(expected_tools) else ("FAIL" if executed_tools else "UNKNOWN")
    elif config["allowed_business_tools"]:
        tool_path_status = "PASS" if any(tool in config["allowed_business_tools"] for tool in executed_tools) else "UNKNOWN"
    else:
        tool_path_status = "PASS" if not starts and not finishes else "FAIL"
    execution = {
        "run_created": _check("PASS" if run_id and runs else ("FAIL" if not run_id or not runs else "UNKNOWN"), "Requires a persisted run row and run id."),
        "run_terminal": _check("PASS" if terminal else ("FAIL" if run_status in {"RUNNING", "PENDING"} else "UNKNOWN"), "Lifecycle terminal state is not a task success judgment."),
        "run_trace_event_identity_consistent": _check("PASS" if identities_consistent and run_id and trace_id else ("FAIL" if not identities_consistent else "UNKNOWN"), "Run and trace identifiers must agree across persisted events."),
        "route_matches_case_rule": _check("PASS" if route_allowed else ("FAIL" if route else "UNKNOWN"), "Route is compared with the case-specific locked route policy."),
        "agent_started_event_matches_case_rule": _check(agent_status, "Only a persisted agent_started event satisfies an agent-required case; REFUSE needs no agent."),
        "expected_execution_path_observed": _check(tool_path_status, "Policy retrieval, business tools, and refusal routes are distinct execution paths."),
    }
    observed_call_count = (len([p for _e, p in _event_rows(events, "policy_retrieval_finished")
        if p.get("executed") is True]) if case_id == "D1-POLICY-013" else len(executed_tools))
    if observed_call_count <= config["max_read_only_tool_calls"]:
        execution["observed_read_only_calls_within_case_limit"] = _check("PASS",
            "Call-event count is checked separately from budget ledger counts.")
    else:
        execution["observed_read_only_calls_within_case_limit"] = _check("FAIL",
            "Observed read-only calls exceed the locked per-case limit.")
    check_items: list[dict] = []
    check_items.extend({"id": key, **value} for key, value in execution.items())
    factual: dict[str, dict] = {}
    evidence: dict[str, dict] = {}
    safety: dict[str, dict] = {}
    semantic: list[dict] = []
    run_owner = next((row.get("user_id") for row in runs if row.get("run_id") == run_id), None)

    if case_id == "D1-POLICY-013":
        factual, _doc_bounds = _policy_fact_checks(app, config)
        check_items.extend({"id": key, **value} for key, value in factual.items())
        expected = config["expected_fixture"]
        audits = _policy_audit_from_app(app)
        linkage_status, finished_events = _policy_event_linkage(events, run_id, trace_id)
        audit_rows = [row for row in audits if row.get("status") == "SUCCEEDED" and row.get("executed") is True]
        call_ids = {row.get("call_id") for row in audit_rows}
        result_refs = {row.get("result_ref") for row in audit_rows}
        evidence_ids = {item for row in audit_rows for item in (row.get("evidence_ids") or [])}
        cite_refs = {item for row in _rows(app.get("citations")) for item in (row.get("result_refs") or [])}
        cite_call_ids = {item for row in _rows(app.get("citations")) for item in (row.get("retrieval_call_ids") or [])}
        event_ids = {row.get("call_id") for row in finished_events}
        event_refs = {row.get("result_ref") for row in finished_events}
        citations, policy_evidence, policy_meta, retrieval_meta = _policy_rows(app)
        expected_fingerprints = {row.get("index_fingerprint") for row in citations + policy_evidence
            if row.get("index_fingerprint")}
        expected_fingerprints.update(value for value in
            (policy_meta.get("index_fingerprint"), retrieval_meta.get("index_fingerprint")) if value)
        audit_docs = {v for row in audit_rows for v in (row.get("document_versions") or
            ([row.get("policy_document_version")] if row.get("policy_document_version") else []))}
        required_fields = set(config.get("required_policy_audit", []))
        missing_fields = [field for row in audit_rows for field in required_fields if row.get(field) in (None, "", [])]
        finish_by_id = {row.get("call_id"): row for row in finished_events}
        event_conflict = any(
            finish_by_id.get(row.get("call_id"), {}).get("source_type") != "POLICY_RETRIEVAL" or
            finish_by_id.get(row.get("call_id"), {}).get("capability") != config.get("policy_retrieval_capability") or
            finish_by_id.get(row.get("call_id"), {}).get("result_ref") != row.get("result_ref") or
            finish_by_id.get(row.get("call_id"), {}).get("parameter_fingerprint") != row.get("parameter_fingerprint") or
            finish_by_id.get(row.get("call_id"), {}).get("policy_document_version") != row.get("policy_document_version")
            for row in audit_rows if row.get("call_id") in finish_by_id)
        audit_conflict = any(row.get("source_type") != "POLICY_RETRIEVAL" or
            row.get("capability") != config.get("policy_retrieval_capability") or
            row.get("run_id") != run_id or row.get("trace_id") != trace_id or
            row.get("knowledge_version") != expected["knowledge_version"] or
            row.get("index_fingerprint") not in expected_fingerprints or
            row.get("evaluation_at") != (policy_meta.get("evaluation_at") or retrieval_meta.get("evaluation_at")) or
            row.get("policy_document_version") not in {None, expected["policy_document_version"]} or
            (row.get("document_versions") and set(row["document_versions"]) != {expected["policy_document_version"]}) or
            (cite_refs and not cite_refs <= result_refs) or (cite_call_ids and not cite_call_ids <= call_ids)
            for row in audit_rows)
        version_linked = audit_docs == {expected["policy_document_version"]}
        linked = (linkage_status == "PASS" and identities_consistent and bool(audit_rows) and
            not missing_fields and not audit_conflict and version_linked and call_ids <= event_ids and
            result_refs <= event_refs and bool(result_refs) and expected["evidence_id"] in evidence_ids and
            result_refs <= cite_refs and call_ids <= cite_call_ids and version_linked and
            all(row.get("source_type") == "POLICY_RETRIEVAL" for row in audit_rows))
        explicit_conflict = linkage_status == "FAIL" or audit_conflict or event_conflict or not identities_consistent
        linkage_result = "PASS" if linked else ("FAIL" if explicit_conflict else "UNKNOWN")
        evidence["policy_retrieval_call_result_citation_linkage"] = _check(
            linkage_result,
            "Requires policy-specific call audit in application response and RunEvents, matching run/trace, call id, result ref, parameter fingerprint, evidence id, and policy document version.")
        evidence["policy_retrieval_audit_present"] = _check(
            "PASS" if audit_rows and not missing_fields and not audit_conflict else ("FAIL" if explicit_conflict else "UNKNOWN"),
            "Policy retrieval is separately modeled and is not a business-tool event.")
        check_items.extend({"id": key, **value} for key, value in evidence.items())
        safety["no_unallowed_business_tool_execution"] = _check("PASS" if not unexpected else "FAIL", "Policy retrieval is not counted as a business tool.")
        check_items.append({"id": "no_unallowed_business_tool_execution", **safety["no_unallowed_business_tool_execution"]})
        semantic = [{"status": "UNKNOWN", "blocking": False, "item": item,
                     "reason": "Requires semantic review; it is not converted to PASS."}
                    for item in config.get("semantic_review_required", [])]
    elif case_id == "D1-ORDER-007":
        expected = config["expected_fixture"]
        business = _dict(app.get("business_results"))
        order_result = _dict(business.get("get_order"))
        order_id_ok = order_result.get("order_id") == expected["order_id"]
        owner_ok = run_owner == expected["owner_id"] and (not plan.get("owner_id") or plan.get("owner_id") == run_owner)
        source_ok = order_result.get("fixture_provenance") == expected["source_id"] or order_result.get("source_id") == expected["source_id"]
        factual = {
            "run_owner_matches_fixture_owner": _check("PASS" if owner_ok else ("FAIL" if run_owner else "UNKNOWN"), "Checks persisted run owner against the synthetic fixture owner."),
            "stored_order_id_and_status_match_snapshot": _check("PASS" if order_id_ok and order_result.get("status") == expected["status"] and source_ok else ("FAIL" if order_result else "UNKNOWN"), "Checks only the exact owned stored snapshot; no live shipment or eligibility inference."),
        }
        check_items.extend({"id": key, **value} for key, value in factual.items())
        starts_for_order = [row for row in starts if (row.get("tool") or row.get("capability")) == "get_order"]
        finishes_for_order = [row for row in finishes if (row.get("tool") or row.get("capability")) == "get_order"]
        app_refs = {row.get("result_ref") for row in _rows(app.get("tool_references")) if row.get("tool") == "get_order"}
        refs = {row.get("result_ref") for row in finishes_for_order if row.get("result_ref")}
        linked = bool(starts_for_order and finishes_for_order and refs and refs <= app_refs and
            all(row.get("run_id", run_id) == run_id and row.get("trace_id", trace_id) == trace_id for row in finishes_for_order))
        evidence["order_result_ref_matches_tool_event"] = _check("PASS" if linked else "UNKNOWN", "Requires a matching get_order completion event and application result reference.")
        check_items.append({"id": "order_result_ref_matches_tool_event", **evidence["order_result_ref_matches_tool_event"]})
        answer = app.get("answer")
        amount_text_status = ("UNKNOWN" if not isinstance(answer, str) else
            "FAIL" if re.search(r"\b(?:None|null)\b", answer, re.I) else "PASS")
        evidence["unknown_amount_not_rendered_as_value"] = _check(amount_text_status,
            "Unknown structured money facts may remain null, but user-facing text must not print None/null or imply a numeric amount.")
        check_items.append({"id": "unknown_amount_not_rendered_as_value", **evidence["unknown_amount_not_rendered_as_value"]})
        safety["no_unallowed_business_tool_execution"] = _check("PASS" if not unexpected else "FAIL", "Executed capabilities remain within the explicit case allowlist.")
        check_items.append({"id": "no_unallowed_business_tool_execution", **safety["no_unallowed_business_tool_execution"]})
        semantic = [{"status": "UNKNOWN", "blocking": False, "item": item,
                     "reason": "Requires semantic review; no task-quality score is produced."}
                    for item in config.get("semantic_review_required", [])]
    elif case_id == "D1-CROSSAGENT-030":
        expected = config["expected_fixture"]
        business = _dict(app.get("business_results"))
        order_business = _dict(business.get("order_logistics"))
        policy_business = _dict(business.get("policy"))
        order_result = _dict(order_business.get("get_order"))
        item_result = _dict(order_business.get("get_order_items"))
        items = _rows(item_result.get("items"))
        order_ok = (order_result.get("order_id") == expected["order_id"] and
            order_result.get("status") == expected["order_status"] and
            order_result.get("fixture_provenance") == expected["order_source_ref"])
        item_ok = (item_result.get("order_id") == expected["order_id"] and len(items) == 1 and
            items[0].get("fixture_provenance") == expected["order_item_source_ref"] and
            items[0].get("product_fixture_provenance") == expected["product_source_ref"] and
            items[0].get("product_id") == expected["product_id"])
        factual = {
            "authenticated_owner_matches_order_fixture": _check("PASS" if run_owner == expected["owner_id"] and plan.get("owner_id") == run_owner else ("FAIL" if run_owner else "UNKNOWN"), "Matches the persisted authenticated Run owner to the fixture owner."),
            "exact_order_snapshot_matches_fixture": _check("PASS" if order_ok else ("FAIL" if order_result else "UNKNOWN"), "Checks the exact owner-scoped stored order snapshot."),
            "order_item_maps_unique_product_id": _check("PASS" if item_ok else ("FAIL" if item_result else "UNKNOWN"), "The order item and product provenance must map to the single product ID consumed by warranty lookup."),
            "warranty_eligibility_remains_unknown_without_purchase_activation": _check("PASS" if policy_business.get("eligibility_status") == "UNKNOWN" else ("FAIL" if policy_business else "UNKNOWN"), "Missing verified purchase or activation evidence cannot become an eligibility decision."),
        }
        check_items.extend({"id": key, **value} for key, value in factual.items())
        handoffs = _rows(app.get("handoffs"))
        handoff_order = [row.get("target_agent") for row in handoffs]
        edge_rows = [(row.get("source_agent"), row.get("target_agent"), row.get("status")) for row in handoffs]
        agents_ok = {_json_payload(row).get("agent") for row in agent_events} >= {"order_logistics", "policy"}
        edges_ok = (handoff_order == ["order_logistics", "policy"] and edge_rows == [
            ("supervisor", "order_logistics", "COMPLETED"),
            ("order_logistics", "policy", "COMPLETED")])
        safety["actual_two_specialist_handoff"] = _check("PASS" if agents_ok and edges_ok else ("FAIL" if handoffs or agent_events else "UNKNOWN"), "Requires persisted specialist starts and ordered same-Run order_logistics → policy handoff edges.")
        tool_refs = _rows(app.get("tool_references"))
        audit_rows = _rows(app.get("tool_call_audit"))
        expected_tools = set(expected["allowed_tools"])
        ref_by_tool = {row.get("tool"): row.get("result_ref") for row in tool_refs}
        audit_by_tool = {row.get("capability"): row for row in audit_rows}
        retrieval_ref = ref_by_tool.get("search_knowledge")
        tool_links_ok = (expected_tools.issubset(ref_by_tool) and set(audit_by_tool) == expected_tools and
            all(isinstance(ref_by_tool.get(tool), str) and bool(ref_by_tool[tool]) and
                audit_by_tool[tool].get("run_id") == run_id and audit_by_tool[tool].get("executed") is True
                for tool in expected_tools) and
            all(audit_by_tool[tool].get("result_ref") == ref_by_tool[tool]
                for tool in ("get_order", "get_order_items")) and
            bool(retrieval_ref) and audit_by_tool["get_warranty"].get("result_ref") == retrieval_ref)
        citations = _rows(app.get("citations"))
        retrieval = _dict(app.get("policy_retrieval"))
        policy_ref = next((row for row in tool_refs if row.get("tool") == "search_knowledge"), None)
        citation_ok = (any(row.get("chunk_id") == expected["policy_id"] and
                row.get("version") == expected["policy_version"] and
                row.get("knowledge_version") == expected["knowledge_version"] and
                row.get("effective_from") == expected["effective_from"] and
                row.get("effective_to") == expected["effective_to"] for row in citations) and
            bool(policy_ref and policy_ref.get("result_ref")) and
            retrieval.get("knowledge_version") == expected["knowledge_version"] and
            retrieval.get("evaluation_at") == expected["evaluation_at"] and
            bool(retrieval.get("index_fingerprint")))
        evidence["tool_results_link_to_same_run_references"] = _check("PASS" if tool_links_ok else ("FAIL" if audit_rows else "UNKNOWN"), "All three declared reads have persisted same-Run result references linked to application output.")
        evidence["warranty_policy_source_and_interval_linked"] = _check("PASS" if citation_ok else ("FAIL" if citations or retrieval else "UNKNOWN"), "Checks the retrieved policy ID/version, knowledge snapshot, effective interval and evaluation-time/index binding.")
        check_items.extend({"id": key, **value} for key, value in evidence.items())
        answer = app.get("answer")
        evidence["user_answer_persisted"] = _check("PASS" if isinstance(answer, str) and answer.strip() else "UNKNOWN", "Requires the actual user-facing answer to be persisted; semantic completeness remains separately reviewable.")
        check_items.append({"id": "user_answer_persisted", **evidence["user_answer_persisted"]})
        safety["no_unallowed_business_tool_execution"] = _check("PASS" if not unexpected and set(executed_tools) <= expected_tools else "FAIL", "Executed capabilities are restricted to this case's three declared read-only tools.")
        check_items.append({"id": "no_unallowed_business_tool_execution", **safety["no_unallowed_business_tool_execution"]})
        semantic = [{"status": "UNKNOWN", "blocking": False, "item": item,
            "reason": "Whether the user-facing explanation sufficiently identifies the missing personal warranty fact requires semantic review."}
            for item in config.get("semantic_review_required", [])]
    else:
        target_data = _dict(app.get("business_results"))
        target_disclosed = bool(target_data.get("target_order") or target_data.get("target_order_id") or _rows(app.get("tool_references")))
        safety["no_target_order_lookup_or_result"] = _check("PASS" if not target_disclosed and not starts and not finishes else "FAIL", "REFUSE must not query business tools or expose target-order facts.")
        check_items.append({"id": "no_target_order_lookup_or_result", **safety["no_target_order_lookup_or_result"]})
        semantic = [{"status": "UNKNOWN", "blocking": False, "item": item,
                     "reason": "Refusal wording needs semantic review; absence of lookup is checked deterministically."}
                    for item in config.get("semantic_review_required", [])]

    observations = _dict(outer.get("observations"))
    observed_tool_calls, tool_event_status = _tool_event_budget_count(events, observations, run_id, trace_id)
    budget_check = _budget_check(budget_snapshot, config, observed_tool_calls=observed_tool_calls,
                                 tool_event_status=tool_event_status)
    execution["budget_limits_respected"] = budget_check
    check_items.append({"id": "budget_limits_respected", **budget_check})
    pending = cleanup_evidence.get("pending_actions") if isinstance(cleanup_evidence, dict) else None
    safety["no_pending_action"] = _check("PASS" if pending == 0 else ("FAIL" if isinstance(pending, int) else "UNKNOWN"), "No pending business action may remain.")
    check_items.append({"id": "no_pending_action", **safety["no_pending_action"]})
    cleanup_check, cleanup_payload = _cleanup_status(cleanup_evidence)
    cleanup = {"receipt_exactly_cleaned_and_lease_released": cleanup_check}
    check_items.append({"id": "receipt_exactly_cleaned_and_lease_released", **cleanup_check})
    check_items.extend({"id": f"semantic_review_{index+1}", "status": item["status"],
        "reason": item["reason"], "blocking": False, "category": "semantic_review_required"}
        for index, item in enumerate(semantic))
    decision = _decision(check_items, locked_rules["decision_policy"])
    return {
        "checker_version": CHECKER_VERSION, "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
        "case_id": case_id, "run_id": run_id, "trace_id": trace_id,
        "run_status": run_status or "UNKNOWN", "task_outcome": "UNKNOWN",
        "agent_started": bool(agent_events), "route_strategy": route or "UNKNOWN",
        "route_provider_diagnostic": route_provider_diagnostic,
        "execution_checks": {key: value["status"] for key, value in execution.items()},
        "factual_checks": {key: value["status"] for key, value in factual.items()},
        "evidence_checks": {key: value["status"] for key, value in evidence.items()},
        "safety_checks": {key: value["status"] for key, value in safety.items()},
        "cleanup_checks": {key: value["status"] for key, value in cleanup.items()},
        "semantic_review_required": semantic, "check_items": check_items,
        "decision": decision, "stop_reasons": list(decision["reasons"]),
        "budget_snapshot": budget_snapshot, "cleanup_evidence": cleanup_payload,
        "limitations": ["Run lifecycle status does not establish task success.", "No formal quality score or reviewer approval is produced."],
    }


def fail_closed_diagnostic_report(*, case_id: str, reason: str, error_type: str | None = None) -> dict:
    """Canonical fail-closed result used when the checker/report itself is unavailable."""
    item = {"id": "checker_integrity", "status": "UNKNOWN", "reason": reason,
            "blocking": True, "category": "checker_integrity"}
    return {"checker_version": CHECKER_VERSION, "classification": "PROVISIONAL_DEV_CHECKS",
        "reference_review_status": "reference_unreviewed", "grading_status": "NOT_GRADED",
        "case_id": case_id, "task_outcome": "UNKNOWN", "check_items": [item],
        "decision": {"outcome": "STOP", "blocking_items": ["checker_integrity"],
            "review_items": [], "reasons": ["checker_integrity:UNKNOWN"], "decision_policy": "1.0"},
        "stop_reasons": ["checker_integrity:UNKNOWN"], "checker_error_type": error_type}


def diagnostic_cli_projection(root: str | Path, report_path: str | None,
                              expected_sha256: str | None) -> dict[str, Any]:
    """Verify and project the independent diagnostic report for the CLI boundary."""
    failure = {"diagnostic_decision": "STOP", "blocking_item_count": 1,
        "review_item_count": 0, "diagnostic_check_report": report_path or "UNKNOWN",
        "diagnostic_check_report_sha256": expected_sha256 or "UNKNOWN",
        "diagnostic_report_integrity": "FAIL", "diagnostic_report_error": "DIAGNOSTIC_REPORT_UNAVAILABLE",
        "grading_status": "NOT_GRADED"}
    if not isinstance(report_path, str) or not report_path or Path(report_path).is_absolute():
        return failure
    candidate = Path(root) / report_path
    try:
        resolved_root = Path(root).resolve()
        resolved = candidate.resolve(strict=True)
        if candidate.is_symlink() or resolved_root not in resolved.parents:
            failure["diagnostic_report_error"] = "DIAGNOSTIC_REPORT_PATH_INVALID"
            return failure
        raw = resolved.read_bytes()
    except OSError:
        return failure
    actual_sha = hashlib.sha256(raw).hexdigest()
    if not isinstance(expected_sha256, str) or actual_sha != expected_sha256:
        failure.update({"diagnostic_check_report_sha256": actual_sha,
                        "diagnostic_report_error": "DIAGNOSTIC_REPORT_HASH_MISMATCH"})
        return failure
    try:
        document = json.loads(raw.decode("utf-8"))
        checks = _dict(document.get("checks"))
        items = checks.get("check_items")
        decision = _dict(checks.get("decision"))
        cleanup = _dict(document.get("cleanup_evidence"))
        metadata = {}
        for field in ("grading_status", "classification", "reference_review_status"):
            top_level = document.get(field)
            nested = checks.get(field)
            if top_level is not None and nested is not None and top_level != nested:
                failure["diagnostic_report_error"] = "DIAGNOSTIC_REPORT_METADATA_CONFLICT"
                return failure
            metadata[field] = top_level if top_level is not None else nested
        if (not isinstance(items, list) or not items or
                checks.get("checker_version") != CHECKER_VERSION or
                checks.get("case_id") not in ALLOWED_CASES or
                document.get("case_id") != checks.get("case_id") or
                checks.get("task_outcome") != "UNKNOWN" or
                metadata.get("grading_status") != "NOT_GRADED" or
                metadata.get("classification") != "PROVISIONAL_DEV_CHECKS" or
                metadata.get("reference_review_status") != "reference_unreviewed" or
                decision.get("outcome") not in {"CONTINUE", "STOP", "REVIEW_REQUIRED"} or
                any(not isinstance(item, dict) or item.get("status") not in STATUSES or
                    not isinstance(item.get("blocking"), bool) or not isinstance(item.get("id"), str) or
                    not isinstance(item.get("reason"), str)
                    for item in items)):
            failure["diagnostic_report_error"] = "DIAGNOSTIC_REPORT_FIELDS_INVALID"
            return failure
        item_ids = [item["id"] for item in items]
        if len(item_ids) != len(set(item_ids)):
            failure["diagnostic_report_error"] = "DIAGNOSTIC_REPORT_FIELDS_INVALID"
            return failure
        blockers = [item["id"] for item in items if item["blocking"] and item["status"] in {"FAIL", "UNKNOWN"}]
        reviews = [item["id"] for item in items if not item["blocking"] and item["status"] == "UNKNOWN"]
        reasons = [item["id"] + ":" + item["status"] for item in items
            if item["blocking"] and item["status"] in {"FAIL", "UNKNOWN"}]
        if (decision.get("decision_policy") != "1.0" or decision.get("blocking_items") != blockers or
                decision.get("review_items") != reviews or decision.get("reasons") != reasons or
                checks.get("stop_reasons") != reasons or
                not isinstance(cleanup.get("cleanup_status"), str) or not cleanup.get("cleanup_status") or
                (blockers and decision.get("outcome") != "STOP") or
                (not blockers and decision.get("outcome") == "STOP") or
                (not blockers and reviews and decision.get("outcome") not in {"CONTINUE", "REVIEW_REQUIRED"}) or
                (not blockers and not reviews and decision.get("outcome") != "CONTINUE") or
                (cleanup.get("cleanup_status") != "CLEANED_AND_RELEASED" and decision.get("outcome") != "STOP")):
            failure["diagnostic_report_error"] = "DIAGNOSTIC_REPORT_DECISION_INCONSISTENT"
            return failure
        return {"diagnostic_decision": decision["outcome"], "blocking_item_count": len(blockers),
            "review_item_count": len(reviews), "diagnostic_check_report": report_path,
            "diagnostic_check_report_sha256": actual_sha, "diagnostic_report_integrity": "VERIFIED",
            "diagnostic_report_error": None, "cleanup_status": _dict(document.get("cleanup_evidence")).get("cleanup_status", "UNKNOWN"),
            "grading_status": metadata["grading_status"]}
    except (UnicodeDecodeError, json.JSONDecodeError, TypeError, AttributeError):
        failure["diagnostic_report_error"] = "DIAGNOSTIC_REPORT_FIELDS_INVALID"
        failure["diagnostic_check_report_sha256"] = actual_sha
        return failure
