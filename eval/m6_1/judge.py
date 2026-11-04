"""Judge protocol and deterministic synthetic-only stub. No provider adapter exists here."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class JudgeRubric:
    rubric_id: str
    version: str
    dimensions: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.rubric_id or not self.version or not self.dimensions or len(set(self.dimensions)) != len(self.dimensions):
            raise ValueError("judge rubric requires unique dimensions and versioned identity")


@dataclass(frozen=True)
class JudgeRequest:
    request_id: str
    trial_id: str
    rubric: JudgeRubric
    blinded_input: Mapping[str, Any]
    allowed_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.request_id or not self.trial_id or not isinstance(self.blinded_input, Mapping):
            raise ValueError("judge request requires identifiers and a blinded object")


@dataclass(frozen=True)
class JudgeResponse:
    request_id: str
    trial_id: str
    verdicts: Mapping[str, str]
    rationale_codes: Mapping[str, str]
    usage: Mapping[str, int | None]
    provider: str = "SYNTHETIC_STUB"
    finish_reason: str = "stub_complete"

    def __post_init__(self) -> None:
        if not self.request_id or not self.trial_id or self.provider != "SYNTHETIC_STUB":
            raise ValueError("M6.1-B JudgeResponse must identify the synthetic stub; external providers are disabled")


class Judge(Protocol):
    def grade(self, request: JudgeRequest) -> JudgeResponse: ...


class SyntheticJudgeStub:
    """Returns a fixed fixture response; never imports a network or model SDK."""
    def __init__(self, response_by_trial: Mapping[str, Mapping[str, str]]) -> None:
        self.response_by_trial = {key: dict(value) for key, value in response_by_trial.items()}
        self.calls = 0

    def grade(self, request: JudgeRequest) -> JudgeResponse:
        self.calls += 1
        verdicts = self.response_by_trial.get(request.trial_id)
        if verdicts is None:
            verdicts = {dimension: "UNKNOWN" for dimension in request.rubric.dimensions}
        allowed = {"PASS", "FAIL", "UNKNOWN", "UNSCORABLE"}
        if set(verdicts) != set(request.rubric.dimensions) or any(value not in allowed for value in verdicts.values()):
            raise ValueError("stub response does not match judge rubric schema")
        return JudgeResponse(request.request_id, request.trial_id, verdicts,
                             {key: "SYNTHETIC_FIXTURE" for key in verdicts},
                             {"prompt_tokens": None, "completion_tokens": None})
