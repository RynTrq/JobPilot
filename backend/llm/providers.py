from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from backend.contracts import LlmRequest


class LlmProviderError(RuntimeError):
    pass


class CloudProvider(Protocol):
    async def complete(self, request: LlmRequest, *, model: str) -> str:
        """Return the generated text for one routed request."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    provider: str
    api_key_env: str
    base_url: str
    timeout_s: float = 30.0


DEFAULT_PROVIDER_CONFIGS: dict[str, ProviderConfig] = {
    "groq": ProviderConfig("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    "cerebras": ProviderConfig("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1"),
    "gemini": ProviderConfig("gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai"),
    "mistral": ProviderConfig("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
    "openrouter": ProviderConfig("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
}


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        config: ProviderConfig,
        api_key: str,
        client: httpx.AsyncClient | None = None,
    ):
        self.config = config
        self.api_key = api_key
        self._client = client

    async def complete(self, request: LlmRequest, *, model: str) -> str:
        client = self._client or httpx.AsyncClient(timeout=self.config.timeout_s)
        close_client = self._client is None
        try:
            response = await client.post(
                f"{self.config.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=_chat_payload(request, model),
            )
            if response.status_code >= 400:
                raise LlmProviderError(f"{self.config.provider} returned HTTP {response.status_code}")
            return _extract_text(response.json())
        except httpx.HTTPError as exc:
            raise LlmProviderError(f"{self.config.provider} request failed: {exc.__class__.__name__}") from exc
        finally:
            if close_client:
                await client.aclose()


def build_provider_clients_from_env(
    configs: Mapping[str, ProviderConfig] = DEFAULT_PROVIDER_CONFIGS,
) -> dict[str, CloudProvider]:
    clients: dict[str, CloudProvider] = {}
    for name, config in configs.items():
        api_key = os.getenv(config.api_key_env)
        if not api_key:
            continue
        clients[name] = OpenAICompatibleProvider(config=config, api_key=api_key)
    return clients


def build_daily_caps_from_env(
    configs: Mapping[str, ProviderConfig] = DEFAULT_PROVIDER_CONFIGS,
) -> dict[str, int]:
    caps: dict[str, int] = {}
    for name in configs:
        env_name = f"JOBPILOT_{name.upper().replace('-', '_')}_DAILY_CAP"
        raw = os.getenv(env_name)
        if raw is None:
            continue
        try:
            caps[name] = max(0, int(raw))
        except ValueError:
            continue
    return caps


def _chat_payload(request: LlmRequest, model: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": request.system},
            {"role": "user", "content": request.user},
        ],
        "temperature": request.temperature,
        "max_tokens": request.max_tokens,
    }
    if request.requires_json:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _extract_text(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LlmProviderError("provider response did not contain choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise LlmProviderError("provider choice is not an object")
    message = first.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    if isinstance(first.get("text"), str):
        return first["text"]
    raise LlmProviderError("provider response did not contain text")
