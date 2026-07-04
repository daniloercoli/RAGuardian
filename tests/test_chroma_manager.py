from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.utils import chroma_manager
from utils.vector_store import chroma_persistent
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


def test_query_chroma_with_rerank_logs_candidate_documents(monkeypatch):
    docs = [
        Document(page_content="uno", metadata={"source": "app/uploads/one.pdf", "chunk_id": 3}),
        Document(page_content="due", metadata={"document_id": "doc-two"}),
    ]
    messages = []

    class FakeLog:
        def info(self, message, *args):
            messages.append(message % args if args else message)

    class FakeReranker:
        def rerank(self, query, retrieved_docs, top_n):
            return retrieved_docs[:top_n]

    store = ChromaPersistentVectorStore()
    monkeypatch.setattr(store, "query", lambda query, k: docs)
    monkeypatch.setattr(chroma_persistent, "log", FakeLog())

    store.query_with_rerank("query", k=2, top_n=10, reranker=FakeReranker())

    assert "ReRanker: candidati dal vector DB: 1:one.pdf chunk=3, 2:doc-two" in messages


def test_query_chroma_with_rerank_source_diversifies_candidates_before_reranker(monkeypatch):
    docs = [
        Document(page_content=f"a{i}", metadata={"source": "a.pdf", "chunk_id": i})
        for i in range(6)
    ] + [
        Document(page_content="b", metadata={"source": "b.pdf"}),
        Document(page_content="c", metadata={"source": "c.pdf"}),
        Document(page_content="d", metadata={"source": "d.pdf"}),
    ]
    captured = {}

    class FakeReranker:
        def rerank(self, query, retrieved_docs, top_n):
            captured["docs"] = retrieved_docs
            return retrieved_docs[:top_n]

    store = ChromaPersistentVectorStore()

    def fake_query(query, k):
        captured["query_k"] = k
        return docs

    monkeypatch.setattr(store, "query", fake_query)

    store.query_with_rerank(
        "query",
        k=3,
        top_n=6,
        reranker=FakeReranker(),
        diversity_mode="source_diversity",
    )

    assert captured["query_k"] == 24
    assert [doc.metadata["source"] for doc in captured["docs"]] == [
        "a.pdf",
        "a.pdf",
        "a.pdf",
        "b.pdf",
        "c.pdf",
        "d.pdf",
    ]


def test_query_chroma_with_rerank_leaves_candidates_unchanged_when_diversity_is_off(monkeypatch):
    docs = [
        Document(page_content=f"a{i}", metadata={"source": "a.pdf", "chunk_id": i})
        for i in range(6)
    ]
    captured = {}

    class FakeReranker:
        def rerank(self, query, retrieved_docs, top_n):
            captured["docs"] = retrieved_docs
            return retrieved_docs[:top_n]

    store = ChromaPersistentVectorStore()

    def fake_query(query, k):
        captured["query_k"] = k
        return docs

    monkeypatch.setattr(store, "query", fake_query)

    store.query_with_rerank(
        "query",
        k=3,
        top_n=6,
        reranker=FakeReranker(),
        diversity_mode="none",
    )

    assert captured["query_k"] == 6
    assert captured["docs"] == docs


def test_query_chroma_with_rerank_mmr_selects_diverse_candidates_before_reranker(monkeypatch):
    docs = [
        Document(page_content="a0", metadata={"source": "a.pdf", "chroma_score": 1.0}),
        Document(page_content="a1", metadata={"source": "a.pdf", "chroma_score": 0.99}),
        Document(page_content="a2", metadata={"source": "a.pdf", "chroma_score": 0.98}),
        Document(page_content="b", metadata={"source": "b.pdf", "chroma_score": 0.70}),
    ]
    embeddings = [
        [1.0, 0.0],
        [0.99, 0.01],
        [0.98, 0.02],
        [0.0, 1.0],
    ]
    captured = {}

    class FakeReranker:
        def rerank(self, query, retrieved_docs, top_n):
            captured["docs"] = retrieved_docs
            return retrieved_docs[:top_n]

    store = ChromaPersistentVectorStore()

    def fake_query_documents(query, k, include_embeddings):
        captured["query_k"] = k
        captured["include_embeddings"] = include_embeddings
        return docs, embeddings, [1.0, 0.0]

    monkeypatch.setattr(store, "_query_documents", fake_query_documents)

    store.query_with_rerank(
        "query",
        k=2,
        top_n=2,
        reranker=FakeReranker(),
        diversity_mode="mmr",
        mmr_lambda=0.3,
        mmr_candidate_pool=4,
    )

    assert captured["query_k"] == 4
    assert captured["include_embeddings"] is True
    assert [doc.metadata["source"] for doc in captured["docs"]] == ["a.pdf", "b.pdf"]


def test_query_chroma_with_rerank_mmr_lambda_one_matches_relevance_order(monkeypatch):
    docs = [
        Document(page_content="a0", metadata={"source": "a.pdf", "chroma_score": 1.0}),
        Document(page_content="a1", metadata={"source": "a.pdf", "chroma_score": 0.99}),
        Document(page_content="b", metadata={"source": "b.pdf", "chroma_score": 0.70}),
    ]
    embeddings = [
        [1.0, 0.0],
        [0.99, 0.01],
        [0.0, 1.0],
    ]
    captured = {}

    class FakeReranker:
        def rerank(self, query, retrieved_docs, top_n):
            captured["docs"] = retrieved_docs
            return retrieved_docs[:top_n]

    store = ChromaPersistentVectorStore()
    monkeypatch.setattr(
        store,
        "_query_documents",
        lambda query, k, include_embeddings: (docs, embeddings, [1.0, 0.0]),
    )

    store.query_with_rerank(
        "query",
        k=2,
        top_n=2,
        reranker=FakeReranker(),
        diversity_mode="mmr",
        mmr_lambda=1.0,
        mmr_candidate_pool=3,
    )

    assert [doc.page_content for doc in captured["docs"]] == ["a0", "a1"]


def test_create_vector_store_returns_chroma_persistent_backend():
    store = create_vector_store("chroma_persistent")

    assert isinstance(store, ChromaPersistentVectorStore)


def test_create_vector_store_reports_future_backends_as_not_implemented():
    with pytest.raises(NotImplementedError, match="preparato ma non ancora implementato"):
        create_vector_store("qdrant")
