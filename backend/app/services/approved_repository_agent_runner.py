import asyncio
from dataclasses import dataclass
from uuid import UUID

from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_runtime.approved_repository_graph import create_approved_repository_graph
from app.agent_runtime.langgraph_runner import LangGraphAgentRunner
from app.agent_runtime.repository_tool_schemas import create_approved_repository_tool_model
from app.agent_runtime.structured_output import create_structured_task_model
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus
from app.permissions.context import PathScope, PermissionContext
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.services.agent_failure import AgentFailureCode
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.approval_contracts import ApprovalDecision
from app.services.approval_outcome import ApprovalOutcomePort
from app.services.approved_tool_executor import ApprovedToolExecutor, _ApprovalPreview
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.repository_agent_runner import RepositoryFixtureBinding
from app.services.tool_calls import ToolCallService
from app.tools.errors import ToolErrorCode, ToolExecutionError
from app.tools.executor import ToolExecutor
from app.tools.registry import ToolRegistry
from app.tools.repository.factory import create_fixture_executor, create_fixture_patch_tool


@dataclass(frozen=True, slots=True)
class ApprovedRepositoryFixtureBinding(RepositoryFixtureBinding):
    """Disposable fixture and explicit grants supplied by trusted backend code."""

    write_scopes: tuple[PathScope, ...]


class ApprovedRepositoryAgentRunner:
    """A runner and private coordination handle for one backend-selected run."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], model: ChatOpenAI,
        binding: ApprovedRepositoryFixtureBinding, *, run_id: UUID, wait_seconds: float = 60,
    ) -> None:
        if model.max_retries != 0:
            raise ValueError("repository_model_retries_must_be_disabled")
        if binding.capabilities != frozenset({Capability.REPOSITORY_READ, Capability.REPOSITORY_WRITE}):
            raise ValueError("approved_repository_requires_exact_grants")
        self._session_factory = session_factory
        self._model = model
        self._binding = binding
        self._run_id = run_id
        self._wait_seconds = wait_seconds
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active: tuple[PermissionContext, ApprovedToolExecutor] | None = None
        self._started = False

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        if self._loop is not loop:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)

    def _approval_preview(self) -> _ApprovalPreview | None:
        self._check_loop()
        if self._active is None:
            return None
        context, executor = self._active
        return executor._pending_preview(context)

    async def _decide(self, preview: _ApprovalPreview, decision: ApprovalDecision) -> None:
        if type(preview) is not _ApprovalPreview or self._approval_preview() != preview or self._active is None:
            raise ToolExecutionError(ToolErrorCode.APPROVAL_INVALID)
        context, executor = self._active
        await executor.decide(preview.reference, decision, context=context)

    async def run(self, request: AgentRunRequest) -> AgentFinalResult:
        return await self.run_with_approval_outcome(request, outcome=None)

    async def run_with_approval_outcome(
        self, request: AgentRunRequest, *, outcome: ApprovalOutcomePort | None,
    ) -> AgentFinalResult:
        self._check_loop()
        if request.run_id != self._run_id:
            raise AgentRunnerError(AgentFailureCode.EXECUTION_FAILED.value)
        binding = self._binding
        async with self._session_factory() as session:
            run = await AgentRunRepository(session).get(request.run_id)
            if run is None or run.task_id != request.task_id or run.status != AgentRunStatus.RUNNING.value:
                raise AgentRunnerError(AgentFailureCode.EXECUTION_FAILED.value)
            task = await TaskRepository(session).get(request.task_id)
            if task is None or task.workspace_id != binding.workspace_id:
                raise AgentRunnerError(AgentFailureCode.EXECUTION_FAILED.value)

        if self._started:
            raise AgentRunnerError(AgentFailureCode.EXECUTION_FAILED.value)
        # The approval contract requires stdlib UUIDs. asyncpg can return UUID subclasses.
        context = PermissionContext(
            UUID(str(request.run_id)), UUID(str(binding.workspace_id)),
            binding.capabilities, binding.read_scopes, binding.write_scopes,
        )
        reader = create_fixture_executor(root=binding.root, context=context, baseline=binding.baseline)
        writer = ToolExecutor(ToolRegistry([create_fixture_patch_tool(root=binding.root, context=context)]))
        audited = AuditedToolExecutor(reader, ToolCallService(self._session_factory))
        approved = ApprovedToolExecutor(
            writer, self._session_factory, wait_seconds=self._wait_seconds, capacity=1, outcome=outcome,
        )
        graph = create_approved_repository_graph(
            create_approved_repository_tool_model(self._model, reader, writer),
            create_structured_task_model(self._model),
            audited, approved, context,
        )
        self._started = True
        self._active = context, approved
        try:
            return await LangGraphAgentRunner(graph).run(request)
        finally:
            self._active = None
