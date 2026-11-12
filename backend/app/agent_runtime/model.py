import json
from collections.abc import Sequence

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from app.agent_runtime.settings import ModelSettings


TASK_SYSTEM_INSTRUCTIONS = (
    "Analyze the supplied task data and acceptance criteria. "
    "Do not claim you read files, changed code, or ran or passed tests. "
    "State what remains unverified."
)


def create_openai_model(settings: ModelSettings) -> ChatOpenAI:
    return ChatOpenAI(
        model=settings.forge_model_name,
        api_key=settings.openai_api_key,
        timeout=settings.forge_model_timeout_seconds,
        max_retries=0,
    )


def build_task_messages(goal: str, acceptance_criteria: Sequence[str]) -> list[BaseMessage]:
    payload = {"goal": goal, "acceptance_criteria": list(acceptance_criteria)}
    return [
        SystemMessage(content=TASK_SYSTEM_INSTRUCTIONS),
        HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
    ]


async def invoke_task_model(
    model: BaseChatModel, *, goal: str, acceptance_criteria: Sequence[str]
) -> AIMessage:
    return await model.ainvoke(build_task_messages(goal, acceptance_criteria))
