from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ConfigDict

from app.agent_runtime.model import build_task_messages


class TaskAnalysis(BaseModel):
    """Preliminary model analysis of the supplied task."""

    model_config = ConfigDict(extra="forbid", strict=True)

    summary: str
    relevant_components: list[str]
    assumptions: list[str]
    blocked_reason: str | None


@dataclass(frozen=True, slots=True)
class TaskAnalysisResult:
    analysis: TaskAnalysis
    raw: AIMessage = field(repr=False)


class StructuredOutputError(RuntimeError):
    def __init__(self, reason: str, *, raw: AIMessage | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.raw = raw


def create_structured_task_model(model: ChatOpenAI) -> Runnable:
    return model.with_structured_output(
        TaskAnalysis, method="json_schema", strict=True, include_raw=True
    )


async def invoke_structured_task_model(
    model: Runnable, *, goal: str, acceptance_criteria: Sequence[str]
) -> TaskAnalysisResult:
    return await invoke_structured_analysis(
        model, messages=build_task_messages(goal, acceptance_criteria)
    )


async def invoke_structured_analysis(
    model: Runnable, *, messages: Sequence[BaseMessage]
) -> TaskAnalysisResult:
    result = await model.ainvoke(list(messages))
    if not isinstance(result, Mapping) or not {"raw", "parsed", "parsing_error"}.issubset(result):
        raise StructuredOutputError("invalid_envelope")

    raw = result["raw"]
    if not isinstance(raw, AIMessage):
        raise StructuredOutputError("invalid_raw")
    if result["parsing_error"] is not None:
        raise StructuredOutputError("parsing_failed", raw=raw)

    parsed = result["parsed"]
    if parsed is None:
        raise StructuredOutputError("missing_parsed", raw=raw)
    if not isinstance(parsed, TaskAnalysis):
        raise StructuredOutputError("invalid_parsed", raw=raw)
    return TaskAnalysisResult(analysis=parsed, raw=raw)
