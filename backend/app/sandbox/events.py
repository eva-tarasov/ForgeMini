"""Live progress events of the agent loop, stored as ordinary AgentEvents (type `agent_step`).

One short transaction per step. The public events API then lets a client `--watch` a run: that is the
answer to M1's "what did the agent actually do?" opacity.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.repositories.agent_events import AgentEventRepository
from app.sandbox.agent_loop import EventSink
from app.services.agent_runner import AgentRunRequest

AGENT_STEP_EVENT = "agent_step"
MAX_SUMMARY_CHARS = 600
_STEP_KINDS = ("model_response", "tool_result", "finish", "budget_exhausted", "error")


def _squeeze(text: str, head: int, tail: int) -> str:
    text = " ".join(text.split())
    if len(text) <= head + tail + 5:
        return text
    return text[:head] + (" … " + text[-tail:] if tail else " …")


def step_payload(kind: str, data: dict[str, Any]) -> dict[str, Any]:
    if kind == "model_response":
        calls = data.get("tool_calls") or []
        text = (data.get("text") or "").strip()
        summary = f"step {data.get('step')}: " + (", ".join(calls) if calls else "(no tool call)")
        if text:
            summary += f" | {text}"
    elif kind == "tool_result":
        flag = "ERROR " if data.get("is_error") else ""
        args = _squeeze(str(data.get("args")), 100, 0)
        # test output ends with its verdict (OK / FAILED), so keep the tail, not just the head
        summary = f"{data.get('name')}({args}) -> {flag}{_squeeze(str(data.get('result')), 120, 300)}"
    elif kind == "finish":
        summary = f"finish[{data.get('status')}]: {data.get('summary')}"
    elif kind == "error":
        summary = f"run error: {data.get('detail')}"
    else:
        summary = f"stopped: {kind} (steps={data.get('steps')})"
    return {"schema_version": 1, "step": kind if kind in _STEP_KINDS else "other",
            "summary": summary[:MAX_SUMMARY_CHARS]}


def make_emit_factory(session_factory: Callable[[], AsyncSession]) -> Callable[[AgentRunRequest], EventSink]:
    def factory(request: AgentRunRequest) -> EventSink:
        async def emit(kind: str, data: dict[str, Any]) -> None:
            async with session_factory() as session:
                try:
                    await AgentEventRepository(session).append(
                        request.run_id, event_type=AGENT_STEP_EVENT, payload=step_payload(kind, data))
                    await session.commit()
                except BaseException:
                    await session.rollback()
                    raise
        return emit
    return factory
