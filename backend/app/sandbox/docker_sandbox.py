"""One throw-away Docker container per agent run.

Isolation properties (all of them are asserted by tests/test_sandbox_docker.py):
  * --network none           the agent cannot reach the internet or the host network
  * code is COPIED in        no bind mount, so the agent can never write to the user's checkout
  * --cap-drop ALL, no-new-privileges, pids/memory/cpu limits
  * every command runs under `timeout -k` INSIDE the container; killing only the local
    `docker exec` client would leave the process running in the container.
  * `docker rm -f` in __aexit__ (shielded from cancellation) + a label so orphans can be swept.

We talk to Docker through its CLI, not the Docker SDK: one fewer dependency, identical on macOS/Linux.
"""
from __future__ import annotations

import asyncio
import contextlib
import uuid
from dataclasses import dataclass

from app.sandbox.changeset import Snapshot

WORKDIR = "/workspace"
LABEL = "forge.sandbox=1"


class SandboxError(RuntimeError):
    """The sandbox itself failed (docker missing, container died). Not an agent mistake."""


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    memory: str = "1g"
    cpus: float = 1.0
    pids: int = 256
    command_timeout: float = 120.0    # per `bash` call
    max_output_chars: int = 12_000    # what the model sees from one command


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    output: str            # stdout and stderr merged, truncated
    timed_out: bool = False
    truncated: bool = False


async def _docker(*args: str, stdin: bytes | None = None, timeout: float = 60.0,
                  merge_stderr: bool = False) -> tuple[int, bytes, bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise SandboxError("docker_not_installed") from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise SandboxError("docker_call_timeout") from None
    return proc.returncode or 0, out or b"", err or b""


def _truncate(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit // 4                       # keep the start (command echo, first error)...
    tail = limit - head                     # ...but mostly the end (test summaries live there)
    cut = len(text) - limit
    return f"{text[:head]}\n... [{cut} chars truncated] ...\n{text[-tail:]}", True


class DockerSandbox:
    def __init__(self, name: str, limits: SandboxLimits) -> None:
        self.name = name
        self.limits = limits
        self._closed = False

    # ---- lifecycle -----------------------------------------------------
    @classmethod
    async def start(cls, *, image: str, source: Snapshot,
                    limits: SandboxLimits = SandboxLimits()) -> DockerSandbox:
        name = f"forge-sbx-{uuid.uuid4().hex[:12]}"
        sandbox = cls(name, limits)
        try:
            rc, _, err = await _docker(
                "create", "--name", name, "--label", LABEL,
                "--network", "none",
                "--memory", limits.memory, "--memory-swap", limits.memory,
                "--cpus", str(limits.cpus), "--pids-limit", str(limits.pids),
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--workdir", WORKDIR,
                "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
                "--entrypoint", "tail", image, "-f", "/dev/null",
            )
            if rc != 0:
                raise SandboxError(f"docker_create_failed: {err.decode(errors='replace')[:300]}")
            rc, _, err = await _docker("start", name)
            if rc != 0:
                raise SandboxError(f"docker_start_failed: {err.decode(errors='replace')[:300]}")
            # Extract as the in-container user (`--no-same-owner`): files end up owned by the user
            # that will run the commands, whatever uid the host checkout had (macOS: 501).
            rc, _, err = await _docker(
                "exec", "-i", name, "tar", "-xf", "-", "-C", WORKDIR, "--no-same-owner",
                stdin=source.to_tar(), timeout=120.0,
            )
            if rc != 0:
                raise SandboxError(f"upload_failed: {err.decode(errors='replace')[:300]}")
        except BaseException:
            await asyncio.shield(sandbox.close())
            raise
        return sandbox

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(SandboxError):
            await _docker("rm", "-f", self.name, timeout=30.0)

    async def __aenter__(self) -> DockerSandbox:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await asyncio.shield(self.close())

    @staticmethod
    async def remove_orphans() -> int:
        """Remove every container this tool ever labelled (crash recovery)."""
        rc, out, _ = await _docker("ps", "-aq", "--filter", f"label={LABEL}")
        ids = out.decode().split()
        if rc == 0 and ids:
            await _docker("rm", "-f", *ids, timeout=60.0)
        return len(ids)

    # ---- running things ------------------------------------------------
    async def exec(self, command: str, *, timeout: float | None = None) -> ExecResult:
        limit = timeout or self.limits.command_timeout
        rc, out, _ = await _docker(
            "exec", "-w", WORKDIR, self.name,
            "timeout", "-k", "2", str(int(limit)), "bash", "-c", command,
            timeout=limit + 15.0, merge_stderr=True,
        )
        text, truncated = _truncate(out.decode("utf-8", errors="replace"), self.limits.max_output_chars)
        # GNU timeout: 124 = TERM was needed, 137 = it had to KILL
        return ExecResult(rc, text, timed_out=rc in (124, 137), truncated=truncated)

    async def read_file(self, path: str) -> bytes:
        rc, out, err = await _docker("exec", self.name, "sh", "-c", 'cat -- "$1"', "_", path)
        if rc != 0:
            raise SandboxError(f"read_failed: {err.decode(errors='replace').strip()[:200]}")
        return out

    async def write_file(self, path: str, data: bytes) -> None:
        rc, _, err = await _docker(
            "exec", "-i", self.name, "sh", "-c",
            'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"', "_", path, stdin=data,
        )
        if rc != 0:
            raise SandboxError(f"write_failed: {err.decode(errors='replace').strip()[:200]}")

    async def snapshot(self) -> Snapshot:
        """Current state of /workspace (ignored dirs such as __pycache__ are skipped)."""
        rc, out, err = await _docker("exec", self.name, "tar", "-cf", "-", "-C", WORKDIR, ".",
                                     timeout=120.0)
        if rc != 0:
            raise SandboxError(f"export_failed: {err.decode(errors='replace')[:200]}")
        return Snapshot.from_tar(out)


async def docker_available() -> bool:
    try:
        rc, _, _ = await _docker("info", timeout=15.0)
    except SandboxError:
        return False
    return rc == 0


def default_image() -> str:
    import os
    return os.environ.get("FORGE_SANDBOX_IMAGE", "python:3.12-slim")


__all__ = ["DockerSandbox", "ExecResult", "SandboxError", "SandboxLimits", "WORKDIR",
           "docker_available", "default_image"]
