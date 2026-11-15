from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, ROUND_DOWN, localcontext
from uuid import uuid4

import pytest

from app.services.agent_cost import AgentCostEstimate, AgentPricing, CostStatus, ModelCallCost, ModelTariff, estimate_agent_cost
from app.services.agent_usage import AgentUsageSnapshot, ModelCallOutcome, ModelCallStage, ModelCallUsage, TokenUsage, UsageStatus


TARIFF = ModelTariff("synthetic", "training-model", "v1", Decimal("2.00"), Decimal("8.00"), Decimal("0.50"))


def _reported(number, tokens, stage=ModelCallStage.RESEARCH):
    return ModelCallUsage(number, stage, ModelCallOutcome.RETURNED, UsageStatus.REPORTED, tokens)


def _estimate(*calls, pricing=AgentPricing(research=TARIFF)):
    return estimate_agent_cost(AgentUsageSnapshot(uuid4(), calls), pricing)


def test_agent_cost_uses_exact_decimal_pricing():
    result = _estimate(
        _reported(1, TokenUsage(12, 8, 20, 4, 2)),
        _reported(2, TokenUsage(24, 10, 34, 7, 3)),
        _reported(3, TokenUsage(35, 11, 46, 10, 9), ModelCallStage.FINALIZATION),
        pricing=AgentPricing(TARIFF, TARIFF),
    )
    assert result.calls == tuple(
        ModelCallCost(number, CostStatus.ESTIMATED, Decimal(value))
        for number, value in enumerate(("0.000082", "0.0001175", "0.000143"), 1)
    )
    assert result.known_cost_usd == Decimal("0.00034250")
    assert result.cost_complete and result.priced_calls == 3 and result.unknown_cost_calls == 0


def test_cost_uses_stage_tariff_and_ignores_reasoning_as_separate_charge():
    tokens = TokenUsage(5, 6, 11, 0, 6)
    result = _estimate(
        _reported(1, tokens), _reported(2, tokens, ModelCallStage.FINALIZATION),
        pricing=AgentPricing(TARIFF, replace(TARIFF, output_usd_per_million=Decimal("1"))),
    )
    assert [call.estimated_usd for call in result.calls] == [Decimal("0.000058"), Decimal("0.000016")]


@pytest.mark.parametrize("tokens,cached_rate,expected", [
    (TokenUsage(1, 1, 2), None, None),
    (TokenUsage(1, 1, 2), Decimal("1"), None),
    (TokenUsage(1, 1, 2), Decimal("2"), Decimal("0.000010")),
    (TokenUsage(1, 1, 2, 0), None, Decimal("0.000010")),
    (TokenUsage(1, 1, 2, 1), None, None),
    (TokenUsage(1, 1, 2, 1), Decimal("0"), Decimal("0.000008")),
    (TokenUsage(0, 1, 1), None, Decimal("0.000008")),
    (TokenUsage(0, 0, 0), None, Decimal(0)),
])
def test_cost_cache_uncertainty_and_zero_usage(tokens, cached_rate, expected):
    result = _estimate(_reported(1, tokens), pricing=AgentPricing(replace(TARIFF, cached_input_usd_per_million=cached_rate)))
    status = CostStatus.CACHE_PRICING_UNKNOWN if expected is None else CostStatus.ESTIMATED
    assert result.calls == (ModelCallCost(1, status, expected),)
    assert result.cost_complete is (expected is not None)


def test_cost_partial_subtotal_and_empty_or_all_unknown():
    unknown = tuple(
        ModelCallUsage(number, ModelCallStage.RESEARCH, outcome, status, None)
        for number, (outcome, status) in enumerate((
            (ModelCallOutcome.RETURNED, UsageStatus.INVALID), (ModelCallOutcome.RETURNED, UsageStatus.MISSING),
            (ModelCallOutcome.STARTED, UsageStatus.MISSING), (ModelCallOutcome.ERROR, UsageStatus.MISSING),
            (ModelCallOutcome.CANCELLED, UsageStatus.MISSING),
        ), 1)
    )
    for calls in ((), unknown):
        result = _estimate(*calls)
        assert not result.cost_complete and result.priced_calls == result.known_cost_usd == 0
        assert result.unknown_cost_calls == len(calls)
        assert all(call.status is CostStatus.USAGE_UNKNOWN and call.estimated_usd is None for call in result.calls)
    subtotal = _estimate(
        *unknown, _reported(6, TokenUsage(0, 1, 1)),
        _reported(7, TokenUsage(0, 0, 0), ModelCallStage.FINALIZATION),
        _reported(8, TokenUsage(2, 1, 3)),
    )
    assert [call.status for call in subtotal.calls[5:]] == [
        CostStatus.ESTIMATED, CostStatus.TARIFF_MISSING, CostStatus.CACHE_PRICING_UNKNOWN,
    ]
    assert subtotal.priced_calls == 1 and subtotal.unknown_cost_calls == 7
    assert not subtotal.cost_complete and subtotal.known_cost_usd == Decimal("0.000008")


