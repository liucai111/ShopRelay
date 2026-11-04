"""Explicit asset intake and independent reference review, before formal grants.

No registry source is discovered or opened implicitly. Intake receives one exact
owner-approved path; real review records use the authenticated OS principal.
The same principal may perform next-day blind review under evaluation §3.4.
"""
from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, AwareDatetime

from .contracts import EvalCase, sha256_json
from .fixture_read_models import READ_TOOLS
from .grants import formal_case_payload, formal_case_digest
from .registry import DatasetAsset, DatasetRegistry

Nonempty = Annotated[str, Field(min_length=1)]
AI_REVIEW_METHOD = "USER_REVIEWED_AI_THEN_CONTEXT_ISOLATED_AI_BLIND_V1"
LEGACY_REVIEW_METHOD = "INDEPENDENT_LABEL_THEN_NEXT_DAY_BLIND_REVIEW"


class IntakePermit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    authorization_ref: Nonempty
    approved_by: Nonempty
    source_path: Nonempty
    source_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    valid_from_utc: AwareDatetime
    expires_at_utc: AwareDatetime


class AssetProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid")
    replacement_of: list[Nonempty]
    copied_or_relabelled_dev: Literal[False]
    exposure_status: Literal["UNEXPOSED_VERIFIED", "NOT_VERIFIED"]
    exposure_evidence_refs: Annotated[list[Nonempty], Field(min_length=1)]
    independence_evidence_refs: Annotated[list[Nonempty], Field(min_length=1)]


class WorkflowConfiguration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stewards: Annotated[list[Nonempty], Field(min_length=1)]
    reviewers: Annotated[list[Nonempty], Field(min_length=1)]
    adjudicators: Annotated[list[Nonempty], Field(min_length=1)]
    asset_id: Nonempty
    expected_case_count: Annotated[int, Field(strict=True, gt=0)]
    required_replacements: list[Nonempty]
    provenance: AssetProvenance
    content_intake_permit: IntakePermit
    intake_record: Nonempty
    day1_record: Nonempty
    blind_record: Nonempty
    frozen_asset: Nonempty
    lock_record: Nonempty
    registry_output: Nonempty
    intake_stage: Literal["FORMAL_VERIFIED", "CANDIDATE_DRAFT"] = "FORMAL_VERIFIED"
    review_method: Literal["INDEPENDENT_LABEL_THEN_NEXT_DAY_BLIND_REVIEW", "USER_REVIEWED_AI_THEN_CONTEXT_ISOLATED_AI_BLIND_V1"] = LEGACY_REVIEW_METHOD
    method_amendment: Nonempty | None = None
    method_amendment_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None


def authenticated_subject() -> str:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        size = wintypes.ULONG(256)
        buffer = ctypes.create_unicode_buffer(size.value)
        # NameSamCompatible resolves the authenticated token, not USERNAME env.
        if not ctypes.windll.secur32.GetUserNameExW(2, buffer, ctypes.byref(size)):
            raise PermissionError("cannot resolve authenticated execution principal")
        return buffer.value
    import pwd
    return pwd.getpwuid(os.geteuid()).pw_name


def utc_now():
    return datetime.now(timezone.utc)


