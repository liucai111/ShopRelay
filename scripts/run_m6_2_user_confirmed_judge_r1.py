"""One-shot two-sample user-authorized M6.2 Dev Judge supplement."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PKG = ROOT / "docs/eval/m6.2-user-confirmed-dev-review-r2-20261029"
SAMPLES = PKG / "judge-frozen-samples-r1.json"
MANIFEST = PKG / "judge-manifest-r1.json"
AUTH = PKG / "judge-authorization-r1.json"
STATE = PKG / "judge-state-r1.json"
BUDGET = PKG / "judge-budget-r1.json"
RESULTS = PKG / "judge-results-r1.json"
JOURNAL = PKG / "judge-journal-r1.jsonl"
PROXY = "http://127.0.0.1:7892"
PROFILE="historical-two-r1"
MAX_REQUESTS,TOKEN_CAP,USD_CAP,TIMEOUT,TOTAL_SECONDS,MAX_OUTPUT=2,24000,.06,30,120,768


def select_profile(name: str) -> None:
    """Only explicit historical or user-approved scopes; no arbitrary paths."""
    global PROFILE,PKG,SAMPLES,MANIFEST,AUTH,STATE,BUDGET,RESULTS,JOURNAL
    global MAX_REQUESTS,TOKEN_CAP,USD_CAP,TIMEOUT,TOTAL_SECONDS,MAX_OUTPUT
    if name!="r23-confirmed-four": raise ValueError("unknown Judge profile")
    PROFILE=name
    PKG=ROOT/"docs/eval/m6.2-r23-confirmed-calibration-and-formal-wiring-20261031"
    SAMPLES,MANIFEST,AUTH,STATE,BUDGET,RESULTS,JOURNAL=[PKG/n for n in (
        "judge-frozen-samples-r1.json","judge-manifest-r1.json","judge-authorization-r1.json",
        "judge-state-r1.json","judge-budget-r1.json","judge-results-r1.json","judge-journal-r1.jsonl")]
    MAX_REQUESTS,TOKEN_CAP,USD_CAP,TIMEOUT,TOTAL_SECONDS,MAX_OUTPUT=4,48000,1.0,120,600,2048


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read(p: Path):
    return json.loads(p.read_text(encoding="utf-8"))


def atomic(p: Path, value) -> None:
    data = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    fd, tmp = tempfile.mkstemp(prefix=p.name + ".", suffix=".tmp", dir=PKG)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data); f.flush(); os.fsync(f.fileno())
        os.replace(tmp, p)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def journal(event: str, **fields) -> None:
    row = {"event": event, "at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"), **fields}
    with JOURNAL.open("a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush(); os.fsync(f.fileno())


def configure_proxy() -> dict:
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[name] = PROXY
    removed = []
    for key in ("NO_PROXY", "no_proxy"):
        for token in re.split(r"[,;\s]+", os.environ.get(key, "")):
            t = token.lower().strip()
            if t and (t == "*" or "deepseek.com" in t):
                removed.append(t)
    retained = []
    for token in re.split(r"[,;\s]+", os.environ.get("NO_PROXY", os.environ.get("no_proxy", ""))):
        t = token.strip()
        if t and t != "*" and "deepseek.com" not in t.lower():
            retained.append(t)
    # This process only contacts the approved endpoint. Emptying NO_PROXY also
    # handles broad suffixes such as .com, which a host-name substring test misses.
    os.environ["NO_PROXY"] = os.environ["no_proxy"] = ""
    import httpx
    selected = httpx._utils.get_environment_proxies().get("https://")
    if selected != PROXY:
        raise RuntimeError("HTTPX did not select the required HTTPS proxy")
    return {"six_proxy_variables_host_port": "127.0.0.1:7892", "httpx_https_proxy_host_port": "127.0.0.1:7892",
            "provider_bypass_removed": bool(removed), "api_deepseek_bypassed": False, "tls_verification": "DEFAULT_ENABLED"}


def preflight():
    m, a, s, b, samples = (read(p) for p in (MANIFEST, AUTH, STATE, BUDGET, SAMPLES))
    if m.get("batch_id") != a.get("batch_id") or m.get("batch_id") != s.get("batch_id"):
        raise RuntimeError("batch binding mismatch")
    if a.get("status") != "AUTHORIZED_READY" or a.get("authorized_by") != "CHAT_USER_SELF_CONFIRMED":
        raise RuntimeError("Judge authorization is not active")
    if a.get("manifest_sha256") != sha(MANIFEST.read_bytes()) or s.get("manifest_sha256") != a.get("manifest_sha256"):
        raise RuntimeError("manifest binding mismatch")
    if a.get("frozen_samples_sha256") != sha(SAMPLES.read_bytes()) or m.get("sample_paths_and_sha256", {}).get(SAMPLES.name) != sha(SAMPLES.read_bytes()):
        raise RuntimeError("frozen sample binding mismatch")
    if s.get("authorization_sha256") != sha(AUTH.read_bytes()) or s.get("manifest_sha256") != sha(MANIFEST.read_bytes()):
        raise RuntimeError("state binding mismatch")
    if s.get("closed") or s.get("status") not in {"READY", "RUNNING"} or s.get("usage_unknown"):
        raise RuntimeError("Judge state is closed, unknown, or not runnable")
    expires = datetime.fromisoformat(a["expires_after_utc"].replace("Z", "+00:00"))
    if datetime.now(timezone.utc) >= expires:
        raise RuntimeError("authorization expired")
    if len(samples) != MAX_REQUESTS or [x["sample_id"] for x in samples] != m["sample_order"]:
        raise RuntimeError("sample scope/order mismatch")
    if PROFILE=="r23-confirmed-four":
        from eval.m6_2.confirmed_review_r23 import JUDGE_IDS
        if tuple(m["sample_order"])!=JUDGE_IDS or m["execution_profile"]!=PROFILE:
            raise RuntimeError("exact four-object Judge profile mismatch")
        if m["limits"]!={"requests":4,"tokens":48000,"estimated_usd":1.0,"request_timeout_seconds":120,"batch_seconds":600,"concurrency":1,"retries":0}:
            raise RuntimeError("R23 user-approved budget/time binding mismatch")
    if s["reserved_tokens"] or s["reserved_usd"]:
        raise RuntimeError("unsettled prior attempt: reconcile without replay")
    if s.get("started_at_utc") and (datetime.now(timezone.utc)-datetime.fromisoformat(s["started_at_utc"].replace("Z","+00:00"))).total_seconds()>=TOTAL_SECONDS:
        raise RuntimeError("Judge batch deadline reached")
    if m.get("model", {}).get("provider") != "DeepSeek" or m["model"].get("model") != "deepseek-flash" or m["model"].get("endpoint") != "https://api.deepseek.com":
        raise RuntimeError("model/endpoint mismatch")
    for rel, expected in m["source_bindings"].items():
        path = ROOT / rel
        if not path.is_file() or sha(path.read_bytes()) != expected:
            raise RuntimeError(f"source binding mismatch: {rel}")
    for rel, expected in m["runtime_bindings"].items():
        path = ROOT / rel
        if not path.is_file() or sha(path.read_bytes()) != expected:
            raise RuntimeError(f"runtime binding mismatch: {rel}")
    from eval.m6_2.judge_contract import build_messages, validate_payload
    for sample in samples:
        valid, errors, _ = validate_payload(sample["payload"])
        if not valid:
            raise RuntimeError(f"invalid frozen payload {sample['sample_id']}: {errors}")
        msgs = build_messages(sample["payload"])
        got = sha(json.dumps(msgs, ensure_ascii=False, separators=(",", ":")).encode())
        if got != sample["messages_sha256"]:
            raise RuntimeError("serialized Judge messages changed")
    pricing = read(ROOT / m["pricing"]["path"])
    if sha((ROOT / m["pricing"]["path"]).read_bytes()) != m["pricing"]["sha256"]:
        raise RuntimeError("price source hash mismatch")
    if PROFILE=="r23-confirmed-four" and (sha(BUDGET.read_bytes())!=m["budget_sha256"] or
            b["limits"]!=m["limits"] or b["rates_usd_per_million"]!=pricing["rates_usd_per_million"]):
        raise RuntimeError("Judge budget/pricing binding mismatch")
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(pricing["checked_at_utc"].replace("Z", "+00:00"))).total_seconds()
    if age < 0 or age > 86400:
        raise RuntimeError("official pricing evidence is stale")
    from app.m0.config import load_settings
    settings = load_settings()
    if settings.model != m["model"]["model"] or settings.base_url.rstrip("/") != m["model"]["endpoint"].rstrip("/") or not settings.api_key:
        raise RuntimeError("live model setting or credential presence mismatch")
    proxy = configure_proxy()
    return m, a, s, b, samples, settings, proxy, round(age)


def close(reason: str) -> None:
    state = read(STATE)
    auth = read(AUTH)
    auth["status"] = "CLOSED_COMPLETED" if reason == "BATCH_COMPLETED" else "CLOSED_STOPPED"
    auth["closed_at_utc"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    auth["closure_reason"] = reason
    atomic(AUTH, auth)
    state["status"] = auth["status"]
    state["closed"] = True
    state["authorization_closure_sha256"] = sha(AUTH.read_bytes())
    atomic(STATE, state)
    closure = {"schema_version": "m6.2-user-confirmed-judge-closure-r1", "batch_id": state["batch_id"],
               "status": auth["status"], "reason": reason, "closed_at_utc": auth["closed_at_utc"],
               "provider_requests_attempted": state["provider_requests_attempted"], "attempted_samples": state["attempted_sample_ids"],
               "settled_tokens": state["settled_tokens"], "local_estimated_usd": state["local_estimated_usd"],
               "usage_unknown": state["usage_unknown"]}
    closure["pricing_unknown"]=state.get("pricing_unknown",False)
    closure["local_estimated_usd_known_subtotal"]=state["local_estimated_usd"]
    if closure["pricing_unknown"] or closure["usage_unknown"]: closure["local_estimated_usd"]=None
    closure["reserved_tokens"]=state["reserved_tokens"];closure["reserved_usd"]=state["reserved_usd"]
    atomic(PKG / "judge-closure-r1.json", closure)
    journal("AUTHORIZATION_CLOSED", closure_sha256=sha((PKG / "judge-closure-r1.json").read_bytes()), reason=reason)


def send_next() -> dict:
    m, a, state, budget, samples, settings, proxy, price_age = preflight()
    if RESULTS.exists() and state.get("status") not in {"RUNNING"}:
        raise RuntimeError("result exists; replay denied")
    next_sample = next((x for x in samples if x["sample_id"] not in state["attempted_sample_ids"]), None)
    if next_sample is None:
        raise RuntimeError("no unused authorized sample")
    index = m["sample_order"].index(next_sample["sample_id"])
    if index != state["provider_requests_attempted"]:
        raise RuntimeError("sample order/attempt ledger mismatch")
    reserve_usd=(12000*max(budget["rates_usd_per_million"]["input_cache_miss_peak"],budget["rates_usd_per_million"]["output_peak"])/1_000_000
        if PROFILE=="r23-confirmed-four" else .03)
    if state["provider_requests_attempted"] >= MAX_REQUESTS or state["local_estimated_usd"] + state["reserved_usd"] + reserve_usd > USD_CAP+1e-9 or state["settled_tokens"]+12000>TOKEN_CAP:
        raise RuntimeError("batch cap exhausted")
    if index:
        prior=read(RESULTS)["samples"]
        if len(prior)!=index or any(r["parse_status"]!="VALID" or not r["usage_valid"] for r in prior):
            raise RuntimeError("previous sample format/usage/settlement gate failed")
    # Consume the sample and reserve maximum local budget/tokens before client construction or request.
    state["status"] = "RUNNING"
    state.setdefault("started_at_utc",datetime.now(timezone.utc).isoformat())
    state["attempted_sample_ids"].append(next_sample["sample_id"])
    state["provider_requests_attempted"] += 1
    state["reserved_tokens"] += 12000
    state["reserved_usd"] = round(state["reserved_usd"] + reserve_usd, 8)
    atomic(STATE, state)
    journal("SAMPLE_ATTEMPT_CONSUMED_AND_RESERVED", sample_id=next_sample["sample_id"],
            request_number=state["provider_requests_attempted"], reserved_tokens=12000, reserved_usd=reserve_usd,
            messages_sha256=next_sample["messages_sha256"])
    existing = read(RESULTS) if RESULTS.exists() else {"schema_version": "m6.2-user-confirmed-judge-results-r1", "batch_id": m["batch_id"], "samples": []}
    try:
        from openai import OpenAI
        from eval.m6_2.judge_contract import create_judge_completion, parse_response
        remaining=TOTAL_SECONDS-(datetime.now(timezone.utc)-datetime.fromisoformat(state["started_at_utc"].replace("Z","+00:00"))).total_seconds()
        with OpenAI(api_key=settings.api_key, base_url=settings.base_url, max_retries=0, timeout=min(TIMEOUT,remaining)) as client:
            response = create_judge_completion(client, settings.model, next_sample["payload"], max_tokens=MAX_OUTPUT)
        content = response.choices[0].message.content if response.choices else None
        finish = response.choices[0].finish_reason if response.choices else None
        reasoning=getattr(response.choices[0].message,"reasoning_content",None) if response.choices else None
        usage_obj = response.usage
        usage = None if usage_obj is None else {"prompt_tokens": usage_obj.prompt_tokens, "completion_tokens": usage_obj.completion_tokens, "total_tokens": usage_obj.total_tokens}
        parse_status, parsed, errors = parse_response(content, {s["source_ref"] for s in next_sample["payload"]["applicable_sources"]})
        valid=parse_status=="VALID"
        if finish == "length":
            valid = False
            errors.append("response_finished_by_token_length")
        if valid and not parsed["evidence_refs"]:
            valid = False
            errors.append("empty_evidence_refs")
        usage_valid = isinstance(usage, dict) and all(isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and usage[k] >= 0 for k in ("prompt_tokens", "completion_tokens", "total_tokens")) and usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"] and usage["total_tokens"] <= 12000
        response_model=getattr(response,"model",None)
        model_valid=(isinstance(response_model,str) and response_model.casefold() in {v.casefold() for v in budget["expected_response_models"]}) if PROFILE=="r23-confirmed-four" else True
        estimate = None if not usage_valid or not model_valid else (usage["prompt_tokens"] * budget["rates_usd_per_million"]["input_cache_miss_peak"] + usage["completion_tokens"] * budget["rates_usd_per_million"]["output_peak"]) / 1_000_000
        item = {"sample_id": next_sample["sample_id"], "case_id": next_sample["case_id"], "attempt": 1,
                "request_binding": {"payload_sha256": next_sample["payload_sha256"], "messages_sha256": next_sample["messages_sha256"],
                                    "manifest_sha256": sha(MANIFEST.read_bytes()), "model": settings.model, "endpoint": settings.base_url,
                                    "temperature": 0, "max_tokens": MAX_OUTPUT, "stream": False, "thinking": "disabled", "automatic_retries": 0},
                "response": {"response_received": True, "response_model": getattr(response, "model", None), "choices_count": len(response.choices),
                             "finish_reason": finish, "content": content,"reasoning_content_present":isinstance(reasoning,str) and bool(reasoning),
                             "reasoning_content_length":len(reasoning) if isinstance(reasoning,str) else None,"reasoning_content_saved":False},
                "parse_status": "VALID" if valid else "FORMAT_FAILURE", "parse_errors": errors,
                "parsed_judge_output": parsed, "usage": usage if usage_valid else "UNKNOWN",
                "usage_valid": bool(usage_valid), "local_estimated_usd": round(estimate, 10) if estimate is not None else None,
                "model_binding_valid":model_valid,
                "provider_invoice": "UNKNOWN", "proxy": proxy, "pricing_evidence_age_seconds": price_age}
        existing["samples"].append(item); atomic(RESULTS, existing)
        if not usage_valid or estimate is None or estimate > reserve_usd+1e-9:
            state = read(STATE); state["usage_unknown"] = not usage_valid;state["pricing_unknown"]=not model_valid
            if usage_valid:
                state["settled_tokens"]+=usage["total_tokens"];state["reserved_tokens"]-=12000
            state["status"] = "STOP_USAGE_OR_BUDGET_UNKNOWN"; atomic(STATE, state)
            journal("SAMPLE_STOPPED", sample_id=next_sample["sample_id"], reason="usage_unknown_or_local_cap_exceeded")
            close("USAGE_UNKNOWN_OR_BUDGET_EXCEEDED")
            return item
        state = read(STATE)
        state["reserved_tokens"] -= 12000; state["reserved_usd"] = round(state["reserved_usd"] - reserve_usd, 8)
        state["settled_tokens"] += usage["total_tokens"]; state["local_estimated_usd"] = round(state["local_estimated_usd"] + estimate, 10)
        state["settled_sample_ids"] = state.get("settled_sample_ids", []) + [next_sample["sample_id"]]
        if not valid:
            state["status"] = "STOP_FORMAT_FAILURE"; atomic(STATE, state)
            journal("SAMPLE_STOPPED", sample_id=next_sample["sample_id"], reason="response_contract_invalid", errors=errors)
            close("RESPONSE_FORMAT_FAILURE")
            return item
        atomic(STATE, state)
        journal("SAMPLE_SETTLED_VALID", sample_id=next_sample["sample_id"], usage=usage,
                local_estimated_usd=round(estimate, 10), response_decision=parsed["decision"])
        if len(state["settled_sample_ids"]) == MAX_REQUESTS:
            close("BATCH_COMPLETED")
        return item
    except Exception as exc:
        # No raw exception, request body, headers, key, or reasoning text is persisted.
        state = read(STATE); state["usage_unknown"] = True; state["status"] = "STOP_REQUEST_EXCEPTION_USAGE_UNKNOWN"; atomic(STATE, state)
        journal("SAMPLE_STOPPED", sample_id=next_sample["sample_id"], exception_type=type(exc).__name__, reason="request_outcome_or_usage_unknown")
        close("REQUEST_EXCEPTION_USAGE_UNKNOWN")
        return {"sample_id": next_sample["sample_id"], "status": "REQUEST_EXCEPTION_USAGE_UNKNOWN", "exception_type": type(exc).__name__, "usage": "UNKNOWN"}


if __name__ == "__main__":
    if sys.argv[1:3]==["--profile","r23-confirmed-four"]:
        select_profile("r23-confirmed-four")
        sys.argv=[sys.argv[0],*sys.argv[3:]]
    if sys.argv[1:] == ["--preflight"]:
        m, a, s, b, samples, settings, proxy, price_age = preflight()
        print(json.dumps({"status": "PREFLIGHT_PASSED", "batch_id": m["batch_id"], "samples": m["sample_order"],
                          "provider_requests_this_command": 0, "agent_runs": 0, "business_tool_calls": 0,
                          "model": settings.model, "endpoint": settings.base_url, "proxy": proxy,
                          "price_evidence_age_seconds": price_age}, ensure_ascii=False))
    elif sys.argv[1:] == ["--send-next"]:
        print(json.dumps(send_next(), ensure_ascii=False))
    else:
        raise SystemExit("use --preflight or --send-next")
