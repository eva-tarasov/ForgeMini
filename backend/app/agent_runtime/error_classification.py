from langgraph.errors import GraphRecursionError

from app.agent_runtime.agent_graph import AgentGraphError
from app.agent_runtime.finalization import FinalizationError
from app.agent_runtime.structured_output import StructuredOutputError
from app.sandbox.provider_errors import classify_provider_error
from app.services.agent_failure import AgentFailureCode


_BUDGET_REASONS = (
    ("model_budget_exceeded",),
    ("tool_budget_exceeded",),
    ("finalization_budget_exceeded",),
)


def classify_runtime_error(error: Exception) -> AgentFailureCode:
    """LangGraph-specific failures first; everything else (provider, timeout, unknown) is the sandbox's classifier.

    Legacy: removed together with the graphs (ADR-003, C4). The exception types below are disjoint from the provider ones,
    so delegating does not change any result (test_agent_failure_classification pins every case).
    """
    if isinstance(error, GraphRecursionError):
        return AgentFailureCode.GRAPH_BUDGET_EXCEEDED
    if isinstance(error, AgentGraphError):
        if error.args in _BUDGET_REASONS:
            return AgentFailureCode.GRAPH_BUDGET_EXCEEDED
        return AgentFailureCode.GRAPH_EXECUTION_FAILED
    if isinstance(error, (StructuredOutputError, FinalizationError)):
        return AgentFailureCode.STRUCTURED_OUTPUT_INVALID
    return classify_provider_error(error)
