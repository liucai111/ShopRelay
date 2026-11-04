"""Append-only raw trial and scoring-result JSONL contracts (no runner logic)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .contracts import ContractError, EvalOutcome, EvalTrial, redact


class AppendOnlyError(ValueError):
    pass


def _append_once(path: Path, record: Mapping[str, Any], key: str, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                previous = json.loads(line)
            except json.JSONDecodeError as exc:
                raise AppendOnlyError("existing append log is malformed; refusing mutation") from exc
            if previous.get(key) == value:
                raise AppendOnlyError(f"duplicate {key}; records are immutable")
    payload = json.dumps(redact(record), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(payload + "\n")


def append_raw_trial(path: Path, trial: EvalTrial) -> None:
    _append_once(path, trial.to_dict(), "trial_id", trial.trial_id)


def append_outcome(path: Path, outcome: EvalOutcome) -> None:
    _append_once(path, outcome.to_dict(), "outcome_id", outcome.outcome_id)


def validate_trial_links(trial: EvalTrial, *, known_case_ids: set[str], known_experiment_ids: set[str]) -> None:
    if trial.case_id not in known_case_ids:
        raise ContractError("trial references unknown case_id")
    if trial.experiment_id not in known_experiment_ids:
        raise ContractError("trial references unknown experiment_id")
