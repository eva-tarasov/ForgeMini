from dataclasses import replace
from hashlib import sha256
from uuid import UUID

import pytest

from app.services.verification_contract import (
    FileDigestCheck, FileDigestObservation, VerificationBinding, VerificationContractError,
    VerificationObservationSet, VerificationPlan, VerificationVerdict, fingerprint_criteria,
)
from app.services.verification_evidence import VerificationEvidence, decode_evidence, encode_evidence


@pytest.fixture
def evidence():
    digest = sha256(b"synthetic").hexdigest()
    plan = VerificationPlan(
        UUID(int=1), VerificationBinding(UUID(int=2), UUID(int=3), UUID(int=4), "fixture-v1"),
        fingerprint_criteria(("exact",)), (FileDigestCheck(UUID(int=5), 0, "out.txt", digest),),
    )
    observations = VerificationObservationSet(
        plan.plan_id, plan.binding, plan.fingerprint, (FileDigestObservation(UUID(int=5), digest),),
    )
    return VerificationEvidence(UUID(int=6), plan, observations, True, True, digest, digest, digest)


def test_evidence_roundtrip_and_recomputed_verdict(evidence):
    payload = encode_evidence(evidence)
    assert decode_evidence(payload) == evidence
    assert evidence.verdict is VerificationVerdict.PASS
    assert replace(evidence, audit_complete=False).verdict is VerificationVerdict.INCONCLUSIVE
    assert replace(evidence, isolated_fixture=False).verdict is VerificationVerdict.INCONCLUSIVE


@pytest.mark.parametrize("field,value", [
    ("attempt_id", UUID(int=0)), ("audit_complete", 1), ("isolated_fixture", "yes"),
    ("audit_sha256", "X" * 64), ("access_sha256", None), ("result_sha256", "bad"),
])
def test_evidence_rejects_invalid_metadata(evidence, field, value):
    with pytest.raises(VerificationContractError):
        replace(evidence, **{field: value})


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 2), ("verdict", "fail"), ("file_verdict", "fail"),
    ("attempt_id", "bad"), ("unexpected", "secret"), ("audit_complete", 1),
])
def test_evidence_rejects_tampered_stored_payload(evidence, field, value):
    payload = encode_evidence(evidence)
    payload[field] = value
    with pytest.raises(VerificationContractError, match="^invalid_verification_evidence$"):
        decode_evidence(payload)


def test_evidence_rejects_foreign_observations(evidence):
    foreign = replace(evidence.observations, binding=replace(evidence.plan.binding, run_id=UUID(int=99)))
    with pytest.raises(VerificationContractError):
        replace(evidence, observations=foreign)


def test_evidence_rejects_changed_plan_body(evidence):
    payload = encode_evidence(evidence)
    payload["plan"]["checks"][0]["path"] = "different.txt"
    with pytest.raises(VerificationContractError, match="^invalid_verification_evidence$"):
        decode_evidence(payload)


def test_evidence_diagnostics_hide_plan_and_observation_metadata(evidence):
    assert "out.txt" not in repr(evidence)
    assert "synthetic" not in repr(evidence)
    payload = encode_evidence(evidence)
    payload["attempt_id"] = "synthetic credential"
    with pytest.raises(VerificationContractError) as caught:
        decode_evidence(payload)
    assert str(caught.value) == "invalid_verification_evidence"
