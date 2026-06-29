from pathlib import Path

from app.utils.settings_store import (
    SettingsStore,
    mask_secret,
    normalize_custom_provider,
    normalize_ocr_provider,
    normalize_reranker_provider,
    normalize_voice_provider,
)


def test_settings_store_creates_defaults(tmp_path):
    settings_file = tmp_path / "settings.json"
    store = SettingsStore(str(settings_file))

    settings = store.load()

    assert settings_file.exists()
    assert settings["rag"]["query_k"] == 5
    assert settings["rag"]["default_model"] == "gpt-oss-120b"
    assert settings["voice"]["enabled"] is True
    assert settings["voice"]["stt_language"] == ""
    assert settings["ocr"]["enabled"] is True
    assert settings["ocr"]["provider"] == "regolo"
    assert settings["ocr"]["default_model"] == "deepseek-ocr-2"
    assert settings["ocr_providers"] == []


def test_settings_store_persists_runtime_values(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    saved = store.update(
        {
            "rag": {
                "query_k": 12,
                "temperature": 0.7,
                "embedding_provider": "regolo",
                "embedding_model": "Qwen3-Embedding-8B",
            }
        }
    )
    loaded = store.load()

    assert saved["rag"]["query_k"] == 12
    assert loaded["rag"]["temperature"] == 0.7
    assert loaded["rag"]["embedding_provider"] == "regolo"
    assert loaded["rag"]["embedding_model"] == "Qwen3-Embedding-8B"


def test_settings_store_canonicalizes_llm_default_model_casing(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "rag": {
                "default_provider": "regolo",
                "default_model": "llama-3.3-70b-instruct",
            }
        }
    )

    assert settings["rag"]["default_provider"] == "regolo"
    assert settings["rag"]["default_model"] == "Llama-3.3-70B-Instruct"


def test_settings_store_normalizes_unknown_embedding_provider(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update({"rag": {"embedding_provider": "unknown"}})

    assert settings["rag"]["embedding_provider"] == "regolo"


def test_settings_store_normalizes_embedding_model_for_provider(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "rag": {
                "embedding_provider": "regolo",
                "embedding_model": "sentence-transformers/all-MiniLM-L6-v2",
            }
        }
    )

    assert settings["rag"]["embedding_model"] == "Qwen3-Embedding-8B"


def test_settings_store_migrates_legacy_lowercase_regolo_embedding_model(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "rag": {
                "embedding_provider": "regolo",
                "embedding_model": "qwen3-embedding-8b",
            }
        }
    )

    assert settings["rag"]["embedding_model"] == "Qwen3-Embedding-8B"


def test_public_view_masks_secrets(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))
    store.update(
        {
            "auth": {"api_keys": [{"name": "client", "key": "secret-key-1234", "can_upload": True}]},
            "custom_providers": [
                {
                    "id": "custom",
                    "name": "Custom",
                    "base_url": "https://example.com/v1",
                    "api_key": "provider-secret-1234",
                    "models": ["model-a"],
                    "default_model": "model-a",
                    "enabled": True,
                }
            ],
            "embedding_providers": [
                {
                    "id": "embedder",
                    "name": "Embedder",
                    "base_url": "https://embed.example.com/v1",
                    "api_key": "embed-secret-1234",
                    "models": ["embed-a"],
                    "default_model": "embed-a",
                    "dimensions": 1536,
                    "enabled": True,
                }
            ],
            "reranker_providers": [
                {
                    "id": "ranker",
                    "name": "Ranker",
                    "base_url": "https://rank.example.com/v1",
                    "api_key": "ranker-secret-1234",
                    "models": ["rerank-a"],
                    "default_model": "rerank-a",
                    "enabled": True,
                }
            ],
            "voice_providers": [
                {
                    "id": "speaker",
                    "name": "Speaker",
                    "base_url": "https://speaker.example.com/v1",
                    "api_key": "speaker-secret-1234",
                    "stt_model": "whisper-1",
                    "tts_model": "tts-1",
                    "voice": "nova",
                    "format": "wav",
                    "enabled": True,
                }
            ],
            "rag": {"reranker_regolo_api_key": "regolo-secret-1234"},
            "voice": {
                "enabled": True,
                "base_url": "https://voice.example.com/v1",
                "api_key": "voice-secret-1234",
                "stt_model": "whisper-1",
                "tts_model": "tts-1",
            },
            "ocr_providers": [
                {
                    "id": "vision-ocr",
                    "name": "Vision OCR",
                    "base_url": "https://ocr.example.com/v1",
                    "api_key": "ocr-provider-secret-1234",
                    "models": ["vision-ocr-model"],
                    "default_model": "vision-ocr-model",
                    "enabled": True,
                }
            ],
            "ocr": {
                "enabled": True,
                "provider": "vision-ocr",
                "base_url": "https://ocr.example.com/v1",
                "api_key": "ocr-active-secret-1234",
                "models": ["vision-ocr-model"],
                "default_model": "vision-ocr-model",
            },
        }
    )

    public = store.public_view()

    assert public["auth"]["api_keys"][0]["key"] == "secr...1234"
    assert public["custom_providers"][0]["api_key"] == "prov...1234"
    assert public["embedding_providers"][0]["api_key"] == "embe...1234"
    assert public["reranker_providers"][0]["api_key"] == "rank...1234"
    assert public["voice_providers"][0]["api_key"] == "spea...1234"
    assert public["ocr_providers"][0]["api_key"] == "ocr-...1234"
    assert public["rag"]["reranker_regolo_api_key"] == "rego...1234"
    assert public["voice"]["api_key"] == "voic...1234"
    assert public["ocr"]["api_key"] == "ocr-...1234"


