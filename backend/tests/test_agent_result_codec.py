from copy import deepcopy

import pytest

from app.services.agent_result import AgentFinalResult, FixtureObservation
from app.services.agent_result_codec import AgentResultContractError, decode_agent_result, encode_agent_result


@pytest.fixture
def ready_result():
    return AgentFinalResult(
        summary="Проверка результата",
        status="ready_for_verification",
        relevant_components=("② сервис", "① репозиторий"),
        assumptions=("β", "α"),
        observations=(
            FixtureObservation(component="task_service", tool_call_id="second", content="Синтетическое наблюдение"),
            FixtureObservation(component="agent_run_service", tool_call_id="first", content="Другое наблюдение"),
        ),
        blocked_reason=None,
    )


@pytest.mark.parametrize("blocker", [None, "Нужны данные"])
def test_agent_result_codec_round_trip(ready_result, blocker):
    result = ready_result if blocker is None else ready_result.model_copy(
        update={"status": "blocked", "blocked_reason": blocker}
    )
    original = result.model_dump()
    document = encode_agent_result(result)
    assert document == {
        "schema_version": 1,
        "result": {
            "summary": "Проверка результата",
            "status": "ready_for_verification" if blocker is None else "blocked",
            "relevant_components": ["② сервис", "① репозиторий"],
            "assumptions": ["β", "α"],
            "observations": [
                {"source": "synthetic_fixture", "tool_name": "read_fixture", "component": "task_service",
                 "tool_call_id": "second", "content": "Синтетическое наблюдение"},
                {"source": "synthetic_fixture", "tool_name": "read_fixture", "component": "agent_run_service",
                 "tool_call_id": "first", "content": "Другое наблюдение"},
            ],
            "blocked_reason": blocker,
        },
    }
    stored = deepcopy(document)
    decoded = decode_agent_result(document)
    assert decoded == result and type(decoded) is AgentFinalResult
    assert all(type(field) is tuple for field in (decoded.relevant_components, decoded.assumptions, decoded.observations))
    assert all(type(observation) is FixtureObservation for observation in decoded.observations)
    assert document == stored
    assert result.model_dump() == original


@pytest.mark.parametrize("invalid", [
    object(),
    AgentFinalResult.model_construct(summary="synthetic"),
])
def test_agent_result_codec_rejects_wrong_type_or_incomplete_model(invalid):
    with pytest.raises(AgentResultContractError, match="^invalid_agent_result$") as exc:
        encode_agent_result(invalid)
    assert exc.value.reason == "invalid_agent_result"


@pytest.mark.parametrize("updates", [
    {"summary": "  "},
    {"summary": 42},
    {"summary": object()},
    {"status": "unsupported"},
    {"status": "blocked"},
    {"blocked_reason": "synthetic sensitive field"},
    {"relevant_components": (" ",)},
    {"observations": (FixtureObservation.model_construct(
        component="task_service", tool_call_id="fixture", content=" ",
    ),)},
])
def test_agent_result_codec_revalidates_constructed_fields(ready_result, updates):
    with pytest.raises(AgentResultContractError, match="^invalid_agent_result$") as exc:
        encode_agent_result(ready_result.model_copy(update=updates))
    assert exc.value.reason == "invalid_agent_result"


@pytest.mark.parametrize("version", [True, 1.0, "1", None, 2, -1])
def test_agent_result_codec_rejects_invalid_versions(ready_result, version):
    document = encode_agent_result(ready_result)
    document["schema_version"] = version
    reason = "unsupported_result_version" if type(version) is int else "invalid_stored_result"
    with pytest.raises(AgentResultContractError, match=f"^{reason}$") as exc:
        decode_agent_result(document)
    assert exc.value.reason == reason


@pytest.mark.parametrize("payload", [None, [], "{}", {}, {"schema_version": 1}, {"result": {}},
                                    {"schema_version": 1, "result": {}, "extra": 0}])
def test_agent_result_codec_rejects_invalid_envelopes(payload):
    with pytest.raises(AgentResultContractError, match="^invalid_stored_result$") as exc:
        decode_agent_result(payload)
    assert exc.value.reason == "invalid_stored_result"


@pytest.mark.parametrize("body", [None, [], "{}", False])
def test_agent_result_codec_requires_object_body(body):
    with pytest.raises(AgentResultContractError, match="^invalid_stored_result$"):
        decode_agent_result({"schema_version": 1, "result": body})


@pytest.mark.parametrize("updates", [
    {"summary": 9}, {"summary": "  "}, {"summary": object()}, {"assumptions": [" "]},
    {"relevant_components": "service"}, {"blocked_reason": "synthetic sensitive field"},
    {"status": "blocked"}, {"status": "blocked", "blocked_reason": " "}, {"extra": "synthetic"},
    {"observations": [{"component": "task_service", "tool_call_id": "fixture", "content": ""}]},
    {"observations": [{"component": "task_service", "tool_call_id": "fixture", "content": "x", "extra": "field"}]},
    {"observations": [{"component": "task_service", "tool_call_id": "fixture"}]},
])
def test_agent_result_codec_rejects_corrupt_fields(ready_result, updates):
    document = encode_agent_result(ready_result)
    document["result"].update(updates)
    with pytest.raises(AgentResultContractError, match="^invalid_stored_result$") as exc:
        decode_agent_result(document)
    assert exc.value.reason == "invalid_stored_result"


def test_agent_result_codec_rejects_missing_result_field(ready_result):
    document = encode_agent_result(ready_result)
    del document["result"]["blocked_reason"]
    with pytest.raises(AgentResultContractError, match="^invalid_stored_result$"):
        decode_agent_result(document)
