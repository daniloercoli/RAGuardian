import asyncio
from typing import AsyncGenerator, Generator, Optional

from ..provider_config import client_api_key, default_model, normalize_base_url, requires_api_key, resolve_api_key
from .base import BaseLLMProvider
from .error_utils import log_http_error
from .exceptions import ProviderError, RateLimitError, TimeoutError


class OpenAICompatibleProvider(BaseLLMProvider):
    """Provider for custom OpenAI-compatible chat completion endpoints."""

    def __init__(self, provider_config: dict):
        self._config = provider_config
        self._provider_name = provider_config.get("name") or "OpenAI-compatible"
        self._base_url = normalize_base_url(provider_config.get("base_url"))
        self._api_key = resolve_api_key(provider_config)
        self._requires_api_key = requires_api_key(provider_config, default=False)
        self._models = provider_config.get("models", [])

        if not self._base_url:
            raise ValueError(f"base_url non configurato per provider {self._provider_name}")
        if self._requires_api_key and not self._api_key:
            env_hint = provider_config.get("api_key_env") or "settings"
            raise ValueError(f"api_key non configurata per provider {self._provider_name} ({env_hint})")

        from openai import AsyncOpenAI, OpenAI

        # api_key è opzionale (es. Ollama non richiede API key)
        api_key = client_api_key(provider_config)
        self._client = OpenAI(api_key=api_key, base_url=self._base_url)
        self._async_client = AsyncOpenAI(api_key=api_key, base_url=self._base_url)

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def _handle_error(self, error: Exception, model: str, stream: bool = False) -> None:
        log_http_error(self._provider_name, model, self._base_url, error, stream)
        error_msg = str(error).lower()
        if "rate" in error_msg or "429" in error_msg:
            raise RateLimitError(str(error))
        if "timeout" in error_msg or "504" in error_msg:
            raise TimeoutError(str(error))
        raise ProviderError(f"{self._provider_name} API error: {error}")

    def generate(
        self,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        model_name = model or self._default_model()
        try:
            response = self._client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self._handle_error(e, model_name, stream=False)

    def generate_stream(
        self,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: float = 0.3,
    ) -> Generator[str, None, None]:
        model_name = model or self._default_model()
        try:
            stream = self._client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                stream=True,
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            self._handle_error(e, model_name, stream=True)

    async def generate_async(
        self,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: float = 0.3,
    ) -> str:
        model_name = model or self._default_model()
        try:
            response = await self._async_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
            )
            return response.choices[0].message.content or ""
        except Exception as e:
            self._handle_error(e, model_name, stream=False)

    async def generate_stream_async(
        self,
        system: str,
        user: str,
        model: Optional[str] = None,
        temperature: float = 0.3,
    ) -> AsyncGenerator[str, None]:
        model_name = model or self._default_model()
        try:
            stream = await self._async_client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=temperature,
                stream=True,
            )
            async for chunk in stream:
                if chunk.choices and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception as e:
            self._handle_error(e, model_name, stream=True)

    def is_model_available(self, model: str) -> bool:
        return model in self._models

    def _default_model(self) -> str:
        return default_model(self._config)
