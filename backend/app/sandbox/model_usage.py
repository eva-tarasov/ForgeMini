"""Report every model call of the sandbox agent loop to the kernel's UsageCollector.

Why: the kernel already persists usage (event `agent_usage_recorded`) and estimates cost (`agent_cost`), but only a
runner that reports its calls feeds it. The M1 LangGraph runner did; the sandbox loop counted tokens only for its own
report, so `/result.usage` was null for every ForgeMini run (found in ADR-003 / C4). Semantics are the deleted
`agent_runtime/usage_capture.py`, kept on purpose:

- a call is opened BEFORE the provider request, so a call that hangs or crashes is still counted (as ERROR/CANCELLED
  with unknown usage) — "we paid for something we cannot measure" must stay visible as incompleteness, not vanish;
- tokens are taken only from LangChain's normalised `usage_metadata`; anything malformed is INVALID, never guessed;
- outside an execution (no collector, e.g. unit tests of the loop) the call is a plain `ainvoke`.
"""
import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from langchain_core.messages import AIMessage, BaseMessage

from app.services.agent_usage import (
    ModelCallOutcome,
    ModelCallStage,
    TokenUsage,
    UsageStatus,
    current_usage_collector,
)


class InvokableModel(Protocol):
    async def ainvoke(self, messages: Sequence[BaseMessage]) -> Any: ...


def _optional_count(details: object, name: str, limit: object) -> int | None:
    """A sub-count (cached input, reasoning output) is trusted only if it is an int within its parent count."""
    if isinstance(details, Mapping):
        count = details.get(name)
        if type(count) is int and type(limit) is int and 0 <= count <= limit:
            return count
    return None


def extract_token_usage(message: object) -> tuple[UsageStatus, TokenUsage | None]:
    metadata = getattr(message, "usage_metadata", None) if isinstance(message, AIMessage) else None
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
            # None = the provider did not say how much input was served from its cache. The cost estimate then refuses
            # to price the call unless both rates are equal (agent_cost: CACHE_PRICING_UNKNOWN) — no guessing.
            cached_input_tokens=_optional_count(metadata.get("input_token_details"), "cache_read", input_tokens),
            reasoning_tokens=_optional_count(metadata.get("output_token_details"), "reasoning", output_tokens),
        )
    except ValueError:                      # negative, non-int, or total inconsistent with input + output
        return UsageStatus.INVALID, None
    return UsageStatus.REPORTED, tokens


async def invoke_observed(model: InvokableModel, messages: Sequence[BaseMessage]) -> Any:
    collector = current_usage_collector()
    if collector is None:
        return await model.ainvoke(messages)
    number = collector.start_call(ModelCallStage.RESEARCH)    # the sandbox loop has a single stage
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
