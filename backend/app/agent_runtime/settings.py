from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.settings import ROOT_ENV, NonBlankString


class ModelSettings(BaseSettings):
    """Model configuration loaded when a real client is needed."""

    model_config = SettingsConfigDict(
        env_file=ROOT_ENV,
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    openai_api_key: SecretStr
    forge_model_name: NonBlankString
    forge_model_timeout_seconds: float = Field(default=15.0, gt=0, allow_inf_nan=False)

    @field_validator("openai_api_key")
    @classmethod
    def api_key_must_not_be_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("API key must not be blank")
        return value
