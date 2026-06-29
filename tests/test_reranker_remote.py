"""Test factory per RemoteReranker."""

import pytest
from langchain_core.documents import Document

from app.utils.reranker import DummyReranker, RemoteReranker, get_reranker


class TestRemoteRerankerFactory:
    """Test factory per RemoteReranker."""

    def test_get_reranker_remote_mode(self, monkeypatch):
        """Verifica che get_reranker ritorni RemoteReranker quando base_url e api_key sono dati."""
        class MockRemoteReranker(RemoteReranker):
            def __init__(self, *args, **kwargs):
                self.model = "mocked"
                self.base_url = kwargs.get("base_url")
                self.api_key = kwargs.get("api_key")

        monkeypatch.setattr("app.utils.reranker.RemoteReranker", MockRemoteReranker)

        reranker = get_reranker(
            enabled=True,
            model_name="reranker-model",
            base_url="https://api.example.com/v1",
            api_key="sk-test-key"
        )

        assert isinstance(reranker, MockRemoteReranker)
        assert reranker.base_url == "https://api.example.com/v1"
        assert reranker.api_key == "sk-test-key"

    def test_get_reranker_local_mode_without_remote_config(self, monkeypatch):
        """Verifica che get_reranker usi locale quando mancano base_url o api_key."""
        from app.utils.reranker import BGEReranker

        class MockBGEReranker(BGEReranker):
            def __init__(self, *args, **kwargs):
                self.model = "mocked"

        monkeypatch.setattr("app.utils.reranker.BGEReranker", MockBGEReranker)

        # Nessun base_url
        reranker = get_reranker(
            enabled=True,
            model_name="test-model",
            base_url=None,
            api_key="sk-test-key"
        )
        assert isinstance(reranker, MockBGEReranker)

        # Nessun api_key
        reranker = get_reranker(
            enabled=True,
            model_name="test-model",
            base_url="https://api.example.com/v1",
            api_key=None
        )
        assert isinstance(reranker, MockBGEReranker)

    def test_get_reranker_fallback_on_remote_error(self, monkeypatch):
        """Verifica che get_reranker torni DummyReranker se RemoteReranker fallisce."""
        def raise_error(*args, **kwargs):
            raise RuntimeError("API not available")

        monkeypatch.setattr(RemoteReranker, "__init__", raise_error)

        reranker = get_reranker(
            enabled=True,
            model_name="test-model",
            base_url="https://api.example.com/v1",
            api_key="sk-test-key"
        )

        assert isinstance(reranker, DummyReranker)


class TestRemoteRerankerBasic:
    """Test base per RemoteReranker (senza chiamate API reali)."""

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
        class MockResponse:
            choices = [MockChoice()]
        def mock_create(*args, **kwargs):
            return MockResponse()
        monkeypatch.setattr(reranker.client.chat.completions, "create", mock_create)
        result = reranker.rerank("query", docs, top_n=5)
        assert len(result) == 5
