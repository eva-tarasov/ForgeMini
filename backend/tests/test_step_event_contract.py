"""Contract: every step the runner can write must be readable through the public events API.

Live-run finding (2026-11-24, `forge solve --setup`): the runner wrote `step: "setup"` events (PR #3), but the
public schema's `Literal` did not list "setup", so GET /agent-runs/{id}/events answered HTTP 500 and the CLI lost the
run. The tests of PR #3 checked the writer (runner emits) and the reader (CLI renders) separately, each against its
own idea of the format; nothing checked writer -> public schema. This test does, for every kind the writer knows.
"""
import pytest

from app.sandbox import events
from app.schemas.agent_run import RunStepDetails

SAMPLE_DATA = {
    "model_response": {"step": 1, "tool_calls": ["bash"], "text": "x"},
    "tool_result": {"turn": 1, "name": "bash", "args": {"command": "ls"}, "tool_input": {"command": "ls"},
                    "result": "exit code: 0\nok", "is_error": False},
    "finish": {"status": "claimed_done", "summary": "done"},
    "budget_exhausted": {"steps": 25},
    "error": {"detail": "setup failed (exit code 1): …"},
    "setup": {"command": "pip install -r requirements.txt", "state": "started"},
}


def test_every_known_step_kind_has_a_sample():
    assert set(SAMPLE_DATA) == set(events._STEP_KINDS)


@pytest.mark.parametrize("kind", sorted(SAMPLE_DATA))
def test_what_the_runner_writes_the_public_api_can_read(kind):
    payload = events.step_payload(kind, SAMPLE_DATA[kind])
    details = RunStepDetails.model_validate({k: v for k, v in payload.items() if k != "schema_version"})
    assert details.step == kind


def test_an_unknown_kind_is_written_as_other_and_still_readable():
    payload = events.step_payload("something_new", {"steps": 1})
    assert RunStepDetails.model_validate({k: v for k, v in payload.items() if k != "schema_version"}).step == "other"
