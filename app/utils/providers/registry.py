from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from utils.settings_store import get_settings
from utils.model_defaults import load_builtin_provider_definitions
from utils.provider_config import resolve_api_key


@dataclass(frozen=True)
class ModelInfo:
    id: str
    name: str
    provider: str
    provider_name: str
    provider_type: str
    enabled: bool = True


class ProviderRegistry:
    """Single source of truth for built-in and custom model routing."""

    def __init__(self, settings: Optional[dict] = None):
        self.settings = settings or get_settings()

    def providers(self) -> Dict[str, Dict[str, object]]:
        providers = {
            key: _with_resolved_api_key(value)
            for key, value in load_builtin_provider_definitions().items()
        }
        for custom in self.settings.get("custom_providers", []):
            if not custom.get("enabled", True):
                continue
            providers[custom["id"]] = {
                "id": custom["id"],
                "name": custom.get("name") or custom["id"],
                "type": custom.get("type", "openai_compatible"),
                "requires_api_key": bool(custom.get("requires_api_key", False)),
                "models": custom.get("models", []),
                "default_model": custom.get("default_model", ""),
                "base_url": custom.get("base_url", ""),
                "api_key_env": custom.get("api_key_env", ""),
                "api_key": custom.get("api_key", ""),
            }
        return providers

    def list_models(self) -> List[ModelInfo]:
        models: List[ModelInfo] = []
        for provider_id, provider in self.providers().items():
            provider_name = str(provider.get("name") or provider_id)
            provider_type = str(provider.get("type") or provider_id)
            for model in provider.get("models", []):
                model_id = str(model)
                models.append(
                    ModelInfo(
                        id=model_id,
                        name=f"{model_id} ({provider_name})",
                        provider=provider_id,
                        provider_name=provider_name,
                        provider_type=provider_type,
                    )
                )
        return models

    def model_ids(self) -> List[str]:
        return [model.id for model in self.list_models()]

    def resolve(self, model: Optional[str] = None, provider: Optional[str] = None) -> Tuple[str, str, Dict[str, object]]:
        providers = self.providers()
        rag = self.settings.get("rag", {})

        if model and ":" in model and provider is None:
            maybe_provider, maybe_model = model.split(":", 1)
            if maybe_provider in providers:
                provider = maybe_provider
                model = maybe_model

        explicit_provider = provider is not None
        if not providers:
            raise ValueError("Nessun provider LLM configurato")

        fallback_provider_id = next(iter(providers))
        provider_id = provider or rag.get("default_provider") or fallback_provider_id
        if provider_id not in providers:
            provider_id = fallback_provider_id

        provider_config = providers[provider_id]
        available_models = [str(item) for item in provider_config.get("models", [])]
        selected_model = model or str(rag.get("default_model") or provider_config.get("default_model") or "")
        explicit_model = bool(model)

        if selected_model not in available_models and not explicit_provider:
            matching_provider = self.find_provider_for_model(selected_model)
            if matching_provider:
                provider_id = matching_provider
                provider_config = providers[provider_id]
                available_models = [str(item) for item in provider_config.get("models", [])]

        if selected_model not in available_models:
            if explicit_model:
                raise ValueError(
                    f"Modello '{selected_model}' non configurato per il provider '{provider_id}'. "
                    "I model id sono case-sensitive: usa il nome esatto esposto da /api/v1/models."
                )
            selected_model = str(provider_config.get("default_model") or (available_models[0] if available_models else ""))

        return provider_id, selected_model, provider_config

    def find_provider_for_model(self, model: str) -> Optional[str]:
        for provider_id, provider in self.providers().items():
            if model in provider.get("models", []):
                return provider_id
        return None


def _with_resolved_api_key(provider: dict) -> Dict[str, object]:
    resolved = dict(provider)
    if not resolved.get("api_key"):
        resolved["api_key"] = resolve_api_key(resolved)
    return resolved
