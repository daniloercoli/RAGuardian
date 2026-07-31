from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from app.utils import rag_engine
from app.utils.conversation_memory import get_conversation_store, reset_conversation_store
from app.utils.file_index import FileIndex
from app.utils.rag_engine import (
    _response_language_instruction,
    _serialize_context,
    _serialize_sources,
    query_rag_stream_events,
)


def test_serialize_context_uses_configured_file_index_path(tmp_path):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    pdf = upload_dir / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    file_index_path = tmp_path / "data" / "files.json"
    FileIndex(str(file_index_path)).record("demo.pdf", str(pdf), 1, status="indexed")

    context = _serialize_context(
        [SimpleNamespace(page_content="testo", metadata={"source": str(pdf)})],
        file_index_path=str(file_index_path),
    )

    assert context[0]["download_url"] == "/admin/files/download/demo.pdf"


def test_public_sources_are_sanitized(tmp_path):
    pdf = tmp_path / "uploads" / "demo.pdf"
    pdf.parent.mkdir()
    pdf.write_text("fake", encoding="utf-8")
    docs = [
        SimpleNamespace(
            page_content="This is a long enough source snippet for an external client.",
            metadata={"source": str(pdf), "chunk_id": 2, "reranker_score": 0.87},
        )
    ]

    context = _serialize_context(docs, include_downloads=False)
    sources = _serialize_sources(docs)

    assert "download_url" not in context[0]
    assert sources == [
        {
            "filename": "demo.pdf",
            "source_type": "pdf",
            "snippet": "This is a long enough source snippet for an external client.",
            "chunk_id": 2,
            "score": 0.87,
        }
    ]


def test_query_rag_stream_events_returns_tokens_and_final_context(tmp_path, monkeypatch):
    upload_dir = tmp_path / "uploads"
    upload_dir.mkdir()
    pdf = upload_dir / "demo.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    file_index_path = tmp_path / "data" / "files.json"
    FileIndex(str(file_index_path)).record("demo.pdf", str(pdf), 1, status="indexed")

    settings = {
        "rag": {
            "query_k": 5,
            "temperature": 0.2,
            "enable_cache": False,
        }
    }
    context_docs = [SimpleNamespace(page_content="Contesto", metadata={"source": str(pdf)})]

    monkeypatch.setattr(rag_engine, "_load_settings", lambda settings_path=None: settings)
    monkeypatch.setattr(
        rag_engine.ProviderFactory,
        "resolve",
        staticmethod(lambda model=None, provider=None, settings=None: ("mistral", "mistral-medium", {"name": "Mistral"})),
    )
    monkeypatch.setattr(rag_engine, "_get_context", lambda *args, **kwargs: context_docs)
    monkeypatch.setattr(rag_engine, "generate_response", lambda *args, **kwargs: iter(["Ciao", " mondo"]))

    events = list(
        query_rag_stream_events(
            "Domanda valida?",
            model="mistral-medium",
            provider="mistral",
            file_index_path=str(file_index_path),
            public=True,
        )
    )

    assert events[0] == {
        "type": "meta",
        "model": "mistral-medium",
        "provider": "mistral",
        "provider_name": "Mistral",
        "response_language": "auto",
    }
    assert events[1] == {"type": "token", "text": "Ciao"}
    assert events[2] == {"type": "token", "text": " mondo"}
    assert events[3]["type"] == "done"
    assert events[3]["answer"] == "Ciao mondo"
    assert events[3]["response_language"] == "auto"
    assert "download_url" not in events[3]["context"][0]
    assert events[3]["sources"][0]["filename"] == "demo.pdf"


