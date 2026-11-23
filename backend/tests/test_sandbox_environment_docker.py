"""Real-Docker proof of the setup phase (ADR-004): what environment.py promises is actually true.

Image: $FORGE_SANDBOX_IMAGE (default python:3.12-slim). No internet is needed: the "dependency" is a file
from the repository copied into site-packages, which is exactly what `pip install` ends up doing.
"""
import asyncio
import subprocess
from uuid import uuid4

import pytest

from app.sandbox.changeset import FileEntry, Snapshot
from app.sandbox.docker_sandbox import DockerSandbox, default_image, docker_available
from app.sandbox.environment import SetupError, environment_tag, prepare_environment

pytestmark = pytest.mark.skipif(not asyncio.run(docker_available()), reason="no Docker daemon available")

SRC = Snapshot({"vendor/forgedep.py": FileEntry(b"VALUE = 42\n", 0o644),
                "app.py": FileEntry(b"import forgedep\n", 0o644)})
SITE = "$(python -c 'import site; print(site.getsitepackages()[0])')"


def _install(nonce: str) -> str:
    # also record the network interfaces the SETUP container saw, to compare with the sandbox later
    return f'cp vendor/forgedep.py "{SITE}/" && ls /sys/class/net > "{SITE}/forge_setup_net.txt"  # {nonce}'


@pytest.fixture
def images():
    created: list[str] = []
    yield created
    for image in created:
        subprocess.run(["docker", "rmi", "-f", image], capture_output=True)


def _env(setup: str, source: Snapshot = SRC):
    return asyncio.run(prepare_environment(base_image=default_image(), source=source, setup_command=setup))


def _in_sandbox(image: str, command: str, source: Snapshot = SRC):
    async def go():
        async with await DockerSandbox.start(image=image, source=source) as sb:
            return await sb.exec(command), await sb.snapshot()
    return asyncio.run(go())


def test_dependency_installed_with_network_is_importable_in_an_offline_sandbox(images):
    env = _env(_install(uuid4().hex))
    images.append(env.image)
    assert env.built and env.image.startswith("forgemini-env:")
    result, _ = _in_sandbox(env.image, f'python -c "import app, forgedep; print(forgedep.VALUE)" && '
                                       f'cat "{SITE}/forge_setup_net.txt" && echo --- && ls /sys/class/net')
    assert result.exit_code == 0, result.output
    seen_by_setup, seen_by_sandbox = result.output.split("42", 1)[1].split("---")
    assert "eth0" in seen_by_setup.split()                     # setup had a network interface...
    assert seen_by_sandbox.split() == ["lo"]                   # ...the sandbox still has none


def test_derived_image_has_an_empty_workspace_so_the_changeset_stays_exact(images):
    # If the image carried a copy of the source (or build leftovers), they would surface as "agent changes".
    setup = 'touch build-leftover.egg-info && ' + _install(uuid4().hex)   # _install ends with a # comment
    env = _env(setup)
    images.append(env.image)
    _, snapshot = _in_sandbox(env.image, "true")
    assert snapshot.changes_to(SRC).is_empty and SRC.changes_to(snapshot).is_empty


def test_same_inputs_reuse_the_image_and_any_change_builds_a_new_one(images):
    setup = _install(uuid4().hex)
    first = _env(setup)
    images.append(first.image)
    again = _env(setup)
    assert (again.image, again.built) == (first.image, False)          # cache hit: no second install
    changed_source = Snapshot({**SRC.files, "app.py": FileEntry(b"import forgedep  # v2\n", 0o644)})
    other = _env(setup, changed_source)
    images.append(other.image)
    assert other.built and other.image != first.image


def test_failing_setup_raises_with_the_tail_of_its_output_and_leaves_nothing_behind(images):
    command = f"echo installing && echo 'ERROR: No matching distribution for nope==9' && exit 3  # {uuid4().hex}"
    with pytest.raises(SetupError) as info:
        _env(command)
    assert info.value.exit_code == 3 and not info.value.timed_out
    assert "No matching distribution" in info.value.output_tail
    leftovers = subprocess.run(["docker", "ps", "-aq", "--filter", "name=forge-setup-"],
                               capture_output=True, text=True).stdout.split()
    assert leftovers == []                                       # the setup container is removed
    base_id = subprocess.run(["docker", "image", "inspect", "--format", "{{.Id}}", default_image()],
                             capture_output=True, text=True).stdout.strip()
    tag = environment_tag(base_id, command, SRC.sha256())
    assert subprocess.run(["docker", "image", "inspect", tag], capture_output=True).returncode != 0   # no image


def test_setup_timeout_is_reported_as_such(images):
    async def go():
        return await prepare_environment(base_image=default_image(), source=SRC,
                                         setup_command=f"sleep 30  # {uuid4().hex}", timeout=2)
    with pytest.raises(SetupError) as info:
        asyncio.run(go())
    assert info.value.timed_out


def test_tag_is_deterministic_and_depends_on_every_input():
    base = environment_tag("sha256:img", "pip install -r requirements.txt", "abc")
    assert base == environment_tag("sha256:img", "pip install -r requirements.txt", "abc")
    assert len({base, environment_tag("sha256:other", "pip install -r requirements.txt", "abc"),
                environment_tag("sha256:img", "pip install .", "abc"),
                environment_tag("sha256:img", "pip install -r requirements.txt", "abd")}) == 4
