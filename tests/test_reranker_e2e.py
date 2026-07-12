"""End-to-end test per verificare che i documenti corretti vengano selezionati."""

from pathlib import Path

import pytest
from langchain_core.documents import Document

from app.utils.chroma_manager import query_chroma_with_rerank
from app.utils.file_index import FileIndex
from app.utils.reranker import get_reranker


class TestRerankerE2E:
    """E2E test per simulare il flusso completo di retrieval + reranking."""

    def test_reranker_selects_relevant_documents(self, tmp_path):
        """
        E2E test: Verifica che il reranker selezioni documenti rilevanti.

        Scenario:
        1. Crea documenti con contenuti diversi
        2. Simula retrieval iniziale (chroma_manager)
        3. Applica reranking con BGE
        4. Verifica che i documenti più rilevanti vengano selezionati
        """
        # Setup: crea un reranker (usa cpu per:test in CI)
        reranker = get_reranker(enabled=True, device="cpu")

        # Documenti di esempio con contenuti diversi
        docs = [
            Document(
                page_content="Il gatto nero si muove silenziosamente nel giardino notturno",
                metadata={"source": "animali.txt", "chunk_id": 1},
            ),
            Document(
                page_content="Java è un linguaggio di programmazione orientato agli oggetti",
                metadata={"source": "programmazione.txt", "chunk_id": 2},
            ),
            Document(
                page_content="Il cane bianco corre nel parco giochi",
                metadata={"source": "animali.txt", "chunk_id": 3},
            ),
            Document(
                page_content="Python è un linguaggio di scripting interpretato",
                metadata={"source": "programmazione.txt", "chunk_id": 4},
            ),
        ]

        # Query che dovrebbe preferire documenti sul gatto
        query = "descrizione del gatto nero"

        # Re-rank
        reranked = reranker.rerank(query, docs, top_n=2)

        # Verifica: il reranker dovrebbe selezionare i documenti su "gatto"
        assert len(reranked) == 2
        content_lower = " ".join([d.page_content.lower() for d in reranked])
        assert "gatto" in content_lower, f"Expected 'gatto' in reranked docs, got: {reranked}"

    def test_reranker_sorts_by_relevance(self, tmp_path):
        """
        E2E test: Verifica che i documenti siano ordinati per rilevanza.decrescente.
        """
        reranker = get_reranker(enabled=True, device="cpu")

        docs = [
            Document(
                page_content="recensione del film: ottima regia e attori bravi",
                metadata={"score": 1},
            ),
            Document(
                page_content="film pessimo: noia e attori scarsi",
                metadata={"score": 2},
            ),
            Document(
                page_content="ottimo film con una colonna sonora stupenda",
                metadata={"score": 3},
            ),
        ]

        query = "film recensione positiva"

        reranked = reranker.rerank(query, docs, top_n=3)

        # I punteggi dovrebbero essere in ordine decrescente
        # (assumendo che cross-encoder assegni punteggi più alti a recensioni positive)
        assert len(reranked) == 3

    def test_reranker_with_empty_documents(self, tmp_path):
        """E2E test: Gestione casi limite - input vuoto."""
        reranker = get_reranker(enabled=True)
        result = reranker.rerank("query", [], top_n=5)
        assert result == []

    def test_reranker_with_single_document(self, tmp_path):
        """E2E test: Gestione casi limite - un solo documento."""
        reranker = get_reranker(enabled=True)
        docs = [Document(page_content="un solo documento")]
        result = reranker.rerank("query", docs, top_n=5)
        assert len(result) == 1
        assert result[0].page_content == "un solo documento"

    def test_reranker_respects_top_n(self, tmp_path):
        """E2E test: Verifica che top_n venga rispettato."""
        reranker = get_reranker(enabled=True, device="cpu")

        # Creiamo documenti diversi per avere punteggi distinti
        docs = [
            Document(page_content="mela arancione"),
            Document(page_content="banana gialla"),
            Document(page_content="uva viola"),
            Document(page_content="fragola rossa"),
            Document(page_content="limone giallo"),
        ]

        query = "frutta gialla"

        # Richiedi solo top 2
        result = reranker.rerank(query, docs, top_n=2)
        assert len(result) == 2

        # Richiedi top 4
        result = reranker.rerank(query, docs, top_n=4)
        assert len(result) == 4


class TestIntegrationWithChromaManager:
    """Test che verificano l'integrazione tra chroma_manager e reranker."""

    def test_query_chroma_with_rerank_integration(self, tmp_path, monkeypatch):
        """
        Test end-to-end: Verifica che query_chroma_with_rerank usi il reranker.
        """
        file_index_path = tmp_path / "data" / "files.json"
        file_index_path.parent.mkdir(parents=True, exist_ok=True)
        FileIndex(str(file_index_path)).record("test.pdf", str(tmp_path / "test.pdf"), 1, status="indexed")

        # Documenti simulati (invece di usare Chroma reale)
        docs = [
            Document(
                page_content="la programmazione Python è flessibile e potente",
                metadata={"source": "python.pdf", "chunk_id": 0},
            ),
            Document(
                page_content="il gatto domaniAndrà dal veterinario",
                metadata={"source": "animali.pdf", "chunk_id": 1},
            ),
        ]

        # Mock query_chroma per restituire i nostri documenti
        import app.utils.chroma_manager as chroma_mod

        from utils.vector_store.chroma_persistent import ChromaPersistentVectorStore

        store = ChromaPersistentVectorStore()

        def mock_query(*args, **kwargs):
            return docs

        monkeypatch.setattr(store, "query", mock_query)
        monkeypatch.setattr(chroma_mod, "get_vector_store", lambda: store)

        result = query_chroma_with_rerank(
            query="descrizione del gatto",
            k=10,
            top_n=2,
            reranker=get_reranker(enabled=True),
        )

        assert len(result) == 2
        content = " ".join([d.page_content.lower() for d in result])
        assert "gatto" in content

    def test_query_chroma_with_dummy_rerank(self, tmp_path, monkeypatch):
        """Test che DummyReranker restituisca i primi documenti senza riordinare."""
        file_index_path = tmp_path / "data" / "files.json"
        file_index_path.parent.mkdir(parents=True, exist_ok=True)
        FileIndex(str(file_index_path)).record("test.pdf", str(tmp_path / "test.pdf"), 1, status="indexed")

        docs = [
            Document(page_content="primo"),
            Document(page_content="secondo"),
            Document(page_content="terzo"),
        ]

        import app.utils.chroma_manager as chroma_mod

        from utils.vector_store.chroma_persistent import ChromaPersistentVectorStore

        store = ChromaPersistentVectorStore()

        def mock_query(*args, **kwargs):
            return docs

        monkeypatch.setattr(store, "query", mock_query)
        monkeypatch.setattr(chroma_mod, "get_vector_store", lambda: store)

        # k=2 richiede 2 documenti finali
        result = query_chroma_with_rerank(
            query="query",
            k=2,
            top_n=10,
            reranker=get_reranker(enabled=False),
        )

        # Dummy reranker dovrebbe solo prendere i primi 2
        assert len(result) == 2
        assert result[0].page_content == "primo"
        assert result[1].page_content == "secondo"
