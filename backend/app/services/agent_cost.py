import re
from dataclasses import dataclass
from decimal import Context, Decimal, Inexact, ROUND_HALF_EVEN, localcontext
from enum import StrEnum

from app.services.agent_usage import AgentUsageSnapshot, ModelCallStage, ModelCallUsage


_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@+\-]{0,127}")
_MILLION = Decimal("1000000")
_MAX_TOKENS = 10**12
_MAX_CALL_COST = Decimal("2000000000000")
# Covers the largest per-call amount and a platform-sized tuple of such calls
# with all 12 fractional places intact.
_COST_CONTEXT = Context(prec=48, rounding=ROUND_HALF_EVEN, Emin=-999, Emax=999, clamp=0, traps=[Inexact])


@dataclass(frozen=True, slots=True)
class ModelTariff:
    provider: str
    model: str
    tariff_version: str
    input_usd_per_million: Decimal
    output_usd_per_million: Decimal
    cached_input_usd_per_million: Decimal | None = None

    def __post_init__(self) -> None:
        if any(
            type(value) is not str or _IDENTIFIER.fullmatch(value) is None
            for value in (self.provider, self.model, self.tariff_version)
        ):
            raise ValueError("invalid_tariff_identifier")
        rates = (self.input_usd_per_million, self.output_usd_per_million)
        if self.cached_input_usd_per_million is not None:
            rates += (self.cached_input_usd_per_million,)
        if any(
            not isinstance(rate, Decimal) or not rate.is_finite() or rate.is_signed()
            or not -6 <= rate.as_tuple().exponent <= 0 or rate > _MILLION
            for rate in rates
        ):
            raise ValueError("invalid_tariff_rate")


@dataclass(frozen=True, slots=True)
class AgentPricing:
    research: ModelTariff | None = None
    finalization: ModelTariff | None = None

    def __post_init__(self) -> None:
        if any(tariff is not None and not isinstance(tariff, ModelTariff) for tariff in (self.research, self.finalization)):
            raise ValueError("invalid_agent_pricing")


class CostStatus(StrEnum):
    ESTIMATED = "estimated"
    USAGE_UNKNOWN = "usage_unknown"
    TARIFF_MISSING = "tariff_missing"
    CACHE_PRICING_UNKNOWN = "cache_pricing_unknown"
    USAGE_OUT_OF_RANGE = "usage_out_of_range"


@dataclass(frozen=True, slots=True)
class ModelCallCost:
    call_number: int
    status: CostStatus
    estimated_usd: Decimal | None

    def __post_init__(self) -> None:
        if type(self.call_number) is not int or self.call_number < 1 or not isinstance(self.status, CostStatus):
            raise ValueError("invalid_call_cost")
        if self.status is CostStatus.ESTIMATED:
            value = self.estimated_usd
            if (
                not isinstance(value, Decimal) or not value.is_finite()
                or not 0 <= value <= _MAX_CALL_COST or value.as_tuple().exponent < -12
            ):
                raise ValueError("invalid_call_cost")
        elif self.estimated_usd is not None:
            raise ValueError("invalid_call_cost")


@dataclass(frozen=True, slots=True)
class AgentCostEstimate:
    calls: tuple[ModelCallCost, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.calls, tuple) or any(
            not isinstance(call, ModelCallCost) or call.call_number != number
            for number, call in enumerate(self.calls, 1)
        ):
            raise ValueError("invalid_cost_estimate")

    @property
    def priced_calls(self) -> int:
        return sum(call.status is CostStatus.ESTIMATED for call in self.calls)

    @property
    def unknown_cost_calls(self) -> int:
        return len(self.calls) - self.priced_calls

    @property
    def known_cost_usd(self) -> Decimal:
        with localcontext(_COST_CONTEXT):
            return sum((call.estimated_usd for call in self.calls if call.estimated_usd is not None), Decimal(0))

    @property
    def cost_complete(self) -> bool:
        return bool(self.calls) and self.unknown_cost_calls == 0


def estimate_agent_cost(snapshot: AgentUsageSnapshot, pricing: AgentPricing) -> AgentCostEstimate:
    if not isinstance(snapshot, AgentUsageSnapshot) or not isinstance(pricing, AgentPricing):
        raise ValueError("invalid_cost_input")
    with localcontext(_COST_CONTEXT):
        return AgentCostEstimate(tuple(
            _estimate_call(call, pricing.research if call.stage is ModelCallStage.RESEARCH else pricing.finalization)
            for call in snapshot.calls
        ))


def _estimate_call(call: ModelCallUsage, tariff: ModelTariff | None) -> ModelCallCost:
    tokens = call.tokens  # The usage contract only provides tokens on a reported call.
    if tokens is None:
        return ModelCallCost(call.call_number, CostStatus.USAGE_UNKNOWN, None)
    if tariff is None:
        return ModelCallCost(call.call_number, CostStatus.TARIFF_MISSING, None)
    if tokens.input_tokens > _MAX_TOKENS or tokens.output_tokens > _MAX_TOKENS:
        return ModelCallCost(call.call_number, CostStatus.USAGE_OUT_OF_RANGE, None)

    cached = tokens.cached_input_tokens
    cached_rate = tariff.cached_input_usd_per_million
    if cached is None:
        if tokens.input_tokens and cached_rate != tariff.input_usd_per_million:
            return ModelCallCost(call.call_number, CostStatus.CACHE_PRICING_UNKNOWN, None)
        cached = 0  # The breakdown cannot affect the price when both rates are equal or input is zero.
    if cached and cached_rate is None:
        return ModelCallCost(call.call_number, CostStatus.CACHE_PRICING_UNKNOWN, None)

    input_cost = (tokens.input_tokens - cached) * tariff.input_usd_per_million
    if cached_rate is not None:
        input_cost += cached * cached_rate
    output_cost = tokens.output_tokens * tariff.output_usd_per_million
    return ModelCallCost(call.call_number, CostStatus.ESTIMATED, (input_cost + output_cost) / _MILLION)
