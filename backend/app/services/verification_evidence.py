"""Bounded, versioned metadata stored in an existing AgentEvent JSONB payload."""

import json
import re
from dataclasses import dataclass, field
from hashlib import sha256
from uuid import UUID

from app.services.verification_contract import (
    FileDigestCheck,
    FileDigestObservation,
    ObservationIssue,
    VerificationBinding,
    VerificationContractError,
    VerificationObservationSet,
    VerificationPlan,
    VerificationVerdict,
    aggregate_verification,
)


VERIFICATION_RECORDED = "verification_recorded"
MAX_EVIDENCE_BYTES = 32 * 1024


def metadata_digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VerificationEvidence:
    attempt_id: UUID
    plan: VerificationPlan = field(repr=False)
    observations: VerificationObservationSet = field(repr=False)
    audit_complete: bool
    isolated_fixture: bool
    audit_sha256: str = field(repr=False)
    access_sha256: str = field(repr=False)
    result_sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.attempt_id) is not UUID or self.attempt_id.int == 0
            or type(self.plan) is not VerificationPlan
            or type(self.observations) is not VerificationObservationSet
            or type(self.audit_complete) is not bool or type(self.isolated_fixture) is not bool
            or any(type(value) is not str or re.fullmatch(r"[0-9a-f]{64}", value) is None
                   for value in (self.audit_sha256, self.access_sha256, self.result_sha256))
        ):
            raise VerificationContractError("invalid_verification_evidence")
        if (self.observations.plan_id, self.observations.binding, self.observations.plan_fingerprint) != (
            self.plan.plan_id, self.plan.binding, self.plan.fingerprint,
        ):
            raise VerificationContractError("invalid_verification_evidence")

    @property
    def file_verdict(self) -> VerificationVerdict:
        return aggregate_verification(self.plan, self.observations)

    @property
    def verdict(self) -> VerificationVerdict:
        if not self.audit_complete or not self.isolated_fixture:
            return VerificationVerdict.INCONCLUSIVE
        return self.file_verdict


def encode_evidence(evidence: VerificationEvidence) -> dict:
    if type(evidence) is not VerificationEvidence:
        raise VerificationContractError("invalid_verification_evidence")
    plan, binding = evidence.plan, evidence.plan.binding
    payload = {
        "schema_version": 1,
        "attempt_id": str(evidence.attempt_id),
        "plan": {
            "plan_id": str(plan.plan_id),
            "fingerprint": plan.fingerprint,
            "run_id": str(binding.run_id),
            "task_id": str(binding.task_id),
            "workspace_id": str(binding.workspace_id),
            "baseline_revision": binding.baseline_revision,
            "criterion_digests": list(plan.criterion_digests),
            "checks": [
                {"check_id": str(check.check_id), "criterion_index": check.criterion_index,
                 "path": check.path, "expected_sha256": check.expected_sha256}
                for check in plan.checks
            ],
        },
        "observations": [
            {"check_id": str(item.check_id), "observed_sha256": item.observed_sha256,
             "issue": item.issue.value if item.issue is not None else None}
            for item in evidence.observations.observations
        ],
        "audit_complete": evidence.audit_complete,
        "isolated_fixture": evidence.isolated_fixture,
        "audit_sha256": evidence.audit_sha256,
        "access_sha256": evidence.access_sha256,
        "result_sha256": evidence.result_sha256,
        "file_verdict": evidence.file_verdict.value,
        "verdict": evidence.verdict.value,
    }
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_EVIDENCE_BYTES:
        raise VerificationContractError("verification_evidence_too_large")
    return payload


def decode_evidence(payload: object) -> VerificationEvidence:
    """Rebuild the contract and recompute both verdicts before trusting stored data."""
    try:
        if type(payload) is not dict or type(payload.get("schema_version")) is not int:
            raise ValueError
        if payload["schema_version"] != 1 or len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_EVIDENCE_BYTES:
            raise ValueError
        body = payload["plan"]
        binding = VerificationBinding(
            UUID(body["run_id"]), UUID(body["task_id"]), UUID(body["workspace_id"]), body["baseline_revision"],
        )
        plan = VerificationPlan(
            UUID(body["plan_id"]), binding, tuple(body["criterion_digests"]),
            tuple(FileDigestCheck(
                UUID(check["check_id"]), check["criterion_index"], check["path"], check["expected_sha256"],
            ) for check in body["checks"]),
        )
        observations = VerificationObservationSet(
            plan.plan_id, binding, plan.fingerprint,
            tuple(FileDigestObservation(
                UUID(item["check_id"]), item["observed_sha256"],
                ObservationIssue(item["issue"]) if item["issue"] is not None else None,
            ) for item in payload["observations"]),
        )
        result = VerificationEvidence(
            UUID(payload["attempt_id"]), plan, observations, payload["audit_complete"], payload["isolated_fixture"],
            payload["audit_sha256"], payload["access_sha256"], payload["result_sha256"],
        )
        if encode_evidence(result) != payload:
            raise ValueError
        return result
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise VerificationContractError("invalid_verification_evidence") from None
