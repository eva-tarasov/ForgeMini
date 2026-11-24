"""Model provider presets (sandbox/model_provider.py) and how bootstrap turns them into a chat model."""
import pytest

from app.bootstrap import ForgeMiniSettings, build_chat_model, model_connection, model_pricing
from app.sandbox.model_provider import PRESETS, ModelConfigError, resolve_connection


def conn(provider, model="m", base_url=None, api_key=None, **keys):
    return resolve_connection(provider=provider, model=model, base_url=base_url, api_key=api_key, provider_keys=keys)


@pytest.mark.parametrize("provider,url", [
    ("moonshot", "https://api.moonshot.ai/v1"), ("zai", "https://api.z.ai/api/paas/v4"),
    ("ollama", "http://localhost:11434/v1"),
])
def test_presets_give_each_provider_its_default_url(provider, url):
    c = conn(provider, MOONSHOT_API_KEY="k1", ZAI_API_KEY="k2")
    assert c.base_url == url and c.chat_completions_only


def test_openai_keeps_the_sdk_default_url_and_may_use_the_responses_api():
    c = conn("openai", OPENAI_API_KEY="sk-x")
    assert c.base_url is None and not c.chat_completions_only and c.api_key == "sk-x"


def test_each_provider_reads_its_own_key_and_never_another_providers_key():
    # the real risk: an OpenAI key silently sent to a third-party endpoint
    with pytest.raises(ModelConfigError, match="MOONSHOT_API_KEY"):
        conn("moonshot", OPENAI_API_KEY="sk-openai")
    assert conn("moonshot", OPENAI_API_KEY="sk-openai", MOONSHOT_API_KEY="mk").api_key == "mk"
    assert conn("zai", ZAI_API_KEY="zk").api_key == "zk"


def test_explicit_override_key_wins():
    assert conn("zai", api_key="override", ZAI_API_KEY="zk").api_key == "override"


def test_ollama_needs_no_key_but_the_sdk_gets_a_placeholder():
    c = conn("ollama")
    assert c.api_key == "ollama" and c.local


def test_openai_compatible_requires_a_base_url_and_custom_urls_are_validated():
    with pytest.raises(ModelConfigError, match="FORGE_MODEL_BASE_URL"):
        conn("openai_compatible")
    assert conn("openai_compatible", base_url="http://127.0.0.1:1234/v1").base_url == "http://127.0.0.1:1234/v1"
    with pytest.raises(ModelConfigError, match="not an http"):
        conn("ollama", base_url="ftp://x")
    assert conn("ollama", base_url="http://gpu-box:11434/v1").base_url == "http://gpu-box:11434/v1"


def test_unknown_provider_and_missing_model_name_are_explained():
    with pytest.raises(ModelConfigError, match="not one of"):
        conn("anthropic")
    with pytest.raises(ModelConfigError, match="FORGE_MODEL_NAME"):
        conn("ollama", model=None)


def test_the_key_never_appears_in_repr_or_in_what_the_server_reports():
    c = conn("openai_compatible", base_url="https://user:pw@host.example/v1", api_key="secret-key")
    assert "secret-key" not in repr(c)
    assert c.public() == {"provider": "openai_compatible", "model": "m", "base_url": "https://host.example/v1"}


def test_every_preset_is_documented_in_the_env_example():
    from pathlib import Path
    example = (Path(__file__).resolve().parents[2] / ".env.example").read_text()
    for name, preset in PRESETS.items():
        assert name in example
        if preset.key_env:
            assert preset.key_env in example


# ---- bootstrap -------------------------------------------------------------------------------------------------
def settings(**values):
    return ForgeMiniSettings(_env_file=None, **values)


def test_chat_model_for_a_compatible_provider_pins_chat_completions_and_its_url():
    model = build_chat_model(model_connection(settings(forge_model_provider="ollama", forge_model_name="qwen")),
                             timeout=30)
    assert model.openai_api_base == "http://localhost:11434/v1"
    assert model.use_responses_api is False
    assert model.model_name == "qwen"


def test_chat_model_for_openai_is_unchanged():
    model = build_chat_model(model_connection(settings(openai_api_key="sk-x", forge_model_name="gpt-x")), timeout=30)
    assert model.openai_api_base is None and model.use_responses_api is None


def test_local_ollama_costs_zero_explicitly_and_cloud_without_prices_stays_unpriced():
    local = model_pricing(settings(forge_model_provider="ollama", forge_model_name="qwen"))
    assert local.research is not None and local.research.input_usd_per_million == 0
    assert local.research.provider == "ollama" and "local" in local.research.tariff_version
    cloud = model_pricing(settings(forge_model_provider="moonshot", forge_model_name="kimi"))
    assert cloud.research is None                                   # "not priced", never a made-up $0


def test_tariff_names_the_configured_provider():
    p = model_pricing(settings(forge_model_provider="zai", forge_model_name="glm",
                               forge_price_input_usd_per_million="1", forge_price_output_usd_per_million="3"))
    assert p.research.provider == "zai"


def test_model_endpoint_reports_the_model_without_the_key_or_explains_what_is_missing():
    from fastapi.testclient import TestClient

    from app.bootstrap import create_forge_app
    from app.core.settings import Settings
    db = Settings(_env_file=None, postgres_user="u", postgres_db="d", postgres_password="unused-in-this-test")
    ok = create_forge_app(settings(forge_model_provider="zai", forge_model_name="glm-x", zai_api_key="zk-secret"),
                          db_settings=db)
    with TestClient(ok) as client:
        body = client.get("/model").json()
    assert body == {"configured": True, "provider": "zai", "model": "glm-x", "base_url": "https://api.z.ai/api/paas/v4"}
    assert "zk-secret" not in str(body)
    broken = create_forge_app(settings(forge_model_provider="moonshot", forge_model_name="kimi"), db_settings=db)
    with TestClient(broken) as client:
        body = client.get("/model").json()
    assert body["configured"] is False and "MOONSHOT_API_KEY" in body["error"]
