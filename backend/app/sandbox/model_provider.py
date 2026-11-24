"""Which model the agent talks to: OpenAI, or any server that speaks the same HTTP protocol.

"OpenAI-compatible" means the same wire format: `POST {base_url}/chat/completions`, the same `tools` schema in the
request and the same `tool_calls` in the response. So one client (`ChatOpenAI`) with a different `base_url` talks to
Kimi, GLM and a local Ollama, and errors arrive as the same `openai` SDK exceptions (see provider_errors.py).

What differs between providers lives here as data (a preset), not as code:
  * the default base URL;
  * whether an API key is required (a local Ollama ignores it, but the SDK still wants a non-empty string);
  * Chat Completions only: `ChatOpenAI` switches some OpenAI models to the Responses API by itself, and other
    providers support that API unevenly. For them we pin the classic endpoint.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal
from urllib.parse import urlsplit

Provider = Literal["openai", "moonshot", "zai", "ollama", "openai_compatible"]
PROVIDERS: tuple[str, ...] = ("openai", "moonshot", "zai", "ollama", "openai_compatible")


@dataclass(frozen=True, slots=True)
class Preset:
    base_url: str | None          # None: the SDK default (OpenAI) or "must be configured" (openai_compatible)
    needs_key: bool
    chat_completions_only: bool
    local: bool = False           # runs on the owner's machine: no per-token price
    key_env: str | None = None    # provider's own key variable, so switching providers does not re-ask for keys


PRESETS: dict[str, Preset] = {
    "openai": Preset(None, needs_key=True, chat_completions_only=False, key_env="OPENAI_API_KEY"),
    # https://platform.kimi.ai/docs/api/overview  (China: https://api.moonshot.cn/v1, set FORGE_MODEL_BASE_URL)
    "moonshot": Preset("https://api.moonshot.ai/v1", needs_key=True, chat_completions_only=True,
                       key_env="MOONSHOT_API_KEY"),
    # https://docs.z.ai/api-reference/introduction  (GLM Coding Plan: https://api.z.ai/api/coding/paas/v4)
    "zai": Preset("https://api.z.ai/api/paas/v4", needs_key=True, chat_completions_only=True, key_env="ZAI_API_KEY"),
    # https://docs.ollama.com/api/openai-compatibility
    "ollama": Preset("http://localhost:11434/v1", needs_key=False, chat_completions_only=True, local=True),
    "openai_compatible": Preset(None, needs_key=False, chat_completions_only=True),
}
OLLAMA_PLACEHOLDER_KEY = "ollama"      # "required but ignored" by Ollama; the SDK refuses an empty key


class ModelConfigError(ValueError):
    """The model settings in .env cannot work. The message names the setting to fix and never contains a key."""


@dataclass(frozen=True, slots=True)
class ModelConnection:
    provider: str
    model: str
    base_url: str | None
    api_key: str = ""
    chat_completions_only: bool = True
    local: bool = False

    def __repr__(self) -> str:     # a connection object may end up in a log or a traceback: never print the key
        return (f"ModelConnection(provider={self.provider!r}, model={self.model!r}, base_url={self.base_url!r}, "
                f"api_key={'***' if self.api_key else ''!r})")

    def public(self) -> dict[str, str | None]:
        """What the server may tell a client (evals label their results with it). No key, no URL credentials."""
        return {"provider": self.provider, "model": self.model, "base_url": _strip_credentials(self.base_url)}


def resolve_connection(*, provider: str, model: str | None, base_url: str | None, api_key: str | None,
                       provider_keys: dict[str, str | None] | None = None) -> ModelConnection:
    """Settings from .env -> one validated connection, or ModelConfigError with a fix-it message."""
    if provider not in PRESETS:
        raise ModelConfigError(f"FORGE_MODEL_PROVIDER={provider!r} is not one of: {', '.join(PROVIDERS)}")
    preset = PRESETS[provider]
    if not model:
        raise ModelConfigError("FORGE_MODEL_NAME must be set in the server environment (.env)")
    url = (base_url or preset.base_url or "").strip() or None
    if url is None and provider == "openai_compatible":
        raise ModelConfigError("FORGE_MODEL_PROVIDER=openai_compatible needs FORGE_MODEL_BASE_URL")
    if url is not None:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ModelConfigError(f"FORGE_MODEL_BASE_URL is not an http(s) URL: {_strip_credentials(url)!r}")
    # Key lookup: FORGE_MODEL_API_KEY (explicit override), else the provider's OWN variable (OPENAI_API_KEY,
    # MOONSHOT_API_KEY, ZAI_API_KEY). Never another provider's key: an OpenAI key must not be sent to a third party.
    own = (provider_keys or {}).get(preset.key_env or "") if preset.key_env else None
    key = (api_key or own or "").strip()
    if not key:
        if preset.needs_key:
            hint = f"{preset.key_env} (or FORGE_MODEL_API_KEY)" if preset.key_env else "FORGE_MODEL_API_KEY"
            raise ModelConfigError(f"{hint} must be set in the server environment (.env) for provider {provider}")
        key = OLLAMA_PLACEHOLDER_KEY
    return ModelConnection(provider, model, url, key, preset.chat_completions_only, preset.local)


def _strip_credentials(url: str | None) -> str | None:
    if not url:
        return url
    parts = urlsplit(url)
    if parts.username or parts.password:
        netloc = parts.hostname or ""
        if parts.port:
            netloc += f":{parts.port}"
        return parts._replace(netloc=netloc).geturl()
    return url


__all__ = ["PRESETS", "PROVIDERS", "ModelConfigError", "ModelConnection", "resolve_connection"]