def test_query_rag_stream_events_uses_conversation_memory(monkeypatch):
    reset_conversation_store()
    conversation_id = "conv-12345678"
    get_conversation_store().append_turn(
        conversation_id,
        user="Chi e' il referente del progetto?",
        assistant="Il referente del progetto e' Laura Rossi.",
    )

    settings = {
        "rag": {
            "query_k": 5,
            "temperature": 0.2,
            "enable_cache": False,
        }
    }
    captured = {}
    context_docs = [SimpleNamespace(page_content="Contesto", metadata={"source": "demo.pdf"})]

    monkeypatch.setattr(rag_engine, "_load_settings", lambda settings_path=None: settings)
    monkeypatch.setattr(
        rag_engine.ProviderFactory,
        "resolve",
        staticmethod(lambda model=None, provider=None, settings=None: ("mistral", "mistral-medium", {"name": "Mistral"})),
    )

    def fake_get_context(query, *args, **kwargs):
        captured["retrieval_query"] = query
        return context_docs

    def fake_generate_response(query, context_docs, **kwargs):
        captured["conversation_context"] = kwargs["conversation_context"]
        captured["response_language"] = kwargs["response_language"]
        return iter(["Risposta contestuale"])

    monkeypatch.setattr(rag_engine, "_get_context", fake_get_context)
    monkeypatch.setattr(rag_engine, "generate_response", fake_generate_response)

    events = list(
        query_rag_stream_events(
            "Qual e' il suo ruolo?",
            model="mistral-medium",
            provider="mistral",
            conversation_id=conversation_id,
        )
    )

    assert "Laura Rossi" in captured["retrieval_query"]
    assert "Laura Rossi" in captured["conversation_context"]
    assert captured["response_language"] == "auto"
    assert events[0]["conversation_id"] == conversation_id
    assert events[-1]["conversation_id"] == conversation_id
    assert "Risposta contestuale" in get_conversation_store().render_for_prompt(conversation_id)
    reset_conversation_store()


def test_query_rag_stream_events_uses_frozen_conversation_context(
    monkeypatch,
):
    reset_conversation_store()
    conversation_id = "conv-frozen-12345678"
    get_conversation_store().append_turn(
        conversation_id,
        user="Domanda live",
        assistant="SEGRETO AGGIUNTO DOPO LO SNAPSHOT",
    )
    settings = {
        "rag": {
            "query_k": 5,
            "temperature": 0.2,
            "enable_cache": False,
        }
    }
    captured = {}
    monkeypatch.setattr(
        rag_engine,
        "_load_settings",
        lambda settings_path=None: settings,
    )
    monkeypatch.setattr(
        rag_engine.ProviderFactory,
        "resolve",
        staticmethod(
            lambda model=None, provider=None, settings=None: (
                "mistral",
                "mistral-medium",
                {"name": "Mistral"},
            )
        ),
    )

    def fake_get_context(query, *args, **kwargs):
        captured["retrieval_query"] = query
        return []

    def fake_generate_response(query, context_docs, **kwargs):
        captured["conversation_context"] = kwargs["conversation_context"]
        return iter(["Risposta"])

    monkeypatch.setattr(rag_engine, "_get_context", fake_get_context)
    monkeypatch.setattr(
        rag_engine,
        "generate_response",
        fake_generate_response,
    )

    list(
        query_rag_stream_events(
            "Domanda corrente",
            model="mistral-medium",
            provider="mistral",
            conversation_id=conversation_id,
            conversation_prompt_context="SNAPSHOT PROMPT AUTORIZZATO",
            conversation_retrieval_context=(
                "SNAPSHOT RETRIEVAL AUTORIZZATO"
            ),
        )
    )

    assert captured["conversation_context"] == (
        "SNAPSHOT PROMPT AUTORIZZATO"
    )
    assert "SNAPSHOT RETRIEVAL AUTORIZZATO" in captured[
        "retrieval_query"
    ]
    assert "SEGRETO AGGIUNTO" not in captured["retrieval_query"]
    reset_conversation_store()


