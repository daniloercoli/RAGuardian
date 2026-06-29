from app.utils.providers import embedding_factory as embedding_factory_module
from app.utils.providers.embedding_factory import EmbeddingFactory


class FakeEmbeddingProvider:
    def __init__(self, model_name=None, provider_config=None):
        self.model_name = model_name
        self.provider_config = provider_config

    @property
    def provider_name(self):
        return "fake"

    def encode_documents(self, texts):
        return [[0.0] for _text in texts]

    def encode_query(self, query):
        return [0.0]

    def dimensions(self):
        return 1


def test_embedding_factory_lists_provider_models():
    providers = EmbeddingFactory.list_provider_models()

    # First builtin providers from JSON, then local fallback
    assert providers[0]["id"] == "regolo"
    assert providers[0]["default_model"] == "Qwen3-Embedding-8B"
    assert providers[1]["id"] == "local"
    assert providers[1]["default_model"] == "sentence-transformers/all-MiniLM-L6-v2"


def test_embedding_factory_lists_custom_embedding_providers():
    providers = EmbeddingFactory.list_provider_models(
        {
            "embedding_providers": [
                {
                    "id": "embedder",
                    "name": "Embedder",
                    "base_url": "https://embed.example.com/v1",
                    "api_key": "embed-key",
                    "models": ["embed-a"],
                    "default_model": "embed-a",
                    "enabled": True,
                }
            ]
        }
    )

    # index 2 = custom (after regolo + local builtin)
    assert providers[2]["id"] == "embedder"
    assert providers[2]["models"] == ["embed-a"]


def test_embedding_factory_resolves_provider_model_pair():
    assert EmbeddingFactory.resolve(provider="local", model="sentence-transformers/all-MiniLM-L6-v2") == (
        "local",
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    assert EmbeddingFactory.resolve(provider="regolo", model="Qwen3-Embedding-8B") == (
        "regolo",
        "Qwen3-Embedding-8B",
    )
    assert EmbeddingFactory.resolve(provider="regolo", model="sentence-transformers/all-MiniLM-L6-v2") == (
        "regolo",
        "Qwen3-Embedding-8B",
    )


def test_embedding_factory_resolves_custom_embedding_provider_model_pair():
    settings = {
        "embedding_providers": [
            {
                "id": "embedder",
                "name": "Embedder",
                "base_url": "https://embed.example.com/v1",
                "api_key": "embed-key",
                "models": ["embed-a", "vendor/embed-b"],
                "default_model": "vendor/embed-b",
                "enabled": True,
            }
        ]
    }

    assert EmbeddingFactory.resolve(provider="embedder", model="vendor/embed-b", settings=settings) == (
        "embedder",
        "vendor/embed-b",
    )
    assert EmbeddingFactory.resolve(provider="embedder", model="missing", settings=settings) == (
        "embedder",
        "vendor/embed-b",
    )


def test_embedding_factory_passes_selected_model_to_provider(monkeypatch):
    EmbeddingFactory.reset_cache()
    monkeypatch.setitem(EmbeddingFactory.PROVIDER_TYPES, "regolo", FakeEmbeddingProvider)

    provider = EmbeddingFactory.get_provider("Qwen3-Embedding-8B")

    assert provider.model_name == "Qwen3-Embedding-8B"
    EmbeddingFactory.reset_cache()


def test_embedding_factory_builds_custom_embedding_provider(monkeypatch):
    settings = {
        "rag": {
            "embedding_provider": "embedder",
            "embedding_model": "vendor/embed-b",
        },
        "embedding_providers": [
            {
                "id": "embedder",
                "name": "Embedder",
                "base_url": "https://embed.example.com/v1",
                "api_key": "embed-key",
                "models": ["vendor/embed-b"],
                "default_model": "vendor/embed-b",
                "dimensions": 3072,
                "enabled": True,
            }
        ],
    }
    EmbeddingFactory.reset_cache()
    monkeypatch.setattr(embedding_factory_module, "get_settings", lambda _path=None: settings)
    monkeypatch.setattr(
        embedding_factory_module,
        "OpenAICompatibleEmbeddingProvider",
        FakeEmbeddingProvider,
    )

    provider = EmbeddingFactory.get_provider()

    assert provider.model_name == "vendor/embed-b"
    assert provider.provider_config["base_url"] == "https://embed.example.com/v1"
    assert provider.provider_config["api_key"] == "embed-key"
    EmbeddingFactory.reset_cache()
