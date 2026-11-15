import re
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from app.services.agent_cost import AgentCostEstimate, AgentPricing, ModelTariff, estimate_agent_cost
from app.services.agent_usage import (
    AgentUsageSnapshot,
    ModelCallOutcome,
    ModelCallStage,
    ModelCallUsage,
    TokenUsage,
    UsageStatus,
)


USAGE_SCHEMA_VERSION = 1
AGENT_USAGE_RECORDED_EVENT = "agent_usage_recorded"
_PLAIN_RATE = re.compile(r"(?:0|[1-9][0-9]{0,6})(?:\.[0-9]{1,6})?")
_TOKEN_FIELDS = ("input_tokens", "output_tokens", "total_tokens", "cached_input_tokens", "reasoning_tokens")
_TARIFF_IDS = ("provider", "model", "tariff_version")
_TARIFF_RATES = ("input_usd_per_million", "output_usd_per_million", "cached_input_usd_per_million")


class AgentUsageContractError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AgentUsageRecord:
    snapshot: AgentUsageSnapshot
    pricing: AgentPricing

    def __post_init__(self) -> None:
        if (
            not isinstance(self.snapshot, AgentUsageSnapshot)
            or not self.snapshot.calls
            or not isinstance(self.pricing, AgentPricing)
        ):
            raise AgentUsageContractError("invalid_agent_usage")

    @property
    def cost_estimate(self) -> AgentCostEstimate:
        return estimate_agent_cost(self.snapshot, self.pricing)


def encode_agent_usage(record: AgentUsageRecord) -> dict:
    if not isinstance(record, AgentUsageRecord):
        raise AgentUsageContractError("invalid_agent_usage")
    return {
        "schema_version": USAGE_SCHEMA_VERSION,
        "calls": [{
            "call_number": call.call_number,
            "stage": call.stage.value,
            "outcome": call.outcome.value,
            "usage_status": call.usage_status.value,
            "tokens": None if call.tokens is None else {name: getattr(call.tokens, name) for name in _TOKEN_FIELDS},
        } for call in record.snapshot.calls],
        "pricing": {
            "research": _encode_tariff(record.pricing.research),
            "finalization": _encode_tariff(record.pricing.finalization),
        },
    }


def _encode_tariff(tariff: ModelTariff | None) -> dict | None:
    if tariff is None:
        return None
    fields = {name: getattr(tariff, name) for name in _TARIFF_IDS}
    for name in _TARIFF_RATES:
        rate = getattr(tariff, name)
        fields[name] = format(rate, "f") if rate is not None else None
    return fields


def decode_agent_usage(run_id: UUID, payload: object) -> AgentUsageRecord:
    document = _exact_object(payload, {"schema_version", "calls", "pricing"})
    version = document["schema_version"]
    if type(version) is not int:
        raise AgentUsageContractError("invalid_stored_usage")
    if version != USAGE_SCHEMA_VERSION:
        raise AgentUsageContractError("unsupported_usage_version")
    calls = document["calls"]
    if not isinstance(calls, list) or not calls:
        raise AgentUsageContractError("invalid_stored_usage")
    pricing = _exact_object(document["pricing"], {"research", "finalization"})
    try:
        snapshot = AgentUsageSnapshot(run_id, tuple(_decode_call(call) for call in calls))
        return AgentUsageRecord(snapshot, AgentPricing(
            research=_decode_tariff(pricing["research"]), finalization=_decode_tariff(pricing["finalization"]),
        ))
    except ValueError:
        raise AgentUsageContractError("invalid_stored_usage") from None


def _exact_object(payload: object, keys: set[str]) -> dict:
    if not isinstance(payload, dict) or payload.keys() != keys:
        raise AgentUsageContractError("invalid_stored_usage")
    return payload


def _decode_call(payload: object) -> ModelCallUsage:
    call = _exact_object(payload, {"call_number", "stage", "outcome", "usage_status", "tokens"})
    if any(type(call[name]) is not str for name in ("stage", "outcome", "usage_status")):
        raise AgentUsageContractError("invalid_stored_usage")
    tokens = call["tokens"]
    return ModelCallUsage(
        call["call_number"],
        ModelCallStage(call["stage"]),
        ModelCallOutcome(call["outcome"]),
        UsageStatus(call["usage_status"]),
        None if tokens is None else TokenUsage(**_exact_object(tokens, set(_TOKEN_FIELDS))),
    )


def _decode_tariff(payload: object) -> ModelTariff | None:
    if payload is None:
        return None
    tariff = _exact_object(payload, set(_TARIFF_IDS + _TARIFF_RATES))
    values = {name: tariff[name] for name in _TARIFF_IDS}
    for name in _TARIFF_RATES:
        value = tariff[name]
        if name == "cached_input_usd_per_million" and value is None:
            values[name] = None
        elif type(value) is str and _PLAIN_RATE.fullmatch(value):
            values[name] = Decimal(value)
        else:
            raise AgentUsageContractError("invalid_stored_usage")
    return ModelTariff(**values)