def test_conversation_append_checks_lease_before_mutating(monkeypatch):
    calls = []

    class FakeStore:
        def append_turn(self, *_args, **_kwargs):
            calls.append("append")

    monkeypatch.setattr(
        rag_engine,
        "get_conversation_store",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(
        rag_engine,
        "assert_distributed_locks_healthy",
        lambda: (_ for _ in ()).throw(RuntimeError("lease lost")),
    )

    with pytest.raises(RuntimeError, match="lease lost"):
        rag_engine._append_conversation_turn(
            "conv-lease-12345678",
            query="Domanda",
            answer="Risposta",
            provider="fake",
            model="fake-model",
            temperature=0.2,
            settings={},
            knowledge_base_ids=["default"],
        )

    assert calls == []


def test_conversation_summary_checks_lease_before_apply(monkeypatch):
    calls = []
    checks = 0
    summary_job = SimpleNamespace()

    class FakeStore:
        def append_turn(self, *_args, **_kwargs):
            calls.append("append")
            return summary_job

        def apply_summary(self, *_args, **_kwargs):
            calls.append("apply")

    def check_lease():
        nonlocal checks
        checks += 1
        if checks == 2:
            raise RuntimeError("lease lost before summary apply")

    monkeypatch.setattr(
        rag_engine,
        "get_conversation_store",
        lambda: FakeStore(),
    )
    monkeypatch.setattr(
        rag_engine,
        "assert_distributed_locks_healthy",
        check_lease,
    )
    monkeypatch.setattr(
        rag_engine,
        "_summarize_conversation",
        lambda *_args, **_kwargs: "Riassunto",
    )

    with pytest.raises(
        RuntimeError,
        match="lease lost before summary apply",
    ):
        rag_engine._append_conversation_turn(
            "conv-summary-12345678",
            query="Domanda",
            answer="Risposta",
            provider="fake",
            model="fake-model",
            temperature=0.2,
            settings={},
            knowledge_base_ids=["default"],
        )

    assert calls == ["append"]


def test_context_cache_is_namespaced_by_conversation_id(monkeypatch):
    docs = [SimpleNamespace(page_content="Contesto", metadata={"source": "demo.pdf"})]
    captured = {"get": [], "set": []}

    class FakeCache:
        def get(self, query, k, model, namespace="stateless"):
            captured["get"].append((query, k, model, namespace))
            return None

        def set(self, query, results, k, model, namespace="stateless"):
            captured["set"].append((query, results, k, model, namespace))

    monkeypatch.setattr(rag_engine, "_cache", FakeCache())
    monkeypatch.setattr(rag_engine, "query_chroma", lambda *args, **kwargs: docs)

    settings = {"rag": {"enable_cache": True, "reranker_enabled": False}}

    result = rag_engine._get_context(
        "Domanda valida?",
        4,
        "fake-model",
        settings,
        collection_name="workspace-alice",
        conversation_id="workspace-alice:conv-12345678",
    )

    assert result == docs
    assert captured["get"][0][3] == "workspace-alice:conv-12345678"
    assert captured["set"][0][4] == "workspace-alice:conv-12345678"


def test_response_language_instruction_policy():
    assert _response_language_instruction(None) == "Rispondi nella stessa lingua della domanda dell'utente."
    assert _response_language_instruction("auto") == "Rispondi nella stessa lingua della domanda dell'utente."
    assert _response_language_instruction("it") == "Rispondi in italiano."
    assert _response_language_instruction("en") == "Answer in English."
    assert _response_language_instruction("pt-BR") == "Rispondi nella lingua indicata dal codice 'pt-br'."


def test_generate_response_includes_client_context(monkeypatch):
    settings = {
        "rag": {
            "temperature": 0.2,
            "use_internal_knowledge": False,
        }
    }
    captured = {}

    class FakeProvider:
        provider_name = "Fake"

        def generate(self, system, prompt, model, temperature):
            captured["system"] = system
            captured["prompt"] = prompt
            return "Risposta"

    monkeypatch.setattr(
        rag_engine.ProviderFactory,
        "resolve",
        staticmethod(lambda model=None, provider=None, settings=None: ("fake", "fake-model", {"name": "Fake"})),
    )
    monkeypatch.setattr(
        rag_engine.ProviderFactory,
        "get_provider",
        staticmethod(lambda model=None, provider=None, settings=None: FakeProvider()),
    )
    monkeypatch.setattr(
        rag_engine.ErrorUtils,
        "retry_with_backoff",
        staticmethod(lambda func, args=None, max_retries=3: func(*(args or []))),
    )

    result = list(
        rag_engine.generate_response(
            "Domanda valida?",
            [SimpleNamespace(page_content="Contesto", metadata={"source": "demo.pdf"})],
            model="fake-model",
            provider="fake",
            settings=settings,
            client_context={
                "site_name": "Example Site",
                "page_title": "Pricing",
                "instructions": "Visitor is comparing plans.",
            },
            response_language="en",
        )
    )

    assert result == ["Risposta"]
    assert "- Answer in English." in captured["system"]
    assert "--- CONTESTO CLIENT ---" in captured["prompt"]
    assert "Sito: Example Site" in captured["prompt"]
    assert "Pagina: Pricing" in captured["prompt"]
    assert "Istruzioni client: Visitor is comparing plans." in captured["prompt"]


def test_get_context_uses_dedicated_reranker_provider(monkeypatch):
    settings = {
        "rag": {
            "enable_cache": False,
            "reranker_enabled": True,
            "reranker_type": "custom",
            "reranker_model": "ranker/vendor/rerank-b",
            "reranker_top_n": 12,
            "reranker_diversity_mode": "mmr",
            "reranker_mmr_lambda": 0.65,
            "reranker_mmr_candidate_pool": 60,
            "reranker_threshold": 1.5,
        },
        "reranker_providers": [
            {
                "id": "ranker",
                "base_url": "https://rank.example.com/v1",
                "api_key": "ranker-key",
                "enabled": True,
            }
        ],
    }
    captured = {}

    class FakeReranker:
        pass

    def fake_get_reranker(**kwargs):
        captured["reranker_kwargs"] = kwargs
        return FakeReranker()

    def fake_query_chroma_with_rerank(
        query,
        k,
        top_n,
        reranker,
        score_threshold,
        diversity_mode,
        mmr_lambda,
        mmr_candidate_pool,
    ):
        captured["query_kwargs"] = {
            "query": query,
            "k": k,
            "top_n": top_n,
            "reranker": reranker,
            "score_threshold": score_threshold,
            "diversity_mode": diversity_mode,
            "mmr_lambda": mmr_lambda,
            "mmr_candidate_pool": mmr_candidate_pool,
        }
        return ["doc"]

    monkeypatch.setattr(rag_engine, "get_reranker", fake_get_reranker)
    monkeypatch.setattr(rag_engine, "query_chroma_with_rerank", fake_query_chroma_with_rerank)

    result = rag_engine._get_context("domanda", 4, "model", settings, use_cache=False)

    assert result == ["doc"]
    assert captured["reranker_kwargs"] == {
        "enabled": True,
        "model_name": "vendor/rerank-b",
        "base_url": "https://rank.example.com/v1",
        "api_key": "ranker-key",
        "mode": "chat_completions",
    }
    assert captured["query_kwargs"]["top_n"] == 12
    assert captured["query_kwargs"]["diversity_mode"] == "mmr"
    assert captured["query_kwargs"]["mmr_lambda"] == 0.65
    assert captured["query_kwargs"]["mmr_candidate_pool"] == 60
    assert captured["query_kwargs"]["score_threshold"] == 1.5


def test_get_context_defaults_diversity_mode_off(monkeypatch):
    settings = {
        "rag": {
            "enable_cache": False,
            "reranker_enabled": True,
            "reranker_type": "local",
            "reranker_model": "local/test-reranker",
            "reranker_top_n": 12,
        },
    }
    captured = {}

    monkeypatch.setattr(rag_engine, "get_reranker", lambda **kwargs: object())

    def fake_query_chroma_with_rerank(query, **kwargs):
        captured.update(kwargs)
        return ["doc"]

    monkeypatch.setattr(rag_engine, "query_chroma_with_rerank", fake_query_chroma_with_rerank)

    assert rag_engine._get_context("domanda", 4, "model", settings, use_cache=False) == ["doc"]
    assert captured["diversity_mode"] == "none"


def test_federated_context_embeds_once_deduplicates_and_preserves_origins(
    monkeypatch,
):
    calls = {"embeddings": 0, "collections": []}

    class FakeEmbeddingProvider:
        def encode_query(self, query):
            calls["embeddings"] += 1
            assert query == "domanda federata"
            return [1.0, 0.0]

    def fake_query(query_embedding, *, k, collection_name, include_embeddings):
        assert query_embedding == [1.0, 0.0]
        assert k == 3
        calls["collections"].append(collection_name)
        if collection_name == "collection-a":
            return (
                [
                    Document(
                        page_content="Documento condiviso",
                        metadata={"source": "/a/shared.pdf", "chunk_id": 1},
                    ),
                    Document(
                        page_content="Solo A",
                        metadata={"source": "/a/only.pdf", "chunk_id": 2},
                    ),
                ],
                [],
            )
        return (
            [
                Document(
                    page_content="  Documento   condiviso ",
                    metadata={"source": "/b/shared.pdf", "chunk_id": 4},
                ),
                Document(
                    page_content="Solo B",
                    metadata={"source": "/b/only.pdf", "chunk_id": 5},
                ),
            ],
            [],
        )

    monkeypatch.setattr(
        rag_engine.EmbeddingFactory,
        "get_provider",
        staticmethod(lambda: FakeEmbeddingProvider()),
    )
    monkeypatch.setattr(rag_engine, "query_chroma_by_embedding", fake_query)
    settings = {
        "rag": {
            "enable_cache": False,
            "reranker_enabled": False,
        }
    }
    targets = [
        {
            "knowledge_base_id": "default",
            "knowledge_base_name": "General",
            "collection_name": "collection-a",
        },
        {
            "knowledge_base_id": "kb_b",
            "knowledge_base_name": "Legal",
            "collection_name": "collection-b",
        },
    ]

    result = rag_engine._get_context(
        "domanda federata",
        3,
        "model",
        settings,
        use_cache=False,
        knowledge_base_targets=targets,
    )

    assert calls["embeddings"] == 1
    assert sorted(calls["collections"]) == ["collection-a", "collection-b"]
    assert len(result) == 3
    shared = next(doc for doc in result if "condiviso" in doc.page_content)
    assert len(shared.metadata["knowledge_base_origins"]) == 2
    assert shared.metadata["rrf_score"] == pytest.approx(2 / 61)
    assert {
        origin["knowledge_base_id"]
        for origin in shared.metadata["knowledge_base_origins"]
    } == {"default", "kb_b"}
    assert {
        origin["local_rank"]
        for origin in shared.metadata["knowledge_base_origins"]
    } == {1}


def test_federated_context_treats_zero_chroma_score_as_a_real_score(
    monkeypatch,
):
    monkeypatch.setattr(
        rag_engine.EmbeddingFactory,
        "get_provider",
        staticmethod(
            lambda: SimpleNamespace(encode_query=lambda _query: [1.0, 0.0])
        ),
    )

    def fake_query(_embedding, *, collection_name, **_kwargs):
        score = 0.0 if collection_name == "exact" else -0.2
        return [
            Document(
                page_content=f"Content {collection_name}",
                metadata={
                    "source": f"/{collection_name}.pdf",
                    "chroma_score": score,
                },
            )
        ], []

    monkeypatch.setattr(rag_engine, "query_chroma_by_embedding", fake_query)

    result = rag_engine._get_context(
        "domanda",
        1,
        "model",
        {"rag": {"enable_cache": False, "reranker_enabled": False}},
        use_cache=False,
        knowledge_base_targets=[
            {
                "knowledge_base_id": "kb_exact",
                "knowledge_base_name": "Exact",
                "collection_name": "exact",
            },
            {
                "knowledge_base_id": "kb_worse",
                "knowledge_base_name": "Worse",
                "collection_name": "worse",
            },
        ],
    )

    assert [doc.metadata["knowledge_base_id"] for doc in result] == ["kb_exact"]


def test_federated_context_counts_each_kb_once_for_rrf_deduplication(
    monkeypatch,
):
    monkeypatch.setattr(
        rag_engine.EmbeddingFactory,
        "get_provider",
        staticmethod(
            lambda: SimpleNamespace(encode_query=lambda _query: [1.0, 0.0])
        ),
    )

    def fake_query(_embedding, *, collection_name, **_kwargs):
        if collection_name == "duplicated":
            return [
                Document(
                    page_content="Repeated content",
                    metadata={
                        "source": "/duplicated/one.pdf",
                        "chunk_id": 1,
                        "chroma_score": -0.3,
                    },
                ),
                Document(
                    page_content="  Repeated   content ",
                    metadata={
                        "source": "/duplicated/two.pdf",
                        "chunk_id": 2,
                        "chroma_score": -0.4,
                    },
                ),
            ], []
        return [
            Document(
                page_content="Unique content",
                metadata={
                    "source": "/unique.pdf",
                    "chunk_id": 1,
                    "chroma_score": -0.1,
                },
            )
        ], []

    monkeypatch.setattr(rag_engine, "query_chroma_by_embedding", fake_query)

    result = rag_engine._get_context(
        "domanda",
        2,
        "model",
        {"rag": {"enable_cache": False, "reranker_enabled": False}},
        use_cache=False,
        knowledge_base_targets=[
            {
                "knowledge_base_id": "kb_duplicated",
                "knowledge_base_name": "Duplicated",
                "collection_name": "duplicated",
            },
            {
                "knowledge_base_id": "kb_unique",
                "knowledge_base_name": "Unique",
                "collection_name": "unique",
            },
        ],
    )

    repeated = next(doc for doc in result if "Repeated" in doc.page_content)
    assert repeated.metadata["rrf_score"] == pytest.approx(1 / 61)
    assert len(repeated.metadata["knowledge_base_origins"]) == 2
    assert result[0].metadata["knowledge_base_id"] == "kb_unique"


def test_federated_context_fails_whole_query_on_collection_error(monkeypatch):
    monkeypatch.setattr(
        rag_engine.EmbeddingFactory,
        "get_provider",
        staticmethod(
            lambda: SimpleNamespace(encode_query=lambda _query: [1.0, 0.0])
        ),
    )

    def fake_query(_embedding, *, collection_name, **_kwargs):
        if collection_name == "broken":
            raise RuntimeError("collection unavailable")
        return [], []

    monkeypatch.setattr(rag_engine, "query_chroma_by_embedding", fake_query)

    with pytest.raises(RuntimeError, match="collection unavailable"):
        rag_engine._get_context(
            "domanda",
            3,
            "model",
            {"rag": {"enable_cache": False, "reranker_enabled": False}},
            use_cache=False,
            knowledge_base_targets=[
                {
                    "knowledge_base_id": "default",
                    "knowledge_base_name": "General",
                    "collection_name": "healthy",
                },
                {
                    "knowledge_base_id": "kb_b",
                    "knowledge_base_name": "Broken",
                    "collection_name": "broken",
                },
            ],
        )


def test_federated_context_invokes_global_reranker_once(monkeypatch):
    monkeypatch.setattr(
        rag_engine.EmbeddingFactory,
        "get_provider",
        staticmethod(
            lambda: SimpleNamespace(encode_query=lambda _query: [1.0, 0.0])
        ),
    )

    def fake_query(_embedding, *, collection_name, **_kwargs):
        return [
            Document(
                page_content=f"Content {collection_name}",
                metadata={"source": f"/{collection_name}.pdf"},
            )
        ], []

    calls = []

    class FakeReranker:
        def rerank(self, query, docs, top_n):
            calls.append((query, len(docs), top_n))
            return docs[:top_n]

    monkeypatch.setattr(rag_engine, "query_chroma_by_embedding", fake_query)
    monkeypatch.setattr(
        rag_engine,
        "_resolve_reranker",
        lambda _settings: FakeReranker(),
    )
    targets = [
        {
            "knowledge_base_id": "default",
            "knowledge_base_name": "General",
            "collection_name": "a",
        },
        {
            "knowledge_base_id": "kb_b",
            "knowledge_base_name": "Legal",
            "collection_name": "b",
        },
    ]

    result = rag_engine._get_context(
        "domanda",
        2,
        "model",
        {
            "rag": {
                "enable_cache": False,
                "reranker_enabled": True,
                "reranker_top_n": 10,
                "reranker_diversity_mode": "none",
            }
        },
        use_cache=False,
        knowledge_base_targets=targets,
    )

    assert len(result) == 2
    assert calls == [("domanda", 2, 2)]
