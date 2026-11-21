"""Test doubles for the sandbox layer: a scripted chat model and an in-memory sandbox."""
from __future__ import annotations

import itertools
from collections.abc import Callable, Sequence

from langchain_core.messages import AIMessage, BaseMessage

from app.sandbox.docker_sandbox import ExecResult, SandboxError

_ids = itertools.count(1)


def tool_turn(name: str, args: dict, *, text: str = "") -> AIMessage:
    return AIMessage(content=text, tool_calls=[{"name": name, "args": args, "id": f"call_{next(_ids)}"}],
                     usage_metadata={"input_tokens": 100, "output_tokens": 10, "total_tokens": 110})


def text_turn(text: str) -> AIMessage:
    return AIMessage(content=text)


class ScriptedModel:
    """Plays back a fixed list of replies and records exactly what the loop sent it.

    A turn may also be a callable(messages) -> AIMessage for reactive behaviour.
    """

    def __init__(self, turns: Sequence[AIMessage | Callable[[list[BaseMessage]], AIMessage]]) -> None:
        self._turns = list(turns)
        self.seen: list[list[BaseMessage]] = []

    def bind_tools(self, *_a, **_k) -> ScriptedModel:
        return self

    async def ainvoke(self, messages: Sequence[BaseMessage]) -> AIMessage:
        self.seen.append(list(messages))
        if not self._turns:
            raise AssertionError("scripted model ran out of turns")
        turn = self._turns.pop(0)
        return turn(list(messages)) if callable(turn) else turn


class FakeSandbox:
    """In-memory stand-in for DockerSandbox (files + canned command output)."""

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files = {k: v.encode() for k, v in (files or {}).items()}
        self.commands: list[str] = []
        self.exec_result = ExecResult(0, "ok")

    async def exec(self, command: str, *, timeout: float | None = None) -> ExecResult:
        self.commands.append(command)
        return self.exec_result

    async def read_file(self, path: str) -> bytes:
        if path not in self.files:
            raise SandboxError(f"read_failed: cat: {path}: No such file or directory")
        return self.files[path]

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data