def write_new(path: Path, value: dict) -> None:
    import uuid
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".staging-" + uuid.uuid4().hex)
    try:
        with temporary.open("x", encoding="utf8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        # Exclusive publication of a fully written file; never overwrite history.
        os.link(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def raw_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def confined(root: Path, relative: str) -> Path:
    path = root / relative
    if Path(relative).is_absolute() or not path.resolve().is_relative_to(root.resolve()) or path.resolve() != path.absolute():
        raise ValueError("workflow path must be an unaliased path inside the approved root")
    return path


def validate_asset(payload: dict) -> list[EvalCase]:
    if set(payload) != {"schema_version", "version", "split", "cases"} or payload["schema_version"] not in {"formal-readonly-assets-v1","formal-owner-action-assets-v2"}:
        raise ValueError("formal asset envelope/schema mismatch")
    if payload["split"] not in {"dev", "frozen_test"} or not payload["cases"]:
        raise ValueError("nonempty formal split required")
    cases = [EvalCase.from_dict(value) for value in payload["cases"]]
    if len({c.case_id for c in cases}) != len(cases):
        raise ValueError("duplicate case ID")
    for case in cases:
        if case.split != payload["split"] or case.data_version != payload["version"]:
            raise ValueError("case version/split drift")
        value = formal_case_payload(case)["input"]
        fields={"user_query", "user_id", "owner_map", "allowed_capabilities", "fixture_refs", "fixture", "evaluation_at"}
        if payload["schema_version"]=="formal-owner-action-assets-v2" and "owner_interaction" in value: fields.add("owner_interaction")
        if set(value) != fields:
            raise ValueError("runtime input must separate fixtures/capabilities from Gold labels")
        if "owner_interaction" in value:
            from .formal_actions import validate_interaction
            validate_interaction(value["owner_interaction"])
        caps = value["allowed_capabilities"]
        if not isinstance(caps, list) or len(set(caps)) != len(caps) or not set(caps) <= READ_TOOLS:
            raise ValueError("only unique supported read capabilities are allowed")
        if value["user_id"] not in value["owner_map"] or len(set(value["owner_map"].values())) != len(value["owner_map"]):
            raise ValueError("explicit unique owner mapping required")
        if not value["user_query"].strip() or datetime.fromisoformat(value["evaluation_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("question and timezone-aware evaluation time required")
        fixture = value["fixture"]
        if not {"users", "orders_and_snapshots", "shipments", "policies", "knowledge_version"} <= set(fixture):
            raise ValueError("fixture source collections missing")
        from datetime import date
        for policy in fixture["policies"]:
            start, end = policy["effective"].split("/")
            if date.fromisoformat(start) > date.fromisoformat(end):
                raise ValueError("policy date-only UTC interval invalid")
        # Validate using the actual source-preserving planner. Do not make an
        # unavailable runtime identifier available through expected labels.
        from .formal_application import application_plan
        application_plan(case)
        if set(case.expected_outcome) != {"route", "required_evidence", "atomic_facts", "task"}:
            raise ValueError("four separate reference dimensions required")
        validate_labels(formal_case_payload(case)["expected_outcome"], case)
    return cases


def validate_labels(labels: dict, case: EvalCase) -> None:
    if set(labels) != {"route", "required_evidence", "atomic_facts", "task"}:
        raise ValueError("review dimensions missing or unexpected")
    route = labels["route"]
    fields = {"strategy", "sub_strategy", "intent_labels", "safety_decision", "reason_code", "needs_clarification", "required_capabilities"}
    if set(route) != fields or route["strategy"] not in {"DIRECT", "REFUSE", "SINGLE", "MULTI", "AGENT"} or not isinstance(route["needs_clarification"], bool):
        raise ValueError("route reference fields invalid")
    if not set(route["required_capabilities"]) <= set(case.input["allowed_capabilities"]):
        raise ValueError("route reference outside capability scope")
    from app.m1.routing import RouteDecision,_validate_classifier_decision
    typed=RouteDecision.model_validate({**route,'decision_source':'RULE',
        'clarification_question':'Required missing context (reference schema validation only)' if route['needs_clarification'] else None})
    _validate_classifier_decision(typed)
    fixture = formal_case_payload(case)["input"]["fixture"]
    source_ids = {row.get("id", row.get("evidence_id")) for rows in fixture.values() if isinstance(rows, list) for row in rows if isinstance(row, dict)}
    for name in ("required_evidence", "atomic_facts"):
        if not isinstance(labels[name], list):
            raise ValueError("evidence/fact labels must be arrays, including explicit empty arrays")
        for item in labels[name]:
            required = {"condition", "source_ids", "requirement"} if name == "required_evidence" else {"fact_id", "subject", "predicate", "value", "condition", "source_ids"}
            if set(item) != required or not item["source_ids"] or not set(item["source_ids"]) <= source_ids:
                raise ValueError("reference source/fields outside reviewed fixture")
    task = labels["task"]
    fields={"requirements", "required_tool_groups", "zero_calls", "per_case_budget"}
    from .formal_reference_rules import V2,V3,ROUTE_KEYS,direct_source_facts
    optional={"route_equivalents","required_resources"} if {V2,V3}.intersection(case.rubric_refs) else set()
    if V3 in case.rubric_refs:optional.add('required_result_rows')
    if not fields<=set(task) or not set(task)<=fields|optional:
        raise ValueError("task requirements and execution caps must be explicit")
    for alternative in task.get('route_equivalents',[]):
        if set(alternative)!=set(ROUTE_KEYS):raise ValueError('routing equivalent fields invalid')
        typed=RouteDecision.model_validate({**route,**alternative,'decision_source':'RULE',
            'clarification_question':'Reference validation only' if alternative['needs_clarification'] else None})
        _validate_classifier_decision(typed)
        if not set(typed.required_capabilities)<=set(case.input['allowed_capabilities']):raise ValueError('route equivalent outside scope')
    for resource in task.get('required_resources',[]):
        if (set(resource)!={'tool','argument','values'} or resource['tool'] not in case.input['allowed_capabilities'] or
                not resource['argument'] or not resource['values'] or len(set(resource['values']))!=len(resource['values'])):
            raise ValueError('resource coverage reference invalid')
    for constraint in task.get('required_result_rows',[]):
        if (set(constraint)!={'tool','result_path','source_field','rows'} or constraint['tool'] not in case.input['allowed_capabilities'] or
                not constraint['source_field'] or not constraint['result_path'] or not all(isinstance(p,str) and p for p in constraint['result_path']) or
                not constraint['rows']):raise ValueError('result row reference invalid')
        ids=[r['source_id'] for r in constraint['rows']]
        if len(set(ids))!=len(ids) or not set(ids)<=source_ids:raise ValueError('result rows duplicate or outside source')
        if any(set(r)!={'source_id','fields'} or not isinstance(r['fields'],dict) or not r['fields'] for r in constraint['rows']):
            raise ValueError('result row fields invalid')
    if V3 in case.rubric_refs:
        fact_ids=[f['fact_id'] for f in labels['atomic_facts']]
        if len(set(fact_ids))!=len(fact_ids):raise ValueError('duplicate atomic fact ID')
        if direct_source_facts(case,labels)['mismatches']:raise ValueError('direct atomic fact contradicts bound source field')
    if not isinstance(task["zero_calls"], bool) or not task["requirements"]:
        raise ValueError("task requirements/zero-call semantics missing")
    for group in task["required_tool_groups"]:
        if not group or any(not option or not set(option) <= set(case.input["allowed_capabilities"]) for option in group):
            raise ValueError("required tool alternatives outside allowed capabilities")
    from .budget import BudgetLimits
    limits = BudgetLimits(**task["per_case_budget"])
    if limits.embedding_calls or limits.judge_calls or limits.max_retries or limits.max_concurrent_trials != 1:
        raise ValueError("formal readonly application profile excludes Judge/embedding/retries/concurrency")
    if task["zero_calls"] and task["required_tool_groups"]:
        raise ValueError("zero-call task conflicts with required tools;permission alone does not require a call")


class AssetWorkflow:
    def __init__(self, root: Path, config: dict):
        self.root = root.resolve(strict=True)
        self.config = WorkflowConfiguration.model_validate(config).model_dump(mode="json")
        if self.config['review_method'] == AI_REVIEW_METHOD:
            if not self.config['method_amendment'] or not self.config['method_amendment_sha256']:
                raise ValueError('approved review method amendment binding required')
            amendment = json.loads(confined(self.root, self.config['method_amendment']).read_text(encoding='utf8'))
            if raw_hash(confined(self.root,self.config['method_amendment'])) != self.config['method_amendment_sha256']:
                raise ValueError('review method amendment hash mismatch')
            if amendment['method_id'] != AI_REVIEW_METHOD or amendment['status'] != 'OWNER_APPROVED_METHOD_AMENDMENT':
                raise ValueError('review method not explicitly approved')
            event_path=confined(self.root,amendment['confirmation']['path'])
            if raw_hash(event_path)!=amendment['confirmation']['sha256']:
                raise ValueError('method confirmation changed')
            event=json.loads(event_path.read_text(encoding='utf8'))
            for binding in event['bound_files']:
                if raw_hash(confined(self.root,binding['path']))!=binding['sha256']:
                    raise ValueError('user-confirmed method source drift')
            permit=self.config['content_intake_permit']
            if not any(r['path']==permit['source_path'] and r['sha256']==permit['source_sha256'] for r in event['bound_files']):
                raise ValueError('method confirmation does not cover this exact asset')
            if event['human_review_status'] != 'USER_ATTESTED_ACTUAL_REVIEW_AND_CONFIRMATION_OF_AI_ASSISTED_REFERENCES':
                raise ValueError('actual user reference review not confirmed')
            self.method_confirmation=event

    def _actor(self, role: str) -> str:
        actor = authenticated_subject()
        if actor not in self.config[role]:
            raise PermissionError("authenticated principal lacks workflow role")
        return actor

    def _configuration_digest(self) -> str:
        config=dict(self.config)
        # Legacy receipts predate these fields; preserve their exact binding
        # meaning. The amended method always binds all three new fields.
        if config['review_method']==LEGACY_REVIEW_METHOD and config['method_amendment'] is None and config['method_amendment_sha256'] is None:
            for key in ('review_method','method_amendment','method_amendment_sha256'):config.pop(key)
        return sha256_json(config)

    def intake(self) -> dict:
        actor = self._actor("stewards")
        permit = self.config["content_intake_permit"]
        now = utc_now()
        if not permit["authorization_ref"] or not permit["approved_by"] or not datetime.fromisoformat(permit["valid_from_utc"]) <= now < datetime.fromisoformat(permit["expires_at_utc"]):
            raise PermissionError("exact content intake authorization missing/expired")
        source = confined(self.root, permit["source_path"])
        if raw_hash(source) != permit["source_sha256"]:
            raise ValueError("intake source hash mismatch")
        provenance = self.config["provenance"]
        if provenance["replacement_of"] != self.config["required_replacements"] or provenance["copied_or_relabelled_dev"] is not False:
            raise ValueError("replacement provenance unresolved/Dev relabelling prohibited")
        if self.config["intake_stage"] == "FORMAL_VERIFIED" and (provenance["exposure_status"] != "UNEXPOSED_VERIFIED" or not provenance["exposure_evidence_refs"] or not provenance["independence_evidence_refs"]):
            raise ValueError("unseen/independence evidence required, not inferred from review")
        payload = json.loads(source.read_text(encoding="utf8"))
        cases = validate_asset(payload)
        if len(cases) != self.config["expected_case_count"]:
            raise ValueError("asset count differs from the approved intake scope")
        record = {"schema_version": "formal-intake-v1", "source_path": permit["source_path"],
            "workflow_configuration_sha256": self._configuration_digest(),
            "source_sha256": permit["source_sha256"], "authorization": permit,
            "asset_id": self.config["asset_id"], "version": payload["version"],
            "case_ids": [c.case_id for c in cases], "provenance": provenance,
            "actor": actor, "recorded_at_utc": now.isoformat(), "status": "INTAKE_VALIDATED_NOT_GOLD"}
        write_new(confined(self.root, self.config["intake_record"]), record)
        return record

    def _bound(self):
        record = json.loads(confined(self.root, self.config["intake_record"]).read_text(encoding="utf8"))
        if record["workflow_configuration_sha256"] != self._configuration_digest():
            raise ValueError("workflow roles/provenance/paths changed after intake")
        source = confined(self.root, record["source_path"])
        if raw_hash(source) != record["source_sha256"]:
            raise ValueError("reviewed source changed after intake")
        cases = validate_asset(json.loads(source.read_text(encoding="utf8")))
        return record, cases

    def form(self, phase: str) -> dict:
        if phase not in {"day1", "blind"}:
            raise ValueError("review phase invalid")
        record, cases = self._bound()
        # Identical necessary context, no expected labels, AI suggestions, or
        # previous submission in either view. Blind review must occur later.
        return {"schema_version": "formal-reference-review-v1", "phase": phase,
            "source_sha256": record["source_sha256"], "asset_id": record["asset_id"],
            "items": [{"case_id": c.case_id, "case_sha256": formal_case_digest(c),
                "context": formal_case_payload(c)["input"], "rubric_refs": list(c.rubric_refs),
                "labels": None, "rationale": None} for c in cases]}

    def accept(self, submission: dict) -> dict:
        if self.config['review_method'] == AI_REVIEW_METHOD:
            raise ValueError('amended workflow uses confirm-ai-review or accept-ai-blind; never records AI as human')
        actor = self._actor("reviewers")
        phase = submission["phase"]
        expected = self.form(phase)
        if set(submission) != set(expected):
            raise ValueError("review submission envelope drift")
        for key in ("schema_version", "phase", "source_sha256", "asset_id"):
            if submission[key] != expected[key]:
                raise ValueError("review source binding mismatch")
        if [i["case_id"] for i in submission["items"]] != [i["case_id"] for i in expected["items"]]:
            raise ValueError("review missing/extra/duplicate/reordered items")
        _, cases = self._bound()
        for actual, blank, case in zip(submission["items"], expected["items"], cases):
            if set(actual) != set(blank) or any(actual[k] != blank[k] for k in ("case_id", "case_sha256", "context", "rubric_refs")):
                raise ValueError("per-item review binding changed")
            validate_labels(actual["labels"], case)
            if not actual["rationale"]:
                raise ValueError("review rationale required")
        now = utc_now()
        intake, _ = self._bound()
        if now < datetime.fromisoformat(intake["recorded_at_utc"]):
            raise ValueError("review reception predates the actual intake receipt")
        if phase == "blind":
            day1 = json.loads(confined(self.root, self.config["day1_record"]).read_text(encoding="utf8"))
            # Compare local calendar dates of the authenticated receipt time.
            if now.astimezone().date() <= datetime.fromisoformat(day1["accepted_at_utc"]).astimezone().date():
                raise ValueError("blind review requires an actual subsequent local date")
        record = {"submission": submission, "reviewer": actor, "accepted_at_utc": now.isoformat(),
            "method": "INDEPENDENT_LABEL_THEN_NEXT_DAY_BLIND_REVIEW", "reviewer_type": "HUMAN_AUTHENTICATED_OS_PRINCIPAL"}
        write_new(confined(self.root, self.config[phase + "_record"]), record)
        return record

    def confirm_ai_review(self) -> dict:
        """Register an already-attested human confirmation, not invented annotation."""
        actor=self._actor('reviewers')
        if self.config['review_method'] != AI_REVIEW_METHOD:
            raise ValueError('AI-assisted confirmation requires the approved method amendment')
        event=self.method_confirmation
        submission=self.form('day1')
        _,cases=self._bound()
        for row,case in zip(submission['items'],cases):
            row['labels']=formal_case_payload(case)['expected_outcome']
            row['rationale']='User confirmed the bound AI-assisted references without item-specific edits; source rationale remains in the original conditions and requirements.'
        record={'submission':submission,'reviewer':'CHAT_USER_SELF_CONFIRMED',
            'reviewer_type':'HUMAN_USER_ACTUALLY_REVIEWED_AI_ASSISTED_REFERENCES',
            'method':AI_REVIEW_METHOD,'confirmation_event_id':event['event_id'],
            'confirmation_recorded_at_utc':event['recorded_at_utc'],
            'accepted_at_utc':utc_now().isoformat(),'receiving_operator':actor,
            'claim_limit':'User attestation, not independent human initial annotation; no per-item edits invented'}
        write_new(confined(self.root,self.config['day1_record']),record)
        return record

    def blind_package(self) -> dict:
        """Export only visible task/source context and the public labeling contract."""
        if self.config['review_method'] != AI_REVIEW_METHOD:raise ValueError('blind package requires amended method')
        from app.m1.routing import RouteDecision
        route_schema=RouteDecision.model_json_schema()
        route_fields=('strategy','sub_strategy','intent_labels','safety_decision','reason_code','needs_clarification','required_capabilities')
        return {'schema_version':'context-isolated-reference-assessment-v1','form':self.form('blind'),
            'rubric':{
                'route':'Infer a legal route from the visible question and allowed capabilities; do not assume every allowed tool must run. DIRECT/REFUSE/SINGLE/MULTI/AGENT; use the application route schema.',
                'required_evidence':'Only evidence necessary for this question, with applicability condition and actual source IDs. Explicitly empty when no policy is needed; do not invent missing context.',
                'atomic_facts':'Separate subject, predicate, value, condition and actual source IDs; preserve object, date/duration, negation, start event and necessary conditions.',
                'task':'Infer task requirements from the visible request, owner follow-up and sources. Equivalent lawful paths accepted; no unauthorized reads or unconfirmed writes. Unknown business facts must remain unknown.',
                'schema':{
                    'route':{'type':'object','properties':{k:route_schema['properties'][k] for k in route_fields},'required':list(route_fields),'additionalProperties':False},
                    'required_evidence':[{'requirement':'string','condition':'string','source_ids':'actual source IDs[]'}],
                    'atomic_facts':[{'fact_id':'unique string','subject':'string','predicate':'string','value':'JSON value','condition':'string','source_ids':'actual source IDs[]'}],
                    'task':{'requirements':'nonempty string[]','required_tool_groups':'AND groups of OR alternatives, each alternative is tool IDs[]','zero_calls':'boolean','per_case_budget':'BudgetLimits object; use the public ceiling, not a suggested task answer','route_equivalents':'optional legal route alternatives','required_resources':'optional tool/argument/values constraints','required_result_rows':'optional tool/result_path/source_field/rows constraints'}},
                'budget_ceiling':{'provider_calls':8,'tool_calls':12,'total_tokens':120000,'estimated_usd':0.5,'max_calls_per_trial':20,'max_duration_seconds':600,'max_concurrent_trials':1,'max_retries':0,'embedding_calls':0,'judge_calls':0},
                'output':'Fill labels and rationale per item in form, retaining all other fields. Return submission plus actual context_receipt separately; never load previous labels, reviews, conversations or the repository.',
                'uncertainty':'If a reference cannot be responsibly derived, report the exact item/dimension and missing information outside the submission; do not fabricate a complete label to pass import. It remains pending human adjudication.'}}

    def accept_ai_blind(self, envelope: dict) -> dict:
        actor=self._actor('reviewers')
        if self.config['review_method'] != AI_REVIEW_METHOD:
            raise ValueError('AI blind review requires the approved amended method')
        if set(envelope)!={'submission','context_receipt'}:raise ValueError('AI blind envelope invalid')
        context=envelope['context_receipt']
        expected_fields={'assessment_context_id','fresh_context','prior_conversation_loaded','reference_labels_loaded','package_path','package_sha256','started_at_utc','completed_at_utc','assessor_type'}
        if set(context)!=expected_fields or context['assessor_type']!='CODEX_AI' or not context['assessment_context_id'].strip():
            raise ValueError('AI context receipt missing or invalid')
        if context['fresh_context'] is not True or context['prior_conversation_loaded'] is not False or context['reference_labels_loaded'] is not False:
            raise ValueError('blind assessment context contaminated')
        package_path=confined(self.root,context['package_path'])
        if raw_hash(package_path)!=context['package_sha256']:raise ValueError('blind package hash mismatch')
        submission=envelope['submission'];blank=self.form('blind')
        package=json.loads(package_path.read_text(encoding='utf8'))
        if package!=self.blind_package():raise ValueError('blind package source/context drift')
        if set(submission)!=set(blank) or any(submission[k]!=blank[k] for k in ('schema_version','phase','source_sha256','asset_id')):
            raise ValueError('AI blind submission binding drift')
        _,cases=self._bound()
        if len(submission['items'])!=len(cases):raise ValueError('AI blind scope incomplete')
        for actual,empty,case in zip(submission['items'],blank['items'],cases):
            if set(actual)!=set(empty) or any(actual[k]!=empty[k] for k in ('case_id','case_sha256','context','rubric_refs')):
                raise ValueError('AI blind per-item binding drift')
            validate_labels(actual['labels'],case)
            if not actual['rationale']:raise ValueError('AI blind rationale missing')
        first=json.loads(confined(self.root,self.config['day1_record']).read_text(encoding='utf8'))
        started=datetime.fromisoformat(context['started_at_utc']);ended=datetime.fromisoformat(context['completed_at_utc']);now=utc_now()
        if started.tzinfo is None or ended.tzinfo is None or not started<=ended<=now:
            raise ValueError('actual AI assessment timing invalid')
        if started.astimezone().date()<=datetime.fromisoformat(first['confirmation_recorded_at_utc']).astimezone().date():
            raise ValueError('AI blind assessment requires an actual subsequent local date')
        record={'submission':submission,'reviewer':actor,'reviewer_type':'CODEX_AI_NOT_HUMAN',
            'method':AI_REVIEW_METHOD,'accepted_at_utc':now.isoformat(),'context_receipt':context,
            'context_evidence_limit':'Receipt is accountable operator evidence, not proof of model pretraining independence'}
        write_new(confined(self.root,self.config['blind_record']),record)
        return record

    def compare(self) -> dict:
        a = json.loads(confined(self.root, self.config["day1_record"]).read_text(encoding="utf8"))
        b = json.loads(confined(self.root, self.config["blind_record"]).read_text(encoding="utf8"))
        # Loading submissions is a recovery/input boundary, not trusting counters.
        source, cases = self._bound()
        for record, phase in ((a, "day1"), (b, "blind")):
            allowed_actor = record['reviewer'] in self.config['reviewers'] or (self.config['review_method']==AI_REVIEW_METHOD and phase=='day1' and record['reviewer']=='CHAT_USER_SELF_CONFIRMED')
            if not allowed_actor or record["submission"]["source_sha256"] != source["source_sha256"]:
                raise ValueError("review receipt identity/source mismatch")
            if record['method']!=self.config['review_method']:raise ValueError('review method receipt drift')
            if self.config['review_method']==AI_REVIEW_METHOD:
                if phase=='day1' and (record['confirmation_event_id']!=self.method_confirmation['event_id'] or record['reviewer_type']!='HUMAN_USER_ACTUALLY_REVIEWED_AI_ASSISTED_REFERENCES'):
                    raise ValueError('human confirmation receipt drift')
                if phase=='blind' and record['reviewer_type']!='CODEX_AI_NOT_HUMAN':raise ValueError('AI receipt cannot masquerade as human')
                if phase=='blind':
                    context=record['context_receipt']
                    if context['fresh_context'] is not True or context['prior_conversation_loaded'] is not False or context['reference_labels_loaded'] is not False or context['assessor_type']!='CODEX_AI':
                        raise ValueError('stored AI context contaminated')
                    package=confined(self.root,context['package_path'])
                    if raw_hash(package)!=context['package_sha256'] or json.loads(package.read_text(encoding='utf8'))!=self.blind_package():
                        raise ValueError('stored AI package drift')
                    start=datetime.fromisoformat(context['started_at_utc']);end=datetime.fromisoformat(context['completed_at_utc'])
                    if start.tzinfo is None or end.tzinfo is None or not start<=end<=datetime.fromisoformat(record['accepted_at_utc']):
                        raise ValueError('stored AI timing invalid')
                    if start.astimezone().date()<=datetime.fromisoformat(a['confirmation_recorded_at_utc']).astimezone().date():
                        raise ValueError('stored AI context not on subsequent date')
            if [i["case_id"] for i in record["submission"]["items"]] != [c.case_id for c in cases]:
                raise ValueError("review receipt scope drift")
            if record["submission"]["phase"] != phase or record["submission"]["asset_id"] != source["asset_id"]:
                raise ValueError("review receipt phase/asset binding drift")
            for row, case in zip(record["submission"]["items"], cases):
                if row["case_sha256"] != formal_case_digest(case) or row["context"] != formal_case_payload(case)["input"] or row["rubric_refs"] != list(case.rubric_refs):
                    raise ValueError("review receipt case binding drift")
                validate_labels(row["labels"], case)
                if self.config['review_method']==AI_REVIEW_METHOD and phase=='day1' and row['labels']!=formal_case_payload(case)['expected_outcome']:
                    raise ValueError('confirmed per-item labels drifted from user-bound source')
        if datetime.fromisoformat(b["accepted_at_utc"]).astimezone().date() <= datetime.fromisoformat(a["accepted_at_utc"]).astimezone().date():
            raise ValueError("stored blind review is not a subsequent date")
        differences = [{"case_id": x["case_id"], "dimension": key, "day1": x["labels"][key], "blind": y["labels"][key]}
            for x, y in zip(a["submission"]["items"], b["submission"]["items"])
            for key in ("route", "required_evidence", "atomic_facts", "task") if x["labels"][key] != y["labels"][key]]
        return {"source_sha256": source["source_sha256"], "day1_sha256": sha256_json(a), "blind_sha256": sha256_json(b), "differences": differences}

    def freeze(self, adjudication: dict | None = None) -> dict:
        if self.config["intake_stage"] != "FORMAL_VERIFIED":
            raise PermissionError("candidate draft intake cannot become Gold; separately verified provenance and review are required")
        actor = self._actor("adjudicators")
        comparison = self.compare()
        first = json.loads(confined(self.root, self.config["day1_record"]).read_text(encoding="utf8"))
        labels = {r["case_id"]: r["labels"] for r in first["submission"]["items"]}
        differences = comparison["differences"]
        if differences:
            if adjudication is None or adjudication["comparison_sha256"] != sha256_json(comparison):
                raise ValueError("explicit bound adjudication required")
            decisions = adjudication["decisions"]
            keys = [(r["case_id"], r["dimension"]) for r in decisions]
            if len(set(keys)) != len(keys) or set(keys) != {(d["case_id"], d["dimension"]) for d in differences}:
                raise ValueError("adjudication scope incomplete")
            for decision in decisions:
                if not decision["rationale"]:
                    raise ValueError("adjudication rationale missing")
                labels[decision["case_id"]][decision["dimension"]] = decision["value"]
        source, cases = self._bound()
        for case in cases:
            validate_labels(labels[case.case_id], case)
        # References live in a new immutable asset version. Raw intake and both
        # independent submissions remain untouched.
        payload = {"cases": [{**formal_case_payload(c), "expected_outcome": labels[c.case_id]} for c in cases]}
        path = confined(self.root, self.config["frozen_asset"])
        if path.exists():
            if json.loads(path.read_text(encoding="utf8")) != payload:
                raise ValueError("existing locked asset conflicts with review recovery")
        else:
            write_new(path, payload)
        now = utc_now().isoformat()
        review = {"comparison": comparison, "adjudication": adjudication, "actor": actor,
            "locked_at_utc": now, "intake": source, "labels_sha256": sha256_json(labels)}
        lock_path = confined(self.root, self.config["lock_record"])
        if lock_path.exists():
            old = json.loads(lock_path.read_text(encoding="utf8"))
            if any(old[key] != review[key] for key in set(review)-{"locked_at_utc"}):
                raise ValueError("existing lock differs from review recovery")
            review, now = old, old["locked_at_utc"]
        else:
            write_new(lock_path, review)
        amended=self.config['review_method']==AI_REVIEW_METHOD
        asset = DatasetAsset(self.config["asset_id"], "reviewed_readonly_cases", cases[0].data_version, None,
            cases[0].split, len(cases), self.config["frozen_asset"], raw_hash(path), "HUMAN_CONFIRMED_AI_BLIND_REVIEWED" if amended else "INDEPENDENT_REVIEWED",
            first["reviewer"], first["accepted_at_utc"], AI_REVIEW_METHOD if amended else "INDEPENDENT_THEN_NEXT_DAY_BLIND_WITH_ADJUDICATION",
            "INDEPENDENT", source["provenance"]["exposure_status"], "ELIGIBLE", now,
            "EXACT_SIGNED_FORMAL_GRANT_ONLY", ("033 electrical-conflict capability excluded unless specifically reviewed",),
            historical_source_ref=self.config["lock_record"])
        registry_path = confined(self.root, self.config["registry_output"])
        if registry_path.exists():
            if DatasetRegistry.load_metadata(registry_path).get(asset.asset_id) != asset:
                raise ValueError("existing registry differs from reviewed lock")
        else:
            DatasetRegistry([asset]).save_new(registry_path)
        return {"status": "REVIEWED_ASSET_LOCKED_NO_EXECUTION_GRANT", "asset": asdict(asset), "review": review}


def workflow_cli(argv, *, repository_root):
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset-workflow-config", type=Path, required=True)
    parser.add_argument("action", choices=("intake", "day1-form", "blind-form", "blind-package", "accept", "confirm-ai-review", "accept-ai-blind", "compare", "freeze"))
    parser.add_argument("--submission", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path(repository_root).resolve(strict=True)
    config = json.loads(confined(root, str(args.asset_workflow_config)).read_text(encoding="utf8"))
    workflow = AssetWorkflow(root, config)
    if args.action == "intake": result = workflow.intake()
    elif args.action == 'blind-package': result=workflow.blind_package()
    elif args.action == 'confirm-ai-review': result=workflow.confirm_ai_review()
    elif args.action == 'accept-ai-blind': result=workflow.accept_ai_blind(json.loads(confined(root,str(args.submission)).read_text(encoding='utf8')))
    elif args.action.endswith("-form"): result = workflow.form(args.action.split("-")[0])
    elif args.action == "compare": result = workflow.compare()
    else:
        submission = json.loads(confined(root, str(args.submission)).read_text(encoding="utf8")) if args.submission else None
        result = workflow.accept(submission) if args.action == "accept" else workflow.freeze(submission)
    write_new(confined(root, str(args.output)), result)
    return {"status": "OFFLINE_WORKFLOW_OUTPUT_SAVED", "output": str(args.output)}
