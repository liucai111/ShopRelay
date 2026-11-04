"""Metadata-only dataset registry and default-deny case access guard."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import hashlib
import os
from pathlib import Path
import re
from typing import Any, Iterable, Mapping

from .contracts import ContractError, EvalCase, ExposureStatus, IndependenceStatus, ReviewStatus, Scoreability, Split, sha256_json


REGISTRY_SCHEMA_VERSION = "1.0"
_HEX64 = re.compile(r"^[0-9a-f]{64}$", re.I)
PROTECTED_SPLITS = {Split.TEST_CANDIDATE.value, Split.FROZEN_TEST.value}
MIXED_SPLIT = Split.MIXED.value


class RegistryError(ValueError):
    pass


class AccessDenied(PermissionError):
    pass


@dataclass(frozen=True)
class DatasetAsset:
    asset_id: str
    asset_type: str
    version: str
    parent_asset_id: str | None
    split: str
    case_count: int | None
    source_path: str
    sha256: str | None
    review_status: str
    reviewer_id: str | None
    reviewed_at_utc: str | None
    review_method: str | None
    independence_status: str
    exposure_status: str
    scoreability: str
    locked_at_utc: str | None
    access_policy: str
    limitations: tuple[str, ...]
    integrity_status: str = "VERIFIED"
    historical_source_ref: str | None = None

    def __post_init__(self) -> None:
        for name in ("asset_id", "asset_type", "version"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise RegistryError(f"{name} is required")
        if self.parent_asset_id is not None and not isinstance(self.parent_asset_id, str):
            raise RegistryError("parent_asset_id must be a string or null")
        if self.split not in {item.value for item in Split}:
            raise RegistryError(f"unsupported split: {self.split}")
        if self.case_count is not None and (isinstance(self.case_count, bool) or not isinstance(self.case_count, int) or self.case_count < 0):
            raise RegistryError("case_count must be a non-negative integer or null")
        if not isinstance(self.source_path, str) or not self.source_path:
            raise RegistryError("source_path is required")
        if self.sha256 is not None and not _HEX64.fullmatch(self.sha256):
            raise RegistryError("sha256 must be a full SHA-256 digest or null")
        if self.integrity_status not in {"VERIFIED", "NOT_VERIFIED", "INVALID_MANIFEST", "UNKNOWN"}:
            raise RegistryError("unsupported integrity_status")
        for name, allowed in (
            ("review_status", {x.value for x in ReviewStatus}),
            ("independence_status", {x.value for x in IndependenceStatus}),
            ("exposure_status", {x.value for x in ExposureStatus}),
            ("scoreability", {x.value for x in Scoreability}),
        ):
            if getattr(self, name) not in allowed:
                raise RegistryError(f"unsupported {name}")
        reviewed=self.review_status in {ReviewStatus.INDEPENDENT_REVIEWED.value,ReviewStatus.HUMAN_CONFIRMED_AI_BLIND_REVIEWED.value}
        if self.review_status==ReviewStatus.HUMAN_CONFIRMED_AI_BLIND_REVIEWED.value and self.review_method!='USER_REVIEWED_AI_THEN_CONTEXT_ISOLATED_AI_BLIND_V1':
            raise RegistryError('amended review requires its explicit method; not legacy independent review')
        if reviewed:
            if not all((self.reviewer_id, self.reviewed_at_utc, self.review_method)):
                raise RegistryError("independent review requires reviewer, time and method")
        if self.locked_at_utc is None and self.scoreability == Scoreability.ELIGIBLE.value:
            raise RegistryError("scoreable assets must have an explicit lock timestamp")
        if self.scoreability == Scoreability.ELIGIBLE.value:
            if not (
                reviewed
                and self.reviewer_id and self.reviewed_at_utc and self.review_method
                and self.independence_status == IndependenceStatus.INDEPENDENT.value
                and self.exposure_status == ExposureStatus.UNEXPOSED_VERIFIED.value
                and self.integrity_status == "VERIFIED" and self.sha256
                and self.split in {Split.DEV.value, Split.VALIDATION.value, Split.FROZEN_TEST.value}
            ):
                raise RegistryError("ELIGIBLE requires verified independent review, integrity, lock, and non-candidate split")
        if not isinstance(self.limitations, (tuple, list)) or any(not isinstance(item, str) for item in self.limitations):
            raise RegistryError("limitations must be a string list")
        for field_name in ("reviewed_at_utc", "locked_at_utc"):
            value = getattr(self, field_name)
            if value is not None:
                _validate_utc(value, field_name)

    def metadata(self) -> dict[str, Any]:
        """Safe registry projection; this method never opens the asset path."""
        return asdict(self)


def _validate_utc(value: str, field_name: str) -> None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistryError(f"{field_name} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RegistryError(f"{field_name} must include timezone")


@dataclass(frozen=True)
class AccessGrant:
    authorization_id: str
    approved_by: str
    approved_at_utc: str
    asset_ids: tuple[str, ...]
    allowed_splits: tuple[str, ...]
    purpose: str
    expires_at_utc: str
    allow_protected_splits: bool = False

    def valid_for(self, asset: DatasetAsset, now: datetime | None = None) -> bool:
        if asset.asset_id not in self.asset_ids or asset.split not in self.allowed_splits:
            return False
        if asset.split in PROTECTED_SPLITS and not self.allow_protected_splits:
            return False
        _validate_utc(self.approved_at_utc, "approved_at_utc")
        _validate_utc(self.expires_at_utc, "expires_at_utc")
        now = now or datetime.now(timezone.utc)
        approved = datetime.fromisoformat(self.approved_at_utc.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(self.expires_at_utc.replace("Z", "+00:00"))
        return bool(self.authorization_id and self.approved_by and self.purpose and approved <= now < expires
                    and asset.scoreability == Scoreability.ELIGIBLE.value)


class DatasetRegistry:
    def __init__(self, assets: Iterable[DatasetAsset] = ()) -> None:
        self._assets: dict[str, DatasetAsset] = {}
        for asset in assets:
            self.add(asset)

    def add(self, asset: DatasetAsset) -> None:
        if asset.asset_id in self._assets:
            existing = self._assets[asset.asset_id]
            if existing != asset:
                raise RegistryError(f"asset_id is immutable; create a new version/id: {asset.asset_id}")
            raise RegistryError(f"duplicate asset_id: {asset.asset_id}")
        if asset.parent_asset_id and asset.parent_asset_id not in self._assets:
            raise RegistryError(f"unknown parent asset: {asset.parent_asset_id}")
        self._assets[asset.asset_id] = asset

    def get(self, asset_id: str) -> DatasetAsset:
        try:
            return self._assets[asset_id]
        except KeyError as exc:
            raise RegistryError("asset not registered") from exc

    def metadata(self, asset_id: str) -> dict[str, Any]:
        asset = self.get(asset_id)
        # This is intentionally narrower than the internal registry record.
        # In particular, it never returns limitations or case-level content.
        return {
            "asset_id": asset.asset_id,
            "file_path": asset.source_path,
            "split": asset.split,
            "version": asset.version,
            "sha256": asset.sha256,
            "review_status": asset.review_status,
            "exposure_status": asset.exposure_status,
            "scoreability": asset.scoreability,
        }

    def safe_metadata(self) -> list[dict[str, Any]]:
        """Return only the approved metadata projection; source files are never opened."""
        return [self.metadata(asset.asset_id) for asset in self.assets()]

    def assets(self) -> tuple[DatasetAsset, ...]:
        return tuple(self._assets.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "registry_sha256": sha256_json([asset.metadata() for asset in self._assets.values()]),
            "assets": [asset.metadata() for asset in self._assets.values()],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DatasetRegistry":
        if set(data) != {"schema_version", "registry_sha256", "assets"}:
            raise RegistryError("registry has missing or unknown fields")
        if data["schema_version"] != REGISTRY_SCHEMA_VERSION:
            raise RegistryError("unsupported registry schema version")
        if not isinstance(data["assets"], list):
            raise RegistryError("assets must be a list")
        assets = []
        for entry in data["assets"]:
            if not isinstance(entry, Mapping):
                raise RegistryError("asset entry must be an object")
            allowed = set(DatasetAsset.__dataclass_fields__) - {"integrity_status", "historical_source_ref"}
            allowed |= {"integrity_status", "historical_source_ref"}
            if set(entry) != allowed:
                raise RegistryError("asset entry fields do not match registry schema")
            item = dict(entry)
            item["limitations"] = tuple(item["limitations"])
            assets.append(DatasetAsset(**item))
        registry = cls(assets)
        expected = sha256_json([asset.metadata() for asset in registry.assets()])
        if data["registry_sha256"] != expected:
            raise RegistryError("registry hash mismatch")
        return registry

    @classmethod
    def load_metadata(cls, path: Path) -> "DatasetRegistry":
        # This reads the registry document only, never any source_path listed inside it.
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def save_new(self, path: Path) -> None:
        """Create an immutable registry snapshot; never overwrite an existing version."""
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n"
        try:
            with path.open("x", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
        except FileExistsError as exc:
            raise RegistryError("registry snapshots are immutable; choose a new versioned path") from exc


class AccessGuard:
    """Default-deny content loader. Metadata reads are a separate operation."""

    def __init__(self, registry: DatasetRegistry, repository_root: Path, audit_path: Path) -> None:
        self.registry = registry
        self.root = repository_root.resolve(strict=True)
        self.audit_path = audit_path

    def metadata(self, asset_id: str) -> dict[str, Any]:
        return self.registry.metadata(asset_id)

    def metadata_only(self) -> list[dict[str, Any]]:
        return self.registry.safe_metadata()

    def read_cases(
        self,
        asset_id: str,
        *,
        principal: str,
        grant: AccessGrant | None = None,
        requested_path: Path | None = None,
    ) -> list[dict[str, Any]]:
        try:
            asset = self.registry.get(asset_id)
        except RegistryError:
            self._audit_unregistered(principal, "UNREGISTERED_ASSET_CONTENT_READ")
            raise AccessDenied("UNREGISTERED_ASSET")
        source = Path(asset.source_path)
        expected = (self.root / source).resolve(strict=False)
        reason: str | None = None
        if source.is_absolute() or not expected.is_relative_to(self.root):
            reason = "PATH_OUTSIDE_REGISTERED_REPOSITORY"
        elif requested_path is not None and requested_path.resolve(strict=False) != expected:
            reason = "PATH_ALIAS_OR_UNREGISTERED_COPY"
        elif asset.split == MIXED_SPLIT:
            reason = "MIXED_SPLIT_CONTENT_READ_FORBIDDEN"
        elif asset.split in PROTECTED_SPLITS and grant is None:
            reason = "PROTECTED_SPLIT_NO_EXPLICIT_AUTHORIZATION"
        elif asset.integrity_status != "VERIFIED" or not asset.sha256:
            reason = "ASSET_INTEGRITY_NOT_VERIFIED"
        elif grant is None or not grant.valid_for(asset):
            reason = "NO_VALID_ASSET_SPLIT_GRANT_OR_NOT_SCOREABLE"
        elif not expected.is_file():
            reason = "ASSET_FILE_NOT_FOUND"
        if reason:
            self._audit(asset, principal, "CASE_CONTENT_READ", "DENIED", reason)
            raise AccessDenied(reason)
        try:
            content = expected.read_bytes()
            actual_hash = hashlib.sha256(content).hexdigest()
            if actual_hash.lower() != asset.sha256.lower():
                self._audit(asset, principal, "CASE_CONTENT_READ", "DENIED", "ASSET_HASH_MISMATCH")
                raise AccessDenied("ASSET_HASH_MISMATCH")
            raw = json.loads(content.decode("utf-8"))
        except AccessDenied:
            raise
        except Exception as exc:
            self._audit(asset, principal, "CASE_CONTENT_READ", "ERROR", "ASSET_PARSE_ERROR")
            raise RegistryError("permitted asset could not be parsed") from exc
        cases = raw.get("cases") if isinstance(raw, Mapping) else None
        if not isinstance(cases, list):
            self._audit(asset, principal, "CASE_CONTENT_READ", "ERROR", "CASE_LIST_MISSING")
            raise RegistryError("permitted asset lacks a cases list")
        validated: list[EvalCase] = []
        try:
            for raw_case in cases:
                item = EvalCase.from_dict(raw_case)
                if item.split != asset.split or item.data_version != asset.version:
                    raise ContractError("case split/data_version does not match registry asset")
                validated.append(item)
        except ContractError as exc:
            self._audit(asset, principal, "CASE_CONTENT_READ", "ERROR", "CASE_SCHEMA_OR_VERSION_INVALID")
            raise RegistryError("case content does not match registered EvalCase contract") from exc
        self._audit(asset, principal, "CASE_CONTENT_READ", "ALLOWED", "AUTHORIZED_ASSET_CONTENT")
        return validated

    def _audit(self, asset: DatasetAsset, principal: str, action: str, decision: str, reason: str) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "asset_id": asset.asset_id,
            "split": asset.split,
            "principal": principal,
            "action": action,
            "decision": decision,
            "reason": reason,
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")

    def _audit_unregistered(self, principal: str, reason: str) -> None:
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "asset_id": None,
            "split": None,
            "principal": principal,
            "action": "CASE_CONTENT_READ",
            "decision": "DENIED",
            "reason": reason,
        }
        with self.audit_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps(event, sort_keys=True) + "\n")
