import asyncio

from utils.providers import EmbeddingFactory
from utils.vector_store.chroma_persistent import _default_chroma_client
from utils.vector_store.chroma_persistent import document_chunk_id as _document_chunk_id
from utils.vector_store.chroma_persistent import _reranker_score
from utils.vector_store.factory import create_vector_store


def _get_chroma_client():
    """Crea/restituisce Chroma client."""
    return _default_chroma_client()


def _get_embedding_provider():
    """Get embedding provider from factory."""
    return EmbeddingFactory.get_provider()


def get_vector_store(collection_name: str | None = None):
    return create_vector_store(
        chroma_client_factory=_get_chroma_client,
        embedding_provider_factory=_get_embedding_provider,
        collection_name=collection_name,
    )


def _vector_store(collection_name: str | None = None):
    if collection_name:
        return get_vector_store(collection_name=collection_name)
    return get_vector_store()


def add_documents_to_chroma(documents, collection_name: str | None = None):
    return _vector_store(collection_name).add_documents(documents)


def delete_documents_by_source(source: str, collection_name: str | None = None) -> int:
    return _vector_store(collection_name).delete_by_source(source)


def find_document_by_id(document_id: str, exclude_source=None, collection_name: str | None = None):
    return _vector_store(collection_name).find_document_by_id(document_id, exclude_source=exclude_source)


def get_collection_status(collection_name: str | None = None):
    return _vector_store(collection_name).status()


def reset_chroma_collection(collection_name: str | None = None):
    return _vector_store(collection_name).reset_collection()


def query_chroma(query, k=None, collection_name: str | None = None):
    return _vector_store(collection_name).query(query, k=k)


def query_chroma_with_rerank(
    query: str,
    k: int = 5,
    top_n: int = 20,
    reranker=None,
    score_threshold: float = 0.0,
    collection_name: str | None = None,
):
    return _vector_store(collection_name).query_with_rerank(
        query,
        k=k,
        top_n=top_n,
        reranker=reranker,
        score_threshold=score_threshold,
    )


async def query_chroma_async(query: str, k=None, collection_name: str | None = None):
    """Async wrapper for the configured vector store query."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, query_chroma, query, k, collection_name)


async def add_documents_to_chroma_async(documents, collection_name: str | None = None):
    """Async wrapper for the configured vector store ingestion."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, add_documents_to_chroma, documents, collection_name)