@pytest.mark.parametrize("tokens", [TokenUsage(10**12 + 1, 0, 10**12 + 1), TokenUsage(0, 10**12 + 1, 10**12 + 1)])
def test_cost_out_of_range_preserves_token_record(tokens):
    snapshot = AgentUsageSnapshot(uuid4(), (_reported(1, tokens),))
    result = estimate_agent_cost(snapshot, AgentPricing(TARIFF))
    assert snapshot.calls[0].tokens is tokens
    assert result.calls == (ModelCallCost(1, CostStatus.USAGE_OUT_OF_RANGE, None),)


def test_cost_does_not_depend_on_ambient_decimal_context():
    tariff = replace(TARIFF, input_usd_per_million=Decimal("999999.999999"), output_usd_per_million=Decimal("0.000001"))
    with localcontext() as context:
        context.prec = 2
        context.Emax = 3
        context.Emin = -3
        context.rounding = ROUND_DOWN
        context.traps[Inexact] = True
        result = _estimate(
            _reported(1, TokenUsage(10**12, 1, 10**12 + 1, 0)),
            _reported(2, TokenUsage(0, 1, 1)),
            pricing=AgentPricing(tariff),
        )
        assert result.calls[0].estimated_usd == Decimal("999999999999.000000000001")
        assert result.known_cost_usd == Decimal("999999999999.000000000002")
        assert context.prec == 2 and context.rounding == ROUND_DOWN


@pytest.mark.parametrize("identifier", ["", " ", "name with spaces", "name\n", "x" * 129, "-leading", None, 1])
def test_tariff_rejects_invalid_identifiers(identifier):
    for field in ("provider", "model", "tariff_version"):
        with pytest.raises(ValueError, match="^invalid_tariff_identifier$"):
            replace(TARIFF, **{field: identifier})


@pytest.mark.parametrize("rate", [
    0, 0.5, True, "0.5", Decimal("NaN"), Decimal("sNaN"), Decimal("Infinity"), Decimal("-1"),
    Decimal("1000000.000001"), Decimal("1.0000000"), Decimal("1e3"), Decimal("-0"),
])
def test_tariff_rejects_invalid_rates(rate):
    for field in ("input_usd_per_million", "output_usd_per_million", "cached_input_usd_per_million"):
        with pytest.raises(ValueError, match="^invalid_tariff_rate$"):
            replace(TARIFF, **{field: rate})


def test_pricing_and_cost_records_enforce_immutable_consistent_values():
    tariff = replace(TARIFF, model="org/my-model@v1", input_usd_per_million=Decimal("1000000.000000"))
    pricing = AgentPricing(research=tariff)
    with pytest.raises(ValueError, match="^invalid_agent_pricing$"):
        AgentPricing(finalization={})
    with pytest.raises(FrozenInstanceError):
        tariff.model = "other"
    with pytest.raises(FrozenInstanceError):
        pricing.research = None
    with pytest.raises(ValueError, match="^invalid_call_cost$"):
        ModelCallCost(1, CostStatus.ESTIMATED, None)
    with pytest.raises(ValueError, match="^invalid_call_cost$"):
        ModelCallCost(1, CostStatus.TARIFF_MISSING, Decimal(0))
    for impossible_cost in (Decimal("2.1E+12"), Decimal("0.0000000000001")):
        with pytest.raises(ValueError, match="^invalid_call_cost$"):
            ModelCallCost(1, CostStatus.ESTIMATED, impossible_cost)
    with pytest.raises(ValueError, match="^invalid_cost_estimate$"):
        AgentCostEstimate((ModelCallCost(2, CostStatus.USAGE_UNKNOWN, None),))
