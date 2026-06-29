import asyncio
from typing import List, Optional, Generator
from .embedding_base import BaseEmbeddingProvider
from ..logging_config import EMBEDDING_LOGGER as log


class AsyncEmbeddingProviderWrapper:
    """Wrapper per provider di embedding con supporto async"""
    
    def __init__(self, provider: BaseEmbeddingProvider):
        self._provider = provider
    
    @property
    def provider_name(self) -> str:
        return self._provider.provider_name
    
    def dimensions(self) -> int:
        return self._provider.dimensions()
    
    async def encode_documents_async(self, texts: List[str]) -> List[List[float]]:
        """Async version di encode_documents"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._provider.encode_documents, texts)
    
    async def encode_query_async(self, query: str) -> List[float]:
        """Async version di encode_query"""
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._provider.encode_query, query)
