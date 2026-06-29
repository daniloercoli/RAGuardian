import os
from typing import List, Optional
from .embedding_base import BaseEmbeddingProvider
from ..logging_config import EMBEDDING_LOGGER as log

class RegoloEmbeddingProvider(BaseEmbeddingProvider):
    """Regolo.ai embedding provider"""
    
    MODEL_NAME = "Qwen3-Embedding-8B"
    MODEL_DIMENSIONS = {
        "Qwen3-Embedding-8B": 1024,
    }
    BASE_URL = "https://api.regolo.ai/v1"
    
    API_KEY = None
    
    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or self.MODEL_NAME
        self._api_key = os.getenv("REGOLO_API_KEY") or os.getenv("MISTRAL_API_KEY")
        if not self._api_key:
            raise ValueError(
                "REGOLO_API_KEY o MISTRAL_API_KEY non configurata. "
                "Nota privacy: usando Regolo embeddings, il testo dei documenti e delle query "
                "viene inviato a Regolo per generare gli embeddings."
            )
        
        from openai import OpenAI
        self._client = OpenAI(
            api_key=self._api_key,
            base_url=self.BASE_URL
        )
    
    @property
    def provider_name(self) -> str:
        return "Regolo.ai (cloud)"
    
    def encode_documents(self, texts: List[str]) -> List[List[float]]:
        response = self._client.embeddings.create(
            model=self.model_name,
            input=texts
        )
        return [data.embedding for data in response.data]
    
    def encode_query(self, query: str) -> List[float]:
        response = self._client.embeddings.create(
            model=self.model_name,
            input=query
        )
        log.debug(f"Encoded Query: {response.data[0].embedding}")
        return response.data[0].embedding
    
    def dimensions(self) -> int:
        return self.MODEL_DIMENSIONS.get(self.model_name, 1024)
