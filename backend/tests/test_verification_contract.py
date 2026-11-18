from dataclasses import FrozenInstanceError, replace
from hashlib import sha256
from uuid import UUID

import pytest

from app.services.verification_contract import (
    MAX_CHECKS,
    FileDigestCheck,
    FileDigestObservation,
    ObservationIssue,
    VerificationBinding,
    VerificationContractError,
    VerificationObservationSet,
    VerificationPlan,
    VerificationVerdict,
    aggregate_verification,
    fingerprint_criteria,
)


EXPECTED = sha256(b"synthetic fixture").hexdigest()
OTHER = sha256(b"different synthetic fixture").hexdigest()


@pytest.fixture
def plan():
    return VerificationPlan(
        plan_id=UUID(int=1),
        binding=VerificationBinding(UUID(int=2), UUID(int=3), UUID(int=4), "fixture-v1"),
        criterion_digests=fingerprint_criteria(("first private criterion", "second criterion")),
        checks=(
            FileDigestCheck(UUID(int=5), 0, "fixtures/private.txt", EXPECTED),
            FileDigestCheck(UUID(int=6), 0, "fixtures/second.txt", EXPECTED),
            FileDigestCheck(UUID(int=7), 1, "fixtures/second.txt", EXPECTED),
        ),
    )


def observe(plan, observations):
    return VerificationObservationSet(plan.plan_id, plan.binding, plan.fingerprint, tuple(observations))


def matches(plan):
    return tuple(FileDigestObservation(check.check_id, EXPECTED) for check in plan.checks)


def test_valid_plan_binds_ordered_criteria_and_has_stable_identity(plan):
    assert len(plan.fingerprint) == 64 and plan.fingerprint == replace(plan).fingerprint
    reversed_criteria = replace(plan, criterion_digests=plan.criterion_digests[::-1])
    assert reversed_criteria.fingerprint != plan.fingerprint
    assert plan.criterion_digests[0] == sha256(b"first private criterion").hexdigest()
    assert fingerprint_criteria(("\x1f",)) == (sha256(b"\x1f").hexdigest(),)


@pytest.mark.parametrize("invalid", ["not-a-uuid", UUID(int=0), None])
def test_uuids_are_nonzero_and_strict(plan, invalid):
    for field in ("run_id", "task_id", "workspace_id"):
        with pytest.raises(VerificationContractError, match="^invalid_verification_identity$"):
            replace(plan.binding, **{field: invalid})
    with pytest.raises(VerificationContractError, match="^invalid_verification_identity$"):
        replace(plan, plan_id=invalid)
    with pytest.raises(VerificationContractError, match="^invalid_verification_identity$"):
        replace(plan.checks[0], check_id=invalid)


@pytest.mark.parametrize("revision", ["", "/tmp/host/path", "../fixture", "x" * 129, 3])
def test_revision_is_a_bounded_fixture_token(plan, revision):
    with pytest.raises(VerificationContractError, match="^invalid_verification_revision$"):
        replace(plan.binding, baseline_revision=revision)


@pytest.mark.parametrize("index", [-1, 10, True, "0"])
def test_check_criterion_index_is_strict_and_bounded(plan, index):
    with pytest.raises(VerificationContractError, match="^invalid_verification_criterion_index$"):
        replace(plan.checks[0], criterion_index=index)


@pytest.mark.parametrize("path", ["../file", ".", "a/../b", "/etc/passwd", "x/.GiT/config", "x/.env.local", "bad\ud800"])
def test_check_reuses_restricted_logical_paths(plan, path):
    with pytest.raises(VerificationContractError, match="^invalid_verification_path$"):
        replace(plan.checks[0], path=path)


@pytest.mark.parametrize("digest", ["A" * 64, "0" * 63, "g" * 64, "0" * 64 + "\n", b"0" * 64])
def test_sha256_is_lowercase_hex_with_exact_length(plan, digest):
    with pytest.raises(VerificationContractError, match="^invalid_verification_digest$"):
        replace(plan.checks[0], expected_sha256=digest)


@pytest.mark.parametrize("criteria", [(), ("",), ("x" * 301,), ("bad\ud800",), ["criterion"], (42,)])
def test_criteria_fingerprints_validate_snapshot_without_retaining_text(criteria):
    with pytest.raises(VerificationContractError, match="^invalid_verification_criteria$"):
        fingerprint_criteria(criteria)


def test_plan_refuses_empty_checks_and_uncovered_or_foreign_criteria(plan):
    with pytest.raises(VerificationContractError, match="^invalid_verification_checks$"):
        replace(plan, checks=())
    with pytest.raises(VerificationContractError, match="^uncovered_verification_criterion$"):
        replace(plan, checks=plan.checks[:2])
    with pytest.raises(VerificationContractError, match="^invalid_verification_criterion_index$"):
        replace(plan, checks=(*plan.checks[:2], replace(plan.checks[2], criterion_index=2)))