def test_api_key_scopes_backfill_from_can_upload(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))
    settings = store.update(
        {
            "auth": {
                "api_keys": [
                    {"name": "reader", "key": "reader-key"},
                    {"name": "uploader", "key": "upload-key", "can_upload": True},
                    {"name": "speech", "key": "speech-key", "scopes": ["speech"]},
                ]
            }
        }
    )

    keys = {item["name"]: item for item in settings["auth"]["api_keys"]}
    assert keys["reader"]["scopes"] == ["query"]
    assert keys["uploader"]["scopes"] == ["query", "ingest"]
    assert keys["uploader"]["can_upload"] is True
    assert keys["speech"]["scopes"] == ["speech"]


def test_settings_store_persists_custom_embedding_provider_selection(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "embedding_providers": [
                {
                    "id": "embedder",
                    "name": "Embedder",
                    "base_url": "https://embed.example.com/v1",
                    "api_key_env": "EMBEDDER_API_KEY",
                    "requires_api_key": True,
                    "api_key": "embed-key",
                    "models": ["embed-small", "vendor/embed-large"],
                    "default_model": "vendor/embed-large",
                    "dimensions": 3072,
                    "enabled": True,
                }
            ],
            "rag": {
                "embedding_provider": "embedder",
                "embedding_model": "vendor/embed-large",
            },
        }
    )

    assert settings["embedding_providers"][0]["dimensions"] == 3072
    assert settings["embedding_providers"][0]["api_key_env"] == "EMBEDDER_API_KEY"
    assert settings["embedding_providers"][0]["requires_api_key"] is True
    assert settings["rag"]["embedding_provider"] == "embedder"
    assert settings["rag"]["embedding_model"] == "vendor/embed-large"


def test_settings_store_resets_disabled_custom_embedding_provider(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "embedding_providers": [
                {
                    "id": "embedder",
                    "name": "Embedder",
                    "base_url": "https://embed.example.com/v1",
                    "api_key": "embed-key",
                    "models": ["embed-small"],
                    "default_model": "embed-small",
                    "enabled": False,
                }
            ],
            "rag": {
                "embedding_provider": "embedder",
                "embedding_model": "embed-small",
            },
        }
    )

    assert settings["rag"]["embedding_provider"] == "regolo"
    assert settings["rag"]["embedding_model"] == "Qwen3-Embedding-8B"


def test_settings_store_supports_no_auth_custom_embedding_provider(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "embedding_providers": [
                {
                    "id": "local-embed",
                    "name": "Local Embed",
                    "base_url": "http://localhost:8001/v1",
                    "requires_api_key": False,
                    "models": ["embed-local"],
                    "default_model": "embed-local",
                    "enabled": True,
                }
            ],
            "rag": {
                "embedding_provider": "local-embed",
                "embedding_model": "embed-local",
            },
        }
    )

    assert settings["embedding_providers"][0]["requires_api_key"] is False
    assert settings["embedding_providers"][0]["api_key"] == ""
    assert settings["rag"]["embedding_provider"] == "local-embed"
    assert settings["rag"]["embedding_model"] == "embed-local"


def test_normalize_custom_provider_parses_model_list():
    provider = normalize_custom_provider(
        {
            "id": "My Provider",
            "base_url": "https://example.com/v1/",
            "api_key": "key",
            "models": "alpha\nbeta, gamma",
            "default_model": "beta",
        }
    )

    assert provider["id"] == "my-provider"
    assert provider["base_url"] == "https://example.com/v1"
    assert provider["models"] == ["alpha", "beta", "gamma"]
    assert provider["default_model"] == "beta"


def test_normalize_custom_provider_canonicalizes_default_model_casing():
    provider = normalize_custom_provider(
        {
            "id": "custom",
            "base_url": "https://example.com/v1/",
            "api_key": "key",
            "models": ["CaseSensitive-Model"],
            "default_model": "casesensitive-model",
        }
    )

    assert provider["models"] == ["CaseSensitive-Model"]
    assert provider["default_model"] == "CaseSensitive-Model"


