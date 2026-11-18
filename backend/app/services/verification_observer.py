"""Read-only observations for a verification plan issued by trusted backend code."""

import asyncio
from dataclasses import dataclass, field
from hashlib import sha256
from os import fsencode
from pathlib import Path
from uuid import UUID

from app.domain.capabilities import Capability
from app.permissions.context import PathScope, PermissionContext
from app.services.verification_contract import (
    FileDigestObservation,
    ObservationIssue,
    VerificationBinding,
    VerificationContractError,
    VerificationObservationSet,
    VerificationPlan,
    fingerprint_criteria,
)
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.factory import create_fixture_executor
from app.tools.repository.filesystem import MAX_FILE_BYTES, RepositoryError
from app.tools.repository.schemas import ReadFileOutput


MAX_OBSERVATION_BYTES = 256 * 1024
# Reserve an extra byte for the reader's EOF probe if a file grows at its limit.
_MAX_READ_CHARGE = MAX_FILE_BYTES + 1


@dataclass(frozen=True, slots=True, kw_only=True)
class TrustedFixtureAccess:
    """Internal grant and Task snapshot supplied by the trusted fixture owner."""

    plan_id: UUID
    plan_fingerprint: str = field(repr=False)
    binding: VerificationBinding
    root: Path = field(repr=False)
    read_scopes: tuple[PathScope, ...] = field(repr=False)
    acceptance_criteria: tuple[str, ...] = field(repr=False)


async def observe(plan: VerificationPlan, trusted_fixture_access: TrustedFixtureAccess) -> VerificationObservationSet:
    """Observe each distinct path through the fixture read tool."""
    access = trusted_fixture_access
    _validate_access(plan, access)
    context = PermissionContext(
        run_id=access.binding.run_id,
        workspace_id=access.binding.workspace_id,
        capabilities=frozenset({Capability.REPOSITORY_READ}),
        read_scopes=access.read_scopes,
    )
    # Only git_diff uses baseline contents. The observer never consults them.
    baseline = FixtureBaseline(access.binding.baseline_revision, {})
    await asyncio.sleep(0)
    try:
        executor = create_fixture_executor(root=access.root, context=context, baseline=baseline)
    except RepositoryError:
        digests = {}
    else:
        digests = await _observe_paths(plan, executor, context)

    await asyncio.sleep(0)
    observations = tuple(
        FileDigestObservation(check.check_id, observed_sha256=digest)
        if (digest := digests.get(check.path)) is not None
        else FileDigestObservation(check.check_id, issue=ObservationIssue.READ_ERROR)
        for check in plan.checks
    )
    return VerificationObservationSet(plan.plan_id, plan.binding, plan.fingerprint, observations)


def _validate_access(plan: VerificationPlan, access: TrustedFixtureAccess) -> None:
    if type(plan) is not VerificationPlan:
        raise VerificationContractError("invalid_verification_plan")
    if (
        type(access) is not TrustedFixtureAccess
        or type(access.plan_id) is not UUID
        or access.plan_id.int == 0
        or type(access.plan_fingerprint) is not str
        or type(access.binding) is not VerificationBinding
        or not isinstance(access.root, Path)
        or not access.root.is_absolute()
        or type(access.read_scopes) is not tuple
        or any(type(scope) is not PathScope for scope in access.read_scopes)
    ):
        raise VerificationContractError("invalid_verification_access")
    try:
        root_bytes = fsencode(access.root)
    except UnicodeEncodeError:
        raise VerificationContractError("invalid_verification_access") from None
    if b"\0" in root_bytes:
        raise VerificationContractError("invalid_verification_access")
    if (
        access.plan_id != plan.plan_id
        or access.plan_fingerprint != plan.fingerprint
        or access.binding != plan.binding
        or fingerprint_criteria(access.acceptance_criteria) != plan.criterion_digests
    ):
        raise VerificationContractError("verification_access_mismatch")


async def _observe_paths(
    plan: VerificationPlan, executor: ToolExecutor, context: PermissionContext,
) -> dict[str, str | None]:
    digests: dict[str, str | None] = {}
    remaining = MAX_OBSERVATION_BYTES
    for path in dict.fromkeys(check.path for check in plan.checks):
        if remaining < _MAX_READ_CHARGE:
            break
        # The fixture handler is synchronous. Let pending cancellations run between reads.
        await asyncio.sleep(0)
        digests[path], charge = await _read_digest(path, executor, context)
        remaining -= charge
    return digests


async def _read_digest(path: str, executor: ToolExecutor, context: PermissionContext) -> tuple[str | None, int]:
    try:
        output = await executor.execute("read_file", {"path": path}, context=context)
    except ToolExecutionError as exc:
        if exc.code is ToolErrorCode.PERMISSION_DENIED:
            return None, 0
        if exc.code in {ToolErrorCode.HANDLER_FAILED, ToolErrorCode.TOOL_TIMEOUT}:
            # The executor doesn't report how many bytes were read before failure.
            return None, _MAX_READ_CHARGE
        raise VerificationContractError("verification_reader_contract_error") from None
    if type(output) is not ReadFileOutput or output.path != path:
        raise VerificationContractError("verification_reader_contract_error")
    try:
        content = output.content.encode("utf-8")
    except UnicodeEncodeError:
        raise VerificationContractError("verification_reader_contract_error") from None
    if len(content) != output.size_bytes or len(content) > MAX_FILE_BYTES:
        raise VerificationContractError("verification_reader_contract_error")
    return sha256(content).hexdigest(), len(content)