def test_plan_refuses_duplicate_check_identity_and_conflicting_expectations(plan):
    with pytest.raises(VerificationContractError, match="^duplicate_verification_check$"):
        replace(plan, plan_id=plan.checks[0].check_id)
    with pytest.raises(VerificationContractError, match="^duplicate_verification_check$"):
        replace(plan, checks=(*plan.checks, plan.checks[0]))
    with pytest.raises(VerificationContractError, match="^duplicate_verification_check$"):
        replace(plan, checks=(*plan.checks, replace(plan.checks[0], check_id=UUID(int=8))))
    with pytest.raises(VerificationContractError, match="^conflicting_verification_expectation$"):
        replace(plan, checks=(*plan.checks[:2], replace(plan.checks[2], expected_sha256=OTHER)))


def test_plan_bounds_checks_and_total_metadata(plan):
    with pytest.raises(VerificationContractError, match="^invalid_verification_checks$"):
        replace(plan, checks=(plan.checks[0],) * (MAX_CHECKS + 1))
    large_paths = tuple(
        FileDigestCheck(UUID(int=20 + index), index % 2, f"fixture/{index}/" + "界" * 3000, EXPECTED)
        for index in range(3)
    )
    with pytest.raises(VerificationContractError, match="^verification_metadata_too_large$"):
        replace(plan, checks=large_paths)


def test_collections_and_records_cannot_be_mutated_through_caller(plan):
    supplied = list(plan.checks)
    with pytest.raises(VerificationContractError, match="^invalid_verification_checks$"):
        replace(plan, checks=supplied)
    copied = replace(plan, checks=tuple(supplied))
    supplied.clear()
    assert copied.checks == plan.checks
    with pytest.raises(FrozenInstanceError):
        copied.checks[0].path = "different"


def test_complete_observations_pass_in_any_order(plan):
    assert aggregate_verification(plan, observe(plan, reversed(matches(plan)))) is VerificationVerdict.PASS


def test_confirmed_mismatch_fails(plan):
    first, *rest = matches(plan)
    observation = replace(first, observed_sha256=OTHER)
    assert aggregate_verification(plan, observe(plan, (observation, *rest))) is VerificationVerdict.FAIL


@pytest.mark.parametrize("issue", [ObservationIssue.READ_ERROR, ObservationIssue.UNKNOWN_EFFECT])
def test_unread_or_unknown_effect_is_inconclusive_even_with_mismatch(plan, issue):
    first, second, third = matches(plan)
    missing_digest = FileDigestObservation(first.check_id, issue=issue)
    confirmed_mismatches = (replace(second, observed_sha256=OTHER), replace(third, observed_sha256=OTHER))
    assert aggregate_verification(plan, observe(plan, (missing_digest, *confirmed_mismatches))) is VerificationVerdict.INCONCLUSIVE


def test_observed_digest_and_unknown_reason_are_mutually_exclusive(plan):
    check_id = plan.checks[0].check_id
    with pytest.raises(VerificationContractError, match="^invalid_verification_observation$"):
        FileDigestObservation(check_id, EXPECTED, ObservationIssue.READ_ERROR)
    with pytest.raises(VerificationContractError, match="^invalid_verification_observation$"):
        FileDigestObservation(check_id)


def test_incomplete_foreign_duplicate_and_conflicting_observations_are_inconclusive(plan):
    first, second, third = matches(plan)
    cases = (
        (),
        (first,),
        (first, second, second),
        (first, second, replace(third, check_id=UUID(int=20))),
        (first, second, replace(third, observed_sha256=OTHER)),
    )
    for observations in cases:
        assert aggregate_verification(plan, observe(plan, observations)) is VerificationVerdict.INCONCLUSIVE


def test_correlation_rejects_foreign_binding_and_changed_plan_with_same_id(plan):
    observations = observe(plan, matches(plan))
    for field in ("run_id", "task_id", "workspace_id"):
        foreign = replace(observations, binding=replace(plan.binding, **{field: UUID(int=20)}))
        assert aggregate_verification(plan, foreign) is VerificationVerdict.INCONCLUSIVE
    assert aggregate_verification(plan, replace(observations, plan_id=UUID(int=20))) is VerificationVerdict.INCONCLUSIVE
    foreign_revision = replace(observations, binding=replace(plan.binding, baseline_revision="fixture-v2"))
    assert aggregate_verification(plan, foreign_revision) is VerificationVerdict.INCONCLUSIVE
    changed = replace(plan, checks=(replace(plan.checks[0], expected_sha256=OTHER), *plan.checks[1:]))
    assert aggregate_verification(changed, observations) is VerificationVerdict.INCONCLUSIVE


def test_raw_verdict_is_not_a_typed_observation(plan):
    with pytest.raises(VerificationContractError, match="^invalid_verification_observations$"):
        observe(plan, ({"check_id": plan.checks[0].check_id, "status": "pass"},))
    with pytest.raises(VerificationContractError, match="^invalid_verification_record$"):
        aggregate_verification(plan, VerificationVerdict.PASS)


def test_repr_and_validation_errors_do_not_echo_metadata(plan):
    observations = observe(plan, matches(plan))
    representation = repr((plan, plan.checks[0], observations, observations.observations[0]))
    for private in ("private", "fixture-v1", EXPECTED, plan.fingerprint, *plan.criterion_digests):
        assert private not in representation
    with pytest.raises(VerificationContractError, match="^invalid_verification_path$") as caught:
        replace(plan.checks[0], path="/host/sensitive-path")
    assert caught.value.__cause__ is None and caught.value.__suppress_context__
