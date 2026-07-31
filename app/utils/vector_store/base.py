from abc import ABC, abstractmethod
from typing import Optional


class VectorStore(ABC):
    """Internal contract for vector database operations used by the RAG service."""

    @abstractmethod
    def add_documents(self, documents):
        pass

    @abstractmethod
    def delete_by_source(self, source: str) -> int:
        pass

    @abstractmethod
    def snapshot_by_source(self, source: str) -> dict:
        pass

    @abstractmethod
    def restore_source_snapshot(self, source: str, snapshot: dict) -> int:
        pass

    @abstractmethod
    def find_document_by_id(self, document_id: str, exclude_source: Optional[str] = None) -> Optional[dict]:
        pass

    @abstractmethod
    def query(self, query: str, k: Optional[int] = None):
        pass

    @abstractmethod
    def query_by_embedding(
        self,
        query_embedding,
        k: int,
        *,
        include_embeddings: bool = False,
    ):
        pass

    @abstractmethod
    def query_with_rerank(
        self,
        query: str,
        k: int = 5,
        top_n: int = 20,
        reranker=None,
        score_threshold: float = 0.0,
        diversity_mode: str = "none",
        mmr_lambda: float = 0.7,
        mmr_candidate_pool: Optional[int] = None,
    ):
        pass

    @abstractmethod
    def reset_collection(self):
        pass

    @abstractmethod
    def status(self) -> dict:
        pass
