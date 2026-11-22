"""Unit tests of app/sandbox/model_usage.py: every sandbox model call is reported to the kernel UsageCollector."""
import asyncio
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from app.sandbox.model_usage import extract_token_usage, invoke_observed
from app.services.agent_usage import (
    ModelCallOutcome,
    ModelCallStage,
    TokenUsage,
    UsageStatus,
    capture_agent_usage,
)

MESSAGES = [HumanMessage("synthetic task")]


def _reply(**metadata):
    return AIMessage(content="ok", usage_metadata=metadata or None)


class Model:
    def __init__(self, reply=None, error=None):
        self.reply, self.error, self.calls = reply, error, 0

    async def ainvoke(self, messages):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.reply


@pytest.mark.parametrize("message,expected", [
    (_reply(input_tokens=10, output_tokens=2, total_tokens=12,
            input_token_details={"cache_read": 4}, output_token_details={"reasoning": 1}),
     (UsageStatus.REPORTED, TokenUsage(10, 2, 12, cached_input_tokens=4, reasoning_tokens=1))),
    # sub-counts that do not fit their parent are dropped (unknown), not trusted
    (_reply(input_tokens=10, output_tokens=2, total_tokens=12, input_token_details={"cache_read": 11}),
     (UsageStatus.REPORTED, TokenUsage(10, 2, 12))),
    (AIMessage(content="no usage"), (UsageStatus.MISSING, None)),
    # a total that contradicts input + output is not "close enough": the whole call is INVALID
    (_reply(input_tokens=10, output_tokens=2, total_tokens=13), (UsageStatus.INVALID, None)),
    (_reply(input_tokens=-1, output_tokens=2, total_tokens=1), (UsageStatus.INVALID, None)),
    ("not a message", (UsageStatus.MISSING, None)),
])
def test_token_usage_is_taken_only_from_valid_usage_metadata(message, expected):
    assert extract_token_usage(message) == expected


def _calls_after(model, *, cancel=False):
    async def main():
        with capture_agent_usage(uuid4()) as collector:
            if cancel:
                task = asyncio.create_task(invoke_observed(model, MESSAGES))
                await asyncio.sleep(0)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                try:
                    await invoke_observed(model, MESSAGES)
                except RuntimeError:
                    pass
            return collector.snapshot().calls
    return asyncio.run(main())


def test_returned_call_is_recorded_with_its_tokens():
    [call] = _calls_after(Model(_reply(input_tokens=3, output_tokens=2, total_tokens=5)))
    assert (call.stage, call.outcome, call.usage_status, call.tokens) == (
        ModelCallStage.RESEARCH, ModelCallOutcome.RETURNED, UsageStatus.REPORTED, TokenUsage(3, 2, 5))


def test_failed_call_is_still_counted_as_error_with_unknown_usage():
    """We may have paid for a call that crashed: it must show up as incomplete usage, not disappear."""
    [call] = _calls_after(Model(error=RuntimeError("synthetic provider outage")))
    assert (call.outcome, call.usage_status, call.tokens) == (ModelCallOutcome.ERROR, UsageStatus.MISSING, None)


def test_cancelled_call_is_counted_as_cancelled():
    class Hanging:
        async def ainvoke(self, messages):
            await asyncio.Event().wait()

    [call] = _calls_after(Hanging(), cancel=True)
    assert (call.outcome, call.usage_status) == (ModelCallOutcome.CANCELLED, UsageStatus.MISSING)


def test_without_an_active_collector_the_call_is_a_plain_invoke():
    model = Model(_reply(input_tokens=1, output_tokens=1, total_tokens=2))
    assert asyncio.run(invoke_observed(model, MESSAGES)).content == "ok" and model.calls == 1
