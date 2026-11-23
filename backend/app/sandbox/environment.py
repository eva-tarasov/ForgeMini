"""Setup phase: install the project's dependencies ONCE, with network, into a derived Docker image.

Why: the agent and the Verifier run with `--network none`, so a real project (requirements.txt,
pyproject dependencies) cannot even import its own libraries there. Instead of opening the network to
the agent, a separate, opt-in step runs the user's setup command (e.g. `pip install -r requirements.txt`)
in a container WITH network, and the result is frozen with `docker commit` into an image. Agent and
Verifier containers then start from that image, still without network.

Properties (asserted by tests/test_sandbox_environment_docker.py):
  * The setup command is repository-controlled code that runs with network access. It is opt-in
    (`forge solve --setup ...`), gets the same cap-drop / pid / memory limits as the sandbox, no host
    mounts and no secrets in its environment. See docs/adr/ADR-004.md.
  * /workspace is emptied before the commit: the image carries the installed packages, not a copy of the
    source. Every container uploads the exact snapshot it must test, so the agent's changeset and the
    Verifier's evidence stay byte-for-byte what they were without a setup phase.
  * Content-addressed tag: forgemini-env:<sha256(base image id, setup command, source snapshot)>. The
    Verifier asks for the same tag and normally gets the image the agent used, without a rebuild.
"""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass

from app.sandbox.changeset import Snapshot
from app.sandbox.docker_sandbox import LABEL, WORKDIR, SandboxError, SandboxLimits, _docker, _truncate

ENV_REPOSITORY = "forgemini-env"
ENV_LABEL = "forge.env=1"
SETUP_TIMEOUT_SECONDS = 600.0
OUTPUT_TAIL_CHARS = 4_000


class SetupError(SandboxError):
    """The user's setup command failed (non-zero exit or timeout). Not an agent mistake."""

    def __init__(self, exit_code: int, timed_out: bool, output_tail: str) -> None:
        reason = "timed out" if timed_out else f"exit code {exit_code}"
        super().__init__(f"setup_failed ({reason}):\n{output_tail}")
        self.exit_code = exit_code
        self.timed_out = timed_out
        self.output_tail = output_tail


@dataclass(frozen=True, slots=True)
class Environment:
    image: str           # what agent and Verifier containers start from
    built: bool          # False = an image with this tag already existed (cache hit)
    output_tail: str     # last lines of the setup command ("" on a cache hit)


def environment_tag(base_image_id: str, setup_command: str, source_sha256: str) -> str:
    """Deterministic: same base image + same command + same source => same image."""
    digest = hashlib.sha256("\0".join((base_image_id, setup_command, source_sha256)).encode()).hexdigest()
    return f"{ENV_REPOSITORY}:{digest[:24]}"


async def _image_id(image: str) -> str | None:
    rc, out, _ = await _docker("image", "inspect", "--format", "{{.Id}}", image, timeout=30.0)
    return out.decode().strip() if rc == 0 else None


async def prepare_environment(*, base_image: str, source: Snapshot, setup_command: str,
                              limits: SandboxLimits = SandboxLimits(),
                              timeout: float = SETUP_TIMEOUT_SECONDS) -> Environment:
    base_id = await _image_id(base_image)
    if base_id is None:                       # not local yet: pull it (this phase is allowed the network)
        rc, _, err = await _docker("pull", "-q", base_image, timeout=600.0)
        base_id = await _image_id(base_image) if rc == 0 else None
        if base_id is None:
            raise SandboxError(f"base_image_unavailable: {base_image}: {err.decode(errors='replace')[:200]}")
    tag = environment_tag(base_id, setup_command, source.sha256())
    if await _image_id(tag) is not None:
        return Environment(tag, built=False, output_tail="")

    name = f"forge-setup-{uuid.uuid4().hex[:12]}"
    try:
        rc, _, err = await _docker(
            "create", "--name", name, "--label", LABEL,
            # NO `--network none`: this is the one phase that may download packages.
            "--memory", limits.memory, "--memory-swap", limits.memory,
            "--cpus", str(limits.cpus), "--pids-limit", str(limits.pids),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--workdir", WORKDIR,
            "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "HOME=/tmp",
            "-e", "PIP_NO_CACHE_DIR=1",          # the download cache would only bloat the image
            "-e", "PIP_DISABLE_PIP_VERSION_CHECK=1",
            "--entrypoint", "tail", base_image, "-f", "/dev/null",
        )
        if rc != 0:
            raise SandboxError(f"docker_create_failed: {err.decode(errors='replace')[:300]}")
        rc, _, err = await _docker("start", name)
        if rc != 0:
            raise SandboxError(f"docker_start_failed: {err.decode(errors='replace')[:300]}")
        rc, _, err = await _docker("exec", "-i", name, "tar", "-xf", "-", "-C", WORKDIR, "--no-same-owner",
                                   stdin=source.to_tar(), timeout=120.0)
        if rc != 0:
            raise SandboxError(f"upload_failed: {err.decode(errors='replace')[:300]}")

        rc, out, _ = await _docker(
            "exec", "-w", WORKDIR, name, "timeout", "-k", "5", str(int(timeout)), "bash", "-c", setup_command,
            timeout=timeout + 30.0, merge_stderr=True)
        tail, _ = _truncate(out.decode("utf-8", errors="replace"), OUTPUT_TAIL_CHARS)
        if rc != 0:
            raise SetupError(rc, rc in (124, 137), tail)

        # The image must carry the installed packages, NOT the source: every later container uploads
        # exactly the snapshot it has to test. A leftover file here would show up as an agent change.
        rc, _, err = await _docker("exec", name, "find", WORKDIR, "-mindepth", "1", "-delete", timeout=120.0)
        if rc != 0:
            raise SandboxError(f"workspace_cleanup_failed: {err.decode(errors='replace')[:200]}")
        rc, _, err = await _docker("commit", "--change", f"LABEL {ENV_LABEL}", name, tag, timeout=300.0)
        if rc != 0:
            raise SandboxError(f"docker_commit_failed: {err.decode(errors='replace')[:300]}")
        return Environment(tag, built=True, output_tail=tail)
    finally:
        await asyncio.shield(_remove(name))


async def _remove(name: str) -> None:
    try:
        await _docker("rm", "-f", name, timeout=30.0)
    except SandboxError:
        pass


__all__ = ["Environment", "SetupError", "environment_tag", "prepare_environment"]
