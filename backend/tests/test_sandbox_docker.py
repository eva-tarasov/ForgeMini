"""Real-Docker tests. They are the proof that the isolation claims in docker_sandbox.py are true.

Image: $FORGE_SANDBOX_IMAGE (default python:3.12-slim). Skipped only when no Docker daemon exists.
"""
import asyncio
import subprocess
import time

import pytest

from app.sandbox.changeset import FileEntry, Snapshot
from app.sandbox.docker_sandbox import (
    DockerSandbox, SandboxLimits, default_image, docker_available,
)

pytestmark = pytest.mark.skipif(
    not asyncio.run(docker_available()), reason="no Docker daemon available"
)

SRC = Snapshot({"hello.txt": FileEntry(b"hello\n", 0o644),
                "run.sh": FileEntry(b"#!/bin/sh\necho from-script\n", 0o755),
                "pkg/mod.py": FileEntry(b"VALUE = 1\n", 0o644)})


def run(coro):
    return asyncio.run(coro)


async def _with_sandbox(body, limits=SandboxLimits()):
    async with await DockerSandbox.start(image=default_image(), source=SRC, limits=limits) as sb:
        return await body(sb)


def test_exec_runs_in_workspace_with_uploaded_files():
    async def body(sb):
        r = await sb.exec("cat hello.txt && ./run.sh && pwd")
        assert r.exit_code == 0
        assert r.output.split() == ["hello", "from-script", "/workspace"]   # exec bit preserved
    run(_with_sandbox(body))


def test_network_is_disabled():
    async def body(sb):
        # Independent of whether the CI runner has internet: with `--network none` the container
        # has no network interface except loopback (a default bridge container also has eth0).
        r = await sb.exec("ls /sys/class/net")
        assert r.output.split() == ["lo"]
        assert (await sb.exec("exec 3<>/dev/tcp/1.1.1.1/53", timeout=10)).exit_code != 0
    run(_with_sandbox(body))


def test_agent_writes_do_not_touch_the_host_checkout(tmp_path):
    (tmp_path / "f.txt").write_text("original\n")
    src = Snapshot.from_directory(tmp_path)

    async def body():
        async with await DockerSandbox.start(image=default_image(), source=src) as sb:
            await sb.exec("echo hacked > f.txt && rm -f f.txt")
    run(body())
    assert (tmp_path / "f.txt").read_text() == "original\n"          # copy, not bind mount


def test_timeout_kills_the_process_inside_the_container():
    async def body(sb):
        t0 = time.monotonic()
        r = await sb.exec("sleep 2 && touch /tmp/survived", timeout=1)
        assert r.timed_out and time.monotonic() - t0 < 8
        await asyncio.sleep(3)                                         # past the sleep's natural end
        assert (await sb.exec("test -e /tmp/survived")).exit_code != 0  # it was really killed
    run(_with_sandbox(body))


def test_output_is_truncated_keeping_the_tail():
    async def body(sb):
        r = await sb.exec("seq 1 50000")
        assert r.truncated and len(r.output) < 400 + 12_000
        assert r.output.rstrip().endswith("50000")     # test summaries live at the end of the output
    run(_with_sandbox(body))


def test_read_write_file_roundtrip_and_errors():
    async def body(sb):
        await sb.write_file("/workspace/new/dir/f.txt", b"data\xff")
        assert await sb.read_file("/workspace/new/dir/f.txt") == b"data\xff"
        with pytest.raises(Exception, match="read_failed"):
            await sb.read_file("/workspace/nope")
    run(_with_sandbox(body))


def test_snapshot_diff_sees_edits_creations_deletions_and_ignores_pycache():
    async def body(sb):
        await sb.exec("echo 'VALUE = 2' > pkg/mod.py && rm hello.txt && echo n > new.txt "
                      "&& mkdir -p pkg/__pycache__ && echo x > pkg/__pycache__/m.pyc")
        return SRC.changes_to(await sb.snapshot())
    cs = run(_with_sandbox(body))
    assert sorted(cs.written) == ["new.txt", "pkg/mod.py"] and cs.deleted == ("hello.txt",)


def test_container_is_removed_after_use_even_on_error():
    names = []

    async def body():
        try:
            async with await DockerSandbox.start(image=default_image(), source=SRC) as sb:
                names.append(sb.name)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
    run(body())
    out = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={names[0]}"],
                         capture_output=True, text=True).stdout
    assert out.strip() == ""


def test_start_with_missing_image_fails_cleanly_and_leaks_nothing():
    async def body():
        await DockerSandbox.start(image="forge-image-that-does-not-exist:0", source=SRC)
    with pytest.raises(Exception, match="docker_create_failed"):
        run(body())
    assert subprocess.run(["docker", "ps", "-aq", "--filter", "label=forge.sandbox=1"],
                          capture_output=True, text=True).stdout.strip() == ""


# --- live-run finding: the model wasted calls on `rg`, which the stock image does not have -------------

import os  # noqa: E402

from app.sandbox.agent_loop import SYSTEM_PROMPT  # noqa: E402

PROMPT_SAYS_AVAILABLE = ["grep", "find", "sed", "awk", "cat", "head", "tail", "diff", "python"]
PROMPT_SAYS_MISSING = ["rg", "git", "patch", "curl", "wget"]


def test_prompt_environment_claims_are_stated_in_the_prompt():
    for tool in PROMPT_SAYS_AVAILABLE + PROMPT_SAYS_MISSING:
        assert tool in SYSTEM_PROMPT, f"{tool} is tested here but not mentioned in the prompt"


def test_tools_the_prompt_promises_exist_in_the_sandbox_image():
    async def body(sb):
        for tool in PROMPT_SAYS_AVAILABLE:
            assert (await sb.exec(f"command -v {tool}")).exit_code == 0, f"prompt promises {tool}, image lacks it"
    run(_with_sandbox(body))


@pytest.mark.skipif(os.environ.get("FORGE_SANDBOX_IMAGE") not in (None, "", "python:3.12-slim"),
                    reason="the 'not installed' claims hold for the stock python:3.12-slim image only")
def test_tools_the_prompt_says_are_missing_really_are_missing_in_the_stock_image():
    async def body(sb):
        for tool in PROMPT_SAYS_MISSING:
            assert (await sb.exec(f"command -v {tool}")).exit_code != 0, f"prompt says {tool} is absent, but it exists"
    run(_with_sandbox(body))
