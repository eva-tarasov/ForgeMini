import json

from pydantic import ValidationError
from pydantic_core import PydanticSerializationError

from app.services.agent_result import AgentFinalResult


RESULT_SCHEMA_VERSION = 1


class AgentResultContractError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def encode_agent_result(result: AgentFinalResult) -> dict:
    if not isinstance(result, AgentFinalResult):
        raise AgentResultContractError("invalid_agent_result")
    try:
        validated = AgentFinalResult.model_validate_json(result.model_dump_json(warnings="error"), strict=True)
    except (ValidationError, PydanticSerializationError):
        raise AgentResultContractError("invalid_agent_result") from None
    return {"schema_version": RESULT_SCHEMA_VERSION, "result": validated.model_dump(mode="json")}


def decode_agent_result(payload: object) -> AgentFinalResult:
    if not isinstance(payload, dict) or payload.keys() != {"schema_version", "result"}:
        raise AgentResultContractError("invalid_stored_result")
    version = payload["schema_version"]
    if type(version) is not int:
        raise AgentResultContractError("invalid_stored_result")
    if version != RESULT_SCHEMA_VERSION:
        raise AgentResultContractError("unsupported_result_version")
    result = payload["result"]
    if not isinstance(result, dict):
        raise AgentResultContractError("invalid_stored_result")
    try:
        return AgentFinalResult.model_validate_json(json.dumps(result, allow_nan=False), strict=True)
    except (TypeError, ValueError, RecursionError):
        raise AgentResultContractError("invalid_stored_result") from None
