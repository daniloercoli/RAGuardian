from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.utils import chroma_manager
from utils.vector_store.chroma_persistent import ChromaPersistentVectorStore
from utils.vector_store.factory import create_vector_store


class FakeEmbeddingProvider:
    def encode_documents(self, texts):
        return [[0.1, 0.2] for _ in texts]

    def encode_query(self, text):
        return [0.1, 0.2]


class FakeCollection:
    def __init__(self):
        self.added = None
        self.deleted_ids = None

    def count(self):
        return 0

    def add(self, documents, metadatas, ids, embeddings):
        self.added = {
            "documents": documents,
            "metadatas": metadatas,
            "ids": ids,
            "embeddings": embeddings,
        }

    def get(self, where=None, include=None):
        if where == {"source": "app/uploads/demo.pdf"}:
            return {"ids": ["src123:abc123:chunk:0", "src123:abc123:chunk:1"]}
        if where == {"document_id": "abc123"}:
            return {
                "ids": ["src456:abc123:chunk:0", "src456:abc123:chunk:1"],
                "metadatas": [
                    {"source": "app/uploads/original.pdf", "document_id": "abc123"},
                    {"source": "app/uploads/original.pdf", "document_id": "abc123"},
                ],
            }
        return {"ids": [], "metadatas": []}

    def delete(self, ids):
        self.deleted_ids = ids


class FakeClient:
    def __init__(self, collection):
        self.collection = collection
        self.deleted_collections = []

    def get_or_create_collection(self, name, embedding_function=None):
        assert name == "documents"
        assert embedding_function is None
        return self.collection

    def delete_collection(self, name):
        self.deleted_collections.append(name)


def test_add_documents_to_chroma_uses_source_and_document_hash_chunk_ids(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(chroma_manager, "_get_chroma_client", lambda: FakeClient(collection))
    monkeypatch.setattr(chroma_manager, "_get_embedding_provider", lambda: FakeEmbeddingProvider())

    documents = [
        SimpleNamespace(page_content="alpha", metadata={"source": "app/uploads/demo.pdf", "source_id": "src123", "document_id": "abc123", "chunk_id": 0}),
        SimpleNamespace(page_content="beta", metadata={"source": "app/uploads/demo.pdf", "source_id": "src123", "document_id": "abc123", "chunk_id": 1}),
    ]

    chroma_manager.add_documents_to_chroma(documents)

    assert collection.added["ids"] == ["src123:abc123:chunk:0", "src123:abc123:chunk:1"]
    assert collection.added["documents"] == ["alpha", "beta"]


def test_document_chunk_id_distinguishes_same_content_different_sources():
    first = chroma_manager._document_chunk_id(
        {"source": "app/uploads/one.pdf", "document_id": "samecontent", "chunk_id": 0},
        0,
    )
    second = chroma_manager._document_chunk_id(
        {"source": "app/uploads/two.pdf", "document_id": "samecontent", "chunk_id": 0},
        0,
    )

    assert first != second
    assert first.endswith(":samecontent:chunk:0")
    assert second.endswith(":samecontent:chunk:0")


def test_delete_documents_by_source_deletes_matching_chroma_ids(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(chroma_manager, "_get_chroma_client", lambda: FakeClient(collection))

    deleted = chroma_manager.delete_documents_by_source("app/uploads/demo.pdf")

    assert deleted == 2
    assert collection.deleted_ids == ["src123:abc123:chunk:0", "src123:abc123:chunk:1"]


def test_reset_chroma_collection_deletes_and_recreates_documents_collection(monkeypatch):
    collection = FakeCollection()
    client = FakeClient(collection)
    monkeypatch.setattr(chroma_manager, "_get_chroma_client", lambda: client)

    result = chroma_manager.reset_chroma_collection()

    assert result is collection
    assert client.deleted_collections == ["documents"]


def test_find_document_by_id_returns_existing_source(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(chroma_manager, "_get_chroma_client", lambda: FakeClient(collection))

    duplicate = chroma_manager.find_document_by_id("abc123", exclude_source="app/uploads/demo.pdf")

    assert duplicate == {
        "document_id": "abc123",
        "source": "app/uploads/original.pdf",
        "chunk_id": "src456:abc123:chunk:0",
        "chunks": 2,
    }


def test_find_document_by_id_can_exclude_same_source(monkeypatch):
    collection = FakeCollection()
    monkeypatch.setattr(chroma_manager, "_get_chroma_client", lambda: FakeClient(collection))

    duplicate = chroma_manager.find_document_by_id("abc123", exclude_source="app/uploads/original.pdf")

    assert duplicate is None


def test_query_chroma_with_rerank_applies_score_threshold(monkeypatch):
    docs = [
        Document(page_content="basso", metadata={"source": "low.pdf"}),
        Document(page_content="alto", metadata={"source": "high.pdf"}),
        Document(page_content="medio", metadata={"source": "mid.pdf"}),
    ]

    class FakeReranker:
        def rerank(self, query, retrieved_docs, top_n):
            assert query == "query"
            assert retrieved_docs == docs
            assert top_n == 3
            scored = [
                Document(page_content="alto", metadata={"source": "high.pdf", "reranker_score": 8.0}),
                Document(page_content="medio", metadata={"source": "mid.pdf", "reranker_score": 4.0}),
                Document(page_content="basso", metadata={"source": "low.pdf", "reranker_score": 1.0}),
            ]
            return scored[:top_n]

    store = ChromaPersistentVectorStore()
    monkeypatch.setattr(store, "query", lambda query, k: docs)
    monkeypatch.setattr(chroma_manager, "get_vector_store", lambda: store)

    result = chroma_manager.query_chroma_with_rerank(
        "query",
        k=3,
        top_n=10,
        reranker=FakeReranker(),
        score_threshold=4.5,
    )

    assert [doc.metadata["source"] for doc in result] == ["high.pdf"]


def test_create_vector_store_returns_chroma_persistent_backend():
    store = create_vector_store("chroma_persistent")

    assert isinstance(store, ChromaPersistentVectorStore)


def test_create_vector_store_reports_future_backends_as_not_implemented():
    with pytest.raises(NotImplementedError, match="preparato ma non ancora implementato"):
        create_vector_store("qdrant")
