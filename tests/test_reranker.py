"""Unit tests for reranker module."""

from langchain_core.documents import Document
import pytest

from app.utils.reranker import BaseReranker, BGEReranker, DummyReranker, RemoteReranker, get_reranker


class TestDummyReranker:
    """Unit test per DummyReranker (pass-through)."""

    def test_returns_empty_list_for_empty_input(self):
        reranker = DummyReranker()
        result = reranker.rerank("query", [], top_n=5)
        assert result == []

    def test_returns_top_n_documents(self):
        reranker = DummyReranker()
        docs = [
            Document(page_content="doc1"),
            Document(page_content="doc2"),
            Document(page_content="doc3"),
            Document(page_content="doc4"),
        ]
        result = reranker.rerank("query", docs, top_n=2)
        assert len(result) == 2
        assert result[0].page_content == "doc1"
        assert result[1].page_content == "doc2"

    def test_returns_all_documents_when_less_than_top_n(self):
        reranker = DummyReranker()
        docs = [
            Document(page_content="doc1"),
            Document(page_content="doc2"),
        ]
        result = reranker.rerank("query", docs, top_n=10)
        assert len(result) == 2
        assert result[0].page_content == "doc1"
        assert result[1].page_content == "doc2"

    def test_preserves_document_metadata(self):
        reranker = DummyReranker()
        docs = [
            Document(page_content="doc1", metadata={"source": "file1.pdf"}),
            Document(page_content="doc2", metadata={"source": "file2.pdf"}),
        ]
        result = reranker.rerank("query", docs, top_n=2)
        assert result[0].metadata["source"] == "file1.pdf"
        assert result[1].metadata["source"] == "file2.pdf"


class TestBGEReranker:
    """Unit test per BGEReranker senza caricare il modello reale."""

    def test_rerank_sorts_by_cross_encoder_score_and_keeps_metadata(self):
        class MockModel:
            def predict(self, pairs):
                assert pairs == [
                    ("query", "doc poco utile"),
                    ("query", "doc molto utile"),
                    ("query", "doc medio"),
                ]
                return [0.1, 2.4, 1.2]

        reranker = BGEReranker.__new__(BGEReranker)
        reranker.model = MockModel()
        docs = [
            Document(page_content="doc poco utile", metadata={"source": "low.pdf"}),
            Document(page_content="doc molto utile", metadata={"source": "high.pdf"}),
            Document(page_content="doc medio", metadata={"source": "mid.pdf"}),
        ]

        result = reranker.rerank("query", docs, top_n=2)

        assert [doc.metadata["source"] for doc in result] == ["high.pdf", "mid.pdf"]
        assert [doc.metadata["reranker_score"] for doc in result] == [2.4, 1.2]

    def test_rerank_returns_empty_for_zero_top_n(self):
        reranker = BGEReranker.__new__(BGEReranker)
        reranker.model = object()

        assert reranker.rerank("query", [Document(page_content="doc")], top_n=0) == []


class TestGetReranker:
    """Unit test per la factory get_reranker."""

    def test_returns_dummy_when_disabled(self):
        reranker = get_reranker(enabled=False)
        assert isinstance(reranker, DummyReranker)
        assert not isinstance(reranker, BGEReranker)

    def test_returns_bge_when_enabled(self, monkeypatch):
        # Mock successful model loading
        class MockModel:
            def predict(self, *args):
                return [1.0] * len(args[0])

        class MockBGEReranker(BGEReranker):
            def __init__(self, *args, **kwargs):
                self.model = MockModel()

        monkeypatch.setattr("app.utils.reranker.BGEReranker", MockBGEReranker)

        reranker = get_reranker(enabled=True, model_name="BAAI/bge-reranker-v2-m3")
        assert isinstance(reranker, MockBGEReranker)

    def test_returns_remote_when_base_url_and_api_key(self, monkeypatch):
        """Verifica che get_reranker ritorni RemoteReranker quando base_url e api_key sono dati."""
        class MockRemoteReranker(RemoteReranker):
            def __init__(self, *args, **kwargs):
                self.model = "mocked"

        monkeypatch.setattr("app.utils.reranker.RemoteReranker", MockRemoteReranker)

        reranker = get_reranker(
            enabled=True,
            model_name="reranker-model",
            base_url="https://api.example.com/v1",
            api_key="test-key"
        )
        assert isinstance(reranker, MockRemoteReranker)

    def test_get_reranker_falls_back_to_local_when_no_api_config(self, monkeypatch):
        """Verifica che get_reranker usi locale quando non c'è config remota."""
        class MockModel:
            def predict(self, *args):
                return [1.0] * len(args[0])

        class MockBGEReranker(BGEReranker):
            def __init__(self, *args, **kwargs):
                self.model = MockModel()

        monkeypatch.setattr("app.utils.reranker.BGEReranker", MockBGEReranker)

        reranker = get_reranker(enabled=True, model_name="test-model")
        assert isinstance(reranker, MockBGEReranker)

    def test_falls_back_to_dummy_on_error(self, monkeypatch):
        def fake_init(*args, **kwargs):
            raise RuntimeError("Model load failed")

        monkeypatch.setattr(BGEReranker, "__init__", fake_init)

        reranker = get_reranker(enabled=True)
        assert isinstance(reranker, DummyReranker)


