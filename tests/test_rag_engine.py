from types import SimpleNamespace

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

    def fake_query_chroma_with_rerank(query, k, top_n, reranker, score_threshold):
        captured["query_kwargs"] = {
            "query": query,
            "k": k,
            "top_n": top_n,
            "reranker": reranker,
            "score_threshold": score_threshold,
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
    assert captured["query_kwargs"]["score_threshold"] == 1.5
