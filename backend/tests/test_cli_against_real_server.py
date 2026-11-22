"""The real `forge` command-line program against a real ForgeMini server over a real TCP port.

Everything the owner's demo uses is real here (CLI process, HTTP, PostgreSQL, Docker sandbox, Verifier, `git clone`
from a URL, `git apply`); only the model's moves are scripted. This is the test that would have caught a CLI/server
contract drift that unit tests on either side cannot see.
"""
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import uvicorn

from sandbox_support import ScriptedModel, tool_turn
from test_demo_shop_e2e import FIX_SHIPPING, MAKE_REPO, _git, _tests
from test_forgemini_http_e2e import _db_settings, pytestmark  # noqa: F401  (skip marks)

from app.bootstrap import ForgeMiniSettings, create_forge_app
from app.sandbox.docker_sandbox import default_image  # noqa: F401

ISSUE1 = "python -m unittest tests.test_regressions.Issue1FreeShippingAtThreshold"


@pytest.fixture
def live_server(tmp_path):
    state = {"turns": []}
    app = create_forge_app(ForgeMiniSettings(forge_data_dir=tmp_path / "data", openai_api_key=None),
                           db_settings=_db_settings(), model_factory=lambda: ScriptedModel(state["turns"]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    assert server.started, "uvicorn did not start"
    yield f"http://127.0.0.1:{port}", state
    server.should_exit = True
    thread.join(timeout=15)


def _forge(base_url: str, *args: str, cwd: Path, env_extra: dict | None = None):
    import os
    env = {**os.environ, "NO_COLOR": "1", **(env_extra or {})}
    entry = "import sys; from forge_cli.main import main; sys.argv[0] = 'forge'; main()"       # = the `forge` script
    return subprocess.run([sys.executable, "-c", entry, "--base-url", base_url, *args], cwd=cwd, env=env,
                          capture_output=True, text=True, check=False, timeout=240)


@pytest.fixture
def stranger_repo(tmp_path):
    dest = tmp_path / "stranger"
    done = subprocess.run(["bash", str(MAKE_REPO), str(dest)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    return dest


def test_solve_with_apply_fixes_the_checkout_and_leaves_tests_alone(live_server, stranger_repo, tmp_path):
    base_url, state = live_server
    state["turns"] = [tool_turn("bash", {"command": ISSUE1}), FIX_SHIPPING, tool_turn("bash", {"command": ISSUE1}),
                      tool_turn("finish", {"status": "done", "summary": "used > instead of >="})]
    assert _tests(stranger_repo, "tests.test_regressions.Issue1FreeShippingAtThreshold").returncode != 0

    done = _forge(base_url, "solve", "--repo", str(stranger_repo), "--goal", "Issue #1: free shipping at 50.00",
                  "--test", ISSUE1, "--apply", cwd=tmp_path)

    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-800:]
    assert "RESULT: SUCCEEDED" in done.stdout and "applied to" in done.stdout
    assert _tests(stranger_repo, "tests.test_regressions.Issue1FreeShippingAtThreshold").returncode == 0
    assert _git(stranger_repo, "diff", "--name-only").stdout.split() == ["shopkit/shipping.py"]


def test_solve_from_a_git_url_clones_then_fixes_the_clone_and_not_the_origin(live_server, stranger_repo, tmp_path):
    base_url, state = live_server
    state["turns"] = [tool_turn("bash", {"command": ISSUE1}), FIX_SHIPPING, tool_turn("bash", {"command": ISSUE1}),
                      tool_turn("finish", {"status": "done", "summary": "fixed"})]
    clones = tmp_path / "clones"
    done = _forge(base_url, "solve", "--repo", stranger_repo.as_uri(), "--goal", "Issue #1", "--test", ISSUE1,
                  "--apply", cwd=tmp_path, env_extra={"FORGE_CLONES_DIR": str(clones)})
    assert done.returncode == 0, done.stdout[-1500:] + done.stderr[-800:]
    cloned = list(clones.iterdir())
    assert len(cloned) == 1 and cloned[0].name.startswith("stranger-")
    assert _tests(cloned[0], "tests.test_regressions.Issue1FreeShippingAtThreshold").returncode == 0   # clone fixed
    assert _tests(stranger_repo, "tests.test_regressions.Issue1FreeShippingAtThreshold").returncode != 0  # origin intact


def test_an_unsolvable_task_ends_not_verified_with_the_agents_explanation_and_nothing_applied(
        live_server, stranger_repo, tmp_path):
    base_url, state = live_server
    state["turns"] = [tool_turn("bash", {"command": ISSUE1}),
                      tool_turn("finish", {"status": "cannot_fix", "summary": "the spec contradicts itself"})]
    before = _git(stranger_repo, "status", "--porcelain").stdout
    done = _forge(base_url, "solve", "--repo", str(stranger_repo), "--goal", "Issue #1", "--test", ISSUE1, "--apply",
                  cwd=tmp_path)
    assert done.returncode == 1
    assert "NOT verified" in done.stdout and "the spec contradicts itself" in done.stdout
    assert "applied to" not in done.stdout and _git(stranger_repo, "status", "--porcelain").stdout == before


def test_cli_and_server_agree_on_the_public_step_timeline(live_server, stranger_repo, tmp_path):
    """`forge run events` (strict DTOs) must parse the new structured step fields the server now emits."""
    base_url, state = live_server
    state["turns"] = [tool_turn("bash", {"command": ISSUE1}), FIX_SHIPPING, tool_turn("bash", {"command": ISSUE1}),
                      tool_turn("finish", {"status": "done", "summary": "ok"})]
    done = _forge(base_url, "solve", "--repo", str(stranger_repo), "--goal", "Issue #1", "--test", ISSUE1,
                  cwd=tmp_path)
    assert done.returncode == 0, done.stdout[-1500:]
    run_id = re.search(r"run\s+([0-9a-f-]{36})", done.stdout).group(1)
    events = _forge(base_url, "--json", "run", "events", run_id, cwd=tmp_path)
    assert events.returncode == 0, events.stderr
    assert '"tool":"bash"' in events.stdout.replace(" ", "") and "shopkit/shipping.py" in events.stdout
    shutil.rmtree(tmp_path / "data", ignore_errors=True)


def _forge_in_a_terminal(base_url: str, *args: str, cwd: Path) -> tuple[int, str]:
    """Run the CLI with a pseudo-terminal as stdout, i.e. what the owner sees in a real terminal window."""
    import os
    import pty
    master, slave = pty.openpty()
    env = {**os.environ, "COLUMNS": "100", "LINES": "50", "LC_ALL": "C.UTF-8", "PYTHONIOENCODING": "utf-8",
           "TERM": "xterm-256color"}
    env.pop("NO_COLOR", None)
    entry = "import sys; from forge_cli.main import main; sys.argv[0] = 'forge'; main()"
    proc = subprocess.Popen([sys.executable, "-c", entry, "--base-url", base_url, *args], cwd=cwd, env=env,
                            stdout=slave, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    os.close(slave)
    chunks = []
    while True:
        try:
            data = os.read(master, 65536)
        except OSError:                      # EIO: the child closed its side
            break
        if not data:
            break
        chunks.append(data)
    os.close(master)
    return proc.wait(timeout=60), b"".join(chunks).decode("utf-8", "replace")


def test_in_a_real_terminal_the_owner_sees_the_story(live_server, stranger_repo, tmp_path):
    base_url, state = live_server
    state["turns"] = [tool_turn("bash", {"command": ISSUE1}), FIX_SHIPPING, tool_turn("bash", {"command": ISSUE1}),
                      tool_turn("finish", {"status": "done", "summary": "used > instead of >="})]
    code, raw = _forge_in_a_terminal(base_url, "solve", "--repo", str(stranger_repo), "--goal",
                                     "Issue #1: free shipping at 50.00", "--test", ISSUE1, "--apply", cwd=tmp_path)
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", raw)
    Path(tmp_path / "terminal.txt").write_text(plain)
    assert code == 0, plain[-1500:]
    assert "\x1b[" in raw                                                     # coloured: it knew it was a terminal
    for expected in ("ForgeMini", "repository", "agent at work", "$ python -m unittest", "✎ edit  shopkit/shipping.py",
                     "verification", "Tests on the original code", "Test files untouched",
                     "+    if order_amount >= FREE_SHIPPING_THRESHOLD:", "VERIFIED", "applied to"):
        assert expected in plain, expected
    assert "NOT VERIFIED" not in plain
    assert "· finish" not in plain                                            # no duplicate of the claim line
