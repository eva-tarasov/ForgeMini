"""Where a run's results live on disk: root/<run_id>/{changeset.json, patch.diff, transcript.json, outcome.json}.

changeset.json is the machine truth the Verifier re-applies; patch.diff is for humans.
The DB stores only small facts (status, evidence); blobs stay in files.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from uuid import UUID

from app.sandbox.agent_loop import LoopOutcome
from app.sandbox.changeset import Changeset, Snapshot


class FileArtifactStore:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    def _dir(self, run_id: UUID) -> Path:
        return self._root / str(run_id)

    def save(self, run_id: UUID, *, base: Snapshot, changeset: Changeset, outcome: LoopOutcome) -> None:
        d = self._dir(run_id)
        d.mkdir(parents=True, exist_ok=True)
        (d / "changeset.json").write_text(changeset.to_json())
        (d / "patch.diff").write_text(changeset.unified_diff(base))
        (d / "transcript.json").write_text(json.dumps(outcome.transcript, indent=1, ensure_ascii=False))
        summary = {k: v for k, v in asdict(outcome).items() if k != "transcript"}
        summary["changeset_sha256"] = changeset.sha256()
        summary["base_sha256"] = base.sha256()
        summary["changed_paths"] = list(changeset.paths)
        (d / "outcome.json").write_text(json.dumps(summary, indent=1, ensure_ascii=False))

    def load_changeset(self, run_id: UUID) -> Changeset:
        return Changeset.from_json((self._dir(run_id) / "changeset.json").read_text())

    def load_outcome(self, run_id: UUID) -> dict:
        return json.loads((self._dir(run_id) / "outcome.json").read_text())

    def has_run(self, run_id: UUID) -> bool:
        return (self._dir(run_id) / "outcome.json").is_file()

    def patch_text(self, run_id: UUID) -> str:
        return (self._dir(run_id) / "patch.diff").read_text()