def test_normalize_reranker_provider_defaults_to_openai_chat_completions():
    provider = normalize_reranker_provider(
        {
            "id": "ranker",
            "base_url": "https://example.com/v1/",
            "models": ["rank-model"],
        }
    )

    assert provider["base_url"] == "https://example.com/v1"
    assert provider["reranker_mode"] == "chat_completions"


def test_normalize_reranker_provider_accepts_rerank_endpoint_mode():
    provider = normalize_reranker_provider(
        {
            "id": "ranker",
            "base_url": "https://example.com/v1/",
            "models": ["rank-model"],
            "reranker_mode": "rerank",
        }
    )

    assert provider["reranker_mode"] == "rerank"


def test_normalize_voice_provider_accepts_stt_and_tts_models():
    provider = normalize_voice_provider(
        {
            "id": "voice vendor",
            "base_url": "https://voice.example.com/v1/",
            "api_key": "voice-key",
            "stt_model": "whisper-1",
            "stt_language": "en",
            "tts_model": "tts-1",
            "voice": "nova",
            "format": "wav",
            "requires_api_key": "on",
        }
    )

    assert provider["id"] == "voice-vendor"
    assert provider["base_url"] == "https://voice.example.com/v1"
    assert provider["stt_model"] == "whisper-1"
    assert provider["stt_language"] == "en"
    assert provider["tts_model"] == "tts-1"
    assert provider["format"] == "wav"
    assert provider["requires_api_key"] is True


def test_normalize_ocr_provider_accepts_vision_models_and_inputs():
    provider = normalize_ocr_provider(
        {
            "id": "Vision OCR",
            "base_url": "https://ocr.example.com/v1/",
            "api_key": "ocr-key",
            "models": "vision-large\nvision-small",
            "default_model": "vision-small",
            "ocr_mode": "vision",
            "input_types": "image\npdf",
            "supports_layout": "on",
            "supports_tables": False,
        }
    )

    assert provider["id"] == "vision-ocr"
    assert provider["base_url"] == "https://ocr.example.com/v1"
    assert provider["models"] == ["vision-large", "vision-small"]
    assert provider["default_model"] == "vision-small"
    assert provider["ocr_mode"] == "vision_chat"
    assert provider["input_types"] == ["image", "pdf"]
    assert provider["supports_layout"] is True


def test_settings_store_selects_custom_voice_provider(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "voice_providers": [
                {
                    "id": "speaker",
                    "name": "Speaker",
                    "base_url": "https://speaker.example.com/v1",
                    "api_key": "speaker-key",
                    "stt_model": "whisper-large",
                    "tts_model": "tts-large",
                    "voice": "nova",
                    "format": "wav",
                    "enabled": True,
                }
            ],
            "voice": {"provider": "speaker", "enabled": True},
        }
    )

    assert settings["voice"]["provider"] == "speaker"
    assert settings["voice"]["base_url"] == "https://speaker.example.com/v1"
    assert settings["voice"]["api_key"] == "speaker-key"
    assert settings["voice"]["stt_model"] == "whisper-large"
    assert settings["voice"]["tts_model"] == "tts-large"
    assert settings["voice"]["voice"] == "nova"
    assert settings["voice"]["format"] == "wav"


def test_settings_store_allows_empty_stt_language_for_autodetect(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update({"voice": {"stt_language": ""}})

    assert settings["voice"]["stt_language"] == ""


def test_settings_store_respects_no_auth_custom_voice_provider(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "voice_providers": [
                {
                    "id": "local-voice",
                    "name": "Local Voice",
                    "base_url": "http://localhost:8000/v1",
                    "requires_api_key": False,
                    "stt_model": "whisper-local",
                    "tts_model": "",
                    "enabled": True,
                }
            ],
            "voice": {"provider": "local-voice", "enabled": True},
        }
    )

    assert settings["voice"]["provider"] == "local-voice"
    assert settings["voice"]["requires_api_key"] is False
    assert settings["voice"]["api_key"] == ""


def test_settings_store_selects_custom_ocr_provider(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.json"))

    settings = store.update(
        {
            "ocr_providers": [
                {
                    "id": "vision-ocr",
                    "name": "Vision OCR",
                    "base_url": "https://ocr.example.com/v1",
                    "requires_api_key": False,
                    "api_key": "",
                    "models": ["vision-ocr-model"],
                    "default_model": "vision-ocr-model",
                    "input_types": ["image", "pdf"],
                    "enabled": True,
                }
            ],
            "ocr": {"provider": "vision-ocr", "enabled": True},
        }
    )

    assert settings["ocr"]["provider"] == "vision-ocr"
    assert settings["ocr"]["base_url"] == "https://ocr.example.com/v1"
    assert settings["ocr"]["requires_api_key"] is False
    assert settings["ocr"]["default_model"] == "vision-ocr-model"
    assert settings["ocr"]["input_types"] == ["image", "pdf"]


def test_mask_secret_short_values():
    assert mask_secret("1234") == "****"
    assert mask_secret("123456789") == "1234...6789"
