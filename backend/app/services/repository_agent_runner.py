from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from langchain_openai import ChatOpenAI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.agent_runtime.langgraph_runner import LangGraphAgentRunner
from app.agent_runtime.repository_graph import create_repository_graph
from app.agent_runtime.repository_tool_schemas import create_repository_tool_model
from app.agent_runtime.structured_output import create_structured_task_model
from app.domain.capabilities import Capability
from app.domain.execution import AgentRunStatus
from app.permissions.context import PathScope, PermissionContext
from app.repositories.agent_runs import AgentRunRepository
from app.repositories.tasks import TaskRepository
from app.services.agent_failure import AgentFailureCode
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunnerError, AgentRunRequest
from app.services.audited_tool_executor import AuditedToolExecutor
from app.services.tool_calls import ToolCallService
from app.tools.repository.baseline import FixtureBaseline
from app.tools.repository.factory import create_fixture_executor


@dataclass(frozen=True, slots=True)
class RepositoryFixtureBinding:
    """Disposable fixture and read grants selected by trusted backend composition."""

    workspace_id: UUID
    root: Path
    baseline: FixtureBaseline
    capabilities: frozenset[Capability]
    read_scopes: tuple[PathScope, ...]


class RepositoryAgentRunner:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        model: ChatOpenAI,
        binding: RepositoryFixtureBinding,
    ) -> None:
        if model.max_retries != 0:
            raise ValueError("repository_model_retries_must_be_disabled")
        self._session_factory = session_factory
        self._model = model
        self._binding = binding

    async def run(self, request: AgentRunRequest) -> AgentFinalResult:
        binding = self._binding
        async with self._session_factory() as session:
            run = await AgentRunRepository(session).get(request.run_id)
            if run is None or run.task_id != request.task_id or run.status != AgentRunStatus.RUNNING.value:
                raise AgentRunnerError(AgentFailureCode.EXECUTION_FAILED.value)
            task = await TaskRepository(session).get(request.task_id)
            if task is None or task.workspace_id != binding.workspace_id:
                raise AgentRunnerError(AgentFailureCode.EXECUTION_FAILED.value)

        context = PermissionContext(
            run_id=request.run_id,
            workspace_id=binding.workspace_id,
            capabilities=binding.capabilities & frozenset({Capability.REPOSITORY_READ}),
            read_scopes=binding.read_scopes,
        )
        executor = create_fixture_executor(root=binding.root, context=context, baseline=binding.baseline)
        audited = AuditedToolExecutor(executor, ToolCallService(self._session_factory))
        graph = create_repository_graph(
            create_repository_tool_model(self._model, executor),
            create_structured_task_model(self._model),
            audited,
            context,
        )
        return await LangGraphAgentRunner(graph).run(request)
