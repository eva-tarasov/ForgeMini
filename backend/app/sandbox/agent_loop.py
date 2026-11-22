"""The agent loop: model <-> tools, with budgets. ~100 lines on purpose.

Replaces the LangGraph graphs of M1. A graph engine buys checkpointing/branching, neither of
which this loop needs; what it DOES need is that the model sees every tool error and that
every limit is explicit and visible here.

The loop returns a CLAIM (`claimed_done`), never a verdict. Only the Verifier may mark a task
succeeded, by re-running the tests in a clean container.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.sandbox.docker_sandbox import DockerSandbox
from app.sandbox.tools import Toolbox

SYSTEM_PROMPT = """You are a software engineer fixing a problem in a repository that is checked out in /workspace.
You work inside an isolated sandbox with NO network access: you cannot install packages.

Environment (a minimal Python image):
- Available in bash: grep, find, sed, awk, cat, head, tail, diff, and `python`. Use `grep -rn` to search.
- NOT installed: rg (ripgrep), git, patch, curl, wget. Do not try them; change files with the edit_file tool.

Workflow:
1. Explore the repo (bash: ls, grep, cat) and RUN the test command first to see the real failure.
2. Find the root cause. Make the smallest change that fixes it (edit_file).
3. Re-run the test command and read the output. Iterate until it passes.
4. Call finish.

Rules:
- Do NOT modify or delete existing tests to make them pass; fix the code under test.
- Your answer is checked independently: after you finish, the tests are re-run in a clean container
  on your changes. Never claim success you have not seen in a test run.
- If the task cannot be done (the code is correct and the tests/requirements are wrong or
  contradictory, needs network/secrets, out of scope), do not hack around it:
  call finish with status="cannot_fix" and explain precisely why and what a human could do.
- Every tool call result may be an ERROR - read it and adapt, do not repeat the same call.
"""

Status = Literal["claimed_done", "cannot_fix", "budget_exhausted", "no_finish"]
EventSink = Callable[[str, dict[str, Any]], Awaitable[None]]


class ChatModel(Protocol):
    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage: ...


@dataclass(frozen=True, slots=True)
class LoopBudget:
    max_steps: int = 25           # model calls
    max_nudges: int = 2           # times we remind the model to call a tool / finish


@dataclass(slots=True)
class LoopOutcome:
    status: Status
    summary: str
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    transcript: list[dict[str, Any]] = field(default_factory=list)


def message_text(content: object) -> str:
    """The human-readable text of a model message, whatever shape the provider returned.

    OpenAI's Responses API returns `content` as a LIST of blocks (reasoning with an encrypted payload,
    function_call, text...). str() of that list is what a live run first showed: raw API objects in the
    progress stream and in transcript.json. Only text blocks are text; everything else is not.
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") in ("text", "output_text"):
            text = block.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(p.strip() for p in parts if p.strip())


def _tool_input(name: str, args: object) -> dict[str, str]:
    """The few fields of a tool call worth showing live (a command or a path); never whole file contents."""
    if not isinstance(args, dict):
        return {}
    key = {"bash": "command", "read_file": "path", "edit_file": "path"}.get(name)
    value = args.get(key) if key else None
    return {key: value[:200]} if key and isinstance(value, str) else {}


def _preview(text: str, n: int = 600) -> str:
    """Head + tail: for test output the verdict (FAILED / OK) is at the END."""
    if len(text) <= n:
        return text
    half = n // 2
    return f"{text[:half]}\n...[{len(text) - n} chars omitted]...\n{text[-half:]}"


async def run_agent_loop(
    model: ChatModel, sandbox: DockerSandbox, *, goal: str, test_command: str,
    budget: LoopBudget = LoopBudget(), emit: EventSink | None = None,
) -> LoopOutcome:
    """`model` must already have TOOL_SCHEMAS bound (model.bind_tools(TOOL_SCHEMAS))."""
    toolbox = Toolbox(sandbox)
    out = LoopOutcome(status="budget_exhausted", summary="step budget exhausted")
    messages: list[BaseMessage] = [
        SystemMessage(SYSTEM_PROMPT),
        HumanMessage(f"Task:\n{goal}\n\nTest command (run it from /workspace): {test_command}"),
    ]

    async def note(kind: str, **data: Any) -> None:
        out.transcript.append({"kind": kind, **data})
        if emit:
            await emit(kind, data)

    nudges = 0
    for step in range(1, budget.max_steps + 1):
        ai = await model.ainvoke(messages)
        messages.append(ai)
        out.steps = step
        usage = getattr(ai, "usage_metadata", None) or {}
        out.input_tokens += usage.get("input_tokens", 0)
        out.output_tokens += usage.get("output_tokens", 0)
        text = message_text(ai.content)
        await note("model_response", step=step, text=_preview(text),
                   tool_calls=[c["name"] for c in ai.tool_calls])

        if not ai.tool_calls and not ai.invalid_tool_calls:
            nudges += 1
            if nudges > budget.max_nudges:
                out.status, out.summary = "no_finish", "model stopped calling tools without finishing"
                return out
            messages.append(HumanMessage("Use a tool to continue, or call finish when you are done."))
            continue

        for bad in ai.invalid_tool_calls:           # unparsable arguments: tell the model, don't crash
            messages.append(ToolMessage(f"ERROR: invalid arguments for {bad.get('name')}: "
                                        f"{bad.get('error')}", tool_call_id=bad["id"], status="error"))
        for call in ai.tool_calls:
            res = await toolbox.call(call["name"], call["args"])
            out.tool_calls += 1
            messages.append(ToolMessage(res.content, tool_call_id=call["id"],
                                        status="error" if res.is_error else "success"))
            await note("tool_result", name=call["name"], is_error=res.is_error, turn=step,
                       tool_input=_tool_input(call["name"], call["args"]),
                       args=_preview(str(call["args"]), 200), result=_preview(res.content))
            if res.finish is not None:
                out.status = "claimed_done" if res.finish.status == "done" else "cannot_fix"
                out.summary = res.finish.summary
                await note("finish", status=out.status, summary=out.summary)
                return out
    await note("budget_exhausted", steps=out.steps)
    return out
