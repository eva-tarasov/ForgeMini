from langgraph.graph.state import CompiledStateGraph

from app.agent_runtime.agent_graph import run_agent_graph
from app.agent_runtime.error_classification import classify_runtime_error
from app.services.agent_result import AgentFinalResult
from app.services.agent_runner import AgentRunnerError, AgentRunRequest


class LangGraphAgentRunner:
    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph

    async def run(self, request: AgentRunRequest) -> AgentFinalResult:
        try:
            state = await run_agent_graph(
                self._graph,
                goal=request.goal,
                acceptance_criteria=request.acceptance_criteria,
            )
            return state["final_result"]
        except Exception as exc:
            code = classify_runtime_error(exc)
            raise AgentRunnerError(code.value) from exc
