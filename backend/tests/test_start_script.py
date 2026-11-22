"""scripts/start.sh: the PostgreSQL step must name the real cause of a failure.

Found in the "outsider" run on a fresh clone: without access to Docker Hub, `docker compose up` failed to
DOWNLOAD postgres:17, but start.sh swallowed the output and said "PostgreSQL did not become healthy" and
pointed at `docker compose logs`, which is empty because no container was ever created.

The tests run the real script against a fake `docker` on PATH, so no Docker or database is needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _run_start(tmp_path: Path, compose_stderr: str) -> subprocess.CompletedProcess[str]:
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    shutil.copy(REPO / "scripts" / "start.sh", root / "scripts" / "start.sh")
    # start.sh imports app.bootstrap from backend/: point it at the real backend.
    (root / "backend").symlink_to(REPO / "backend")
    (root / ".env").write_text("POSTGRES_PASSWORD=test-not-a-real-password\n", encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_docker = bin_dir / "docker"
    # `docker info` succeeds (daemon "running"); `docker compose up` fails with the given message.
    fake_docker.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  info) exit 0 ;;\n"
        f"  compose) printf '%s\\n' '{compose_stderr}' >&2; exit 1 ;;\n"
        "esac\n"
        "exit 1\n",
        encoding="utf-8",
    )
    fake_docker.chmod(0o755)

    env = {k: v for k, v in os.environ.items() if not k.startswith(("POSTGRES_", "OPENAI_", "FORGE_"))}
    env.update(PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', '')}", VIRTUAL_ENV=sys.prefix)
    env.pop("SKIP_COMPOSE", None)
    return subprocess.run(
        ["bash", str(root / "scripts" / "start.sh")],
        env=env, capture_output=True, text=True, timeout=120,
    )


def test_image_download_failure_is_named_as_such(tmp_path: Path) -> None:
    done = _run_start(
        tmp_path,
        'Error response from daemon: failed to resolve reference "docker.io/library/postgres:17": no such host',
    )
    assert done.returncode == 1
    assert "Cannot download the PostgreSQL image" in done.stderr
    assert "resolve reference" in done.stderr          # the real error is shown, not swallowed
    assert "did not become healthy" not in done.stderr


def test_unhealthy_container_keeps_the_logs_hint(tmp_path: Path) -> None:
    done = _run_start(tmp_path, "dependency failed to start: container forgemini-postgres-1 is unhealthy")
    assert done.returncode == 1
    assert "PostgreSQL did not become healthy" in done.stderr
    assert "docker compose logs postgres" in done.stderr
    assert "Cannot download" not in done.stderr