class TestBaseReranker:
    """Test per la classe base (interfaccia)."""

    def test_rerank_signature(self):
        """Verifica che l'interfaccia sia corretta."""
        docs = [Document(page_content="test")]
        try:
            BaseReranker.rerank(None, "query", docs, 5)
        except NotImplementedError:
            pass

    def test_dummy_reranker_implements_interface(self):
        """Verifica che DummyReranker implementi BaseReranker."""
        reranker = DummyReranker()
        assert isinstance(reranker, BaseReranker)


class TestRemoteReranker:
    """Unit test per RemoteReranker (mocked)."""

    def test_remote_reranker_empty_input(self, monkeypatch):
        """Verifica che RemoteReranker gestisca input vuoto."""
        reranker = RemoteReranker(
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="reranker-model"
        )
        # Mockare la risposta API
        class MockChoice:
            class Message:
                content = "7"
            message = Message()
        
        class MockResponse:
            choices = [MockChoice()]
        
        def mock_create(*args, **kwargs):
            return MockResponse()
        
        monkeypatch.setattr(reranker.client.chat.completions, "create", mock_create)
        
        result = reranker.rerank("query", [], top_n=5)
        assert result == []

    def test_remote_reranker_fewer_than_top_n(self, monkeypatch):
        """Verifica che RemoteReranker restituisca tutto se pochi documenti."""
        reranker = RemoteReranker(
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="reranker-model"
        )
        docs = [Document(page_content=f"doc {i}") for i in range(3)]
        result = reranker.rerank("query", docs, top_n=10)
        assert len(result) == 3

    def test_remote_reranker_respects_top_n(self, monkeypatch):
        """Verifica che RemoteReranker rispetti top_n."""
        reranker = RemoteReranker(
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="reranker-model"
        )
        docs = [Document(page_content=f"doc {i}") for i in range(10)]
        
        # Mockare la risposta API
        class MockChoice:
            class Message:
                content = "7"
            message = Message()
        
        class MockResponse:
            choices = [MockChoice()]
        
        def mock_create(*args, **kwargs):
            return MockResponse()
        
        monkeypatch.setattr(reranker.client.chat.completions, "create", mock_create)
        
        result = reranker.rerank("query", docs, top_n=5)
        assert len(result) == 5

    def test_remote_reranker_chat_mode_overrides_model_name_detection(self, monkeypatch):
        reranker = RemoteReranker(
            base_url="https://api.example.com/v1",
            api_key="test-key",
            model="vendor-rerank-model",
            mode="chat_completions",
        )
        calls = []

        class MockChoice:
            class Message:
                content = "7"
            message = Message()

        class MockResponse:
            choices = [MockChoice()]

        def mock_create(*args, **kwargs):
            calls.append(kwargs)
            return MockResponse()

        monkeypatch.setattr(reranker.client.chat.completions, "create", mock_create)
        docs = [Document(page_content=f"doc {i}") for i in range(3)]

        result = reranker.rerank("query", docs, top_n=1)

        assert len(result) == 1
        assert calls
        assert reranker.use_rerank_endpoint is False
