"""Deterministic local configuration for the M0 baseline and probes."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG = ROOT / ".env.m0.local"


def parse_env_file(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def _local_values(path: Path) -> dict[str, str]:
    try:
        return parse_env_file(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


@dataclass(frozen=True)
class Setting:
    value: str
    source: str


@dataclass(frozen=True)
class NodeConfig:
    name: str
    scope: str
    thinking: str
    max_tokens: int
    timeout_seconds: float
    max_cost_usd: float
    locked: bool = False


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    api_key_source: str
    base_url: str
    base_url_source: str
    model: str
    model_source: str
    database_url: str | None = field(repr=False)
    database_url_source: str | None
    allowed_origins: str
    cookie_secure: bool
    nodes: Mapping[str, NodeConfig]
    orchestrator: str = "legacy"
    checkpoint_schema: str = "shoprelay_langgraph"
    graph_version: str = "g2"
    execution_version: str = "g2-api-v1"
    observability_admin_user_ids: tuple[str, ...] = ()
    m3_supervisor_enabled: bool = False


def load_settings(*, environ: Mapping[str, str] | None = None, local_path: Path = LOCAL_CONFIG,
                  local_values: Mapping[str, str] | None = None, require_api_key: bool = True) -> Settings:
    env = os.environ if environ is None else environ
    local = dict(local_values) if local_values is not None else _local_values(local_path)

    def get(name: str, default: str = "") -> Setting:
        # Presence wins, including an explicitly empty process value.
        if name in env:
            return Setting(env[name], "process_environment")
        if name in local:
            return Setting(local[name], ".env.m0.local")
        return Setting(default, "default")

    key = get("DEEPSEEK_API_KEY")
    if require_api_key and not key.value.strip():
        raise RuntimeError("DeepSeek API key is missing; set process DEEPSEEK_API_KEY or add it to the Git-ignored .env.m0.local. Secret values are never logged.")
    base = get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = get("DEEPSEEK_MODEL", "deepseek-flash")
    db = get("DATABASE_URL")
    if not db.value:
        password = get("SHOPRELAY_POSTGRES_PASSWORD")
        if password.value:
            from urllib.parse import quote
            db = Setting(f"postgresql://shoprelay:{quote(password.value, safe='')}@127.0.0.1:5432/shoprelay", password.source)
        else:
            db = Setting("", "unset")

    def node(name: str, scope: str, defaults: tuple[str, int, float, float], locked: bool = False) -> NodeConfig:
        thinking, tokens, timeout, cost = defaults
        prefix = f"SHOPRELAY_DEEPSEEK_{name.upper()}_"
        return NodeConfig(name, scope, get(prefix + "THINKING", thinking).value,
                          int(get(prefix + "MAX_TOKENS", str(tokens)).value),
                          float(get(prefix + "TIMEOUT_SECONDS", str(timeout)).value),
                          float(get(prefix + "MAX_COST_USD", str(cost)).value), locked)

    nodes = {
        "policy_answer": node("policy_answer", "implemented_m0_baseline", ("disabled", 512, 20, .10), True),
        "business_answer": node("business_answer", "implemented_m63_bounded_agent", ("disabled", 1536, 60, .10)),
        "route": node("route", "implemented_m2_1", ("disabled", 384, 10, .01)),
        "route_diagnostic": node("route_diagnostic", "diagnostic_only", ("disabled", 768, 40, .01)),
        "planning_diagnostic": node("planning_diagnostic", "diagnostic_only", ("disabled", 1536, 40, .03)),
        "condition_extraction": node("condition_extraction", "implemented_m2_3", ("disabled", 768, 20, .02)),
        "policy_aspect_planning": node("policy_aspect_planning", "implemented_m2_r", ("disabled", 512, 15, .02)),
        "evidence_sufficiency": node("evidence_sufficiency", "implemented_m2_3", ("disabled", 3072, 30, .03)),
        "evidence_sufficiency_diagnostic": node("evidence_sufficiency_diagnostic", "diagnostic_only", ("enabled", 768, 40, .02)),
    }
    for cfg in nodes.values():
        if cfg.thinking not in {"enabled", "disabled"} or cfg.max_tokens < 1 or cfg.timeout_seconds <= 0 or cfg.max_cost_usd < 0:
            raise ValueError(f"invalid DeepSeek configuration for node {cfg.name}")
    secure_value = get("SHOPRELAY_COOKIE_SECURE", "false").value.strip().lower()
    if secure_value not in {"true", "false"}:
        raise ValueError("SHOPRELAY_COOKIE_SECURE must be true or false")
    orchestrator = get("SHOPRELAY_ORCHESTRATOR", "legacy").value.strip().lower()
    if orchestrator not in {"legacy", "langgraph"}:
        raise ValueError("SHOPRELAY_ORCHESTRATOR must be legacy or langgraph")
    supervisor_value = get("SHOPRELAY_M3_SUPERVISOR_ENABLED", "false").value.strip().lower()
    if supervisor_value not in {"true", "false"}:
        raise ValueError("SHOPRELAY_M3_SUPERVISOR_ENABLED must be true or false")
    supervisor_enabled = supervisor_value == "true"
    if supervisor_enabled and orchestrator != "langgraph":
        raise ValueError("SHOPRELAY_M3_SUPERVISOR_ENABLED requires SHOPRELAY_ORCHESTRATOR=langgraph")
    checkpoint_schema = get("SHOPRELAY_LANGGRAPH_CHECKPOINT_SCHEMA", "shoprelay_langgraph").value.strip()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", checkpoint_schema):
        raise ValueError("SHOPRELAY_LANGGRAPH_CHECKPOINT_SCHEMA must be a simple PostgreSQL schema name")
    graph_version = get("SHOPRELAY_GRAPH_VERSION", "g2").value.strip()
    execution_version = get("SHOPRELAY_EXECUTION_VERSION", "g2-api-v1").value.strip()
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", graph_version):
        raise ValueError("SHOPRELAY_GRAPH_VERSION is invalid")
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", execution_version):
        raise ValueError("SHOPRELAY_EXECUTION_VERSION is invalid")
    admin_ids = tuple(sorted({item.strip() for item in get("SHOPRELAY_OBSERVABILITY_ADMIN_USER_IDS").value.split(",")
                              if item.strip()}))
    if any(len(item) > 64 for item in admin_ids):
        raise ValueError("SHOPRELAY_OBSERVABILITY_ADMIN_USER_IDS contains an invalid identifier")
    return Settings(key.value, key.source, base.value, base.source, model.value, model.source,
                    db.value or None, db.source if db.value else None,
                    get("SHOPRELAY_ALLOWED_ORIGINS").value, secure_value == "true", nodes,
                    orchestrator, checkpoint_schema, graph_version, execution_version, admin_ids,
                    supervisor_enabled)


def create_deepseek_client(settings: Settings, node: NodeConfig):
    from openai import OpenAI
    return OpenAI(api_key=settings.api_key, base_url=settings.base_url,
                  timeout=node.timeout_seconds, max_retries=0)
