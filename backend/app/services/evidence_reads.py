"""An allowlisted view of independently recorded, validated evidence."""

from collections import defaultdict
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.evidence import (
    EvidenceAttemptDetail, EvidenceAttemptSummary, EvidenceCheckResponse, EvidenceListResponse, EvidenceOutcome,
)
from app.services.agent_run_reads import AgentRunReadService
from app.services.verification_contract import FileDigestObservation
from app.services.verification_evidence import VerificationEvidence


class EvidenceAttemptNotFoundError(LookupError):
    pass


def _summary(sequence: int, evidence: VerificationEvidence) -> EvidenceAttemptSummary:
    return EvidenceAttemptSummary(
        attempt_id=evidence.attempt_id, sequence=sequence, verdict=evidence.verdict, file_verdict=evidence.file_verdict,
        audit_complete=evidence.audit_complete, isolated_fixture=evidence.isolated_fixture,
        check_count=len(evidence.plan.checks),
    )


def _outcome(
    observations: list[FileDigestObservation], path_digests: set[str], expected_sha256: str,
) -> EvidenceOutcome:
    if not observations:
        return "missing"
    if len(observations) != 1:
        return "ambiguous"
    [observed] = observations
    if observed.issue is not None:
        return observed.issue.value
    if len(path_digests) > 1:
        return "ambiguous"
    return "match" if observed.observed_sha256 == expected_sha256 else "mismatch"


def _checks(evidence: VerificationEvidence) -> list[EvidenceCheckResponse]:
    checks = evidence.plan.checks
    path_by_id = {check.check_id: check.path for check in checks}
    observations_by_id: dict[UUID, list[FileDigestObservation]] = defaultdict(list)
    digests_by_path: dict[str, set[str]] = defaultdict(set)
    for observed in evidence.observations.observations:
        path = path_by_id.get(observed.check_id)
        if path is None:
            continue
        observations_by_id[observed.check_id].append(observed)
        if observed.observed_sha256 is not None:
            digests_by_path[path].add(observed.observed_sha256)
    return [
        EvidenceCheckResponse(
            check_id=check.check_id, criterion_index=check.criterion_index, path=check.path,
            outcome=_outcome(observations_by_id[check.check_id], digests_by_path[check.path], check.expected_sha256),
        )
        for check in checks
    ]


class EvidenceReadService:
    def __init__(self, session: AsyncSession) -> None:
        self._runs = AgentRunReadService(session)

    async def list_attempts(
        self, run_id: UUID, workspace_id: UUID, *, after_sequence: int, limit: int,
    ) -> EvidenceListResponse:
        _, history = await self._runs.read_validated_result(run_id, workspace_id=workspace_id)
        # Apply the cursor only after checking the entire bounded history for contradictions.
        candidates = [(sequence, evidence) for sequence, evidence in history if sequence > after_sequence]
        items = [_summary(sequence, evidence) for sequence, evidence in candidates[:limit]]
        return EvidenceListResponse(
            run_id=run_id, items=items, next_after_sequence=items[-1].sequence if items else after_sequence,
            has_more=len(candidates) > limit,
        )

    async def get_attempt(self, run_id: UUID, workspace_id: UUID, attempt_id: UUID) -> EvidenceAttemptDetail:
        _, history = await self._runs.read_validated_result(run_id, workspace_id=workspace_id)
        for sequence, evidence in history:
            if evidence.attempt_id == attempt_id:
                return EvidenceAttemptDetail(**_summary(sequence, evidence).model_dump(), checks=_checks(evidence))
        raise EvidenceAttemptNotFoundError()
