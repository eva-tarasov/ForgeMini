"""SandboxRunner: implements the existing AgentRunner protocol on top of the new execution layer.

Flow: resolve job -> snapshot source -> start container -> agent loop -> snapshot container ->
changeset = diff(source, container) -> save artifacts -> preliminary AgentFinalResult.

The result is *preliminary* (`ready_for_verification` / `blocked`): the runner never says "succeeded".
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage

from app.sandbox.agent_loop import ChatModel, EventSink, LoopBudget, LoopOutcome, run_agent_loop
from app.sandbox.artifacts import FileArtifactStore
from app.sandbox.changeset import Snapshot
from app.sandbox.docker_sandbox import DockerSandbox, SandboxError, SandboxLimits
from app.sandbox.tools import TOOL_SCHEMAS
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunnerError, AgentRunRequest


class ToolCallingModel(Protocol):
    def bind_tools(self, tools: list[dict[str, Any]], **kwargs: Any) -> ChatModel: ...
    async def ainvoke(self, messages: list[BaseMessage]) -> AIMessage: ...


@dataclass(frozen=True, slots=True)
class SandboxJob:
    source_dir: Path
    image: str
    test_command: str


JobResolver = Callable[[AgentRunRequest], Awaitable[SandboxJob]]


class SandboxRunner:
    def __init__(self, *, model: ToolCallingModel, resolve_job: JobResolver, artifacts: FileArtifactStore,
                 limits: SandboxLimits = SandboxLimits(), budget: LoopBudget = LoopBudget(),
                 emit_factory: Callable[[AgentRunRequest], EventSink | None] | None = None) -> None:
        self._model = model
        self._resolve_job = resolve_job
        self._artifacts = artifacts
        self._limits = limits
        self._budget = budget
        self._emit_factory = emit_factory

    async def run(self, request: AgentRunRequest) -> AgentFinalResult:
        job = await self._resolve_job(request)
        base = await asyncio.to_thread(Snapshot.from_directory, job.source_dir)
        try:
            async with await DockerSandbox.start(image=job.image, source=base, limits=self._limits) as sandbox:
                outcome = await run_agent_loop(
                    self._model.bind_tools(TOOL_SCHEMAS), sandbox, goal=request.goal,
                    test_command=job.test_command, budget=self._budget,
                    emit=self._emit_factory(request) if self._emit_factory else None,
                )
                final = await sandbox.snapshot()
        except SandboxError as exc:
            raise AgentRunnerError(f"sandbox_error: {exc}") from exc
        changeset = base.changes_to(final)
        self._artifacts.save(request.run_id, base=base, changeset=changeset, outcome=outcome)
        return self._to_result(outcome, changeset.paths, changeset.is_empty, changeset.sha256())

    @staticmethod
    def _to_result(outcome: LoopOutcome, paths: tuple[str, ...], empty: bool, digest: str) -> AgentFinalResult:
        if outcome.status == "claimed_done" and not empty:
            return AgentFinalResult(
                summary=outcome.summary, status="ready_for_verification",
                relevant_components=paths,
                assumptions=(f"Agent claims the tests pass. NOT verified yet. changeset sha256={digest[:16]}",),
                observations=(), blocked_reason=None)
        reason = {
            "claimed_done": "agent_reported_done_but_changed_nothing",
            "cannot_fix": f"agent_cannot_fix: {outcome.summary}",
            "budget_exhausted": f"step_budget_exhausted after {outcome.steps} steps",
            "no_finish": outcome.summary,
        }[outcome.status]
        return AgentFinalResult(summary=outcome.summary or reason, status="blocked",
                                relevant_components=paths, assumptions=(), observations=(),
                                blocked_reason=reason)
