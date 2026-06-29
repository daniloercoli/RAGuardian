from typing import Dict, Optional, Tuple

from utils.provider_config import is_openai_compatible, provider_cache_key
from .base import BaseLLMProvider
from .exceptions import ProviderError
from .openai_compatible_provider import OpenAICompatibleProvider
from .registry import ProviderRegistry


class ProviderFactory:
    """Factory for built-in and custom LLM providers."""

    _instances: Dict[str, BaseLLMProvider] = {}

    @classmethod
    def get_provider(
        cls,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        settings: Optional[dict] = None,
    ) -> BaseLLMProvider:
        provider_id, _model, provider_config = cls.resolve(model=model, provider=provider, settings=settings)
        cache_key = provider_cache_key(provider_id, provider_config)
        if cache_key in cls._instances:
            return cls._instances[cache_key]

        provider_type = provider_config.get("type")
        try:
            if is_openai_compatible(provider_config):
                instance = OpenAICompatibleProvider(provider_config)
            elif provider_type == "mistral":
                from .mistral_provider import MistralProvider

                instance = MistralProvider()
            elif provider_type == "regolo":
                from .regolo_provider import RegoloProvider

                instance = RegoloProvider()
            else:
                raise ValueError(f"Provider {provider_type} non supportato")
        except Exception as e:
            raise ProviderError(f"Provider initialization failed: {e}") from e

        cls._instances[cache_key] = instance
        return instance

    @classmethod
    def resolve(
        cls,
        model: Optional[str] = None,
        provider: Optional[str] = None,
        settings: Optional[dict] = None,
    ) -> Tuple[str, str, dict]:
        return ProviderRegistry(settings=settings).resolve(model=model, provider=provider)

    @classmethod
    def reset_cache(cls):
        cls._instances.clear()
