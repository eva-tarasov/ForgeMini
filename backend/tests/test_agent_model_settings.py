import os

import pytest
from pydantic import SecretStr, ValidationError

from app.agent_runtime.settings import ModelSettings
from app.core.settings import ROOT_ENV


@pytest.fixture(autouse=True)
def clear_model_environment(monkeypatch):
    fields = {name.upper() for name in ModelSettings.model_fields}
    for name in tuple(os.environ):
        if name.upper() in fields:
            monkeypatch.delenv(name)


def make_settings(**overrides):
    values = {"openai_api_key": "synthetic-key", "forge_model_name": "synthetic-model"}
    values.update(overrides)
    return ModelSettings(_env_file=None, **values)


def test_model_settings_loads_independently_from_database_settings(tmp_path, monkeypatch):
    assert ModelSettings.model_config["env_file"] == ROOT_ENV
    env_file = tmp_path / ".env"
    env_file.write_text(
        "OPENAI_API_KEY=synthetic-key\nFORGE_MODEL_NAME=synthetic-model\nPOSTGRES_USER=ignored\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(ModelSettings.model_config, "env_file", env_file)

    settings = ModelSettings()

    assert isinstance(settings.openai_api_key, SecretStr)
    assert settings.openai_api_key.get_secret_value() == "synthetic-key"
    assert settings.forge_model_name == "synthetic-model"
    assert settings.forge_model_timeout_seconds == 15.0


def test_model_settings_requires_key_and_name():
    with pytest.raises(ValidationError) as exc_info:
        ModelSettings(_env_file=None)

    assert {error["loc"] for error in exc_info.value.errors(include_input=False)} == {
        ("openai_api_key",), ("forge_model_name",)
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("openai_api_key", ""),
        ("openai_api_key", " \t"),
        ("forge_model_name", ""),
        ("forge_model_name", " \t"),
        ("forge_model_timeout_seconds", 0),
        ("forge_model_timeout_seconds", -1),
        ("forge_model_timeout_seconds", "invalid"),
        ("forge_model_timeout_seconds", float("inf")),
    ],
)
def test_model_settings_rejects_invalid_values(field, value):
    with pytest.raises(ValidationError) as exc_info:
        make_settings(**{field: value})

    assert exc_info.value.errors(include_input=False)[0]["loc"] == (field,)


def test_model_settings_masks_key_and_hides_validation_input():
    assert "synthetic-key" not in repr(make_settings())
    with pytest.raises(ValidationError) as exc_info:
        make_settings(openai_api_key={"key": "invalid-secret-input"})

    assert "invalid-secret-input" not in str(exc_info.value)
