"""Strict, versioned M6.1 evaluation serialization contracts.

These contracts deliberately model execution lifecycle and grading separately.
They are data formats only; they do not invoke an application, provider, or grader.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from datetime import date, datetime, timezone
import re
from typing import Any, ClassVar, Mapping, TypeVar
from types import MappingProxyType


SCHEMA_VERSION = "1.0"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_SECRET_KEY_RE = re.compile(r"password|passwd|secret|api[_-]?key|authorization|cookie", re.I)
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(Bearer\s+\S+|(?:api[_-]?key|password|secret|token)\s*[:=]\s*\S+)"
)


class ContractError(ValueError):
    """Input does not conform to the versioned evaluation contract."""


class Split(str, Enum):
    DEV = "dev"
    VALIDATION = "validation"
    SEEN_VALIDATION = "SEEN_VALIDATION"
    TEST_CANDIDATE = "test_candidate"
    FROZEN_TEST = "frozen_test"
    SYNTHETIC = "synthetic"
    MIXED = "MIXED"
    UNKNOWN = "UNKNOWN"


class ReviewStatus(str, Enum):
    HUMAN_CONFIRMED_AI_BLIND_REVIEWED = "HUMAN_CONFIRMED_AI_BLIND_REVIEWED"
    BATCH_USER_CONFIRMED = "BATCH_USER_CONFIRMED"
    INDEPENDENT_REVIEWED = "INDEPENDENT_REVIEWED"
    CANDIDATE = "CANDIDATE"
    UNKNOWN = "UNKNOWN"
    NOT_REVIEWED = "NOT_REVIEWED"


class IndependenceStatus(str, Enum):
    INDEPENDENT = "INDEPENDENT"
    SEEN_VALIDATION = "SEEN_VALIDATION"
    HISTORICALLY_EXPOSED = "HISTORICALLY_EXPOSED"
    NOT_VERIFIED = "NOT_VERIFIED"
    UNKNOWN = "UNKNOWN"


class ExposureStatus(str, Enum):
    UNEXPOSED_VERIFIED = "UNEXPOSED_VERIFIED"
    SEEN_VALIDATION = "SEEN_VALIDATION"
    HISTORIC_EXPOSURE_DISCLOSED = "HISTORIC_EXPOSURE_DISCLOSED"
    NOT_VERIFIED = "NOT_VERIFIED"
    UNKNOWN = "UNKNOWN"


class Scoreability(str, Enum):
    ELIGIBLE = "ELIGIBLE"
    HISTORICAL_ONLY = "HISTORICAL_ONLY"
    NOT_SCOREABLE = "NOT_SCOREABLE"
    NOT_VERIFIED = "NOT_VERIFIED"


class LifecycleStatus(str, Enum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    UNKNOWN_RESULT = "UNKNOWN_RESULT"
    UNSCORABLE = "UNSCORABLE"
    NOT_RUN = "NOT_RUN"
    BUDGET_STOP = "BUDGET_STOP"
    ENV_ERROR = "ENV_ERROR"
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"


class GradingStatus(str, Enum):
    NOT_GRADED = "NOT_GRADED"
    PENDING_HUMAN_REVIEW = "PENDING_HUMAN_REVIEW"
    GRADED = "GRADED"
    UNSCORABLE = "UNSCORABLE"
    GRADING_ERROR = "GRADING_ERROR"


class Verdict(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


def _utc(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ContractError(f"{field_name} must be an ISO-8601 string or null")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractError(f"{field_name} must include a UTC offset")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise ContractError(f"{field_name} must be a non-empty stable identifier")
    return value


def _strict_keys(cls: type, data: Mapping[str, Any]) -> None:
    allowed = {f.name for f in fields(cls)} | {"schema_version"}
    unknown = set(data) - allowed
    # Required-field checks are handled by dataclass construction; reject only extras here.
    if unknown:
        raise ContractError(f"unknown fields for {cls.__name__}: {', '.join(sorted(unknown))}")


def redact(value: Any) -> Any:
    """Return a JSON-safe copy with secret-like keys and inline credentials removed."""
    if isinstance(value, Mapping):
        return {
            str(key): ("[REDACTED]" if _is_secret_key(str(key)) and not _is_safe_usage_counter(str(key), item)
                       else redact(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(item) for item in value]
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ContractError("naive datetime is not allowed in serialized evidence")
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, str):
        return _SENSITIVE_VALUE_RE.sub("[REDACTED]", value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ContractError(f"unsupported value type: {type(value).__name__}")


def _is_safe_usage_counter(key: str, value: Any) -> bool:
    """Keep bounded numeric usage counters while continuing to redact token secrets."""
    return (key.casefold() in {"prompt_tokens", "completion_tokens", "total_tokens"} and
            isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10_000_000)


def _is_secret_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    if _SECRET_KEY_RE.search(normalized):
        return True
    return normalized in {"token", "access_token", "refresh_token", "session_token", "auth_token", "csrf_token", "api_token"}


def canonical_json(value: Any) -> str:
    return json.dumps(redact(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ContractError(f"unsupported value type: {type(value).__name__}")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


T = TypeVar("T", bound="StrictModel")


class StrictModel:
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        data = {field.name: getattr(self, field.name) for field in fields(self)}
        data["schema_version"] = self.schema_version
        return redact(data)

    @classmethod
    def from_dict(cls: type[T], data: Mapping[str, Any]) -> T:
        if not isinstance(data, Mapping):
            raise ContractError(f"{cls.__name__} must be an object")
        _strict_keys(cls, data)
        supplied_version = data.get("schema_version", SCHEMA_VERSION)
        if supplied_version != SCHEMA_VERSION:
            raise ContractError(f"unsupported schema_version: {supplied_version}")
        values = dict(data)
        values.pop("schema_version", None)
        try:
            return cls(**values)  # type: ignore[arg-type]
        except TypeError as exc:
            raise ContractError(f"invalid {cls.__name__} fields") from exc


@dataclass(frozen=True)
class EvalCase(StrictModel):
    case_id: str
    group_id: str
    split: str
    input: Mapping[str, Any]
    expected_outcome: Mapping[str, Any]
    rubric_refs: tuple[str, ...]
    fixture_ref: str | None
    risk_level: str
    data_version: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        _identifier(self.case_id, "case_id")
        _identifier(self.group_id, "group_id")
        _identifier(self.data_version, "data_version")
        if self.split not in {s.value for s in Split}:
            raise ContractError("split is not a supported value")
        if not isinstance(self.input, Mapping) or not isinstance(self.expected_outcome, Mapping):
            raise ContractError("input and expected_outcome must be objects")
        if not isinstance(self.rubric_refs, (tuple, list)) or any(not isinstance(x, str) for x in self.rubric_refs):
            raise ContractError("rubric_refs must be a string list")
        if self.fixture_ref is not None and not isinstance(self.fixture_ref, str):
            raise ContractError("fixture_ref must be a string or null")
        if not isinstance(self.risk_level, str) or not self.risk_level:
            raise ContractError("risk_level is required")
        object.__setattr__(self, "input", _freeze(self.input))
        object.__setattr__(self, "expected_outcome", _freeze(self.expected_outcome))
        object.__setattr__(self, "rubric_refs", tuple(self.rubric_refs))


@dataclass(frozen=True)
class EvalTrial(StrictModel):
    trial_id: str
    case_id: str
    experiment_id: str
    trial_index: int
    run_ids: tuple[str, ...]
    lifecycle_status: str
    model_calls: tuple[Mapping[str, Any], ...]
    tool_calls: tuple[Mapping[str, Any], ...]
    raw_result_ref: str | None
    error: Mapping[str, Any] | None
    usage: Mapping[str, Any]
    latency_ms: Mapping[str, int | float | None]
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("trial_id", "case_id", "experiment_id"):
            _identifier(getattr(self, name), name)
        if isinstance(self.trial_index, bool) or not isinstance(self.trial_index, int) or self.trial_index < 1:
            raise ContractError("trial_index must be a positive integer")
        if not isinstance(self.run_ids, (tuple, list)) or any(not isinstance(x, str) for x in self.run_ids):
            raise ContractError("run_ids must be a string list")
        if self.lifecycle_status not in {s.value for s in LifecycleStatus}:
            raise ContractError("unsupported lifecycle_status")
        if not isinstance(self.model_calls, (tuple, list)) or not isinstance(self.tool_calls, (tuple, list)):
            raise ContractError("model_calls and tool_calls must be lists")
        if any(not isinstance(item, Mapping) for item in (*self.model_calls, *self.tool_calls)):
            raise ContractError("model_calls and tool_calls entries must be objects")
        if self.error is not None and not isinstance(self.error, Mapping):
            raise ContractError("error must be an object or null")
        if self.raw_result_ref is not None and not isinstance(self.raw_result_ref, str):
            raise ContractError("raw_result_ref must be a string or null")
        if not isinstance(self.usage, Mapping) or not isinstance(self.latency_ms, Mapping):
            raise ContractError("usage and latency_ms must be objects")
        if any(v is not None and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0) for v in self.latency_ms.values()):
            raise ContractError("latency_ms values must be non-negative numbers or null")
        object.__setattr__(self, "run_ids", tuple(self.run_ids))
        object.__setattr__(self, "model_calls", tuple(_freeze(self.model_calls)))
        object.__setattr__(self, "tool_calls", tuple(_freeze(self.tool_calls)))
        object.__setattr__(self, "usage", _freeze(self.usage))
        object.__setattr__(self, "latency_ms", _freeze(self.latency_ms))
        if self.error is not None:
            object.__setattr__(self, "error", _freeze(self.error))


@dataclass(frozen=True)
class EvalExperiment(StrictModel):
    experiment_id: str
    config_snapshot: Mapping[str, Any]
    config_sha256: str
    git_commit: str
    knowledge_index_version: str | None
    model_versions: Mapping[str, str]
    prompt_versions: Mapping[str, str]
    strategy: str
    tool_permissions: tuple[str, ...]
    budget: Mapping[str, Any]
    data_version: str
    scoring_version: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("experiment_id", "git_commit", "data_version", "scoring_version"):
            _identifier(getattr(self, name), name)
        if not re.fullmatch(r"[a-fA-F0-9]{64}", self.config_sha256):
            raise ContractError("config_sha256 must be a 64-character SHA-256 hex digest")
        if not isinstance(self.config_snapshot, Mapping) or not isinstance(self.budget, Mapping):
            raise ContractError("config_snapshot and budget must be objects")
        if sha256_json(self.config_snapshot) != self.config_sha256.lower():
            raise ContractError("config_snapshot hash mismatch")
        if not isinstance(self.model_versions, Mapping) or not isinstance(self.prompt_versions, Mapping):
            raise ContractError("model_versions and prompt_versions must be objects")
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in (*self.model_versions.items(), *self.prompt_versions.items())):
            raise ContractError("model_versions and prompt_versions values must be strings")
        if not isinstance(self.tool_permissions, (tuple, list)) or any(not isinstance(x, str) for x in self.tool_permissions):
            raise ContractError("tool_permissions must be a string list")
        object.__setattr__(self, "config_snapshot", _freeze(self.config_snapshot))
        object.__setattr__(self, "model_versions", _freeze(self.model_versions))
        object.__setattr__(self, "prompt_versions", _freeze(self.prompt_versions))
        object.__setattr__(self, "budget", _freeze(self.budget))
        object.__setattr__(self, "tool_permissions", tuple(self.tool_permissions))


@dataclass(frozen=True)
class EvalOutcome(StrictModel):
    outcome_id: str
    trial_id: str
    run_success: str
    answer_correctness: str
    task_success: str
    safety_result: str
    grading_status: str
    rubric_version: str
    score_refs: tuple[str, ...]
    created_at_utc: str
    schema_version: ClassVar[str] = SCHEMA_VERSION

    def __post_init__(self) -> None:
        for name in ("outcome_id", "trial_id", "rubric_version"):
            _identifier(getattr(self, name), name)
        for name in ("run_success", "answer_correctness", "task_success", "safety_result"):
            if getattr(self, name) not in {v.value for v in Verdict}:
                raise ContractError(f"unsupported {name}")
        if self.grading_status not in {v.value for v in GradingStatus}:
            raise ContractError("unsupported grading_status")
        if not isinstance(self.score_refs, (tuple, list)) or any(not isinstance(x, str) for x in self.score_refs):
            raise ContractError("score_refs must be a string list")
        normalized = _utc(self.created_at_utc, "created_at_utc")
        object.__setattr__(self, "created_at_utc", normalized)
        object.__setattr__(self, "score_refs", tuple(self.score_refs))


def validate_unique_ids(records: list[StrictModel], id_field: str) -> None:
    seen: set[str] = set()
    for record in records:
        value = getattr(record, id_field)
        if value in seen:
            raise ContractError(f"duplicate {id_field}: {value}")
        seen.add(value)
