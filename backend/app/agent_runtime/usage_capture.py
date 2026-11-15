import asyncio
from collections.abc import Mapping, Sequence

from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.runnables import Runnable

from app.services.agent_usage import (
    ModelCallOutcome,
    ModelCallStage,
    TokenUsage,
    UsageStatus,
    current_usage_collector,
)


def _optional_count(details: object, name: str, limit: object) -> int | None:
    if isinstance(details, Mapping):
        count = details.get(name)
        if type(count) is int and type(limit) is int and 0 <= count <= limit:
            return count
    return None


def extract_token_usage(response: object) -> tuple[UsageStatus, TokenUsage | None]:
    raw = response.get("raw") if isinstance(response, Mapping) else response
    metadata = getattr(raw, "usage_metadata", None) if isinstance(raw, AIMessage) else None
    if metadata is None:
        return UsageStatus.MISSING, None
    if not isinstance(metadata, Mapping):
        return UsageStatus.INVALID, None
    input_tokens = metadata.get("input_tokens")
    output_tokens = metadata.get("output_tokens")
    try:
        tokens = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=metadata.get("total_tokens"),
            cached_input_tokens=_optional_count(metadata.get("input_token_details"), "cache_read", input_tokens),
            reasoning_tokens=_optional_count(metadata.get("output_token_details"), "reasoning", output_tokens),
        )
    except ValueError:
        return UsageStatus.INVALID, None
    return UsageStatus.REPORTED, tokens


async def invoke_observed_model(
    model: Runnable, messages: Sequence[BaseMessage], *, stage: ModelCallStage,
) -> object:
    collector = current_usage_collector()
    if collector is None:
        return await model.ainvoke(messages)
    number = collector.start_call(stage)
    try:
        response = await model.ainvoke(messages)
    except BaseException as error:
        if not collector.closed:
            outcome = ModelCallOutcome.CANCELLED if isinstance(error, asyncio.CancelledError) else ModelCallOutcome.ERROR
            collector.finish_call(number, outcome=outcome, usage_status=UsageStatus.MISSING, tokens=None)
        raise
    if not collector.closed:
        status, tokens = extract_token_usage(response)
        collector.finish_call(number, outcome=ModelCallOutcome.RETURNED, usage_status=status, tokens=tokens)
    return response
