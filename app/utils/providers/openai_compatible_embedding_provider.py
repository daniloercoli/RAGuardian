from typing import List, Optional

from ..provider_config import client_api_key, default_model, normalize_base_url, requires_api_key, resolve_api_key
from .embedding_base import BaseEmbeddingProvider
from .exceptions import ProviderError


class OpenAICompatibleEmbeddingProvider(BaseEmbeddingProvider):
    """Embedding provider for custom OpenAI-compatible endpoints."""

    def __init__(self, model_name: Optional[str] = None, provider_config: Optional[dict] = None):
        self._config = provider_config or {}
        self.model_name = model_name or self._default_model()
        self._provider_name = self._config.get("name") or "OpenAI-compatible embeddings"
        self._base_url = normalize_base_url(self._config.get("base_url"))
        self._api_key = resolve_api_key(self._config)
        self._requires_api_key = requires_api_key(self._config, default=True)
        self._dimensions = int(self._config.get("dimensions") or 0)

        if not self._base_url:
            raise ValueError(f"base_url non configurato per provider embeddings {self._provider_name}")
        if self._requires_api_key and not self._api_key:
            raise ValueError(f"api_key non configurata per provider embeddings {self._provider_name}")
        if not self.model_name:
            raise ValueError(f"modello non configurato per provider embeddings {self._provider_name}")

        from openai import OpenAI

        self._client = OpenAI(api_key=client_api_key(self._config), base_url=self._base_url)

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def encode_documents(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            response = self._client.embeddings.create(
                model=self.model_name,
                input=texts,
            )
            return [data.embedding for data in response.data]
        except Exception as e:
            raise ProviderError(f"{self._provider_name} embeddings API error: {e}") from e

    def encode_query(self, query: str) -> List[float]:
        try:
            response = self._client.embeddings.create(
                model=self.model_name,
                input=query,
            )
            return response.data[0].embedding
        except Exception as e:
            raise ProviderError(f"{self._provider_name} embeddings API error: {e}") from e

    def dimensions(self) -> int:
        return self._dimensions

    def _default_model(self) -> str:
        return default_model(self._config)
