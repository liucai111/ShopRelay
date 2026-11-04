"""Versioned owner governance at the deployment boundary, not dataset approval."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path

CONTAINER_GATES = {
    "CONTAINER_NETWORK_AND_MINIMAL_MOUNTS_VERIFIED", "GOLD_AND_MODEL_INPUT_SEPARATED",
    "SIGNING_KEY_SEPARATED", "TARGET_IDENTITY_VERIFIED", "RECONCILABLE_EXECUTION_AUDIT",
    "INDEPENDENT_REVIEW_OF_REPLACEMENT_DATASET", "SEPARATE_EXPLICIT_FROZEN_TEST_ACCESS_AUTHORIZATION",
}


def load_governance(root: Path, binding: dict) -> dict:
    from .formal_assets import confined, raw_hash
    path=confined(root,binding["path"])
    if raw_hash(path)!=binding["sha256"]:raise PermissionError("governance binding changed")
    record=json.loads(path.read_text(encoding="utf8"))
    if record['overlay_id']=='m6.2-personal-real-eval-governance-r6':
        parent=load_governance(root,{'path':record['parent_path'],'sha256':record['parent_sha256']})
        decision=confined(root,record['authorization']['path'])
        if raw_hash(decision)!=record['authorization']['sha256']:raise PermissionError('personal authorization changed')
        source=json.loads(decision.read_text(encoding='utf8'))
        if (source['event_id']!=record['authorization']['event_id'] or record['mode']!='PERSONAL_REAL_EVAL' or
            record['method']!='USER_REVIEWED_PERSONAL_SYNTHETIC_NO_BLIND_V1' or record['independent_unseen_claim'] is not False or
            record['blind_assessment_status']!='CANCELLED_BY_USER'):
            raise PermissionError('personal method/scope differs from owner decision')
        return record
    if record["overlay_id"]!="m6.2-container-governance-r4" or set(record["pre_blind_test_gates"])!=CONTAINER_GATES:
        raise PermissionError("unsupported or incomplete successor governance")
    if raw_hash(confined(root,record["parent_path"]))!=record["parent_sha256"]:
        raise PermissionError("governance parent history changed")
    if record["replaced_gates"]!=["OS_LEVEL_PROTECTED_STORAGE_AND_ACL_VERIFIED"] or record["formal_execution_authorized"] is not False:
        raise PermissionError("successor exceeds the approved isolation-method change")
    return record


def verify_container_process(expected_uid: int) -> dict:
    """Actual kernel identity/hardening; an environment role label is insufficient."""
    import os
    if os.name!="posix" or not Path('/.dockerenv').is_file() or os.geteuid()!=expected_uid:
        raise PermissionError("actual container process identity mismatch")
    status=dict(line.split(':',1) for line in Path('/proc/self/status').read_text().splitlines() if ':' in line)
    if int(status['CapEff'].strip(),16)!=0 or status['NoNewPrivs'].strip()!='1':
        raise PermissionError("container capabilities/no-new-privileges differ from deployment")
    forbidden=['/var/run/docker.sock','/run/issuer-key/private.key','/control-assets/candidates/frozen100.json',
        '/opt/shoprelay/data/eval/new-assets-r26/candidates/frozen100.json',
        '/opt/shoprelay/eval/m6_2/candidate_construction_r26.py']
    if any(Path(p).exists() for p in forbidden):raise PermissionError("application has a forbidden mount")
    return {"uid":os.geteuid(),"cap_effective_zero":True,"no_new_privileges":True,"forbidden_mounts_absent":True}


def verify_isolation_evidence(root: Path,binding: dict,runtime_sha256: str) -> dict:
    """Trusted administrator's bound actual observations, not an OS-role claim."""
    from .formal_assets import confined,raw_hash
    path=confined(root,binding['path'])
    if raw_hash(path)!=binding['sha256']:raise PermissionError('container isolation receipt changed')
    evidence=json.loads(path.read_text(encoding='utf8'))
    app=evidence['services']['application']
    if (evidence['actual_application_runtime_sha256']!=runtime_sha256 or not app['readonly_rootfs'] or
            app['cap_drop']!=['ALL'] or any(v=='READ_SUCCEEDED_UNEXPECTED' for v in evidence['application_access']['access'].values()) or
            not evidence['networks']['control']['internal'] or not evidence['networks']['database']['internal'] or
            evidence['control_access']['private_key_mounted']):
        raise PermissionError('actual isolation evidence incomplete or incompatible')
    return evidence
