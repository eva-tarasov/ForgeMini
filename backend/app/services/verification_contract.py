"""Чистый контракт проверки доверенного fixture workspace."""

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from app.permissions.paths import has_protected_component, parse_logical_path
from app.tools.repository.baseline import MAX_REVISION_BYTES
from app.tools.repository.filesystem import MAX_PATH_DEPTH


MAX_CHECKS = 32
MAX_CRITERIA = 10
MAX_CRITERION_LENGTH = 300
MAX_PLAN_METADATA_BYTES = 16 * 1024

_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVISION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")


class VerificationContractError(ValueError):
    """Стабильный код ошибки контракта без входных данных."""


class VerificationVerdict(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    INCONCLUSIVE = "inconclusive"


class ObservationIssue(StrEnum):
    READ_ERROR = "read_error"
    UNKNOWN_EFFECT = "unknown_effect"


@dataclass(frozen=True, slots=True)
class VerificationBinding:
    run_id: UUID
    task_id: UUID
    workspace_id: UUID
    baseline_revision: str = field(repr=False)

    def __post_init__(self) -> None:
        for identifier in (self.run_id, self.task_id, self.workspace_id):
            _require_uuid(identifier)
        revision = self.baseline_revision
        if (
            type(revision) is not str or not 1 <= len(revision) <= MAX_REVISION_BYTES
            or _REVISION.fullmatch(revision) is None
        ):
            raise VerificationContractError("invalid_verification_revision")


@dataclass(frozen=True, slots=True)
class FileDigestCheck:
    check_id: UUID
    criterion_index: int
    path: str = field(repr=False)
    expected_sha256: str = field(repr=False)

    def __post_init__(self) -> None:
        _require_uuid(self.check_id)
        if type(self.criterion_index) is not int or not 0 <= self.criterion_index < MAX_CRITERIA:
            raise VerificationContractError("invalid_verification_criterion_index")
        if type(self.path) is not str:
            raise VerificationContractError("invalid_verification_path")
        try:
            components = parse_logical_path(self.path)
            self.path.encode("utf-8")
        except ValueError:
            raise VerificationContractError("invalid_verification_path") from None
        if not 1 <= len(components) <= MAX_PATH_DEPTH or has_protected_component(components):
            raise VerificationContractError("invalid_verification_path")
        _require_digest(self.expected_sha256)


@dataclass(frozen=True, slots=True)
class VerificationPlan:
    plan_id: UUID
    binding: VerificationBinding
    criterion_digests: tuple[str, ...] = field(repr=False)
    checks: tuple[FileDigestCheck, ...] = field(repr=False)
    fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        _require_uuid(self.plan_id)
        if type(self.binding) is not VerificationBinding:
            raise VerificationContractError("invalid_verification_binding")
        _require_criteria_tuple(self.criterion_digests)
        for digest in self.criterion_digests:
            _require_digest(digest)
        if type(self.checks) is not tuple or not 1 <= len(self.checks) <= MAX_CHECKS:
            raise VerificationContractError("invalid_verification_checks")

        used_ids = {self.plan_id}
        covered_paths: set[tuple[int, str]] = set()
        covered_indices: set[int] = set()
        expected_by_path: dict[str, str] = {}
        for check in self.checks:
            if type(check) is not FileDigestCheck:
                raise VerificationContractError("invalid_verification_checks")
            if check.criterion_index >= len(self.criterion_digests):
                raise VerificationContractError("invalid_verification_criterion_index")
            target = check.criterion_index, check.path
            if check.check_id in used_ids or target in covered_paths:
                raise VerificationContractError("duplicate_verification_check")
            if expected_by_path.setdefault(check.path, check.expected_sha256) != check.expected_sha256:
                raise VerificationContractError("conflicting_verification_expectation")
            used_ids.add(check.check_id)
            covered_paths.add(target)
            covered_indices.add(check.criterion_index)
        if len(covered_indices) != len(self.criterion_digests):
            raise VerificationContractError("uncovered_verification_criterion")
        object.__setattr__(self, "fingerprint", _fingerprint_plan(self))


@dataclass(frozen=True, slots=True)
class FileDigestObservation:
    check_id: UUID
    observed_sha256: str | None = field(default=None, repr=False)
    issue: ObservationIssue | None = None

    def __post_init__(self) -> None:
        _require_uuid(self.check_id)
        if self.observed_sha256 is None and type(self.issue) is ObservationIssue:
            return
        if self.observed_sha256 is None or self.issue is not None:
            raise VerificationContractError("invalid_verification_observation")
        _require_digest(self.observed_sha256)


@dataclass(frozen=True, slots=True)
class VerificationObservationSet:
    plan_id: UUID
    binding: VerificationBinding
    plan_fingerprint: str = field(repr=False)
    observations: tuple[FileDigestObservation, ...] = field(repr=False)

    def __post_init__(self) -> None:
        _require_uuid(self.plan_id)
        if type(self.binding) is not VerificationBinding:
            raise VerificationContractError("invalid_verification_binding")
        _require_digest(self.plan_fingerprint)
        if (
            type(self.observations) is not tuple or len(self.observations) > MAX_CHECKS
            or any(type(observation) is not FileDigestObservation for observation in self.observations)
        ):
            raise VerificationContractError("invalid_verification_observations")


def fingerprint_criteria(criteria: tuple[str, ...]) -> tuple[str, ...]:
    """Связать план с точным упорядоченным снимком критериев."""
    _require_criteria_tuple(criteria)
    digests = []
    for criterion in criteria:
        if type(criterion) is not str or not 1 <= len(criterion) <= MAX_CRITERION_LENGTH:
            raise VerificationContractError("invalid_verification_criteria")
        try:
            encoded = criterion.encode("utf-8")
        except UnicodeEncodeError:
            raise VerificationContractError("invalid_verification_criteria") from None
        digests.append(sha256(encoded).hexdigest())
    return tuple(digests)


def aggregate_verification(plan: VerificationPlan, evidence: VerificationObservationSet) -> VerificationVerdict:
    """Оценить метаданные, полученные от доверенного независимого verifier."""
    if type(plan) is not VerificationPlan or type(evidence) is not VerificationObservationSet:
        raise VerificationContractError("invalid_verification_record")
    if (plan.plan_id, plan.binding, plan.fingerprint) != (evidence.plan_id, evidence.binding, evidence.plan_fingerprint):
        return VerificationVerdict.INCONCLUSIVE

    by_id = {observation.check_id: observation for observation in evidence.observations}
    if len(by_id) != len(evidence.observations) or by_id.keys() != {check.check_id for check in plan.checks}:
        return VerificationVerdict.INCONCLUSIVE
    observed_by_path: dict[str, str] = {}
    mismatch = False
    for check in plan.checks:
        digest = by_id[check.check_id].observed_sha256
        if digest is None or observed_by_path.setdefault(check.path, digest) != digest:
            return VerificationVerdict.INCONCLUSIVE
        if digest != check.expected_sha256:
            mismatch = True
    return VerificationVerdict.FAIL if mismatch else VerificationVerdict.PASS


def _require_uuid(value: UUID) -> None:
    if type(value) is not UUID or value.int == 0:
        raise VerificationContractError("invalid_verification_identity")


def _require_digest(value: str) -> None:
    if type(value) is not str or len(value) != 64 or _SHA256.fullmatch(value) is None:
        raise VerificationContractError("invalid_verification_digest")


def _require_criteria_tuple(values: tuple[str, ...]) -> None:
    if type(values) is not tuple or not 1 <= len(values) <= MAX_CRITERIA:
        raise VerificationContractError("invalid_verification_criteria")


def _fingerprint_plan(plan: VerificationPlan) -> str:
    binding = plan.binding
    payload = {
        "plan_id": str(plan.plan_id),
        "run_id": str(binding.run_id),
        "task_id": str(binding.task_id),
        "workspace_id": str(binding.workspace_id),
        "baseline_revision": binding.baseline_revision,
        "criterion_digests": plan.criterion_digests,
        "checks": [
            [str(check.check_id), check.criterion_index, check.path, check.expected_sha256]
            for check in plan.checks
        ],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(encoded) > MAX_PLAN_METADATA_BYTES:
        raise VerificationContractError("verification_metadata_too_large")
    return sha256(b"forge:verification-plan:v1\x00" + encoded).hexdigest()
