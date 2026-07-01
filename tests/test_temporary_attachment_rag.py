from pathlib import Path

from app.utils import temporary_attachment_rag as temp_rag


class FakeEmbeddingProvider:
    def encode_query(self, query):
        return [1.0, 0.0]

    def encode_documents(self, texts):
        vectors = []
        for text in texts:
            vectors.append([1.0, 0.0] if "alpha" in text else [0.0, 1.0])
        return vectors


def test_retrieve_attachment_context_embeds_in_memory_without_chroma(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        '{"rag": {"chunk_size": 1000, "chunk_overlap": 0, "embedding_model": "fake"}}',
        encoding="utf-8",
    )
    attachment = tmp_path / "notes.txt"
    attachment.write_text("alpha project policy\n\nbeta unrelated", encoding="utf-8")
    monkeypatch.setattr(
        temp_rag.EmbeddingFactory,
        "get_provider",
        staticmethod(lambda model_name=None: FakeEmbeddingProvider()),
    )

    docs = temp_rag.retrieve_attachment_context(
        "alpha",
        [{"id": "abc123", "name": "notes.txt", "path": str(attachment)}],
        settings_path=str(settings_path),
        top_k=1,
    )

    assert len(docs) == 1
    assert "alpha" in docs[0].page_content
    assert docs[0].metadata["source_type"] == "temporary_attachment"
    assert docs[0].metadata["temporary_attachment"] is True
