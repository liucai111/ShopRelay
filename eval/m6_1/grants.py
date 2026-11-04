"""Scoped synthetic grants and separate owner-approved formal execution plans."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import sqlite3
from pathlib import Path
from typing import Any

from .contracts import EvalCase, Split
from .registry import DatasetAsset, DatasetRegistry, PROTECTED_SPLITS
from .budget import BudgetLimits


class GrantDenied(PermissionError):
    pass


@dataclass(frozen=True)
class SyntheticGrant:
    grant_id: str
    asset_id: str
    split: str
    sha256: str
    issuer: str
    subject: str
    expires_at_utc: str
    signature: str


class SyntheticGrantAuthority:
    """This authority intentionally cannot authorize DEV history, candidates or Frozen data."""
    FIXTURE_PREFIX = Path("eval") / "m6_1" / "fixtures"
    def __init__(self, registry: DatasetRegistry, *, repository_root: Path, issuer: str,
                 trusted_issuers: set[str], signing_key: bytes) -> None:
        if issuer not in trusted_issuers or len(signing_key) < 32:
            raise GrantDenied("untrusted issuer or insufficient signing key")
        self.registry, self.root, self.issuer, self.key = registry, repository_root.resolve(), issuer, signing_key
        self.revoked: set[str] = set()

    def _signature(self, fields: str) -> str:
        return hmac.new(self.key, fields.encode("utf-8"), hashlib.sha256).hexdigest()

    def issue(self, asset_id: str, *, subject: str, ttl_minutes: int = 30) -> SyntheticGrant:
        asset = self.registry.get(asset_id)
        if asset.split != Split.SYNTHETIC.value or not asset.asset_id.startswith("m61b-synthetic-"):
            raise GrantDenied("only newly registered M6.1-B synthetic assets may receive grants")
        relative = Path(asset.source_path)
        if relative.is_absolute() or relative.parent != self.FIXTURE_PREFIX or relative.suffix.casefold() != ".json":
            raise GrantDenied("only allowlisted synthetic fixture directory paths may receive grants")
        if not asset.sha256 or asset.integrity_status != "VERIFIED":
            raise GrantDenied("synthetic asset integrity is not verified")
        target = (self.root / asset.source_path).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            raise GrantDenied("asset path is outside the registered synthetic root or missing")
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if not hmac.compare_digest(digest, asset.sha256):
            raise GrantDenied("synthetic asset hash mismatch")
        grant_id = f"grant.{hashlib.sha256((asset_id+subject+digest).encode()).hexdigest()[:20]}"
        expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat().replace("+00:00", "Z")
        fields = "|".join((grant_id, asset_id, asset.split, digest, self.issuer, subject, expires))
        return SyntheticGrant(grant_id, asset_id, asset.split, digest, self.issuer, subject, expires, self._signature(fields))

    def revoke(self, grant: SyntheticGrant) -> None:
        self.revoked.add(grant.grant_id)

    def read_cases(self, grant: SyntheticGrant, *, subject: str) -> list[EvalCase]:
        if grant.grant_id in self.revoked or subject != grant.subject or grant.issuer != self.issuer:
            raise GrantDenied("grant is revoked or subject/issuer mismatch")
        if grant.split in PROTECTED_SPLITS or grant.split != Split.SYNTHETIC.value:
            raise GrantDenied("protected and non-synthetic splits are never authorized here")
        expires = datetime.fromisoformat(grant.expires_at_utc.replace("Z", "+00:00"))
        if datetime.now(timezone.utc) >= expires:
            raise GrantDenied("grant expired")
        fields = "|".join((grant.grant_id, grant.asset_id, grant.split, grant.sha256, grant.issuer, grant.subject, grant.expires_at_utc))
        if not hmac.compare_digest(self._signature(fields), grant.signature):
            raise GrantDenied("grant signature invalid")
        asset = self.registry.get(grant.asset_id)
        if asset.split != Split.SYNTHETIC.value or asset.sha256 != grant.sha256:
            raise GrantDenied("registry grant mismatch")
        relative = Path(asset.source_path)
        if relative.is_absolute() or relative.parent != self.FIXTURE_PREFIX or relative.suffix.casefold() != ".json":
            raise GrantDenied("only allowlisted synthetic fixture directory paths may be read")
        target = (self.root / asset.source_path).resolve()
        if not target.is_relative_to(self.root):
            raise GrantDenied("path traversal rejected")
        data = target.read_bytes()
        if not hmac.compare_digest(hashlib.sha256(data).hexdigest(), grant.sha256):
            raise GrantDenied("asset changed after grant issuance")
        payload = json.loads(data.decode("utf-8"))
        if set(payload) != {"schema_version", "asset_id", "data_version", "fixture_kind", "cases"}:
            raise GrantDenied("synthetic fixture schema mismatch")
        if payload["asset_id"] != asset.asset_id or payload["data_version"] != asset.version or payload["fixture_kind"] != "SYNTHETIC":
            raise GrantDenied("synthetic fixture identity mismatch")
        cases = [EvalCase.from_dict(row) for row in payload["cases"]]
        if any(case.split != Split.SYNTHETIC.value or case.data_version != asset.version for case in cases):
            raise GrantDenied("fixture case scope does not match synthetic asset")
        return cases


@dataclass(frozen=True)
class FormalExecutionPlan:
    """An exact owner-approved plan, not an authorization inferred from a mode."""
    approval_ref: str
    mode: str
    asset_id: str
    asset_version: str
    asset_sha256: str
    case_bindings: tuple[tuple[str, str], ...]
    subject: str
    config_sha256: str
    target_id: str
    target_profile: str
    target_identity: str
    ledger_path: str
    valid_from_utc: str
    expires_at_utc: str
    budget: BudgetLimits
    runtime_bindings: tuple[tuple[str, str], ...]
    execution_implementation_sha256: str
    experiment_id: str
    runtime_input_bindings: tuple[tuple[str,str], ...] = ()
    task_scope_ref: str | None = None
    pre_run_recoveries: tuple[tuple[str,str,str], ...] = ()

    def __post_init__(self):
        import re
        if self.mode not in {"DEV_SMOKE","FROZEN_EVAL","ENGINEERING_EVAL","PERSONAL_REAL_EVAL"} or not isinstance(self.budget,BudgetLimits):
            raise GrantDenied("formal mode and typed budget required")
        if self.mode=='PERSONAL_REAL_EVAL' and not self.task_scope_ref:raise GrantDenied('personal real evaluation needs a persistent task authorization')
        if self.pre_run_recoveries and (self.mode!='PERSONAL_REAL_EVAL' or len({r[0] for r in self.pre_run_recoveries})!=len(self.pre_run_recoveries) or
            any(cid not in dict(self.case_bindings) or dict(self.runtime_bindings).get(path)!=digest for cid,path,digest in self.pre_run_recoveries)):
            raise GrantDenied('pre-Run recovery must bind exact personal case and evidence')
        if not all((self.approval_ref,self.asset_id,self.asset_version,self.subject,self.target_id,self.target_profile,self.target_identity,self.ledger_path,self.experiment_id)):
            raise GrantDenied("formal approval, subject, asset and target are required")
        if (not self.case_bindings or len(dict(self.case_bindings))!=len(self.case_bindings) or
                not self.runtime_bindings or len(dict(self.runtime_bindings))!=len(self.runtime_bindings)):
            raise GrantDenied("unique exact cases and runtime artifacts required")
        digests=[self.asset_sha256,self.config_sha256,self.execution_implementation_sha256,*[v for _,v in self.case_bindings],*[v for _,v in self.runtime_bindings]]
        if any(not re.fullmatch(r"[0-9a-f]{64}",v) for v in digests): raise GrantDenied("invalid formal binding hash")
        if self.runtime_input_bindings and (set(dict(self.runtime_input_bindings))!=set(dict(self.case_bindings)) or
                len(dict(self.runtime_input_bindings))!=len(self.runtime_input_bindings) or
                any(not re.fullmatch(r"[0-9a-f]{64}",v) for _,v in self.runtime_input_bindings)):
            raise GrantDenied("runtime input bindings must cover the exact case scope")
        for value in (self.valid_from_utc,self.expires_at_utc):
            date=datetime.fromisoformat(value.replace("Z","+00:00"))
            if date.utcoffset() is None: raise GrantDenied("formal times require timezone")
        if datetime.fromisoformat(self.valid_from_utc.replace("Z","+00:00"))>=datetime.fromisoformat(self.expires_at_utc.replace("Z","+00:00")):
            raise GrantDenied("invalid formal lifetime")
        if self.budget.max_concurrent_trials!=1: raise GrantDenied("formal runner currently supports serial trials only")


@dataclass(frozen=True)
class FormalExecutionGrant:
    grant_id: str
    issuer: str
    plan: FormalExecutionPlan
    asset_metadata_sha256: str
    signature: str


class FormalGrantAuthority:
    """Separate signed formal grants, with durable approval-level consumption.

    Deployment supplies the trusted issuer/key, approved plan hashes and target
    bindings. No production issuer or eligible asset is fabricated by this code.
    Synthetic grants and metadata-only candidate records are never accepted.
    """
    def __init__(self, registry: DatasetRegistry, *, repository_root: Path, store,
                 issuer: str, trusted_issuers: set[str], signing_key: bytes | None,
                 approved_plan_hashes: set[str], allowed_targets: set[tuple[str,str,str]], clock=None,
                 verification_key: bytes | None = None):
        if issuer not in trusted_issuers or (verification_key is None and (signing_key is None or len(signing_key)<32)):
            raise GrantDenied("untrusted formal issuer or insufficient signing key")
        self.registry,self.root,self.store=registry,repository_root.resolve(strict=True),store
        self.issuer,self.key=issuer,bytes(signing_key) if signing_key is not None else None
        self.public_key = None
        self.private_key = None
        if verification_key is not None:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
            from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
            self.public_key = Ed25519PublicKey.from_public_bytes(verification_key)
            if signing_key is not None:
                self.private_key = Ed25519PrivateKey.from_private_bytes(signing_key)
                if self.private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw) != verification_key:
                    raise GrantDenied("formal signing key does not match configured verification key")
        self.approved_plans=frozenset(approved_plan_hashes);self.targets=frozenset(allowed_targets)
        self.clock=clock or (lambda:datetime.now(timezone.utc))
        with store.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS formal_grants(grant_id TEXT PRIMARY KEY, approval_ref TEXT UNIQUE NOT NULL, payload_json TEXT NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS formal_grant_events(event_id TEXT PRIMARY KEY,grant_id TEXT NOT NULL REFERENCES formal_grants(grant_id),case_id TEXT,attempt_id TEXT,event_type TEXT NOT NULL,payload_json TEXT NOT NULL,created_at_utc TEXT NOT NULL)")
            db.execute('CREATE TABLE IF NOT EXISTS personal_task_attempts(task_ref TEXT NOT NULL,case_id TEXT NOT NULL,grant_id TEXT NOT NULL,attempt_id TEXT NOT NULL,experiment_id TEXT NOT NULL,claimed_at_utc TEXT NOT NULL,PRIMARY KEY(task_ref,case_id))')
            db.execute('CREATE TABLE IF NOT EXISTS personal_task_recovery_claims(task_ref TEXT NOT NULL,case_id TEXT NOT NULL,previous_attempt_id TEXT UNIQUE NOT NULL,new_attempt_id TEXT UNIQUE NOT NULL,grant_id TEXT NOT NULL,proof_sha256 TEXT NOT NULL)')
            for table in ("formal_grants","formal_grant_events","personal_task_attempts","personal_task_recovery_claims"):
                for action in ("UPDATE","DELETE"):
                    db.execute(f"CREATE TRIGGER IF NOT EXISTS deny_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'append-only formal grant'); END")

    def _fields(self,grant):
        from .contracts import canonical_json
        plan=asdict(grant.plan)
        if plan['task_scope_ref'] is None:plan.pop('task_scope_ref')
        if not plan['pre_run_recoveries']:plan.pop('pre_run_recoveries')
        return canonical_json({"kind":"ENGINEERING_EXECUTION_V1" if grant.plan.mode=='ENGINEERING_EVAL' else 'PERSONAL_REAL_EXECUTION_V1' if grant.plan.mode=='PERSONAL_REAL_EVAL' else "FORMAL_EXECUTION_V1","grant_id":grant.grant_id,
            "issuer":grant.issuer,"plan":plan,"asset_metadata_sha256":grant.asset_metadata_sha256})

    def _sign(self, fields: str) -> str:
        if self.public_key is not None:
            if self.private_key is None: raise GrantDenied("verification-only authority cannot issue grants")
            return "ed25519:" + self.private_key.sign(fields.encode()).hex()
        return hmac.new(self.key, fields.encode(), hashlib.sha256).hexdigest()

    def _signature_valid(self, grant) -> bool:
        if self.public_key is not None:
            from cryptography.exceptions import InvalidSignature
            if not grant.signature.startswith("ed25519:"): return False
            try:
                self.public_key.verify(bytes.fromhex(grant.signature.split(":", 1)[1]), self._fields(grant).encode())
                return True
            except (InvalidSignature, ValueError): return False
        return hmac.compare_digest(hmac.new(self.key,self._fields(grant).encode(),hashlib.sha256).hexdigest(),grant.signature)

    def _eligible(self,plan):
        from .contracts import Scoreability, sha256_json
        asset=self.registry.get(plan.asset_id)
        if plan.mode=='PERSONAL_REAL_EVAL':
            if (asset.asset_type!='user_reviewed_synthetic_benchmark' or asset.review_status!='BATCH_USER_CONFIRMED' or
                asset.scoreability!='NOT_SCOREABLE' or asset.independence_status!='NOT_VERIFIED' or
                asset.review_method!='USER_REVIEWED_PERSONAL_SYNTHETIC_NO_BLIND_V1' or
                asset.access_policy!='EXACT_SIGNED_PERSONAL_REAL_EVAL_ONLY' or asset.version!=plan.asset_version or asset.sha256!=plan.asset_sha256):
                raise GrantDenied('personal real permit requires explicitly user-reviewed non-Gold benchmark metadata')
            decision_path=(self.root/asset.historical_source_ref).resolve(strict=True)
            if not decision_path.is_relative_to(self.root) or asset.historical_source_ref not in dict(plan.runtime_bindings):raise GrantDenied('personal decision must be bound')
            decision=json.loads(decision_path.read_text(encoding='utf8'))
            if decision['event_id']!=plan.task_scope_ref or not any(r['path']==asset.source_path and r['sha256']==asset.sha256 for r in decision['bound_inputs']):
                raise GrantDenied('personal decision does not authorize this source')
            return asset,sha256_json(asset.metadata())
        if plan.mode=='ENGINEERING_EVAL':
            if (asset.split!='synthetic' or asset.asset_type!='isolated_engineering_fixture' or asset.scoreability!='NOT_SCOREABLE' or
                    asset.access_policy!='ENGINEERING_SIGNED_PERMIT_ONLY' or asset.version!=plan.asset_version or asset.sha256!=plan.asset_sha256):
                raise GrantDenied('engineering permit requires explicitly non-Gold synthetic fixture metadata')
            return asset,sha256_json(asset.metadata())
        expected_split="frozen_test" if plan.mode=="FROZEN_EVAL" else "dev"
        if (asset.split!=expected_split or asset.scoreability!=Scoreability.ELIGIBLE.value or
                asset.version!=plan.asset_version or asset.sha256!=plan.asset_sha256):
            raise GrantDenied("formal asset split/version/hash/review eligibility mismatch")
        reviewed = datetime.fromisoformat(asset.reviewed_at_utc.replace("Z", "+00:00"))
        locked = datetime.fromisoformat(asset.locked_at_utc.replace("Z", "+00:00"))
        if not reviewed <= locked <= self.clock():
            raise GrantDenied("formal review/lock timestamps are inconsistent or in the future")
        return asset,sha256_json(asset.metadata())

    def issue(self,plan: FormalExecutionPlan) -> FormalExecutionGrant:
        from .contracts import sha256_json, canonical_json
        import uuid
        if sha256_json(asdict(plan)) not in self.approved_plans:
            raise GrantDenied("exact formal plan is not approved for this issuer")
        _,metadata=self._eligible(plan)
        grant=FormalExecutionGrant("formal."+uuid.uuid4().hex,self.issuer,plan,metadata,"")
        from dataclasses import replace
        grant=replace(grant,signature=self._sign(self._fields(grant)))
        self._verify_bindings(grant,subject=plan.subject,mode=plan.mode,config_sha256=plan.config_sha256,
            target=(plan.target_id,plan.target_profile,plan.target_identity))
        try:
            with self.store.transaction() as db:
                db.execute("INSERT INTO formal_grants VALUES(?,?,?)",(grant.grant_id,plan.approval_ref,canonical_json(asdict(grant))))
        except sqlite3.IntegrityError as exc:
            raise GrantDenied("formal approval already issued; new directory or restart cannot reset it") from exc
        return grant

    def _verify_bindings(self,grant,*,subject,mode,config_sha256,target):
        from .contracts import sha256_json
        if not isinstance(grant,FormalExecutionGrant): raise GrantDenied("signed formal grant required")
        if grant.issuer!=self.issuer or not self._signature_valid(grant): raise GrantDenied("formal signature invalid")
        p=grant.plan
        if p.execution_implementation_sha256!=formal_runtime_digest():
            raise GrantDenied("formal execution implementation changed")
        ledger=(self.root/p.ledger_path).resolve()
        if not ledger.is_relative_to(self.root) or self.store.path.resolve()!=ledger:
            raise GrantDenied("formal durable ledger path differs from approved plan")
        if sha256_json(asdict(p)) not in self.approved_plans: raise GrantDenied("formal plan approval removed")
        if (subject!=p.subject or mode!=p.mode or config_sha256!=p.config_sha256 or
                target!=(p.target_id,p.target_profile,p.target_identity) or target not in self.targets):
            raise GrantDenied("formal mode/subject/config/target mismatch")
        now=self.clock()
        if not datetime.fromisoformat(p.valid_from_utc.replace("Z","+00:00"))<=now<datetime.fromisoformat(p.expires_at_utc.replace("Z","+00:00")):
            raise GrantDenied("formal grant expired or not yet valid")
        _,metadata=self._eligible(p)
        if metadata!=grant.asset_metadata_sha256: raise GrantDenied("formal asset metadata changed")
        for relative,expected in p.runtime_bindings:
            path=(self.root/relative).resolve(strict=True)
            if not path.is_relative_to(self.root) or hashlib.sha256(path.read_bytes()).hexdigest()!=expected:
                raise GrantDenied("formal runtime binding changed")

    def verify(self,grant,*,subject,mode,config_sha256,target,case=None,attempt_id=None):
        self._verify_bindings(grant,subject=subject,mode=mode,config_sha256=config_sha256,target=target)
        from .contracts import sha256_json,canonical_json
        if case is not None and (case.data_version!=grant.plan.asset_version or
                case.split!=(self.registry.get(grant.plan.asset_id).split if mode=='PERSONAL_REAL_EVAL' else 'synthetic' if mode=='ENGINEERING_EVAL' else "frozen_test" if mode=="FROZEN_EVAL" else "dev") or
                dict(grant.plan.case_bindings).get(case.case_id)!=formal_case_digest(case)):
            raise GrantDenied("case is outside formal exact scope")
        with self.store.transaction(immediate=False) as db:
            row=db.execute("SELECT payload_json FROM formal_grants WHERE grant_id=?",(grant.grant_id,)).fetchone()
            if row is None or row[0]!=canonical_json(asdict(grant)): raise GrantDenied("formal grant not durably issued")
            events=db.execute("SELECT * FROM formal_grant_events WHERE grant_id=? ORDER BY rowid",(grant.grant_id,)).fetchall()
        if any(r['event_type'] in {"REVOKED","CLOSED","STOPPED"} for r in events): raise GrantDenied("formal grant revoked/closed/stopped")
        if attempt_id is not None:
            matching=[r for r in events if r['attempt_id']==attempt_id]
            if (not matching or matching[0]['event_type']!="CLAIMED" or any(r['event_type']=="SETTLED" for r in matching)
                    or case is None or matching[0]['case_id']!=case.case_id):
                raise GrantDenied("formal active attempt missing or already settled")

    def verify_archive_access(self, grant, *, subject):
        """Read/recompute closed evidence without granting any new execution."""
        from .contracts import canonical_json
        if subject != grant.plan.subject or grant.issuer != self.issuer or not self._signature_valid(grant):
            raise GrantDenied("archive signature/principal mismatch")
        if self.store.path.resolve() != (self.root / grant.plan.ledger_path).resolve():
            raise GrantDenied("archive ledger binding mismatch")
        with self.store.transaction(immediate=False) as db:
            row = db.execute("SELECT payload_json FROM formal_grants WHERE grant_id=?", (grant.grant_id,)).fetchone()
        if row is None or row[0] != canonical_json(asdict(grant)):
            raise GrantDenied("archive grant is not the persisted signed record")

    def _event(self,db,grant,kind,case_id=None,attempt_id=None,payload=None):
        from .contracts import canonical_json
        import uuid
        db.execute("INSERT INTO formal_grant_events VALUES(?,?,?,?,?,?,?)",(uuid.uuid4().hex,grant.grant_id,
            case_id,attempt_id,kind,canonical_json(payload or {}),self.clock().isoformat()))

    def claim(self,grant,*,case,attempt_id,**binding):
        self.verify(grant,case=case,**binding)
        self._claim(grant,case.case_id,attempt_id)

    def claim_runtime(self,grant,*,runtime_payload,attempt_id,**binding):
        """Application-container boundary: verify signed input, without Gold."""
        from .contracts import sha256_json
        self.verify(grant,**binding)
        cid=runtime_payload['case_id']
        if dict(grant.plan.runtime_input_bindings).get(cid)!=sha256_json(runtime_payload):
            raise GrantDenied("dispatched runtime input/limit differs from signed scope")
        self._claim(grant,cid,attempt_id)

    def _claim(self,grant,case_id,attempt_id):
        with self.store.transaction() as db:
            if grant.plan.mode=='PERSONAL_REAL_EVAL':
                try:db.execute('INSERT INTO personal_task_attempts VALUES(?,?,?,?,?,?)',(grant.plan.task_scope_ref,case_id,grant.grant_id,attempt_id,grant.plan.experiment_id,self.clock().isoformat()))
                except sqlite3.IntegrityError as exc:
                    bound=next((r for r in grant.plan.pre_run_recoveries if r[0]==case_id),None)
                    if bound is None:raise GrantDenied('case already consumed in this personal task;new segment cannot reset it') from exc
                    proof_path=(self.root/bound[1]).resolve(strict=True)
                    if not proof_path.is_relative_to(self.root) or hashlib.sha256(proof_path.read_bytes()).hexdigest()!=bound[2]:raise GrantDenied('pre-Run recovery proof drift')
                    proof=json.loads(proof_path.read_text(encoding='utf8'))
                    previous=proof['previous_attempt_id']
                    root_attempt=db.execute('SELECT attempt_id FROM personal_task_attempts WHERE task_ref=? AND case_id=?',(grant.plan.task_scope_ref,case_id)).fetchone()[0]
                    recovery_parent=db.execute('SELECT 1 FROM personal_task_recovery_claims WHERE task_ref=? AND case_id=? AND new_attempt_id=?',(grant.plan.task_scope_ref,case_id,previous)).fetchone()
                    if previous!=root_attempt and recovery_parent is None:raise GrantDenied('pre-Run recovery refers to another task/case')
                    local=proof['classification']=='VERIFIED_PRE_RPC_LOCAL_SETUP_FAILURE' and proof['remote_attempt_present'] is False and proof['case_file_present'] is False
                    remote=proof['classification']=='VERIFIED_REMOTE_PREPARATION_FAILED_BEFORE_RUN' and proof['remote_attempt_present'] is True and proof['case_file_present'] is True and proof['remote_stage'] in {'CLAIMED','PREPARED'} and proof['remote_result_present'] is False
                    prepared=remote and proof['remote_stage']=='PREPARED'
                    if prepared and (proof.get('cleanup_evidence',{}).get('residual_case_rows')!=0 or
                            proof.get('cleanup_evidence',{}).get('pending_actions')!=0 or
                            proof.get('cleanup_evidence',{}).get('lease_released') is not True):
                        raise GrantDenied('prepared zero-Run recovery requires exact cleanup evidence')
                    if (proof['task_ref']!=grant.plan.task_scope_ref or proof['case_id']!=case_id or proof['target_identity']!=grant.plan.target_identity or
                        not(local or remote) or proof['actual_runs']!=0 or proof['provider_calls']!=0 or proof['tool_calls']!=0 or proof['lease_free'] is not True):
                        raise GrantDenied('pre-Run recovery lacks verified zero-execution evidence')
                    if db.execute('SELECT 1 FROM trials WHERE trial_id=?',(previous,)).fetchone():raise GrantDenied('existing application result cannot be replayed')
                    previous_event=db.execute("SELECT payload_json FROM trial_events WHERE trial_id=? AND event_type='EXECUTION_UNKNOWN'",(previous,)).fetchone()
                    if previous_event is None:
                        exists=db.execute("SELECT 1 FROM sqlite_master WHERE name='container_attempts'").fetchone()
                        row=db.execute('SELECT stage,receipt_json,result_json FROM container_attempts WHERE trial_id=?',(previous,)).fetchone() if exists else None
                        if (not remote or row is None or row['stage']!=proof['remote_stage'] or row['result_json'] is not None or
                                (prepared and row['receipt_json'] is None) or (not prepared and row['receipt_json'] is not None)):
                            raise GrantDenied('previous remote preparation failure not proven')
                    elif prepared:
                        inspection=json.loads(previous_event[0])
                        if inspection.get('receipt_saved') is not True or inspection.get('run_created') is not False:
                            raise GrantDenied('prepared inspection does not prove zero Runs')
                    elif json.loads(previous_event[0])['receipt_saved'] is not False:raise GrantDenied('previous pre-Run failure not proven')
                    prior_grant=db.execute("SELECT grant_id FROM formal_grant_events WHERE attempt_id=? AND event_type='CLAIMED'",(previous,)).fetchone()
                    if prior_grant is None or not db.execute("SELECT 1 FROM formal_grant_events WHERE grant_id=? AND event_type='CLOSED'",(prior_grant[0],)).fetchone():raise GrantDenied('previous pre-Run segment not closed')
                    if db.execute("SELECT 1 FROM budget_events WHERE json_extract(payload_json,'$.trial_id')=?",(previous,)).fetchone():raise GrantDenied('previous budget activity requires reconciliation,not zero-Run recovery')
                    try:db.execute('INSERT INTO personal_task_recovery_claims VALUES(?,?,?,?,?,?)',(grant.plan.task_scope_ref,case_id,previous,attempt_id,grant.grant_id,bound[2]))
                    except sqlite3.IntegrityError as err:raise GrantDenied('pre-Run recovery opportunity already consumed') from err
            events=db.execute("SELECT * FROM formal_grant_events WHERE grant_id=?",(grant.grant_id,)).fetchall()
            if any(r['event_type'] in {"REVOKED","CLOSED","STOPPED"} for r in events): raise GrantDenied("formal state changed before claim")
            claims=[r for r in events if r['event_type']=="CLAIMED"]
            settled={r['attempt_id'] for r in events if r['event_type']=="SETTLED"}
            if not attempt_id or any(r['attempt_id']==attempt_id for r in claims): raise GrantDenied("formal attempt identifier consumed or empty")
            if any(r['case_id']==case_id for r in claims): raise GrantDenied("case attempt already consumed")
            if any(r['attempt_id'] not in settled for r in claims): raise GrantDenied("unsettled formal attempt; reconcile without replay")
            if len(claims)>=len(grant.plan.case_bindings): raise GrantDenied("formal attempt cap exhausted")
            self._event(db,grant,"CLAIMED",case_id,attempt_id)

    def settle(self,grant,*,case_id,attempt_id,continue_allowed,cleanup_status):
        with self.store.transaction() as db:
            rows=db.execute("SELECT event_type,case_id FROM formal_grant_events WHERE grant_id=? AND attempt_id=?",(grant.grant_id,attempt_id)).fetchall()
            if len(rows)!=1 or rows[0]['event_type']!="CLAIMED" or rows[0]['case_id']!=case_id:
                raise GrantDenied("formal settlement is missing or duplicate")
            allowed=continue_allowed and cleanup_status=="CLEANED"
            self._event(db,grant,"SETTLED",case_id,attempt_id,{"continue_allowed":allowed,"cleanup_status":cleanup_status})
            if not allowed: self._event(db,grant,"STOPPED",payload={"reason":"critical execution/usage/audit/cleanup condition"})

    def revoke(self,grant):
        with self.store.transaction() as db: self._event(db,grant,"REVOKED")

    def close(self,grant):
        with self.store.transaction() as db: self._event(db,grant,"CLOSED")

    def read_cases(self,grant,*,subject,config_sha256,target):
        from .registry import AccessGuard,AccessGrant
        self.verify(grant,subject=subject,mode=grant.plan.mode,config_sha256=config_sha256,target=target)
        p=grant.plan
        if p.mode in {'ENGINEERING_EVAL','PERSONAL_REAL_EVAL'}:
            from .contracts import EvalCase
            asset=self.registry.get(p.asset_id)
            source=(self.root/asset.source_path).resolve(strict=True)
            if not source.is_relative_to(self.root) or hashlib.sha256(source.read_bytes()).hexdigest()!=p.asset_sha256:raise GrantDenied('engineering fixture changed')
            cases=[EvalCase.from_dict(c) for c in json.loads(source.read_text(encoding='utf8'))['cases']]
            if p.mode=='PERSONAL_REAL_EVAL':
                by_id={c.case_id:c for c in cases}
                if not set(dict(p.case_bindings))<=set(by_id):raise GrantDenied('personal subset outside approved source')
                cases=[by_id[cid] for cid,_ in p.case_bindings]
            if [c.case_id for c in cases]!=[c for c,_ in p.case_bindings]:raise GrantDenied('engineering exact scope changed')
            for case in cases:self.verify(grant,subject=subject,mode=p.mode,config_sha256=config_sha256,target=target,case=case)
            return cases
        access=AccessGrant(grant.grant_id,grant.issuer,p.valid_from_utc,(p.asset_id,),
            (("frozen_test",) if p.mode=="FROZEN_EVAL" else ("dev",)),p.approval_ref,p.expires_at_utc,p.mode=="FROZEN_EVAL")
        guard=AccessGuard(self.registry,self.root,self.store.path.parent/"formal-asset-access.jsonl")
        cases=guard.read_cases(p.asset_id,principal=subject,grant=access)
        selected=[c for c in cases if c.case_id in dict(p.case_bindings)]
        if len(selected)!=len(p.case_bindings): raise GrantDenied("formal case inventory missing")
        by_id={c.case_id:c for c in selected}
        for case in selected: self.verify(grant,subject=subject,mode=p.mode,config_sha256=config_sha256,target=target,case=case)
        return [by_id[cid] for cid,_ in p.case_bindings]


def formal_case_payload(case: EvalCase) -> dict:
    """Exact execution values; unlike logging projection, preserves input attack strings."""
    from collections.abc import Mapping
    from dataclasses import fields
    def plain(value):
        if isinstance(value,Mapping): return {k:plain(v) for k,v in value.items()}
        if isinstance(value,(list,tuple)): return [plain(v) for v in value]
        return value
    payload={f.name:plain(getattr(case,f.name)) for f in fields(case)}
    payload["schema_version"]=case.schema_version
    return payload


def formal_case_digest(case: EvalCase) -> str:
    return hashlib.sha256(json.dumps(formal_case_payload(case),ensure_ascii=False,sort_keys=True,separators=(",",":")).encode()).hexdigest()


def formal_runtime_digest() -> str:
    """Versioned binding for the actual formal gate/runner, not caller labels."""
    from .contracts import sha256_json
    from .personal_dev import implementation_digest
    code_root=Path(__file__).resolve().parents[2]
    paths=("app/m3/single_agent.py","eval/m6_1/grants.py","eval/m6_1/registry.py","eval/m6_1/execution_modes.py",
        "eval/m6_1/runner.py","eval/m6_1/shoprelay_adapter.py","eval/m6_1/store.py",
        "eval/m6_1/budget.py","eval/m6_1/contracts.py","eval/m6_1/experiment.py",
        "scripts/run_m6_1_b_synthetic.py", "eval/m6_1/formal_application.py",
        "eval/m6_1/formal_assets.py", "eval/m6_1/formal_scoring.py", "eval/m6_1/formal_reference_rules.py", "eval/m6_1/formal_actions.py", "app/m4/actions.py", "app/m1/db.py", "eval/m6_1/aggregate.py", "eval/m6_1/grader.py",
        "eval/m6_1/container_boundary.py", "eval/m6_1/governance.py",
        "eval/m6_1/container_startup.py", "eval/m6_1/container_canary_sdk.py", "eval/m6_1/container_proxy.py",
        "requirements-formal-eval.lock", "requirements-langgraph-g1.lock", "requirements-langgraph-g2.lock", "pyproject.toml")
    return sha256_json({"version":"formal-execution-runtime-v3",
        "application_implementation_digest":implementation_digest(code_root),
        "policy_semantic_runtime_sha256":hashlib.sha256((code_root/"app/m2/self_rag.py").read_bytes()).hexdigest(),
        "files":{p:hashlib.sha256((code_root/p).read_bytes()).hexdigest() for p in paths}})
