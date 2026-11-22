"""Independent verification of a sandbox run: the only code path that can make a run `succeeded`.

Three phases, so no DB transaction is held while Docker runs tests for minutes:
  1. read   (short tx)   run must be VERIFYING with a `ready_for_verification` result; load workspace config
  2. observe (no tx)      pristine source + saved changeset -> FRESH containers -> run the test command
  3. record (locked tx)   re-check run unchanged, append evidence event, set the terminal status
                          (and Task status) in ONE commit: evidence and status can never disagree.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.domain.execution import AgentRunStatus
from app.domain.transitions import validate_agent_run_transition
from app.repositories.agent_events import AgentEventRepository
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.repositories.workspaces import WorkspaceRepository
from app.sandbox.artifacts import FileArtifactStore
from app.sandbox.changeset import Changeset, Snapshot, SnapshotError
from app.sandbox.docker_sandbox import DockerSandbox, SandboxError, SandboxLimits, default_image
from app.sandbox.verification import (
    SANDBOX_VERIFICATION_RECORDED, CommandRun, SandboxVerification, decide, find_protected_violations,
)
from app.services.agent_result_codec import AgentResultContractError, decode_agent_result
from app.services.agent_runs import AgentRunNotFoundError, StaleAgentRunStatusError
from app.services.task_sync import sync_task_to_run
from app.services.verification_contract import VerificationVerdict
from app.services.verification_evidence import metadata_digest

logger = logging.getLogger("forge.sandbox.verifier")
SessionFactory = Callable[[], AsyncSession]


class SandboxVerificationError(RuntimeError):
    """The run is not in a state that can be verified (wrong status / no preliminary result)."""


class SandboxVerifier:
    def __init__(self, session_factory: SessionFactory, artifacts: FileArtifactStore, *,
                 limits: SandboxLimits = SandboxLimits(command_timeout=300.0)) -> None:
        self._session_factory = session_factory
        self._artifacts = artifacts
        self._limits = limits

    async def verify(self, run_id: UUID) -> SandboxVerification:
        # ---- phase 1: read ------------------------------------------------
        async with self._session_factory() as session:
            run = await AgentRunRepository(session).get(run_id)
            if run is None:
                raise AgentRunNotFoundError(run_id)
            if run.status != AgentRunStatus.VERIFYING.value:
                raise StaleAgentRunStatusError(AgentRunStatus.VERIFYING, AgentRunStatus(run.status))
            try:
                result = decode_agent_result(run.final_result)
            except AgentResultContractError:
                raise SandboxVerificationError("preliminary_result_required") from None
            if result.status != "ready_for_verification":
                raise SandboxVerificationError("preliminary_result_required")
            result_sha = metadata_digest(run.final_result)
            task = await TaskRepository(session).get(run.task_id)
            workspace = await WorkspaceRepository(session).get(task.workspace_id) if task else None
        started = time.monotonic()

        # ---- phase 2: observe (never holds a DB transaction) ---------------
        try:
            verification = await self._observe(run_id, workspace)
        except Exception as exc:    # a verifier that crashes must not leave the run stuck in VERIFYING
            logger.exception("sandbox_verification_crashed")
            verification = _inconclusive(f"verification_internal_error: {type(exc).__name__}", workspace)
        verification = _with_duration(verification, int((time.monotonic() - started) * 1000))

        # ---- phase 3: record evidence + terminal status atomically -----------
        async with self._session_factory() as session:
            try:
                locked = await AgentRunRepository(session).get_for_update(run_id)
                if locked is None:
                    raise AgentRunNotFoundError(run_id)
                if locked.status != AgentRunStatus.VERIFYING.value:
                    raise StaleAgentRunStatusError(AgentRunStatus.VERIFYING, AgentRunStatus(locked.status))
                if metadata_digest(locked.final_result) != result_sha:
                    raise SandboxVerificationError("result_changed_during_verification")
                events = AgentEventRepository(session)
                await events.append(run_id, event_type=SANDBOX_VERIFICATION_RECORDED,
                                    payload=verification.to_payload(result_sha256=result_sha))
                passed = verification.verdict is VerificationVerdict.PASS
                target = AgentRunStatus.SUCCEEDED if passed else AgentRunStatus.FAILED
                validate_agent_run_transition(AgentRunStatus.VERIFYING, target)
                locked.status = target.value     # direct write on purpose: the generic transition forbids SUCCEEDED
                await events.add_status_changed(run_id, AgentRunStatus.VERIFYING, target)
                task_row = await TaskRepository(session).get_for_update(locked.task_id)
                if task_row is not None:
                    sync_task_to_run(task_row, target, verified=passed)
                await session.commit()
            except BaseException:
                await session.rollback()
                raise
        return verification

    # ------------------------------------------------------------------
    async def _observe(self, run_id: UUID, workspace) -> SandboxVerification:
        if workspace is None or not (workspace.source_path and workspace.test_command):
            return _inconclusive("workspace_not_configured: source_path and test_command are required", workspace)
        image = workspace.image or default_image()
        if not self._artifacts.has_run(run_id):
            return _inconclusive("artifacts_missing", workspace, image)
        outcome = self._artifacts.load_outcome(run_id)
        changeset = self._artifacts.load_changeset(run_id)
        if changeset.sha256() != outcome.get("changeset_sha256"):
            return _inconclusive("changeset_integrity_mismatch", workspace, image)
        try:
            base = await asyncio.to_thread(Snapshot.from_directory, _path(workspace.source_path))
        except SnapshotError as exc:
            return _inconclusive(f"source_unreadable: {exc}", workspace, image)
        if base.sha256() != outcome.get("base_sha256"):
            return _inconclusive("source_changed_since_run: the project files differ from what the agent saw",
                                 workspace, image, changeset)
        violations = find_protected_violations(base, changeset)
        patched = baseline = None
        try:
            if not violations and not changeset.is_empty:
                patched = await self._run_tests(image, changeset.apply_to(base), workspace.test_command)
                if patched.passed:    # only worth proving the "was red before" half if it can matter
                    baseline = await self._run_tests(image, base, workspace.test_command)
        except SandboxError as exc:
            return _inconclusive(f"verification_infrastructure_error: {exc}", workspace, image, changeset,
                                 violations)
        verdict, reason = decide(changeset_empty=changeset.is_empty, violations=violations,
                                 patched=patched, baseline=baseline)
        return SandboxVerification(
            verdict, reason, workspace.test_command, image, changeset.sha256(), changeset.paths,
            violations, patched, baseline, 0)

    async def _run_tests(self, image: str, tree: Snapshot, command: str) -> CommandRun:
        # A brand-new container per run: nothing the agent left behind (processes, caches, files
        # outside the changeset, edited tools) can influence the result.
        async with await DockerSandbox.start(image=image, source=tree, limits=self._limits) as sandbox:
            r = await sandbox.exec(command)
        return CommandRun(r.exit_code, r.timed_out, r.output)


def _path(value: str):
    from pathlib import Path
    return Path(value)


def _inconclusive(reason: str, workspace, image: str | None = None, changeset: Changeset | None = None,
                  violations: tuple[str, ...] = ()) -> SandboxVerification:
    return SandboxVerification(
        VerificationVerdict.INCONCLUSIVE, reason,
        getattr(workspace, "test_command", None) or "", image or getattr(workspace, "image", None) or "",
        changeset.sha256() if changeset else "", changeset.paths if changeset else (), violations,
        None, None, 0)


def _with_duration(v: SandboxVerification, ms: int) -> SandboxVerification:
    from dataclasses import replace
    return replace(v, duration_ms=ms)
