from typing import Callable, Optional

from config import Config
from .base import VectorStore
from .chroma_persistent import ChromaPersistentVectorStore


SUPPORTED_VECTOR_STORE_BACKENDS = {
    "chroma_persistent",
    "chroma_http",
    "qdrant",
    "managed",
}

IMPLEMENTED_VECTOR_STORE_BACKENDS = {"chroma_persistent"}


def create_vector_store(
    backend: Optional[str] = None,
    chroma_client_factory: Optional[Callable] = None,
    embedding_provider_factory: Optional[Callable] = None,
    collection_name: Optional[str] = None,
) -> VectorStore:
    selected_backend = (backend or Config.vector_store.backend or "chroma_persistent").strip().lower()

    if selected_backend == "chroma_persistent":
        return ChromaPersistentVectorStore(
            client_factory=chroma_client_factory,
            embedding_provider_factory=embedding_provider_factory,
            collection_name=collection_name,
        )

    if selected_backend in SUPPORTED_VECTOR_STORE_BACKENDS:
        raise NotImplementedError(
            f"Vector store backend '{selected_backend}' preparato ma non ancora implementato. "
            "Usa VECTOR_STORE_BACKEND=chroma_persistent."
        )

    raise ValueError(
        f"Vector store backend '{selected_backend}' non supportato. "
        f"Backend validi: {', '.join(sorted(SUPPORTED_VECTOR_STORE_BACKENDS))}."
    )
