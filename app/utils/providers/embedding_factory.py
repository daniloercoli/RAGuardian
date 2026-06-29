import os
import sys
from typing import Any, Dict, Optional, Type

# Assicurati che la root dell'app sia in sys.path
root_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from .embedding_base import BaseEmbeddingProvider
from .sentence_transformer_provider import SentenceTransformerProvider
from .openai_compatible_embedding_provider import OpenAICompatibleEmbeddingProvider
from .exceptions import ProviderError
from ..logging_config import EMBEDDING_LOGGER as log
from config import Config
from utils.settings_store import get_settings


# "local" is always available as offline fallback (no API key needed)
_LOCAL_EMBEDDING = {
    "id": "local",
    "name": "Local sentence-transformers",
    "models": ["sentence-transformers/all-MiniLM-L6-v2"],
    "default_model": "sentence-transformers/all-MiniLM-L6-v2",
    "privacy_note": "Esegue gli embeddings sulla macchina locale.",
}


class EmbeddingFactory:
    """Factory for embedding providers with caching and fallback."""

    _instances: Dict[str, BaseEmbeddingProvider] = {}

    # Provider type to class mapping (static, not data-driven)
    PROVIDER_TYPES: Dict[str, Type[BaseEmbeddingProvider]] = {
        "local": SentenceTransformerProvider,
        "sentence-transformers": SentenceTransformerProvider,
    }

    @classmethod
    def _builtin_providers(cls) -> Dict[str, Dict[str, Any]]:
        """Load builtin embedding provider configs from default_providers.json."""
        from ..model_defaults import BUILTIN_EMBEDDING

        providers: Dict[str, Dict[str, Any]] = {}
        for prov in BUILTIN_EMBEDDING:
            pid = prov.get("id", "")
            if pid:
                providers[pid] = {
                    "id": pid,
                    "name": prov.get("name", pid),
                    "type": prov.get("type", "openai_compatible"),
                    "base_url": prov.get("base_url", ""),
                    "requires_api_key": prov.get("requires_api_key", True),
                    "api_key_env": prov.get("api_key_env", ""),
                    "models": prov.get("models", []),
                    "default_model": prov.get("default_model", ""),
                    "dimensions": prov.get("dimensions", 0),
                    "privacy_note": prov.get("privacy_note", ""),
                }
        return providers

    @classmethod
    def get_provider(cls, model_name: Optional[str] = None) -> BaseEmbeddingProvider:
        """
        Get embedding provider instance (cached).

        Args:
            model_name: Name of the embedding model (None uses default from config)

        Returns:
            BaseEmbeddingProvider instance
        """
        settings = get_settings(Config.paths.settings_file)
        configured_provider_type = None
        if model_name is None:
            rag_settings = settings["rag"]
            model_name = rag_settings["embedding_model"]
            configured_provider_type = rag_settings.get("embedding_provider")
        elif "/" in model_name:
            possible_provider, possible_model = model_name.split("/", 1)
            provider_configs = cls._provider_configs(settings)
            if possible_provider in provider_configs:
                configured_provider_type = possible_provider
                model_name = possible_model

        provider_type, model_name = cls.resolve(
            provider=configured_provider_type,
            model=model_name,
            settings=settings,
        )
        cache_key = f"{provider_type}:{model_name}"

        # Return cached instance if available for this provider type
        if cache_key in cls._instances:
            log.debug(f"Returning cached embedding provider: {cache_key}")
            return cls._instances[cache_key]

        provider_class = cls.PROVIDER_TYPES.get(provider_type)
        provider_config = cls._provider_configs(settings).get(provider_type)

        if provider_class is None and not provider_config:
            available = list(cls.PROVIDER_TYPES.keys()) + [
                provider["id"] for provider in settings.get("embedding_providers", [])
            ]
            log.error(f"Embedding provider type '{provider_type}' non supportato. "
                      f"Provider disponibili: {available}")
            raise ProviderError(f"Embedding provider type '{provider_type}' non supportato")

        log.info(f"Creating embedding provider: {provider_type} (model: {model_name})")

        try:
            if provider_class is None:
                provider = OpenAICompatibleEmbeddingProvider(
                    model_name=model_name,
                    provider_config=provider_config,
                )
            else:
                provider = provider_class(model_name=model_name)

            # Cache the instance by provider and model.
            cls._instances[cache_key] = provider
            log.info(f"Embedding provider {provider_type} created and cached "
                    f"(model={model_name}, {provider.dimensions()} dimensions)")

            return provider

        except ValueError as e:
            log.error(f"Failed to create {provider_type} embedding provider: {e}")
            raise ProviderError(f"Embedding provider initialization failed: {e}") from e
        except Exception as e:
            log.error(f"Unexpected error creating {provider_type} embedding provider: {e}",
                      exc_info=True)
            raise ProviderError(f"Embedding provider initialization failed: {e}") from e

    @classmethod
    def get_provider_with_fallback(cls, model_name: Optional[str] = None) -> BaseEmbeddingProvider:
        """
        Get embedding provider with fallback to available providers.

        Args:
            model_name: Name of the preferred embedding model

        Returns:
            BaseEmbeddingProvider instance
        """
        settings = get_settings(Config.paths.settings_file)
        # Try primary provider first
        if model_name is None:
            preferred_models = [settings["rag"]["embedding_model"], "local"]
        else:
            preferred_models = [model_name, "local"]

        last_error = None
        for model in preferred_models:
            try:
                return cls.get_provider(model)
            except ProviderError as e:
                last_error = e
                log.warning(f"Fallback: model '{model}' embedding provider non disponibile: {e}")
                continue

        log.error("Nessun embedding provider disponibile")
        raise ProviderError("Nessun embedding provider disponibile")

    @classmethod
    def reset_cache(cls):
        """Reset embedding provider cache."""
        cls._instances.clear()
        log.info("Embedding provider cache reset")

    @classmethod
    def list_available_models(cls, settings: Optional[dict] = None) -> list:
        """List available embedding model names."""
        models: list[str] = []
        for config in cls._provider_configs(settings).values():
            models.extend(config.get("models", []))
        return models

    @classmethod
    def list_provider_types(cls, settings: Optional[dict] = None) -> list:
        """List available provider type names."""
        return list(cls._provider_configs(settings).keys())

    @classmethod
    def list_provider_models(cls, settings: Optional[dict] = None) -> list[dict]:
        """List available embedding providers and their configurable models."""
        providers = []
        # Builtin providers from JSON
        for provider_id, config in cls._builtin_providers().items():
            providers.append(
                {
                    "id": provider_id,
                    "name": config.get("name", provider_id),
                    "models": list(config.get("models", [])),
                    "default_model": config.get("default_model", ""),
                    "privacy_note": config.get("privacy_note", ""),
                }
            )
        # Local (always available)
        providers.append(
            {
                "id": "local",
                "name": _LOCAL_EMBEDDING["name"],
                "models": list(_LOCAL_EMBEDDING["models"]),
                "default_model": _LOCAL_EMBEDDING["default_model"],
                "privacy_note": _LOCAL_EMBEDDING["privacy_note"],
            }
        )
        # Custom from settings
        for provider in cls._custom_provider_configs(settings):
            providers.append(
                {
                    "id": provider["id"],
                    "name": provider.get("name") or provider["id"],
                    "type": provider.get("type", "openai_compatible"),
                    "requires_api_key": bool(provider.get("requires_api_key", False)),
                    "models": list(provider.get("models", [])),
                    "default_model": provider.get("default_model", ""),
                    "privacy_note": (
                        "Invia testo dei documenti e query al provider custom configurato."
                    ),
                }
            )
        return providers

    @classmethod
    def resolve(
        cls,
        provider: Optional[str] = None,
        model: Optional[str] = None,
        settings: Optional[dict] = None,
    ) -> tuple[str, str]:
        provider_configs = cls._provider_configs(settings)
        selected_model = (model or "").strip()
        provider_type = provider or cls._find_provider_for_model(selected_model, provider_configs)
        if not provider_type:
            provider_type = cls._get_provider_type(selected_model, provider_configs)
        if provider_type not in provider_configs:
            log.warning(f"Unknown embedding provider '{provider_type}', defaulting to local")
            provider_type = "local"

        provider_config = provider_configs[provider_type]
        model_for_provider = str(provider_config["default_model"]).strip()
        if not selected_model:
            selected_model = model_for_provider
        # If model is not in this provider's list, try canonical match
        if selected_model not in provider_config.get("models", []):
            canonical = cls._canonical_model(
                provider_config.get("models", []),
                selected_model,
            )
            if canonical:
                selected_model = canonical
            elif provider is None:
                # Model belongs to another provider
                inferred_provider = cls._find_provider_for_model(
                    selected_model, provider_configs
                )
                inferred_config = provider_configs.get(inferred_provider or "")
                if inferred_config:
                    inferred_canonical = cls._canonical_model(
                        inferred_config.get("models", []), selected_model
                    )
                    return inferred_provider or provider_type, inferred_canonical or selected_model
                selected_model = model_for_provider
            else:
                selected_model = model_for_provider

        return provider_type, selected_model

    @classmethod
    def _provider_configs(cls, settings: Optional[dict] = None) -> Dict[str, Dict[str, Any]]:
        """Merge builtin (from JSON) + local + custom (from settings) providers."""
        # Start with builtin from JSON
        providers = {
            pid: {**config, "custom": False}
            for pid, config in cls._builtin_providers().items()
        }
        # Add local fallback (always present)
        providers["local"] = {
            **_LOCAL_EMBEDDING,
            "custom": False,
        }
        # Add custom from settings
        for provider in cls._custom_provider_configs(settings):
            providers[provider["id"]] = {
                "id": provider["id"],
                "name": provider.get("name") or provider["id"],
                "models": list(provider.get("models", [])),
                "default_model": provider.get("default_model", ""),
                "privacy_note": (
                    "Invia testo dei documenti e query al provider custom configurato."
                ),
                "type": provider.get("type", "openai_compatible"),
                "base_url": provider.get("base_url", ""),
                "api_key_env": provider.get("api_key_env", ""),
                "requires_api_key": bool(provider.get("requires_api_key", False)),
                "api_key": provider.get("api_key", ""),
                "dimensions": provider.get("dimensions", 0),
                "custom": True,
            }
        return providers

    @classmethod
    def _custom_provider_configs(cls, settings: Optional[dict] = None) -> list[dict]:
        if settings is None:
            settings = get_settings(Config.paths.settings_file)
        return [
            provider
            for provider in settings.get("embedding_providers", [])
            if provider.get("id") and provider.get("enabled", True)
        ]

    @classmethod
    def _find_provider_for_model(
        cls,
        selected_model: str,
        provider_configs: Dict[str, Dict[str, Any]],
    ) -> Optional[str]:
        if not selected_model:
            return None
        for provider_id, config in provider_configs.items():
            if cls._canonical_model(config.get("models", []), selected_model):
                return provider_id
        return None

    @classmethod
    def _canonical_model(cls, models: list, selected_model: str) -> Optional[str]:
        if not selected_model:
            return None
        canonical_models = [str(model) for model in models]
        if selected_model in canonical_models:
            return selected_model
        selected_lower = selected_model.lower()
        for model in canonical_models:
            if model.lower() == selected_lower:
                return model
        return None

    @classmethod
    def _get_provider_type(cls, model_name: str, provider_configs: Dict[str, Dict[str, Any]]) -> str:
        """Infer provider type from a model name using provider_configs."""
        model_name_lower = model_name.lower().strip()

        # Check all provider configs for a matching model
        for provider_id, config in provider_configs.items():
            for m in config.get("models", []):
                if str(m).lower() == model_name_lower:
                    return provider_id
            # Partial match for well-known patterns
            if "sentence" in model_name_lower or "minilm" in model_name_lower:
                return "local"

        # Fallback to first builtin provider or local
        builtin = cls._builtin_providers()
        if builtin:
            return next(iter(builtin))

        log.warning(f"Unknown embedding model '{model_name}', defaulting to local")
        return "local"
