"""Tariff configuration (FORGE_PRICE_* in .env) and the report's cost status, without DB or Docker."""
from decimal import Decimal
from uuid import uuid4

import pytest

from app.api.sandbox_runs import _cost
from app.bootstrap import ForgeMiniSettings, model_pricing
from app.services.agent_cost import AgentPricing
from app.services.agent_usage import (
    AgentUsageSnapshot, ModelCallOutcome, ModelCallStage, ModelCallUsage, TokenUsage, UsageStatus,
)
from app.services.agent_usage_codec import AgentUsageRecord


def _settings(**values):
    return ForgeMiniSettings(_env_file=None, **values)


def test_no_tariff_means_usage_is_recorded_but_not_priced():
    assert model_pricing(_settings(forge_model_name="m")) == AgentPricing()
    assert model_pricing(_settings(forge_model_name="m", forge_price_input_usd_per_million="2")) == AgentPricing()


def test_one_tariff_prices_every_call_of_the_sandbox_loop():
    pricing = model_pricing(_settings(forge_model_name="gpt-x.1", forge_price_input_usd_per_million="2",
                                      forge_price_output_usd_per_million="8", forge_price_version="2026-11-22"))
    assert pricing.research is pricing.finalization
    assert (pricing.research.model, pricing.research.input_usd_per_million,
            pricing.research.cached_input_usd_per_million) == ("gpt-x.1", Decimal("2"), None)


@pytest.mark.parametrize("values,message", [
    ({"forge_price_input_usd_per_million": "2", "forge_price_output_usd_per_million": "8"}, "FORGE_MODEL_NAME"),
    ({"forge_model_name": "m", "forge_price_input_usd_per_million": "0.0000001",       # finer than 1e-6 per 1M
      "forge_price_output_usd_per_million": "8"}, "invalid_tariff_rate"),
])
def test_invalid_tariff_fails_at_startup_not_mid_run(values, message):
    with pytest.raises(ValueError, match=message):
        model_pricing(_settings(**values))


def _record(pricing):
    call = ModelCallUsage(1, ModelCallStage.RESEARCH, ModelCallOutcome.RETURNED, UsageStatus.REPORTED,
                          TokenUsage(1000, 100, 1100, cached_input_tokens=0))
    return AgentUsageRecord(AgentUsageSnapshot(uuid4(), (call,)), pricing)


def test_report_cost_status_distinguishes_unknown_from_free():
    priced = model_pricing(_settings(forge_model_name="m", forge_price_input_usd_per_million="2",
                                     forge_price_output_usd_per_million="8"))
    assert _cost(None) == {"known_cost_usd": None, "cost_status": "not_recorded"}       # e.g. run before wiring
    assert _cost(_record(AgentPricing())) == {"known_cost_usd": None, "cost_status": "not_priced"}
    estimated = _cost(_record(priced))
    assert estimated["cost_status"] == "estimated" and Decimal(estimated["known_cost_usd"]) == Decimal("0.0028")
