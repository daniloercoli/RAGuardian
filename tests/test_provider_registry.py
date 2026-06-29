import pytest

from app.utils.model_defaults import (
    ModelConfigurationError,
    load_builtin_ocr_providers,
    load_builtin_provider_definitions,
    load_builtin_reranker_providers,
    load_builtin_voice_providers,
)
from app.utils.providers.registry import ProviderRegistry


def test_builtin_models_are_listed():
    registry = ProviderRegistry()
    model_ids = registry.model_ids()

    assert "mistral-medium" in model_ids
    assert "gpt-oss-120b" in model_ids
    assert "Llama-3.3-70B-Instruct" in model_ids


def test_builtin_models_are_loaded_from_json():
    providers = load_builtin_provider_definitions()

    assert providers["mistral"]["requires_api_key"] is True
    assert providers["mistral"]["api_key_env"] == "MISTRAL_API_KEY"
    assert "mistral-medium" in providers["mistral"]["models"]
    assert providers["regolo"]["api_key_env"] == "REGOLO_API_KEY"


def test_builtin_reranker_mode_is_loaded_from_json():
    providers = {provider["id"]: provider for provider in load_builtin_reranker_providers()}

    assert providers["regolo"]["reranker_mode"] == "rerank"


def test_builtin_voice_language_defaults_to_autodetect():
    providers = {provider["id"]: provider for provider in load_builtin_voice_providers()}

    assert providers["regolo"]["stt_language"] == ""


def test_builtin_ocr_provider_is_loaded_from_json():
    providers = {provider["id"]: provider for provider in load_builtin_ocr_providers()}

    assert providers["regolo"]["base_url"] == "https://api.regolo.ai/v1"
    assert providers["regolo"]["api_key_env"] == "REGOLO_API_KEY"
    assert providers["regolo"]["default_model"] == "deepseek-ocr-2"
    assert providers["regolo"]["ocr_mode"] == "vision_chat"
    assert providers["regolo"]["input_types"] == ["image", "pdf"]


def test_missing_builtin_model_json_raises_clear_error(tmp_path):
    missing = tmp_path / "missing-default-providers.json"

    with pytest.raises(ModelConfigurationError) as exc:
        load_builtin_provider_definitions(str(missing))

    assert "File configurazione provider/modelli non trovato" in str(exc.value)
    assert "default_providers.json" in str(exc.value)


def test_empty_builtin_model_json_raises_clear_error(tmp_path):
    empty = tmp_path / "default_providers.json"
    empty.write_text('{"providers": {"mistral": {"models": []}}}', encoding="utf-8")

    with pytest.raises(ModelConfigurationError) as exc:
        load_builtin_provider_definitions(str(empty))

    assert "Nessun modello configurato" in str(exc.value)


def test_custom_provider_models_are_listed_and_resolved():
    settings = {
        "rag": {"default_provider": "custom", "default_model": "custom-a"},
        "custom_providers": [
            {
                "id": "custom",
                "name": "Custom OpenAI",
                "type": "openai_compatible",
                "base_url": "https://example.com/v1",
                "api_key": "secret",
                "models": ["custom-a", "custom-b"],
                "default_model": "custom-a",
                "enabled": True,
            }
        ],
    }

    registry = ProviderRegistry(settings)

    provider, model, config = registry.resolve(model="custom-b", provider="custom")
    assert provider == "custom"
    assert model == "custom-b"
    assert config["type"] == "openai_compatible"
    assert "custom-a" in registry.model_ids()


def test_new_builtin_provider_can_be_added_from_json(tmp_path, monkeypatch):
    config = tmp_path / "default_providers.json"
    config.write_text(
        """
        {
          "llm": [
            {
              "id": "acme",
              "name": "Acme AI",
              "type": "openai_compatible",
              "base_url": "https://api.acme.example/v1",
              "api_key_env": "ACME_API_KEY",
              "models": ["acme-chat"],
              "default_model": "acme-chat"
            }
          ]
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setenv("RAG_DEFAULT_PROVIDERS_FILE", str(config))

    providers = load_builtin_provider_definitions()
    registry = ProviderRegistry({"rag": {"default_provider": "acme", "default_model": "acme-chat"}})
    provider, model, resolved = registry.resolve()

    assert providers["acme"]["base_url"] == "https://api.acme.example/v1"
    assert provider == "acme"
    assert model == "acme-chat"
    assert resolved["type"] == "openai_compatible"


def test_explicit_model_resolution_is_case_sensitive():
    registry = ProviderRegistry()

    provider, model, _config = registry.resolve(model="Llama-3.3-70B-Instruct")

    assert provider == "regolo"
    assert model == "Llama-3.3-70B-Instruct"

    with pytest.raises(ValueError, match="case-sensitive"):
        registry.resolve(model="llama-3.3-70b-instruct", provider="regolo")


def test_explicit_provider_does_not_reassign_model_to_another_provider():
    registry = ProviderRegistry()

    with pytest.raises(ValueError):
        registry.resolve(model="Llama-3.3-70B-Instruct", provider="mistral")
