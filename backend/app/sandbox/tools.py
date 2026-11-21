"""The four tools the model can call, and the schemas it sees.

Key design rule (the opposite of the M1 code, where a tool error killed the whole run and the
model never saw it): *every mistake the model can make is returned to it as an error result*,
so it can read the message and try again. Only SandboxError (docker itself broke) propagates.
"""
from __future__ import annotations

import posixpath
from dataclasses import dataclass
from typing import Any

from app.sandbox.docker_sandbox import WORKDIR, DockerSandbox, SandboxError

MAX_READ_LINES = 400
FINISH_STATUSES = ("done", "cannot_fix")

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {"type": "function", "function": {
        "name": "bash",
        "description": "Run a shell command in the sandbox (cwd /workspace, no network). "
                       "Use it to explore the repo and to run the tests. Returns exit code and output.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "bash command line"}},
            "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "read_file",
        "description": f"Read a text file with line numbers. At most {MAX_READ_LINES} lines per call; "
                       "use start_line/end_line (1-based, inclusive) for more.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"},
            "start_line": {"type": "integer"}, "end_line": {"type": "integer"}},
            "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "edit_file",
        "description": "Replace old_str with new_str in a file. old_str must match the file EXACTLY "
                       "once (copy it from read_file WITHOUT the line-number prefix). "
                       "To create a new file pass old_str = \"\".",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}, "old_str": {"type": "string"}, "new_str": {"type": "string"}},
            "required": ["path", "old_str", "new_str"]}}},
    {"type": "function", "function": {
        "name": "finish",
        "description": "End the task. status=done means you believe the tests now pass (this is only a "
                       "claim - it will be re-checked independently). status=cannot_fix means you could "
                       "not or should not fix it; explain why in summary.",
        "parameters": {"type": "object", "properties": {
            "status": {"type": "string", "enum": list(FINISH_STATUSES)},
            "summary": {"type": "string", "description": "what was wrong and what you changed / why not"}},
            "required": ["status", "summary"]}}},
]


@dataclass(frozen=True, slots=True)
class FinishRequest:
    status: str    # "done" | "cannot_fix"
    summary: str


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    content: str
    is_error: bool = False
    finish: FinishRequest | None = None


def _err(msg: str) -> ToolOutcome:
    return ToolOutcome(f"ERROR: {msg}", is_error=True)


def _resolve(path: object) -> str | None:
    if not isinstance(path, str) or not path.strip():
        return None
    full = posixpath.normpath(posixpath.join(WORKDIR, path))
    return full if full == WORKDIR or full.startswith(WORKDIR + "/") else None


class Toolbox:
    def __init__(self, sandbox: DockerSandbox) -> None:
        self._sb = sandbox

    async def call(self, name: str, args: object) -> ToolOutcome:
        if not isinstance(args, dict):
            return _err("arguments must be a JSON object")
        handler = {"bash": self._bash, "read_file": self._read, "edit_file": self._edit,
                   "finish": self._finish}.get(name)
        if handler is None:
            return _err(f"unknown tool {name!r}; available: bash, read_file, edit_file, finish")
        return await handler(args)

    async def _bash(self, a: dict) -> ToolOutcome:
        cmd = a.get("command")
        if not isinstance(cmd, str) or not cmd.strip():
            return _err("'command' must be a non-empty string")
        r = await self._sb.exec(cmd)
        head = "TIMEOUT (command killed)\n" if r.timed_out else ""
        return ToolOutcome(f"{head}exit code: {r.exit_code}\n{r.output}", is_error=False)

    async def _read(self, a: dict) -> ToolOutcome:
        path = _resolve(a.get("path"))
        if path is None:
            return _err(f"'path' must be a non-empty path inside {WORKDIR}")
        try:
            raw = await self._sb.read_file(path)
        except SandboxError as exc:
            return _err(str(exc))
        lines = raw.decode("utf-8", errors="replace").splitlines()
        start = a.get("start_line") if isinstance(a.get("start_line"), int) else 1
        start = max(1, start)
        end = a.get("end_line") if isinstance(a.get("end_line"), int) else start + MAX_READ_LINES - 1
        end = min(end, start + MAX_READ_LINES - 1, len(lines))
        body = "\n".join(f"{i:>5}\t{lines[i - 1]}" for i in range(start, end + 1))
        note = f"\n[lines {start}-{end} of {len(lines)}]"
        return ToolOutcome((body if body else "[empty file]") + note)

    async def _edit(self, a: dict) -> ToolOutcome:
        path = _resolve(a.get("path"))
        old, new = a.get("old_str"), a.get("new_str")
        if path is None or path == WORKDIR:
            return _err(f"'path' must be a file path inside {WORKDIR}")
        if not isinstance(old, str) or not isinstance(new, str):
            return _err("'old_str' and 'new_str' must be strings")
        if old == "":                                   # create a new file
            exists = await self._exists(path)
            if exists:
                return _err("file already exists; to change it pass the exact old_str")
            try:
                await self._sb.write_file(path, new.encode())
            except SandboxError as exc:
                return _err(str(exc))
            return ToolOutcome(f"Created {path}")
        try:
            text = (await self._sb.read_file(path)).decode("utf-8")
        except SandboxError as exc:
            return _err(str(exc))
        except UnicodeDecodeError:
            return _err("file is not valid UTF-8 text")
        count = text.count(old)
        if count == 0:
            return _err("old_str not found. Re-read the file and copy the text exactly "
                        "(whitespace and indentation matter; do not include line numbers).")
        if count > 1:
            return _err(f"old_str matches {count} places; include more surrounding lines to make it unique.")
        try:
            await self._sb.write_file(path, text.replace(old, new, 1).encode())
        except SandboxError as exc:
            return _err(str(exc))
        return ToolOutcome(f"Edited {path} (1 replacement)")

    async def _exists(self, path: str) -> bool:
        try:
            await self._sb.read_file(path)
            return True
        except SandboxError:
            return False

    async def _finish(self, a: dict) -> ToolOutcome:
        status, summary = a.get("status"), a.get("summary")
        if status not in FINISH_STATUSES:
            return _err(f"'status' must be one of {list(FINISH_STATUSES)}")
        if not isinstance(summary, str) or not summary.strip():
            return _err("'summary' must be a non-empty string")
        return ToolOutcome("Finished.", finish=FinishRequest(status, summary.strip()))
