from abc import ABC, abstractmethod
from typing import List

class BaseEmbeddingProvider(ABC):
    """Abstract base class for embedding providers"""
    
    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Return provider name"""
        pass
    
    @abstractmethod
    def encode_documents(self, texts: List[str]) -> List[List[float]]:
        """Encode multiple documents"""
        pass
    
    @abstractmethod
    def encode_query(self, query: str) -> List[float]:
        """Encode single query"""
        pass
    
    @abstractmethod
    def dimensions(self) -> int:
        """Return embedding dimensions"""
        pass
